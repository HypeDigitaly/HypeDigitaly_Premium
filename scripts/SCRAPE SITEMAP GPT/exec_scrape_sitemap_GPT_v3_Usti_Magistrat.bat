@echo off
echo Spouštím scraping pro magistrát Ústí nad Labem (https://www.usti.cz/cz/mapa-serveru.html)...
python run_gpt_scraper_v3.py --config scrape_sitemap_GPT_config_Usti_Magistrat_v3.json
pause
