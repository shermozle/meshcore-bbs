"""Logging configuration."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import LoggingConfig


def configure_logging(cfg: LoggingConfig) -> None:
    level = getattr(logging, cfg.level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    # Remove any default handlers (e.g. when re-configuring under tests).
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if cfg.path:
        try:
            Path(cfg.path).parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.TimedRotatingFileHandler(
                cfg.path, when="midnight", backupCount=14, utc=True
            )
            handler.setFormatter(fmt)
            root.addHandler(handler)
        except Exception as e:
            root.warning("could not open log file %s: %s", cfg.path, e)
