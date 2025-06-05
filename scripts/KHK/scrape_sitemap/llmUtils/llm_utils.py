import requests
import anthropic
import time
import random
import logging
import json
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Get logger for this module
logger = logging.getLogger(__name__)

def requests_retry_session(
    retries=3,
    backoff_factor=0.3,
    status_forcelist=(500, 502, 503, 504, 524),
    session=None,
):
    """Create a requests session with retry configuration."""
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def convert_anthropic_tool_to_openai(anthropic_tool):
    """
    Convert Anthropic tool format to OpenAI-compatible format for Groq.
    
    Args:
        anthropic_tool (dict): Tool in Anthropic format
        
    Returns:
        dict: Tool in OpenAI format
    """
    openai_tool = {
        "type": "function",
        "function": {
            "name": anthropic_tool["name"],
            "description": anthropic_tool["description"],
            "parameters": anthropic_tool["input_schema"]
        }
    }
    return openai_tool

def convert_anthropic_tool_choice_to_openai(anthropic_tool_choice):
    """
    Convert Anthropic tool_choice format to OpenAI-compatible format for Groq.
    
    Args:
        anthropic_tool_choice (dict): Tool choice in Anthropic format
        
    Returns:
        str or dict: Tool choice in OpenAI format
    """
    if not anthropic_tool_choice:
        return "auto"
    
    if isinstance(anthropic_tool_choice, dict):
        if anthropic_tool_choice.get("type") == "tool":
            # Force specific tool usage
            return {
                "type": "function",
                "function": {"name": anthropic_tool_choice.get("name")}
            }
        elif anthropic_tool_choice.get("type") == "auto":
            return "auto"
    
    return "auto"

def handle_groq_tool_response(response_json, messages, tools, model, api_key, api_url, request_timeout):
    """
    Handle Groq tool calling response and return the structured data.
    
    Args:
        response_json (dict): Initial Groq API response
        messages (list): Conversation messages
        tools (list): Available tools in OpenAI format
        model (str): Model name
        api_key (str): API key
        api_url (str): API URL
        request_timeout (int): Request timeout
        
    Returns:
        dict or str: The tool result data or final response content
    """
    choice = response_json["choices"][0]
    message = choice["message"]
    
    if choice.get("finish_reason") == "tool_calls" and message.get("tool_calls"):
        logger.info(f"Groq model used {len(message['tool_calls'])} tools")
        
        # For structured data extraction tools like extract_qa_pairs, extract_contact_details, etc.
        # the tool arguments already contain the structured data we need
        tool_calls = message["tool_calls"]
        
        if len(tool_calls) == 1:
            tool_call = tool_calls[0]
            tool_name = tool_call["function"]["name"]
            tool_args = json.loads(tool_call["function"]["arguments"])
            
            logger.info(f"Groq tool call: {tool_name} with structured data")
            logger.debug(f"Tool arguments: {tool_args}")
            
            # For data extraction tools, return the arguments directly as they contain the structured data
            extraction_tools = ["extract_qa_pairs", "extract_contact_details", "extract_resolution_data", "report_extracted_date_year", "extract_pre_table_contextual_information"]
            
            if tool_name in extraction_tools:
                logger.info(f"Returning structured data directly from tool: {tool_name}")
                return tool_args
            else:
                # For other types of tools that might need actual execution, we could add logic here
                logger.warning(f"Unknown tool type for direct data extraction: {tool_name}")
                return tool_args
        
        # Handle multiple tool calls if needed
        elif len(tool_calls) > 1:
            logger.info(f"Multiple tool calls detected: {len(tool_calls)}")
            # For now, return the first tool's result
            tool_call = tool_calls[0]
            tool_args = json.loads(tool_call["function"]["arguments"])
            return tool_args
            
    return message.get("content", "").strip()

