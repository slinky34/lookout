"""One check pass: Docker hosts, watched images, GitHub repos; then notify what's new."""

from __future__ import annotations

import hashlib
import logging
import time
from copy import deepcopy

from . import config, imageref, notify, versions
from .docker_watch import DockerWatcher, image_url, key_prefix
from .github_watch import GitHubClient
from .models import Update
from .registry import RegistryClient
from .store import Store

log = logging.getLogger(__name__)


def effective_config(base: dict, store: Store) -> dict:
    with store.lock:
        cfg = config._merge(base, deepcopy(store.data.get("settings") or {}))
    try:
        cfg["interval_seconds"] = config.parse_interval(str(cfg["interval"]))
    except ValueError:
        cfg["interval_seconds"] = base["interval_seconds"]
    return cfg


def host_list(cfg: dict, store: Store) -> list[dict]:
    hosts = []
    if (cfg.get("docker") or {}).get("enabled", True):
        hosts.append({"id": "local", "name": "This server", "url": (cfg.get("docker") or {}).get("host")})
    with store.lock:
        hosts += deepcopy(store.data["hosts"])
    return hosts


class Checker:
    def __init__(self, base_cfg: dict, store: Store, docker_factory=None, registry=None, github=None):
        self.base_cfg = base_cfg
        self.store = store
        self.docker_factory = docker_factory   # tests: (url) -> fake client
        self._registry = registry
        self._github = github

    def run(self, only: set[str] | None = None, dry_run: bool = False) -> list[Update]:
        """Check everything, or just the repo/image/host ids in `only`."""
        cfg = effective_config(self.base_cfg, self.store)
        registry = self._registry or RegistryClient(cfg.get("registries"))
        github = self._github or GitHubClient((cfg.get("github") or {}).get("token"))

        pending: list[Update] = []
        errors: list[str] = []
        clean_prefixes: list[str] = []   # sources checked cleanly; their stale keys get pruned

        # -- Docker hosts --------------------------------------------------
        for host in host_list(cfg, self.store):
            if only is not None and host["id"] not in only:
                continue
            try:
                client = self.docker_factory(host["url"]) if self.docker_factory else None
                w = DockerWatcher(cfg.get("docker") or {}, registry, client,
                                  host_id=host["id"], host_name=host["name"], url=host["url"])
                result, ups, errs = w.scan()
            except Exception as e:
                msg = _docker_error(e)
                log.warning("%s: %s", host["name"], msg)
                errors.append(f"{host['name']}: {msg}")
                with self.store.lock:
                    prev = self.store.data["results"].get(host["id"]) or {}
                    self.store.data["results"][host["id"]] = {
                        **prev, "id": host["id"], "name": host["name"], "url": host["url"],
                        "ok": False, "error": msg, "checked_at": int(time.time()),
                        "images": prev.get("images") or [], "ignored": prev.get("ignored") or [],
                    }
                continue
            pending += ups
            errors += errs
            if not errs:
                clean_prefixes.append(key_prefix(host["id"]))
            with self.store.lock:
                self.store.data["results"][host["id"]] = result

        # -- watched images ------------------------------------------------
        with self.store.lock:
            images = deepcopy(self.store.data["images"])
        check_tags = bool((cfg.get("docker") or {}).get("check_new_tags", True))
        for item in images:
            if only is not None and item["id"] not in only:
                continue
            ups, err = self._check_image(item, registry, check_tags)
            if err:
                errors.append(err)
            else:
                clean_prefixes.append(f"image:{item['id']}:")
            pending += ups
            self._write_back("images", item)

        # -- GitHub repos --------------------------------------------------
        with self.store.lock:
            repos = deepcopy(self.store.data["repos"])
        for item in repos:
            if only is not None and item["id"] not in only:
                continue
            up, err = self._check_repo(item, github)
            if err:
                errors.append(err)
            else:
                clean_prefixes.append(f"github:{item['repo']}")
            if up:
                pending.append(up)
            self._write_back("repos", item)

        return self._announce(cfg, pending, errors, clean_prefixes, dry_run, full=only is None)

    # ------------------------------------------------------------------

    def _write_back(self, kind: str, item: dict):
        with self.store.lock:
            current = self.store.find(kind, item["id"])
            if current is None or current.get("rev", 0) != item.get("rev", 0):
                return   # removed or edited (e.g. "Mark updated") while this check ran
            for k, v in item.items():
                if k in ("label", "include_prereleases", "ref", "repo"):
                    continue   # user-editable; the dashboard may have changed them meanwhile
                if k in ("installed", "installed_digest") and current.get(k) is not None:
                    continue   # only fill the first baseline; "Mark updated" owns it after that
                current[k] = v

    def _check_image(self, item: dict, registry, check_tags: bool):
        item["checked_at"] = int(time.time())
        try:
            ref = imageref.parse(item["ref"])
            item["url"] = image_url(ref)
            remote = registry.manifest_digest(ref)
            item["latest_digest"] = remote
            if item.get("installed_digest") is None:
                item["installed_digest"] = remote   # first look: that's what you run
            newest = None
            if check_tags and ref.tag and versions.parse(ref.tag):
                newest = versions.newest_tag(ref.tag, registry.list_tags(ref))
            item["newest_tag"] = newest
            item["error"] = None
        except Exception as e:
            item["error"] = str(e)
            log.warning("image %s: %s", item["ref"], e)
            return [], f"{item.get('label') or item['ref']}: {e}"

        name = item.get("label") or item["ref"]
        where = [item["label"]] if item.get("label") else []
        ups = []
        if item["latest_digest"] != item["installed_digest"]:
            ups.append(Update(kind="digest", key=f"image:{item['id']}:digest", value=item["latest_digest"],
                              title=item["ref"], current=ref.tag, latest=ref.tag, url=item["url"],
                              containers=where))
        if item["newest_tag"]:
            ups.append(Update(kind="tag", key=f"image:{item['id']}:tag", value=item["newest_tag"],
                              title=ref.name if not item.get("label") else f"{name} ({ref.name})",
                              current=ref.tag, latest=item["newest_tag"], url=item["url"]))
        return ups, None

    def _check_repo(self, item: dict, github):
        item["checked_at"] = int(time.time())
        try:
            rel = github.latest(item["repo"], bool(item.get("include_prereleases")))
            item["error"] = None
        except Exception as e:
            item["error"] = str(e)
            log.warning("GitHub %s: %s", item["repo"], e)
            return None, f"GitHub {item['repo']}: {e}"
        if rel is None:
            item.update(latest=None, url=f"https://github.com/{item['repo']}", notes=None)
            return None, None
        item.update(latest=rel["tag"], url=rel["url"], notes=rel["notes"], published_at=rel["published_at"])
        if item.get("installed") is None:
            item["installed"] = rel["tag"]   # first look: don't announce an old release
            return None, None
        if item["installed"] != rel["tag"]:
            title = item["repo"] if not item.get("label") else f"{item['label']} ({item['repo']})"
            return Update(kind="release", key=f"github:{item['repo']}", value=rel["tag"], title=title,
                          current=item["installed"], latest=rel["tag"], url=rel["url"],
                          notes=rel["notes"]), None
        return None, None

    def _announce(self, cfg, pending, errors, clean_prefixes, dry_run, full):
        with self.store.lock:
            notified = self.store.data["notified"]
            fresh = [u for u in pending if notified.get(u.key) != u.value]
            keys = {u.key for u in pending}
            for k in list(notified):
                if k not in keys and any(k.startswith(p) for p in clean_prefixes):
                    del notified[k]

            errors_to_send: list[str] = []
            if full:
                if errors and cfg.get("notify_errors"):
                    digest = hashlib.sha256("\n".join(sorted(errors)).encode()).hexdigest()
                    if self.store.data.get("errors_last") != digest:
                        errors_to_send = errors
                        self.store.data["errors_last"] = digest
                elif not errors:
                    self.store.data["errors_last"] = None
                self.store.data["last_run"] = int(time.time())

        log.info("check done: %d pending, %d new, %d error(s)", len(pending), len(fresh), len(errors))
        if dry_run:
            for u in fresh:
                print(f"[{u.kind}] {u.title}: {u.summary}" + (f"  ({', '.join(u.containers)})" if u.containers else ""))
            for e in errors:
                print(f"[error] {e}")
            return fresh

        if fresh or errors_to_send:
            if notify.send_all(notify.build(cfg.get("notify") or {}), fresh, errors_to_send):
                with self.store.lock:
                    for u in fresh:
                        self.store.data["notified"][u.key] = u.value
            elif errors_to_send:
                with self.store.lock:
                    self.store.data["errors_last"] = None
        self.store.save()
        return fresh


def _docker_error(e: Exception) -> str:
    text = str(e)
    if "FileNotFoundError" in text or "No such file" in text:
        return "Docker socket not found. Mount /var/run/docker.sock into the container."
    if "Connection refused" in text:
        return "Connection refused. Is the Docker API (or socket proxy) running on that address and port?"
    if "timed out" in text.lower() or "timeout" in text.lower():
        return "No answer. Check the address, and that the port is open on that machine."
    if "nodename nor servname" in text or "Name or service not known" in text or "getaddrinfo" in text:
        return "Can't find that host name. Try its IP address."
    if "Max retries" in text:
        return "Can't reach Docker at that address."
    if "Permission denied" in text:
        return "No permission to use the Docker socket"
    return text
