from __future__ import annotations

import base64
from email.message import Message
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Optional
from urllib.parse import unquote, unquote_to_bytes, urlparse

from scripts.script_mgr import get_response_param_template
from utils.utilities import parse_json_text, normalize_response_params, resolve_param_path


MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
FENCED_CODE_PATTERN = re.compile(r"```(?:[^\n`]*)\n?(.*?)```", re.DOTALL)
FILENAME_IN_TEXT_PATTERN = re.compile(r"`([^`\n/\\]+\.[A-Za-z0-9]{1,10})`")
# Header-style artifact reference: a filename in backticks (with an extension)
# optionally preceded by an introductory verb such as "Created"/"Saved"/"Wrote"/
# "Generated"/"Here is"/"File:" and optionally followed by a colon, then a
# fenced code block whose body is the file's content.
# Example match target:
#     Created `positive_sentiment.txt`:
#
#     ```txt
#     ...content...
#     ```
HEADER_FILENAME_PATTERN = re.compile(
    r"`([^`\n/\\]+\.[A-Za-z0-9]{1,10})`\s*:?\s*(?=\n+\s*```)",
)

REMOTE_DOWNLOADABLE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".svg",
    ".pdf",
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".json",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".zip",
}

FILE_CUE_PATTERN = re.compile(r"\b(save|saved|download|file|attached|attachment|created|generated|write|wrote)\b", re.IGNORECASE)

CONTENT_TYPE_FILENAME_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/octet-stream": ".bin",
}

DOWNLOADABLE_BINARY_CONTENT_TYPES = {
    "application/pdf",
    "application/zip",
    "application/octet-stream",
}

NON_DOWNLOADABLE_CONTENT_TYPES = {
    "application/json",
    "text/html",
}


def _content_disposition_message(headers: Optional[Dict[str, Any]]) -> Message:
    message = Message()
    disposition = normalize_headers(headers).get("content-disposition", "")
    if disposition:
        message["Content-Disposition"] = disposition
    return message


def normalize_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not headers:
        return {}
    return {str(key).lower(): str(value) for key, value in dict(headers).items()}


def get_download_filename(headers: Optional[Dict[str, Any]], default: str = "artifact.bin") -> str:
    normalized = normalize_headers(headers)
    filename = _content_disposition_message(normalized).get_filename()
    if filename:
        safe_name = Path(filename.replace("\\", "/")).name.strip()
        if safe_name and safe_name not in {".", ".."}:
            return safe_name
        return default
    content_type = normalized.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type.startswith("text/"):
        return "artifact.txt"
    suffix = CONTENT_TYPE_FILENAME_SUFFIX.get(content_type)
    if suffix:
        return f"artifact{suffix}"
    return default


def is_downloadable_response(headers: Optional[Dict[str, Any]]) -> bool:
    message = _content_disposition_message(headers)
    if message.get_content_disposition() == "attachment" or bool(message.get_filename()):
        return True

    content_type = normalize_headers(headers).get("content-type", "").split(";", 1)[0].strip().lower()
    if not content_type or content_type in NON_DOWNLOADABLE_CONTENT_TYPES:
        return False
    if content_type.startswith(("image/", "audio/", "video/")):
        return True
    return content_type in DOWNLOADABLE_BINARY_CONTENT_TYPES


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


def _filename_from_download_link(label: str, link: str) -> str:
    parsed = urlparse(link)
    path_name = Path(unquote(parsed.path or "")).name.strip()
    if path_name and path_name not in {".", ".."}:
        return path_name

    label_name = Path(label.replace("\\", "/")).name.strip()
    if label_name and label_name not in {".", ".."} and "." in label_name:
        return label_name

    return "artifact.txt"


def _mime_from_filename(filename: str, default: str = "text/plain") -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        return "text/plain"
    if suffix == ".json":
        return "application/json"
    if suffix == ".csv":
        return "text/csv"
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    if suffix == ".pdf":
        return "application/pdf"
    return default


def _latest_code_block_before(text: str, end: int) -> Optional[str]:
    code_blocks = [match.group(1) for match in FENCED_CODE_PATTERN.finditer(text[:end])]
    if not code_blocks:
        return None
    return code_blocks[-1].strip("\n")


def _next_code_block_after(text: str, start: int) -> Optional[str]:
    match = FENCED_CODE_PATTERN.search(text, start)
    if match is None:
        return None
    return match.group(1).strip("\n")


def _safe_filename(raw: str, default: str = "artifact.txt") -> str:
    # Normalize Windows-style backslash separators so Path().name strips any
    # leading directory portion regardless of the host platform.
    name = Path((raw or "").replace("\\", "/")).name.strip()
    if name and name not in {".", ".."}:
        return name
    return default


def _candidate_from_data_uri(label: str, link: str) -> Optional[Dict[str, Any]]:
    if not link.startswith("data:"):
        return None

    header, separator, data = link[5:].partition(",")
    if not separator:
        return None

    parts = header.split(";") if header else []
    mime = parts[0] if parts and "/" in parts[0] else "application/octet-stream"
    try:
        if "base64" in parts:
            content = base64.b64decode(data, validate=True)
        else:
            content = unquote_to_bytes(data)
    except Exception:
        return None

    return {
        "filename": _filename_from_download_link(label, link),
        "mime": mime,
        "content": content,
        "source_url": link,
    }


def _is_remote_downloadable_link(link: str, filename: str) -> bool:
    parsed = urlparse(link)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    suffix = Path(filename).suffix.lower()
    return suffix in REMOTE_DOWNLOADABLE_SUFFIXES


