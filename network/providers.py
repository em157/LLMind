"""Provider-specific API request configuration for LLMind.

Centralises all per-provider knowledge: endpoint recognition, authentication
header construction, default payload shapes, and response template mapping.

Supported providers: ``openai``, ``anthropic``, ``xai``, ``gemini``,
``deepseek``, ``mistral``, and ``generic`` (any unrecognised host).
"""

from __future__ import annotations

import json as _json
import urllib.request as _urllib_request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


# Default model names shown in the interactive payload builder.
DEFAULT_MODELS: Dict[str, str] = {
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-opus-4-5",
    "xai": "grok-3",
    "gemini": "gemini-2.0-flash",
    "deepseek": "deepseek-chat",
    "mistral": "mistral-large-latest",
}


def _is_openai_image_model(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return normalized.startswith("gpt-image") or normalized in {"gpt-4o-image"}


def _openai_path(url: str) -> str:
    return urlparse(url).path.rstrip("/")


def normalize_provider_url(provider: str, url: str) -> Tuple[str, Optional[str]]:
    """Return a provider-normalized URL and optional warning message."""
    if provider != "openai":
        return url, None

    path = _openai_path(url)
    if path == "/v1/images":
        parsed = urlparse(url)
        normalized = urlunparse(parsed._replace(path="/v1/images/generations"))
        return (
            normalized,
            "OpenAI image generation endpoint '/v1/images' normalized to '/v1/images/generations'.",
        )

    return url, None


def detect_provider(url: str) -> str:
    """Return the provider name inferred from *url*.

    Returns one of ``"openai"``, ``"anthropic"``, ``"xai"``, ``"gemini"``,
    ``"deepseek"``, ``"mistral"``, or ``"generic"`` when the domain is not
    recognised.
    """
    # Strip port number before matching to avoid false negatives.
    host = urlparse(url).netloc.lower().split(":")[0]
    if host == "api.openai.com" or host.endswith(".openai.com"):
        return "openai"
    if host == "api.anthropic.com" or host.endswith(".anthropic.com"):
        return "anthropic"
    if host == "api.x.ai" or host.endswith(".x.ai"):
        return "xai"
    if host == "generativelanguage.googleapis.com" or host.endswith(".googleapis.com"):
        return "gemini"
    if host == "api.deepseek.com" or host.endswith(".deepseek.com"):
        return "deepseek"
    if host == "api.mistral.ai" or host.endswith(".mistral.ai"):
        return "mistral"
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
    # openai, xai, deepseek, mistral
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
    new_query = urlencode({k: v[0] for k, v in qs.items() if len(v) > 0})
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
    if provider in {"deepseek", "mistral"}:
        return "openai_chat"
    if provider == "openai":
        path = _openai_path(url)
        if path == "/v1/responses":
            return "openai_responses"
        if path in ("/v1/images", "/v1/images/generations"):
            return "openai_images"
        return "openai_chat"
    return "openai_responses"


def requires_post(provider: str, url: str) -> bool:
    """Return ``True`` when the endpoint unconditionally requires POST."""
    if provider in ("anthropic", "xai", "gemini", "deepseek", "mistral"):
        return True
    if provider == "openai":
        path = _openai_path(url)
        return path in ("/v1/responses", "/v1/chat/completions", "/v1/images", "/v1/images/generations")
    return False


def get_default_payload(provider: str, prompt_text: str = "Hello from LLMind", url: str = "") -> Dict[str, Any]:
    """Return a minimal default JSON payload for *provider*."""
    if provider == "anthropic":
        return {
            "model": DEFAULT_MODELS["anthropic"],
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt_text}],
        }
    if provider == "xai":
        return {
            "model": DEFAULT_MODELS["xai"],
            "messages": [{"role": "user", "content": prompt_text}],
        }
    if provider == "deepseek":
        return {
            "model": DEFAULT_MODELS["deepseek"],
            "messages": [{"role": "user", "content": prompt_text}],
        }
    if provider == "mistral":
        return {
            "model": DEFAULT_MODELS["mistral"],
            "messages": [{"role": "user", "content": prompt_text}],
        }
    if provider == "gemini":
        return {"contents": [{"parts": [{"text": prompt_text}]}]}
    if provider == "openai" and _openai_path(url) == "/v1/images/generations":
        return {
            "model": "gpt-image-1",
            "prompt": prompt_text,
        }

    # openai (responses endpoint)
    return {
        "model": DEFAULT_MODELS["openai"],
        "input": prompt_text,
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

    Pass ``provider="openai_chat"`` for OpenAI Chat Completions format and
    ``provider="openai_images"`` for OpenAI Images format.
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

    if provider in ("xai", "openai_chat", "deepseek", "mistral"):
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

    if provider == "openai_images":
        effective_prompt = prompt_text
        if system_instructions:
            effective_prompt = f"{system_instructions}\n\n{prompt_text}"
        return {
            "model": model,
            "prompt": effective_prompt,
        }

    # openai responses endpoint
    payload: Dict[str, Any] = {"model": model}
    if _is_openai_image_model(model):
        # Image models expect multimodal input content blocks rather than
        # tool invocation with plain string input.
        payload["input"] = [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt_text}],
            }
        ]
    else:
        payload["input"] = prompt_text
    if system_instructions:
        payload["instructions"] = system_instructions
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_output_tokens"] = max_tokens
    return payload


