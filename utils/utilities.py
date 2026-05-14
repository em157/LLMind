from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional


# Default mapping for OpenAI /v1/responses payloads with nested `output` and
# `usage` sections.
DEFAULT_RESPONSE_PARAM_TEMPLATE: List[Dict[str, Any]] = [
    {"name": "response_id", "path": "id", "default": None},
    {"name": "model", "path": "model", "default": None},
    {"name": "status", "path": "status", "default": None},
    {"name": "role", "path": "output.0.role", "default": None},
    {"name": "message_text", "path": "output.0.content.0.text", "default": ""},
    {"name": "input_tokens", "path": "usage.input_tokens", "default": 0},
    {"name": "output_tokens", "path": "usage.output_tokens", "default": 0},
    {"name": "total_tokens", "path": "usage.total_tokens", "default": 0},
]


def parse_json_text(response_text: str) -> Optional[Any]:
    try:
        loaded = json.loads(response_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return loaded


def get_response_params_copy(response_params: Optional[Iterable[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    source = response_params if response_params is not None else DEFAULT_RESPONSE_PARAM_TEMPLATE
    return [deepcopy(param) for param in source]


def _clean_string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def normalize_response_params(response_params: Optional[Iterable[Any]] = None) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in get_response_params_copy(response_params):
        if isinstance(item, str):
            normalized.append({"name": item, "path": item, "default": None})
            continue

        path = _clean_string(item.get("path"))
        name = _clean_string(item.get("name")) or path
        if not name or not path:
            continue

        normalized.append(
            {
                "name": name,
                "path": path,
                "default": item.get("default"),
            }
        )
    return normalized


def resolve_param_path(payload: Any, path: str, default: Any = None) -> Any:
    """Resolve a dot-separated path inside dict/list payloads.

    Args:
        payload: Parsed JSON payload to inspect.
        path: Dot-separated lookup path such as `output.0.content.0.text`.
        default: Value returned when any path segment cannot be resolved.

    Returns:
        The resolved value when the full path exists, otherwise `default`.
    """
    current = payload
    for segment in path.split("."):
        if isinstance(current, list):
            if not segment.isdigit():
                return default
            try:
                current = current[int(segment)]
            except IndexError:
                return default
            continue

        if not isinstance(current, dict):
            return default

        if segment not in current:
            return default

        current = current[segment]

    return current
