from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Allow direct execution from the response/ directory by adding the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks.hook_registry import HookContext, HookRegistry, HookResult


@dataclass(frozen=True)
class ModelCapabilityProfile:
    provider_family: str
    key_models: str
    json_schema_constraint: str
    strict_tool_constraint: str


MODEL_CAPABILITY_TABLE: Dict[str, ModelCapabilityProfile] = {
    "openai": ModelCapabilityProfile(
        provider_family="OpenAI",
        key_models="GPT-4o, GPT-4o-mini, GPT-5.4, GPT-5.4 Thinking/Pro",
        json_schema_constraint="response_format with strict schema adherence",
        strict_tool_constraint="tools with strict: true",
    ),
    "anthropic": ModelCapabilityProfile(
        provider_family="Anthropic",
        key_models="Claude 3.5 Sonnet, Claude 4.5, Claude 4.6/4.7 series",
        json_schema_constraint="output_config.format with JSON schema grammar restrictions",
        strict_tool_constraint="Native tool definition with strict schema validation",
    ),
    "gemini": ModelCapabilityProfile(
        provider_family="Google Gemini",
        key_models="Gemini 2.5 Flash/Pro, Gemini 3.1 Pro (Preview)",
        json_schema_constraint="response_mime_type + response_schema object mapping",
        strict_tool_constraint="Function declarations with structural execution rules",
    ),
    "xai": ModelCapabilityProfile(
        provider_family="xAI",
        key_models="Grok 2, Grok 3 series",
        json_schema_constraint="OpenAI-compatible response_format",
        strict_tool_constraint="Supports custom local tool schemas and server-side features",
    ),
    "mistral": ModelCapabilityProfile(
        provider_family="Mistral AI",
        key_models="Mistral Large 2, Codestral",
        json_schema_constraint="Native JSON mode (response_format)",
        strict_tool_constraint="Strict tool use configurations",
    ),
    "deepseek": ModelCapabilityProfile(
        provider_family="DeepSeek",
        key_models="DeepSeek-V3, DeepSeek-R1",
        json_schema_constraint="OpenAI-compatible JSON mode",
        strict_tool_constraint="Standard function calling with client-side verification",
    ),
}


def get_model_capability_table() -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for key in ("openai", "anthropic", "gemini", "xai", "mistral", "deepseek"):
        profile = MODEL_CAPABILITY_TABLE[key]
        rows.append(
            {
                "provider": profile.provider_family,
                "key_models": profile.key_models,
                "json_schema_constraint": profile.json_schema_constraint,
                "strict_tool_constraint": profile.strict_tool_constraint,
            }
        )
    return rows


def _parse_payload(payload: Any) -> Optional[Dict[str, Any]]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
    return None


def _decode_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {"raw": raw}
    return {}


