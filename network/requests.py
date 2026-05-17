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
from network.providers import (
    build_request_headers,
    detect_provider,
    get_default_payload,
    get_response_template_name,
    inject_api_key_into_url,
    requires_post,
)
from response.response_handler import (
    build_artifact_response,
    extract_file_artifact_candidates,
    get_download_filename,
    is_downloadable_response,
    normalize_headers,
    parameterize_json_response,
)


MAX_REMOTE_ARTIFACT_BYTES = 10 * 1024 * 1024


def _fetch_remote_artifact(candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    source_url = candidate.get("source_url")
    if not isinstance(source_url, str) or not source_url.strip():
        return None

    fallback_filename = str(candidate.get("filename") or "artifact.bin")

    if _requests is not None:
        try:
            response = _requests.get(source_url, timeout=10)
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code >= 400:
                return None
            content = getattr(response, "content", b"")
            if not isinstance(content, bytes) or not content:
                return None
            if len(content) > MAX_REMOTE_ARTIFACT_BYTES:
                return None
            headers = getattr(response, "headers", {})
            normalized = normalize_headers(headers)
            filename = get_download_filename(headers, default=fallback_filename)
            mime = normalized.get("content-type", "").split(";", 1)[0].strip() or str(
                candidate.get("mime") or "application/octet-stream"
            )
            return {
                "filename": filename,
                "mime": mime,
                "content": content,
                "source_url": source_url,
            }
        except Exception:
            return None

    try:
        from urllib import request as _request

        req = _request.Request(source_url, method="GET")
        with _request.urlopen(req, timeout=10) as response:
            content = response.read(MAX_REMOTE_ARTIFACT_BYTES + 1)
            if not content or len(content) > MAX_REMOTE_ARTIFACT_BYTES:
                return None
            headers = dict(response.headers.items())
            normalized = normalize_headers(headers)
            filename = get_download_filename(headers, default=fallback_filename)
            mime = normalized.get("content-type", "").split(";", 1)[0].strip() or str(
                candidate.get("mime") or "application/octet-stream"
            )
            return {
                "filename": filename,
                "mime": mime,
                "content": content,
                "source_url": source_url,
            }
    except Exception:
        return None


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


def _format_llm_response_with_artifacts(
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
            effective_candidate = candidate
            if candidate.get("remote_fetch"):
                fetched_candidate = _fetch_remote_artifact(candidate)
                if fetched_candidate is None:
                    continue
                effective_candidate = fetched_candidate

            content = effective_candidate.get("content", b"")
            if not isinstance(content, bytes) or not content:
                continue
            saved_artifacts.append(
                _save_file_artifact(
                    effective_candidate.get("filename", "artifact.bin"),
                    content,
                    effective_candidate.get("mime", "application/octet-stream"),
                    writer,
                    cache,
                    effective_candidate.get("source_url"),
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
    response_template: Optional[str] = None,
) -> Tuple[int, str]:
    """Perform an API request using the stored API key.

    Detects the LLM provider from *url* and automatically applies the
    correct authentication headers, default payload, and response template.

    Returns ``(status_code, response_text)``.  A ``status_code`` of ``0``
    signals a local error (no key, network failure, etc.).
    """
    progress = ProgressOutput()
    writer = DataWriter()
    cache = CacheManager(writer)

    key = cache.load_api_key()
    if not key:
        progress.warn("No API key available for request")
        return 0, "no-api-key"

    provider = detect_provider(url)

    # Normalise method once and override to POST when the endpoint requires it.
    method = method.upper()
    if requires_post(provider, url) and method == "GET":
        progress.warn(
            f"{provider.upper()} endpoint requires POST; overriding GET to POST"
        )
        method = "POST"

    headers = build_request_headers(provider, key)

    # For generic endpoints add Content-Type only when sending a body.
    if provider == "generic" and json_payload is not None:
        headers["Content-Type"] = "application/json"

    # Inject the API key into the URL when the provider requires it (Gemini).
    request_url = inject_api_key_into_url(url, provider, key)

    # Use a sensible default payload for known LLM providers.
    if json_payload is None and provider != "generic":
        json_payload = get_default_payload(provider)

    # Resolve the response-parameter template name.
    if response_template is None:
        response_template = get_response_template_name(provider, url)

    is_llm_provider = provider != "generic"

    progress.step(f"Sending {method} request to {url}")

    # Prefer requests if installed
    if _requests is not None:
        try:
            if method == "GET":
                r = _requests.get(request_url, headers=headers, timeout=10)
            else:
                r = _requests.request(method, request_url, headers=headers, json=json_payload, timeout=10)
            if is_downloadable_response(r.headers):
                return r.status_code, _store_downloadable_response(
                    r.status_code,
                    r.content,
                    r.headers,
                    writer,
                    cache,
                )
            body = r.text
            if is_llm_provider:
                body = _format_llm_response_with_artifacts(
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

        req = _request.Request(request_url, data=data, headers=headers, method=method)
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
                if is_llm_provider:
                    decoded = _format_llm_response_with_artifacts(
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
