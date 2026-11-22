import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from lookout import config, imageref, versions
from lookout.docker_watch import DockerWatcher
from lookout.models import Update
from lookout.notify import Discord


# -- image references -------------------------------------------------------

@pytest.mark.parametrize("text,registry,repo,tag", [
    ("nginx", "registry-1.docker.io", "library/nginx", "latest"),
    ("nginx:1.25", "registry-1.docker.io", "library/nginx", "1.25"),
    ("docker.io/linuxserver/plex:latest", "registry-1.docker.io", "linuxserver/plex", "latest"),
    ("lscr.io/linuxserver/sonarr", "lscr.io", "linuxserver/sonarr", "latest"),
    ("ghcr.io/slinky34/hearth-amd64:0.12.0", "ghcr.io", "slinky34/hearth-amd64", "0.12.0"),
    ("localhost:5000/app:dev", "localhost:5000", "app", "dev"),
    ("myregistry.local:5000/team/app", "myregistry.local:5000", "team/app", "latest"),
])
def test_parse_ref(text, registry, repo, tag):
    r = imageref.parse(text)
    assert (r.registry, r.repository, r.tag) == (registry, repo, tag)


def test_parse_digest_and_names():
    r = imageref.parse("nginx@sha256:abc")
    assert r.digest == "sha256:abc" and r.tag is None
    assert str(imageref.parse("nginx:1.2")) == "nginx:1.2"
    assert str(imageref.parse("ghcr.io/a/b")) == "ghcr.io/a/b:latest"


# -- versions ---------------------------------------------------------------

def test_newest_tag_same_shape():
    tags = ["1.25.3", "1.25.4", "1.27.0", "1.27.0-alpine", "1.28.0-rc1", "2", "latest", "1.9.9"]
    assert versions.newest_tag("1.25.3", tags) == "1.27.0"
    assert versions.newest_tag("1.25.3-alpine", ["1.26.0-alpine", "1.27.0"]) == "1.26.0-alpine"
    assert versions.newest_tag("v1.2", ["v1.10", "1.11", "v1.3"]) == "v1.10"
    assert versions.newest_tag("latest", tags) is None
    assert versions.newest_tag("1.27.0", tags) is None


# -- config -----------------------------------------------------------------

def test_interval():
    assert config.parse_interval("6h") == 21600
    assert config.parse_interval("1h30m") == 5400
    assert config.parse_interval("10") == 60  # floor of one minute
    with pytest.raises(ValueError):
        config.parse_interval("soon")


def test_config_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK", "https://discord.example/hook")
    monkeypatch.setenv("GITHUB_REPOS", "a/b, c/d")
    p = tmp_path / "c.yaml"
    p.write_text("interval: 30m\nnotify:\n  discord:\n    webhook_url: ${HOOK}\n  ntfy:\n    url: ${MISSING}\n")
    cfg = config.load(str(p))
    assert cfg["interval_seconds"] == 1800
    assert cfg["notify"]["discord"]["webhook_url"] == "https://discord.example/hook"
    assert cfg["notify"]["ntfy"]["url"] == ""
    assert cfg["github"]["repos"] == ["a/b", "c/d"]
    assert cfg["docker"]["check_new_tags"] is True  # default kept


# -- docker watcher with fakes ------------------------------------------------

class FakeRegistry:
    def __init__(self, digests, tags=None):
        self.digests, self.tags = digests, tags or {}

    def manifest_digest(self, ref):
        return self.digests[str(ref)]

    def list_tags(self, ref):
        return self.tags.get(ref.name, [])


def fake_container(name, image, digest, labels=None):
    img = SimpleNamespace(attrs={"RepoDigests": [f"x@{digest}"] if digest else []})
    return SimpleNamespace(name=name, labels=labels or {}, image=img,
                           attrs={"Config": {"Image": image}, "Image": f"id-{name}"})


class FakeDocker:
    def __init__(self, containers):
        by_id = {c.attrs["Image"]: c.image for c in containers}
        self.containers = SimpleNamespace(list=lambda all=False: containers)
        self.images = SimpleNamespace(get=lambda i: by_id[i])


def test_docker_detects_digest_and_tag_updates():
    cs = [
        fake_container("web", "nginx:1.25.3", "sha256:old"),
        fake_container("web2", "nginx:1.25.3", "sha256:old"),
        fake_container("plex", "lscr.io/linuxserver/plex", "sha256:same"),
        fake_container("trove", "trove:latest", None),                     # built locally
        fake_container("skip", "redis", "sha256:x", {"lookout.enable": "false"}),
    ]
    reg = FakeRegistry(
        {"nginx:1.25.3": "sha256:new", "lscr.io/linuxserver/plex:latest": "sha256:same"},
        {"nginx": ["1.25.3", "1.26.1", "1.27.2"]},
    )
    w = DockerWatcher({"check_new_tags": True}, reg, FakeDocker(cs))
    updates, errors = w.check()
    assert errors == []
    kinds = {(u.kind, u.title, u.latest) for u in updates}
    assert kinds == {("digest", "nginx:1.25.3", "1.25.3"), ("tag", "nginx", "1.27.2")}
    assert all(u.containers == ["web", "web2"] for u in updates)


def test_discord_payload(monkeypatch):
    sent = []

    class R:
        def raise_for_status(self):
            pass

    monkeypatch.setattr("lookout.notify.requests.post", lambda url, json, timeout: sent.append(json) or R())
    ups = [Update(kind="release", key=f"k{i}", value="v2", title=f"o/r{i}", current="v1", latest="v2",
                  url="https://x") for i in range(12)]
    Discord({"webhook_url": "https://hook"}).send(ups, [])
    assert len(sent) == 2 and len(sent[0]["embeds"]) == 10
    assert sent[0]["content"] == "**12 updates available**"
    assert sent[0]["embeds"][0]["description"].startswith("v1 → v2")
