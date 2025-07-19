# 🕷️ Scrape Sitemap GPT - Kompletní Průvodce

Univerzální skript pro stahování a zpracování obsahu webových stránek pomocí AI služeb s pokročilými funkcemi pro obnovu zpracování a správu duplicitních souborů.

## 📋 Obsah

1. [Přehled funkcí](#přehled-funkcí)
2. [Instalace a konfigurace](#instalace-a-konfigurace)
3. [Konfigurační soubor](#konfigurační-soubor)
4. [Způsoby použití](#způsoby-použití)
5. [Parametry příkazové řádky](#parametry-příkazové-řádky)
6. [Režimy zpracování](#režimy-zpracování)
7. [Funkce obnovy (Resume)](#funkce-obnovy-resume)
8. [Vector Store integrace](#vector-store-integrace)
9. [Příklady použití](#příklady-použití)
10. [Řešení problémů](#řešení-problémů)
11. [Časté dotazy](#časté-dotazy)

---

## 🎯 Přehled funkcí

### **Základní funkcionalita:**
- ✅ **Automatické stahování** obsahu z HTML sitemap a XML sitemap
- ✅ **RSS feeds zpracování** - podpora RSS 2.0 a Atom formátů
- ✅ **AI služby integrace** - Jina AI a Firecrawl pro převod na markdown
- ✅ **Metadata extrakce** - automatické získávání titulků, cest, dat modifikace
- ✅ **Inteligentní filtrování** - kontrola posledních změn pro efektivní zpracování

### **Pokročilé funkce:**
- 🔄 **Resume funkcionalita** - pokračování po přerušení bez duplikátů
- 🚀 **Vector Store podpora** - automatické nahrávání do OpenAI Vector Store
- 🎯 **Deduplication** - inteligentní správa duplicitních souborů
- 📊 **Chunking strategie** - optimalizace pro AI asistenty
- 🏷️ **Multi-projekt podpora** - nezávislé konfigurace pro různé weby
- 📈 **Detailní logování** - kompletní audit trail pro každý běh

---

## ⚙️ Instalace a konfigurace

### **1. Požadavky:**
```bash
pip install requests beautifulsoup4 python-dateutil
```

### **2. API klíče:**
Potřebujete následující API klíče:
- **Jina AI API** - https://jina.ai/
- **Firecrawl API** - https://firecrawl.dev/
- **OpenAI API** - https://platform.openai.com/ (volitelné pro Vector Store)

### **3. Struktura souborů:**
```
projekt/
├── scrape_sitemap_GPT.py          # Hlavní skript
├── config_example.json            # Vzorová konfigurace
├── scrape_sitemap_GPT_config_Web.json  # Vaše konfigurace
├── Web_files/                     # Výstupní soubory (auto-generováno)
├── Web_logs/                      # Log soubory (auto-generováno)
└── README_CZ.md                   # Tento soubor
```

---

## 📝 Konfigurační soubor

### **Kompletní příklad konfigurace:**
```json
{
  "website": {
    "base_url": "https://example.com/",
    "sitemap_url": "https://example.com/sitemap",
    "xml_sitemap_url": "https://example.com/sitemap.xml",
    "blacklisted_urls": [
      "https://example.com/admin",
      "https://example.com/private"
    ],
    "rss_feeds": [
      "https://example.com/rss",
      "https://example.com/feed.xml"
    ]
  },
  "api_keys": {
    "jina_ai": "jina_xxxxxxxxxxxxxxxxxxxxxxxx",
    "firecrawl": "fc-xxxxxxxxxxxxxxxxxxxxxxxx",
    "openai": "sk-xxxxxxxxxxxxxxxxxxxxxxxx"
  },
  "content_providers": {
    "jina": {
      "name": "jina",
      "remove_selectors": ".advertisement,.popup,.cookie-banner"
    },
    "firecrawl": {
      "name": "firecrawl"
    },
    "provider_sequence": "jina,firecrawl"
  },
  "vector_store": {
    "id": "vs_xxxxxxxxxxxxxxxxxxxxxxxx",
    "enable_deduplication": true,
    "chunking_strategy": "auto",
    "max_chunk_size": 800,
    "chunk_overlap": 400
  },
  "http_settings": {
    "request_timeout": 150,
    "retry_codes": [500, 502, 503, 504, 524],
    "retry_count": 3,
    "backoff_factor": 0.3
  },
  "processing": {
    "check_last_modified": true,
    "max_filename_length": 200
  },
  "script_info": {
    "name": "scrape_sitemap_universal",
    "version": "1.0.0"
  }
}
```

### **Vysvětlení jednotlivých sekcí:**

#### **🌐 Website sekce:**
- `base_url` - Základní URL webu (povinné)
- `sitemap_url` - URL HTML sitemap (volitelné)
- `xml_sitemap_url` - URL XML sitemap (doporučené)
- `blacklisted_urls` - Seznam URL k přeskočení
- `rss_feeds` - Seznam RSS/Atom feedů k zpracování

#### **🔑 API Keys sekce:**
- `jina_ai` - API klíč pro Jina AI službu
- `firecrawl` - API klíč pro Firecrawl službu  
- `openai` - API klíč pro OpenAI (Vector Store)

#### **🔧 Content Providers:**
- `remove_selectors` - CSS selektory k odstranění (reklamy, popup, atd.)
- `provider_sequence` - Pořadí pokusů služeb ("jina,firecrawl" nebo "firecrawl,jina")

#### **📦 Vector Store:**
- `id` - ID vašeho OpenAI Vector Store
- `enable_deduplication` - Zapnout/vypnout kontrolu duplicitů
- `chunking_strategy` - "auto" nebo "static"
- `max_chunk_size` - Maximální velikost chunku (tokeny)
- `chunk_overlap` - Překryv mezi chunky (tokeny)

---

## 🚀 Způsoby použití

### **1. Základní použití:**
```bash
python scrape_sitemap_GPT.py
```
- Použije `config.json` v aktuálním adresáři
- Zpracuje HTML sitemap + XML sitemap + RSS feeds
- Uloží soubory do `default_files/` adresáře

### **2. S vlastní konfigurací:**
```bash
python scrape_sitemap_GPT.py --config muj_web_config.json
```
- Použije vaši konfiguraci
- Vytvoří `MujWeb_files/` a `MujWeb_logs/` adresáře
- Nezávislé zpracování pro každou konfiguraci

### **3. S debug módem:**
```bash
python scrape_sitemap_GPT.py --config web.json --debug
```
- Detailní výstup všech operací
- Užitečné pro ladění problémů
- Zobrazí všechny HTTP požadavky a odpovědi

---

## 📋 Parametry příkazové řádky

### **Konfigurační parametry:**
```bash
--config SOUBOR              # Cesta ke konfiguračnímu souboru
--base-url URL               # Přepsat base_url z konfigurace
--sitemap-url URL            # Přepsat sitemap_url z konfigurace
--xml-sitemap-url URL        # Přepsat xml_sitemap_url z konfigurace
--output-dir ADRESÁŘ         # Přepsat výstupní adresář
```

### **API klíče (přepsání konfigurace):**
```bash
--jina-api-key KLÍČ          # Přepsat Jina AI API klíč
--firecrawl-api-key KLÍČ     # Přepsat Firecrawl API klíč
--openai-api-key KLÍČ        # Přepsat OpenAI API klíč
```

### **Zpracovací režimy:**
```bash
--rss-only                   # Zpracovat pouze RSS feeds
--sitemap-only              # Zpracovat pouze HTML sitemap
--xml-only                  # Zpracovat pouze XML sitemap
--legacy-html-parsing       # Použít legacy HTML parsing místo generalizovaného
--resume                    # Pokračovat od místa přerušení
--test-resume               # Otestovat resume funkcionalitu
```

### **Kontrolní parametry:**
```bash
--debug                     # Zapnout detailní debug výstup
--no-check-modified         # Vypnout kontrolu posledních změn
--verbose-url-matching      # Zobrazit detailní info o párování URL
--jina-remove-selectors "CSS"  # CSS selektory k odstranění
```

### **Vector Store parametry:**
```bash
--vector-store-id ID        # ID OpenAI Vector Store
--disable-deduplication     # Vypnout kontrolu duplicitů
--chunking-strategy STRATEGIE  # "auto" nebo "static"
--max-chunk-size ČÍSLO      # Maximální velikost chunku
--chunk-overlap ČÍSLO       # Překryv mezi chunky
```

---

## 🎛️ Režimy zpracování

### **1. Kombinovaný režim (výchozí):**
```bash
python scrape_sitemap_GPT.py --config web.json
```
**Co se zpracuje:**
- ✅ HTML sitemap (pokud je nakonfigurována)
- ✅ XML sitemap
- ✅ RSS feeds (pokud jsou nakonfigurovány)
- ✅ Všechny URL se zkombinují a deduplikují

### **2. RSS-only režim:**
```bash
python scrape_sitemap_GPT.py --config web.json --rss-only
```
**Co se zpracuje:**
- ✅ Pouze RSS feeds
- ❌ Přeskočí HTML a XML sitemap
- 💡 Ideální pro zpracování pouze aktualit/blogů

### **3. Sitemap-only režim:**
```bash
python scrape_sitemap_GPT.py --config web.json --sitemap-only
```
**Co se zpracuje:**
- ✅ HTML sitemap + XML sitemap
- ❌ Přeskočí RSS feeds
- 💡 Ideální pro zpracování statického obsahu

### **4. XML-only režim:**
```bash
python scrape_sitemap_GPT.py --config web.json --xml-only
```
**Co se zpracuje:**
- ✅ Pouze XML sitemap
- ❌ Přeskočí HTML sitemap i RSS feeds
- 💡 Nejrychlejší režim pro velké weby

---

## 🔄 Funkce obnovy (Resume)

### **Problém, který řeší:**
Když se skript přeruší po zpracování tisíců URL, běžně by při dalším spuštění začal znovu od začátku. Resume funkcionalita to řeší.

### **Jak funguje:**
1. **Skenuje existující soubory** ve výstupním adresáři
2. **Extrahuje URL z metadata** každého souboru
3. **Vytvoří cache** pro rychlé vyhledávání
4. **Přeskočí URL**, které už mají soubory
5. **Zpracuje pouze nové** nebo chybějící URL

### **Použití:**

#### **Testování resume funkce:**
```bash
python scrape_sitemap_GPT.py --config web.json --test-resume
```
**Výstup:**
```
🧪 Testing resume cache functionality...
📁 Output directory: Web_files

📊 RESUME CACHE TEST RESULTS:
✅ Cache would contain 4,847 URLs
📁 Scanned directory: C:\path\to\Web_files

📝 Sample URLs found:
   1. https://example.com/aktuality
      → aktuality.txt
   2. https://example.com/kontakty
      → kontakty.txt
   ... and 4,845 more URLs

✅ Resume functionality test completed!
```

#### **Spuštění s resume:**
```bash
python scrape_sitemap_GPT.py --config web.json --resume
```
**Co se stane:**
```
🔄 Building local files cache...
📁 Found 5,000 .txt files to scan...
✅ Local cache built: 4,847 URLs indexed in 15.3s
📊 Format compatibility: 4,847/5,000 files had extractable URLs

🔄 Resume mode: ENABLED - Skipped 4,847 already processed URLs

=== URL PROCESSING STATUS (RESUME) ===
URL: https://example.com/already-processed
Local file exists: YES
Processing: SKIPPED (RESUME)
=====================================

=== URL PROCESSING STATUS (RESUME) ===
URL: https://example.com/new-content
Local file exists: NO
Processing: WILL PROCEED
=====================================
```

### **Kompatibilita se starými soubory:**
Resume funkce rozpozná různé formáty souborů:

#### **✅ Aktuální formát (nejlepší podpora):**
```markdown
## 🔗 **ZDROJOVÁ URL:**
### **https://example.com/page**
```

#### **✅ Legacy formáty (dobrá podpora):**
```
ZDROJOVÁ URL: https://example.com/page
Source URL: https://example.com/page
URL: https://example.com/page
```

#### **✅ Obecný formát (základní podpora):**
- Jakákoli HTTP/HTTPS URL v prvních 3KB souboru

---

## 📦 Vector Store integrace

### **Co je Vector Store:**
OpenAI Vector Store je služba pro ukládání a vyhledávání textových dokumentů pomocí AI. Umožňuje vytvářet AI asistenty, kteří mohou odpovídat na otázky založené na vašem obsahu.

### **Základní použití:**
```bash
python scrape_sitemap_GPT.py --config web.json --vector-store-id vs_abc123
```

### **Deduplication (kontrola duplicitů):**

#### **Zapnuto (výchozí):**
```bash
python scrape_sitemap_GPT.py --vector-store-id vs_abc123
```
**Co se stane:**
- Před nahráním nového souboru zkontroluje, jestli už existuje soubor se stejnou URL
- Pokud existuje, smaže starý a nahraje nový
- **Výsledek:** Každá URL má v Vector Store pouze jednu nejnovější verzi

#### **Vypnuto:**
```bash
python scrape_sitemap_GPT.py --vector-store-id vs_abc123 --disable-deduplication
```
**Co se stane:**
- Nahraje všechny soubory bez kontroly duplicitů
- **Výsledek:** Vector Store může obsahovat více verzí stejné URL

### **Chunking strategie:**

#### **Auto chunking (doporučeno):**
```bash
python scrape_sitemap_GPT.py --vector-store-id vs_abc123 --chunking-strategy auto
```
- OpenAI automaticky rozdělí dokumenty na optimální části
- Velikost: ~800 tokenů na chunk, ~400 tokenů překryv

#### **Static chunking:**
```bash
python scrape_sitemap_GPT.py --vector-store-id vs_abc123 --chunking-strategy static --max-chunk-size 1200 --chunk-overlap 200
```
- Pevně definovaná velikost chunků
- Více kontroly nad dělením obsahu

### **Kombinace s resume:**
```bash
python scrape_sitemap_GPT.py --config web.json --resume --vector-store-id vs_abc123
```
- Přeskočí lokálně existující soubory
- Nahraje do Vector Store pouze nové soubory
- **Velmi efektivní** pro velké weby s tisíci stránek

---

## 💡 Příklady použití

### **1. První spuštění na novém webu:**
```bash
python scrape_sitemap_GPT.py --config nova_firma.json --debug
```
- Debug mód pro sledování průběhu
- Zpracuje všechny dostupné zdroje
- Vytvoří `NovaFirma_files/` adresář

### **2. Denní aktualizace obsahu:**
```bash
python scrape_sitemap_GPT.py --config firma.json
```
- Zpracuje pouze změněné stránky (díky `check_last_modified`)
- Rychlé a efektivní
- Automaticky přeskočí nezměněný obsah

### **3. Obnova po přerušení:**
```bash
# Nejprve otestujte resume
python scrape_sitemap_GPT.py --config firma.json --test-resume

# Pak spusťte resume
python scrape_sitemap_GPT.py --config firma.json --resume
```

### **4. Pouze RSS feeds zpracování:**
```bash
python scrape_sitemap_GPT.py --config news.json --rss-only --vector-store-id vs_news123
```
- Ideální pro zpracování pouze aktualit
- Přímé nahrání do Vector Store

### **5. Rychlé zpracování bez časových kontrol:**
```bash
python scrape_sitemap_GPT.py --config web.json --no-check-modified --xml-only
```
- Zpracuje všechny URL bez ohledu na datum změny
- Pouze XML sitemap pro rychlost

### **6. Vlastní CSS selektory pro čištění:**
```bash
python scrape_sitemap_GPT.py --config web.json --jina-remove-selectors ".reklama,.popup,.cookie-notice"
```
- Odstraní nežádoucí elementy ze stránek
- Čistší výsledný obsah

### **7. Multi-projekt zpracování:**
```bash
# Projekt A
python scrape_sitemap_GPT.py --config projekt_a.json --vector-store-id vs_a123

# Projekt B  
python scrape_sitemap_GPT.py --config projekt_b.json --vector-store-id vs_b456
```
- Nezávislé zpracování více webů
- Každý má své soubory a logy

---

## 🔧 Řešení problémů

### **❌ "No module named 'requests'"**
**Řešení:**
```bash
pip install requests beautifulsoup4 python-dateutil
```

### **❌ "Config file not found"**
**Řešení:**
```bash
# Zkontrolujte cestu k souboru
ls -la *.json

# Nebo vytvořte nový config
cp config_example.json muj_config.json
```

### **❌ "API key invalid"**
**Řešení:**
1. Zkontrolujte API klíče v konfiguraci
2. Ověřte platnost klíčů na příslušných platformách
3. Zkontrolujte kvóty a limity

### **❌ "Skript se zasekl při 'Building local files cache...'"**
**Příčina:** Příliš mnoho souborů ve výstupním adresáři

**Řešení:**
```bash
# Zkontrolujte počet souborů
ls Web_files/*.txt | wc -l

# Pokud je jich více než 10,000, použijte:
python scrape_sitemap_GPT.py --config web.json --no-check-modified
```

### **❌ "Vector Store upload failed"**
**Možné příčiny:**
1. Neplatný Vector Store ID
2. Nedostatečné OpenAI kredity
3. Příliš velký soubor

**Řešení:**
```bash
# Zkuste bez Vector Store
python scrape_sitemap_GPT.py --config web.json

# Nebo zkontrolujte Vector Store ID
python scrape_sitemap_GPT.py --config web.json --debug --vector-store-id vs_test123
```

### **❌ "No URLs found to process"**
**Možné příčiny:**
1. Všechny URL jsou blacklistované
2. Všechny URL už byly zpracovány
3. Chybná sitemap URL

**Řešení:**
```bash
# Zkuste bez časových kontrol
python scrape_sitemap_GPT.py --config web.json --no-check-modified

# Nebo zkontrolujte blacklist v konfiguraci
```

---

## 🚀 Generalizované zpracování HTML sitemap

## 🎯 **Problém starého přístupu:**
Původní algoritmus používal hardkódované CSS selektory specifické pro určité typy webů:
```python
selectors_to_try = [
    ".portlet-site-map ul",           # Specifické pro Liferay
    ".gov-container .portlet-site-map ul",  # Specifické pro gov weby
    ".portlet-body ul",
    ".sitemap ul",
    "ul"                             # Obecný fallback
]
```

**Problémy:**
- ❌ Funguje pouze pro specifické HTML struktury
- ❌ Vyžaduje ruční přidávání selektorů pro nové weby
- ❌ Nezvládá dynamické nebo nestandardní struktury
- ❌ Složité ladění pro různé weby

## 🚀 **Nové řešení: Jina AI Links Summary**

### **Jak funguje:**
1. **Jina AI API volání** s `X-With-Links-Summary: all`
2. **Automatická extrakce všech linků** ze stránky
3. **Strukturovaná JSON odpověď** s metadaty
4. **Generalizovaný algoritmus** nezávislý na HTML struktuře

### **API volání:**
```bash
curl "https://eu-r-beta.jina.ai/[URL]" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer [API_KEY]" \
  -H "X-Engine: browser" \
  -H "X-Remove-Selector: [SELECTORS]" \
  -H "X-Return-Format: html" \
  -H "X-With-Links-Summary: all"
```

### **JSON struktura odpovědi:**
```json
{
  "code": 200,
  "status": "success", 
  "data": {
    "title": "Mapa webu",
    "url": "https://example.com/sitemap",
    "html": "<html>...</html>",
    "links": [
      {
        "url": "https://example.com/o-nas",
        "text": "O nás", 
        "title": "Informace o společnosti"
      },
      {
        "url": "https://example.com/sluzby",
        "text": "Služby",
        "title": "Přehled služeb"
      }
    ]
  }
}
```

## ⚙️ **Použití:**

### **Generalizovaný přístup (výchozí):**
```bash
python scrape_sitemap_GPT.py --config web.json
```
- ✅ Automaticky funguje s jakoukoliv HTML strukturou
- ✅ Žádné hardkódované selektory
- ✅ Rychlejší a spolehlivější

### **Legacy přístup (pro kompatibilitu):**
```bash
python scrape_sitemap_GPT.py --config web.json --legacy-html-parsing
```
- ⚠️  Používá staré CSS selektory
- ⚠️  Funguje pouze pro specifické struktury
- 💡 Zachováno pro zpětnou kompatibilitu

## 🔄 **Automatické fallback:**
Skript má inteligentní fallback systém:
1. **Primární:** Generalizovaný přístup (Jina AI links)
2. **Fallback 1:** Legacy parsing (CSS selektory)
3. **Fallback 2:** XML-only zpracování

## 🎯 **Výhody nového přístupu:**

| Funkce | Legacy přístup | Generalizovaný přístup |
|--------|----------------|----------------------|
| **Univerzálnost** | ❌ Pouze specifické weby | ✅ Jakákoliv HTML struktura |
| **Údržba** | ❌ Ruční přidávání selektorů | ✅ Žádná údržba potřeba |
| **Rychlost** | ⚠️  Pomalé parsing | ✅ Rychlá extrakce |
| **Spolehlivost** | ❌ Často selhává | ✅ Vysoká úspěšnost |
| **Debugging** | ❌ Složité ladění | ✅ Jasná JSON struktura |

---

## ❓ Časté dotazy

### **Q: Mohu zpracovávat více webů současně?**
A: Ano, každý config soubor vytvoří nezávislé adresáře. Můžete spustit více instancí skriptu s různými konfiguracemi.

### **Q: Jak dlouho trvá zpracování velkého webu?**
A: Závisí na počtu stránek a rychlosti API:
- **1,000 stránek**: ~30-60 minut
- **10,000 stránek**: ~5-10 hodin
- **100,000 stránek**: ~2-3 dny

### **Q: Spotřebovává skript hodně API kreditů?**
A: Ano, každá stránka = 1 API call. Pro 10,000 stránek potřebujete ~10,000 API volání.

### **Q: Můžu přerušit běh skriptu?**
A: Ano, stiskněte Ctrl+C. Při dalším spuštění použijte `--resume` pro pokračování.

### **Q: Jak často mám spouštět aktualizace?**
A: Závisí na častosti změn webu:
- **Denně**: Pro news/blog weby
- **Týdně**: Pro běžné firemní weby  
- **Měsíčně**: Pro statické weby

### **Q: Podporuje skript jiné jazyky než češtinu?**
A: Ano, skript funguje s jakýmkoli jazykem. Metadata jsou v češtině, ale obsah zůstává v původním jazyce.

### **Q: Můžu upravit metadata formát?**
A: Ano, upravte funkci `create_metadata_header()` ve skriptu podle vašich potřeb.

### **Q: Co když má web ochranu proti botům?**
A: Skript používá retry mechanismus a respektuje rate limity. Pro problematické weby zvyšte `request_timeout` a `backoff_factor` v konfiguraci.

---

## 📞 Podpora

Pokud narazíte na problémy:

1. **Zkontrolujte logy** v `Web_logs/` adresáři
2. **Spusťte s --debug** pro detailní výstup
3. **Otestujte resume** pomocí `--test-resume`
4. **Zkontrolujte API klíče** a kvóty

---

## 📄 Licence

Tento skript je poskytován "jak je" bez jakýchkoli záruk. Používejte na vlastní odpovědnost a respektujte robots.txt a terms of service cílových webů.

---

*Vytvořeno pro efektivní zpracování webového obsahu pomocí AI služeb. 🚀* 