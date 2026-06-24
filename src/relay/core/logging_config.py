"""Relay centralised logging configuration.

Call ``configure_logging()`` once at each process/Lambda entrypoint before any
other work so the root logger has a handler and the level is correct.  The
function is idempotent — repeated calls (e.g. in tests that import multiple
entrypoints) are safe.
"""

from __future__ import annotations

import logging
import os
import sys

_configured: bool = False

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str | None = None) -> None:
    """Configure the root logger with a single StreamHandler to stdout.

    Args:
        level: Optional log level string (e.g. ``"DEBUG"``, ``"INFO"``).  When
               *None* the value is read from the ``LOG_LEVEL`` environment
               variable, defaulting to ``"INFO"`` if that is also unset.
               Invalid level strings are silently demoted to ``"INFO"``.

    The function is idempotent: the second and subsequent calls are no-ops so
    it is safe to call from every entrypoint (Lambda handler, ``main()``,
    test setup).
    """
    global _configured
    if _configured:
        return

    raw = level or os.environ.get("LOG_LEVEL", "INFO")
    numeric = getattr(logging, raw.upper(), None)
    if not isinstance(numeric, int):
        numeric = logging.INFO

    root = logging.getLogger()
    root.setLevel(numeric)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(numeric)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)

    _configured = True
