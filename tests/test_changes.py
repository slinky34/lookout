import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lookout import changes, config
from lookout.checker import Checker
from lookout.store import Store
from test_lookout import FakeDocker, fake_container


def rel(tag, when, body="", pre=False):
    return {"tag_name": tag, "name": tag, "published_at": when, "body": body, "html_url": f"https://gh/{tag}",
            "prerelease": pre, "draft": False}


RELEASES = [   # newest first, like GitHub
    rel("v3.0.0", "2026-11-10T00:00:00Z", "## Breaking changes\n- Config moved, migration required"),
    rel("v2.5.0-rc1", "2026-11-05T00:00:00Z", "pre", pre=True),
    rel("v2.4.1", "2026-11-01T00:00:00Z", "- Fix XSS in the login page (CVE-2026-1234)"),
    rel("v2.4.0", "2026-10-20T00:00:00Z", "- Faster thumbnails"),
    rel("v2.3.0", "2026-10-01T00:00:00Z", "- Read our SECURITY.md"),
]


class FakeGH:
    def releases(self, repo, per_page=60):
        return RELEASES


class FakeReg:
    def __init__(self):
        self.configs = {
            "sha256:old": {"created": "2026-10-02T00:00:00Z",
                           "labels": {changes.SOURCE_LABEL: "https://github.com/acme/app.git", changes.VERSION_LABEL: "2.3.0"}},
            "latest": {"created": "2026-11-10T01:00:00Z",
                       "labels": {changes.SOURCE_LABEL: "https://github.com/acme/app", changes.VERSION_LABEL: "3.0.0"}},
        }
        self.digests = {"acme/app:latest": "sha256:new"}

    def image_config(self, ref, reference=None):
        return self.configs[reference or ref.tag]

    def manifest_digest(self, ref):
        return self.digests[str(ref)]

    def list_tags(self, ref):
        return []


def test_flags_and_reasons():
    info = changes.summarize("acme/app", RELEASES[:4], "v2.3.0", "v3.0.0")
    assert info["flags"] == ["security", "breaking", "major"]
    assert info["why"]["security"] == ["Fix XSS in the login page (CVE-2026-1234)"]
    assert "migration required" in info["why"]["breaking"][-1]
    assert info["why"]["major"] == ["v2.3.0 → v3.0.0"]
    # "SECURITY.md" alone isn't a security fix
    assert changes.summarize("acme/app", [RELEASES[4]], "2.2", "2.3")["flags"] == []


def test_releases_between_by_tag_and_by_date():
    gh = FakeGH()
    by_tag = changes.releases_between(gh, "acme/app", "2.3.0", "v3.0.0")
    assert [r["tag_name"] for r in by_tag] == ["v3.0.0", "v2.4.1", "v2.4.0"]      # pre-release skipped
    from datetime import datetime, timezone
    by_date = changes.releases_between(gh, "acme/app", "unknown", "unknown",
                                       old_time=datetime(2026, 10, 15, tzinfo=timezone.utc),
                                       new_time=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert [r["tag_name"] for r in by_date] == ["v2.4.1", "v2.4.0"]
    only_new = changes.releases_between(gh, "acme/app", None, "v2.4.1")
    assert [r["tag_name"] for r in only_new] == ["v2.4.1"]


def test_for_image_uses_labels():
    info = changes.for_image(FakeReg(), FakeGH(), "acme/app:latest", "sha256:old")
    assert info["repo"] == "acme/app" and (info["from"], info["to"]) == ("2.3.0", "3.0.0")
    assert info["flags"] == ["security", "breaking", "major"]
    assert [r["tag"] for r in info["releases"]] == ["v3.0.0", "v2.4.1", "v2.4.0"]


def test_image_without_source_says_so():
    reg = FakeReg()
    reg.configs["latest"]["labels"] = {}
    reg.configs["sha256:old"]["labels"] = {}
    info = changes.for_image(reg, FakeGH(), "acme/app:latest", "sha256:old")
    assert info["releases"] == [] and "doesn't link to its source" in info["note"]


def test_checker_attaches_changes_and_flags(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("lookout.checker.notify.send_all", lambda n, ups, errs: sent.extend(ups) or True)
    store = Store(str(tmp_path / "s.json"))
    containers = [fake_container("app", "acme/app:latest", "sha256:old")]
    reg = FakeReg()
    calls = {"n": 0}
    real = reg.image_config

    def counting(ref, reference=None):
        calls["n"] += 1
        return real(ref, reference)
    reg.image_config = counting

    class GH(FakeGH):
        def latest(self, repo, pre=False):
            return None

    checker = Checker(config.load(None), store, docker_factory=lambda u: FakeDocker(containers), registry=reg, github=GH())
    checker.run()
    row = store.data["results"]["local"]["images"][0]
    assert row["changes"]["flags"] == ["security", "breaking", "major"]
    assert sent[0].flags == ["security", "breaking", "major"]
    first = calls["n"]
    checker.run()
    assert calls["n"] == first        # cached: no second lookup
