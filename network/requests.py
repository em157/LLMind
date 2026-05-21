from __future__ import annotations

import json
import time
from uuid import uuid4
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import requests as _requests
except Exception:
    _requests = None

from appdata.progress_output import ProgressOutput
from cache.cache_mgr import CacheManager
from appdata.data_writer import DataWriter
from hooks.hook_registry import HookRegistry
from hooks.provider_adapters import render_provider_tools
from network.providers import (
    build_request_headers,
    detect_provider,
    get_default_payload,
    get_response_template_name,
    inject_api_key_into_url,
    normalize_provider_url,
    requires_post,
)
from response.model_hook_processor import process_model_response_with_hooks
from response.response_handler import (
    build_artifact_response,
    extract_file_artifact_candidates,
    get_download_filename,
    is_downloadable_response,
    normalize_headers,
    parameterize_json_response,
)
from utils.request_timing import RequestDelayTimer


MAX_REMOTE_ARTIFACT_BYTES = 10 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10
OPENAI_IMAGE_TIMEOUT_SECONDS = 120
XAI_CHAT_TIMEOUT_SECONDS = 180
GEMINI_CHAT_TIMEOUT_SECONDS = 180
MAX_HOOK_ORCHESTRATION_STEPS = 100


def _format_http_error(status_code: int, reason: Optional[str], body: str = "") -> str:
    reason_text = (reason or "").strip()
    head = f"http-error: HTTP {status_code}"
    if reason_text:
        head = f"{head} {reason_text}"

    body_text = (body or "").strip()
    if not body_text:
        return head

    # Keep errors readable in the terminal while still preserving details.
    if len(body_text) > 3000:
        body_text = body_text[:3000] + "... [truncated]"
    return f"{head}: {body_text}"


def _is_read_timeout_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "read timed out" in text or "read timeout" in text


def _is_transient_network_error(exc: Exception) -> bool:
    """Return True for transient SSL/connection errors that are safe to retry."""
    if _is_read_timeout_error(exc):
        return True
    text = str(exc).lower()
    _transient_markers = (
        "ssl",
        "eof occurred",
        "connection reset",
        "connection aborted",
        "remote end closed",
        "broken pipe",
        "connection refused",
        "temporary failure",
        "network is unreachable",
    )
    return any(m in text for m in _transient_markers)


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


