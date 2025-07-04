"""
Example config:
ai_agent_ha:
  ai_provider: openai  # or 'llama', 'gemini', 'openrouter', 'anthropic', 'local'
  llama_token: "..."
  openai_token: "..."
  gemini_token: "..."
  openrouter_token: "..."
  anthropic_token: "..."
  local_url: "http://localhost:11434/api/generate"  # Required for local models
  # Model configuration (optional, defaults will be used if not specified)
  models:
    openai: "gpt-3.5-turbo"  # or "gpt-4", "gpt-4-turbo", etc.
    llama: "Llama-4-Maverick-17B-128E-Instruct-FP8"
    gemini: "gemini-1.5-flash"  # or "gemini-1.5-pro", "gemini-1.0-pro", etc.
    openrouter: "openai/gpt-4o"  # or any model available on OpenRouter
    anthropic: "claude-3-5-sonnet-20241022"  # or "claude-3-opus-20240229", etc.
    local: "llama3.2"  # model name for local API (optional if your API doesn't require it)
"""
"""The AI Agent implementation with multiple provider support."""
import logging
import json
import aiohttp
import time
import yaml
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.helpers.storage import Store
from .const import DOMAIN, CONF_WEATHER_ENTITY, CONF_LANGUAGE, DEFAULT_LANGUAGE
import random
import re

_LOGGER = logging.getLogger(__name__)

# Provider-specific configurations for better rate limiting
PROVIDER_CONFIGS = {
    "openai": {
        "token_limits": {
            "gpt-4o-mini": {"tpm": 200000, "rpm": 10000, "context": 16384, "max_output": 4096},
            "gpt-4o": {"tpm": 30000, "rpm": 500, "context": 8192, "max_output": 4096},
            "gpt-3.5-turbo": {"tpm": 160000, "rpm": 10000, "context": 4096, "max_output": 4096},
            "gpt-4": {"tpm": 10000, "rpm": 500, "context": 8192, "max_output": 4096}
        },
        "max_retries": 10,
        "base_delay": 1,
        "max_delay": 120,
        "timeout": 300
    },
    "gemini": {
        "token_limits": {
            "gemini-1.5-flash": {"rpm": 1500, "context": 32768},
            "gemini-1.5-pro": {"rpm": 360, "context": 32768}
        },
        "max_retries": 5,
        "base_delay": 2,
        "max_delay": 60,
        "timeout": 30
    },
    "anthropic": {
        "token_limits": {
            "claude-3-5-sonnet-20241022": {"rpm": 1000, "context": 200000},
            "claude-3-haiku-20240307": {"rpm": 1000, "context": 200000}
        },
        "max_retries": 5,
        "base_delay": 1,
        "max_delay": 60,
        "timeout": 30
    },
    "openrouter": {
        "token_limits": {},  # Varies by model
        "max_retries": 8,
        "base_delay": 2,
        "max_delay": 60,
        "timeout": 300
    },
    "local": {
        "token_limits": {},
        "max_retries": 3,
        "base_delay": 1,
        "max_delay": 10,
        "timeout": 30
    }
}

