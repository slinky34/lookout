"""What's in an update: release notes between the version you run and the new one,
and whether any of it looks important (security fix, breaking change, major version)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from . import imageref
from .github_watch import excerpt

log = logging.getLogger(__name__)

SOURCE_LABEL = "org.opencontainers.image.source"
VERSION_LABEL = "org.opencontainers.image.version"

SECURITY = re.compile(
    r"\bsecurity\b|\bcve-\d{4}-\d+|\bghsa-[a-z0-9]{4}-|vulnerab|\bxss\b|\bcsrf\b|remote code execution|"
    r"\brce\b|privilege escalation|auth(entication)? bypass|\bexploit", re.I)
BREAKING = re.compile(
    r"breaking|backwards?[- ]incompatible|not backwards? compatible|migration (is )?(required|needed)|"
    r"manual (action|intervention|step)s? (is |are )?(required|needed)|action required|"
    r"must (now )?(update|change|migrate)|⚠", re.I)
# Words that make a "security" mention not a fix ("security headers docs", etc. still count;
# this only drops the commonest false positive).
NOT_SECURITY = re.compile(r"security\.md|security policy", re.I)

FLAG_ORDER = ["security", "breaking", "major"]


def _github_repo(url: str | None) -> str | None:
    if not url:
        return None
    m = re.match(r"^(?:https?://|git@)?(?:www\.)?github\.com[/:]([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?(?:[#?].*)?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _major(version: str | None) -> int | None:
    if not version:
        return None
    m = re.search(r"(\d+)(?:\.\d+)", version)   # first x.y; skips "version-" / "v" prefixes
    return int(m.group(1)) if m else None


def _norm(tag: str | None) -> str:
    return re.sub(r"^(v|version-|release-)", "", (tag or "").strip().lower())


def _parse_time(text: str | None):
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")[:32])
    except ValueError:
        try:
            return datetime.fromisoformat(re.sub(r"\.\d+", "", text).replace("Z", "+00:00"))
        except ValueError:
            return None


def flags_for(text: str) -> list[str]:
    out = []
    if SECURITY.search(text) and not (NOT_SECURITY.search(text) and len(SECURITY.findall(text)) <= 1):
        out.append("security")
    if BREAKING.search(text):
        out.append("breaking")
    return out


def releases_between(github, repo: str, old: str | None, new: str | None,
                     old_time=None, new_time=None, include_prereleases: bool = False) -> list[dict]:
    """Releases after `old` up to and including `new`, newest first. Matches by tag when
    both are found in the list; otherwise by publish date between the two image dates."""
    rels = [r for r in github.releases(repo, per_page=60)
            if not r.get("draft") and (include_prereleases or not r.get("prerelease"))]
    tags = [_norm(r.get("tag_name")) for r in rels]
    n_old, n_new = _norm(old), _norm(new)
    if n_new in tags and n_old in tags:
        i_new, i_old = tags.index(n_new), tags.index(n_old)
        picked = rels[i_new:i_old] if i_new < i_old else []
    elif n_new in tags:
        i_new = tags.index(n_new)
        if old_time is None:
            picked = rels[i_new:i_new + 1]
        else:
            picked = [r for r in rels[i_new:] if (_parse_time(r.get("published_at")) or old_time) > old_time]
    elif old_time is not None or new_time is not None:
        lo = old_time
        hi = new_time + timedelta(days=1) if new_time else None
        picked = []
        for r in rels:
            t = _parse_time(r.get("published_at"))
            if t is None:
                continue
            if (lo is None or t > lo) and (hi is None or t <= hi):
                picked.append(r)
        if lo is None:
            picked = picked[:1]   # only the new image's date is known: show its release
    else:
        picked = rels[:1]
    return picked[:25]


def _clean_line(line: str) -> str:
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)          # markdown links -> text
    line = re.sub(r"\(\s*\[?@[\w-]+\]?[^)]*\)|\(\[[^\]]*docs\]\)|\[#?\d+\]|#\d{3,}", "", line)  # (@user …), ([x docs]), [#123]
    line = re.sub(r"[*_`>#]+", "", line).strip(" -•\t")
    return re.sub(r"\s{2,}", " ", line)[:180]


def evidence(text: str) -> dict[str, list[str]]:
    """The lines that made us flag something, so the dashboard can show why."""
    out: dict[str, list[str]] = {"security": [], "breaking": []}
    for raw in text.splitlines():
        line = _clean_line(raw)
        if len(line) < 6:
            continue
        if SECURITY.search(line) and not NOT_SECURITY.search(line):
            out["security"].append(line)
        if BREAKING.search(line):
            out["breaking"].append(line)
    return out


def summarize(repo: str | None, releases: list[dict], old_version: str | None, new_version: str | None) -> dict:
    flags: list[str] = []
    why: dict[str, list[str]] = {"security": [], "breaking": []}
    out = []
    for r in releases:
        text = f"{r.get('name') or ''}\n{r.get('body') or ''}"
        f = flags_for(text)
        for x in f:
            if x not in flags:
                flags.append(x)
        for k, lines in evidence(text).items():
            for line in lines:
                if line not in why[k] and len(why[k]) < 6:
                    why[k].append(line)
        out.append({"tag": r.get("tag_name"), "name": r.get("name") or r.get("tag_name"),
                    "url": r.get("html_url"), "published_at": r.get("published_at"),
                    "flags": f, "notes": excerpt(r.get("body") or "", 900)})
    a, b = _major(old_version), _major(new_version)
    if a is not None and b is not None and b > a:
        flags.append("major")
        why["major"] = [f"{old_version} → {new_version}"]
    return {
        "repo": repo, "from": old_version, "to": new_version,
        "flags": sorted(set(flags), key=FLAG_ORDER.index),
        "why": {k: v for k, v in why.items() if v and k in flags},
        "releases": out,
        "url": f"https://github.com/{repo}/releases" if repo else None,
    }


def for_image(registry, github, ref_text: str, old_digest: str | None, new_ref: str | None = None) -> dict | None:
    """Changes for an image going from `old_digest` to the newest image of `new_ref`
    (default: the same tag). None when the image doesn't say where its source is."""
    ref = imageref.parse(ref_text)
    target = imageref.parse(new_ref) if new_ref else ref
    new_cfg = registry.image_config(target)
    old_cfg = None
    if old_digest:
        try:
            old_cfg = registry.image_config(ref, old_digest)
        except Exception as e:   # old images get cleaned out of registries
            log.info("old config for %s unavailable: %s", ref, e)
    labels = new_cfg["labels"]
    repo = _github_repo(labels.get(SOURCE_LABEL)) or _github_repo((old_cfg or {}).get("labels", {}).get(SOURCE_LABEL))
    new_version = labels.get(VERSION_LABEL) or (target.tag if new_ref else None)
    old_version = ((old_cfg or {}).get("labels") or {}).get(VERSION_LABEL) or (ref.tag if new_ref else None)
    if not repo:
        info = summarize(None, [], old_version, new_version)
        info["note"] = "This image doesn't link to its source, so there are no release notes to show."
        return info
    releases = releases_between(
        github, repo, old_version, new_version,
        old_time=_parse_time((old_cfg or {}).get("created")), new_time=_parse_time(new_cfg.get("created")))
    return summarize(repo, releases, old_version, new_version)


def for_repo(github, repo: str, installed: str | None, latest: str | None, prereleases: bool = False) -> dict:
    releases = releases_between(github, repo, installed, latest, include_prereleases=prereleases)
    return summarize(repo, releases, installed, latest)
