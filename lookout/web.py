"""Dashboard: a JSON API plus the static page in ./static."""

from __future__ import annotations

import hmac
import logging
import re
import time
from copy import deepcopy
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from . import __version__, imageref, notify, updater
from .checker import _docker_error, effective_config, host_list, portainer_client
from .docker_watch import connect
from .models import Update
from .store import Store, image_item, new_id, normalize_repo, repo_item

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

# Dashboard field -> (path in config, kind)
SETTINGS = {
    "interval": (("interval",), "text"),
    "discord_webhook_url": (("notify", "discord", "webhook_url"), "secret"),
    "ntfy_url": (("notify", "ntfy", "url"), "text"),
    "ntfy_token": (("notify", "ntfy", "token"), "secret"),
    "ntfy_priority": (("notify", "ntfy", "priority"), "text"),
    "gotify_url": (("notify", "gotify", "url"), "text"),
    "gotify_token": (("notify", "gotify", "token"), "secret"),
    "github_token": (("github", "token"), "secret"),
    "check_new_tags": (("docker", "check_new_tags"), "bool"),
    "watch_by_default": (("docker", "watch_by_default"), "bool"),
    "include_stopped": (("docker", "include_stopped"), "bool"),
    "notify_errors": (("notify_errors",), "bool"),
    "portainer_url": (("portainer", "url"), "text"),
    "portainer_token": (("portainer", "token"), "secret"),
    "portainer_verify_tls": (("portainer", "verify_tls"), "bool"),
    "portainer_watch_all": (("portainer", "watch_all"), "bool"),
    "portainer_allow_updates": (("portainer", "allow_updates"), "bool"),
}


def _get(d: dict, path: tuple):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _set(d: dict, path: tuple, value):
    for p in path[:-1]:
        d = d.setdefault(p, {})
    d[path[-1]] = value


def normalize_image(text: str) -> str | None:
    t = text.strip()
    m = re.match(r"^https?://hub\.docker\.com/(?:r/|_/)([^?#]+?)/?(?:[?#].*)?$", t)
    if m:
        t = m.group(1)
        t = t.split("/tags")[0]
    if not t or " " in t or t.startswith(("http://", "https://")):
        return None
    try:
        ref = imageref.parse(t)
    except Exception:
        return None
    if not ref.repository or not re.fullmatch(r"[a-z0-9._/-]+", ref.repository):
        return None
    return str(ref) if not ref.digest else None


def normalize_host(text: str) -> str | None:
    t = text.strip()
    if not t:
        return None
    if t.startswith("unix://"):
        return t
    t = re.sub(r"^(tcp|http)://", "", t)
    if t.startswith("https://"):
        return None   # TLS needs certificates; use a socket proxy on the LAN instead
    t = t.rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", t):
        return None
    if not re.search(r":\d+$", t):
        t += ":2375"
    return f"tcp://{t}"


