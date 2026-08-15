@echo off
cd /d "%~dp0"
echo Spouštím scraping pro město Ústí nad Labem...
python scrape_sitemap_GPT.py --config scrape_sitemap_GPT_config_Usti.json
pause