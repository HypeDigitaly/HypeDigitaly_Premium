"""Drop-in replacement for scrape_sitemap_GPT_v2.py"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gpt_scraper_v3.cli import build_argument_parser, main

if __name__ == "__main__":
    parser = build_argument_parser()
    args = parser.parse_args()
    main(args)
