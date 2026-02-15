@echo off
echo Spouštím scraping pro město Ústí nad Labem městský obvod (https://www.mesto-ul.cz/cz/mapa-serveru.html)...
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_Usti_Obvod_UstinadLabem_v3.json
pause
