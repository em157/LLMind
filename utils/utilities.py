from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional


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


def load_json_text(response_text: str) -> Optional[Dict[str, Any]]:
    try:
        loaded = json.loads(response_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(loaded, dict):
        return loaded
    return {"value": loaded}


def clone_response_params(response_params: Optional[Iterable[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    source = response_params if response_params is not None else DEFAULT_RESPONSE_PARAM_TEMPLATE
    return [deepcopy(param) for param in source]


def normalize_response_params(response_params: Optional[Iterable[Any]] = None) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in clone_response_params(response_params):
        if isinstance(item, str):
            normalized.append({"name": item, "path": item, "default": None})
            continue

        name = str(item.get("name") or item.get("path") or "").strip()
        path = item.get("path")
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
