@echo off
cd /d "%~dp0"
echo Spouštím scraping pro Litoměřice...
python scrape_sitemap_GPT_v2.py --config scrape_sitemap_GPT_config_Litomerice_v2.json
pause