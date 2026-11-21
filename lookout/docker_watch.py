"""Check running containers' images against their registries."""

from __future__ import annotations

import logging
import re
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


class DockerWatcher:
    def __init__(self, cfg: dict, registry: RegistryClient, docker_client=None):
        self.cfg = cfg
        self.registry = registry
        self._client = docker_client

    @property
    def client(self):
        if self._client is None:
            import docker
            host = self.cfg.get("host")
            self._client = docker.DockerClient(base_url=host) if host else docker.from_env()
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
        """Returns (updates, errors)."""
        containers = self.client.containers.list(all=bool(self.cfg.get("include_stopped")))

        # Group by the image reference each container was created from, so each
        # image is checked once and the notification lists every container using it.
        groups: dict[str, list] = defaultdict(list)
        for c in containers:
            if not self._wanted(c):
                continue
            ref_text = (c.attrs.get("Config") or {}).get("Image") or ""
            if not ref_text or ref_text.startswith("sha256:"):
                log.debug("skip %s: created from an image ID, not a name", c.name)
                continue
            groups[ref_text].append(c)

        updates: list[Update] = []
        errors: list[str] = []
        for ref_text, members in sorted(groups.items()):
            try:
                updates.extend(self._check_image(ref_text, members))
            except RegistryError as e:
                errors.append(str(e))
                log.warning("%s", e)
            except Exception as e:  # keep going if one image is odd
                errors.append(f"{ref_text}: {e}")
                log.exception("checking %s failed", ref_text)
        return updates, errors

    def _check_image(self, ref_text: str, members: list) -> list[Update]:
        ref = imageref.parse(ref_text)
        names = sorted(c.name for c in members)
        if ref.digest:
            log.debug("skip %s: pinned to a digest", ref)
            return []

        # Digests the containers' images were pulled as. Locally built images have none.
        local_digests: set[str] = set()
        for c in members:
            try:
                image_attrs = self.client.images.get(c.attrs["Image"]).attrs
            except Exception:
                image_attrs = c.image.attrs
            for rd in image_attrs.get("RepoDigests") or []:
                local_digests.add(rd.split("@", 1)[-1])
        if not local_digests:
            log.info("skip %s (%s): built locally or never pulled from a registry", ref, ", ".join(names))
            return []

        out: list[Update] = []
        remote = self.registry.manifest_digest(ref)
        log.info("%s: local %s, remote %s", ref, ",".join(d[:19] for d in local_digests), remote[:19])
        if remote not in local_digests:
            out.append(Update(
                kind="digest", key=f"docker:{ref}:digest", value=remote, title=str(ref),
                current=ref.tag, latest=ref.tag, containers=names, url=_image_url(ref),
            ))

        check_tags = bool(self.cfg.get("check_new_tags", True))
        labels = members[0].labels or {}
        if _truthy(labels.get(LABEL_TAGS)) is not None:
            check_tags = _truthy(labels.get(LABEL_TAGS))
        if check_tags and ref.tag and versions.parse(ref.tag):
            newest = versions.newest_tag(ref.tag, self.registry.list_tags(ref))
            if newest:
                out.append(Update(
                    kind="tag", key=f"docker:{ref}:tag", value=newest, title=ref.name,
                    current=ref.tag, latest=newest, containers=names, url=_image_url(ref),
                ))
        return out


def _image_url(ref) -> str | None:
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