def call_llm_api(messages, system_prompt=None, max_tokens=1024, temperature=0.7, max_retries=3, initial_retry_delay=5, api_call_delay=10, tools=None, tool_choice=None, llm_providers=None, llm_sequence=None, request_timeout=150):
    """
    Calls LLM provider APIs based on the LLM_SEQUENCE, with fallback.
    Now supports tool calling for both Anthropic and Groq providers.

    Args:
        messages (list): List of message dictionaries (e.g., [{'role': 'user', 'content': '...'}]).
        system_prompt (str, optional): System prompt text.
        max_tokens (int): Maximum tokens to generate.
        temperature (float): Sampling temperature.
        max_retries (int): Maximum retry attempts for EACH provider.
        initial_retry_delay (int): Initial delay before retrying for EACH provider.
        api_call_delay (int): Fixed delay after a successful API call.
        tools (list, optional): A list of tools the model can use.
        tool_choice (dict, optional): Controls how the model uses tools.
        llm_providers (dict): Dictionary of LLM provider configurations.
        llm_sequence (str): Comma-separated sequence of provider IDs.
        request_timeout (int): Request timeout in seconds.

    Returns:
        str or dict: The content of the LLM's response or tool result dictionary. Returns None if all providers fail.
    """
    if not llm_providers or not llm_sequence:
        logger.error("LLM providers configuration or sequence not provided.")
        return None
        
    sequence_ids = [id.strip() for id in llm_sequence.split(',') if id.strip()]
    if not sequence_ids:
        logger.error("LLM_SEQUENCE je prázdná nebo neplatná.")
        return None

    original_messages = messages[:] # Copy original messages for Groq prepending

    for provider_id in sequence_ids:
        provider_config = llm_providers.get(provider_id)
        if not provider_config:
            logger.warning(f"Provider ID '{provider_id}' ze sekvence nebyl nalezen v LLM_PROVIDERS. Přeskakuji.")
            continue

        provider_name = provider_config["name"]
        api_key = provider_config["api_key"]
        model = provider_config["model"]
        api_url = provider_config.get("api_url")
        current_messages = original_messages[:]
        current_system_prompt = system_prompt

        logger.info(f"Pokus o volání LLM API s providerem ID: {provider_id} (Název: {provider_name}, Model: {model})")
        
        if api_url:
            logger.info(f"Cílová URL: {api_url}")
        else:
            logger.warning(f"API URL není definována pro providera ID: {provider_id}")
            if provider_name == "groq":
                 logger.error(f"Groq (ID: {provider_id}) vyžaduje 'api_url' v konfiguraci. Přeskakuji.")
                 continue

        # Prepare provider-specific parameters
        client = None
        current_tools = tools
        current_tool_choice = tool_choice
        
        if provider_name == "anthropic":
            if not api_key:
                logger.error(f"Chybí CLAUDE_API_KEY pro providera {provider_id}. Přeskakuji.")
                continue
            client = anthropic.Anthropic(api_key=api_key)
            # Tools are already in Anthropic format
            
        elif provider_name == "groq":
            if not api_key:
                logger.error(f"Chybí GROQ_API_KEY pro providera {provider_id}. Přeskakuji.")
                continue
            # Groq uses OpenAI's message format, prepend system prompt if provided
            if current_system_prompt:
                # Truncate system prompt for Groq compatibility
                truncated_system_prompt = truncate_system_prompt_for_groq(current_system_prompt)
                if len(truncated_system_prompt) < len(current_system_prompt):
                    logger.warning(f"Groq system prompt truncated from {len(current_system_prompt)} to {len(truncated_system_prompt)} characters")
                current_messages = [{"role": "system", "content": truncated_system_prompt}] + current_messages
                current_system_prompt = None
            
            # Validate and sanitize messages for Groq
            current_messages = validate_groq_messages(current_messages)
            if not current_messages:
                logger.error(f"No valid messages after validation for Groq (ID: {provider_id}). Skipping provider.")
                continue
            
            # Convert tools to OpenAI format if provided
            if tools:
                current_tools = []
                for tool in tools:
                    current_tools.append(convert_anthropic_tool_to_openai(tool))
                logger.info(f"Converted {len(tools)} tools from Anthropic to OpenAI format for Groq")
            
            # Convert tool_choice to OpenAI format if provided
            if tool_choice:
                current_tool_choice = convert_anthropic_tool_choice_to_openai(tool_choice)
                logger.info(f"Converted tool_choice from Anthropic to OpenAI format: {current_tool_choice}")
        else:
            logger.error(f"Neznámý typ providera '{provider_name}' pro ID {provider_id}. Přeskakuji.")
            continue

        # Inner retry loop for the current provider
        for attempt in range(max_retries):
            try:
                if provider_name == "anthropic":
                    api_params = {
                        "model": model,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "messages": current_messages
                    }
                    if current_system_prompt:
                        api_params["system"] = current_system_prompt
                    if current_tools:
                        api_params["tools"] = current_tools
                    if current_tool_choice:
                        api_params["tool_choice"] = current_tool_choice

                    message = client.messages.create(**api_params)
                    logger.debug(f"Anthropic API Response object (ID: {provider_id}): {message}")

                    # Check for tool use in the response
                    response_text = None
                    tool_used = False
                    if message.content and isinstance(message.content, list):
                        for content_block in message.content:
                             if content_block.type == "tool_use":
                                 logger.info(f"Anthropic model (ID: {provider_id}) used tool: {content_block.name}")
                                 # For Anthropic, the input to the tool is the structured data we need
                                 if isinstance(content_block.input, dict):
                                     response_text = content_block.input
                                 else: # Should be a dict, but log if not
                                     logger.warning(f"Anthropic tool input was not a dict: {type(content_block.input)}. Raw input: {content_block.input}")
                                     # Attempt to parse if it looks like a JSON string, otherwise keep as is or handle error
                                     try:
                                         response_text = json.loads(str(content_block.input))
                                     except json.JSONDecodeError:
                                         logger.error(f"Could not parse Anthropic tool input string to JSON: {str(content_block.input)[:200]}")
                                         response_text = str(content_block.input) # Fallback to string
                                 tool_used = True
                                 break
                    
                    if not tool_used:
                        if message.content and isinstance(message.content, list) and message.content[0].type == "text":
                             response_text = message.content[0].text.strip()
                        else:
                             logger.error(f"Anthropic API (ID: {provider_id}) response format unexpected or text content missing.")
                             raise ValueError("Unexpected Anthropic response format or missing text content")

                elif provider_name == "groq":
                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    }
                    payload = {
                        "model": model,
                        "messages": current_messages,
                        # "max_completion_tokens": max_tokens, # Groq uses max_tokens for output, not completion specific
                        "max_tokens": max_tokens, # Use max_tokens for Groq, it implies completion tokens
                        "temperature": temperature,
                    }
                    
                    # Add tools and tool_choice for Groq if provided
                    if current_tools:
                        payload["tools"] = current_tools
                        logger.info(f"Added {len(current_tools)} tools to Groq request")
                    if current_tool_choice:
                        payload["tool_choice"] = current_tool_choice
                        logger.info(f"Added tool_choice to Groq request: {current_tool_choice}")

                    if not api_url:
                         logger.error(f"Chybí api_url pro Groq (ID: {provider_id}) v konfiguraci.")
                         break
                         
                    response = requests_retry_session().post(api_url, headers=headers, json=payload, timeout=request_timeout)
                    response.raise_for_status()
                    
                    response_json = response.json()
                    logger.debug(f"Groq API Response JSON (ID: {provider_id}): {response_json}")

                    if response_json.get("choices") and len(response_json["choices"]) > 0:
                        # Handle tool calling response if present
                        if current_tools and response_json["choices"][0].get("finish_reason") == "tool_calls":
                            response_text = handle_groq_tool_response(
                                response_json, current_messages, current_tools, model, api_key, api_url, request_timeout
                            )
                            
                            # Response from handle_groq_tool_response is already structured if it's a dict
                            # No further parsing needed here unless it returned a string that needs parsing.
                            if isinstance(response_text, str):
                                try:
                                    parsed_response = json.loads(response_text)
                                    if isinstance(parsed_response, dict):
                                        response_text = parsed_response
                                except json.JSONDecodeError:
                                    # Keep as string if not valid JSON, or if it was meant to be a final text response
                                    logger.debug("Groq tool response was string, but not valid JSON. Kept as string.")
                                    pass 
                        else:
                            response_text = response_json["choices"][0]["message"]["content"].strip()
                    else:
                        logger.error(f"Groq API (ID: {provider_id}) nevrátil platné 'choices': {response_json}")
                        raise ValueError(f"Groq API (ID: {provider_id}) did not return valid choices")
                
                # Success! Apply delay and return.
                time.sleep(api_call_delay)
                logger.info(f"LLM API volání úspěšné (Provider ID: {provider_id}, Název: {provider_name})")
                return response_text

            except anthropic.APIError as e: 
                logger.warning(f"Chyba Anthropic API (ID: {provider_id}, pokus {attempt + 1}/{max_retries}): {type(e).__name__} - {str(e)}")
                if "tool_choice" in str(e) or "tool_name" in str(e):
                     logger.error(f"Chyba související s tool_choice/tool_name u Anthropic (ID: {provider_id}): {str(e)}. Zkontrolujte definici nástroje a tool_choice.")
                     break # Don't retry for definitive tool errors

                if attempt == max_retries - 1:
                    logger.error(f"Nepodařilo se zavolat Anthropic API (ID: {provider_id}) po {max_retries} pokusech. Přecházím na dalšího providera (pokud existuje).")
                    break
                delay = initial_retry_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Čekání {delay:.2f} sekund před dalším pokusem s providerem {provider_id}.")
                time.sleep(delay)
                
            except requests.exceptions.RequestException as e:
                status_code = e.response.status_code if e.response is not None else "N/A"
                
                error_details = "N/A"
                if e.response is not None:
                    try:
                        error_response = e.response.json()
                        error_details = error_response.get('error', {}).get('message', error_response)
                    except:
                        error_details = e.response.text[:500] if e.response.text else "No response text"
                
                logger.warning(f"Chyba Groq API (ID: {provider_id}, HTTP {status_code}, pokus {attempt + 1}/{max_retries}): {str(e)}")
                logger.warning(f"Groq API Error Details: {error_details}")
                
                if status_code == 400:
                    logger.error(f"Groq API 400 Error - Request payload preview: {json.dumps(payload, ensure_ascii=False)[:1000]}...")
                
                should_retry = False
                if e.response is not None and (e.response.status_code == 429 or e.response.status_code >= 500):
                    should_retry = True
                elif e.response is not None:
                     logger.error(f"Groq API (ID: {provider_id}) vrátilo neočekávaný HTTP status {status_code}, neprovádí se retry pro tohoto providera.")
                     break
                else:
                    logger.error(f"Chyba sítě nebo jiná chyba při volání Groq API (ID: {provider_id}): {str(e)}")
                    should_retry = True
                         
                if should_retry and attempt < max_retries - 1:
                    delay = initial_retry_delay * (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"Čekání {delay:.2f} sekund před dalším pokusem s providerem {provider_id}.")
                    time.sleep(delay)
                else:
                    logger.error(f"Nepodařilo se zavolat Groq API (ID: {provider_id}) po {max_retries} pokusech nebo došlo k neopravitelné chybě. Přecházím na dalšího providera (pokud existuje).")
                    break
                    
            except Exception as e:
                logger.error(f"Neočekávaná chyba při volání LLM API (ID: {provider_id}, Název: {provider_name}, pokus {attempt + 1}/{max_retries}): {type(e).__name__} - {str(e)}")
                if attempt == max_retries - 1:
                    logger.error(f"Nepodařilo se provést volání LLM API (ID: {provider_id}) po {max_retries} pokusech kvůli neočekávané chybě. Přecházím na dalšího providera (pokud existuje).")
                    break
                delay = initial_retry_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Čekání {delay:.2f} sekund před dalším pokusem s providerem {provider_id}.")
                time.sleep(delay)

    # If all providers failed
    logger.error(f"Všichni LLM provideři v sekvenci ({llm_sequence}) selhali.")
    return None 

