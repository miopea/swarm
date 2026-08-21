"""Centralized logging configuration for swarm."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from swarm.paths import state_path_str

_DEFAULT_LOG_FILE = state_path_str("swarm.log")
_MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 3


class JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    # Fields from extra={} to promote to top-level JSON keys
    _EXTRA_FIELDS = frozenset(
        {
            "request_id",
            "method",
            "path",
            "status",
            "latency_ms",
            "worker",
            "action",
            "task_id",
            "count",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Promote known extra fields to top-level keys
        for key in self._EXTRA_FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info and record.exc_info[1]:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(
    level: str = "WARNING",
    log_file: str | None = None,
    stderr: bool = True,
    json_format: bool = False,
) -> logging.Logger:
    """Configure and return the swarm root logger.

    Called once at startup (CLI entry point).  All modules use
    ``logging.getLogger("swarm.<module>")`` which inherit this config.

    Parameters
    ----------
    level:
        Logging verbosity (DEBUG, INFO, WARNING, ERROR).
    log_file:
        Explicit log file path.  When *None* the default
        ``~/.swarm/swarm.log`` is used so there is always a file to
        check for troubleshooting.
    stderr:
        Whether to also emit log records to stderr.
    """
    logger = logging.getLogger("swarm")

    # Close and clear existing handlers so re-configuration works correctly
    for h in logger.handlers[:]:
        h.close()
    logger.handlers.clear()

    logger.setLevel(getattr(logging, level.upper(), logging.WARNING))

    if json_format:
        fmt = JsonFormatter(datefmt="%Y-%m-%dT%H:%M:%S")
    else:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # stderr handler
    if stderr:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(fmt)
        logger.addHandler(stderr_handler)

    # File handler — always present for troubleshooting
    file_path = Path(log_file or _DEFAULT_LOG_FILE).expanduser()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        str(file_path),
        maxBytes=_MAX_LOG_BYTES,
        backupCount=_BACKUP_COUNT,
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``swarm`` namespace."""
    return logging.getLogger(f"swarm.{name}")


def setup_logging_from_cli(cli_obj: dict, cfg, *, stderr: bool = True) -> logging.Logger:
    """Resolve logging settings from CLI flags + config and call :func:`setup_logging`.

    The CLI's ``serve``, ``daemon``, and ``test`` subcommands all
    derive log_level / log_file / log_format the same way:
    ``cli_obj.get("...") or cfg....``.  Pre-Phase-F of the duplication
    sweep this 8-line block was copy-pasted across three call sites in
    ``cli.py`` (lines ~837, ~913, ~1053).  This helper is the single
    place to change that policy.

    Parameters
    ----------
    cli_obj:
        Click context object (``ctx.obj``) carrying ``log_level``,
        ``log_file``, and ``log_format`` overrides from the top-level
        flags.  ``{}`` is fine when none are set.
    cfg:
        Loaded :class:`swarm.config.SwarmConfig` providing the
        config-file fallback values (``cfg.log_level``, ``cfg.log_file``).
    stderr:
        Forwarded to :func:`setup_logging`.  Each subcommand currently
        passes True so logs also tail to the terminal.
    """
    level = cli_obj.get("log_level") or cfg.log_level
    log_file = cli_obj.get("log_file") or cfg.log_file
    fmt = cli_obj.get("log_format", "text")
    return setup_logging(
        level=level,
        log_file=log_file,
        stderr=stderr,
        json_format=fmt == "json",
    )
