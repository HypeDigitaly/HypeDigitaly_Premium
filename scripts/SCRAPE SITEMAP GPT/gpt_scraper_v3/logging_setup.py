"""Logging configuration for GPT Scraper V3."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gpt_scraper_v3.config import ScraperConfig

logger: logging.Logger = logging.getLogger("gpt_scraper_v3")


def setup_logging(cfg: ScraperConfig) -> None:
    """Configure logging with file and console handlers.

    Truncates the log file at the start of each run for a clean log,
    then sets up a RotatingFileHandler (as a safety net for long runs)
    and a StreamHandler for console output.
    """
    if not cfg.LOG_DIR or not cfg.LOG_FILE:
        raise ValueError("LOG_DIR and LOG_FILE must be set before setup_logging()")

    os.makedirs(cfg.LOG_DIR, exist_ok=True)

    # Truncate log file for a fresh start each run (V2 parity)
    with open(cfg.LOG_FILE, "w"):
        pass

    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False  # Prevent duplicate console output via root logger

    # F1: include the thread name so per-URL lines under parallel workers are
    # attributable to a worker. Shared by both file + console handlers below.
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s"
    )

    file_handler = RotatingFileHandler(
        cfg.LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)


def get_logger() -> logging.Logger:
    """Get the package logger."""
    return logger
