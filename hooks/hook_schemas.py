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
            name="windows_metrics",
            description="Get Windows 10/11 display metrics including work area and virtual screen",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get_display_metrics"]},
                    "reason": {"type": "string"},
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
                    "hwnd": {"type": "integer", "minimum": 1},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "width": {"type": "integer", "minimum": 1},
                    "height": {"type": "integer", "minimum": 1},
                    "expected_text_any": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 128},
                        "maxItems": 20,
                    },
                    "expected_text_all": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 128},
                        "maxItems": 20,
                    },
                    "ocr_notes": {"type": "string", "maxLength": 500},
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
            name="web_fetch_parse",
            description="Fetch an HTTP/HTTPS page and parse links/images with BeautifulSoup",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["fetch_parse"]},
                    "url": {"type": "string", "maxLength": 2048},
                    "max_items": {"type": "integer", "minimum": 1, "maximum": 100},
                    "reason": {"type": "string"},
                },
                "required": ["action", "url"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="download_remote_file",
            description="Download a remote HTTP/HTTPS file into Desktop/AppData safe directories",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["download"]},
                    "url": {"type": "string", "maxLength": 2048},
                    "filepath": {"type": "string", "maxLength": 512},
                    "overwrite": {"type": "boolean"},
                    "max_bytes": {"type": "integer", "minimum": 1, "maximum": 10485760},
                    "reason": {"type": "string"},
                },
                "required": ["action", "url", "filepath"],
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
        HookSchema(
            name="read_file",
            description="Read text file contents from Desktop and AppData directories",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read"]},
                    "filepath": {"type": "string", "maxLength": 512},
                    "max_chars": {"type": "integer", "minimum": 100, "maximum": 50000},
                    "reason": {"type": "string"},
                },
                "required": ["action", "filepath"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="list_directory",
            description="List files in a directory on Desktop or AppData",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list"]},
                    "dirpath": {"type": "string", "maxLength": 512},
                    "extension": {"type": "string", "maxLength": 10},
                    "reason": {"type": "string"},
                },
                "required": ["action", "dirpath"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="write_file",
            description="Write text file contents to Desktop and AppData directories",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["write"]},
                    "filepath": {"type": "string", "maxLength": 512},
                    "content": {"type": "string", "maxLength": 100000},
                    "overwrite": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["action", "filepath", "content"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="send_email_smtp",
            description=(
                "Send an email via SMTP. Credentials are read from environment variables "
                "(LLMIND_SMTP_HOST, LLMIND_SMTP_PORT, LLMIND_SMTP_USER, LLMIND_SMTP_PASSWORD, "
                "LLMIND_SMTP_FROM). Set LLMIND_ENABLE_EMAIL_HOOKS=1 to enable."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["send"]},
                    "to": {
                        "type": "string",
                        "maxLength": 1024,
                        "description": "Recipient email address(es), comma-separated",
                    },
                    "subject": {"type": "string", "maxLength": 256},
                    "body": {"type": "string", "maxLength": 50000},
                    "cc": {"type": "string", "maxLength": 1024},
                    "bcc": {"type": "string", "maxLength": 1024},
                    "html": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["action", "to", "subject", "body"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="send_email_outlook",
            description=(
                "Send an email via the local Microsoft Outlook COM interface (Windows only). "
                "Uses the currently signed-in Outlook profile. "
                "Set LLMIND_ENABLE_EMAIL_HOOKS=1 to enable. Requires pywin32."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["send"]},
                    "to": {
                        "type": "string",
                        "maxLength": 1024,
                        "description": "Recipient email address(es), comma or semicolon separated",
                    },
                    "subject": {"type": "string", "maxLength": 256},
                    "body": {"type": "string", "maxLength": 50000},
                    "cc": {"type": "string", "maxLength": 1024},
                    "bcc": {"type": "string", "maxLength": 1024},
                    "html": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["action", "to", "subject", "body"],
                "additionalProperties": False,
            },
            strict=False,
        ),
    ]


def get_hook_schema_map() -> Dict[str, HookSchema]:
    return {schema.name: schema for schema in get_hook_schemas()}
