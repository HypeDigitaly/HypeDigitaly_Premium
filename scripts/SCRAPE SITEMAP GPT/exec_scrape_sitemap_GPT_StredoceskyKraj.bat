@echo off
cd /d "%~dp0"
echo Spouštím scraping pro Středočeský kraj...
python scrape_sitemap_GPT.py --config scrape_sitemap_GPT_config_StredoceskyKraj.json
pause