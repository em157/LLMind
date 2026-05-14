from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional


# Default mapping for OpenAI /v1/responses payloads with nested ``output`` and
# ``usage`` sections.
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


def load_json_text(response_text: str) -> Optional[Any]:
    try:
        loaded = json.loads(response_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return loaded


def get_response_params_copy(response_params: Optional[Iterable[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    source = response_params if response_params is not None else DEFAULT_RESPONSE_PARAM_TEMPLATE
    return [deepcopy(param) for param in source]


def normalize_response_params(response_params: Optional[Iterable[Any]] = None) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in get_response_params_copy(response_params):
        if isinstance(item, str):
            normalized.append({"name": item, "path": item, "default": None})
            continue

        name_value = item.get("name")
        path = item.get("path")
        if isinstance(name_value, str) and name_value.strip():
            name = name_value.strip()
        elif isinstance(path, str) and path.strip():
            name = path.strip()
        else:
            name = ""
        if not name or not isinstance(path, str) or not path.strip():
            continue

        normalized.append(
            {
                "name": name,
                "path": path.strip(),
                "default": item.get("default"),
            }
        )
    return normalized


def resolve_param_path(payload: Any, path: str, default: Any = None) -> Any:
    """Resolve a dot-separated path inside dict/list payloads.

    Numeric path segments are treated as list indexes, so paths like
    ``output.0.content.0.text`` can traverse nested response structures.
    """
    current = payload
    for segment in path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return default
            continue

        if not isinstance(current, dict):
            return default

        if segment not in current:
            return default

        current = current[segment]

    return current
