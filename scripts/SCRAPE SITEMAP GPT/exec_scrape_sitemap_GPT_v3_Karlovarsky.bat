@echo off
cd /d "%~dp0"
echo Spouštím scraping pro Karlovarský kraj...
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_Karlovarsky_v3.json
pause
