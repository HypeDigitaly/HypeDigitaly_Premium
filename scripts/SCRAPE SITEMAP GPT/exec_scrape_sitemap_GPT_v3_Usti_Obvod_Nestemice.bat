@echo off
cd /d "%~dp0"
echo Spouštím scraping pro město Ústí nad Labem městský obvod Neštěmice...
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_Usti_Obvod_Nestemice_v3.json
pause
