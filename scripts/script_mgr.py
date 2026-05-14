from __future__ import annotations

from typing import Any, Dict, List

from utils.utilities import get_response_params_copy


RESPONSE_PARAM_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "openai_responses": {
        "description": "Default parameter mapping for OpenAI /v1/responses JSON bodies.",
        "response_params": get_response_params_copy(),
    }
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
