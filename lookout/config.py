"""Load config from a YAML file, with ${ENV_VAR} substitution and env-only fallbacks."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "interval": "6h",
    "run_on_start": True,
    "state_file": "/data/state.json",
    "notify_errors": False,
    "docker": {
        "enabled": True,
        "host": None,                 # None = DOCKER_HOST env or the local socket
        "watch_by_default": True,     # False = only containers labelled lookout.enable=true
        "check_new_tags": True,       # also report newer version tags (1.2.3 -> 1.3.0)
        "include_stopped": False,
        "exclude": [],                # container names or regexes to skip
    },
    "registries": {},                 # {"ghcr.io": {"username": ..., "password": ...}}
    "portainer": {
        "url": None,                  # e.g. https://192.168.1.10:9443
        "token": None,                # Portainer access token (My account -> Access tokens)
        "verify_tls": False,          # Portainer's default certificate is self-signed
        "watch_all": True,            # watch every Portainer environment's containers
        "allow_updates": True,        # show Update buttons that redeploy through Portainer
    },
    "github": {
        "token": None,
        "repos": [],
        "include_prereleases": False,
    },
    "notify": {
        "discord": {"webhook_url": None, "username": "Lookout", "avatar_url": None},
        "ntfy": {"url": None, "token": None, "username": None, "password": None,
                 "priority": "default", "tags": "package"},
        "gotify": {"url": None, "token": None, "priority": 5},
    },
}

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _merge(base: dict, over: dict) -> dict:
    out = deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides() -> dict:
    """Let a docker-compose file configure the basics without a config file."""
    e = os.environ.get
    o: dict = {"notify": {"discord": {}, "ntfy": {}, "gotify": {}}, "github": {}, "docker": {}, "portainer": {}}
    if e("PORTAINER_URL"):
        o["portainer"]["url"] = e("PORTAINER_URL")
    if e("PORTAINER_TOKEN"):
        o["portainer"]["token"] = e("PORTAINER_TOKEN")
    if e("LOOKOUT_INTERVAL"):
        o["interval"] = e("LOOKOUT_INTERVAL")
    if e("DISCORD_WEBHOOK_URL"):
        o["notify"]["discord"]["webhook_url"] = e("DISCORD_WEBHOOK_URL")
    if e("NTFY_URL"):
        o["notify"]["ntfy"]["url"] = e("NTFY_URL")
    if e("NTFY_TOKEN"):
        o["notify"]["ntfy"]["token"] = e("NTFY_TOKEN")
    if e("GOTIFY_URL"):
        o["notify"]["gotify"]["url"] = e("GOTIFY_URL")
    if e("GOTIFY_TOKEN"):
        o["notify"]["gotify"]["token"] = e("GOTIFY_TOKEN")
    if e("GITHUB_TOKEN"):
        o["github"]["token"] = e("GITHUB_TOKEN")
    if e("GITHUB_REPOS"):
        o["github"]["repos"] = [r.strip() for r in e("GITHUB_REPOS").split(",") if r.strip()]
    if e("LOOKOUT_WATCH_BY_DEFAULT"):
        o["docker"]["watch_by_default"] = e("LOOKOUT_WATCH_BY_DEFAULT").lower() in ("1", "true", "yes")
    return o


def load(path: str | None) -> dict:
    cfg = deepcopy(DEFAULTS)
    if path and Path(path).is_file():
        with open(path, encoding="utf-8") as f:
            cfg = _merge(cfg, _expand(yaml.safe_load(f) or {}))
    cfg = _merge(cfg, _env_overrides())
    cfg["interval_seconds"] = parse_interval(str(cfg["interval"]))
    return cfg


def parse_interval(text: str) -> int:
    """'90s', '30m', '6h', '1d', '1h30m', or a bare number of seconds."""
    text = text.strip().lower()
    if text.isdigit():
        return max(int(text), 60)
    total = 0
    for amount, unit in re.findall(r"(\d+)\s*([smhd])", text):
        total += int(amount) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if total <= 0:
        raise ValueError(f"can't read interval {text!r}; use something like 30m, 6h or 1d")
    return max(total, 60)