def _candidate_from_plain_text_with_file_cue(text: str) -> Optional[Dict[str, Any]]:
    stripped = text.strip()
    if len(stripped) < 80:
        return None
    if FILE_CUE_PATTERN.search(stripped) is None:
        return None

    match = FILENAME_IN_TEXT_PATTERN.search(stripped)
    if match is not None:
        filename = _safe_filename(match.group(1).strip())
    else:
        filename = "artifact.txt"

    return {
        "filename": filename,
        "mime": _mime_from_filename(filename),
        "content": stripped.encode("utf-8"),
        "source_url": None,
    }


def _candidates_from_openai_image_generation_payload(payload: Any) -> Iterable[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    output_items = payload.get("output")
    if not isinstance(output_items, list):
        return []

    candidates = []
    image_index = 1
    for output_item in output_items:
        if not isinstance(output_item, dict):
            continue
        if output_item.get("type") != "image_generation_call":
            continue

        raw_base64 = output_item.get("result")
        if not isinstance(raw_base64, str) or not raw_base64.strip():
            continue

        try:
            content = base64.b64decode(raw_base64, validate=True)
        except Exception:
            continue

        if not content:
            continue

        filename = f"image_generation_{image_index}.png"
        image_index += 1
        candidates.append(
            {
                "filename": filename,
                "mime": "image/png",
                "content": content,
                "source_url": None,
            }
        )

    return candidates


def _candidates_from_openai_images_payload(payload: Any) -> Iterable[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    data_items = payload.get("data")
    if not isinstance(data_items, list):
        return []

    candidates = []
    image_index = 1
    for item in data_items:
        if not isinstance(item, dict):
            continue

        raw_base64 = item.get("b64_json")
        if isinstance(raw_base64, str) and raw_base64.strip():
            try:
                content = base64.b64decode(raw_base64, validate=True)
            except Exception:
                content = b""
            if content:
                candidates.append(
                    {
                        "filename": f"image_generation_{image_index}.png",
                        "mime": "image/png",
                        "content": content,
                        "source_url": None,
                    }
                )
                image_index += 1
                continue

        image_url = item.get("url")
        if isinstance(image_url, str) and image_url.strip():
            filename = _filename_from_download_link(f"image_generation_{image_index}.png", image_url)
            candidates.append(
                {
                    "filename": filename,
                    "mime": _mime_from_filename(filename, default="image/png"),
                    "content": None,
                    "source_url": image_url,
                    "remote_fetch": True,
                }
            )
            image_index += 1

    return candidates


def extract_file_artifact_candidates_from_text(text: str) -> Iterable[Dict[str, Any]]:
    """Find downloadable file artifacts described in generated response text."""
    if not isinstance(text, str) or not text.strip():
        return []

    candidates = []
    for match in MARKDOWN_LINK_PATTERN.finditer(text):
        label = match.group(1).strip()
        link = match.group(2).strip()
        filename = _filename_from_download_link(label, link)

        data_uri_candidate = _candidate_from_data_uri(label, link)
        if data_uri_candidate is not None:
            candidates.append(data_uri_candidate)
            continue

        content = _latest_code_block_before(text, match.start())
        if content is None:
            if _is_remote_downloadable_link(link, filename):
                candidates.append(
                    {
                        "filename": filename,
                        "mime": _mime_from_filename(filename, default="application/octet-stream"),
                        "content": None,
                        "source_url": link,
                        "remote_fetch": True,
                    }
                )
            continue

        candidates.append(
            {
                "filename": filename,
                "mime": _mime_from_filename(filename),
                "content": content.encode("utf-8"),
                "source_url": link,
            }
        )

    # Header-style references: "Created `foo.txt`:" followed by a fenced code
    # block. The code block immediately following the header becomes the file
    # content. These have no source URL.
    for match in HEADER_FILENAME_PATTERN.finditer(text):
        filename = _safe_filename(match.group(1).strip())
        block = _next_code_block_after(text, match.end())
        if block is None:
            continue
        encoded = block.encode("utf-8")
        if any(
            existing.get("filename") == filename
            and existing.get("content") == encoded
            for existing in candidates
        ):
            continue
        candidates.append(
            {
                "filename": filename,
                "mime": _mime_from_filename(filename),
                "content": encoded,
                "source_url": None,
            }
        )

    if not candidates:
        fallback_candidate = _candidate_from_plain_text_with_file_cue(text)
        if fallback_candidate is not None:
            candidates.append(fallback_candidate)

    return candidates


def extract_file_artifact_candidates(payload: Any) -> Iterable[Dict[str, Any]]:
    """Extract artifact candidates from response parameter/raw response strings."""
    candidates = []
    seen = set()

    def append_candidate(candidate: Dict[str, Any]) -> None:
        key = (candidate.get("source_url"), candidate.get("filename"))
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    for candidate in _candidates_from_openai_image_generation_payload(payload):
        append_candidate(candidate)
    for candidate in _candidates_from_openai_images_payload(payload):
        append_candidate(candidate)

    def visit(value: Any) -> None:
        if isinstance(value, str):
            for candidate in extract_file_artifact_candidates_from_text(value):
                key = (candidate.get("source_url"), candidate.get("filename"))
                if key not in seen:
                    seen.add(key)
                    candidates.append(candidate)
            return
        if isinstance(value, dict):
            for candidate in _candidates_from_openai_image_generation_payload(value):
                append_candidate(candidate)
            for candidate in _candidates_from_openai_images_payload(value):
                append_candidate(candidate)
            for child in value.values():
                visit(child)
            return
        if isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return candidates


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
