"""Parse Docker image references the same way the Docker CLI does."""

from __future__ import annotations

from dataclasses import dataclass

DOCKER_HUB = "registry-1.docker.io"
_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}


@dataclass(frozen=True)
class ImageRef:
    registry: str      # host used for API calls, e.g. registry-1.docker.io, ghcr.io
    repository: str    # e.g. library/nginx, linuxserver/plex
    tag: str | None    # e.g. latest, 1.25.3
    digest: str | None # set when the reference is pinned with @sha256:...

    @property
    def is_docker_hub(self) -> bool:
        return self.registry == DOCKER_HUB

    @property
    def display_registry(self) -> str:
        return "docker.io" if self.is_docker_hub else self.registry

    @property
    def name(self) -> str:
        """Familiar name without tag: nginx, linuxserver/plex, ghcr.io/owner/app."""
        if self.is_docker_hub:
            repo = self.repository
            return repo[len("library/"):] if repo.startswith("library/") else repo
        return f"{self.registry}/{self.repository}"

    def __str__(self) -> str:
        s = self.name
        if self.tag:
            s += f":{self.tag}"
        if self.digest:
            s += f"@{self.digest}"
        return s


def parse(ref: str) -> ImageRef:
    ref = ref.strip()
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)

    # Split off the registry host: the first path component counts as a host
    # if it has a dot or a port, or is "localhost" (Docker's rule).
    parts = ref.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, rest = parts
    else:
        registry, rest = "docker.io", ref

    # Tag: a colon after the last slash.
    tag = None
    last = rest.rsplit("/", 1)[-1]
    if ":" in last:
        rest, tag = rest.rsplit(":", 1)
    if tag is None and digest is None:
        tag = "latest"

    if registry in _HUB_ALIASES:
        registry = DOCKER_HUB
        if "/" not in rest:
            rest = f"library/{rest}"

    return ImageRef(registry=registry, repository=rest.lower(), tag=tag, digest=digest)
