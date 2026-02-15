"""Entry point: python -m gpt_scraper_v3"""
from gpt_scraper_v3.cli import build_argument_parser, main

if __name__ == "__main__":
    parser = build_argument_parser()
    args = parser.parse_args()
    main(args)