def fetch_openai_models(api_key: str) -> List[str]:
    """Return a sorted list of model IDs available under *api_key*.

    Calls ``GET https://api.openai.com/v1/models`` with the supplied key.
    Returns an empty list on any network or authentication failure so callers
    can gracefully fall back to the static default.
    """
    url = "https://api.openai.com/v1/models"
    auth_headers = {"Authorization": f"Bearer {api_key}"}

    # Prefer the ``requests`` library if available.
    try:
        import requests as _req  # type: ignore
        resp = _req.get(url, headers=auth_headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return sorted(
                m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m
            )
        return []
    except Exception:
        pass

    # Fallback: stdlib urllib
    try:
        req = _urllib_request.Request(url, headers=auth_headers)
        with _urllib_request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
            return sorted(
                m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m
            )
    except Exception:
        return []


def fetch_anthropic_models(api_key: str) -> List[str]:
    """Return a sorted list of Anthropic model IDs available under *api_key*.

    Calls ``GET https://api.anthropic.com/v1/models`` with the supplied key.
    Returns an empty list on any failure.
    """
    url = "https://api.anthropic.com/v1/models"
    auth_headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    try:
        import requests as _req  # type: ignore
        resp = _req.get(url, headers=auth_headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return sorted(
                m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m
            )
        return []
    except Exception:
        pass

    try:
        req = _urllib_request.Request(url, headers=auth_headers)
        with _urllib_request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
            return sorted(
                m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m
            )
    except Exception:
        return []


def fetch_xai_models(api_key: str) -> List[str]:
    """Return a sorted list of xAI model IDs available under *api_key*.

    Calls ``GET https://api.x.ai/v1/models`` (OpenAI-compatible format).
    Returns an empty list on any failure.
    """
    url = "https://api.x.ai/v1/models"
    auth_headers = {"Authorization": f"Bearer {api_key}"}

    try:
        import requests as _req  # type: ignore
        resp = _req.get(url, headers=auth_headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return sorted(
                m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m
            )
        return []
    except Exception:
        pass

    try:
        req = _urllib_request.Request(url, headers=auth_headers)
        with _urllib_request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
            return sorted(
                m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m
            )
    except Exception:
        return []


def fetch_gemini_models(api_key: str) -> List[str]:
    """Return a sorted list of Gemini model IDs available under *api_key*.

    Calls ``GET https://generativelanguage.googleapis.com/v1beta/models?key=<api_key>``.
    Returns an empty list on any failure.
    """
    base_url = "https://generativelanguage.googleapis.com/v1beta/models"
    url = f"{base_url}?key={api_key}"

    try:
        import requests as _req  # type: ignore
        resp = _req.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # Each entry has a "name" like "models/gemini-2.0-flash"; strip the prefix.
            return sorted(
                m["name"].removeprefix("models/")
                for m in data.get("models", [])
                if isinstance(m, dict) and "name" in m
            )
        return []
    except Exception:
        pass

    try:
        req = _urllib_request.Request(url)
        with _urllib_request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
            return sorted(
                m["name"].removeprefix("models/")
                for m in data.get("models", [])
                if isinstance(m, dict) and "name" in m
            )
    except Exception:
        return []