def _extract_openai_style_tool_calls(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    # Chat completions / xAI compatible tool_calls
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    fn = tool_call.get("function")
                    if not isinstance(fn, dict):
                        continue
                    name = fn.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    calls.append(
                        {
                            "hook_name": name.strip(),
                            "args": _decode_arguments(fn.get("arguments")),
                            "source": "openai_chat_tool_call",
                        }
                    )

            # Legacy function_call shape
            function_call = message.get("function_call")
            if isinstance(function_call, dict):
                name = function_call.get("name")
                if isinstance(name, str) and name.strip():
                    calls.append(
                        {
                            "hook_name": name.strip(),
                            "args": _decode_arguments(function_call.get("arguments")),
                            "source": "openai_legacy_function_call",
                        }
                    )

    # OpenAI Responses API output function call items
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "function_call":
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            calls.append(
                {
                    "hook_name": name.strip(),
                    "args": _decode_arguments(item.get("arguments")),
                    "source": "openai_responses_function_call",
                }
            )

    return calls


def _extract_anthropic_tool_calls(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    content = payload.get("content")
    if not isinstance(content, list):
        return calls
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "tool_use":
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        calls.append(
            {
                "hook_name": name.strip(),
                "args": _decode_arguments(item.get("input")),
                "source": "anthropic_tool_use",
            }
        )
    return calls


def _extract_gemini_tool_calls(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return calls

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            function_call = part.get("functionCall") or part.get("function_call")
            if not isinstance(function_call, dict):
                continue
            name = function_call.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            calls.append(
                {
                    "hook_name": name.strip(),
                    "args": _decode_arguments(function_call.get("args")),
                    "source": "gemini_function_call",
                }
            )
    return calls


def _extract_schema_hook_calls(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fallback parser for strict JSON responses that include hook declarations."""
    calls: List[Dict[str, Any]] = []

    raw_calls = payload.get("hook_calls")
    if isinstance(raw_calls, list):
        for item in raw_calls:
            if not isinstance(item, dict):
                continue
            hook_name = item.get("hook") or item.get("hook_name") or item.get("name")
            if not isinstance(hook_name, str) or not hook_name.strip():
                continue
            calls.append(
                {
                    "hook_name": hook_name.strip(),
                    "args": _decode_arguments(item.get("args") or item.get("arguments") or {}),
                    "source": "schema_hook_calls",
                }
            )

    single_hook = payload.get("hook") or payload.get("hook_name")
    if isinstance(single_hook, str) and single_hook.strip():
        calls.append(
            {
                "hook_name": single_hook.strip(),
                "args": _decode_arguments(payload.get("args") or payload.get("arguments") or {}),
                "source": "schema_single_hook",
            }
        )

    return calls


def extract_hook_calls_from_response(payload: Any, provider: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Extract hook calls from provider-specific strict tool-call/JSON formats."""
    parsed = _parse_payload(payload)
    if parsed is None:
        return [], ["Response was not JSON/object; no structured hook calls extracted"]

    provider_key = (provider or "").strip().lower()
    calls: List[Dict[str, Any]] = []
    warnings: List[str] = []

    if provider_key in {"openai", "xai", "deepseek", "mistral"}:
        calls.extend(_extract_openai_style_tool_calls(parsed))
    if provider_key == "anthropic":
        calls.extend(_extract_anthropic_tool_calls(parsed))
    if provider_key == "gemini":
        calls.extend(_extract_gemini_tool_calls(parsed))

    # Schema fallback regardless of provider.
    if not calls:
        calls.extend(_extract_schema_hook_calls(parsed))

    if not calls:
        warnings.append("No hook calls found in structured response")

    return calls, warnings


def process_model_response_with_hooks(
    payload: Any,
    provider: str,
    registry: HookRegistry,
    app_data_dir: Path,
    execute: bool = False,
    resolved_executables: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Parse, validate, and optionally execute hook calls from model responses."""
    calls, warnings = extract_hook_calls_from_response(payload, provider)
    valid_hooks = set(registry.list_hook_names())

    validated_calls: List[Dict[str, Any]] = []
    validation_errors: List[str] = []
    for call in calls:
        name = call.get("hook_name")
        if name not in valid_hooks:
            validation_errors.append(f"Unknown hook requested by model: {name}")
            continue
        validated_calls.append(call)

    hook_results: List[HookResult] = []
    allow_ui_actions = os.getenv("LLMIND_ENABLE_UI_HOOKS", "0").strip() == "1"
    if execute and validated_calls:
        base_extras: Dict[str, Any] = {
            "allow_ui_actions": allow_ui_actions,
            "registry": registry,
        }
        if isinstance(resolved_executables, dict) and resolved_executables:
            base_extras["resolved_executables"] = resolved_executables
        context: HookContext = registry.build_context(
            app_data_dir,
            extras=base_extras,
        )
        for call in validated_calls:
            context.extras["hook_args"] = call.get("args", {})
            context.extras["hook_source"] = call.get("source")
            result = registry.execute(call["hook_name"], context)
            hook_results.append(result)

    profile = MODEL_CAPABILITY_TABLE.get(provider.lower())
    profile_view = None
    if profile is not None:
        profile_view = {
            "provider": profile.provider_family,
            "key_models": profile.key_models,
            "json_schema_constraint": profile.json_schema_constraint,
            "strict_tool_constraint": profile.strict_tool_constraint,
        }

    return {
        "provider": provider,
        "capability_profile": profile_view,
        "hook_calls": calls,
        "validated_hook_calls": validated_calls,
        "warnings": warnings,
        "validation_errors": validation_errors,
        "executed": execute,
        "execution_flags": {"allow_ui_actions": allow_ui_actions},
        "hook_results": [
            {
                "hook_name": result.hook_name,
                "success": result.success,
                "message": result.message,
                "details": result.details,
            }
            for result in hook_results
        ],
    }
