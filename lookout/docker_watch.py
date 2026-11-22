"""Check a Docker host's containers against their images' registries."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict

from . import imageref, versions
from .models import Update
from .registry import RegistryClient, RegistryError

log = logging.getLogger(__name__)

LABEL_ENABLE = "lookout.enable"
LABEL_TAGS = "lookout.tags"


def _truthy(v: str | None) -> bool | None:
    if v is None:
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")


def key_prefix(host_id: str) -> str:
    # The local host keeps 0.1.x's keys so upgrading doesn't re-announce anything.
    return "docker:" if host_id == "local" else f"docker@{host_id}:"


def connect(url: str | None, timeout: int = 15):
    import docker
    return docker.DockerClient(base_url=url, timeout=timeout) if url else docker.from_env(timeout=timeout)


class DockerWatcher:
    def __init__(self, cfg: dict, registry: RegistryClient, docker_client=None,
                 host_id: str = "local", host_name: str = "This server", url: str | None = None):
        self.cfg = cfg
        self.registry = registry
        self._client = docker_client
        self.host_id = host_id
        self.host_name = host_name
        self.url = url if url is not None else cfg.get("host")

    @property
    def client(self):
        if self._client is None:
            self._client = connect(self.url)
        return self._client

    def _wanted(self, container) -> bool:
        labels = container.labels or {}
        enabled = _truthy(labels.get(LABEL_ENABLE))
        if enabled is not None:
            return enabled
        for pattern in self.cfg.get("exclude") or []:
            if container.name == pattern or re.fullmatch(pattern, container.name):
                return False
        return bool(self.cfg.get("watch_by_default", True))

    def check(self) -> tuple[list[Update], list[str]]:
        """Back-compat helper: just the pending updates and errors."""
        _, updates, errors = self.scan()
        return updates, errors

    def scan(self) -> tuple[dict, list[Update], list[str]]:
        """Returns (host result for the dashboard, pending updates, errors).
        Raises if the host can't be reached at all."""
        containers = self.client.containers.list(all=bool(self.cfg.get("include_stopped")))

        groups: dict[str, list] = defaultdict(list)
        ignored: list[str] = []
        for c in containers:
            if not self._wanted(c):
                ignored.append(c.name)
                continue
            ref_text = (c.attrs.get("Config") or {}).get("Image") or ""
            groups[ref_text].append(c)

        images: list[dict] = []
        updates: list[Update] = []
        errors: list[str] = []
        for ref_text, members in sorted(groups.items()):
            names = sorted(c.name for c in members)
            row = {"ref": ref_text, "containers": names, "status": "ok", "reason": None,
                   "newest_tag": None, "digest_update": False, "url": None}
            try:
                row_updates = self._check_image(ref_text, members, row)
                updates.extend(row_updates)
            except RegistryError as e:
                row.update(status="error", reason=str(e))
                errors.append(f"{self.host_name}: {e}")
                log.warning("%s: %s", self.host_name, e)
            except Exception as e:
                row.update(status="error", reason=str(e))
                errors.append(f"{self.host_name}: {ref_text}: {e}")
                log.exception("checking %s on %s failed", ref_text, self.host_name)
            images.append(row)

        result = {
            "id": self.host_id, "name": self.host_name, "url": self.url,
            "ok": True, "error": None, "checked_at": int(time.time()),
            "images": images, "ignored": sorted(ignored),
        }
        return result, updates, errors

    def _check_image(self, ref_text: str, members: list, row: dict) -> list[Update]:
        names = row["containers"]
        if not ref_text or ref_text.startswith("sha256:"):
            row.update(status="skipped", reason="Created from an image ID, not a name")
            return []
        ref = imageref.parse(ref_text)
        row["url"] = image_url(ref)
        if ref.digest:
            row.update(status="skipped", reason="Pinned to a digest")
            return []

        local_digests: set[str] = set()
        for c in members:
            try:
                image_attrs = self.client.images.get(c.attrs["Image"]).attrs
            except Exception:
                image_attrs = c.image.attrs
            for rd in image_attrs.get("RepoDigests") or []:
                local_digests.add(rd.split("@", 1)[-1])
        if not local_digests:
            row.update(status="skipped", reason="Built locally, so there's no registry copy to compare")
            return []

        where = [n if self.host_id == "local" else f"{n} ({self.host_name})" for n in names]
        prefix = key_prefix(self.host_id)
        out: list[Update] = []
        remote = self.registry.manifest_digest(ref)
        if remote not in local_digests:
            row["status"] = "update"
            row["digest_update"] = True
            out.append(Update(
                kind="digest", key=f"{prefix}{ref}:digest", value=remote, title=str(ref),
                current=ref.tag, latest=ref.tag, containers=where, url=row["url"],
            ))

        check_tags = bool(self.cfg.get("check_new_tags", True))
        label = _truthy((members[0].labels or {}).get(LABEL_TAGS))
        if label is not None:
            check_tags = label
        if check_tags and ref.tag and versions.parse(ref.tag):
            newest = versions.newest_tag(ref.tag, self.registry.list_tags(ref))
            if newest:
                row["newest_tag"] = newest
                if row["status"] == "ok":
                    row["status"] = "update"
                out.append(Update(
                    kind="tag", key=f"{prefix}{ref}:tag", value=newest, title=ref.name,
                    current=ref.tag, latest=newest, containers=where, url=row["url"],
                ))
        return out


def image_url(ref) -> str | None:
    if ref.is_docker_hub:
        repo = ref.repository
        if repo.startswith("library/"):
            return f"https://hub.docker.com/_/{repo[len('library/'):]}"
        return f"https://hub.docker.com/r/{repo}"
    if ref.registry == "ghcr.io":
        return f"https://ghcr.io/{ref.repository}"  # redirects to the GitHub package page
    if ref.registry == "lscr.io" and ref.repository.startswith("linuxserver/"):
        return f"https://github.com/linuxserver/docker-{ref.repository.split('/', 1)[1]}/releases"
    if ref.registry == "quay.io":
        return f"https://quay.io/repository/{ref.repository}"
    return None