def sanitize_message_content(content):
    """
    Sanitize message content for Groq API compatibility.
    
    Args:
        content (str): The message content to sanitize
        
    Returns:
        str: Sanitized content
    """
    import re
    
    if not content or not isinstance(content, str):
        return ""
    
    # Remove or replace problematic characters
    # Replace various quote types with standard quotes
    content = content.replace('\u201c', '"').replace('\u201d', '"')  # Left and right double quotation marks
    content = content.replace('\u2018', "'").replace('\u2019', "'")  # Left and right single quotation marks
    content = content.replace('\u201e', '"').replace('\u201c', '"')  # Double low-9 and left double quotation marks
    
    # Replace em-dashes and en-dashes with regular hyphens
    content = content.replace('\u2014', '-').replace('\u2013', '-')  # Em dash and en dash
    
    # Remove or replace other potentially problematic Unicode characters
    content = content.replace('\u00a0', ' ')   # Non-breaking space
    content = content.replace('\u2028', '\n')  # Line separator
    content = content.replace('\u2029', '\n\n')  # Paragraph separator
    
    # Clean up excessive whitespace
    content = re.sub(r'\s+', ' ', content)
    content = content.strip()
    
    return content

def truncate_system_prompt_for_groq(system_prompt, max_chars=8000):
    """
    Truncate system prompt if it's too long for Groq API.
    
    Args:
        system_prompt (str): The system prompt to truncate
        max_chars (int): Maximum characters allowed
        
    Returns:
        str: Truncated system prompt
    """
    if not system_prompt or len(system_prompt) <= max_chars:
        return system_prompt
    
    # Truncate and add notice
    truncated = system_prompt[:max_chars-200]  # Leave room for notice
    truncated += "\n\n[NOTE: Prompt truncated for API compatibility. Focus on key requirements above.]"
    
    return truncated