def _build_llm_response_bundle(
    response_text: str,
    provider: str,
    response_params: Optional[Iterable[Dict[str, Any]]],
    response_template: str,
    writer: DataWriter,
    cache: CacheManager,
    execute_hook_calls: bool = False,
    resolved_executables: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
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

    hook_registry = HookRegistry(app_name=writer.app_name)
    hook_registry.register_builtin_hooks()
    parameterized["hook_processing"] = process_model_response_with_hooks(
        payload=parameterized.get("raw_response"),
        provider=provider,
        registry=hook_registry,
        app_data_dir=writer.app_data_dir,
        execute=execute_hook_calls,
        resolved_executables=resolved_executables,
    )

    return parameterized


def _format_llm_response_with_artifacts(
    response_text: str,
    provider: str,
    response_params: Optional[Iterable[Dict[str, Any]]],
    response_template: str,
    writer: DataWriter,
    cache: CacheManager,
    execute_hook_calls: bool = False,
    resolved_executables: Optional[Dict[str, str]] = None,
) -> str:
    parameterized = _build_llm_response_bundle(
        response_text=response_text,
        provider=provider,
        response_params=response_params,
        response_template=response_template,
        writer=writer,
        cache=cache,
        execute_hook_calls=execute_hook_calls,
        resolved_executables=resolved_executables,
    )

    return json.dumps(parameterized, indent=2, ensure_ascii=False)


def _build_openai_chat_followup_payload(
    request_payload: Dict[str, Any],
    bundle: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build a follow-up payload by appending assistant tool_calls + tool outputs."""
    messages = request_payload.get("messages")
    if not isinstance(messages, list):
        return None

    raw_response = bundle.get("raw_response")
    if not isinstance(raw_response, dict):
        return None

    hook_processing = bundle.get("hook_processing")
    if not isinstance(hook_processing, dict):
        return None

    choices = raw_response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return None
    assistant_message = first_choice.get("message")
    if not isinstance(assistant_message, dict):
        return None

    tool_calls = assistant_message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return None

    hook_results = hook_processing.get("hook_results", [])
    if not isinstance(hook_results, list) or not hook_results:
        return None

    followup_payload = dict(request_payload)
    next_messages: List[Dict[str, Any]] = list(messages)
    next_messages.append(
        {
            "role": "assistant",
            "content": assistant_message.get("content"),
            "tool_calls": tool_calls,
        }
    )

    for index, tool_call in enumerate(tool_calls):
        if not isinstance(tool_call, dict):
            continue
        tool_id = tool_call.get("id")
        if not isinstance(tool_id, str) or not tool_id.strip():
            continue

        result: Dict[str, Any]
        if index < len(hook_results) and isinstance(hook_results[index], dict):
            result = hook_results[index]
        else:
            result = {
                "hook_name": "unknown",
                "success": False,
                "message": "No matching hook execution result found",
                "details": {},
            }

        tool_content = json.dumps(
            {
                "hook_name": result.get("hook_name"),
                "success": bool(result.get("success", False)),
                "message": result.get("message", ""),
                "details": result.get("details", {}),
            },
            ensure_ascii=False,
        )
        next_messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_id,
                "content": tool_content,
            }
        )

    followup_payload["messages"] = next_messages
    return followup_payload


def _build_anthropic_followup_payload(
    request_payload: Dict[str, Any],
    bundle: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build an Anthropic follow-up payload with tool_result blocks."""
    messages = request_payload.get("messages")
    if not isinstance(messages, list):
        return None

    raw_response = bundle.get("raw_response")
    if not isinstance(raw_response, dict):
        return None

    hook_processing = bundle.get("hook_processing")
    if not isinstance(hook_processing, dict):
        return None

    assistant_content = raw_response.get("content")
    if not isinstance(assistant_content, list) or not assistant_content:
        return None

    tool_uses: List[Dict[str, Any]] = []
    for item in assistant_content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "tool_use":
            continue
        tool_use_id = item.get("id")
        if not isinstance(tool_use_id, str) or not tool_use_id.strip():
            continue
        tool_uses.append(item)

    if not tool_uses:
        return None

    hook_results = hook_processing.get("hook_results", [])
    if not isinstance(hook_results, list) or not hook_results:
        return None

    followup_payload = dict(request_payload)
    next_messages: List[Dict[str, Any]] = list(messages)
    next_messages.append(
        {
            "role": "assistant",
            "content": assistant_content,
        }
    )

    tool_result_blocks: List[Dict[str, Any]] = []
    for index, tool_use in enumerate(tool_uses):
        tool_use_id = str(tool_use.get("id", "")).strip()
        if not tool_use_id:
            continue

        if index < len(hook_results) and isinstance(hook_results[index], dict):
            result = hook_results[index]
        else:
            result = {
                "hook_name": "unknown",
                "success": False,
                "message": "No matching hook execution result found",
                "details": {},
            }

        tool_content = json.dumps(
            {
                "hook_name": result.get("hook_name"),
                "success": bool(result.get("success", False)),
                "message": result.get("message", ""),
                "details": result.get("details", {}),
            },
            ensure_ascii=False,
        )
        tool_result_blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": tool_content,
            }
        )

    if not tool_result_blocks:
        return None

    next_messages.append(
        {
            "role": "user",
            "content": tool_result_blocks,
        }
    )
    followup_payload["messages"] = next_messages
    return followup_payload


