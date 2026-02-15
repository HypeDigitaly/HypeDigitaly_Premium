"""GPT Scraper V3 - Modular web scraping and RAG content processing toolkit."""

from gpt_scraper_v3.config import load_configuration, get_config, ScraperConfig
from gpt_scraper_v3.logging_setup import setup_logging, get_logger
from gpt_scraper_v3.cli import main

__version__ = "3.0.0"
__all__ = [
    "load_configuration", "get_config", "ScraperConfig",
    "setup_logging", "get_logger", "main",
]
