import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from lookout import config
from lookout.checker import Checker
from lookout.store import Store
from lookout.web import create_app, normalize_host, normalize_image
from test_lookout import FakeDocker, FakeRegistry, fake_container


class FakeGitHub:
    def __init__(self, tags):
        self.tags = tags

    def latest(self, repo, prereleases=False):
        tag = self.tags[repo]
        if isinstance(tag, Exception):
            raise tag
        return {"tag": tag, "url": f"https://github.com/{repo}/releases/tag/{tag}", "notes": "", "published_at": None}


class FakeScheduler:
    checking = False
    next_run = 0

    def __init__(self):
        self.triggered = []

    def trigger(self, only=None):
        self.triggered.append(only)

    def reschedule(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("DISCORD_WEBHOOK_URL", "NTFY_URL", "GITHUB_REPOS", "GOTIFY_URL"):
        monkeypatch.delenv(k, raising=False)
    sent = []
    monkeypatch.setattr("lookout.checker.notify.send_all", lambda n, ups, errs: sent.append([u.key for u in ups]) or True)
    base = config.load(None)
    store = Store(str(tmp_path / "lookout.json"))
    reg = FakeRegistry({"nginx:1.25.3": "sha256:new", "lscr.io/linuxserver/sonarr:latest": "sha256:a"},
                       {"nginx": ["1.25.3", "1.27.2"]})
    gh = FakeGitHub({"a/b": "v1.0"})
    containers = [fake_container("web", "nginx:1.25.3", "sha256:old")]
    checker = Checker(base, store, docker_factory=lambda url: FakeDocker(containers), registry=reg, github=gh)
    sched = FakeScheduler()
    app = create_app(base, store, sched).test_client()
    return SimpleNamespace(store=store, checker=checker, reg=reg, gh=gh, sent=sent, app=app, sched=sched)


def test_add_check_ack_flow(env):
    r = env.app.post("/api/repos", json={"repos": "a/b\nhttps://github.com/a/b\nnot-a-repo"}).get_json()
    assert [i["repo"] for i in r["added"]] == ["a/b"]
    assert {s["reason"] for s in r["skipped"]} == {"Already watched", "Not a GitHub repo (use owner/name or its URL)"}
    r = env.app.post("/api/images", json={"images": ["https://hub.docker.com/r/linuxserver/sonarr", "lscr.io/linuxserver/sonarr"]}).get_json()
    assert [i["ref"] for i in r["added"]] == ["linuxserver/sonarr:latest", "lscr.io/linuxserver/sonarr:latest"]
    env.store.remove("images", r["added"][0]["id"])
    assert env.sched.triggered  # adding queues a quick check

    env.checker.run()   # first look sets baselines; only the local nginx container is out of date
    assert env.sent == [["docker:nginx:1.25.3:digest", "docker:nginx:1.25.3:tag"]]

    env.gh.tags["a/b"] = "v1.1"
    env.reg.digests["lscr.io/linuxserver/sonarr:latest"] = "sha256:b"
    env.checker.run()
    assert sorted(env.sent[-1]) == ["github:a/b", f"image:{env.store.data['images'][0]['id']}:digest"]
    env.checker.run()
    assert len(env.sent) == 2   # nothing new, nothing re-sent

    st = env.app.get("/api/status").get_json()
    assert st["counts"] == {"updates": 3, "errors": 0, "watching": 3}
    repo = st["repos"][0]
    assert (repo["status"], repo["installed"], repo["latest"]) == ("update", "v1.0", "v1.1")

    env.app.post(f"/api/repos/{repo['id']}/ack")
    env.app.post(f"/api/images/{st['images'][0]['id']}/ack")
    env.checker.run()
    st = env.app.get("/api/status").get_json()
    assert st["counts"]["updates"] == 1   # only the local container is left
    assert "github:a/b" not in env.store.data["notified"]


def test_unreachable_host_is_reported(env):
    def boom(url):
        raise ConnectionError("Connection refused")
    env.checker.docker_factory = boom
    env.checker.run()
    st = env.app.get("/api/status").get_json()
    assert st["hosts"][0]["ok"] is False and "Connection refused" in st["hosts"][0]["error"]
    assert st["counts"]["errors"] == 1


def test_settings_secrets(env):
    r = env.app.put("/api/settings", json={"discord_webhook_url": "https://discord.com/api/webhooks/1/abcdefgh",
                                           "interval": "2h", "check_new_tags": False}).get_json()
    assert r["discord_webhook_url"] == {"set": True, "hint": "…efgh"} and r["interval"] == "2h"
    r = env.app.put("/api/settings", json={"discord_webhook_url": ""}).get_json()
    assert r["discord_webhook_url"]["set"] is True      # blank keeps it
    r = env.app.put("/api/settings", json={"discord_webhook_url": None}).get_json()
    assert r["discord_webhook_url"]["set"] is False     # null clears it
    assert env.app.put("/api/settings", json={"interval": "whenever"}).status_code == 400
    assert env.app.get("/api/status").get_json()["interval"] == "2h"


def test_password(env, tmp_path):
    app = create_app(config.load(None), env.store, env.sched, password="pw").test_client()
    assert app.get("/api/status").status_code == 401
    assert app.get("/api/health").status_code == 200
    assert app.get("/api/status", auth=("any", "pw")).status_code == 200


def test_normalizers():
    assert normalize_host("192.168.1.5") == "tcp://192.168.1.5:2375"
    assert normalize_host("tcp://nas.lan:2376") == "tcp://nas.lan:2376"
    assert normalize_host("http://nas:2375/") == "tcp://nas:2375"
    assert normalize_host("bad host") is None
    assert normalize_image("ghcr.io/home-assistant/home-assistant:stable") == "ghcr.io/home-assistant/home-assistant:stable"
    assert normalize_image("https://hub.docker.com/_/nginx") == "nginx:latest"
    assert normalize_image("not an image") is None


def test_migrates_and_seeds(tmp_path):
    legacy = tmp_path / "state.json"
    legacy.write_text('{"github:a/b": "v3", "docker:nginx:latest:digest": "sha256:x", "last_run": 1}')
    store = Store(str(tmp_path / "lookout.json"), legacy_state=str(legacy))
    store.seed_repos(["a/b", "c/d"])
    assert [(r["repo"], r["installed"]) for r in store.data["repos"]] == [("a/b", "v3"), ("c/d", None)]
    store.remove("repos", store.data["repos"][1]["id"])
    store.seed_repos(["a/b", "c/d"])     # deleting a seeded repo sticks
    assert [r["repo"] for r in store.data["repos"]] == ["a/b"]
    assert store.data["notified"]["docker:nginx:latest:digest"] == "sha256:x"


def test_page_has_versioned_assets(env):
    from lookout import __version__
    html = env.app.get("/").get_data(as_text=True)
    assert f"app.js?v={__version__}" in html and "__V__" not in html
