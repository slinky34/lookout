"""Find tags that look like a newer version of the one a container runs."""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION = re.compile(r"^(?P<prefix>v?)(?P<nums>\d+(?:\.\d+)*)(?P<suffix>.*)$")


@dataclass(frozen=True)
class Version:
    prefix: str
    nums: tuple[int, ...]
    suffix: str

    def same_shape(self, other: "Version") -> bool:
        return (self.prefix == other.prefix and self.suffix == other.suffix
                and len(self.nums) == len(other.nums))


def parse(tag: str) -> Version | None:
    m = _VERSION.match(tag)
    if not m:
        return None
    nums = tuple(int(n) for n in m.group("nums").split("."))
    return Version(m.group("prefix"), nums, m.group("suffix"))


def newest_tag(current: str, tags: list[str]) -> str | None:
    """Highest tag that has the same shape as `current` (prefix, part count, suffix)
    and is newer. "1.25.3-alpine" only matches "1.x.y-alpine"; "latest" matches nothing."""
    cur = parse(current)
    if cur is None:
        return None
    best: tuple[tuple[int, ...], str] | None = None
    for tag in tags:
        v = parse(tag)
        if v is None or not v.same_shape(cur) or v.nums <= cur.nums:
            continue
        if best is None or v.nums > best[0]:
            best = (v.nums, tag)
    return best[1] if best else None
