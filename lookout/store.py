"""Everything Lookout keeps between restarts: the watch list, settings saved from the
dashboard, last results and what has already been announced. One JSON file in /data."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from copy import deepcopy
from pathlib import Path

log = logging.getLogger(__name__)

EMPTY = {
    "version": 1,
    "settings": {},        # partial config, same shape as config.DEFAULTS; wins over env/file
    "repos": [],           # GitHub repos added in the dashboard or seeded from config
    "images": [],          # images for services that don't run on a watched Docker host
    "hosts": [],           # extra Docker hosts: {"id", "name", "url"}
    "results": {},         # host id -> last scan of that host's containers
    "notified": {},        # update key -> value last announced
    "seeded": [],          # config/env repos already copied in, so deleting one sticks
    "errors_last": None,
    "last_run": None,
}


def new_id() -> str:
    return secrets.token_hex(4)


class Store:
    def __init__(self, path: str, legacy_state: str | None = None):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = deepcopy(EMPTY)
        if self.path.is_file():
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data.update(loaded)
            except (OSError, ValueError) as e:
                log.error("could not read %s (%s); starting with an empty store", self.path, e)
        elif legacy_state and Path(legacy_state).is_file():
            self._migrate(legacy_state)

    def _migrate(self, legacy: str):
        """0.1.x kept only {key: value} in state.json."""
        try:
            with open(legacy, encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            return
        for k, v in old.items():
            if k.startswith(("docker:", "github:")):
                self.data["notified"][k] = v
        log.info("imported %d entries from %s", len(self.data["notified"]), legacy)

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)

    # -- watch list ---------------------------------------------------------

    def seed_repos(self, repos: list):
        """Copy repos listed in env/config into the store once."""
        with self.lock:
            for entry in repos or []:
                if isinstance(entry, str):
                    entry = {"repo": entry}
                repo = normalize_repo(entry["repo"])
                if not repo or repo.lower() in (s.lower() for s in self.data["seeded"]):
                    continue
                self.data["seeded"].append(repo)
                if not self.find_repo(repo):
                    item = repo_item(repo, bool(entry.get("include_prereleases", False)))
                    # Carry over what 0.1.x had already seen, so nothing is re-announced.
                    prev = self.data["notified"].get(f"github:{repo}")
                    if prev:
                        item["installed"] = prev
                    item["source"] = "config"
                    self.data["repos"].append(item)

    def find_repo(self, repo: str):
        repo = repo.lower()
        return next((r for r in self.data["repos"] if r["repo"].lower() == repo), None)

    def find(self, kind: str, item_id: str):
        return next((x for x in self.data[kind] if x["id"] == item_id), None)

    def remove(self, kind: str, item_id: str) -> bool:
        with self.lock:
            before = len(self.data[kind])
            self.data[kind] = [x for x in self.data[kind] if x["id"] != item_id]
            if kind == "hosts":
                self.data["results"].pop(item_id, None)
            return len(self.data[kind]) != before


def normalize_repo(text: str) -> str:
    t = text.strip().rstrip("/")
    for prefix in ("https://", "http://", "www."):
        if t.lower().startswith(prefix):
            t = t[len(prefix):]
    if t.lower().startswith("github.com/"):
        t = t[len("github.com/"):]
    parts = [p for p in t.split("/") if p]
    if len(parts) < 2:
        return ""
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[:-4]
    return f"{owner}/{name}"


def repo_item(repo: str, include_prereleases: bool = False, label: str | None = None) -> dict:
    return {
        "id": new_id(), "repo": repo, "label": label or None,
        "include_prereleases": include_prereleases,
        "installed": None, "latest": None, "url": None, "notes": None, "published_at": None,
        "checked_at": None, "error": None, "added_at": int(time.time()), "source": "dashboard",
    }


def image_item(ref: str, label: str | None = None) -> dict:
    return {
        "id": new_id(), "ref": ref, "label": label or None,
        "installed_digest": None, "latest_digest": None, "newest_tag": None, "url": None,
        "checked_at": None, "error": None, "added_at": int(time.time()),
    }
