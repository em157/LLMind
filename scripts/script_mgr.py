from __future__ import annotations

from typing import Any, Dict, List

from utils.utilities import clone_response_params


RESPONSE_PARAM_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "openai_responses": {
        "description": "Default parameter mapping for OpenAI /v1/responses JSON bodies.",
        "response_params": clone_response_params(),
    }
}


def get_response_param_template(template_name: str = "openai_responses") -> List[Dict[str, Any]]:
    template = RESPONSE_PARAM_TEMPLATES.get(template_name, RESPONSE_PARAM_TEMPLATES["openai_responses"])
    return clone_response_params(template["response_params"])


def get_response_param_script(template_name: str = "openai_responses") -> Dict[str, Any]:
    template = RESPONSE_PARAM_TEMPLATES.get(template_name, RESPONSE_PARAM_TEMPLATES["openai_responses"])
    return {
        "template": template_name,
        "description": template["description"],
        "response_params": get_response_param_template(template_name),
    }
