@echo off
cd /d "%~dp0"
echo Spouštím scraping pro město Ústí nad Labem městský obvod Neštěmice...
python scrape_sitemap_GPT_v2.py --config scrape_sitemap_GPT_config_Usti_Obvod_Nestemice_v2.json
pause