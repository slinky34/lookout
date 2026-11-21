from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Update:
    kind: str                 # "digest" (same tag, new image), "tag" (newer version tag), "release"
    key: str                  # state key, stable across runs
    value: str                # what we last told you about (digest / tag); a change means notify again
    title: str                # e.g. "nginx:latest" or "owner/repo"
    current: str | None = None
    latest: str | None = None
    url: str | None = None
    containers: list[str] = field(default_factory=list)
    notes: str | None = None  # release notes excerpt

    @property
    def summary(self) -> str:
        if self.kind == "digest":
            return "New image published for this tag"
        if self.current and self.latest:
            return f"{self.current} → {self.latest}"
        return self.latest or "Update available"
