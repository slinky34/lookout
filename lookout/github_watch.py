"""Look up the latest release of a GitHub repository."""

from __future__ import annotations

import requests

API = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str | None = None, session: requests.Session | None = None,
                 timeout: float = 20.0):
        self.timeout = timeout
        self.http = session or requests.Session()
        self.http.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "lookout-update-watcher",
        })
        if token:
            self.http.headers["Authorization"] = f"Bearer {token}"

    def latest(self, repo: str, prereleases: bool = False) -> dict | None:
        """{"tag", "url", "notes", "published_at"} or None if the repo has no releases."""
        if not prereleases:
            r = self.http.get(f"{API}/repos/{repo}/releases/latest", timeout=self.timeout)
            if r.status_code == 404:
                self._exists(repo)
                return None
            _raise(r, repo)
            return _release(r.json())
        r = self.http.get(f"{API}/repos/{repo}/releases", params={"per_page": 10}, timeout=self.timeout)
        _raise(r, repo)
        for rel in r.json():
            if not rel.get("draft"):
                return _release(rel)
        return None

    def _exists(self, repo: str):
        r = self.http.get(f"{API}/repos/{repo}", timeout=self.timeout)
        if r.status_code == 404:
            raise RuntimeError(f"{repo} doesn't exist or is private (private repos need a GitHub token)")
        _raise(r, repo)


def _release(rel: dict) -> dict:
    return {
        "tag": rel["tag_name"], "url": rel.get("html_url"),
        "notes": excerpt(rel.get("body") or ""), "published_at": rel.get("published_at"),
    }


def _raise(r, repo):
    if r.status_code in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
        raise RuntimeError("GitHub rate limit hit; add a GitHub token in Settings to raise it")
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub answered {r.status_code} for {repo}")


def excerpt(body: str, limit: int = 600) -> str:
    body = body.strip()
    return body if len(body) <= limit else body[:limit].rsplit("\n", 1)[0] + "\n…"
