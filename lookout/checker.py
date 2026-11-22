"""One check pass: Docker hosts, watched images, GitHub repos; then notify what's new."""

from __future__ import annotations

import hashlib
import logging
import time
from copy import deepcopy

from . import changes, config, imageref, notify, versions
from .docker_watch import DockerWatcher, image_url, key_prefix
from .github_watch import GitHubClient
from .models import Update
from .portainer import PortainerClient, PortainerDocker, classify
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


def portainer_client(cfg: dict):
    p = cfg.get("portainer") or {}
    if not p.get("url") or not p.get("token"):
        return None
    return PortainerClient(p["url"], p["token"], verify_tls=bool(p.get("verify_tls")))


def annotate(row: dict, idx) -> None:
    """Attach how each container is managed, plus a summary for the row."""
    for m in row.get("members") or []:
        m["manager"] = classify(m, idx)
    mgrs = [m["manager"] for m in row.get("members") or []]
    row["can_update"] = bool(mgrs) and any(x["can_update"] for x in mgrs)
    # Re-pulling only helps when the tag itself got a new image.
    row["update_fixes"] = bool(row.get("digest_update"))


class Checker:
    def __init__(self, base_cfg: dict, store: Store, docker_factory=None, registry=None, github=None,
                 portainer=None):
        self.base_cfg = base_cfg
        self.store = store
        self.docker_factory = docker_factory   # tests: (url) -> fake client
        self._registry = registry
        self._github = github
        self._portainer = portainer            # tests: a fake PortainerClient

    def portainer(self, cfg: dict):
        return self._portainer if self._portainer is not None else portainer_client(cfg)

    def portainer_index(self, cfg: dict):
        client = self.portainer(cfg)
        if client is None:
            return None, None
        try:
            return client.index(), None
        except Exception as e:
            log.warning("Portainer: %s", e)
            return None, str(e)

    def run(self, only: set[str] | None = None, dry_run: bool = False) -> list[Update]:
        """Check everything, or just the repo/image/host ids in `only`."""
        cfg = effective_config(self.base_cfg, self.store)
        registry = self._registry or RegistryClient(cfg.get("registries"))
        github = self._github or GitHubClient((cfg.get("github") or {}).get("token"))

        pending: list[Update] = []
        errors: list[str] = []
        clean_prefixes: list[str] = []   # sources checked cleanly; their stale keys get pruned
        scanned: list[dict] = []

        # -- Docker hosts --------------------------------------------------
        pcfg = cfg.get("portainer") or {}
        want_hosts = only is None or any(not (i in {x["id"] for x in self.store.data["images"]}
                                              or i in {x["id"] for x in self.store.data["repos"]}) for i in only)
        idx, perr = self.portainer_index(cfg) if want_hosts else (None, None)
        if perr:
            errors.append(f"Portainer: {perr}")

        def scan(host, client):
            try:
                if client is None and self.docker_factory:
                    client = self.docker_factory(host["url"])
                w = DockerWatcher(cfg.get("docker") or {}, registry, client,
                                  host_id=host["id"], host_name=host["name"], url=host.get("url"))
                result, ups, errs = w.scan()
            except Exception as e:
                msg = _docker_error(e)
                log.warning("%s: %s", host["name"], msg)
                errors.append(f"{host['name']}: {msg}")
                with self.store.lock:
                    prev = self.store.data["results"].get(host["id"]) or {}
                    self.store.data["results"][host["id"]] = {
                        **prev, "id": host["id"], "name": host["name"], "url": host.get("url"),
                        "source": host.get("source", "docker"),
                        "ok": False, "error": msg, "checked_at": int(time.time()),
                        "images": prev.get("images") or [], "ignored": prev.get("ignored") or [],
                    }
                return None
            pending.extend(ups)
            errors.extend(errs)
            if not errs:
                clean_prefixes.append(key_prefix(host["id"]))
            result["source"] = host.get("source", "docker")
            scanned.append(result)
            with self.store.lock:
                prev = self.store.data["results"].get(host["id"]) or {}
                for k in ("portainer_env", "portainer_env_id"):
                    if k in prev:
                        result[k] = prev[k]
                self.store.data["results"][host["id"]] = result
            return result

        direct_ids: dict[str, set] = {}   # host id -> container ids seen directly
        for host in host_list(cfg, self.store):
            if only is not None and host["id"] not in only:
                continue
            result = scan(host, None)
            if result:
                direct_ids[host["id"]] = {m["id"] for row in result["images"]
                                          for m in row.get("members") or [] if m.get("id")}
                direct_ids[host["id"]] |= set(result.get("ignored_ids") or [])

        # Every Portainer environment, through Portainer (no socket proxies needed).
        if pcfg.get("watch_all") and idx is not None:
            client = self.portainer(cfg)
            seen_pt: set[str] = set()
            for ep_id, ep_name in sorted(idx.endpoints.items()):
                hid = f"pt-{ep_id}"
                ep_ids = {cid for cid, e in idx.containers.items() if e["endpoint"] == ep_id}
                seen_pt.add(hid)
                if only is not None and hid not in only:
                    continue
                if ep_id in idx.errors:
                    msg = idx.errors[ep_id]
                    errors.append(f"{ep_name}: {msg}")
                    with self.store.lock:
                        prev = self.store.data["results"].get(hid) or {}
                        self.store.data["results"][hid] = {
                            **prev, "id": hid, "name": ep_name, "url": None, "source": "portainer",
                            "ok": False, "error": msg, "checked_at": int(time.time()),
                            "images": prev.get("images") or [], "ignored": prev.get("ignored") or [],
                        }
                    continue
                same = next((h for h, ids in direct_ids.items() if ids and ids & ep_ids), None)
                if same is None and only is not None:
                    # Partial check: rely on the link found by the last full check.
                    with self.store.lock:
                        same = next((k for k, r in self.store.data["results"].items()
                                     if not k.startswith("pt-") and r.get("portainer_env_id") == ep_id), None)
                if same:
                    # Already read directly (e.g. this server); just note the link.
                    with self.store.lock:
                        r = self.store.data["results"].get(same)
                        if r is not None:
                            r["portainer_env"] = ep_name
                            r["portainer_env_id"] = ep_id
                        self.store.data["results"].pop(hid, None)
                    continue
                scan({"id": hid, "name": ep_name, "url": None, "source": "portainer"},
                     PortainerDocker(client, ep_id))
            if only is None:
                with self.store.lock:
                    for k in [k for k in self.store.data["results"] if k.startswith("pt-") and k not in seen_pt]:
                        del self.store.data["results"][k]
        elif only is None and not perr:
            with self.store.lock:
                for k in [k for k in self.store.data["results"] if k.startswith("pt-")]:
                    del self.store.data["results"][k]

        # -- what manages each container (Portainer stack, compose, standalone) --
        with self.store.lock:
            for result in scanned:
                for row in result["images"]:
                    annotate(row, idx)
                    if not pcfg.get("allow_updates", True):
                        row["can_update"] = False
                result["portainer_error"] = perr

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

        try:
            self._attach_changes(registry, github, scanned, pending, only)
        except Exception:
            log.exception("looking up release notes failed")
        return self._announce(cfg, pending, errors, clean_prefixes, dry_run, full=only is None)

    # ------------------------------------------------------------------

    MAX_LOOKUPS = 25          # new release-note lookups per check (rate limits)
    RETRY_FAILED = 6 * 3600   # look again this long after a failed lookup

    def _attach_changes(self, registry, github, scanned, pending, only):
        """Release notes + importance flags for each pending update, cached by
        (what you run, what's available) so each is looked up once."""
        jobs = []   # (cache key, compute fn, where to attach, update keys)
        for result in scanned:
            prefix = key_prefix(result["id"])
            for row in result["images"]:
                if row.get("status") != "update" or not row.get("local_digest"):
                    continue
                ref = imageref.parse(row["ref"])
                new_ref = str(imageref.ImageRef(ref.registry, ref.repository, row["newest_tag"], None)) \
                    if row.get("newest_tag") else None
                key = f"img|{ref}|{row['local_digest']}|{new_ref or row.get('remote_digest')}"
                jobs.append((key, lambda r=row, n=new_ref: changes.for_image(registry, github, r["ref"], r["local_digest"], n),
                             ("results", result["id"], row["ref"]),
                             [f"{prefix}{ref}:digest", f"{prefix}{ref}:tag"]))
        with self.store.lock:
            images = deepcopy(self.store.data["images"])
            repos = deepcopy(self.store.data["repos"])
        for i in images:
            if (only is not None and i["id"] not in only) or i.get("error") or not i.get("latest_digest"):
                continue
            if i.get("installed_digest") == i.get("latest_digest") and not i.get("newest_tag"):
                continue
            ref = imageref.parse(i["ref"])
            new_ref = str(imageref.ImageRef(ref.registry, ref.repository, i["newest_tag"], None)) if i.get("newest_tag") else None
            key = f"img|{ref}|{i['installed_digest']}|{new_ref or i['latest_digest']}"
            jobs.append((key, lambda it=i, n=new_ref: changes.for_image(registry, github, it["ref"], it["installed_digest"], n),
                         ("images", i["id"]), [f"image:{i['id']}:digest", f"image:{i['id']}:tag"]))
        for r in repos:
            if (only is not None and r["id"] not in only) or r.get("error"):
                continue
            if not r.get("latest") or r.get("installed") == r.get("latest"):
                continue
            key = f"gh|{r['repo']}|{r['installed']}|{r['latest']}"
            jobs.append((key, lambda it=r: changes.for_repo(github, it["repo"], it["installed"], it["latest"],
                                                            bool(it.get("include_prereleases"))),
                         ("repos", r["id"]), [f"github:{r['repo']}"]))

        by_update_key = {}
        lookups = 0
        now = int(time.time())
        with self.store.lock:
            cache = self.store.data.setdefault("changes", {})
        for key, compute, where, ukeys in jobs:
            with self.store.lock:
                info = cache.get(key)
            stale_failure = info and info.get("error") and now - info.get("at", 0) > self.RETRY_FAILED
            if (info is None or stale_failure) and lookups < self.MAX_LOOKUPS:
                lookups += 1
                try:
                    info = compute() or {}
                except Exception as e:
                    log.info("release notes for %s: %s", key, e)
                    info = {"error": str(e), "flags": [], "releases": []}
                info["at"] = now
                with self.store.lock:
                    cache[key] = info
            if info is None:
                continue
            self._attach(where, info)
            for k in ukeys:
                by_update_key[k] = info

        for u in pending:
            info = by_update_key.get(u.key)
            if info:
                u.flags = list(info.get("flags") or [])
                if not u.notes and info.get("releases"):
                    u.notes = info["releases"][0].get("notes")
                if info.get("releases") and u.kind != "release":
                    u.url = info["releases"][0].get("url") or u.url

        if only is None:   # forget lookups nothing points at any more
            live = {k for k, *_ in jobs}
            with self.store.lock:
                for k in [k for k in cache if k not in live]:
                    del cache[k]

    def _attach(self, where, info):
        with self.store.lock:
            if where[0] == "results":
                result = self.store.data["results"].get(where[1]) or {}
                for row in result.get("images") or []:
                    if row["ref"] == where[2]:
                        row["changes"] = info
            else:
                item = self.store.find(where[0], where[1])
                if item is not None:
                    item["changes"] = info

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
