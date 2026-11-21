"""Entry point: python -m lookout [--once] [--dry-run] [--test-notify] [--config PATH]"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import sys
import threading
import time

from . import __version__, config, notify, state as state_mod
from .docker_watch import DockerWatcher
from .github_watch import GitHubWatcher
from .models import Update
from .registry import RegistryClient

log = logging.getLogger("lookout")


def run_once(cfg: dict, state: dict, docker_watcher, github_watcher, notifiers, dry_run=False) -> list[Update]:
    updates: list[Update] = []
    errors: list[str] = []
    docker_ok = True

    if docker_watcher is not None:
        try:
            u, e = docker_watcher.check()
            updates += u
            errors += e
            docker_ok = not e
        except Exception as e:
            docker_ok = False
            errors.append(f"Docker: {e}")
            log.error("could not list containers: %s", e)

    if github_watcher is not None:
        u, e = github_watcher.check(state)
        updates += u
        errors += e

    fresh = [u for u in updates if state.get(u.key) != u.value]

    # Image updates that are no longer pending (you pulled them) are forgotten,
    # so a later update to the same tag is announced again. Only prune after a
    # clean Docker pass, so a registry hiccup doesn't cause repeat notices.
    if docker_ok and docker_watcher is not None:
        current = {u.key for u in updates}
        for key in [k for k in state if k.startswith("docker:") and k not in current]:
            del state[key]

    errors_to_send: list[str] = []
    if errors and cfg.get("notify_errors"):
        digest = hashlib.sha256("\n".join(sorted(errors)).encode()).hexdigest()
        if state.get("errors:last") != digest:
            errors_to_send = errors
            state["errors:last"] = digest
    elif not errors:
        state.pop("errors:last", None)

    log.info("check done: %d pending, %d new, %d error(s)", len(updates), len(fresh), len(errors))

    if dry_run:
        for u in fresh:
            print(f"[{u.kind}] {u.title}: {u.summary}" + (f"  ({', '.join(u.containers)})" if u.containers else ""))
        for e in errors:
            print(f"[error] {e}")
        return fresh

    if fresh or errors_to_send:
        if notify.send_all(notifiers, fresh, errors_to_send):
            for u in fresh:
                state[u.key] = u.value
        elif errors_to_send:
            state.pop("errors:last", None)  # retry next time
    state["last_run"] = int(time.time())
    return fresh


def build(cfg: dict):
    registry = RegistryClient(cfg.get("registries"))
    dcfg = cfg.get("docker") or {}
    docker_watcher = DockerWatcher(dcfg, registry) if dcfg.get("enabled", True) else None
    gcfg = cfg.get("github") or {}
    github_watcher = GitHubWatcher(gcfg) if gcfg.get("repos") else None
    return docker_watcher, github_watcher, notify.build(cfg.get("notify") or {})


def test_notify(notifiers) -> int:
    sample = [Update(kind="release", key="test", value="test", title="lookout/test",
                     current="v1.0.0", latest="v1.1.0", url="https://github.com",
                     notes="This is a test notification from Lookout.")]
    if not notifiers:
        log.error("no notifiers configured; set a Discord webhook, ntfy URL or Gotify URL+token")
        return 1
    ok = True
    for n in notifiers:
        try:
            n.send(sample, [])
            log.info("test sent to %s", n.name)
        except Exception as e:
            ok = False
            log.error("%s failed: %s", n.name, e)
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="lookout", description="Watch Docker images and GitHub releases for updates.")
    p.add_argument("--config", default=os.environ.get("LOOKOUT_CONFIG", "/config/config.yaml"))
    p.add_argument("--once", action="store_true", help="check once and exit")
    p.add_argument("--dry-run", action="store_true", help="check once, print what would be sent, change nothing")
    p.add_argument("--test-notify", action="store_true", help="send a sample notification and exit")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOOKOUT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("urllib3", "docker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = config.load(args.config)
    docker_watcher, github_watcher, notifiers = build(cfg)
    log.info("Lookout %s: docker=%s, github repos=%d, notifiers=%s, every %ss",
             __version__, "on" if docker_watcher else "off",
             len((cfg.get("github") or {}).get("repos") or []),
             ",".join(n.name for n in notifiers) or "none", cfg["interval_seconds"])

    if args.test_notify:
        return test_notify(notifiers)

    state_path = os.environ.get("LOOKOUT_STATE_FILE", cfg["state_file"])

    def cycle(dry=False):
        st = state_mod.load(state_path)
        try:
            run_once(cfg, st, docker_watcher, github_watcher, notifiers, dry_run=dry)
        finally:
            if not dry:
                state_mod.save(state_path, st)

    if args.dry_run:
        cycle(dry=True)
        return 0
    if args.once:
        cycle()
        return 0

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    first = True
    while not stop.is_set():
        if not first or cfg.get("run_on_start", True):
            try:
                cycle()
            except Exception:
                log.exception("check failed")
        first = False
        stop.wait(cfg["interval_seconds"])
    log.info("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
