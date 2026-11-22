"""Entry point.

  python -m lookout                 dashboard on port 18099 + checks on the interval
  python -m lookout --once          check once, notify, exit
  python -m lookout --dry-run       check once, print, change nothing
  python -m lookout --test-notify   send a sample notice and exit
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from . import __version__, config, notify
from .checker import Checker, effective_config
from .models import Update
from .scheduler import Scheduler
from .store import Store

log = logging.getLogger("lookout")


def test_notify(notifiers) -> int:
    sample = [Update(kind="release", key="test", value="test", title="Lookout test",
                     current="v1.0.0", latest="v1.1.0",
                     notes="If you can see this, notifications from Lookout work.")]
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
    for noisy in ("urllib3", "docker", "waitress"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    base = config.load(args.config)
    data_dir = os.environ.get("LOOKOUT_DATA", os.path.dirname(base["state_file"]) or "/data")
    store = Store(os.path.join(data_dir, "lookout.json"),
                  legacy_state=os.environ.get("LOOKOUT_STATE_FILE", base["state_file"]))
    store.seed_repos((base.get("github") or {}).get("repos") or [])
    cfg = effective_config(base, store)
    notifiers = notify.build(cfg.get("notify") or {})

    if args.test_notify:
        return test_notify(notifiers)

    checker = Checker(base, store)
    if args.dry_run:
        checker.run(dry_run=True)
        return 0
    if args.once:
        checker.run()
        return 0

    port = int(os.environ.get("LOOKOUT_PORT", "18099"))
    log.info("Lookout %s: dashboard on port %s, notifiers=%s, checks every %ss",
             __version__, port or "off", ",".join(n.name for n in notifiers) or "none", cfg["interval_seconds"])

    scheduler = Scheduler(checker, run_on_start=bool(cfg.get("run_on_start", True)))
    scheduler.start()

    stop = threading.Event()

    def _stop(*_):
        stop.set()
        scheduler.stop()
        if server is not None:
            server.close()

    server = None
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if port:
        from waitress import create_server
        from .web import create_app
        app = create_app(base, store, scheduler, password=os.environ.get("LOOKOUT_PASSWORD") or None)
        server = create_server(app, host=os.environ.get("LOOKOUT_HOST", "0.0.0.0"), port=port, threads=4)
        try:
            server.run()
        except OSError:
            if not stop.is_set():
                raise
    else:
        stop.wait()
    log.info("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
