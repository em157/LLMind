from __future__ import annotations

from typing import Dict, List

from hooks.hook_schemas import HookSchema, get_hook_schemas


def _strip_gemini_unsupported_schema_fields(value: object) -> object:
    """Recursively drop JSON schema fields Gemini tool declarations reject."""
    if isinstance(value, dict):
        cleaned: Dict[str, object] = {}
        for key, nested in value.items():
            if key == "additionalProperties":
                continue
            cleaned[key] = _strip_gemini_unsupported_schema_fields(nested)
        return cleaned
    if isinstance(value, list):
        return [_strip_gemini_unsupported_schema_fields(item) for item in value]
    return value


def render_openai_tools(schemas: List[HookSchema] | None = None) -> List[Dict[str, object]]:
    """Chat Completions API tool format: name nested inside 'function'."""
    schemas = schemas or get_hook_schemas()
    tools: List[Dict[str, object]] = []
    for schema in schemas:
        function_def: Dict[str, object] = {
            "name": schema.name,
            "description": schema.description,
            "parameters": schema.parameters,
        }
        if schema.strict:
            function_def["strict"] = True
        tools.append({"type": "function", "function": function_def})
    return tools


def render_openai_responses_tools(schemas: List[HookSchema] | None = None) -> List[Dict[str, object]]:
    """Responses API tool format: name at top level."""
    schemas = schemas or get_hook_schemas()
    tools: List[Dict[str, object]] = []
    for schema in schemas:
        tool: Dict[str, object] = {
            "type": "function",
            "name": schema.name,
            "description": schema.description,
            "parameters": schema.parameters,
        }
        if schema.strict:
            tool["strict"] = True
        tools.append(tool)
    return tools


def render_anthropic_tools(schemas: List[HookSchema] | None = None) -> List[Dict[str, object]]:
    schemas = schemas or get_hook_schemas()
    tools: List[Dict[str, object]] = []
    for schema in schemas:
        tools.append(
            {
                "name": schema.name,
                "description": schema.description,
                "input_schema": schema.parameters,
            }
        )
    return tools


def render_gemini_tools(schemas: List[HookSchema] | None = None) -> List[Dict[str, object]]:
    schemas = schemas or get_hook_schemas()
    declarations: List[Dict[str, object]] = []
    for schema in schemas:
        declarations.append(
            {
                "name": schema.name,
                "description": schema.description,
                "parameters": _strip_gemini_unsupported_schema_fields(schema.parameters),
            }
        )
    return declarations


def render_provider_tools(
    provider: str,
    schemas: List[HookSchema] | None = None,
    tool_choice: Dict[str, object] | str | None = None,
    response_template: str | None = None,
) -> Dict[str, object]:
    provider_key = (provider or "").strip().lower()
    if provider_key in {"openai", "xai", "deepseek", "mistral"}:
        if response_template == "openai_responses":
            # Responses API: name is a top-level field, no tool_choice wrapper.
            return {"tools": render_openai_responses_tools(schemas)}
        payload: Dict[str, object] = {"tools": render_openai_tools(schemas)}
        payload["tool_choice"] = tool_choice or "auto"
        return payload
    if provider_key == "anthropic":
        return {"tools": render_anthropic_tools(schemas)}
    if provider_key == "gemini":
        return {
            "tools": [
                {
                    "function_declarations": render_gemini_tools(schemas),
                }
            ]
        }
    return {"tools": []}
