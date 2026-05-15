"""Provider-specific API request configuration for LLMind.

Centralises all per-provider knowledge: endpoint recognition, authentication
header construction, default payload shapes, and response template mapping.

Supported providers: ``openai``, ``anthropic``, ``xai``, ``gemini``,
and ``generic`` (any unrecognised host).
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


# Default model names shown in the interactive payload builder.
DEFAULT_MODELS: Dict[str, str] = {
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-opus-4-5",
    "xai": "grok-3",
    "gemini": "gemini-2.0-flash",
}


def detect_provider(url: str) -> str:
    """Return the provider name inferred from *url*.

    Returns one of ``"openai"``, ``"anthropic"``, ``"xai"``, ``"gemini"``,
    or ``"generic"`` when the domain is not recognised.
    """
    host = urlparse(url).netloc.lower()
    if "openai.com" in host:
        return "openai"
    if "anthropic.com" in host:
        return "anthropic"
    if "x.ai" in host:
        return "xai"
    if "googleapis.com" in host:
        return "gemini"
    return "generic"


def build_request_headers(provider: str, api_key: str) -> Dict[str, str]:
    """Return the authentication and content-type headers for *provider*.

    Gemini authenticates via a ``key`` query parameter (see
    :func:`inject_api_key_into_url`), so no auth header is included for it.
    """
    if provider == "anthropic":
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    if provider == "gemini":
        return {"Content-Type": "application/json"}
    if provider == "generic":
        return {"Authorization": f"Bearer {api_key}"}
    # openai, xai
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def inject_api_key_into_url(url: str, provider: str, api_key: str) -> str:
    """Return *url* with the API key appended as ``?key=`` for Gemini.

    All other providers return *url* unchanged.
    """
    if provider != "gemini":
        return url
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["key"] = [api_key]
    new_query = urlencode({k: v[0] for k, v in qs.items()})
    return urlunparse(parsed._replace(query=new_query))


def get_response_template_name(provider: str, url: str = "") -> str:
    """Return the response-parameter template name for *provider* and *url*.

    For OpenAI, the template depends on whether the endpoint is the Responses
    API (``/v1/responses``) or the Chat Completions API
    (``/v1/chat/completions``).
    """
    if provider == "anthropic":
        return "anthropic_messages"
    if provider == "xai":
        return "xai_chat"
    if provider == "gemini":
        return "gemini_generate"
    if provider == "openai":
        path = urlparse(url).path.rstrip("/")
        if path == "/v1/responses":
            return "openai_responses"
        return "openai_chat"
    return "openai_responses"


def requires_post(provider: str, url: str) -> bool:
    """Return ``True`` when the endpoint unconditionally requires POST."""
    if provider in ("anthropic", "xai", "gemini"):
        return True
    if provider == "openai":
        path = urlparse(url).path.rstrip("/")
        return path in ("/v1/responses", "/v1/chat/completions")
    return False


def get_default_payload(provider: str, prompt: str = "Hello from LLMind") -> Dict[str, Any]:
    """Return a minimal default JSON payload for *provider*."""
    if provider == "anthropic":
        return {
            "model": DEFAULT_MODELS["anthropic"],
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
    if provider == "xai":
        return {
            "model": DEFAULT_MODELS["xai"],
            "messages": [{"role": "user", "content": prompt}],
        }
    if provider == "gemini":
        return {"contents": [{"parts": [{"text": prompt}]}]}
    # openai (responses endpoint)
    return {
        "model": DEFAULT_MODELS["openai"],
        "input": prompt,
    }


def build_payload_from_user_input(
    provider: str,
    model: str,
    prompt_text: str,
    system_instructions: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a provider-appropriate JSON payload from interactive user input.

    Pass ``provider="openai_chat"`` to use the OpenAI Chat Completions request
    format (shared with xAI) even when the host is ``api.openai.com``.
    """
    if provider == "anthropic":
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or 1024,
            "messages": [{"role": "user", "content": prompt_text}],
        }
        if system_instructions:
            payload["system"] = system_instructions
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    if provider in ("xai", "openai_chat"):
        messages: list = []
        if system_instructions:
            messages.append({"role": "system", "content": system_instructions})
        messages.append({"role": "user", "content": prompt_text})
        payload = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return payload

    if provider == "gemini":
        payload = {"contents": [{"parts": [{"text": prompt_text}]}]}
        if system_instructions:
            payload["system_instruction"] = {"parts": [{"text": system_instructions}]}
        generation_config: Dict[str, Any] = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if generation_config:
            payload["generationConfig"] = generation_config
        return payload

    # openai responses endpoint
    payload = {"model": model, "input": prompt_text}
    if system_instructions:
        payload["instructions"] = system_instructions
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_output_tokens"] = max_tokens
    return payload
