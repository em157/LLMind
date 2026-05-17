from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class HookSchema:
    name: str
    description: str
    parameters: Dict[str, object]
    strict: bool = True


def get_hook_schemas() -> List[HookSchema]:
    return [
        HookSchema(
            name="filesystem_access",
            description="Validate file write/read/delete in appdata",
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="registry_settings",
            description="Validate HKCU registry settings read/write",
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="windows_ui_action",
            description="Perform guarded Win10/11 UI actions (find/activate/move/click/type)",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["find_window", "activate_window", "move_window", "click", "type_text"],
                    },
                    "reason": {"type": "string"},
                    "title_contains": {"type": "string"},
                    "class_name": {"type": "string"},
                    "hwnd": {"type": "integer", "minimum": 1},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "width": {"type": "integer", "minimum": 1},
                    "height": {"type": "integer", "minimum": 1},
                    "button": {"type": "string", "enum": ["left", "right"]},
                    "text": {"type": "string", "maxLength": 500},
                    "press_enter": {"type": "boolean"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="launch_process",
            description="Launch allowlisted Windows applications for UI workflows",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["start"]},
                    "app": {
                        "type": "string",
                        "enum": ["notepad", "wordpad", "edge", "chrome", "firefox"],
                    },
                    "url": {"type": "string", "maxLength": 2048},
                    "args": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 256},
                        "maxItems": 12,
                    },
                    "reason": {"type": "string"},
                },
                "required": ["action", "app"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="capture_screenshot",
            description="Capture a full-screen screenshot and store it in appdata artifacts",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["capture_fullscreen", "capture_window", "capture_region"],
                    },
                    "filename": {"type": "string", "maxLength": 128},
                    "title_contains": {"type": "string", "maxLength": 128},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "width": {"type": "integer", "minimum": 1},
                    "height": {"type": "integer", "minimum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="browser_navigation",
            description="Open an allowlisted browser to a specific URL with safe launch args",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["open_url"]},
                    "browser": {"type": "string", "enum": ["edge", "chrome", "firefox"]},
                    "url": {"type": "string", "maxLength": 2048},
                    "args": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 256},
                        "maxItems": 12,
                    },
                    "reason": {"type": "string"},
                },
                "required": ["action", "browser", "url"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="system_command",
            description="Run guarded allowlisted system commands for diagnostics",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["execute"]},
                    "command": {
                        "type": "string",
                        "enum": ["whoami", "hostname", "ipconfig", "tasklist", "systeminfo", "ping"],
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 64},
                        "maxItems": 8,
                    },
                    "reason": {"type": "string"},
                },
                "required": ["action", "command"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="orchestrate_workflow",
            description="Execute a short sequence of allowed hooks in-order",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["run_sequence"]},
                    "stop_on_error": {"type": "boolean"},
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "hook": {"type": "string"},
                                "args": {"type": "object"},
                            },
                            "required": ["hook"],
                            "additionalProperties": False,
                        },
                    },
                    "reason": {"type": "string"},
                },
                "required": ["action", "steps"],
                "additionalProperties": False,
            },
            strict=False,
        ),
    ]


def get_hook_schema_map() -> Dict[str, HookSchema]:
    return {schema.name: schema for schema in get_hook_schemas()}
