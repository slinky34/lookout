"""Minimal Docker Registry v2 client: manifest digests and tag lists, with token auth."""

from __future__ import annotations

import logging
import re
import time

import requests

from .imageref import ImageRef

log = logging.getLogger(__name__)

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])

_CHALLENGE_PARAM = re.compile(r'(\w+)="([^"]*)"')


class RegistryError(Exception):
    pass


class RegistryClient:
    def __init__(self, credentials: dict[str, dict] | None = None, timeout: float = 20.0,
                 session: requests.Session | None = None):
        # credentials: {"ghcr.io": {"username": ..., "password": ...}}; "docker.io" maps to Hub.
        self.credentials = {}
        for host, cred in (credentials or {}).items():
            key = "registry-1.docker.io" if host in ("docker.io", "index.docker.io") else host
            self.credentials[key] = cred
        self.timeout = timeout
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = "lookout-update-watcher"
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}

    # -- public -----------------------------------------------------------

    def manifest_digest(self, ref: ImageRef) -> str:
        """Digest of the manifest (or multi-arch index) the tag currently points at."""
        url = f"https://{ref.registry}/v2/{ref.repository}/manifests/{ref.tag}"
        resp = self._request("HEAD", ref, url, headers={"Accept": MANIFEST_ACCEPT})
        digest = resp.headers.get("Docker-Content-Digest")
        if not digest:
            # Some registries omit the header on HEAD; GET always has it or a body we can hash.
            resp = self._request("GET", ref, url, headers={"Accept": MANIFEST_ACCEPT})
            digest = resp.headers.get("Docker-Content-Digest")
        if not digest:
            raise RegistryError(f"{ref.registry} returned no digest for {ref}")
        return digest

    def image_config(self, ref: ImageRef, reference: str | None = None) -> dict:
        """The image config (created date, labels) for a tag or digest. For multi-arch
        images, the linux/amd64 variant (labels are the same across architectures).
        Note: on Docker Hub a manifest GET counts toward the pull limit, so callers cache."""
        reference = reference or ref.tag
        base = f"https://{ref.registry}/v2/{ref.repository}"
        man = self._request("GET", ref, f"{base}/manifests/{reference}",
                            headers={"Accept": MANIFEST_ACCEPT}).json()
        if "manifests" in man:   # index / manifest list
            entries = [m for m in man["manifests"]
                       if (m.get("platform") or {}).get("os") not in (None, "unknown")]
            pick = next((m for m in entries if (m.get("platform") or {}).get("architecture") == "amd64"),
                        entries[0] if entries else None)
            if pick is None:
                raise RegistryError(f"{ref} has no usable image in its manifest list")
            man = self._request("GET", ref, f"{base}/manifests/{pick['digest']}",
                                headers={"Accept": MANIFEST_ACCEPT}).json()
        digest = (man.get("config") or {}).get("digest")
        if not digest:
            raise RegistryError(f"{ref} has no image config")
        blob = self._request("GET", ref, f"{base}/blobs/{digest}").json()
        cfg = blob.get("config") or {}
        return {"created": blob.get("created"), "labels": cfg.get("Labels") or {}}

    def list_tags(self, ref: ImageRef, max_pages: int = 20) -> list[str]:
        tags: list[str] = []
        url: str | None = f"https://{ref.registry}/v2/{ref.repository}/tags/list?n=1000"
        for _ in range(max_pages):
            if not url:
                break
            resp = self._request("GET", ref, url)
            tags.extend(resp.json().get("tags") or [])
            url = _next_link(resp, ref.registry)
        return tags

    # -- internals --------------------------------------------------------

    def _request(self, method: str, ref: ImageRef, url: str, headers: dict | None = None):
        headers = dict(headers or {})
        token = self._cached_token(ref)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = self.http.request(method, url, headers=headers, timeout=self.timeout)
        if resp.status_code == 401:
            challenge = resp.headers.get("WWW-Authenticate", "")
            auth = self._authenticate(ref, challenge)
            if auth:
                headers["Authorization"] = auth
                resp = self.http.request(method, url, headers=headers, timeout=self.timeout)
        if resp.status_code == 429:
            raise RegistryError(f"{ref.display_registry} rate limit hit while checking {ref}")
        if resp.status_code == 404:
            raise RegistryError(f"{ref} not found on {ref.display_registry}")
        if resp.status_code >= 400:
            raise RegistryError(f"{ref.display_registry} answered {resp.status_code} for {ref}")
        return resp

    def _cached_token(self, ref: ImageRef) -> str | None:
        entry = self._tokens.get((ref.registry, ref.repository))
        if entry and entry[1] > time.time():
            return entry[0]
        return None

    def _authenticate(self, ref: ImageRef, challenge: str) -> str | None:
        cred = self.credentials.get(ref.registry)
        scheme = challenge.split(" ", 1)[0].lower()
        if scheme == "basic":
            if not cred:
                raise RegistryError(f"{ref.display_registry} needs a login to check {ref}")
            return requests.auth._basic_auth_str(cred["username"], cred["password"])
        if scheme != "bearer":
            return None

        params = dict(_CHALLENGE_PARAM.findall(challenge))
        realm = params.pop("realm", None)
        if not realm:
            return None
        params.setdefault("scope", f"repository:{ref.repository}:pull")
        auth = (cred["username"], cred["password"]) if cred else None
        resp = self.http.get(realm, params=params, auth=auth, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RegistryError(
                f"could not get a token from {ref.display_registry} for {ref} ({resp.status_code})"
                + ("" if cred else "; private images need credentials in config")
            )
        body = resp.json()
        token = body.get("token") or body.get("access_token")
        if not token:
            return None
        ttl = int(body.get("expires_in") or 60)
        self._tokens[(ref.registry, ref.repository)] = (token, time.time() + max(ttl - 10, 10))
        return f"Bearer {token}"


def _next_link(resp, registry: str) -> str | None:
    link = resp.headers.get("Link")
    if not link:
        return None
    m = re.search(r"<([^>]+)>", link)
    if not m:
        return None
    target = m.group(1)
    return target if target.startswith("http") else f"https://{registry}{target}"
