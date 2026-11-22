"""Send update notices to Discord, ntfy and Gotify."""

from __future__ import annotations

import logging

import requests

from .models import Update

log = logging.getLogger(__name__)

COLORS = {"digest": 0x3B82F6, "tag": 0x8B5CF6, "release": 0x22C55E, "error": 0xEF4444}
LABELS = {"digest": "Image updated", "tag": "New version tag", "release": "New release"}


def _containers_line(u: Update) -> str | None:
    if not u.containers:
        return None
    return ("Container: " if len(u.containers) == 1 else "Containers: ") + ", ".join(u.containers)


FLAG_TEXT = {"security": "🔒 Security fix", "breaking": "⚠️ Breaking changes", "major": "⬆️ Major version"}
FLAG_COLOR = {"security": 0xDC2626, "breaking": 0xEA580C, "major": 0x7C3AED}


def _ordered(updates: list[Update]) -> list[Update]:
    rank = {"security": 0, "breaking": 1, "major": 2}
    return sorted(updates, key=lambda u: min((rank[f] for f in u.flags), default=3))


def _flag_line(u: Update) -> str:
    return " · ".join(FLAG_TEXT[f] for f in u.flags if f in FLAG_TEXT)


def _plain_lines(updates: list[Update]) -> list[str]:
    lines = []
    for u in updates:
        line = f"• {u.title}: {u.summary}"
        c = _containers_line(u)
        if c:
            line += f" ({c.split(': ', 1)[1]})"
        if u.url:
            line += f"\n  {u.url}"
        lines.append(line)
    return lines


def _markdown_lines(updates: list[Update]) -> list[str]:
    lines = []
    for u in updates:
        title = f"[{u.title}]({u.url})" if u.url else u.title
        line = f"- **{title}**: {u.summary}"
        if u.flags:
            line += f" ({_flag_line(u)})"
        c = _containers_line(u)
        if c:
            line += f" ({c.split(': ', 1)[1]})"
        lines.append(line)
    return lines


def _headline(updates: list[Update]) -> str:
    n = len(updates)
    text = "1 update available" if n == 1 else f"{n} updates available"
    sec = sum(1 for u in updates if "security" in u.flags)
    brk = sum(1 for u in updates if "breaking" in u.flags)
    extra = [f"{sec} security" if sec else "", f"{brk} breaking" if brk else ""]
    extra = [e for e in extra if e]
    return text + (f" ({', '.join(extra)})" if extra else "")


class Discord:
    name = "Discord"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def send(self, updates: list[Update], errors: list[str]):
        updates = _ordered(updates)
        embeds = []
        for u in updates:
            desc = [u.summary]
            c = _containers_line(u)
            if c:
                desc.append(c)
            if u.notes:
                desc.append("")
                desc.append(u.notes[:1500])
            embed = {
                "title": f"{u.title}"[:256],
                "description": "\n".join(desc)[:4000],
                "color": next((FLAG_COLOR[f] for f in u.flags if f in FLAG_COLOR), COLORS[u.kind]),
                "author": {"name": _flag_line(u) or LABELS[u.kind]},
            }
            if u.url:
                embed["url"] = u.url
            embeds.append(embed)
        if errors:
            embeds.append({
                "title": "Some checks failed",
                "description": "\n".join(f"• {e}" for e in errors)[:4000],
                "color": COLORS["error"],
            })
        # Discord allows 10 embeds per message.
        for i in range(0, len(embeds), 10):
            payload = {"embeds": embeds[i:i + 10], "username": self.cfg.get("username") or "Lookout"}
            if i == 0 and updates:
                payload["content"] = f"**{_headline(updates)}**"
            if self.cfg.get("avatar_url"):
                payload["avatar_url"] = self.cfg["avatar_url"]
            r = requests.post(self.cfg["webhook_url"], json=payload, timeout=20)
            r.raise_for_status()


class Ntfy:
    name = "ntfy"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def send(self, updates: list[Update], errors: list[str]):
        updates = _ordered(updates)
        body = "\n".join(_markdown_lines(updates))
        if errors:
            body += ("\n\n" if body else "") + "**Some checks failed:**\n" + "\n".join(f"- {e}" for e in errors)
        headers = {
            "Title": _headline(updates) if updates else "Lookout: checks failed",
            "Markdown": "yes",
            "Priority": "high" if any("security" in u.flags for u in updates)
                        else str(self.cfg.get("priority") or "default"),
            "Tags": self.cfg.get("tags") or "package",
        }
        if len(updates) == 1 and updates[0].url:
            headers["Click"] = updates[0].url
        auth = None
        if self.cfg.get("token"):
            headers["Authorization"] = f"Bearer {self.cfg['token']}"
        elif self.cfg.get("username"):
            auth = (self.cfg["username"], self.cfg.get("password") or "")
        r = requests.post(self.cfg["url"], data=body.encode("utf-8"), headers=headers, auth=auth, timeout=20)
        r.raise_for_status()


class Gotify:
    name = "Gotify"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def send(self, updates: list[Update], errors: list[str]):
        updates = _ordered(updates)
        body = "\n".join(_markdown_lines(updates))
        if errors:
            body += ("\n\n" if body else "") + "**Some checks failed:**\n" + "\n".join(f"- {e}" for e in errors)
        payload = {
            "title": _headline(updates) if updates else "Lookout: checks failed",
            "message": body,
            "priority": max(int(self.cfg.get("priority") or 5), 8 if any("security" in u.flags for u in updates) else 0),
            "extras": {"client::display": {"contentType": "text/markdown"}},
        }
        if len(updates) == 1 and updates[0].url:
            payload["extras"]["client::notification"] = {"click": {"url": updates[0].url}}
        url = self.cfg["url"].rstrip("/") + "/message"
        r = requests.post(url, json=payload, headers={"X-Gotify-Key": self.cfg["token"]}, timeout=20)
        r.raise_for_status()


def build(notify_cfg: dict) -> list:
    out = []
    d = notify_cfg.get("discord") or {}
    if d.get("webhook_url"):
        out.append(Discord(d))
    n = notify_cfg.get("ntfy") or {}
    if n.get("url"):
        out.append(Ntfy(n))
    g = notify_cfg.get("gotify") or {}
    if g.get("url") and g.get("token"):
        out.append(Gotify(g))
    return out


def send_all(notifiers: list, updates: list[Update], errors: list[str]) -> bool:
    """True if at least one notifier delivered (or there were none to try)."""
    if not notifiers:
        log.warning("no notifiers configured; updates are only logged:\n%s", "\n".join(_plain_lines(updates)))
        return True
    ok = False
    for n in notifiers:
        try:
            n.send(updates, errors)
            log.info("sent %d update(s) to %s", len(updates), n.name)
            ok = True
        except Exception as e:
            log.error("sending to %s failed: %s", n.name, e)
    return ok
