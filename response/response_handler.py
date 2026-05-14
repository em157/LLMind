from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Optional
from urllib.parse import unquote

from scripts.script_mgr import get_response_param_template
from utils.utilities import parse_json_text, normalize_response_params, resolve_param_path


_FILENAME_RE = re.compile(r'filename="?([^";]+)"?', re.IGNORECASE)
_FILENAME_STAR_RE = re.compile(r"filename\*=UTF-8''([^;]+)", re.IGNORECASE)


def normalize_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not headers:
        return {}
    return {str(key).lower(): str(value) for key, value in dict(headers).items()}


def get_download_filename(headers: Optional[Dict[str, Any]], default: str = "artifact.bin") -> str:
    normalized = normalize_headers(headers)
    disposition = normalized.get("content-disposition", "")
    match = _FILENAME_STAR_RE.search(disposition) or _FILENAME_RE.search(disposition)
    if match:
        return unquote(match.group(1).strip()) or default
    content_type = normalized.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type.startswith("text/"):
        return "artifact.txt"
    return default


def is_downloadable_response(headers: Optional[Dict[str, Any]]) -> bool:
    normalized = normalize_headers(headers)
    disposition = normalized.get("content-disposition", "").lower()
    return "attachment" in disposition or "filename=" in disposition or "filename*=" in disposition


def build_artifact_response(
    status_code: int,
    artifact: Dict[str, Any],
    headers: Optional[Dict[str, Any]] = None,
) -> str:
    normalized = normalize_headers(headers)
    return json.dumps(
        {
            "status": "ok" if 200 <= status_code < 400 else "error",
            "status_code": status_code,
            "artifact": artifact,
            "headers": {
                key: normalized[key]
                for key in ("content-type", "content-disposition", "content-length", "cache-control")
                if key in normalized
            },
        },
        indent=2,
        ensure_ascii=False,
    )


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