def validate_groq_messages(messages):
    """
    Validate and sanitize messages for Groq API.
    
    Args:
        messages (list): List of message dictionaries
        
    Returns:
        list: Validated and sanitized messages
    """
    if not messages or not isinstance(messages, list):
        return []
    
    validated_messages = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
            
        # Ensure required fields are present
        if "role" not in msg or "content" not in msg:
            continue
            
        # Validate role
        valid_roles = ["system", "user", "assistant", "tool"]
        if msg["role"] not in valid_roles:
            continue
            
        # Sanitize content
        sanitized_content = sanitize_message_content(msg["content"])
        if not sanitized_content:
            # If original content was just whitespace and sanitize_message_content returns "",
            # Groq might error. It's better to skip such messages if they are not 'tool' role.
            # Tool calls might have empty content if the tool has no output, which is valid.
            if msg["role"] != "tool":
                logger.warning(f"Skipping message with empty content after sanitization (Role: {msg['role']}). Original: '{msg["content"][:50]}...'")
                continue
            else: # For tool role, empty string content is acceptable by OpenAI spec
                validated_msg_content = "" # Allow empty string for tool content

        validated_msg = {
            "role": msg["role"],
            "content": sanitized_content
        }
        
        # Copy other valid fields if present (OpenAI/Groq specific)
        for field in ["name", "tool_call_id", "tool_calls"]: # Added tool_calls as it's part of assistant message with tool use
            if field in msg:
                validated_msg[field] = msg[field]
                
        validated_messages.append(validated_msg)
    
    # Ensure messages alternate between user and assistant, or start with system then user.
    # This is a common requirement for many models including Groq's OpenAI compatible API.
    if not validated_messages:
        return []

    final_validated_messages = []
    last_role = None
    
    # Handle initial system message if present
    if validated_messages[0]["role"] == "system":
        final_validated_messages.append(validated_messages[0])
        if len(validated_messages) > 1:
            last_role = validated_messages[0]["role"] # System
            # Start iterating from the message after system message
            messages_to_check = validated_messages[1:]
        else:
            messages_to_check = [] # Only system message, valid by itself
    else:
        messages_to_check = validated_messages

    for i, msg in enumerate(messages_to_check):
        current_role = msg["role"]
        
        # First message (after optional system) must be user or tool (if responding to tool_calls)
        if not last_role:
            if current_role not in ["user", "tool"]:
                logger.error(f"Invalid start role after system: '{current_role}'. Expected 'user' or 'tool'. Skipping message and subsequent ones.")
                # Return only up to the system message if that was valid
                return final_validated_messages if final_validated_messages and final_validated_messages[0]["role"] == "system" else []
            final_validated_messages.append(msg)
            last_role = current_role
            continue

        # Role alternation checks
        if current_role == last_role:
            # Allow consecutive tool messages (tool_calls by assistant, then tool results by tool role)
            if current_role == "tool" and last_role == "tool":
                final_validated_messages.append(msg)
                last_role = current_role # Stays tool
                continue
            #Groq/OpenAI requires assistant message with tool_calls before tool message with tool_call_id
            if current_role == "assistant" and msg.get("tool_calls") and last_role == "user":
                final_validated_messages.append(msg)
                last_role = current_role # assistant with tool_calls
                continue
                
            logger.warning(f"Consecutive messages with role '{current_role}' detected. Message: {msg}. Previous role: {last_role}. Attempting to merge or fix if possible.")
            # Simple fix: if it's user/assistant, try to merge content. This is a basic heuristic.
            if current_role in ["user", "assistant"] and final_validated_messages and final_validated_messages[-1]["role"] == current_role:
                 logger.info(f"Merging content for consecutive '{current_role}' messages.")
                 final_validated_messages[-1]["content"] += "\n" + msg["content"]
                 # Do not update last_role, it remains the same.
                 continue # Skip adding this message as it's merged
            else:
                logger.error(f"Cannot simply merge consecutive '{current_role}' messages or invalid sequence. Skipping message: {msg}")
                continue # Skip this problematic message

        if current_role == "system":
            logger.error("System message found in a position other than the first. Skipping system message.")
            continue
            
        # Standard alternation: user -> assistant, assistant -> user/tool
        final_validated_messages.append(msg)
        last_role = current_role

    if not final_validated_messages:
        logger.error("Message validation resulted in an empty list of messages for Groq.")
        return []
        
    # Final check: last message should not be assistant if tools were called and no tool response is provided yet.
    # However, call_llm_api handles the conversation flow, this function just validates the current list.

    return final_validated_messages 

