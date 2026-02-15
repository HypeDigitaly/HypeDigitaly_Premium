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

    Sets up a RotatingFileHandler (append mode) and a StreamHandler.
    Fix M2: Does NOT truncate the log file on startup -- RotatingFileHandler
    manages the file lifecycle automatically.

    Args:
        cfg: Loaded scraper configuration providing LOG_DIR and LOG_FILE.
    """
    os.makedirs(cfg.LOG_DIR, exist_ok=True)  # type: ignore[arg-type]
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)  # type: ignore[arg-type]

    logging.basicConfig(level=logging.INFO)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = RotatingFileHandler(
        cfg.LOG_FILE,  # type: ignore[arg-type]
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
