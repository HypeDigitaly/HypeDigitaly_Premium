@echo off
echo Spouštím scraping pro město Ústí nad Labem městský obvod (https://www.mesto-ul.cz/cz/mapa-serveru.html)...
python scrape_sitemap_GPT_v2.py --config scrape_sitemap_GPT_config_Usti_Obvod_UstinadLabem_v2.json
pause