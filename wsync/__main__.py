"""`python -m wsync [--only deku|ta|collection] [--dry-run|--apply] [--no-report] [--accept-feed-count N]`."""

from __future__ import annotations

import logging
import os
import signal
import sys

from wsync.run import ConfigError, RedactingFilter, Terminated, config_from, run


def _on_sigterm(signum, frame):
    """Kubernetes sends SIGTERM at the job's deadline or on shutdown: stop the run, once."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise Terminated()


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = config_from(sys.argv[1:] if argv is None else argv, os.environ)
    except ConfigError as e:
        print(f"wsync: {e}", file=sys.stderr)
        return 2
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter(cfg.secrets()))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        return run(cfg)  # catches Terminated: saves state, reports, returns 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