def create_app(base_cfg: dict, store: Store, scheduler, password: str | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.json.sort_keys = False

    @app.before_request
    def _auth():
        if not password or request.path == "/api/health":
            return None
        auth = request.authorization
        if auth and hmac.compare_digest((auth.password or "").encode(), password.encode()):
            return None
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Lookout"'})

    def cfg():
        return effective_config(base_cfg, store)

    # -- pages ---------------------------------------------------------------

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html", max_age=0)

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC, name, max_age=0)

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    # -- status ----------------------------------------------------------------

    @app.get("/api/status")
    def status():
        c = cfg()
        with store.lock:
            results = deepcopy(store.data["results"])
            repos = deepcopy(store.data["repos"])
            images = deepcopy(store.data["images"])
            last_run = store.data.get("last_run")
        hosts = []
        listed = host_list(c, store)
        if (c.get("portainer") or {}).get("watch_all"):
            listed += [{"id": k, "name": r.get("name"), "url": None} for k, r in sorted(results.items())
                       if k.startswith("pt-")]
        for h in listed:
            r = results.get(h["id"]) or {}
            source = "portainer" if h["id"].startswith("pt-") else "docker"
            hosts.append({**h, "source": source,
                          "removable": source == "docker" and h["id"] != "local",
                          "ok": r.get("ok"), "error": r.get("error"),
                          "checked_at": r.get("checked_at"), "images": r.get("images") or [],
                          "ignored": r.get("ignored") or [], "portainer_env": r.get("portainer_env")})
        pc = c.get("portainer") or {}
        portainer = {"connected": bool(pc.get("url") and pc.get("token")),
                     "allow_updates": bool(pc.get("allow_updates", True)),
                     "error": next((r.get("portainer_error") for r in results.values() if r.get("portainer_error")), None)}
        for r in repos:
            r["status"] = _repo_status(r)
        for i in images:
            i["status"] = _image_status(i)
        updates = (sum(1 for h in hosts for i in h["images"] if i["status"] == "update")
                   + sum(1 for r in repos if r["status"] == "update")
                   + sum(1 for i in images if i["status"] == "update"))
        errors = ((1 if portainer["error"] else 0)
                  + sum(1 for h in hosts if h["ok"] is False)
                  + sum(1 for h in hosts for i in h["images"] if i["status"] == "error")
                  + sum(1 for r in repos if r["status"] == "error")
                  + sum(1 for i in images if i["status"] == "error"))
        watching = sum(len(h["images"]) for h in hosts) + len(repos) + len(images)
        return {
            "version": __version__,
            "checking": scheduler.checking,
            "last_run": last_run,
            "next_run": int(scheduler.next_run),
            "interval": c["interval"],
            "notifiers": [n.name for n in notify.build(c.get("notify") or {})],
            "counts": {"updates": updates, "errors": errors, "watching": watching},
            "hosts": hosts, "repos": repos, "images": images, "portainer": portainer,
        }

    @app.post("/api/check")
    def check_now():
        only = (request.get_json(silent=True) or {}).get("only")
        scheduler.trigger(set(only) if only else None)
        return {"ok": True}

    # -- Portainer -----------------------------------------------------------------

    @app.post("/api/portainer/test")
    def portainer_test():
        client = portainer_client(cfg())
        if client is None:
            return _err("Add Portainer's address and an access token first.")
        try:
            s = client.summary()
        except Exception as e:
            return _err(str(e))
        return {"ok": True, **s}

    @app.post("/api/hosts/<host_id>/update")
    def update_image(host_id):
        """Body: {"ref": "...", "confirm": false} returns the plan; confirm=true runs it."""
        body = request.get_json(force=True) or {}
        c = cfg()
        if not (c.get("portainer") or {}).get("allow_updates", True):
            return _err("Updating from the dashboard is turned off in Settings.")
        client = portainer_client(c)
        if client is None:
            return _err("Connect Portainer in Settings to update from here.")
        with store.lock:
            result = store.data["results"].get(host_id) or {}
            row = next((deepcopy(r) for r in result.get("images") or [] if r["ref"] == body.get("ref")), None)
        if row is None:
            return _err("That image isn't on this host any more. Run a check and try again.", 404)
        try:
            p = updater.plan(client, row)
        except Exception as e:
            return _err(str(e))
        if not body.get("confirm"):
            return {"plan": updater.describe(p), "ref": row["ref"]}
        if not p["steps"]:
            return _err("Nothing here can be updated through Portainer.")
        results = updater.run(client, p)
        scheduler.trigger({host_id})
        return {"results": results}

    # -- GitHub repos ------------------------------------------------------------

    @app.post("/api/repos")
    def add_repos():
        body = request.get_json(force=True)
        lines = _lines(body.get("repos"))
        label = (body.get("label") or "").strip() or None
        added, skipped = [], []
        with store.lock:
            for line in lines:
                repo = normalize_repo(line)
                if not repo:
                    skipped.append({"value": line, "reason": "Not a GitHub repo (use owner/name or its URL)"})
                elif store.find_repo(repo):
                    skipped.append({"value": line, "reason": "Already watched"})
                else:
                    item = repo_item(repo, bool(body.get("include_prereleases")), label if len(lines) == 1 else None)
                    store.data["repos"].append(item)
                    added.append(item)
            store.save()
        if added:
            scheduler.trigger({i["id"] for i in added})
        return {"added": added, "skipped": skipped}

    # -- images ----------------------------------------------------------------

    @app.post("/api/images")
    def add_images():
        body = request.get_json(force=True)
        lines = _lines(body.get("images"))
        label = (body.get("label") or "").strip() or None
        added, skipped = [], []
        with store.lock:
            existing = {i["ref"] for i in store.data["images"]}
            for line in lines:
                ref = normalize_image(line)
                if not ref:
                    skipped.append({"value": line, "reason": "Not an image name (e.g. lscr.io/linuxserver/sonarr:latest)"})
                elif ref in existing:
                    skipped.append({"value": line, "reason": "Already watched"})
                else:
                    item = image_item(ref, label if len(lines) == 1 else None)
                    store.data["images"].append(item)
                    existing.add(ref)
                    added.append(item)
            store.save()
        if added:
            scheduler.trigger({i["id"] for i in added})
        return {"added": added, "skipped": skipped}

    # -- shared item routes ------------------------------------------------------

    @app.delete("/api/<kind>/<item_id>")
    def remove(kind, item_id):
        if kind not in ("repos", "images", "hosts"):
            return _err("Unknown list", 404)
        if not store.remove(kind, item_id):
            return _err("Not found", 404)
        store.save()
        return {"ok": True}

    @app.patch("/api/<kind>/<item_id>")
    def edit(kind, item_id):
        if kind not in ("repos", "images", "hosts"):
            return _err("Unknown list", 404)
        body = request.get_json(force=True)
        with store.lock:
            item = store.find(kind, item_id)
            if not item:
                return _err("Not found", 404)
            if "label" in body and kind != "hosts":
                item["label"] = (body["label"] or "").strip() or None
            if "name" in body and kind == "hosts":
                item["name"] = (body["name"] or "").strip() or item["name"]
            if "include_prereleases" in body and kind == "repos":
                item["include_prereleases"] = bool(body["include_prereleases"])
                item["rev"] = item.get("rev", 0) + 1
            store.save()
        return {"ok": True, "item": item}

    @app.post("/api/<kind>/<item_id>/ack")
    def ack(kind, item_id):
        """'I've updated it': what's available now becomes what you run."""
        with store.lock:
            item = store.find(kind, item_id) if kind in ("repos", "images") else None
            if not item:
                return _err("Not found", 404)
            item["rev"] = item.get("rev", 0) + 1
            if kind == "repos":
                if item.get("latest"):
                    item["installed"] = item["latest"]
            else:
                if item.get("newest_tag"):
                    ref = imageref.parse(item["ref"])
                    item["ref"] = str(imageref.ImageRef(ref.registry, ref.repository, item["newest_tag"], None))
                    item.update(installed_digest=None, latest_digest=None, newest_tag=None)
                elif item.get("latest_digest"):
                    item["installed_digest"] = item["latest_digest"]
            store.save()
        if kind == "images" and item.get("installed_digest") is None:
            scheduler.trigger({item_id})
        return {"ok": True, "item": item}

    # -- Docker hosts ------------------------------------------------------------

    @app.post("/api/hosts/test")
    def test_host():
        url = normalize_host((request.get_json(force=True) or {}).get("url") or "")
        if not url:
            return _err("Use an address like 192.168.1.20:2375 or tcp://nas.lan:2375")
        try:
            n = _probe(url)
        except Exception as e:
            return _err(f"Couldn't connect to {url}. {_docker_error(e)}")
        return {"ok": True, "url": url, "containers": n}

    @app.post("/api/hosts")
    def add_host():
        body = request.get_json(force=True)
        url = normalize_host(body.get("url") or "")
        name = (body.get("name") or "").strip()
        if not url:
            return _err("Use an address like 192.168.1.20:2375 or tcp://nas.lan:2375")
        if not body.get("skip_test"):
            try:
                _probe(url)
            except Exception as e:
                return _err(f"Couldn't connect to {url}. {_docker_error(e)}")
        with store.lock:
            if any(h["url"] == url for h in store.data["hosts"]):
                return _err("That host is already added")
            host = {"id": new_id(), "name": name or url.split("://", 1)[1].rsplit(":", 1)[0], "url": url}
            store.data["hosts"].append(host)
            store.save()
        scheduler.trigger({host["id"]})
        return {"ok": True, "host": host}

    # -- settings ----------------------------------------------------------------

    @app.get("/api/settings")
    def get_settings():
        c = cfg()
        out = {}
        for field, (path, kind) in SETTINGS.items():
            v = _get(c, path)
            if kind == "secret":
                out[field] = {"set": bool(v), "hint": ("…" + str(v)[-4:]) if v and len(str(v)) > 8 else None}
            elif kind == "bool":
                out[field] = bool(v)
            else:
                out[field] = "" if v is None else str(v)
        return out

    @app.put("/api/settings")
    def put_settings():
        body = request.get_json(force=True)
        if "interval" in body:
            from .config import parse_interval
            try:
                parse_interval(str(body["interval"]))
            except ValueError as e:
                return _err(str(e))
        before = cfg()
        with store.lock:
            overrides = store.data.setdefault("settings", {})
            for field, (path, kind) in SETTINGS.items():
                if field not in body:
                    continue
                v = body[field]
                if kind == "secret":
                    if v == "":
                        continue          # left blank: keep what's there
                    _set(overrides, path, v.strip() if isinstance(v, str) else None)
                elif kind == "bool":
                    _set(overrides, path, bool(v))
                else:
                    _set(overrides, path, (str(v).strip() or None) if v is not None else None)
            store.save()
        scheduler.reschedule()
        after = cfg()
        # What's watched depends on these, so look again now rather than at the next interval.
        if any(before.get(k) != after.get(k) for k in ("portainer", "docker", "registries")):
            scheduler.trigger(None)
        return get_settings()

    @app.post("/api/test-notify")
    def test_notify():
        notifiers = notify.build(cfg().get("notify") or {})
        if not notifiers:
            return _err("No notifications set up yet. Add a Discord webhook, ntfy topic or Gotify server first.")
        sample = [Update(kind="release", key="test", value="test", title="Lookout test",
                         current="v1.0.0", latest="v1.1.0", url=None,
                         notes="If you can see this, notifications from Lookout work.")]
        results = []
        for n in notifiers:
            try:
                n.send(sample, [])
                results.append({"name": n.name, "ok": True})
            except Exception as e:
                results.append({"name": n.name, "ok": False, "error": str(e)})
        return {"results": results}

    return app


def _probe(url: str) -> int:
    client = connect(url, timeout=8)
    try:
        client.ping()
        return len(client.containers.list())
    finally:
        client.close()


def _lines(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[\n,]+", value)
    seen, out = set(), []
    for v in value or []:
        v = str(v).strip()
        if v and not v.startswith("#") and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _err(message: str, code: int = 400):
    return jsonify({"error": message}), code


def _repo_status(r: dict) -> str:
    if r.get("error"):
        return "error"
    if not r.get("checked_at"):
        return "pending"
    if not r.get("latest"):
        return "none"
    return "update" if r.get("installed") != r.get("latest") else "ok"


def _image_status(i: dict) -> str:
    if i.get("error"):
        return "error"
    if not i.get("checked_at") or not i.get("latest_digest"):
        return "pending"
    if i.get("newest_tag") or i.get("installed_digest") != i.get("latest_digest"):
        return "update"
    return "ok"