def _run_openai_chat_hook_orchestration(
    request_payload: Dict[str, Any],
    initial_bundle: Dict[str, Any],
    send_followup_request: Callable[[Dict[str, Any]], Tuple[int, str, str]],
    provider: str,
    response_params: Optional[Iterable[Dict[str, Any]]],
    response_template: str,
    writer: DataWriter,
    cache: CacheManager,
    resolved_executables: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run a bounded hook orchestration loop for OpenAI-chat style tool calls."""
    current_payload = request_payload
    latest_bundle = initial_bundle
    iterations: List[Dict[str, Any]] = []
    stopped_reason = "max_steps_reached"
    stop_step = MAX_HOOK_ORCHESTRATION_STEPS

    for step in range(1, MAX_HOOK_ORCHESTRATION_STEPS + 1):
        followup_payload = _build_openai_chat_followup_payload(current_payload, latest_bundle)
        if followup_payload is None:
            stopped_reason = "no_followup_payload"
            stop_step = step
            break

        status_code, response_text, reason = send_followup_request(followup_payload)
        if status_code >= 400:
            latest_bundle["orchestration"] = {
                "enabled": True,
                "iterations": iterations,
                "stopped_reason": "followup_http_error",
                "stop_step": step,
                "error": _format_http_error(status_code, reason, response_text),
            }
            return latest_bundle

        next_bundle = _build_llm_response_bundle(
            response_text=response_text,
            provider=provider,
            response_params=response_params,
            response_template=response_template,
            writer=writer,
            cache=cache,
            execute_hook_calls=True,
            resolved_executables=resolved_executables,
        )
        hook_processing = next_bundle.get("hook_processing", {})
        executed_hooks = 0
        _hook_results: list = []
        if isinstance(hook_processing, dict):
            _hr = hook_processing.get("hook_results", [])
            if isinstance(_hr, list):
                _hook_results = _hr
                executed_hooks = len(_hook_results)
        hook_summary = [
            {
                "hook": r.get("hook_name"),
                "success": r.get("success"),
                "message": (r.get("message") or "")[:300],
                "details": r.get("details"),
            }
            for r in _hook_results
            if isinstance(r, dict)
        ]
        iterations.append({"step": step, "executed_hook_calls": executed_hooks, "results": hook_summary})
        latest_bundle = next_bundle
        current_payload = followup_payload

        if executed_hooks == 0:
            stopped_reason = "no_executed_hooks"
            stop_step = step
            break

    latest_bundle["orchestration"] = {
        "enabled": True,
        "iterations": iterations,
        "max_steps": MAX_HOOK_ORCHESTRATION_STEPS,
        "stopped_reason": stopped_reason,
        "stop_step": stop_step,
    }
    return latest_bundle


def _run_anthropic_hook_orchestration(
    request_payload: Dict[str, Any],
    initial_bundle: Dict[str, Any],
    send_followup_request: Callable[[Dict[str, Any]], Tuple[int, str, str]],
    provider: str,
    response_params: Optional[Iterable[Dict[str, Any]]],
    response_template: str,
    writer: DataWriter,
    cache: CacheManager,
    resolved_executables: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run a bounded hook orchestration loop for Anthropic tool_use blocks."""
    current_payload = request_payload
    latest_bundle = initial_bundle
    iterations: List[Dict[str, Any]] = []
    stopped_reason = "max_steps_reached"
    stop_step = MAX_HOOK_ORCHESTRATION_STEPS

    for step in range(1, MAX_HOOK_ORCHESTRATION_STEPS + 1):
        followup_payload = _build_anthropic_followup_payload(current_payload, latest_bundle)
        if followup_payload is None:
            stopped_reason = "no_followup_payload"
            stop_step = step
            break

        status_code, response_text, reason = send_followup_request(followup_payload)
        if status_code >= 400:
            latest_bundle["orchestration"] = {
                "enabled": True,
                "iterations": iterations,
                "stopped_reason": "followup_http_error",
                "stop_step": step,
                "error": _format_http_error(status_code, reason, response_text),
            }
            return latest_bundle

        next_bundle = _build_llm_response_bundle(
            response_text=response_text,
            provider=provider,
            response_params=response_params,
            response_template=response_template,
            writer=writer,
            cache=cache,
            execute_hook_calls=True,
            resolved_executables=resolved_executables,
        )
        hook_processing = next_bundle.get("hook_processing", {})
        executed_hooks = 0
        _hook_results: list = []
        if isinstance(hook_processing, dict):
            _hr = hook_processing.get("hook_results", [])
            if isinstance(_hr, list):
                _hook_results = _hr
                executed_hooks = len(_hook_results)
        hook_summary = [
            {
                "hook": r.get("hook_name"),
                "success": r.get("success"),
                "message": (r.get("message") or "")[:300],
                "details": r.get("details"),
            }
            for r in _hook_results
            if isinstance(r, dict)
        ]
        iterations.append({"step": step, "executed_hook_calls": executed_hooks, "results": hook_summary})
        latest_bundle = next_bundle
        current_payload = followup_payload

        if executed_hooks == 0:
            stopped_reason = "no_executed_hooks"
            stop_step = step
            break

    latest_bundle["orchestration"] = {
        "enabled": True,
        "iterations": iterations,
        "max_steps": MAX_HOOK_ORCHESTRATION_STEPS,
        "stopped_reason": stopped_reason,
        "stop_step": stop_step,
    }
    return latest_bundle


def _build_gemini_followup_payload(
    request_payload: Dict[str, Any],
    bundle: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build a Gemini follow-up payload with functionResponse parts."""
    contents = request_payload.get("contents")
    if not isinstance(contents, list):
        return None

    raw_response = bundle.get("raw_response")
    if not isinstance(raw_response, dict):
        return None

    hook_processing = bundle.get("hook_processing")
    if not isinstance(hook_processing, dict):
        return None

    candidates = raw_response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None

    first_candidate = candidates[0]
    if not isinstance(first_candidate, dict):
        return None
    model_content = first_candidate.get("content")
    if not isinstance(model_content, dict):
        return None
    model_parts = model_content.get("parts")
    if not isinstance(model_parts, list) or not model_parts:
        return None

    function_calls: List[Dict[str, Any]] = []
    for part in model_parts:
        if not isinstance(part, dict):
            continue
        function_call = part.get("functionCall") or part.get("function_call")
        if not isinstance(function_call, dict):
            continue
        name = function_call.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        function_calls.append(function_call)

    if not function_calls:
        return None

    hook_results = hook_processing.get("hook_results", [])
    if not isinstance(hook_results, list) or not hook_results:
        return None

    followup_payload = dict(request_payload)
    next_contents: List[Dict[str, Any]] = list(contents)
    next_contents.append(model_content)

    response_parts: List[Dict[str, Any]] = []
    for index, function_call in enumerate(function_calls):
        function_name = str(function_call.get("name", "")).strip()
        if not function_name:
            continue

        if index < len(hook_results) and isinstance(hook_results[index], dict):
            result = hook_results[index]
        else:
            result = {
                "hook_name": "unknown",
                "success": False,
                "message": "No matching hook execution result found",
                "details": {},
            }

        response_payload = {
            "hook_name": result.get("hook_name"),
            "success": bool(result.get("success", False)),
            "message": result.get("message", ""),
            "details": result.get("details", {}),
        }
        response_parts.append(
            {
                "functionResponse": {
                    "name": function_name,
                    "response": response_payload,
                }
            }
        )

    if not response_parts:
        return None

    next_contents.append(
        {
            "role": "user",
            "parts": response_parts,
        }
    )
    followup_payload["contents"] = next_contents
    return followup_payload


def _build_gemini_recovery_payload(
    request_payload: Dict[str, Any],
    bundle: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build a one-shot recovery prompt when Gemini returns text instead of functionCall."""
    contents = request_payload.get("contents")
    if not isinstance(contents, list):
        return None

    raw_response = bundle.get("raw_response")
    if not isinstance(raw_response, dict):
        return None

    candidates = raw_response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None

    first_candidate = candidates[0]
    if not isinstance(first_candidate, dict):
        return None

    model_content = first_candidate.get("content")
    if not isinstance(model_content, dict):
        return None

    model_parts = model_content.get("parts")
    if not isinstance(model_parts, list) or not model_parts:
        return None

    followup_payload = dict(request_payload)
    next_contents: List[Dict[str, Any]] = list(contents)
    next_contents.append(model_content)
    next_contents.append(
        {
            "role": "user",
            "parts": [
                {
                    "text": (
                        "Continue with functionCall tool use. "
                        "If the task is blocked, return blocked_reason with explicit evidence. "
                        "Do not return plain completion text without either tool calls or blocked evidence."
                    )
                }
            ],
        }
    )
    followup_payload["contents"] = next_contents
    return followup_payload


def _run_gemini_hook_orchestration(
    request_payload: Dict[str, Any],
    initial_bundle: Dict[str, Any],
    send_followup_request: Callable[[Dict[str, Any]], Tuple[int, str, str]],
    provider: str,
    response_params: Optional[Iterable[Dict[str, Any]]],
    response_template: str,
    writer: DataWriter,
    cache: CacheManager,
    resolved_executables: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run a bounded hook orchestration loop for Gemini function calls."""
    current_payload = request_payload
    latest_bundle = initial_bundle
    iterations: List[Dict[str, Any]] = []
    stopped_reason = "max_steps_reached"
    stop_step = MAX_HOOK_ORCHESTRATION_STEPS
    recovery_attempted = False

    for step in range(1, MAX_HOOK_ORCHESTRATION_STEPS + 1):
        followup_payload = _build_gemini_followup_payload(current_payload, latest_bundle)
        if followup_payload is None:
            stopped_reason = "no_followup_payload"
            stop_step = step
            break

        status_code, response_text, reason = send_followup_request(followup_payload)
        if status_code >= 400:
            latest_bundle["orchestration"] = {
                "enabled": True,
                "iterations": iterations,
                "stopped_reason": "followup_http_error",
                "stop_step": step,
                "error": _format_http_error(status_code, reason, response_text),
            }
            return latest_bundle

        next_bundle = _build_llm_response_bundle(
            response_text=response_text,
            provider=provider,
            response_params=response_params,
            response_template=response_template,
            writer=writer,
            cache=cache,
            execute_hook_calls=True,
            resolved_executables=resolved_executables,
        )
        hook_processing = next_bundle.get("hook_processing", {})
        executed_hooks = 0
        hook_summary: List[Dict[str, Any]] = []
        if isinstance(hook_processing, dict):
            hook_results = hook_processing.get("hook_results", [])
            if isinstance(hook_results, list):
                executed_hooks = len(hook_results)
                hook_summary = [
                    {
                        "hook": r.get("hook_name"),
                        "success": r.get("success"),
                        "message": (r.get("message") or "")[:300],
                        "details": r.get("details"),
                    }
                    for r in hook_results
                    if isinstance(r, dict)
                ]

        iterations.append({"step": step, "executed_hook_calls": executed_hooks, "results": hook_summary})
        latest_bundle = next_bundle
        current_payload = followup_payload

        if executed_hooks == 0:
            if not recovery_attempted:
                recovery_attempted = True
                recovery_payload = _build_gemini_recovery_payload(current_payload, latest_bundle)
                if recovery_payload is not None:
                    status_code, response_text, reason = send_followup_request(recovery_payload)
                    if status_code >= 400:
                        latest_bundle["orchestration"] = {
                            "enabled": True,
                            "iterations": iterations,
                            "stopped_reason": "followup_http_error",
                            "stop_step": step,
                            "error": _format_http_error(status_code, reason, response_text),
                            "recovery_attempted": recovery_attempted,
                        }
                        return latest_bundle

                    recovery_bundle = _build_llm_response_bundle(
                        response_text=response_text,
                        provider=provider,
                        response_params=response_params,
                        response_template=response_template,
                        writer=writer,
                        cache=cache,
                        execute_hook_calls=True,
                        resolved_executables=resolved_executables,
                    )
                    recovery_hook_processing = recovery_bundle.get("hook_processing", {})
                    recovery_executed_hooks = 0
                    recovery_hook_summary: List[Dict[str, Any]] = []
                    if isinstance(recovery_hook_processing, dict):
                        recovery_hook_results = recovery_hook_processing.get("hook_results", [])
                        if isinstance(recovery_hook_results, list):
                            recovery_executed_hooks = len(recovery_hook_results)
                            recovery_hook_summary = [
                                {
                                    "hook": r.get("hook_name"),
                                    "success": r.get("success"),
                                    "message": (r.get("message") or "")[:300],
                                    "details": r.get("details"),
                                }
                                for r in recovery_hook_results
                                if isinstance(r, dict)
                            ]

                    iterations.append(
                        {
                            "step": step,
                            "recovery": True,
                            "executed_hook_calls": recovery_executed_hooks,
                            "results": recovery_hook_summary,
                        }
                    )
                    latest_bundle = recovery_bundle
                    current_payload = recovery_payload

                    if recovery_executed_hooks > 0:
                        continue

            stopped_reason = "no_executed_hooks"
            stop_step = step
            break

    latest_bundle["orchestration"] = {
        "enabled": True,
        "iterations": iterations,
        "max_steps": MAX_HOOK_ORCHESTRATION_STEPS,
        "stopped_reason": stopped_reason,
        "stop_step": stop_step,
        "recovery_attempted": recovery_attempted,
    }
    return latest_bundle


def perform_api_request(
    url: str,
    method: str = "GET",
    json_payload: Optional[Dict[str, Any]] = None,
    response_params: Optional[Iterable[Dict[str, Any]]] = None,
    response_template: Optional[str] = None,
    api_key: Optional[str] = None,
    execute_hook_calls: bool = False,
    resolved_executables: Optional[Dict[str, str]] = None,
    request_delay_seconds: Optional[float] = None,
    delay_timer: Optional[RequestDelayTimer] = None,
) -> Tuple[int, str]:
    """Perform an API request using the stored API key.

    Detects the LLM provider from *url* and automatically applies the
    correct authentication headers, default payload, and response template.

    *api_key* overrides the cached key when provided (e.g. when the user has
    selected a specific key from a multi-key store).

    *execute_hook_calls* controls whether extracted structured tool/function
    calls are validated only (default) or also executed via the hook registry.

    Returns ``(status_code, response_text)``.  A ``status_code`` of ``0``
    signals a local error (no key, network failure, etc.).
    """
    progress = ProgressOutput()
    writer = DataWriter()
    cache = CacheManager(writer)

    key = api_key or cache.load_api_key()
    if not key:
        progress.warn("No API key available for request")
        return 0, "no-api-key"

    provider = detect_provider(url)
    effective_url, url_warning = normalize_provider_url(provider, url)
    if url_warning:
        progress.warn(url_warning)

    # Normalise method once and override to POST when the endpoint requires it.
    method = method.upper()
    if requires_post(provider, effective_url) and method == "GET":
        progress.warn(
            f"{provider.upper()} endpoint requires POST; overriding GET to POST"
        )
        method = "POST"

    headers = build_request_headers(provider, key)

    # For generic endpoints add Content-Type only when sending a body.
    if provider == "generic" and json_payload is not None:
        headers["Content-Type"] = "application/json"

    # Inject the API key into the URL when the provider requires it (Gemini).
    request_url = inject_api_key_into_url(effective_url, provider, key)

    # Use a sensible default payload for known LLM providers.
    if json_payload is None and provider != "generic":
        json_payload = get_default_payload(provider, url=effective_url)

    # Resolve the response-parameter template name first so tool rendering
    # can select the correct format (e.g. Responses API vs Chat Completions).
    if response_template is None:
        response_template = get_response_template_name(provider, effective_url)

    # Auto-attach tool schemas for supported providers unless already set.
    if provider != "generic" and isinstance(json_payload, dict):
        if "tools" not in json_payload:
            json_payload.update(render_provider_tools(provider, response_template=response_template))

    is_llm_provider = provider != "generic"
    if response_template == "openai_images":
        timeout_seconds = OPENAI_IMAGE_TIMEOUT_SECONDS
    elif provider == "xai" and response_template in {"openai_chat", "xai_chat"}:
        timeout_seconds = XAI_CHAT_TIMEOUT_SECONDS
    elif provider == "gemini":
        timeout_seconds = GEMINI_CHAT_TIMEOUT_SECONDS
    else:
        timeout_seconds = DEFAULT_REQUEST_TIMEOUT_SECONDS

    # Optional pacing controls for rate-limited providers.
    if delay_timer is not None:
        slept = delay_timer.wait_for_turn()
        if slept > 0:
            progress.info(f"Request pacing applied via shared timer (+{slept:.2f}s)")
    elif request_delay_seconds is not None and request_delay_seconds > 0:
        time.sleep(request_delay_seconds)
        progress.info(f"Request pacing applied (+{request_delay_seconds:.2f}s)")

    progress.step(f"Sending {method} request to {request_url}")

    # Prefer requests if installed
    if _requests is not None:
        try:
            should_retry_xai = provider == "xai" and response_template in {"openai_chat", "xai_chat"}
            attempt = 0
            while True:
                attempt += 1
                try:
                    if method == "GET":
                        r = _requests.get(request_url, headers=headers, timeout=timeout_seconds)
                    else:
                        r = _requests.request(
                            method,
                            request_url,
                            headers=headers,
                            json=json_payload,
                            timeout=timeout_seconds,
                        )
                    break
                except Exception as exc:
                    if should_retry_xai and attempt <= 3 and _is_read_timeout_error(exc):
                        progress.warn(f"xAI request read timeout; retrying (attempt {attempt})")
                        continue
                    raise
            if r.status_code >= 400:
                return r.status_code, _format_http_error(r.status_code, getattr(r, "reason", ""), r.text)
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
                bundle = _build_llm_response_bundle(
                    response_text=body,
                    provider=provider,
                    response_params=response_params,
                    response_template=response_template,
                    writer=writer,
                    cache=cache,
                    execute_hook_calls=execute_hook_calls,
                    resolved_executables=resolved_executables,
                )
                if (
                    execute_hook_calls
                    and response_template in {"openai_chat", "xai_chat"}
                    and provider in {"openai", "xai", "deepseek", "mistral"}
                    and isinstance(json_payload, dict)
                ):
                    progress.step("Submitting hook results for follow-up orchestration", duration_seconds=0)

                    def _send_followup(payload: Dict[str, Any]) -> Tuple[int, str, str]:
                        _max_retries = 3
                        for _attempt in range(1, _max_retries + 1):
                            try:
                                followup_response = _requests.request(
                                    method,
                                    request_url,
                                    headers=headers,
                                    json=payload,
                                    timeout=timeout_seconds,
                                )
                                return (
                                    followup_response.status_code,
                                    followup_response.text,
                                    str(getattr(followup_response, "reason", "")),
                                )
                            except Exception as _exc:
                                if _attempt < _max_retries and _is_transient_network_error(_exc):
                                    progress.warn(f"Follow-up request transient error (attempt {_attempt}); retrying")
                                    continue
                                raise

                    bundle = _run_openai_chat_hook_orchestration(
                        request_payload=json_payload,
                        initial_bundle=bundle,
                        send_followup_request=_send_followup,
                        provider=provider,
                        response_params=response_params,
                        response_template=response_template,
                        writer=writer,
                        cache=cache,
                        resolved_executables=resolved_executables,
                    )
                elif (
                    execute_hook_calls
                    and provider == "anthropic"
                    and response_template == "anthropic_messages"
                    and isinstance(json_payload, dict)
                ):
                    progress.step("Submitting hook results for follow-up orchestration", duration_seconds=0)

                    def _send_followup(payload: Dict[str, Any]) -> Tuple[int, str, str]:
                        _max_retries = 3
                        for _attempt in range(1, _max_retries + 1):
                            try:
                                followup_response = _requests.request(
                                    method,
                                    request_url,
                                    headers=headers,
                                    json=payload,
                                    timeout=timeout_seconds,
                                )
                                return (
                                    followup_response.status_code,
                                    followup_response.text,
                                    str(getattr(followup_response, "reason", "")),
                                )
                            except Exception as _exc:
                                if _attempt < _max_retries and _is_transient_network_error(_exc):
                                    progress.warn(f"Follow-up request transient error (attempt {_attempt}); retrying")
                                    continue
                                raise

                    bundle = _run_anthropic_hook_orchestration(
                        request_payload=json_payload,
                        initial_bundle=bundle,
                        send_followup_request=_send_followup,
                        provider=provider,
                        response_params=response_params,
                        response_template=response_template,
                        writer=writer,
                        cache=cache,
                        resolved_executables=resolved_executables,
                    )
                elif (
                    execute_hook_calls
                    and provider == "gemini"
                    and response_template == "gemini_generate"
                    and isinstance(json_payload, dict)
                ):
                    progress.step("Submitting hook results for follow-up orchestration", duration_seconds=0)

                    def _send_followup(payload: Dict[str, Any]) -> Tuple[int, str, str]:
                        _max_retries = 3
                        for _attempt in range(1, _max_retries + 1):
                            try:
                                followup_response = _requests.request(
                                    method,
                                    request_url,
                                    headers=headers,
                                    json=payload,
                                    timeout=timeout_seconds,
                                )
                                return (
                                    followup_response.status_code,
                                    followup_response.text,
                                    str(getattr(followup_response, "reason", "")),
                                )
                            except Exception as _exc:
                                if _attempt < _max_retries and _is_transient_network_error(_exc):
                                    progress.warn(f"Follow-up request transient error (attempt {_attempt}); retrying")
                                    continue
                                raise

                    bundle = _run_gemini_hook_orchestration(
                        request_payload=json_payload,
                        initial_bundle=bundle,
                        send_followup_request=_send_followup,
                        provider=provider,
                        response_params=response_params,
                        response_template=response_template,
                        writer=writer,
                        cache=cache,
                        resolved_executables=resolved_executables,
                    )
                body = json.dumps(bundle, indent=2, ensure_ascii=False)
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
        with _request.urlopen(req, timeout=timeout_seconds) as resp:
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
                    bundle = _build_llm_response_bundle(
                        response_text=decoded,
                        provider=provider,
                        response_params=response_params,
                        response_template=response_template,
                        writer=writer,
                        cache=cache,
                        execute_hook_calls=execute_hook_calls,
                        resolved_executables=resolved_executables,
                    )

                    if (
                        execute_hook_calls
                        and response_template in {"openai_chat", "xai_chat"}
                        and provider in {"openai", "xai", "deepseek", "mistral"}
                        and isinstance(json_payload, dict)
                    ):
                        progress.step("Submitting hook results for follow-up orchestration", duration_seconds=0)

                        def _send_followup(payload: Dict[str, Any]) -> Tuple[int, str, str]:
                            followup_data = json.dumps(payload).encode("utf-8")
                            followup_req = _request.Request(
                                request_url,
                                data=followup_data,
                                headers=headers,
                                method=method,
                            )
                            with _request.urlopen(followup_req, timeout=timeout_seconds) as followup_resp:
                                followup_body = followup_resp.read().decode("utf-8", errors="replace")
                                return (
                                    followup_resp.getcode(),
                                    followup_body,
                                    str(getattr(followup_resp, "reason", "")),
                                )

                        bundle = _run_openai_chat_hook_orchestration(
                            request_payload=json_payload,
                            initial_bundle=bundle,
                            send_followup_request=_send_followup,
                            provider=provider,
                            response_params=response_params,
                            response_template=response_template,
                            writer=writer,
                            cache=cache,
                            resolved_executables=resolved_executables,
                        )
                    elif (
                        execute_hook_calls
                        and provider == "anthropic"
                        and response_template == "anthropic_messages"
                        and isinstance(json_payload, dict)
                    ):
                        progress.step("Submitting hook results for follow-up orchestration", duration_seconds=0)

                        def _send_followup(payload: Dict[str, Any]) -> Tuple[int, str, str]:
                            followup_data = json.dumps(payload).encode("utf-8")
                            followup_req = _request.Request(
                                request_url,
                                data=followup_data,
                                headers=headers,
                                method=method,
                            )
                            with _request.urlopen(followup_req, timeout=timeout_seconds) as followup_resp:
                                followup_body = followup_resp.read().decode("utf-8", errors="replace")
                                return (
                                    followup_resp.getcode(),
                                    followup_body,
                                    str(getattr(followup_resp, "reason", "")),
                                )

                        bundle = _run_anthropic_hook_orchestration(
                            request_payload=json_payload,
                            initial_bundle=bundle,
                            send_followup_request=_send_followup,
                            provider=provider,
                            response_params=response_params,
                            response_template=response_template,
                            writer=writer,
                            cache=cache,
                            resolved_executables=resolved_executables,
                        )
                    elif (
                        execute_hook_calls
                        and provider == "gemini"
                        and response_template == "gemini_generate"
                        and isinstance(json_payload, dict)
                    ):
                        progress.step("Submitting hook results for follow-up orchestration", duration_seconds=0)

                        def _send_followup(payload: Dict[str, Any]) -> Tuple[int, str, str]:
                            followup_data = json.dumps(payload).encode("utf-8")
                            followup_req = _request.Request(
                                request_url,
                                data=followup_data,
                                headers=headers,
                                method=method,
                            )
                            with _request.urlopen(followup_req, timeout=timeout_seconds) as followup_resp:
                                followup_body = followup_resp.read().decode("utf-8", errors="replace")
                                return (
                                    followup_resp.getcode(),
                                    followup_body,
                                    str(getattr(followup_resp, "reason", "")),
                                )

                        bundle = _run_gemini_hook_orchestration(
                            request_payload=json_payload,
                            initial_bundle=bundle,
                            send_followup_request=_send_followup,
                            provider=provider,
                            response_params=response_params,
                            response_template=response_template,
                            writer=writer,
                            cache=cache,
                            resolved_executables=resolved_executables,
                        )

                    decoded = json.dumps(bundle, indent=2, ensure_ascii=False)
                return resp.getcode(), decoded
            except Exception:
                return resp.getcode(), str(body)
    except Exception as exc:
        try:
            from urllib import error as _urlerror

            if isinstance(exc, _urlerror.HTTPError):
                response_body = ""
                try:
                    raw_body = exc.read()
                    if isinstance(raw_body, bytes):
                        response_body = raw_body.decode("utf-8", errors="replace").strip()
                except Exception:
                    response_body = ""

                return exc.code, _format_http_error(exc.code, str(exc.reason), response_body)
        except Exception:
            pass

        if isinstance(exc, TimeoutError):
            return 0, f"timeout-error: request timed out after {timeout_seconds}s"

        return 0, f"urllib-error: {exc}"
