"""Logging configuration shared by the GUI, the CLI and the pipeline."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Final

ROOT_LOGGER: Final[str] = "fbxconv"
_CONSOLE_FORMAT: Final[str] = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_FILE_FORMAT: Final[str] = "%(asctime)s %(levelname)-7s %(name)s:%(lineno)d %(message)s"
_DATE_FORMAT: Final[str] = "%H:%M:%S"


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the package logger."""
    if not name or name == ROOT_LOGGER:
        return logging.getLogger(ROOT_LOGGER)
    if name.startswith(ROOT_LOGGER + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")


def configure_logging(
    level: int = logging.INFO,
    *,
    log_file: Path | None = None,
    quiet_console: bool = False,
) -> logging.Logger:
    """Install console/file handlers on the package logger.

    Idempotent: repeated calls replace existing handlers rather than stacking
    them, which matters because the GUI reconfigures logging per run.
    """
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    if not quiet_console:
        console = logging.StreamHandler(stream=sys.stderr)
        console.setLevel(level)
        console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT))
        logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(file_handler)

    return logger


class CallbackHandler(logging.Handler):
    """Forwards formatted records to a plain callable (used to feed the GUI log pane)."""

    def __init__(self, callback) -> None:
        super().__init__()
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        try:
            self._callback(record.levelno, self.format(record))
        except Exception:  # pragma: no cover - never let logging kill a run
            self.handleError(record)


__all__ = ["ROOT_LOGGER", "CallbackHandler", "configure_logging", "get_logger"]
