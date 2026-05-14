from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional

from scripts.script_mgr import get_response_param_template
from utils.utilities import parse_json_text, normalize_response_params, resolve_param_path


def parameterize_json_response(
    response_text: str,
    response_params: Optional[Iterable[Dict[str, Any]]] = None,
    template_name: str = "openai_responses",
) -> Dict[str, Any]:
    """Extract configured response parameters from a JSON response body.

    Args:
        response_text: Raw response body returned by the LLM endpoint.
        response_params: Optional response parameter definitions with `name`,
            `path`, and optional `default` values.
        template_name: Template name used when `response_params` is omitted.

    Returns:
        A dictionary with `response_params` for extracted fields and
        `raw_response` containing either the parsed JSON body or the original
        text when the response could not be decoded as JSON.
    """
    parsed_response = parse_json_text(response_text)
    if parsed_response is None:
        return {"response_params": {}, "raw_response": response_text}

    active_params = normalize_response_params(response_params or get_response_param_template(template_name))
    parameterized_response: Dict[str, Any] = {}

    for param in active_params:
        parameterized_response[param["name"]] = resolve_param_path(
            parsed_response,
            param["path"],
            param.get("default"),
        )

    return {
        "response_params": parameterized_response,
        "raw_response": parsed_response,
    }


def format_parameterized_response(
    response_text: str,
    response_params: Optional[Iterable[Dict[str, Any]]] = None,
    template_name: str = "openai_responses",
) -> str:
    return json.dumps(
        parameterize_json_response(
            response_text=response_text,
            response_params=response_params,
            template_name=template_name,
        ),
        indent=2,
        ensure_ascii=False,
    )
