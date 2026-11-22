import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from fake_portainer import TOKEN, FakePortainer
from lookout import config
from lookout.checker import Checker
from lookout.portainer import PortainerClient, PortainerError
from lookout.store import Store
from lookout.web import create_app
from test_dashboard import FakeGitHub, FakeScheduler
from test_lookout import FakeDocker, fake_container

OLD = "sha256:" + "0" * 64


class AnyNewRegistry:
    """Every image has a newer digest than the fake's OLD one; radarr/agent are current."""
    current = {"lscr.io/linuxserver/radarr:latest", "portainer/agent:latest"}

    def manifest_digest(self, ref):
        return OLD if str(ref) in self.current else "sha256:" + "1" * 64

    def list_tags(self, ref):
        return ["1.25.3", "1.27.2"] if ref.name == "nginx" else []


@pytest.fixture
def fp():
    with FakePortainer() as f:
        yield f


def make(tmp_path, fp, docker_factory, monkeypatch, **portainer):
    monkeypatch.setattr("lookout.checker.notify.send_all", lambda *a: True)
    for k in ("PORTAINER_URL", "PORTAINER_TOKEN", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    base = config.load(None)
    store = Store(str(tmp_path / "lookout.json"))
    store.data["settings"] = {"portainer": {"url": fp.url, "token": TOKEN, **portainer}}
    checker = Checker(base, store, docker_factory=docker_factory, registry=AnyNewRegistry(), github=FakeGitHub({}))
    sched = FakeScheduler()
    app = create_app(base, store, sched).test_client()
    return store, checker, app, sched


def no_socket(url):
    raise FileNotFoundError("No such file or directory")


def test_bad_token(fp):
    with pytest.raises(PortainerError, match="rejected the access token"):
        PortainerClient(fp.url, "nope").index()


def test_watches_every_environment_and_classifies(tmp_path, fp, monkeypatch):
    store, checker, app, _ = make(tmp_path, fp, no_socket, monkeypatch)
    checker.run()
    st = app.get("/api/status").get_json()
    hosts = {h["id"]: h for h in st["hosts"]}
    assert set(hosts) == {"local", "pt-1", "pt-2"}       # k8s environment ignored
    assert hosts["pt-1"]["name"] == "local" and hosts["pt-1"]["source"] == "portainer"

    rows = {r["ref"]: r for r in hosts["pt-1"]["images"]}
    sonarr = rows["lscr.io/linuxserver/sonarr:latest"]
    assert sonarr["status"] == "update" and sonarr["can_update"]
    assert sonarr["members"][0]["manager"]["type"] == "stack"
    assert sonarr["members"][0]["manager"]["control"] == "total"
    immich = rows["ghcr.io/immich-app/immich-server:release"]
    assert immich["members"][0]["manager"]["control"] == "limited" and not immich["can_update"]
    nginx = rows["nginx:1.25.3"]
    assert nginx["members"][0]["manager"]["type"] == "container" and nginx["newest_tag"] == "1.27.2"
    assert rows["lscr.io/linuxserver/radarr:latest"]["status"] == "ok"
    assert st["portainer"]["connected"] and st["portainer"]["error"] is None


def test_same_docker_is_not_listed_twice(tmp_path, fp, monkeypatch):
    # This server's Docker is also Portainer environment 1 (same container ids).
    local = [fake_container("sonarr", "lscr.io/linuxserver/sonarr:latest", OLD)]
    local[0].id = "a" * 64
    store, checker, app, _ = make(tmp_path, fp, lambda url: FakeDocker(local), monkeypatch)
    checker.run()
    st = app.get("/api/status").get_json()
    hosts = {h["id"]: h for h in st["hosts"]}
    assert set(hosts) == {"local", "pt-2"}
    assert hosts["local"]["portainer_env"] == "local"
    # And Portainer still knows this server's stack.
    assert hosts["local"]["images"][0]["members"][0]["manager"]["stack"] == "media"
    checker.run(only={"pt-1"})          # a partial check doesn't bring the duplicate back
    assert "pt-1" not in store.data["results"]


def test_update_plan_and_run(tmp_path, fp, monkeypatch):
    store, checker, app, sched = make(tmp_path, fp, no_socket, monkeypatch)
    checker.run()

    plan = app.post("/api/hosts/pt-1/update", json={"ref": "lscr.io/linuxserver/sonarr:latest"}).get_json()["plan"]
    assert [s["text"] for s in plan["steps"]] == ["Redeploy stack “media” with a fresh pull (sonarr) on local"]
    assert fp.state["calls"] == []      # planning changes nothing

    r = app.post("/api/hosts/pt-1/update", json={"ref": "lscr.io/linuxserver/sonarr:latest", "confirm": True}).get_json()
    assert r["results"] == [{"target": "stack media", "ok": True}]
    method, path, args, body = fp.state["calls"][-1]
    assert (method, path, args) == ("PUT", "/api/stacks/5", {"endpointId": "1"})
    assert body["RepullImageAndRedeploy"] is True and body["PullImage"] is True
    assert body["StackFileContent"].startswith("services:") and body["Env"] == [{"name": "TZ", "value": "UTC"}]
    assert sched.triggered[-1] == {"pt-1"}

    app.post("/api/hosts/pt-1/update", json={"ref": "nginx:1.25.3", "confirm": True})
    assert fp.state["calls"][-1][:2] == ("POST", f"/api/docker/1/containers/{'d' * 64}/recreate")
    assert fp.state["calls"][-1][3] == {"PullImage": True}

    app.post("/api/hosts/pt-2/update", json={"ref": "lscr.io/linuxserver/qbittorrent:latest", "confirm": True})
    assert fp.state["calls"][-1][:3] == ("PUT", "/api/stacks/9/git/redeploy", {"endpointId": "2"})

    r = app.post("/api/hosts/pt-1/update", json={"ref": "ghcr.io/immich-app/immich-server:release"}).get_json()
    assert r["plan"]["steps"] == [] and "limited control" in r["plan"]["skipped"][0]["why"]
    r = app.post("/api/hosts/pt-1/update", json={"ref": "ghcr.io/immich-app/immich-server:release", "confirm": True})
    assert r.status_code == 400


def test_updates_can_be_turned_off(tmp_path, fp, monkeypatch):
    store, checker, app, _ = make(tmp_path, fp, no_socket, monkeypatch, allow_updates=False)
    checker.run()
    r = app.post("/api/hosts/pt-1/update", json={"ref": "nginx:1.25.3", "confirm": True})
    assert r.status_code == 400 and fp.state["calls"] == []
    rows = {x["ref"]: x for x in store.data["results"]["pt-1"]["images"]}
    assert rows["nginx:1.25.3"]["can_update"] is False


def test_portainer_test_endpoint(tmp_path, fp, monkeypatch):
    _, _, app, _ = make(tmp_path, fp, no_socket, monkeypatch)
    r = app.post("/api/portainer/test").get_json()
    assert (r["environments"], r["containers"], r["stacks"]) == (2, 6, 2)
    assert {d["name"]: (d["containers"], d["error"]) for d in r["details"]} == {
        "local": (4, None), "nas": (2, None),
        "k8s": (0, "Not a Docker environment (Kubernetes/Azure aren't supported)")}


def test_saving_portainer_settings_checks_right_away(tmp_path, fp, monkeypatch):
    _, _, app, sched = make(tmp_path, fp, no_socket, monkeypatch)
    app.put("/api/settings", json={"interval": "3h"})
    assert sched.triggered == []                       # unrelated setting: no extra check
    app.put("/api/settings", json={"portainer_url": fp.url + "/"})
    assert sched.triggered == [None]                   # full check queued


def test_unreadable_environment_shows_its_error(tmp_path, fp, monkeypatch):
    fp.state["endpoints"][1]["Status"] = 2             # nas is down
    store, checker, app, _ = make(tmp_path, fp, no_socket, monkeypatch)
    checker.run()
    hosts = {h["id"]: h for h in app.get("/api/status").get_json()["hosts"]}
    assert hosts["pt-2"]["ok"] is False and "down" in hosts["pt-2"]["error"]
    assert len(hosts["pt-1"]["images"]) == 4
