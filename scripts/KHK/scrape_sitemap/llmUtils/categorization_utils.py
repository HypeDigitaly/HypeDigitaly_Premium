import logging
from .llm_utils import call_llm_api

# Get logger for this module
logger = logging.getLogger(__name__)

def categorize_link(path, categories, llm_providers, llm_sequence, max_retries=3, initial_retry_delay=5, api_call_delay=10):
    """Categorizes a link path using the configured LLM provider."""
    path_string = ' > '.join(path)
    
    prompt = f"""Dána je následující cesta menu z webových stránek Královéhradeckého kraje:

{path_string}

Zařaďte prosím tuto cestu do JEDNÉ z následujících kategorií:

{', '.join(categories)}

DŮLEŽITÉ INSTRUKCE:
1. MUSÍTE odpovědět POUZE názvem JEDNÉ JEDINÉ kategorie ze seznamu výše - odpověď "Nezařazeno" NENÍ povolena.
2. I když si nejste jisti, vyberte kategorii, která se nejvíce blíží obsahu nebo tématu cesty.
3. Neodpovídejte žádným jiným textem, pouze názvem kategorie ze seznamu.
4. V případě nejistoty použijte následující prioritizaci:
   a) Nejdříve hledejte přímou tematickou shodu
   b) Pokud není nalezena, hledejte související témata
   c) Pokud stále není jasné, použijte nejobecnější související kategorii
5. Použijte následující vodítka pro kategorizaci:
   - Kontakt:
     * lidé, osoby, krajský/městský úřad, organizační struktura
     * kontaktní informace, komise, výbory, zastupitelstvo, radní, zaměstnanci
     * všechna sportoviště, aquacentra, sportovní arény, sportovní zařízení
     * všechny portály (mapové, eGovernment, informační, atd.)
     * platformy a systémy pro komunikaci s úřadem
     * otvírací hodiny (úřad, podatelna, czechpoint, dílčí odbory/oddělení)
   - Administrativa_Uredni_Zalezitosti:
     * úřední dokumenty, postupy, vyhlášky
     * veškeré vyřizování a zařizování úředních záležitostí
     * formuláře a žádosti
     * hlášení závad a problémů
     * životní situace
     * usnesení zastupitelstva, rady, komise, výboru kraje/města/obce
     * konání a jednání zastupitelstva, rady, komise, výboru kraje/města/obce
     * veškeré záležitosti související s usneseními a jednáními orgánů kraje/města/obce

Vezměte v úvahu celou absolutní cestu v daném stromě k URL odkazu pro co nejpřesnější zařazení/zvolení dané kategorie ze vstupního seznamu. MUSÍTE vybrat jednu kategorii, i když si nejste zcela jisti - vyberte tu nejvíce odpovídající.
"""

    messages = [{"role": "user", "content": prompt}]
    
    category = call_llm_api(
        messages=messages, 
        max_tokens=50, 
        temperature=0,
        max_retries=max_retries,
        initial_retry_delay=initial_retry_delay,
        api_call_delay=api_call_delay,
        llm_providers=llm_providers,
        llm_sequence=llm_sequence
    )

    if category is None:
        logger.error(f"Nepodařilo se získat kategorii pro cestu: {path_string} po všech pokusech.")
        return "Nezařazeno"
        
    category = category.strip()
    
    if category not in categories:
        # Basic check if the response contains one of the categories maybe with extra text
        found_category = None
        for valid_cat in categories:
            if valid_cat in category:
                found_category = valid_cat
                logger.warning(f"LLM vrátil kategorii s extra textem: '{category}'. Extrahovaná platná kategorie: '{found_category}'.")
                break
        if found_category:
            category = found_category
        else:
            logger.warning(f"LLM vrátil neočekávanou nebo neplatnou kategorii: '{category}'. Použije se 'Nezařazeno'.")
            return "Nezařazeno"
            
    return category

def generate_rag_question(path, llm_providers, llm_sequence, max_retries=3, initial_retry_delay=5, api_call_delay=10):
    """Generates RAG questions using the configured LLM provider."""
    path_string = ' > '.join(path)
    
    prompt = f"""Based on the following absolute path from a website sitemap:

{path_string}

Create an open-ended, RAG-optimized question in both Czech and English that would help users find this specific page. The question should:
1. Be natural and conversational
2. Include key details from the path
3. Be suitable for semantic search
4. Help users find the exact page they're looking for
5. Include both Czech and English versions separated by " | "

Example format:
For path: "Dotace > Kotlíková dotace > Aktuality"
Output: "Kde najdu aktuality, články a důležité informace ke kotlíkovým dotacím? | Where do I find the latest articles, updates and important information regarding boiler subsidies?"

Return ONLY the question pair without any additional text or formatting."""

    messages = [{"role": "user", "content": prompt}]
    
    question = call_llm_api(
        messages=messages, 
        max_tokens=200, 
        temperature=0,
        max_retries=max_retries,
        initial_retry_delay=initial_retry_delay,
        api_call_delay=api_call_delay,
        llm_providers=llm_providers,
        llm_sequence=llm_sequence
    )
    
    if question is None:
        logger.error(f"Nepodařilo se vygenerovat RAG otázku pro cestu: {path_string} po všech pokusech.")
        # Provide a generic fallback to avoid breaking downstream processes
        fallback_title = path[-1] if path else "the page"
        return f"Kde najdu informace o {fallback_title}? | Where can I find information about {fallback_title}?"

    return question.strip() 