def assess_content_type(content, page_title, llm_providers, llm_sequence, max_retries=3, initial_retry_delay=5, api_call_delay=10):
    """
    Uses LLM to assess whether content contains contact information or resolution information.
    
    Args:
        content (str): The text content to analyze
        page_title (str): Title of the page being processed (for context)
        llm_providers (dict): Dictionary of LLM provider configurations
        llm_sequence (str): Comma-separated sequence of LLM provider IDs
        max_retries (int): Maximum number of retry attempts
        initial_retry_delay (int): Initial delay for retries
        api_call_delay (int): Delay between API calls
        
    Returns:
        tuple: (contains_contacts: bool, contains_resolutions: bool)
    """
    
    # Define the tool schema for content assessment
    assessment_tool = [
        {
            "name": "assess_content_type",
            "description": "Analyzes text content to determine if it contains contact information or resolution information.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "contains_contacts": {
                        "type": "boolean",
                        "description": "True if the content contains identifiable human contact details such as names with emails, phone numbers, job titles, roles, or departments. False if it's just general text without specific contact information."
                    },
                    "contains_resolutions": {
                        "type": "boolean",
                        "description": "True ONLY if the content contains actual resolution documents, voting records, meeting minutes, formal decisions (usnesení), agenda items with voting results, or official council/committee decisions. False for contact lists, staff directories, organizational charts, or general information about council members."
                    }
                },
                "required": ["contains_contacts", "contains_resolutions"]
            }
        }
    ]
    
    tool_choice = {"type": "tool", "name": "assess_content_type"}
    
    system_prompt = f"""You are a content classification specialist. Your task is to analyze the provided text content and determine whether it contains:

1. **CONTACT INFORMATION**: Identifiable human contact details including:
   - Names with email addresses
   - Names with phone numbers  
   - Job titles and roles
   - Department assignments
   - Office locations
   - Staff directories or contact lists

2. **RESOLUTION INFORMATION**: Official governmental/organizational documents including:
   - Formal resolutions (usnesení)
   - Voting records and results
   - Meeting minutes with decisions
   - Agenda items with voting outcomes
   - Official council/committee decisions
   - Document attachments related to resolutions

**IMPORTANT DISTINCTIONS:**
- Contact information about council members (names, emails, phones, roles) = CONTACTS, NOT resolutions
- Lists of people in governmental positions = CONTACTS, NOT resolutions  
- Organizational charts or staff directories = CONTACTS, NOT resolutions
- Only actual decision documents, voting records, or meeting minutes = RESOLUTIONS

**CONTEXT:** You are analyzing content from page titled: "{page_title}"

Use the `assess_content_type` tool to provide your assessment."""

    user_prompt = f"""Please analyze the following content and determine if it contains contact information and/or resolution information:

**Content to analyze:**
```
{content}
```

**Instructions:**
- Set `contains_contacts` to true if the content has identifiable people with contact details (emails, phones, roles, departments)
- Set `contains_resolutions` to true ONLY if the content has actual resolution documents, voting records, or formal decisions
- Remember: Contact information about officials/council members is CONTACTS, not resolutions

Use the `assess_content_type` tool to provide your assessment."""

    messages = [{"role": "user", "content": user_prompt}]
    
    try:
        response_data = call_llm_api(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=512,  # Small response needed
            temperature=0.0,
            tools=assessment_tool,
            tool_choice=tool_choice,
            max_retries=max_retries,
            initial_retry_delay=initial_retry_delay,
            api_call_delay=api_call_delay,
            llm_providers=llm_providers,
            llm_sequence=llm_sequence
        )

        if response_data is None:
            logger.error(f"Failed to assess content type for '{page_title}' (API call failed or returned no data).")
            return False, False

        # Handle tool response
        if isinstance(response_data, dict):
            contains_contacts = response_data.get('contains_contacts', False)
            contains_resolutions = response_data.get('contains_resolutions', False)
            
            # Ensure they are booleans
            if not isinstance(contains_contacts, bool):
                logger.warning(f"contains_contacts is not boolean: {contains_contacts}. Setting to False.")
                contains_contacts = False
            if not isinstance(contains_resolutions, bool):
                logger.warning(f"contains_resolutions is not boolean: {contains_resolutions}. Setting to False.")
                contains_resolutions = False
            
            logger.info(f"Content assessment for '{page_title}': contacts={contains_contacts}, resolutions={contains_resolutions}")
            return contains_contacts, contains_resolutions
        else:
            logger.error(f"Invalid response structure for content assessment from '{page_title}': {response_data}")
            return False, False

    except Exception as e:
        logger.error(f"Error during content type assessment for '{page_title}': {str(e)}")
        return False, False 