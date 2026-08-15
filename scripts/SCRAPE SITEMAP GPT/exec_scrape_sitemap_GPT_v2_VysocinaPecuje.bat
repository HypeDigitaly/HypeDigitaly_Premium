@echo off
cd /d "%~dp0"
echo Spouštím scraping pro VysocinaPecuje...
python scrape_sitemap_GPT_v2.py --config scrape_sitemap_GPT_config_VysocinaPecuje_v2.json
pause