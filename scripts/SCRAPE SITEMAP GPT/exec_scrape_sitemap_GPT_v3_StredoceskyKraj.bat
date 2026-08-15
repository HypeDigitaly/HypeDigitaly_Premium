@echo off
cd /d "%~dp0"
echo Spouštím scraping pro Středočeský kraj...
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_StredoceskyKraj_v3.json
pause
