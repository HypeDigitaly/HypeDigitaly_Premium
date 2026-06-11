"""Entry point: python -m gpt_scraper_v3"""
import sys
from gpt_scraper_v3.cli import build_argument_parser, main

if __name__ == "__main__":
    parser = build_argument_parser()
    args = parser.parse_args()
    # Bug 8: propagate main()'s int exit code to the OS (guard None defensively).
    rc = main(args)
    sys.exit(rc if isinstance(rc, int) else 0)
