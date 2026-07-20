"""Central logging configuration with file rotation.

H-Op1: long-running forven processes (lab worker, bot runner, API server)
previously wrote unbounded log files. A RotatingFileHandler caps each log
at ~50 MiB and keeps 3 backups so disks don't fill up silently.

Import `setup_rotating_file_logger` from here rather than calling
`logging.FileHandler` directly; the two are drop-in except that this
helper automatically adds a stdout handler, sets a consistent format, and
wires in the correlation-id filter when it's available.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB per rotation
DEFAULT_BACKUP_COUNT = 3  # keep .1 .2 .3 alongside the live file
DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _env_int(name: str, default: int) -> int:
    """Read a positive integer override from the environment.

    Falsy or malformed values fall back to the default so bad config
    never silences rotation entirely.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def setup_rotating_file_logger(
    log_path: str | os.PathLike[str],
    *,
    level: int = logging.INFO,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    fmt: str = DEFAULT_FORMAT,
    also_stdout: bool = True,
    force: bool = True,
) -> None:
    """Configure the root logger with a rotating file handler.

    log_path: target log file. Parent directory is created if missing.
    level: root log level (defaults to INFO).
    max_bytes: rotation threshold (FORVEN_LOG_MAX_BYTES env override).
    backup_count: number of rotated files to keep
                  (FORVEN_LOG_BACKUP_COUNT env override).
    also_stdout: also attach a StreamHandler to stdout so operators can
                 tail logs live in the terminal.
    force: pass through to logging.basicConfig; True replaces any existing
           handlers so repeated calls don't duplicate output.
    """
    resolved_max = _env_int("FORVEN_LOG_MAX_BYTES", max_bytes or DEFAULT_MAX_BYTES)
    resolved_backup = _env_int("FORVEN_LOG_BACKUP_COUNT", backup_count or DEFAULT_BACKUP_COUNT)

    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(fmt)

    handlers: list[logging.Handler] = []

    file_handler = logging.handlers.RotatingFileHandler(
        str(path),
        mode="a",
        maxBytes=resolved_max,
        backupCount=resolved_backup,
        encoding="utf-8",
        delay=True,  # don't open until first write; matches FileHandler default
    )
    file_handler.setFormatter(formatter)
    handlers.append(file_handler)

    if also_stdout:
        # Windows consoles/redirects default to cp1252; a log message containing
        # e.g. '→' then raises UnicodeEncodeError inside logging and the whole
        # line is replaced by a "--- Logging error ---" traceback (observed
        # masking backend diagnostics 2026-07-17). Reconfigure the stream to
        # UTF-8 with replacement so console logging can never throw on content.
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # non-TextIOWrapper stand-ins (tests, captured streams)
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    logging.basicConfig(level=level, handlers=handlers, force=force)

    # Attach the correlation-id filter so any downstream formatter that
    # references %(request_id)s works. The filter is cheap and installing
    # it here keeps the call site (cli.py / runner.py) minimal.
    try:
        from forven.correlation import RequestIdLogFilter

        root = logging.getLogger()
        if not any(isinstance(f, RequestIdLogFilter) for f in root.filters):
            root.addFilter(RequestIdLogFilter())
    except Exception:
        # correlation module is optional at import time; ignore.
        pass

    # Attach the secret-redaction filter so a stray log line containing an API
    # key / bearer token / wallet private key is scrubbed before it hits the
    # file or console — covering ALL loggers, not just tool output.
    # NOTE: a logging.Filter on the root LOGGER only runs for records emitted
    # through the root logger itself; to guarantee coverage of records that
    # propagate up from named child loggers, the filter is also attached to
    # each handler (handlers see every propagated record).
    try:
        from forven.redact import RedactingLogFilter

        redaction_filter = RedactingLogFilter()
        for handler in handlers:
            if not any(isinstance(f, RedactingLogFilter) for f in handler.filters):
                handler.addFilter(redaction_filter)
    except Exception:
        # redact module is optional at import time; never block logging setup.
        pass


__all__ = [
    "DEFAULT_BACKUP_COUNT",
    "DEFAULT_FORMAT",
    "DEFAULT_MAX_BYTES",
    "setup_rotating_file_logger",
]
