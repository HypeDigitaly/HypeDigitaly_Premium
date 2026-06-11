"""Drop-in replacement for scrape_sitemap_GPT_v2.py"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gpt_scraper_v3.cli import build_argument_parser, main

if __name__ == "__main__":
    parser = build_argument_parser()
    args = parser.parse_args()
    # Bug 8: propagate main()'s int exit code to the OS. main() always returns an
    # int (0 success / 1 config-or-runtime error / 130 Ctrl+C); guard defensively
    # in case any path ever returns None so we never sys.exit(None) (treated as 0).
    rc = main(args)
    sys.exit(rc if isinstance(rc, int) else 0)
