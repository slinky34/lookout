"""Check GitHub repositories for new releases."""

from __future__ import annotations

import logging

import requests

from .models import Update

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubWatcher:
    def __init__(self, cfg: dict, session: requests.Session | None = None, timeout: float = 20.0):
        self.cfg = cfg
        self.timeout = timeout
        self.http = session or requests.Session()
        self.http.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "lookout-update-watcher",
        })
        if cfg.get("token"):
            self.http.headers["Authorization"] = f"Bearer {cfg['token']}"

    def _repos(self) -> list[dict]:
        out = []
        for entry in self.cfg.get("repos") or []:
            if isinstance(entry, str):
                entry = {"repo": entry}
            entry.setdefault("include_prereleases", self.cfg.get("include_prereleases", False))
            out.append(entry)
        return out

    def check(self, state: dict) -> tuple[list[Update], list[str]]:
        updates: list[Update] = []
        errors: list[str] = []
        for entry in self._repos():
            repo = entry["repo"].strip().strip("/")
            try:
                rel = self._latest(repo, bool(entry["include_prereleases"]))
            except Exception as e:
                errors.append(f"GitHub {repo}: {e}")
                log.warning("GitHub %s: %s", repo, e)
                continue
            if rel is None:
                log.info("%s: no releases yet", repo)
                continue
            tag = rel["tag_name"]
            key = f"github:{repo}"
            previous = state.get(key)
            log.info("%s: latest release %s (last seen %s)", repo, tag, previous or "none")
            if previous is None:
                # First time we see this repo: remember where it is, don't announce
                # an old release as if it were new.
                state[key] = tag
                continue
            if tag != previous:
                updates.append(Update(
                    kind="release", key=key, value=tag, title=repo,
                    current=previous, latest=tag, url=rel.get("html_url"),
                    notes=_excerpt(rel.get("body") or ""),
                ))
        return updates, errors

    def _latest(self, repo: str, prereleases: bool) -> dict | None:
        if not prereleases:
            r = self.http.get(f"{API}/repos/{repo}/releases/latest", timeout=self.timeout)
            if r.status_code == 404:
                return None
            _raise(r)
            return r.json()
        r = self.http.get(f"{API}/repos/{repo}/releases", params={"per_page": 10}, timeout=self.timeout)
        _raise(r)
        for rel in r.json():
            if not rel.get("draft"):
                return rel
        return None


def _raise(r):
    if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
        raise RuntimeError("GitHub rate limit hit; add a github.token to raise it")
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub answered {r.status_code}")


def _excerpt(body: str, limit: int = 600) -> str:
    body = body.strip()
    return body if len(body) <= limit else body[:limit].rsplit("\n", 1)[0] + "\n…"