def estimate_tokens_simple(text: str) -> int:
    """Simple token estimation: ~4 characters per token for most models"""
    if not text:
        return 0
    return max(1, len(text) // 4)

def estimate_message_tokens(message: Dict[str, Any]) -> int:
    """Estimate tokens in a message"""
    content = message.get("content", "")
    role = message.get("role", "")
    # Add overhead for role and formatting
    return estimate_tokens_simple(content) + estimate_tokens_simple(role) + 4

def parse_retry_after_from_error(error_text: str) -> Optional[float]:
    """Parse retry-after time from OpenAI error message"""
    try:
        # Look for "Please try again in X.Xs" pattern
        match = re.search(r"try again in ([\d.]+)s", str(error_text))
        if match:
            return float(match.group(1))
        
        # Look for "Retry-After" style numbers
        match = re.search(r"retry.{0,10}([\d.]+)", str(error_text), re.IGNORECASE)
        if match:
            return float(match.group(1))
    except (ValueError, AttributeError):
        pass
    return None

def calculate_exponential_backoff(retry_count: int, base_delay: float = 1.0, max_delay: float = 60.0) -> float:
    """Calculate exponential backoff with jitter"""
    # Exponential backoff: base_delay * (2 ^ retry_count)
    delay = min(base_delay * (2 ** retry_count), max_delay)
    
    # Add jitter (±30% random variation) to avoid thundering herd
    jitter = random.uniform(0.7, 1.3)
    return delay * jitter

# === AI Client Abstractions ===
class BaseAIClient:
    async def get_response(self, messages, **kwargs):
        raise NotImplementedError

class LocalClient(BaseAIClient):
    def __init__(self, url, model=""):
        self.url = url
        self.model = model
    
    async def get_response(self, messages, **kwargs):
        _LOGGER.debug("Making request to local API with model: %s at URL: %s", self.model, self.url)
        headers = {
            "Content-Type": "application/json"
        }
        
        # Format user prompt from messages
        prompt = ""
        for message in messages:
            role = message.get("role", "")
            content = message.get("content", "")
            
            # Simple formatting: prefixing each message with its role
            if role == "system":
                prompt += f"System: {content}\n\n"
            elif role == "user":
                prompt += f"User: {content}\n\n"
            elif role == "assistant":
                prompt += f"Assistant: {content}\n\n"
        
        # Add final prompt prefix for the assistant's response
        prompt += "Assistant: "
        
        # Build a generic payload that works with most local API servers
        payload = {
            "prompt": prompt,
            "stream": False  # Disable streaming to get a single complete response
        }
        
        # Add model if specified
        if self.model:
            payload["model"] = self.model
        
        _LOGGER.debug("Local API request payload: %s", json.dumps(payload, indent=2))
        
        async with aiohttp.ClientSession() as session:
            async with session.post(self.url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    _LOGGER.error("Local API error %d: %s", resp.status, error_text)
                    raise Exception(f"Local API error {resp.status}")
                
                try:
                    response_text = await resp.text()
                    _LOGGER.debug("Local API response (first 200 chars): %s", response_text[:200])
                    
                    # Try to parse as JSON
                    try:
                        data = json.loads(response_text)
                        
                        # Try common response formats
                        # Ollama format - return only the response text
                        if "response" in data:
                            response_content = data["response"]
                            _LOGGER.debug("Extracted response content: %s", response_content[:100])
                            
                            # Check if the response looks like JSON
                            response_content = response_content.strip()
                            if response_content.startswith('{') and response_content.endswith('}'):
                                try:
                                    # Validate that it's actually JSON and contains valid request_type
                                    parsed_json = json.loads(response_content)
                                    if isinstance(parsed_json, dict) and "request_type" in parsed_json:
                                        _LOGGER.debug("Local model provided valid JSON response")
                                        return response_content
                                    else:
                                        _LOGGER.debug("JSON missing request_type, treating as plain text")
                                except json.JSONDecodeError:
                                    _LOGGER.debug("Invalid JSON from local model, treating as plain text")
                                    pass
                            
                            # If it's plain text, wrap it in the expected JSON format
                            wrapped_response = {
                                "request_type": "final_response",
                                "response": response_content
                            }
                            _LOGGER.debug("Wrapped plain text response in JSON format")
                            return json.dumps(wrapped_response)
                        
                        # OpenAI-like format
                        elif "choices" in data and len(data["choices"]) > 0:
                            choice = data["choices"][0]
                            if "message" in choice and "content" in choice["message"]:
                                content = choice["message"]["content"]
                            elif "text" in choice:
                                content = choice["text"]
                            else:
                                content = str(data)
                            
                            # Check if it's valid JSON with request_type
                            content = content.strip()
                            if content.startswith('{') and content.endswith('}'):
                                try:
                                    parsed_json = json.loads(content)
                                    if isinstance(parsed_json, dict) and "request_type" in parsed_json:
                                        _LOGGER.debug("Local model provided valid JSON response (OpenAI format)")
                                        return content
                                    else:
                                        _LOGGER.debug("JSON missing request_type, treating as plain text (OpenAI format)")
                                except json.JSONDecodeError:
                                    _LOGGER.debug("Invalid JSON from local model, treating as plain text (OpenAI format)")
                                    pass
                            
                            # Wrap in expected format if plain text
                            wrapped_response = {
                                "request_type": "final_response",
                                "response": content
                            }
                            return json.dumps(wrapped_response)
                        
                        # Generic content field
                        elif "content" in data:
                            content = data["content"]
                            content = content.strip()
                            if content.startswith('{') and content.endswith('}'):
                                try:
                                    parsed_json = json.loads(content)
                                    if isinstance(parsed_json, dict) and "request_type" in parsed_json:
                                        _LOGGER.debug("Local model provided valid JSON response (generic format)")
                                        return content
                                    else:
                                        _LOGGER.debug("JSON missing request_type, treating as plain text (generic format)")
                                except json.JSONDecodeError:
                                    _LOGGER.debug("Invalid JSON from local model, treating as plain text (generic format)")
                                    pass
                            
                            wrapped_response = {
                                "request_type": "final_response",
                                "response": content
                            }
                            return json.dumps(wrapped_response)
                        
                        # Return the whole data as string if we can't find a specific field
                        return str(data)
                        
                    except json.JSONDecodeError:
                        # If not JSON, check if it's a JSON response that got corrupted by wrapping
                        response_text = response_text.strip()
                        if response_text.startswith('{') and response_text.endswith('}'):
                            try:
                                parsed_json = json.loads(response_text)
                                if isinstance(parsed_json, dict) and "request_type" in parsed_json:
                                    _LOGGER.debug("Local model provided valid JSON response (direct)")
                                    return response_text
                            except json.JSONDecodeError:
                                pass
                        
                        # If not valid JSON, wrap the raw text in expected format
                        _LOGGER.debug("Response is not JSON, wrapping plain text")
                        wrapped_response = {
                            "request_type": "final_response", 
                            "response": response_text
                        }
                        return json.dumps(wrapped_response)
                        
                except Exception as e:
                    _LOGGER.error("Failed to parse local API response: %s", str(e))
                    raise Exception(f"Failed to parse local API response: {str(e)}")

class LlamaClient(BaseAIClient):
    def __init__(self, token, model="Llama-4-Maverick-17B-128E-Instruct-FP8"):
        self.token = token
        self.model = model
        self.api_url = "https://api.llama.com/v1/chat/completions"
    
    async def get_response(self, messages, **kwargs):
        _LOGGER.debug("Making request to Llama API with model: %s", self.model)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 2048,
            "temperature": 0.7,
            "top_p": 0.9
        }

        _LOGGER.debug("Llama request payload: %s", json.dumps(payload, indent=2))

        async with aiohttp.ClientSession() as session:
            async with session.post(self.api_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    _LOGGER.error("Llama API error %d: %s", resp.status, error_text)
                    raise Exception(f"Llama API error {resp.status}")
                data = await resp.json()
                # Extract text from Llama response
                completion = data.get('completion_message', {})
                content = completion.get('content', {})
                return content.get('text', str(data))

class OpenAIClient(BaseAIClient):
    def __init__(self, token, model="gpt-3.5-turbo"):
        self.token = token
        self.model = model
        self.api_url = "https://api.openai.com/v1/chat/completions"
        
        # Get provider config for this model
        self.config = PROVIDER_CONFIGS.get("openai", {})
        self.token_limits = self.config.get("token_limits", {}).get(model, {})
        
    def _get_token_parameter(self):
        """Determine which token parameter to use based on the model."""
        # Models that require max_completion_tokens instead of max_tokens
        completion_token_models = [
            "o3-mini", "o3", "o1-mini", "o1-preview", "o1"
        ]
        
        # Check if the model name contains any of the newer model identifiers
        model_lower = self.model.lower()
        if any(model_id in model_lower for model_id in completion_token_models):
            return "max_completion_tokens"
        return "max_tokens"
    
    def _is_restricted_model(self):
        """Check if the model has restricted parameters (no temperature, top_p, etc.)."""
        # Models that don't support temperature, top_p and other parameters
        restricted_models = [
            "o3-mini", "o3", "o1-mini", "o1-preview", "o1"
        ]
        
        model_lower = self.model.lower()
        return any(model_id in model_lower for model_id in restricted_models)
    
    def _estimate_request_tokens(self, messages: List[Dict[str, Any]], max_tokens: int = 2048) -> int:
        """Estimate total tokens for the request"""
        input_tokens = sum(estimate_message_tokens(msg) for msg in messages)
        # Add overhead for API formatting
        overhead = 50
        return input_tokens + max_tokens + overhead
    
    def _handle_rate_limit_error(self, error_text: str) -> float:
        """Extract wait time from OpenAI rate limit error"""
        wait_time = parse_retry_after_from_error(error_text)
        
        if "tokens per min" in error_text:
            # For token rate limits, wait longer
            return max(wait_time or 60, 30)
        elif "requests per min" in error_text:
            # For request rate limits, shorter wait
            return max(wait_time or 5, 1)
        else:
            # Default wait time
            return wait_time or 10

    async def get_response(self, messages, **kwargs):
        _LOGGER.debug("Making request to OpenAI API with model: %s", self.model)
        
        # Validate token
        if not self.token or not self.token.startswith("sk-"):
            raise Exception("Invalid OpenAI API key format")
        
        # Pre-check token limits if available
        if self.token_limits.get("tpm"):
            estimated_tokens = self._estimate_request_tokens(messages)
            max_safe_tokens = int(self.token_limits["tpm"] * 0.8)  # Use 80% of limit for safety
            
            if estimated_tokens > max_safe_tokens:
                _LOGGER.warning("Request may exceed token limits: estimated %d tokens, safe limit %d", 
                               estimated_tokens, max_safe_tokens)
            
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        
        # Determine which token parameter to use
        token_param = self._get_token_parameter()
        is_restricted = self._is_restricted_model()
        _LOGGER.debug("Using token parameter '%s' for model: %s (restricted: %s)", 
                     token_param, self.model, is_restricted)
        
        # Build payload with model-appropriate parameters
        # Reduce max_tokens to be more conservative
        max_tokens = min(2048, self.token_limits.get("max_output", 2048))
        
        payload = {
            "model": self.model,
            "messages": messages,
            token_param: max_tokens
        }
        
        # Only add temperature and top_p for models that support them
        if not is_restricted:
            payload.update({
                "temperature": 0.7,
                "top_p": 0.9
            })
        
        _LOGGER.debug("OpenAI request payload: %s", json.dumps(payload, indent=2))
        
        async with aiohttp.ClientSession() as session:
            async with session.post(self.api_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                response_text = await resp.text()
                _LOGGER.debug("OpenAI API response status: %d", resp.status)
                _LOGGER.debug("OpenAI API response: %s", response_text[:500])
                
                if resp.status == 429:
                    # Rate limit exceeded - extract wait time and raise with specific info
                    wait_time = self._handle_rate_limit_error(response_text)
                    error_msg = f"Rate limit exceeded. Wait time: {wait_time}s. Original error: {response_text}"
                    _LOGGER.warning("OpenAI rate limit hit, recommended wait: %ss", wait_time)
                    raise Exception(error_msg)
                elif resp.status != 200:
                    _LOGGER.error("OpenAI API error %d: %s", resp.status, response_text)
                    raise Exception(f"OpenAI API error {resp.status}: {response_text}")
                    
                try:
                    data = json.loads(response_text)
                except json.JSONDecodeError as e:
                    _LOGGER.error("Failed to parse OpenAI response as JSON: %s", str(e))
                    raise Exception(f"Invalid JSON response from OpenAI: {response_text[:200]}")
                
                # Extract text from OpenAI response
                choices = data.get('choices', [])
                if choices and 'message' in choices[0]:
                    content = choices[0]['message'].get('content', '')
                    if not content:
                        _LOGGER.warning("OpenAI returned empty content in message")
                        _LOGGER.debug("Full OpenAI response: %s", json.dumps(data, indent=2))
                    return content
                else:
                    _LOGGER.warning("OpenAI response missing expected structure")
                    _LOGGER.debug("Full OpenAI response: %s", json.dumps(data, indent=2))
                    return str(data)

class GeminiClient(BaseAIClient):
    def __init__(self, token, model="gemini-1.5-flash"):
        self.token = token
        self.model = model
        self.api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    
    async def get_response(self, messages, **kwargs):
        _LOGGER.debug("Making request to Gemini API with model: %s", self.model)
        
        # Validate token
        if not self.token:
            raise Exception("Missing Gemini API key")
        
        headers = {
            "Content-Type": "application/json"
        }
        
        # Convert OpenAI-style messages to Gemini format
        gemini_contents = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            
            if role == "system":
                # Gemini doesn't have a system role, so we prepend it to the first user message
                if not gemini_contents:
                    gemini_contents.append({
                        "role": "user",
                        "parts": [{"text": f"System: {content}"}]
                    })
                else:
                    # Add system message as user message
                    gemini_contents.append({
                        "role": "user", 
                        "parts": [{"text": f"System: {content}"}]
                    })
            elif role == "user":
                gemini_contents.append({
                    "role": "user",
                    "parts": [{"text": content}]
                })
            elif role == "assistant":
                gemini_contents.append({
                    "role": "model",
                    "parts": [{"text": content}]
                })
        
        payload = {
            "contents": gemini_contents,
            "generationConfig": {
                "temperature": 0.7,
                "topP": 0.9,
                "maxOutputTokens": 2048
            }
        }
        
        # Add API key as query parameter
        url_with_key = f"{self.api_url}?key={self.token}"
        
        _LOGGER.debug("Gemini request payload: %s", json.dumps(payload, indent=2))
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url_with_key, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                response_text = await resp.text()
                _LOGGER.debug("Gemini API response status: %d", resp.status)
                _LOGGER.debug("Gemini API response: %s", response_text[:500])
                
                if resp.status != 200:
                    _LOGGER.error("Gemini API error %d: %s", resp.status, response_text)
                    raise Exception(f"Gemini API error {resp.status}: {response_text}")
                    
                try:
                    data = json.loads(response_text)
                except json.JSONDecodeError as e:
                    _LOGGER.error("Failed to parse Gemini response as JSON: %s", str(e))
                    raise Exception(f"Invalid JSON response from Gemini: {response_text[:200]}")
                
                # Extract text from Gemini response
                candidates = data.get('candidates', [])
                if candidates and 'content' in candidates[0]:
                    parts = candidates[0]['content'].get('parts', [])
                    if parts:
                        content = parts[0].get('text', '')
                        if not content:
                            _LOGGER.warning("Gemini returned empty text content")
                            _LOGGER.debug("Full Gemini response: %s", json.dumps(data, indent=2))
                        return content
                    else:
                        _LOGGER.warning("Gemini response missing parts")
                        _LOGGER.debug("Full Gemini response: %s", json.dumps(data, indent=2))
                else:
                    _LOGGER.warning("Gemini response missing expected structure")
                    _LOGGER.debug("Full Gemini response: %s", json.dumps(data, indent=2))
                return str(data)

class AnthropicClient(BaseAIClient):
    def __init__(self, token, model="claude-3-5-sonnet-20241022"):
        self.token = token
        self.model = model
        self.api_url = "https://api.anthropic.com/v1/messages"
    
    async def get_response(self, messages, **kwargs):
        _LOGGER.debug("Making request to Anthropic API with model: %s", self.model)
        headers = {
            "x-api-key": self.token,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01"
        }
        
        # Convert OpenAI-style messages to Anthropic format
        system_message = None
        anthropic_messages = []
        
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            
            if role == "system":
                # Anthropic uses a separate system parameter
                system_message = content
            elif role == "user":
                anthropic_messages.append({
                    "role": "user",
                    "content": content
                })
            elif role == "assistant":
                anthropic_messages.append({
                    "role": "assistant", 
                    "content": content
                })
        
        payload = {
            "model": self.model,
            "max_tokens": 2048,
            "temperature": 0.7,
            "messages": anthropic_messages
        }
        
        # Add system message if present
        if system_message:
            payload["system"] = system_message
        
        _LOGGER.debug("Anthropic request payload: %s", json.dumps(payload, indent=2))
        
        async with aiohttp.ClientSession() as session:
            async with session.post(self.api_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    _LOGGER.error("Anthropic API error %d: %s", resp.status, error_text)
                    raise Exception(f"Anthropic API error {resp.status}")
                data = await resp.json()
                # Extract text from Anthropic response
                content_blocks = data.get('content', [])
                if content_blocks and isinstance(content_blocks, list):
                    # Get the text from the first content block
                    for block in content_blocks:
                        if block.get('type') == 'text':
                            return block.get('text', str(data))
                return str(data)

class OpenRouterClient(BaseAIClient):
    def __init__(self, token, model="openai/gpt-4o"):
        self.token = token
        self.model = model
        self.api_url = "https://openrouter.ai/api/v1/chat/completions"
    
    async def get_response(self, messages, **kwargs):
        _LOGGER.debug("Making request to OpenRouter API with model: %s", self.model)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://home-assistant.io",  # Optional for OpenRouter rankings
            "X-Title": "Home Assistant AI Agent"  # Optional for OpenRouter rankings
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 2048,
            "temperature": 0.7,
            "top_p": 0.9
        }

        _LOGGER.debug("OpenRouter request payload: %s", json.dumps(payload, indent=2))

        async with aiohttp.ClientSession() as session:
            async with session.post(self.api_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    _LOGGER.error("OpenRouter API error %d: %s", resp.status, error_text)
                    raise Exception(f"OpenRouter API error {resp.status}")
                data = await resp.json()
                # Extract text from OpenRouter response (OpenAI-compatible format)
                choices = data.get('choices', [])
                if not choices:
                    _LOGGER.warning("OpenRouter response missing choices")
                    _LOGGER.debug("Full OpenRouter response: %s", json.dumps(data, indent=2))
                    return str(data)
                if choices and 'message' in choices[0]:
                    return choices[0]['message'].get('content', str(data))
                return str(data)

# === Main Agent ===
class AiAgentHaAgent:
    """Agent for handling queries with dynamic data requests and multiple AI providers."""

    SYSTEM_PROMPT = {
        "role": "system",
        "content": (
            "You are an AI assistant integrated with Home Assistant.\n"
            "You can request specific data by using only these commands:\n"
            "- get_entity_state(entity_id): Get state of a specific entity\n"
            "- get_entities_by_domain(domain): Get all entities in a domain\n"
            "- get_entities_by_area(area_id): Get all entities in a specific area\n"
            "- get_entities(area_id or area_ids): Get entities by area(s) - supports single area_id or list of area_ids\n"
            "  Use as: get_entities(area_ids=['area1', 'area2']) for multiple areas or get_entities(area_id='single_area')\n"
            "- get_calendar_events(entity_id?): Get calendar events\n"
            "- get_automations(): Get all automations\n"
            "- get_weather_data(): Get current weather and forecast data\n"
            "- get_entity_registry(): Get entity registry entries\n"
            "- get_device_registry(): Get device registry entries\n"
            "- get_area_registry(): Get room/area information\n"
            "- get_history(entity_id, hours): Get historical state changes\n"
            "- get_logbook_entries(hours): Get recent events\n"
            "- get_person_data(): Get person tracking information\n"
            "- get_statistics(entity_id): Get sensor statistics\n"
            "- get_scenes(): Get scene configurations\n"
            "- get_dashboards(): Get list of all dashboards\n"
            "- get_dashboard_config(dashboard_url): Get configuration of a specific dashboard\n"
            "- set_entity_state(entity_id, state, attributes?): Set state of an entity (e.g., turn on/off lights, open/close covers)\n"
            "- call_service(domain, service, target?, service_data?): Call any Home Assistant service directly\n"
            "- create_automation(automation): Create a new automation with the provided configuration\n"
            "- create_dashboard(dashboard_config): Create a new dashboard with the provided configuration\n"
            "- update_dashboard(dashboard_url, dashboard_config): Update an existing dashboard configuration\n\n"
            "You can also create dashboards when users ask for them. When creating dashboards:\n"
            "1. First gather information about available entities, areas, and devices\n"
            "2. Ask follow-up questions if the user's requirements are unclear\n"
            "3. Create a dashboard configuration with appropriate cards and views\n"
            "4. Use common card types like: entities, glance, picture-entity, weather-forecast, thermostat, media-control, etc.\n"
            "5. Organize cards logically by rooms, device types, or functionality\n"
            "6. Include relevant entities based on the user's request\n\n"
            "IMPORTANT AREA/FLOOR GUIDANCE:\n"
            "- When users ask for entities from a specific floor, use get_area_registry() first\n"
            "- Areas have both 'area_id' and 'floor_id' - these are different concepts\n"
            "- Filter areas by their floor_id to find all areas on a specific floor\n"
            "- Use get_entities() with area_ids parameter to get entities from multiple areas efficiently\n"
            "- Example: get_entities(area_ids=['area1', 'area2', 'area3']) for multiple areas at once\n"
            "- This is more efficient than calling get_entities_by_area() multiple times\n\n"
            "You can also create automations when users ask for them. When you detect that a user wants to create an automation. make sure to request first entities so you know the entities ids to trigger on. pay attention that if you want to set specfic days in the autoamtion you should use those days: ['fri', 'mon', 'sat', 'sun', 'thu', 'tue', 'wed'] \n"
            "respond with a JSON object in this format:\n"
            "{\n"
            "  \"request_type\": \"automation_suggestion\",\n"
            "  \"message\": \"I've created an automation that might help you. Would you like me to create it?\",\n"
            "  \"automation\": {\n"
            "    \"alias\": \"Name of the automation\",\n"
            "    \"description\": \"Description of what the automation does\",\n"
            "    \"trigger\": [...],  // Array of trigger conditions\n"
            "    \"condition\": [...], // Optional array of conditions\n"
            "    \"action\": [...]     // Array of actions to perform\n"
            "  }\n"
            "}\n\n"
            "For dashboard creation requests, use this exact JSON format:\n"
            "{\n"
            "  \"request_type\": \"dashboard_suggestion\",\n"
            "  \"message\": \"I've created a dashboard configuration for you. Would you like me to create it?\",\n"
            "  \"dashboard\": {\n"
            "    \"title\": \"Dashboard Title\",\n"
            "    \"url_path\": \"dashboard-url-path\",\n"
            "    \"icon\": \"mdi:icon-name\",\n"
            "    \"show_in_sidebar\": true,\n"
            "    \"views\": [{\n"
            "      \"title\": \"View Title\",\n"
            "      \"cards\": [...] // Array of card configurations\n"
            "    }]\n"
            "  }\n"
            "}\n\n"
            "For data requests, use this exact JSON format:\n"
            "{\n"
            "  \"request_type\": \"data_request\",\n"
            "  \"request\": \"command_name\",\n"
            "  \"parameters\": {...}\n"
            "}\n"
            "For get_entities with multiple areas: {\"request_type\": \"get_entities\", \"parameters\": {\"area_ids\": [\"area1\", \"area2\"]}}\n"
            "For get_entities with single area: {\"request_type\": \"get_entities\", \"parameters\": {\"area_id\": \"single_area\"}}\n\n"
            "For service calls, use this exact JSON format:\n"
            "{\n"
            "  \"request_type\": \"call_service\",\n"
            "  \"domain\": \"light\",\n"
            "  \"service\": \"turn_on\",\n"
            "  \"target\": {\"entity_id\": [\"entity1\", \"entity2\"]},\n"
            "  \"service_data\": {\"brightness\": 255}\n"
            "}\n\n"
            "When you have all the data you need, respond with this exact JSON format:\n"
            "{\n"
            "  \"request_type\": \"final_response\",\n"
            "  \"response\": \"your answer to the user\"\n"
            "}\n\n"
            "CRITICAL FORMATTING RULES:\n"
            "- You must ALWAYS respond with ONLY a valid JSON object\n"
            "- DO NOT include any text before the JSON\n"
            "- DO NOT include any text after the JSON\n"
            "- DO NOT include explanations or descriptions outside the JSON\n"
            "- Your entire response must be parseable as JSON\n"
            "- Use the 'message' field inside the JSON for user-facing text\n"
            "- NEVER mix regular text with JSON in your response\n\n"
            "WRONG: 'I'll create this for you. {\"request_type\": ...}'\n"
            "CORRECT: '{\"request_type\": \"dashboard_suggestion\", \"message\": \"I'll create this for you.\", ...}'"
        )
    }

    SYSTEM_PROMPT_DE = {
        "role": "system",
        "content": (
            "Du bist ein KI-Assistent, der in Home Assistant integriert ist.\n"
            "Du kannst spezifische Daten mit diesen Befehlen abrufen:\n"
            "- get_entity_state(entity_id): Zustand einer bestimmten Entität abrufen\n"
            "- get_entities_by_domain(domain): Alle Entitäten einer Domäne abrufen\n"
            "- get_entities_by_area(area_id): Alle Entitäten eines bestimmten Bereichs abrufen\n"
            "- get_entities(area_id oder area_ids): Entitäten nach Bereich(en) abrufen - unterstützt einzelne area_id oder Liste von area_ids\n"
            "  Verwendung: get_entities(area_ids=['area1', 'area2']) für mehrere Bereiche oder get_entities(area_id='einzelner_bereich')\n"
            "- get_calendar_events(entity_id?): Kalender-Ereignisse abrufen\n"
            "- get_automations(): Alle Automatisierungen abrufen\n"
            "- get_weather_data(): Aktuelle Wetter- und Vorhersagedaten abrufen\n"
            "- get_entity_registry(): Entitäts-Registry-Einträge abrufen\n"
            "- get_device_registry(): Geräte-Registry-Einträge abrufen\n"
            "- get_area_registry(): Raum-/Bereichsinformationen abrufen\n"
            "- get_history(entity_id, hours): Historische Zustandsänderungen abrufen\n"
            "- get_logbook_entries(hours): Aktuelle Ereignisse abrufen\n"
            "- get_person_data(): Personenverfolgungsinformationen abrufen\n"
            "- get_statistics(entity_id): Sensor-Statistiken abrufen\n"
            "- get_scenes(): Szenen-Konfigurationen abrufen\n"
            "- get_dashboards(): Liste aller Dashboards abrufen\n"
            "- get_dashboard_config(dashboard_url): Konfiguration eines bestimmten Dashboards abrufen\n"
            "- set_entity_state(entity_id, state, attributes?): Zustand einer Entität setzen (z.B. Lichter ein-/ausschalten, Rollläden öffnen/schließen)\n"
            "- call_service(domain, service, target?, service_data?): Jeden Home Assistant Service direkt aufrufen\n"
            "- create_automation(automation): Neue Automatisierung mit bereitgestellter Konfiguration erstellen\n"
            "- create_dashboard(dashboard_config): Neues Dashboard mit bereitgestellter Konfiguration erstellen\n"
            "- update_dashboard(dashboard_url, dashboard_config): Bestehende Dashboard-Konfiguration aktualisieren\n\n"
            "Du kannst auch Dashboards erstellen, wenn Benutzer danach fragen. Beim Erstellen von Dashboards:\n"
            "1. Sammle zuerst Informationen über verfügbare Entitäten, Bereiche und Geräte\n"
            "2. Stelle Nachfragen, wenn die Anforderungen des Benutzers unklar sind\n"
            "3. Erstelle eine Dashboard-Konfiguration mit passenden Karten und Ansichten\n"
            "4. Verwende gängige Kartentypen wie: entities, glance, picture-entity, weather-forecast, thermostat, media-control, usw.\n"
            "5. Organisiere Karten logisch nach Räumen, Gerätetypen oder Funktionalität\n"
            "6. Füge relevante Entitäten basierend auf der Benutzeranfrage hinzu\n\n"
            "WICHTIGE BEREICH/ETAGEN-HINWEISE:\n"
            "- Wenn Benutzer nach Entitäten aus einer bestimmten Etage fragen, verwende zuerst get_area_registry()\n"
            "- Bereiche haben sowohl 'area_id' als auch 'floor_id' - das sind verschiedene Konzepte\n"
            "- Filtere Bereiche nach ihrer floor_id, um alle Bereiche einer bestimmten Etage zu finden\n"
            "- Verwende get_entities() mit area_ids Parameter, um Entitäten aus mehreren Bereichen effizient abzurufen\n"
            "- Beispiel: get_entities(area_ids=['area1', 'area2', 'area3']) für mehrere Bereiche auf einmal\n"
            "- Das ist effizienter als mehrfaches Aufrufen von get_entities_by_area()\n\n"
            "Du kannst auch Automatisierungen erstellen, wenn Benutzer danach fragen. Wenn du erkennst, dass ein Benutzer eine Automatisierung erstellen möchte, stelle sicher, dass du zuerst Entitäten abrufst, damit du die Entitäts-IDs für die Trigger kennst. Beachte, dass wenn du bestimmte Tage in der Automatisierung festlegen möchtest, du diese Tage verwenden solltest: ['fri', 'mon', 'sat', 'sun', 'thu', 'tue', 'wed']\n"
            "Antworte mit einem JSON-Objekt in diesem Format:\n"
            "{\n"
            "  \"request_type\": \"automation_suggestion\",\n"
            "  \"message\": \"Ich habe eine Automatisierung erstellt, die dir helfen könnte. Soll ich sie erstellen?\",\n"
            "  \"automation\": {\n"
            "    \"alias\": \"Name der Automatisierung\",\n"
            "    \"description\": \"Beschreibung was die Automatisierung macht\",\n"
            "    \"trigger\": [...],  // Array von Trigger-Bedingungen\n"
            "    \"condition\": [...], // Optionales Array von Bedingungen\n"
            "    \"action\": [...]     // Array von auszuführenden Aktionen\n"
            "  }\n"
            "}\n\n"
            "Für Dashboard-Erstellungsanfragen verwende exakt dieses JSON-Format:\n"
            "{\n"
            "  \"request_type\": \"dashboard_suggestion\",\n"
            "  \"message\": \"Ich habe eine Dashboard-Konfiguration für dich erstellt. Soll ich sie erstellen?\",\n"
            "  \"dashboard\": {\n"
            "    \"title\": \"Dashboard-Titel\",\n"
            "    \"url_path\": \"dashboard-url-pfad\",\n"
            "    \"icon\": \"mdi:icon-name\",\n"
            "    \"show_in_sidebar\": true,\n"
            "    \"views\": [{\n"
            "      \"title\": \"Ansichts-Titel\",\n"
            "      \"cards\": [...] // Array von Kartenkonfigurationen\n"
            "    }]\n"
            "  }\n"
            "}\n\n"
            "Für Datenanfragen verwende exakt dieses JSON-Format:\n"
            "{\n"
            "  \"request_type\": \"data_request\",\n"
            "  \"request\": \"befehl_name\",\n"
            "  \"parameters\": {...}\n"
            "}\n"
            "Für get_entities mit mehreren Bereichen: {\"request_type\": \"get_entities\", \"parameters\": {\"area_ids\": [\"area1\", \"area2\"]}}\n"
            "Für get_entities mit einzelnem Bereich: {\"request_type\": \"get_entities\", \"parameters\": {\"area_id\": \"einzelner_bereich\"}}\n\n"
            "Für Service-Aufrufe verwende exakt dieses JSON-Format:\n"
            "{\n"
            "  \"request_type\": \"call_service\",\n"
            "  \"domain\": \"light\",\n"
            "  \"service\": \"turn_on\",\n"
            "  \"target\": {\"entity_id\": [\"entity1\", \"entity2\"]},\n"
            "  \"service_data\": {\"brightness\": 255}\n"
            "}\n\n"
            "Wenn du alle benötigten Daten hast, antworte mit exakt diesem JSON-Format:\n"
            "{\n"
            "  \"request_type\": \"final_response\",\n"
            "  \"response\": \"deine Antwort an den Benutzer\"\n"
            "}\n\n"
            "KRITISCHE FORMATIERUNGSREGELN:\n"
            "- Du MUSST IMMER mit NUR einem gültigen JSON-Objekt antworten\n"
            "- Füge KEINEN Text vor dem JSON hinzu\n"
            "- Füge KEINEN Text nach dem JSON hinzu\n"
            "- Füge KEINE Erklärungen oder Beschreibungen außerhalb des JSON hinzu\n"
            "- Deine gesamte Antwort muss als JSON parsebar sein\n"
            "- Verwende das 'message'-Feld innerhalb des JSON für benutzerzugewandten Text\n"
            "- Mische NIEMALS normalen Text mit JSON in deiner Antwort\n\n"
            "FALSCH: 'Ich erstelle das für dich. {\"request_type\": ...}'\n"
            "RICHTIG: '{\"request_type\": \"dashboard_suggestion\", \"message\": \"Ich erstelle das für dich.\", ...}'"
        )
    }

    SYSTEM_PROMPT_LOCAL = {
        "role": "system",
        "content": (
            "You are an AI assistant integrated with Home Assistant.\n"
            "You can request specific data by using only these commands:\n"
            "- get_entity_state(entity_id): Get state of a specific entity\n"
            "- get_entities_by_domain(domain): Get all entities in a domain\n"
            "- get_entities_by_area(area_id): Get all entities in a specific area\n"
            "- get_entities(area_id or area_ids): Get entities by area(s) - supports single area_id or list of area_ids\n"
            "  Use as: get_entities(area_ids=['area1', 'area2']) for multiple areas or get_entities(area_id='single_area')\n"
            "- get_calendar_events(entity_id?): Get calendar events\n"
            "- get_automations(): Get all automations\n"
            "- get_weather_data(): Get current weather and forecast data\n"
            "- get_entity_registry(): Get entity registry entries\n"
            "- get_device_registry(): Get device registry entries\n"
            "- get_area_registry(): Get room/area information\n"
            "- get_history(entity_id, hours): Get historical state changes\n"
            "- get_logbook_entries(hours): Get recent events\n"
            "- get_person_data(): Get person tracking information\n"
            "- get_statistics(entity_id): Get sensor statistics\n"
            "- get_scenes(): Get scene configurations\n"
            "- get_dashboards(): Get list of all dashboards\n"
            "- get_dashboard_config(dashboard_url): Get configuration of a specific dashboard\n"
            "- set_entity_state(entity_id, state, attributes?): Set state of an entity (e.g., turn on/off lights, open/close covers)\n"
            "- call_service(domain, service, target?, service_data?): Call any Home Assistant service directly\n"
            "- create_automation(automation): Create a new automation with the provided configuration\n"
            "- create_dashboard(dashboard_config): Create a new dashboard with the provided configuration\n"
            "- update_dashboard(dashboard_url, dashboard_config): Update an existing dashboard configuration\n\n"
            "You can also create dashboards when users ask for them. When creating dashboards:\n"
            "1. First gather information about available entities, areas, and devices\n"
            "2. Ask follow-up questions if the user's requirements are unclear\n"
            "3. Create a dashboard configuration with appropriate cards and views\n"
            "4. Use common card types like: entities, glance, picture-entity, weather-forecast, thermostat, media-control, etc.\n"
            "5. Organize cards logically by rooms, device types, or functionality\n"
            "6. Include relevant entities based on the user's request\n\n"
            "IMPORTANT AREA/FLOOR GUIDANCE:\n"
            "- When users ask for entities from a specific floor, use get_area_registry() first\n"
            "- Areas have both 'area_id' and 'floor_id' - these are different concepts\n"
            "- Filter areas by their floor_id to find all areas on a specific floor\n"
            "- Use get_entities() with area_ids parameter to get entities from multiple areas efficiently\n"
            "- Example: get_entities(area_ids=['area1', 'area2', 'area3']) for multiple areas at once\n"
            "- This is more efficient than calling get_entities_by_area() multiple times\n\n"
            "You can also create automations when users ask for them. When you detect that a user wants to create an automation, make sure to request first entities so you know the entities ids to trigger on. pay attention that if you want to set specific days in the automation you should use those days: ['fri', 'mon', 'sat', 'sun', 'thu', 'tue', 'wed'] \n"
            "respond with a JSON object in this format:\n"
            "{\n"
            "  \"request_type\": \"automation_suggestion\",\n"
            "  \"message\": \"I've created an automation that might help you. Would you like me to create it?\",\n"
            "  \"automation\": {\n"
            "    \"alias\": \"Name of the automation\",\n"
            "    \"description\": \"Description of what the automation does\",\n"
            "    \"trigger\": [...],  // Array of trigger conditions\n"
            "    \"condition\": [...], // Optional array of conditions\n"
            "    \"action\": [...]     // Array of actions to perform\n"
            "  }\n"
            "}\n\n"
            "For dashboard creation requests, use this exact JSON format:\n"
            "{\n"
            "  \"request_type\": \"dashboard_suggestion\",\n"
            "  \"message\": \"I've created a dashboard configuration for you. Would you like me to create it?\",\n"
            "  \"dashboard\": {\n"
            "    \"title\": \"Dashboard Title\",\n"
            "    \"url_path\": \"dashboard-url-path\",\n"
            "    \"icon\": \"mdi:icon-name\",\n"
            "    \"show_in_sidebar\": true,\n"
            "    \"views\": [{\n"
            "      \"title\": \"View Title\",\n"
            "      \"cards\": [...] // Array of card configurations\n"
            "    }]\n"
            "  }\n"
            "}\n\n"
            "For data requests, use this exact JSON format:\n"
            "{\n"
            "  \"request_type\": \"data_request\",\n"
            "  \"request\": \"command_name\",\n"
            "  \"parameters\": {...}\n"
            "}\n"
            "For get_entities with multiple areas: {\"request_type\": \"get_entities\", \"parameters\": {\"area_ids\": [\"area1\", \"area2\"]}}\n"
            "For get_entities with single area: {\"request_type\": \"get_entities\", \"parameters\": {\"area_id\": \"single_area\"}}\n\n"
            "For service calls, use this exact JSON format:\n"
            "{\n"
            "  \"request_type\": \"call_service\",\n"
            "  \"domain\": \"light\",\n"
            "  \"service\": \"turn_on\",\n"
            "  \"target\": {\"entity_id\": [\"entity1\", \"entity2\"]},\n"
            "  \"service_data\": {\"brightness\": 255}\n"
            "}\n\n"
            "When you have all the data you need, respond with this exact JSON format:\n"
            "{\n"
            "  \"request_type\": \"final_response\",\n"
            "  \"response\": \"your answer to the user\"\n"
            "}\n\n"
            "CRITICAL FORMATTING RULES:\n"
            "- You must ALWAYS respond with ONLY a valid JSON object\n"
            "- DO NOT include any text before the JSON\n"
            "- DO NOT include any text after the JSON\n"
            "- DO NOT include explanations or descriptions outside the JSON\n"
            "- Your entire response must be parseable as JSON\n"
            "- Use the 'message' field inside the JSON for user-facing text\n"
            "- NEVER mix regular text with JSON in your response\n\n"
            "WRONG: 'I'll create this for you. {\"request_type\": ...}'\n"
            "CORRECT: '{\"request_type\": \"dashboard_suggestion\", \"message\": \"I'll create this for you.\", ...}'"
        )
    }

    SYSTEM_PROMPT_LOCAL_DE = {
        "role": "system",
        "content": (
            "Du bist ein KI-Assistent, der in Home Assistant integriert ist.\n"
            "Du kannst spezifische Daten mit diesen Befehlen abrufen:\n"
            "- get_entity_state(entity_id): Zustand einer bestimmten Entität abrufen\n"
            "- get_entities_by_domain(domain): Alle Entitäten einer Domäne abrufen\n"
            "- get_entities_by_area(area_id): Alle Entitäten eines bestimmten Bereichs abrufen\n"
            "- get_entities(area_id oder area_ids): Entitäten nach Bereich(en) abrufen - unterstützt einzelne area_id oder Liste von area_ids\n"
            "  Verwendung: get_entities(area_ids=['area1', 'area2']) für mehrere Bereiche oder get_entities(area_id='einzelner_bereich')\n"
            "- get_calendar_events(entity_id?): Kalender-Ereignisse abrufen\n"
            "- get_automations(): Alle Automatisierungen abrufen\n"
            "- get_weather_data(): Aktuelle Wetter- und Vorhersagedaten abrufen\n"
            "- get_entity_registry(): Entitäts-Registry-Einträge abrufen\n"
            "- get_device_registry(): Geräte-Registry-Einträge abrufen\n"
            "- get_area_registry(): Raum-/Bereichsinformationen abrufen\n"
            "- get_history(entity_id, hours): Historische Zustandsänderungen abrufen\n"
            "- get_logbook_entries(hours): Aktuelle Ereignisse abrufen\n"
            "- get_person_data(): Personenverfolgungsinformationen abrufen\n"
            "- get_statistics(entity_id): Sensor-Statistiken abrufen\n"
            "- get_scenes(): Szenen-Konfigurationen abrufen\n"
            "- get_dashboards(): Liste aller Dashboards abrufen\n"
            "- get_dashboard_config(dashboard_url): Konfiguration eines bestimmten Dashboards abrufen\n"
            "- set_entity_state(entity_id, state, attributes?): Zustand einer Entität setzen (z.B. Lichter ein-/ausschalten, Rollläden öffnen/schließen)\n"
            "- call_service(domain, service, target?, service_data?): Jeden Home Assistant Service direkt aufrufen\n"
            "- create_automation(automation): Neue Automatisierung mit bereitgestellter Konfiguration erstellen\n"
            "- create_dashboard(dashboard_config): Neues Dashboard mit bereitgestellter Konfiguration erstellen\n"
            "- update_dashboard(dashboard_url, dashboard_config): Bestehende Dashboard-Konfiguration aktualisieren\n\n"
            "Du kannst auch Dashboards erstellen, wenn Benutzer danach fragen. Beim Erstellen von Dashboards:\n"
            "1. Sammle zuerst Informationen über verfügbare Entitäten, Bereiche und Geräte\n"
            "2. Stelle Nachfragen, wenn die Anforderungen des Benutzers unklar sind\n"
            "3. Erstelle eine Dashboard-Konfiguration mit passenden Karten und Ansichten\n"
            "4. Verwende gängige Kartentypen wie: entities, glance, picture-entity, weather-forecast, thermostat, media-control, usw.\n"
            "5. Organisiere Karten logisch nach Räumen, Gerätetypen oder Funktionalität\n"
            "6. Füge relevante Entitäten basierend auf der Benutzeranfrage hinzu\n\n"
            "WICHTIGE BEREICH/ETAGEN-HINWEISE:\n"
            "- Wenn Benutzer nach Entitäten aus einer bestimmten Etage fragen, verwende zuerst get_area_registry()\n"
            "- Bereiche haben sowohl 'area_id' als auch 'floor_id' - das sind verschiedene Konzepte\n"
            "- Filtere Bereiche nach ihrer floor_id, um alle Bereiche einer bestimmten Etage zu finden\n"
            "- Verwende get_entities() mit area_ids Parameter, um Entitäten aus mehreren Bereichen effizient abzurufen\n"
            "- Beispiel: get_entities(area_ids=['area1', 'area2', 'area3']) für mehrere Bereiche auf einmal\n"
            "- Das ist effizienter als mehrfaches Aufrufen von get_entities_by_area()\n\n"
            "Du kannst auch Automatisierungen erstellen, wenn Benutzer danach fragen. Wenn du erkennst, dass ein Benutzer eine Automatisierung erstellen möchte, stelle sicher, dass du zuerst Entitäten abrufst, damit du die Entitäts-IDs für die Trigger kennst. Beachte, dass wenn du bestimmte Tage in der Automatisierung festlegen möchtest, du diese Tage verwenden solltest: ['fri', 'mon', 'sat', 'sun', 'thu', 'tue', 'wed']\n"
            "Antworte mit einem JSON-Objekt in diesem Format:\n"
            "{\n"
            "  \"request_type\": \"automation_suggestion\",\n"
            "  \"message\": \"Ich habe eine Automatisierung erstellt, die dir helfen könnte. Soll ich sie erstellen?\",\n"
            "  \"automation\": {\n"
            "    \"alias\": \"Name der Automatisierung\",\n"
            "    \"description\": \"Beschreibung was die Automatisierung macht\",\n"
            "    \"trigger\": [...],  // Array von Trigger-Bedingungen\n"
            "    \"condition\": [...], // Optionales Array von Bedingungen\n"
            "    \"action\": [...]     // Array von auszuführenden Aktionen\n"
            "  }\n"
            "}\n\n"
            "Für Dashboard-Erstellungsanfragen verwende exakt dieses JSON-Format:\n"
            "{\n"
            "  \"request_type\": \"dashboard_suggestion\",\n"
            "  \"message\": \"Ich habe eine Dashboard-Konfiguration für dich erstellt. Soll ich sie erstellen?\",\n"
            "  \"dashboard\": {\n"
            "    \"title\": \"Dashboard-Titel\",\n"
            "    \"url_path\": \"dashboard-url-pfad\",\n"
            "    \"icon\": \"mdi:icon-name\",\n"
            "    \"show_in_sidebar\": true,\n"
            "    \"views\": [{\n"
            "      \"title\": \"Ansichts-Titel\",\n"
            "      \"cards\": [...] // Array von Kartenkonfigurationen\n"
            "    }]\n"
            "  }\n"
            "}\n\n"
            "Für Datenanfragen verwende exakt dieses JSON-Format:\n"
            "{\n"
            "  \"request_type\": \"data_request\",\n"
            "  \"request\": \"befehl_name\",\n"
            "  \"parameters\": {...}\n"
            "}\n"
            "Für get_entities mit mehreren Bereichen: {\"request_type\": \"get_entities\", \"parameters\": {\"area_ids\": [\"area1\", \"area2\"]}}\n"
            "Für get_entities mit einzelnem Bereich: {\"request_type\": \"get_entities\", \"parameters\": {\"area_id\": \"einzelner_bereich\"}}\n\n"
            "Für Service-Aufrufe verwende exakt dieses JSON-Format:\n"
            "{\n"
            "  \"request_type\": \"call_service\",\n"
            "  \"domain\": \"light\",\n"
            "  \"service\": \"turn_on\",\n"
            "  \"target\": {\"entity_id\": [\"entity1\", \"entity2\"]},\n"
            "  \"service_data\": {\"brightness\": 255}\n"
            "}\n\n"
            "Wenn du alle benötigten Daten hast, antworte mit exakt diesem JSON-Format:\n"
            "{\n"
            "  \"request_type\": \"final_response\",\n"
            "  \"response\": \"deine Antwort an den Benutzer\"\n"
            "}\n\n"
            "KRITISCHE FORMATIERUNGSREGELN:\n"
            "- Du MUSST IMMER mit NUR einem gültigen JSON-Objekt antworten\n"
            "- Füge KEINEN Text vor dem JSON hinzu\n"
            "- Füge KEINEN Text nach dem JSON hinzu\n"
            "- Füge KEINE Erklärungen oder Beschreibungen außerhalb des JSON hinzu\n"
            "- Deine gesamte Antwort muss als JSON parsbar sein\n"
            "- Verwende das 'message'-Feld innerhalb des JSON für benutzerzugewandten Text\n"
            "- Mische NIEMALS normalen Text mit JSON in deiner Antwort\n\n"
            "FALSCH: 'Ich erstelle das für dich. {\"request_type\": ...}'\n"
            "RICHTIG: '{\"request_type\": \"dashboard_suggestion\", \"message\": \"Ich erstelle das für dich.\", ...}'"
        )
    }

    def __init__(self, hass: HomeAssistant, config: Dict[str, Any]):
        """Initialize the agent with provider selection."""
        self.hass = hass
        self.config = config
        self.conversation_history = []
        self._cache = {}
        self._cache_timeout = 300  # 5 minutes
        
        # Get provider and its configuration
        provider = config.get("ai_provider", "openai")
        self.provider_config = PROVIDER_CONFIGS.get(provider, PROVIDER_CONFIGS["openai"])
        
        # Initialize with provider-specific settings
        self._max_retries = self.provider_config.get("max_retries", 10)
        self._base_delay = self.provider_config.get("base_delay", 1)
        self._max_delay = self.provider_config.get("max_delay", 60)
        self._rate_limit = 60  # Keep existing simple rate limit as fallback
        self._last_request_time = 0
        self._request_count = 0
        self._request_window_start = time.time()
        
        models_config = config.get("models", {})
        language = config.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        
        _LOGGER.debug("Initializing AiAgentHaAgent with provider: %s, language: %s", provider, language)
        _LOGGER.debug("Models config loaded: %s", models_config)
        _LOGGER.debug("Provider config: %s", self.provider_config)
        
        # Set the appropriate system prompt based on provider and language
        if provider == "local":
            if language == "de":
                self.system_prompt = self.SYSTEM_PROMPT_LOCAL_DE
                _LOGGER.debug("Using German local-optimized system prompt")
            else:
                self.system_prompt = self.SYSTEM_PROMPT_LOCAL
                _LOGGER.debug("Using English local-optimized system prompt")
        else:
            if language == "de":
                self.system_prompt = self.SYSTEM_PROMPT_DE
                _LOGGER.debug("Using German standard system prompt")
            else:
                self.system_prompt = self.SYSTEM_PROMPT
                _LOGGER.debug("Using English standard system prompt")
        
        # Initialize the appropriate AI client with model selection
        if provider == "openai":
            model = models_config.get("openai", "gpt-3.5-turbo")
            self.ai_client = OpenAIClient(config.get("openai_token"), model)
            self.model = model
        elif provider == "gemini":
            model = models_config.get("gemini", "gemini-1.5-flash")
            self.ai_client = GeminiClient(config.get("gemini_token"), model)
            self.model = model
        elif provider == "openrouter":
            model = models_config.get("openrouter", "openai/gpt-4o")
            self.ai_client = OpenRouterClient(config.get("openrouter_token"), model)
            self.model = model
        elif provider == "anthropic":
            model = models_config.get("anthropic", "claude-3-5-sonnet-20241022")
            self.ai_client = AnthropicClient(config.get("anthropic_token"), model)
            self.model = model
        elif provider == "local":
            model = models_config.get("local", "")
            url = config.get("local_url")
            if not url:
                _LOGGER.error("Missing local_url for local provider")
                raise Exception("Missing local_url configuration for local provider")
            self.ai_client = LocalClient(url, model)
            self.model = model
        else:  # default to llama if somehow specified
            model = models_config.get("llama", "Llama-4-Maverick-17B-128E-Instruct-FP8")
            self.ai_client = LlamaClient(config.get("llama_token"), model)
            self.model = model
        
        _LOGGER.debug("AiAgentHaAgent initialized successfully with provider: %s, model: %s", provider, model)

    def _optimize_conversation_history(self, messages: List[Dict[str, Any]], max_tokens: int = 12000) -> List[Dict[str, Any]]:
        """Optimize conversation history based on token limits"""
        if not messages:
            return messages
        
        # Always keep system prompt
        system_msg = None
        other_messages = messages
        
        if messages and messages[0].get("role") == "system":
            system_msg = messages[0]
            other_messages = messages[1:]
        
        if not other_messages:
            return messages
        
        # Count tokens for system message
        current_tokens = estimate_message_tokens(system_msg) if system_msg else 0
        
        # Add messages from newest to oldest until we hit the limit
        selected_messages = []
        for msg in reversed(other_messages):
            msg_tokens = estimate_message_tokens(msg)
            if current_tokens + msg_tokens > max_tokens:
                break
            selected_messages.insert(0, msg)
            current_tokens += msg_tokens
        
        # Rebuild messages list
        result = []
        if system_msg:
            result.append(system_msg)
        result.extend(selected_messages)
        
        if len(result) < len(messages):
            _LOGGER.debug("Optimized conversation history: %d -> %d messages, ~%d tokens", 
                         len(messages), len(result), current_tokens)
        
        return result

    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits."""
        current_time = time.time()
        if current_time - self._request_window_start >= 60:
            self._request_count = 0
            self._request_window_start = current_time
        
        if self._request_count >= self._rate_limit:
            return False
        
        self._request_count += 1
        return True

    def _validate_api_key(self) -> bool:
        """Validate the API key format."""
        provider = self.config.get("ai_provider", "openai")
        
        if provider == "openai":
            token = self.config.get("openai_token")
        elif provider == "gemini":
            token = self.config.get("gemini_token")
        elif provider == "openrouter":
            token = self.config.get("openrouter_token")
        elif provider == "anthropic":
            token = self.config.get("anthropic_token")
        elif provider == "local":
            token = self.config.get("local_url")
        else:
            token = self.config.get("llama_token")
        
        if not token or not isinstance(token, str):
            return False
        
        # For local provider, validate URL format
        if provider == "local":
            return token.startswith(("http://", "https://"))
        
        # Add more specific validation based on your API key format
        return len(token) >= 32

    def _get_cached_data(self, key: str) -> Optional[Any]:
        """Get data from cache if it's still valid."""
        if key in self._cache:
            timestamp, data = self._cache[key]
            if time.time() - timestamp < self._cache_timeout:
                return data
            del self._cache[key]
        return None

    def _set_cached_data(self, key: str, data: Any) -> None:
        """Store data in cache with timestamp."""
        self._cache[key] = (time.time(), data)

    def _sanitize_automation_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize automation configuration to prevent injection attacks."""
        sanitized = {}
        for key, value in config.items():
            if key in ['alias', 'description']:
                # Sanitize strings
                sanitized[key] = str(value).strip()[:100]  # Limit length
            elif key in ['trigger', 'condition', 'action']:
                # Validate arrays
                if isinstance(value, list):
                    sanitized[key] = value
            elif key == 'mode':
                # Validate mode
                if value in ['single', 'restart', 'queued', 'parallel']:
                    sanitized[key] = value
        return sanitized

    async def get_entity_state(self, entity_id: str) -> Dict[str, Any]:
        """Get the state of a specific entity."""
        try:
            _LOGGER.debug("Requesting entity state for: %s", entity_id)
            state = self.hass.states.get(entity_id)
            if not state:
                _LOGGER.warning("Entity not found: %s", entity_id)
                return {"error": f"Entity {entity_id} not found"}
            
            result = {
                "entity_id": state.entity_id,
                "state": state.state,
                "last_changed": state.last_changed.isoformat() if state.last_changed else None,
                "friendly_name": state.attributes.get("friendly_name"),
                "attributes": {k: (v.isoformat() if hasattr(v, 'isoformat') else v) 
                            for k, v in state.attributes.items()}
            }
            _LOGGER.debug("Retrieved entity state: %s", json.dumps(result))
            return result
        except Exception as e:
            _LOGGER.exception("Error getting entity state: %s", str(e))
            return {"error": f"Error getting entity state: {str(e)}"}

    async def get_entities_by_domain(self, domain: str) -> List[Dict[str, Any]]:
        """Get all entities for a specific domain."""
        try:
            _LOGGER.debug("Requesting all entities for domain: %s", domain)
            states = [state for state in self.hass.states.async_all() 
                    if state.entity_id.startswith(f"{domain}.")]
            _LOGGER.debug("Found %d entities in domain %s", len(states), domain)
            return [await self.get_entity_state(state.entity_id) for state in states]
        except Exception as e:
            _LOGGER.exception("Error getting entities by domain: %s", str(e))
            return [{"error": f"Error getting entities for domain {domain}: {str(e)}"}]

    async def get_entities_by_area(self, area_id: str) -> List[Dict[str, Any]]:
        """Get all entities for a specific area."""
        try:
            _LOGGER.debug("Requesting all entities for area: %s", area_id)
            
            # Get entity registry to find entities assigned to the area
            from homeassistant.helpers import entity_registry as er
            from homeassistant.helpers import device_registry as dr
            
            entity_registry = er.async_get(self.hass)
            device_registry = dr.async_get(self.hass)
            
            entities_in_area = []
            
            # Find entities assigned to the area (directly or through their device)
            for entity in entity_registry.entities.values():
                # Check if entity is directly assigned to the area
                if entity.area_id == area_id:
                    entities_in_area.append(entity.entity_id)
                # Check if entity's device is assigned to the area
                elif entity.device_id:
                    device = device_registry.devices.get(entity.device_id)
                    if device and device.area_id == area_id:
                        entities_in_area.append(entity.entity_id)
            
            _LOGGER.debug("Found %d entities in area %s", len(entities_in_area), area_id)
            
            # Get state information for each entity
            result = []
            for entity_id in entities_in_area:
                state_info = await self.get_entity_state(entity_id)
                if not state_info.get("error"):  # Only include entities that exist
                    result.append(state_info)
            
            return result
            
        except Exception as e:
            _LOGGER.exception("Error getting entities by area: %s", str(e))
            return [{"error": f"Error getting entities for area {area_id}: {str(e)}"}]

    async def get_entities(self, area_id=None, area_ids=None) -> List[Dict[str, Any]]:
        """Get entities by area(s) - flexible method that supports single area or multiple areas."""
        try:
            # Handle different parameter formats
            areas_to_process = []
            
            if area_ids:
                # Multiple areas provided
                if isinstance(area_ids, list):
                    areas_to_process = area_ids
                else:
                    areas_to_process = [area_ids]
            elif area_id:
                # Single area provided
                if isinstance(area_id, list):
                    areas_to_process = area_id
                else:
                    areas_to_process = [area_id]
            else:
                return [{"error": "No area_id or area_ids provided"}]
            
            _LOGGER.debug("Requesting entities for areas: %s", areas_to_process)
            
            all_entities = []
            for area in areas_to_process:
                entities_in_area = await self.get_entities_by_area(area)
                all_entities.extend(entities_in_area)
            
            # Remove duplicates based on entity_id
            seen_entities = set()
            unique_entities = []
            for entity in all_entities:
                if isinstance(entity, dict) and "entity_id" in entity:
                    if entity["entity_id"] not in seen_entities:
                        seen_entities.add(entity["entity_id"])
                        unique_entities.append(entity)
                else:
                    unique_entities.append(entity)  # Keep error messages
            
            _LOGGER.debug("Found %d unique entities across %d areas", len(unique_entities), len(areas_to_process))
            return unique_entities
            
        except Exception as e:
            _LOGGER.exception("Error getting entities: %s", str(e))
            return [{"error": f"Error getting entities: {str(e)}"}]

    async def get_calendar_events(self, entity_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get calendar events, optionally filtered by entity_id."""
        try:
            if entity_id:
                _LOGGER.debug("Requesting calendar events for specific entity: %s", entity_id)
                return [await self.get_entity_state(entity_id)]
            
            _LOGGER.debug("Requesting all calendar events")
            return await self.get_entities_by_domain("calendar")
        except Exception as e:
            _LOGGER.exception("Error getting calendar events: %s", str(e))
            return [{"error": f"Error getting calendar events: {str(e)}"}]

    async def get_automations(self) -> List[Dict[str, Any]]:
        """Get all automations."""
        try:
            _LOGGER.debug("Requesting all automations")
            return await self.get_entities_by_domain("automation")
        except Exception as e:
            _LOGGER.exception("Error getting automations: %s", str(e))
            return [{"error": f"Error getting automations: {str(e)}"}]

    async def get_entity_registry(self) -> List[Dict]:
        """Get entity registry entries"""
        _LOGGER.debug("Requesting all entity registry entries")
        try:
            from homeassistant.helpers import entity_registry as er
            registry = er.async_get(self.hass)
            if not registry:
                return []
            return [
                {
                    "entity_id": entry.entity_id,
                    "device_id": entry.device_id,
                    "platform": entry.platform,
                    "disabled": entry.disabled,
                    "area_id": entry.area_id,
                    "original_name": entry.original_name,
                    "unique_id": entry.unique_id
                } 
                for entry in registry.entities.values()
            ]
        except Exception as e:
            _LOGGER.exception("Error getting entity registry entries: %s", str(e))
            return [{"error": f"Error getting entity registry entries: {str(e)}"}]

    async def get_device_registry(self) -> List[Dict]:
        """Get device registry entries"""
        _LOGGER.debug("Requesting all device registry entries")
        try:
            from homeassistant.helpers import device_registry as dr
            registry = dr.async_get(self.hass)
            if not registry:
                return []
            return [
                {
                    "id": device.id,
                    "name": device.name,
                    "model": device.model,
                    "manufacturer": device.manufacturer,
                    "sw_version": device.sw_version,
                    "hw_version": device.hw_version,
                    "connections": list(device.connections) if device.connections else [],
                    "identifiers": list(device.identifiers) if device.identifiers else [],
                    "area_id": device.area_id,
                    "disabled": device.disabled_by is not None,
                    "entry_type": device.entry_type.value if device.entry_type else None,
                    "name_by_user": device.name_by_user
                }
                for device in registry.devices.values()
            ]
        except Exception as e:
            _LOGGER.exception("Error getting device registry entries: %s", str(e))
            return [{"error": f"Error getting device registry entries: {str(e)}"}]

    async def get_history(self, entity_id: str, hours: int = 24) -> List[Dict]:
        """Get historical state changes for an entity"""
        _LOGGER.debug("Requesting historical state changes for entity: %s", entity_id)
        try:
            from homeassistant.components import history
            now = dt_util.utcnow()
            start = now - timedelta(hours=hours)
            
            # Get history using the history component
            history_data = await self.hass.async_add_executor_job(
                history.get_significant_states,
                self.hass, start, now, [entity_id]
            )
            
            # Convert to serializable format
            result = []
            for entity_id_key, states in history_data.items():
                for state in states:
                    result.append({
                        "entity_id": state.entity_id,
                        "state": state.state,
                        "last_changed": state.last_changed.isoformat(),
                        "last_updated": state.last_updated.isoformat(),
                        "attributes": dict(state.attributes)
                    })
            return result
        except Exception as e:
            _LOGGER.exception("Error getting history: %s", str(e))
            return [{"error": f"Error getting history: {str(e)}"}]
    
    async def get_logbook_entries(self, hours: int = 24) -> List[Dict]:
        """Get recent logbook entries"""
        _LOGGER.debug("Requesting recent logbook entries")
        try:
            from homeassistant.components import logbook
            now = dt_util.utcnow()
            start = now - timedelta(hours=hours)
            
            # Get logbook entries
            entries = await self.hass.async_add_executor_job(
                logbook.get_events, self.hass, start, now
            )
            
            # Convert to serializable format
            result = []
            for entry in entries:
                result.append({
                    "when": entry.get("when"),
                    "name": entry.get("name"),
                    "message": entry.get("message"),
                    "entity_id": entry.get("entity_id"),
                    "state": entry.get("state"),
                    "domain": entry.get("domain")
                })
            return result
        except Exception as e:
            _LOGGER.exception("Error getting logbook entries: %s", str(e))
            return [{"error": f"Error getting logbook entries: {str(e)}"}]

    async def get_area_registry(self) -> Dict[str, Dict]:
        """Get area registry information"""
        _LOGGER.debug("Get area registry information")
        try:
            from homeassistant.helpers import area_registry as ar
            registry = ar.async_get(self.hass)
            if not registry:
                return {}
            
            result = {}
            for area in registry.areas.values():
                result[area.id] = {
                    "name": area.name,
                    "normalized_name": area.normalized_name,
                    "picture": area.picture,
                    "icon": area.icon,
                    "floor_id": area.floor_id,
                    "labels": list(area.labels) if area.labels else []
                }
            return result
        except Exception as e:
            _LOGGER.exception("Error getting area registry: %s", str(e))
            return {"error": f"Error getting area registry: {str(e)}"}
        
    async def get_person_data(self) -> List[Dict]:
        """Get person tracking information"""
        _LOGGER.debug("Requesting person tracking information")
        try:
            result = []
            for state in self.hass.states.async_all("person"):
                result.append({
                    "entity_id": state.entity_id,
                    "name": state.attributes.get("friendly_name", state.entity_id),
                    "state": state.state,
                    "latitude": state.attributes.get("latitude"),
                    "longitude": state.attributes.get("longitude"),
                    "source": state.attributes.get("source"),
                    "gps_accuracy": state.attributes.get("gps_accuracy"),
                    "last_changed": state.last_changed.isoformat() if state.last_changed else None
                })
            return result
        except Exception as e:
            _LOGGER.exception("Error getting person tracking information: %s", str(e))
            return [{"error": f"Error getting person tracking information: {str(e)}"}]
    
    async def get_statistics(self, entity_id: str) -> Dict:
        """Get statistics for an entity"""
        _LOGGER.debug("Requesting statistics for entity: %s", entity_id)
        try:
            from homeassistant.components import recorder
            # Check if recorder is available
            if not self.hass.data.get(recorder.DATA_INSTANCE):
                return {"error": "Recorder component is not available"}
                
            # from homeassistant.components.recorder.statistics import get_latest_short_term_statistics
            import homeassistant.components.recorder.statistics as stats_module

            # Get latest statistics
            stats = await self.hass.async_add_executor_job(
                # get_latest_short_term_statistics,
                stats_module.get_last_short_term_statistics,
                self.hass, 1, entity_id, True, set()
            )
            
            if entity_id in stats:
                stat_data = stats[entity_id][0] if stats[entity_id] else {}
                return {
                    "entity_id": entity_id,
                    "start": stat_data.get("start"),
                    "mean": stat_data.get("mean"),
                    "min": stat_data.get("min"),
                    "max": stat_data.get("max"),
                    "last_reset": stat_data.get("last_reset"),
                    "state": stat_data.get("state"),
                    "sum": stat_data.get("sum")
                }
            else:
                return {"error": f"No statistics available for entity {entity_id}"}
        except Exception as e:
            _LOGGER.exception("Error getting statistics: %s", str(e))
            return {"error": f"Error getting statistics: {str(e)}"}
        
    async def get_scenes(self) -> List[Dict]:
        """Get scene configurations"""
        _LOGGER.debug("Requesting scene configurations")
        try:
            result = []
            for state in self.hass.states.async_all("scene"):
                result.append({
                    "entity_id": state.entity_id,
                    "name": state.attributes.get("friendly_name", state.entity_id),
                    "last_activated": state.attributes.get("last_activated"),
                    "icon": state.attributes.get("icon"),
                    "last_changed": state.last_changed.isoformat() if state.last_changed else None
                })
            return result
        except Exception as e:
            _LOGGER.exception("Error getting scene configurations: %s", str(e))
            return [{"error": f"Error getting scene configurations: {str(e)}"}]
        
    async def get_weather_data(self) -> Dict[str, Any]:
        """Get weather data from any available weather entity in the system."""
        try:
            # Find all weather entities
            weather_entities = [
                state for state in self.hass.states.async_all()
                if state.domain == "weather"
            ]
            
            if not weather_entities:
                return {
                    "error": "No weather entities found in the system. Please add a weather integration."
                }
            
            # Use the first available weather entity
            state = weather_entities[0]
            _LOGGER.debug("Using weather entity: %s", state.entity_id)
            
            # Get all available attributes
            all_attributes = state.attributes
            _LOGGER.debug("Available weather attributes: %s", json.dumps(all_attributes))
            
            # Get forecast data
            forecast = all_attributes.get("forecast", [])
            
            # Process forecast data
            processed_forecast = []
            for day in forecast:
                forecast_entry = {
                    "datetime": day.get("datetime"),
                    "temperature": day.get("temperature"),
                    "condition": day.get("condition"),
                    "precipitation": day.get("precipitation"),
                    "precipitation_probability": day.get("precipitation_probability"),
                    "humidity": day.get("humidity"),
                    "wind_speed": day.get("wind_speed"),
                    "wind_bearing": day.get("wind_bearing")
                }
                # Only add entries that have at least some data
                if any(v is not None for v in forecast_entry.values()):
                    processed_forecast.append(forecast_entry)
            
            # Get current weather data
            current = {
                "entity_id": state.entity_id,
                "temperature": all_attributes.get("temperature"),
                "humidity": all_attributes.get("humidity"),
                "pressure": all_attributes.get("pressure"),
                "wind_speed": all_attributes.get("wind_speed"),
                "wind_bearing": all_attributes.get("wind_bearing"),
                "condition": state.state,
                "forecast_available": len(processed_forecast) > 0
            }
            
            # Log the processed data for debugging
            _LOGGER.debug("Processed weather data: %s", json.dumps({
                "current": current,
                "forecast_count": len(processed_forecast)
            }))
            
            return {
                "current": current,
                "forecast": processed_forecast
            }
        except Exception as e:
            _LOGGER.exception("Error getting weather data: %s", str(e))
            return {
                "error": f"Error getting weather data: {str(e)}"
            }

    async def create_automation(self, automation_config: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new automation with validation and sanitization."""
        try:
            _LOGGER.debug("Creating automation with config: %s", json.dumps(automation_config))
            
            # Validate required fields
            if not all(key in automation_config for key in ['alias', 'trigger', 'action']):
                return {
                    "error": "Missing required fields in automation configuration"
                }
            
            # Sanitize configuration
            sanitized_config = self._sanitize_automation_config(automation_config)
            
            # Generate a unique ID for the automation
            automation_id = f"ai_agent_auto_{int(time.time() * 1000)}"
            
            # Create the automation entry
            automation_entry = {
                'id': automation_id,
                'alias': sanitized_config['alias'],
                'description': sanitized_config.get('description', ''),
                'trigger': sanitized_config['trigger'],
                'condition': sanitized_config.get('condition', []),
                'action': sanitized_config['action'],
                'mode': sanitized_config.get('mode', 'single')
            }
            
            # Read current automations.yaml using async executor
            automations_path = self.hass.config.path('automations.yaml')
            try:
                current_automations = await self.hass.async_add_executor_job(
                    lambda: yaml.safe_load(open(automations_path, 'r')) or []
                )
            except FileNotFoundError:
                current_automations = []
            
            # Check for duplicate automation names
            if any(auto.get('alias') == automation_entry['alias'] for auto in current_automations):
                return {
                    "error": f"An automation with the name '{automation_entry['alias']}' already exists"
                }
            
            # Append new automation
            current_automations.append(automation_entry)
            
            # Write back to file using async executor
            await self.hass.async_add_executor_job(
                lambda: yaml.dump(current_automations, open(automations_path, 'w'), default_flow_style=False)
            )
            
            # Reload automations
            await self.hass.services.async_call('automation', 'reload')
            
            # Clear automation-related caches
            self._cache.clear()
            
            return {
                "success": True,
                "message": f"Automation '{automation_entry['alias']}' created successfully"
            }
            
        except Exception as e:
            _LOGGER.exception("Error creating automation: %s", str(e))
            return {
                "error": f"Error creating automation: {str(e)}"
            }

    async def get_dashboards(self) -> List[Dict[str, Any]]:
        """Get list of all dashboards."""
        try:
            _LOGGER.debug("Requesting all dashboards")
            
            # Get dashboards via WebSocket API
            ws_api = self.hass.data.get("websocket_api")
            if not ws_api:
                return {"error": "WebSocket API not available"}
            
            # Use the lovelace service to get dashboards
            try:
                from homeassistant.components.lovelace import DOMAIN as LOVELACE_DOMAIN
                from homeassistant.components.lovelace import CONF_DASHBOARDS
                
                # Get lovelace config
                lovelace_config = self.hass.data.get(LOVELACE_DOMAIN, {})
                dashboards = lovelace_config.get(CONF_DASHBOARDS, {})
                
                dashboard_list = []
                
                # Add default dashboard
                dashboard_list.append({
                    "url_path": None,
                    "title": "Overview",
                    "icon": "mdi:home",
                    "show_in_sidebar": True,
                    "require_admin": False
                })
                
                # Add custom dashboards
                for url_path, config in dashboards.items():
                    dashboard_list.append({
                        "url_path": url_path,
                        "title": config.get("title", url_path),
                        "icon": config.get("icon", "mdi:view-dashboard"),
                        "show_in_sidebar": config.get("show_in_sidebar", True),
                        "require_admin": config.get("require_admin", False)
                    })
                
                _LOGGER.debug("Found %d dashboards", len(dashboard_list))
                return dashboard_list
                
            except Exception as e:
                _LOGGER.warning("Could not get dashboards via lovelace: %s", str(e))
                return [{"error": f"Could not retrieve dashboards: {str(e)}"}]
                
        except Exception as e:
            _LOGGER.exception("Error getting dashboards: %s", str(e))
            return [{"error": f"Error getting dashboards: {str(e)}"}]

    async def get_dashboard_config(self, dashboard_url: Optional[str] = None) -> Dict[str, Any]:
        """Get configuration of a specific dashboard."""
        try:
            _LOGGER.debug("Requesting dashboard config for: %s", dashboard_url or "default")
            
            # Import the websocket handler
            from homeassistant.components.websocket_api import require_admin
            from homeassistant.components.lovelace import websocket_api as lovelace_ws
            
            # Create a mock websocket connection for internal use
            class MockConnection:
                def __init__(self, hass):
                    self.hass = hass
                    self.user = None
                    
                def send_message(self, message):
                    pass
            
            # Get dashboard configuration
            try:
                from homeassistant.components.lovelace import DOMAIN as LOVELACE_DOMAIN
                from homeassistant.components.lovelace.dashboard import LovelaceDashboard
                
                # Get the dashboard
                lovelace_config = self.hass.data.get(LOVELACE_DOMAIN, {})
                
                if dashboard_url is None:
                    # Get default dashboard
                    dashboard = lovelace_config.get("default_dashboard")
                    if dashboard:
                        config = await dashboard.async_get_info()
                        return config
                    else:
                        return {"error": "Default dashboard not found"}
                else:
                    # Get custom dashboard
                    dashboards = lovelace_config.get("dashboards", {})
                    if dashboard_url in dashboards:
                        dashboard = dashboards[dashboard_url]
                        config = await dashboard.async_get_info()
                        return config
                    else:
                        return {"error": f"Dashboard '{dashboard_url}' not found"}
                
            except Exception as e:
                _LOGGER.warning("Could not get dashboard config: %s", str(e))
                return {"error": f"Could not retrieve dashboard config: {str(e)}"}
                
        except Exception as e:
            _LOGGER.exception("Error getting dashboard config: %s", str(e))
            return {"error": f"Error getting dashboard config: {str(e)}"}

    async def create_dashboard(self, dashboard_config: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new dashboard using Home Assistant's Lovelace WebSocket API."""
        try:
            _LOGGER.debug("Creating dashboard with config: %s", json.dumps(dashboard_config, default=str))
            
            # Validate required fields
            if not dashboard_config.get("title"):
                return {"error": "Dashboard title is required"}
            
            if not dashboard_config.get("url_path"):
                return {"error": "Dashboard URL path is required"}
            
            # Sanitize the URL path
            url_path = dashboard_config["url_path"].lower().replace(" ", "-").replace("_", "-")
            
            # Prepare dashboard configuration for Lovelace
            dashboard_data = {
                "title": dashboard_config["title"],
                "icon": dashboard_config.get("icon", "mdi:view-dashboard"),
                "show_in_sidebar": dashboard_config.get("show_in_sidebar", True),
                "require_admin": dashboard_config.get("require_admin", False),
                "views": dashboard_config.get("views", [])
            }
            
            try:
                # Create dashboard file directly - this is the most reliable method
                import os
                import yaml
                
                # Create the dashboard YAML file
                lovelace_config_file = self.hass.config.path(f"ui-lovelace-{url_path}.yaml")
                
                # Use async_add_executor_job to perform file I/O asynchronously
                def write_dashboard_file():
                    with open(lovelace_config_file, 'w') as f:
                        yaml.dump(dashboard_data, f, default_flow_style=False, allow_unicode=True)
                
                await self.hass.async_add_executor_job(write_dashboard_file)
                
                _LOGGER.info("Successfully created dashboard file: %s", lovelace_config_file)
                
                # Now update configuration.yaml
                try:
                    config_file = self.hass.config.path('configuration.yaml')
                    dashboard_config_entry = {
                        url_path: {
                            'mode': 'yaml',
                            'title': dashboard_config['title'],
                            'icon': dashboard_config.get('icon', 'mdi:view-dashboard'),
                            'show_in_sidebar': dashboard_config.get('show_in_sidebar', True),
                            'filename': f'ui-lovelace-{url_path}.yaml'
                        }
                    }
                    
                    def update_config_file():
                        try:
                            with open(config_file, 'r') as f:
                                content = f.read()
                            
                            # Dashboard configuration to add
                            dashboard_yaml = f"""    {url_path}:
      mode: yaml
      title: {dashboard_config['title']}
      icon: {dashboard_config.get('icon', 'mdi:view-dashboard')}
      show_in_sidebar: {str(dashboard_config.get('show_in_sidebar', True)).lower()}
      filename: ui-lovelace-{url_path}.yaml"""
                            
                            # Check if lovelace section exists
                            if 'lovelace:' not in content:
                                # Add complete lovelace section at the end
                                lovelace_section = f"""
# Lovelace dashboards configuration added by AI Agent
lovelace:
  dashboards:
{dashboard_yaml}
"""
                                with open(config_file, 'a') as f:
                                    f.write(lovelace_section)
                                return True
                            
                            # If lovelace exists, check for dashboards section
                            lines = content.split('\n')
                            new_lines = []
                            dashboard_added = False
                            in_lovelace = False
                            lovelace_indent = 0
                            
                            for i, line in enumerate(lines):
                                new_lines.append(line)
                                
                                # Detect lovelace section
                                if line.strip() == 'lovelace:' or line.strip().startswith('lovelace:'):
                                    in_lovelace = True
                                    lovelace_indent = len(line) - len(line.lstrip())
                                    continue
                                
                                # If we're in lovelace section
                                if in_lovelace:
                                    current_indent = len(line) - len(line.lstrip()) if line.strip() else 0
                                    
                                    # If we hit another top-level section, we're out of lovelace
                                    if line.strip() and current_indent <= lovelace_indent and not line.startswith(' '):
                                        if line.strip() != 'lovelace:':
                                            in_lovelace = False
                                    
                                    # Look for dashboards section
                                    if in_lovelace and 'dashboards:' in line:
                                        # Add our dashboard after the dashboards: line
                                        new_lines.append(dashboard_yaml)
                                        dashboard_added = True
                                        in_lovelace = False  # We're done
                                        break
                            
                            # If we found lovelace but no dashboards section, add it
                            if not dashboard_added and 'lovelace:' in content:
                                # Find lovelace section and add dashboards
                                new_lines = []
                                for line in lines:
                                    new_lines.append(line)
                                    if line.strip() == 'lovelace:' or line.strip().startswith('lovelace:'):
                                        # Add dashboards section right after lovelace
                                        new_lines.append('  dashboards:')
                                        new_lines.append(dashboard_yaml)
                                        dashboard_added = True
                                        break
                            
                            if dashboard_added:
                                with open(config_file, 'w') as f:
                                    f.write('\n'.join(new_lines))
                                return True
                            else:
                                # Last resort: append to end of file
                                with open(config_file, 'a') as f:
                                    f.write(f'\n  dashboards:\n{dashboard_yaml}\n')
                                return True
                            
                        except Exception as e:
                            _LOGGER.error("Failed to update configuration.yaml: %s", str(e))
                            # Fallback to simple append method
                            try:
                                with open(config_file, 'r') as f:
                                    content = f.read()
                                
                                # Check if lovelace section exists
                                if 'lovelace:' not in content:
                                    # Add lovelace section
                                    lovelace_config = f"""
# Lovelace dashboards
lovelace:
  dashboards:
    {url_path}:
      mode: yaml
      title: {dashboard_config['title']}
      icon: {dashboard_config.get('icon', 'mdi:view-dashboard')}
      show_in_sidebar: {str(dashboard_config.get('show_in_sidebar', True)).lower()}
      filename: ui-lovelace-{url_path}.yaml
"""
                                    with open(config_file, 'a') as f:
                                        f.write(lovelace_config)
                                else:
                                    # Add to existing lovelace section (simple approach)
                                    dashboard_entry = f"""    {url_path}:
      mode: yaml
      title: {dashboard_config['title']}
      icon: {dashboard_config.get('icon', 'mdi:view-dashboard')}
      show_in_sidebar: {str(dashboard_config.get('show_in_sidebar', True)).lower()}
      filename: ui-lovelace-{url_path}.yaml
"""
                                    # Find the dashboards section and add to it
                                    lines = content.split('\n')
                                    new_lines = []
                                    in_dashboards = False
                                    dashboards_indented = False
                                    
                                    for line in lines:
                                        new_lines.append(line)
                                        if 'dashboards:' in line and 'lovelace' in content[:content.find(line)]:
                                            in_dashboards = True
                                            # Add our dashboard entry after dashboards:
                                            new_lines.append(dashboard_entry.rstrip())
                                            in_dashboards = False
                                    
                                    # If we couldn't find dashboards section, add it under lovelace
                                    if not any('dashboards:' in line for line in lines):
                                        for i, line in enumerate(new_lines):
                                            if line.strip() == 'lovelace:':
                                                new_lines.insert(i + 1, '  dashboards:')
                                                new_lines.insert(i + 2, dashboard_entry.rstrip())
                                                break
                                    
                                    with open(config_file, 'w') as f:
                                        f.write('\n'.join(new_lines))
                                
                                return True
                            except Exception as fallback_error:
                                _LOGGER.error("Fallback config update also failed: %s", str(fallback_error))
                                return False
                    
                    config_updated = await self.hass.async_add_executor_job(update_config_file)
                    
                    if config_updated:
                        success_message = f"""Dashboard '{dashboard_config['title']}' created successfully!

✅ Dashboard file created: ui-lovelace-{url_path}.yaml
✅ Configuration.yaml updated automatically

🔄 Please restart Home Assistant to see your new dashboard in the sidebar."""
                        
                        return {
                            "success": True,
                            "message": success_message,
                            "url_path": url_path,
                            "restart_required": True
                        }
                    else:
                        # Config update failed, provide manual instructions
                        config_instructions = f"""Dashboard '{dashboard_config['title']}' created successfully!

✅ Dashboard file created: ui-lovelace-{url_path}.yaml
⚠️  Could not automatically update configuration.yaml

Please manually add this to your configuration.yaml:

lovelace:
  dashboards:
    {url_path}:
      mode: yaml
      title: {dashboard_config['title']}
      icon: {dashboard_config.get('icon', 'mdi:view-dashboard')}
      show_in_sidebar: {str(dashboard_config.get('show_in_sidebar', True)).lower()}
      filename: ui-lovelace-{url_path}.yaml

Then restart Home Assistant to see your new dashboard in the sidebar."""
                        
                        return {
                            "success": True,
                            "message": config_instructions,
                            "url_path": url_path,
                            "restart_required": True
                        }
                        
                except Exception as config_error:
                    _LOGGER.error("Error updating configuration.yaml: %s", str(config_error))
                    # Provide manual instructions as fallback
                    config_instructions = f"""Dashboard '{dashboard_config['title']}' created successfully!

✅ Dashboard file created: ui-lovelace-{url_path}.yaml
⚠️  Could not automatically update configuration.yaml

Please manually add this to your configuration.yaml:

lovelace:
  dashboards:
    {url_path}:
      mode: yaml
      title: {dashboard_config['title']}
      icon: {dashboard_config.get('icon', 'mdi:view-dashboard')}
      show_in_sidebar: {str(dashboard_config.get('show_in_sidebar', True)).lower()}
      filename: ui-lovelace-{url_path}.yaml

Then restart Home Assistant to see your new dashboard in the sidebar."""
                    
                    return {
                        "success": True,
                        "message": config_instructions,
                        "url_path": url_path,
                        "restart_required": True
                    }
                
            except Exception as e:
                _LOGGER.error("Failed to create dashboard file: %s", str(e))
                return {"error": f"Failed to create dashboard file: {str(e)}"}
                
        except Exception as e:
            _LOGGER.exception("Error creating dashboard: %s", str(e))
            return {"error": f"Error creating dashboard: {str(e)}"}

    async def update_dashboard(self, dashboard_url: str, dashboard_config: Dict[str, Any]) -> Dict[str, Any]:
        """Update an existing dashboard using Home Assistant's Lovelace WebSocket API."""
        try:
            _LOGGER.debug("Updating dashboard %s with config: %s", dashboard_url, json.dumps(dashboard_config, default=str))
            
            # Prepare updated dashboard configuration
            dashboard_data = {
                "title": dashboard_config.get("title", "Updated Dashboard"),
                "icon": dashboard_config.get("icon", "mdi:view-dashboard"),
                "show_in_sidebar": dashboard_config.get("show_in_sidebar", True),
                "require_admin": dashboard_config.get("require_admin", False),
                "views": dashboard_config.get("views", [])
            }
            
            try:
                # Update dashboard file directly
                import yaml
                import os
                
                # Try updating the YAML file
                dashboard_file = self.hass.config.path(f"ui-lovelace-{dashboard_url}.yaml")
                
                # Check if file exists asynchronously
                def check_file_exists():
                    return os.path.exists(dashboard_file)
                
                file_exists = await self.hass.async_add_executor_job(check_file_exists)
                
                if not file_exists:
                    dashboard_file = self.hass.config.path(f"dashboards/{dashboard_url}.yaml")
                    file_exists = await self.hass.async_add_executor_job(lambda: os.path.exists(dashboard_file))
                
                if file_exists:
                    # Use async_add_executor_job to perform file I/O asynchronously
                    def update_dashboard_file():
                        with open(dashboard_file, 'w') as f:
                            yaml.dump(dashboard_data, f, default_flow_style=False, allow_unicode=True)
                    
                    await self.hass.async_add_executor_job(update_dashboard_file)
                    
                    _LOGGER.info("Successfully updated dashboard file: %s", dashboard_file)
                    return {
                        "success": True,
                        "message": f"Dashboard '{dashboard_url}' updated successfully!"
                    }
                else:
                    return {"error": f"Dashboard file for '{dashboard_url}' not found"}
                
            except Exception as e:
                _LOGGER.error("Failed to update dashboard file: %s", str(e))
                return {"error": f"Failed to update dashboard file: {str(e)}"}
                
        except Exception as e:
            _LOGGER.exception("Error updating dashboard: %s", str(e))
            return {"error": f"Error updating dashboard: {str(e)}"}

    async def process_query(self, user_query: str, provider: str = None) -> Dict[str, Any]:
        """Process a user query with input validation and rate limiting."""
        try:
            if not user_query or not isinstance(user_query, str):
                return {"success": False, "error": "Invalid query format"}

            # Get the correct configuration for the requested provider
            if provider and provider in self.hass.data[DOMAIN]["configs"]:
                config = self.hass.data[DOMAIN]["configs"][provider]
            else:
                config = self.config

            _LOGGER.debug(f"Processing query with provider: {provider}")
            _LOGGER.debug(f"Using config: {json.dumps(config, default=str)}")

            selected_provider = provider or config.get("ai_provider", "llama")
            models_config = config.get("models", {})

            provider_config = {
                "openai": {
                    "token_key": "openai_token",
                    "model": models_config.get("openai", "gpt-3.5-turbo"),
                    "client_class": OpenAIClient
                },
                "gemini": {
                    "token_key": "gemini_token",
                    "model": models_config.get("gemini", "gemini-1.5-flash"),
                    "client_class": GeminiClient
                },
                "openrouter": {
                    "token_key": "openrouter_token",
                    "model": models_config.get("openrouter", "openai/gpt-4o"),
                    "client_class": OpenRouterClient
                },
                "llama": {
                    "token_key": "llama_token",
                    "model": models_config.get("llama", "Llama-4-Maverick-17B-128E-Instruct-FP8"),
                    "client_class": LlamaClient
                },
                "anthropic": {
                    "token_key": "anthropic_token",
                    "model": models_config.get("anthropic", "claude-3-5-sonnet-20241022"),
                    "client_class": AnthropicClient
                },
                "local": {
                    "token_key": "local_url",
                    "model": models_config.get("local", ""),
                    "client_class": LocalClient
                },
            }

            # Validate provider and get configuration
            if selected_provider not in provider_config:
                _LOGGER.warning(f"Invalid provider {selected_provider}, falling back to llama")
                selected_provider = "llama"

            provider_settings = provider_config[selected_provider]
            token = self.config.get(provider_settings["token_key"])

            # Validate token/URL
            if not token:
                error_msg = f"No {'URL' if selected_provider == 'local' else 'token'} configured for provider {selected_provider}"
                _LOGGER.error(error_msg)
                return {
                    "success": False,
                    "error": error_msg
                }

            # Initialize client
            try:
                if selected_provider == "local":
                    # LocalClient takes (url, model)
                    self.ai_client = provider_settings["client_class"](
                        url=token,
                        model=provider_settings["model"]
                    )
                else:
                    # Other clients take (token, model)
                    self.ai_client = provider_settings["client_class"](
                        token=token,
                        model=provider_settings["model"]
                    )
                _LOGGER.debug(f"Initialized {selected_provider} client with model {provider_settings['model']}")
            except Exception as e:
                error_msg = f"Error initializing {selected_provider} client: {str(e)}"
                _LOGGER.error(error_msg)
                return {
                    "success": False,
                    "error": error_msg
                }

            # Process the query with rate limiting and retries
            if not self._check_rate_limit():
                return {
                    "success": False,
                    "error": "Rate limit exceeded. Please wait before trying again."
                }

            # Sanitize user input
            user_query = user_query.strip()[:1000]  # Limit length and trim whitespace
            
            _LOGGER.debug("Processing new query: %s", user_query)
            
            # Check cache for identical query
            cache_key = f"query_{hash(user_query)}"
            cached_result = self._get_cached_data(cache_key)
            if cached_result:
                return cached_result

            # Add system message to conversation if it's the first message
            if not self.conversation_history:
                _LOGGER.debug("Adding system message to new conversation")
                self.conversation_history.append(self.system_prompt)

            # Add user query to conversation
            self.conversation_history.append({
                "role": "user",
                "content": user_query
            })
            _LOGGER.debug("Added user query to conversation history")

            max_iterations = 5  # Prevent infinite loops
            iteration = 0

            while iteration < max_iterations:
                iteration += 1
                _LOGGER.debug(f"Processing iteration {iteration} of {max_iterations}")
                
                try:
                    # Get AI response
                    _LOGGER.debug("Requesting response from AI provider")
                    response = await self._get_ai_response()
                    _LOGGER.debug("Received response from AI provider: %s", response)
                    
                    try:
                        # Try to parse the response as JSON with simplified approach
                        response_clean = response.strip()
                        
                        # Remove potential BOM and other invisible characters
                        import codecs
                        if response_clean.startswith(codecs.BOM_UTF8.decode('utf-8')):
                            response_clean = response_clean[1:]
                        
                        # Remove other common invisible characters
                        invisible_chars = ['\ufeff', '\u200b', '\u200c', '\u200d', '\u2060']
                        for char in invisible_chars:
                            response_clean = response_clean.replace(char, '')
                        
                        _LOGGER.debug("Cleaned response length: %d", len(response_clean))
                        _LOGGER.debug("Cleaned response first 100 chars: %s", response_clean[:100])
                        _LOGGER.debug("Cleaned response last 100 chars: %s", response_clean[-100:])
                        
                        # Simple strategy: try to parse the cleaned response directly
                        response_data = None
                        try:
                            _LOGGER.debug("Attempting basic JSON parse...")
                            response_data = json.loads(response_clean)
                            _LOGGER.debug("Basic JSON parse succeeded!")
                        except json.JSONDecodeError as e:
                            _LOGGER.warning("Basic JSON parse failed: %s", str(e))
                            _LOGGER.debug("JSON error position: %d", e.pos)
                            if e.pos < len(response_clean):
                                _LOGGER.debug("Character at error position: %s (ord: %d)", repr(response_clean[e.pos]), ord(response_clean[e.pos]))
                                _LOGGER.debug("Context around error: %s", repr(response_clean[max(0, e.pos-10):e.pos+10]))
                            
                            # Fallback: try to extract JSON by finding the first { and last }
                            json_start = response_clean.find('{')
                            json_end = response_clean.rfind('}')
                            
                            if json_start != -1 and json_end != -1 and json_end > json_start:
                                json_part = response_clean[json_start:json_end + 1]
                                _LOGGER.debug("Trying fallback extraction from pos %d to %d", json_start, json_end)
                                _LOGGER.debug("Extracted JSON: %s", json_part[:200])
                                
                                try:
                                    response_data = json.loads(json_part)
                                    _LOGGER.debug("Fallback JSON extraction succeeded!")
                                except json.JSONDecodeError as e2:
                                    _LOGGER.warning("Fallback JSON extraction also failed: %s", str(e2))
                                    raise e  # Re-raise the original error
                            else:
                                _LOGGER.warning("Could not find JSON boundaries in response")
                                raise e  # Re-raise the original error
                        
                        if response_data is None:
                            raise json.JSONDecodeError("All parsing strategies failed", response_clean, 0)
                        
                        _LOGGER.debug("Successfully parsed JSON response")
                        _LOGGER.debug("Parsed response type: %s", response_data.get("request_type", "unknown"))
                        
                        if response_data.get("request_type") == "data_request":
                            # Handle data request
                            request_type = response_data.get("request")
                            parameters = response_data.get("parameters", {})
                            _LOGGER.debug("Processing data request: %s with parameters: %s", 
                                        request_type, json.dumps(parameters))
                            
                            # Add AI's response to conversation history
                            self.conversation_history.append({
                                "role": "assistant",
                                "content": json.dumps(response_data)  # Store clean JSON
                            })
                            
                            # Get requested data
                            if request_type == "get_entity_state":
                                data = await self.get_entity_state(parameters.get("entity_id"))
                            elif request_type == "get_entities_by_domain":
                                data = await self.get_entities_by_domain(parameters.get("domain"))
                            elif request_type == "get_entities_by_area":
                                data = await self.get_entities_by_area(parameters.get("area_id"))
                            elif request_type == "get_entities":
                                data = await self.get_entities(
                                    area_id=parameters.get("area_id"),
                                    area_ids=parameters.get("area_ids")
                                )
                            elif request_type == "get_calendar_events":
                                data = await self.get_calendar_events(parameters.get("entity_id"))
                            elif request_type == "get_automations":
                                data = await self.get_automations()
                            elif request_type == "get_entity_registry":
                                data = await self.get_entity_registry()
                            elif request_type == "get_device_registry":
                                data = await self.get_device_registry()
                            elif request_type == "get_weather_data":
                                data = await self.get_weather_data()
                            elif request_type == "get_area_registry":
                                data = await self.get_area_registry()
                            elif request_type == "get_history":
                                data = await self.get_history(
                                    parameters.get("entity_id"),
                                    parameters.get("hours", 24)
                                )
                            elif request_type == "get_logbook_entries":
                                data = await self.get_logbook_entries(
                                    parameters.get("hours", 24)
                                )
                            elif request_type == "get_person_data":
                                data = await self.get_person_data()
                            elif request_type == "get_statistics":
                                data = await self.get_statistics(
                                    parameters.get("entity_id")
                                )
                            elif request_type == "get_scenes":
                                data = await self.get_scenes()
                            elif request_type == "get_dashboards":
                                data = await self.get_dashboards()
                            elif request_type == "get_dashboard_config":
                                data = await self.get_dashboard_config(
                                    parameters.get("dashboard_url")
                                )
                            elif request_type == "set_entity_state":
                                data = await self.set_entity_state(
                                    parameters.get("entity_id"),
                                    parameters.get("state"),
                                    parameters.get("attributes")
                                )
                            elif request_type == "create_automation":
                                data = await self.create_automation(
                                    parameters.get("automation")
                                )
                            elif request_type == "create_dashboard":
                                data = await self.create_dashboard(
                                    parameters.get("dashboard_config")
                                )
                            elif request_type == "update_dashboard":
                                data = await self.update_dashboard(
                                    parameters.get("dashboard_url"),
                                    parameters.get("dashboard_config")
                                )
                            else:
                                data = {"error": f"Unknown request type: {request_type}"}
                                _LOGGER.warning("Unknown request type: %s", request_type)
                            
                            # Check if any data request resulted in an error
                            if isinstance(data, dict) and "error" in data:
                                return {
                                    "success": False,
                                    "error": data["error"]
                                }
                            elif isinstance(data, list) and any("error" in item for item in data if isinstance(item, dict)):
                                errors = [item["error"] for item in data if isinstance(item, dict) and "error" in item]
                                return {
                                    "success": False,
                                    "error": "; ".join(errors)
                                }
                            
                            _LOGGER.debug("Retrieved data for request: %s", json.dumps(data, default=str))
                            
                            # Add data to conversation as a system message
                            self.conversation_history.append({
                                "role": "system",
                                "content": json.dumps({"data": data}, default=str)
                            })
                            continue
                        
                        elif response_data.get("request_type") == "final_response":
                            # Add final response to conversation history
                            self.conversation_history.append({
                                "role": "assistant",
                                "content": json.dumps(response_data)  # Store clean JSON
                            })
                            
                            # Return final response
                            _LOGGER.debug("Received final response: %s", response_data.get("response"))
                            result = {
                                "success": True,
                                "answer": response_data.get("response", "")
                            }
                            self._set_cached_data(cache_key, result)
                            return result
                        elif response_data.get("request_type") == "automation_suggestion":
                            # Add automation suggestion to conversation history
                            self.conversation_history.append({
                                "role": "assistant",
                                "content": json.dumps(response_data)  # Store clean JSON
                            })
                            
                            # Return automation suggestion
                            _LOGGER.debug("Received automation suggestion: %s", json.dumps(response_data.get("automation")))
                            result = {
                                "success": True,
                                "answer": json.dumps(response_data)
                            }
                            self._set_cached_data(cache_key, result)
                            return result
                        elif response_data.get("request_type") == "dashboard_suggestion":
                            # Add dashboard suggestion to conversation history
                            self.conversation_history.append({
                                "role": "assistant",
                                "content": json.dumps(response_data)  # Store clean JSON
                            })
                            
                            # Return dashboard suggestion
                            _LOGGER.debug("Received dashboard suggestion: %s", json.dumps(response_data.get("dashboard")))
                            result = {
                                "success": True,
                                "answer": json.dumps(response_data)
                            }
                            self._set_cached_data(cache_key, result)
                            return result
                        elif response_data.get("request_type") in ["get_entities", "get_entities_by_area", "get_entities_by_domain", "get_area_registry", "get_person_data"]:
                            # Handle direct requests (for backward compatibility)
                            parameters = response_data.get("parameters", {})
                            request_type = response_data.get("request_type")
                            _LOGGER.debug("Processing direct %s request with parameters: %s", request_type, json.dumps(parameters))
                            
                            # Add AI's response to conversation history
                            self.conversation_history.append({
                                "role": "assistant",
                                "content": json.dumps(response_data)  # Store clean JSON
                            })
                            
                            # Get data based on request type
                            if request_type == "get_entities":
                                data = await self.get_entities(
                                    area_id=parameters.get("area_id"),
                                    area_ids=parameters.get("area_ids")
                                )
                            elif request_type == "get_entities_by_area":
                                data = await self.get_entities_by_area(parameters.get("area_id"))
                            elif request_type == "get_entities_by_domain":
                                data = await self.get_entities_by_domain(parameters.get("domain"))
                            elif request_type == "get_area_registry":
                                data = await self.get_area_registry()
                            elif request_type == "get_person_data":
                                data = await self.get_person_data()
                            
                            if isinstance(data, list):
                                _LOGGER.debug("Retrieved %d items for %s", len(data), request_type)
                            else:
                                _LOGGER.debug("Retrieved data for %s", request_type)
                            
                            # Add data to conversation as a system message
                            self.conversation_history.append({
                                "role": "system",
                                "content": json.dumps({"data": data}, default=str)
                            })
                            continue
                        elif response_data.get("request_type") == "call_service":
                            # Handle service call request
                            domain = response_data.get("domain")
                            service = response_data.get("service")
                            target = response_data.get("target", {})
                            service_data = response_data.get("service_data", {})
                            
                            # Handle backward compatibility with old format
                            if not domain or not service:
                                request = response_data.get("request")
                                parameters = response_data.get("parameters", {})
                                
                                if request and "entity_id" in parameters:
                                    entity_id = parameters["entity_id"]
                                    # Infer domain from entity_id
                                    if "." in entity_id:
                                        domain = entity_id.split(".")[0]
                                        service = request
                                        target = {"entity_id": entity_id}
                                        # Remove entity_id from parameters to avoid duplication
                                        service_data = {k: v for k, v in parameters.items() if k != "entity_id"}
                                        _LOGGER.debug("Converted old format: domain=%s, service=%s", domain, service)
                            
                            _LOGGER.debug("Processing service call: %s.%s with target: %s and data: %s", 
                                        domain, service, json.dumps(target), json.dumps(service_data))
                            
                            # Add AI's response to conversation history
                            self.conversation_history.append({
                                "role": "assistant",
                                "content": json.dumps(response_data)  # Store clean JSON
                            })
                            
                            # Call the service
                            data = await self.call_service(domain, service, target, service_data)
                            
                            # Check if service call resulted in an error
                            if isinstance(data, dict) and "error" in data:
                                return {
                                    "success": False,
                                    "error": data["error"]
                                }
                            
                            _LOGGER.debug("Service call completed: %s", json.dumps(data, default=str))
                            
                            # Add data to conversation as a system message
                            self.conversation_history.append({
                                "role": "system",
                                "content": json.dumps({"data": data}, default=str)
                            })
                            continue
                        else:
                            _LOGGER.warning("Unknown response type: %s", response_data.get("request_type"))
                            return {
                                "success": False,
                                "error": f"Unknown response type: {response_data.get('request_type')}"
                            }
                            
                    except json.JSONDecodeError as e:
                        # Check if this is a local provider that might have already wrapped the response
                        provider = self.config.get("ai_provider", "unknown")
                        if provider == "local":
                            _LOGGER.debug("Local provider returned non-JSON response (this is normal and handled): %s", response[:200])
                        else:
                            # Log more of the response to help with debugging for non-local providers
                            response_preview = response[:1000] if len(response) > 1000 else response
                            _LOGGER.warning("Failed to parse response as JSON: %s. Response length: %d. Response preview: %s", 
                                          str(e), len(response), response_preview)
                            
                            # Log additional debugging information
                            _LOGGER.debug("First 50 characters as bytes: %s", response[:50].encode('utf-8') if response else b'')
                            _LOGGER.debug("Response starts with: %s", repr(response[:10]) if response else 'None')
                        
                        # Also log the response to a separate debug file for detailed analysis (non-local providers only)
                        if provider != "local":
                            try:
                                import os
                                debug_dir = "/config/ai_agent_ha_debug"
                                
                                def write_debug_file():
                                    if not os.path.exists(debug_dir):
                                        os.makedirs(debug_dir)
                                    
                                    import datetime
                                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                                    debug_file = os.path.join(debug_dir, f"failed_response_{timestamp}.txt")
                                    
                                    with open(debug_file, 'w', encoding='utf-8') as f:
                                        f.write(f"Timestamp: {timestamp}\n")
                                        f.write(f"Provider: {provider}\n")
                                        f.write(f"Error: {str(e)}\n")
                                        f.write(f"Response length: {len(response)}\n")
                                        f.write(f"Response bytes: {response.encode('utf-8') if response else b''}\n")
                                        f.write(f"Response repr: {repr(response)}\n")
                                        f.write(f"Full response:\n{response}\n")
                                    
                                    return debug_file
                                
                                # Run file operations in executor to avoid blocking
                                debug_file = await self.hass.async_add_executor_job(write_debug_file)
                                _LOGGER.info("Failed response saved to debug file: %s", debug_file)
                            except Exception as debug_error:
                                _LOGGER.debug("Could not save debug file: %s", str(debug_error))
                        
                        # If response is not valid JSON, try to wrap it as a final response
                        try:
                            wrapped_response = {
                                "request_type": "final_response",
                                "response": response
                            }
                            result = {
                                "success": True,
                                "answer": json.dumps(wrapped_response)
                            }
                            _LOGGER.debug("Wrapped non-JSON response as final_response")
                        except Exception as wrap_error:
                            _LOGGER.error("Failed to wrap response: %s", str(wrap_error))
                            result = {
                                "success": False,
                                "error": f"Invalid response format: {str(e)}"
                            }
                        
                        self._set_cached_data(cache_key, result)
                        return result
                        
                except Exception as e:
                    _LOGGER.exception("Error processing AI response: %s", str(e))
                    return {
                        "success": False,
                        "error": f"Error processing AI response: {str(e)}"
                    }

            # If we've reached max iterations without a final response
            _LOGGER.warning("Reached maximum iterations without final response")
            result = {
                "success": False,
                "error": "Maximum iterations reached without final response"
            }
            self._set_cached_data(cache_key, result)
            return result

        except Exception as e:
            _LOGGER.exception("Error in process_query: %s", str(e))
            return {
                "success": False,
                "error": f"Error in process_query: {str(e)}"
            }

    async def _get_ai_response(self) -> str:
        """Get response from the selected AI provider with intelligent retries and rate limiting."""
        if not self._check_rate_limit():
            raise Exception("Rate limit exceeded. Please try again later.")
        
        retry_count = 0
        last_error = None
        
        # Get provider and model specific limits
        provider = self.config.get("ai_provider", "openai")
        token_limits = self.provider_config.get("token_limits", {}).get(self.model, {})
        
        # Determine max context tokens (conservative approach)
        max_context_tokens = token_limits.get("context", 16384)
        safe_context_tokens = int(max_context_tokens * 0.7)  # Use 70% of context window
        
        # Optimize conversation history based on token limits
        optimized_messages = self._optimize_conversation_history(
            self.conversation_history, 
            safe_context_tokens
        )
        
        # Ensure system prompt is always first
        if not optimized_messages or optimized_messages[0].get("role") != "system":
            optimized_messages = [self.system_prompt] + optimized_messages
            
        _LOGGER.debug("Sending %d optimized messages to AI provider (was %d)", 
                     len(optimized_messages), len(self.conversation_history))
        _LOGGER.debug("AI provider: %s, model: %s", provider, self.model)
        
        while retry_count < self._max_retries:
            try:
                _LOGGER.debug("Attempt %d/%d: Calling AI client", retry_count + 1, self._max_retries)
                response = await self.ai_client.get_response(optimized_messages)
                _LOGGER.debug("AI client returned response of length: %d", len(response or ""))
                _LOGGER.debug("AI response preview: %s", (response or "")[:200])
                
                # Check if response is empty
                if not response or response.strip() == "":
                    _LOGGER.warning("AI client returned empty response on attempt %d", retry_count + 1)
                    if retry_count + 1 >= self._max_retries:
                        raise Exception("AI provider returned empty response after all retries")
                    else:
                        retry_count += 1
                        delay = calculate_exponential_backoff(retry_count, self._base_delay, self._max_delay)
                        _LOGGER.debug("Waiting %ss before retry due to empty response", delay)
                        await asyncio.sleep(delay)
                        continue
                        
                return response
                
            except Exception as e:
                error_str = str(e)
                _LOGGER.error("AI client error on attempt %d: %s", retry_count + 1, error_str)
                last_error = e
                retry_count += 1
                
                # Handle rate limit errors specifically
                if "rate limit" in error_str.lower() or "429" in error_str:
                    # Extract wait time from error message
                    wait_time = parse_retry_after_from_error(error_str)
                    
                    if wait_time:
                        _LOGGER.info("Rate limit hit, waiting %ss as suggested by API", wait_time)
                        await asyncio.sleep(wait_time)
                    else:
                        # Fallback to exponential backoff for rate limits
                        delay = calculate_exponential_backoff(retry_count, 30, 300)  # Longer delays for rate limits
                        _LOGGER.info("Rate limit hit, using exponential backoff: %ss", delay)
                        await asyncio.sleep(delay)
                else:
                    # Regular exponential backoff for other errors
                    if retry_count < self._max_retries:
                        delay = calculate_exponential_backoff(retry_count, self._base_delay, self._max_delay)
                        _LOGGER.debug("Waiting %ss before retry (attempt %d)", delay, retry_count)
                        await asyncio.sleep(delay)
                
                continue
                
        raise Exception(f"Failed after {retry_count} retries. Last error: {str(last_error)}")

    def clear_conversation_history(self) -> None:
        """Clear the conversation history and cache."""
        self.conversation_history = []
        self._cache.clear()
        _LOGGER.debug("Conversation history and cache cleared")

    async def set_entity_state(self, entity_id: str, state: str, attributes: Dict[str, Any] = None) -> Dict[str, Any]:
        """Set the state of an entity."""
        try:
            _LOGGER.debug("Setting state for entity %s to %s with attributes: %s", 
                        entity_id, state, json.dumps(attributes or {}))
            
            # Validate entity exists
            if not self.hass.states.get(entity_id):
                return {
                    "error": f"Entity {entity_id} not found"
                }
            
            # Call the appropriate service based on the domain
            domain = entity_id.split('.')[0]
            
            if domain == "light":
                service = "turn_on" if state.lower() in ["on", "true", "1"] else "turn_off"
                service_data = {"entity_id": entity_id}
                if attributes and service == "turn_on":
                    service_data.update(attributes)
                await self.hass.services.async_call("light", service, service_data)
            
            elif domain == "switch":
                service = "turn_on" if state.lower() in ["on", "true", "1"] else "turn_off"
                await self.hass.services.async_call("switch", service, {"entity_id": entity_id})
            
            elif domain == "cover":
                if state.lower() in ["open", "up"]:
                    service = "open_cover"
                elif state.lower() in ["close", "down"]:
                    service = "close_cover"
                elif state.lower() == "stop":
                    service = "stop_cover"
                else:
                    return {"error": f"Invalid state {state} for cover entity"}
                await self.hass.services.async_call("cover", service, {"entity_id": entity_id})
            
            elif domain == "climate":
                service_data = {"entity_id": entity_id}
                if state.lower() in ["on", "true", "1"]:
                    service = "turn_on"
                elif state.lower() in ["off", "false", "0"]:
                    service = "turn_off"
                elif state.lower() in ["heat", "cool", "dry", "fan_only", "auto"]:
                    service = "set_hvac_mode"
                    service_data["hvac_mode"] = state.lower()
                else:
                    return {"error": f"Invalid state {state} for climate entity"}
                await self.hass.services.async_call("climate", service, service_data)
            
            elif domain == "fan":
                service = "turn_on" if state.lower() in ["on", "true", "1"] else "turn_off"
                service_data = {"entity_id": entity_id}
                if attributes and service == "turn_on":
                    service_data.update(attributes)
                await self.hass.services.async_call("fan", service, service_data)
            
            else:
                # For other domains, try to set the state directly
                await self.hass.states.async_set(entity_id, state, attributes or {})
            
            # Get the new state to confirm the change
            new_state = self.hass.states.get(entity_id)
            return {
                "success": True,
                "entity_id": entity_id,
                "new_state": new_state.state,
                "new_attributes": new_state.attributes
            }
            
        except Exception as e:
            _LOGGER.exception("Error setting entity state: %s", str(e))
            return {
                "error": f"Error setting entity state: {str(e)}"
            }

    async def call_service(self, domain: str, service: str, target: Dict[str, Any] = None, service_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Call a Home Assistant service."""
        try:
            _LOGGER.debug("Calling service %s.%s with target: %s and data: %s", 
                        domain, service, json.dumps(target or {}), json.dumps(service_data or {}))
            
            # Prepare the service call data
            call_data = {}
            
            # Add target entities if provided
            if target:
                if "entity_id" in target:
                    entity_ids = target["entity_id"]
                    if isinstance(entity_ids, list):
                        call_data["entity_id"] = entity_ids
                    else:
                        call_data["entity_id"] = [entity_ids]
                
                # Add other target properties
                for key, value in target.items():
                    if key != "entity_id":
                        call_data[key] = value
            
            # Add service data if provided
            if service_data:
                call_data.update(service_data)
            
            _LOGGER.debug("Final service call data: %s", json.dumps(call_data))
            
            # Call the service
            await self.hass.services.async_call(domain, service, call_data)
            
            # Get the updated states of affected entities
            result_entities = []
            if "entity_id" in call_data:
                for entity_id in call_data["entity_id"]:
                    state = self.hass.states.get(entity_id)
                    if state:
                        result_entities.append({
                            "entity_id": entity_id,
                            "state": state.state,
                            "attributes": dict(state.attributes)
                        })
            
            return {
                "success": True,
                "service": f"{domain}.{service}",
                "entities_affected": result_entities,
                "message": f"Successfully called {domain}.{service}"
            }
            
        except Exception as e:
            _LOGGER.exception("Error calling service %s.%s: %s", domain, service, str(e))
            return {
                "error": f"Error calling service {domain}.{service}: {str(e)}"
            }

    async def save_user_prompt_history(self, user_id: str, history: List[str]) -> Dict[str, Any]:
        """Save user's prompt history to HA storage."""
        try:
            store = Store(self.hass, 1, f"ai_agent_ha_history_{user_id}")
            await store.async_save({"history": history})
            return {"success": True}
        except Exception as e:
            _LOGGER.exception("Error saving prompt history: %s", str(e))
            return {"error": f"Error saving prompt history: {str(e)}"}

    async def load_user_prompt_history(self, user_id: str) -> Dict[str, Any]:
        """Load user's prompt history from HA storage."""
        try:
            store = Store(self.hass, 1, f"ai_agent_ha_history_{user_id}")
            data = await store.async_load()
            history = data.get("history", []) if data else []
            return {"success": True, "history": history}
        except Exception as e:
            _LOGGER.exception("Error loading prompt history: %s", str(e))
            return {"error": f"Error loading prompt history: {str(e)}", "history": []}