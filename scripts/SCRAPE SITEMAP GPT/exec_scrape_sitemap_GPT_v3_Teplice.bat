@echo off
cd /d "%~dp0"
echo Spouštím scraping pro Teplice...
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_Teplice_v3.json
pause
