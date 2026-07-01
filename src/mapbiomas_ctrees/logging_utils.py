"""Logging utilities for command-line execution."""

from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(log_directory: Path, verbose: bool = False) -> None:
    """Write workflow logs to the terminal and to outputs/logs/workflow.log."""
    log_directory.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_directory / "workflow.log", encoding="utf-8"),
        ],
        force=True,
    )
