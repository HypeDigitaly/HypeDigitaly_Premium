@echo off
cd /d "%~dp0"
echo Spouštím scraping pro Kraj Vysočina...
python scrape_sitemap_GPT_v2.py --config scrape_sitemap_GPT_config_KrajVysocina_v2.json
pause