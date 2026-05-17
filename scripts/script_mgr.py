from __future__ import annotations

from typing import Any, Dict, List

from utils.utilities import (
    get_response_params_copy,
    ANTHROPIC_RESPONSE_PARAM_TEMPLATE,
    GEMINI_RESPONSE_PARAM_TEMPLATE,
    OPENAI_CHAT_RESPONSE_PARAM_TEMPLATE,
)


RESPONSE_PARAM_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "openai_responses": {
        "description": "Default parameter mapping for OpenAI /v1/responses JSON bodies.",
        "response_params": get_response_params_copy(),
    },
    "openai_chat": {
        "description": "Parameter mapping for OpenAI /v1/chat/completions JSON bodies.",
        "response_params": get_response_params_copy(OPENAI_CHAT_RESPONSE_PARAM_TEMPLATE),
    },
    "openai_images": {
        "description": "Parameter mapping for OpenAI /v1/images/generations JSON bodies.",
        "response_params": get_response_params_copy(),
    },
    "anthropic_messages": {
        "description": "Parameter mapping for Anthropic /v1/messages JSON bodies.",
        "response_params": get_response_params_copy(ANTHROPIC_RESPONSE_PARAM_TEMPLATE),
    },
    "xai_chat": {
        "description": "Parameter mapping for xAI /v1/chat/completions JSON bodies (OpenAI-compatible).",
        "response_params": get_response_params_copy(OPENAI_CHAT_RESPONSE_PARAM_TEMPLATE),
    },
    "gemini_generate": {
        "description": "Parameter mapping for Google Gemini generateContent JSON bodies.",
        "response_params": get_response_params_copy(GEMINI_RESPONSE_PARAM_TEMPLATE),
    },
}


def get_response_param_template(template_name: str = "openai_responses") -> List[Dict[str, Any]]:
    template = RESPONSE_PARAM_TEMPLATES.get(template_name, RESPONSE_PARAM_TEMPLATES["openai_responses"])
    return get_response_params_copy(template["response_params"])


def get_response_param_script(template_name: str = "openai_responses") -> Dict[str, Any]:
    template = RESPONSE_PARAM_TEMPLATES.get(template_name, RESPONSE_PARAM_TEMPLATES["openai_responses"])
    return {
        "template": template_name,
        "description": template["description"],
        "response_params": get_response_param_template(template_name),
    }
