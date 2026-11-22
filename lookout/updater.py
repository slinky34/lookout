"""Update a watched image's containers through Portainer: redeploy their stacks with a
fresh pull, or recreate standalone containers with a fresh pull."""

from __future__ import annotations

import logging
import os
import threading
import time
from copy import deepcopy

from .portainer import PortainerClient, classify

log = logging.getLogger(__name__)


def _self_ids() -> set[str]:
    """Ways to recognise Lookout's own container (Docker sets HOSTNAME to the short id)."""
    ids = set()
    h = os.environ.get("HOSTNAME")
    if h:
        ids.add(h)
    return ids


def _is_self(container_id: str | None) -> bool:
    return bool(container_id) and any(container_id.startswith(s) for s in _self_ids() if len(s) >= 12)


def plan(client: PortainerClient, row: dict) -> dict:
    """What updating this row would do. Stacks are deduplicated: one redeploy covers
    every container of that stack."""
    idx = client.index()
    stacks: dict[int, dict] = {}
    containers: list[dict] = []
    skipped: list[dict] = []
    stack_by_id = {s["Id"]: s for s in idx.stacks.values()}
    for m in row.get("members") or []:
        mgr = classify(m, idx)
        if not mgr["can_update"]:
            skipped.append({"name": m["name"], "why": mgr.get("why") or "Can't be updated from here"})
            continue
        if mgr["type"] == "stack":
            s = stack_by_id.get(mgr["stack_id"])
            entry = stacks.setdefault(mgr["stack_id"], {
                "kind": "stack", "stack": s, "name": mgr["stack"], "git": mgr["git"],
                "endpoint_name": mgr["endpoint_name"], "containers": [], "self": False,
            })
            entry["containers"].append(m["name"])
            entry["self"] = entry["self"] or _is_self(mgr.get("container_id"))
        else:
            containers.append({
                "kind": "container", "name": m["name"], "endpoint": mgr["endpoint"],
                "endpoint_name": mgr["endpoint_name"], "container_id": mgr["container_id"],
                "self": _is_self(mgr.get("container_id")),
            })
    steps = list(stacks.values()) + containers
    steps.sort(key=lambda s: s["self"])   # Lookout itself goes last
    return {"steps": steps, "skipped": skipped}


def describe(p: dict) -> dict:
    """The plan without Portainer's raw objects, for the dashboard."""
    out = []
    for s in p["steps"]:
        if s["kind"] == "stack":
            text = f"Redeploy stack “{s['name']}” with a fresh pull" + (" from Git" if s["git"] else "")
            text += f" ({', '.join(s['containers'])})"
        else:
            text = f"Recreate container “{s['name']}” with a fresh pull"
        if s["endpoint_name"]:
            text += f" on {s['endpoint_name']}"
        out.append({"text": text, "self": s["self"]})
    return {"steps": out, "skipped": deepcopy(p["skipped"])}


def run(client: PortainerClient, p: dict) -> list[dict]:
    results = []
    for s in p["steps"]:
        label = f"stack {s['name']}" if s["kind"] == "stack" else f"container {s['name']}"
        if s["self"]:
            # Updating Lookout restarts it, which would cut this request off.
            # Answer first, then do it.
            def later(step=s):
                time.sleep(1.5)
                try:
                    _do(client, step)
                except Exception as e:
                    log.error("updating Lookout itself failed: %s", e)
            threading.Thread(target=later, daemon=True).start()
            results.append({"target": label, "ok": True, "note": "Lookout is restarting to update itself"})
            continue
        try:
            _do(client, s)
            results.append({"target": label, "ok": True})
            log.info("updated %s through Portainer", label)
        except Exception as e:
            results.append({"target": label, "ok": False, "error": str(e)})
            log.warning("updating %s failed: %s", label, e)
    return results


def _do(client: PortainerClient, s: dict):
    if s["kind"] == "stack":
        client.redeploy_stack(s["stack"])
    else:
        client.recreate_container(s["endpoint"], s["container_id"])
