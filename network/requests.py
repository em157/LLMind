from __future__ import annotations

import json
from uuid import uuid4
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

try:
    import requests as _requests
except Exception:
    _requests = None

from appdata.progress_output import ProgressOutput
from cache.cache_mgr import CacheManager
from appdata.data_writer import DataWriter
from response.response_handler import (
    build_artifact_response,
    extract_file_artifact_candidates,
    get_download_filename,
    is_downloadable_response,
    normalize_headers,
    parameterize_json_response,
)


def _store_downloadable_response(
    status_code: int,
    body: bytes,
    headers: Optional[Dict[str, Any]],
    writer: DataWriter,
    cache: CacheManager,
) -> str:
    normalized_headers = normalize_headers(headers)
    filename = writer.sanitize_filename(get_download_filename(normalized_headers))
    artifact_id = f"artifact_{uuid4().hex}"
    path = writer.write_artifact(artifact_id, filename, body)
    content_type = normalized_headers.get("content-type", "application/octet-stream")
    mime = content_type.split(";", 1)[0].strip() or "application/octet-stream"
    artifact = {
        "id": artifact_id,
        "filename": filename,
        "mime": mime,
        "content_type": content_type,
        "url": writer.file_url(path),
        "path": str(path),
        "size": len(body),
    }
    cache.save_artifact_record(artifact)
    return build_artifact_response(status_code, artifact, normalized_headers)


def _save_file_artifact(
    filename: str,
    content: bytes,
    mime: str,
    writer: DataWriter,
    cache: CacheManager,
    source_url: Optional[str] = None,
) -> Dict[str, Any]:
    artifact_id = f"artifact_{uuid4().hex}"
    safe_filename = writer.sanitize_filename(filename)
    path = writer.write_artifact(artifact_id, safe_filename, content)
    artifact = {
        "id": artifact_id,
        "filename": safe_filename,
        "mime": mime,
        "content_type": mime,
        "url": writer.file_url(path),
        "path": str(path),
        "size": len(content),
    }
    if source_url:
        artifact["source_url"] = source_url
    cache.save_artifact_record(artifact)
    return artifact


def _format_openai_response_with_artifacts(
    response_text: str,
    response_params: Optional[Iterable[Dict[str, Any]]],
    response_template: str,
    writer: DataWriter,
    cache: CacheManager,
) -> str:
    parameterized = parameterize_json_response(
        response_text=response_text,
        response_params=response_params,
        template_name=response_template,
    )
    saved_artifacts = []
    for candidate in extract_file_artifact_candidates(parameterized):
        try:
            content = candidate.get("content", b"")
            if not isinstance(content, bytes) or not content:
                continue
            saved_artifacts.append(
                _save_file_artifact(
                    candidate.get("filename", "artifact.bin"),
                    content,
                    candidate.get("mime", "application/octet-stream"),
                    writer,
                    cache,
                    candidate.get("source_url"),
                )
            )
        except Exception:
            continue
    if saved_artifacts:
        parameterized["artifacts"] = saved_artifacts
    return json.dumps(parameterized, indent=2, ensure_ascii=False)


def perform_api_request(
    url: str,
    method: str = "GET",
    json_payload: Optional[Dict[str, Any]] = None,
    response_params: Optional[Iterable[Dict[str, Any]]] = None,
    response_template: str = "openai_responses",
) -> Tuple[int, str]:
    """Perform a simple API request using stored API key.

    Returns (status_code, response_text). If no requests library is available,
    falls back to urllib. Uses CacheManager to load the stored key and passes
    it in an Authorization header as 'Bearer <key>'.
    """
    progress = ProgressOutput()
    writer = DataWriter()
    cache = CacheManager(writer)

    key = cache.load_api_key()
    if not key:
        progress.warn("No API key available for request")
        return 0, "no-api-key"

    parsed = urlparse(url)
    is_openai_responses = (
        parsed.netloc.lower() == "api.openai.com"
        and parsed.path.rstrip("/") == "/v1/responses"
    )

    method = method.upper()
    if is_openai_responses and method == "GET":
        progress.warn("OpenAI /v1/responses requires POST; overriding GET to POST")
        method = "POST"

    headers = {"Authorization": f"Bearer {key}"}
    if json_payload is not None or is_openai_responses:
        headers["Content-Type"] = "application/json"

    if is_openai_responses and json_payload is None:
        json_payload = {
            "model": "gpt-4.1-mini",
            "input": "Hello from LLMind",
        }

    progress.step(f"Sending {method} request to {url}")

    # Prefer requests if installed
    if _requests is not None:
        try:
            if method == "GET":
                r = _requests.get(url, headers=headers, timeout=10)
            else:
                r = _requests.request(method, url, headers=headers, json=json_payload, timeout=10)
            if is_downloadable_response(r.headers):
                return r.status_code, _store_downloadable_response(
                    r.status_code,
                    r.content,
                    r.headers,
                    writer,
                    cache,
                )
            body = r.text
            if is_openai_responses:
                body = _format_openai_response_with_artifacts(
                    body,
                    response_params=response_params,
                    response_template=response_template,
                    writer=writer,
                    cache=cache,
                )
            return r.status_code, body
        except Exception as exc:
            return 0, f"requests-error: {exc}"

    # Fallback to urllib
    try:
        from urllib import request as _request

        data = None
        if json_payload is not None:
            data = json.dumps(json_payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = _request.Request(url, data=data, headers=headers, method=method)
        with _request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            response_headers = dict(resp.headers.items())
            if is_downloadable_response(response_headers):
                return resp.getcode(), _store_downloadable_response(
                    resp.getcode(),
                    body,
                    response_headers,
                    writer,
                    cache,
                )
            try:
                decoded = body.decode("utf-8")
                if is_openai_responses:
                    decoded = _format_openai_response_with_artifacts(
                        decoded,
                        response_params=response_params,
                        response_template=response_template,
                        writer=writer,
                        cache=cache,
                    )
                return resp.getcode(), decoded
            except Exception:
                return resp.getcode(), str(body)
    except Exception as exc:
        return 0, f"urllib-error: {exc}"
