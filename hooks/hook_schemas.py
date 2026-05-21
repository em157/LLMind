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
            name="download_url",
            description=(
                "Download a file (image, audio, video, document, etc.) from a direct HTTP(S) URL to a specified path. "
                "Supports all common media types and credible file extensions found on the web."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "maxLength": 2048},
                    "save_path": {"type": "string", "maxLength": 512},
                    "overwrite": {"type": "boolean"},
                    "allowed_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of allowed file extensions or MIME types (e.g., .jpg, .png, .mp4, .mp3, .pdf, image/*, video/*, audio/*, application/pdf, etc.)"
                    },
                    "reason": {"type": "string"},
                },
                "required": ["url", "save_path"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="parse_html_for_media",
            description=(
                "Parse HTML and extract all credible media URLs (images, audio, video, documents) from <img>, <audio>, <video>, <source>, <a>, and CSS backgrounds. "
                "Returns a structured list of URLs with type, extension, and context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["extract"]},
                    "html": {"type": "string", "maxLength": 1000000},
                    "media_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of media types/extensions to extract (e.g., .jpg, .png, .mp4, .mp3, .pdf, image/*, video/*, audio/*, application/pdf, etc.)"
                    },
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                    "reason": {"type": "string"},
                },
                "required": ["action", "html"],
                "additionalProperties": False,
            },
            strict=False,
        ),
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
            description=(
                "Launch allowlisted Windows applications for UI workflows. "
                "To open a file in a non-browser app (e.g. paint/notepad/wordpad), pass the "
                "absolute file path as the first element of 'args' — e.g. "
                "{\"action\":\"start\",\"app\":\"paint\",\"args\":[\"C:\\\\path\\\\to\\\\image.jpg\"]}. "
                "Browser apps (edge/chrome/firefox) use 'url' instead and only accept safe launch flags in 'args'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["start"]},
                    "app": {
                        "type": "string",
                        "enum": ["notepad", "wordpad", "paint", "mspaint", "edge", "chrome", "firefox"],
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
            name="fetch_webpage_html",
            description="Download an http/https page HTML, optionally parse interactive elements, and save artifacts",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["download_parse"]},
                    "url": {"type": "string", "maxLength": 2048},
                    "parse_action": {"type": "string", "enum": ["none", "extract", "comment_inputs"]},
                    "parser_engine": {"type": "string", "enum": ["auto", "beautifulsoup", "html_parser"]},
                    "include_hidden": {"type": "boolean"},
                    "max_elements": {"type": "integer", "minimum": 1, "maximum": 500},
                    "max_chars": {"type": "integer", "minimum": 1000, "maximum": 2000000},
                    "save_filename": {"type": "string", "maxLength": 128},
                    "reason": {"type": "string"},
                },
                "required": ["action", "url"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="parse_html_content",
            description=(
                "Parse HTML from inline text or a local file and extract interactive elements, "
                "including likely comment input fields"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["extract", "comment_inputs"]},
                    "html": {"type": "string", "maxLength": 500000},
                    "filepath": {"type": "string", "maxLength": 512},
                    "include_hidden": {"type": "boolean"},
                    "max_elements": {"type": "integer", "minimum": 1, "maximum": 500},
                    "reason": {"type": "string"},
                },
                "required": ["action"],
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
            name="query_browser_history",
            description=(
                "Query Chrome/Edge History SQLite database using granular time-window tools "
                "(for example: last_14_days) and return structured analytics"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["summary", "top_domains", "top_pages", "daily_timeline"],
                    },
                    "filepath": {"type": "string", "maxLength": 512},
                    "window": {
                        "type": "string",
                        "enum": [
                            "last_24_hours",
                            "last_7_days",
                            "last_14_days",
                            "last_30_days",
                            "custom_days",
                            "custom_range",
                        ],
                    },
                    "days": {"type": "integer", "minimum": 1, "maximum": 365},
                    "start_date": {
                        "type": "string",
                        "description": "UTC date/datetime in ISO format, e.g. 2026-05-01 or 2026-05-01T00:00:00Z",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "UTC date/datetime in ISO format, e.g. 2026-05-14 or 2026-05-14T23:59:59Z",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "reason": {"type": "string"},
                },
                "required": ["action", "filepath"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="capture_and_ocr_screen",
            description=(
                "Capture full screen or region, run OCR, and return normalized observations "
                "for downstream planning"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["capture"]},
                    "region": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "width": {"type": "integer", "minimum": 1},
                            "height": {"type": "integer", "minimum": 1},
                        },
                        "required": ["x", "y", "width", "height"],
                        "additionalProperties": False,
                    },
                    "include_image": {"type": "boolean"},
                    "ocr_engine": {"type": "string", "enum": ["auto", "paddle", "tesseract", "winrt"]},
                    "reason": {"type": "string"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="analyze_ui_with_vision_model",
            description=(
                "Analyze OCR/screen observations and produce a strict decision with confidence, "
                "fallback, and discrimination details"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["analyze"]},
                    "objective": {"type": "string", "maxLength": 500},
                    "image_ref": {"type": "string", "maxLength": 2048},
                    "ocr_blocks": {
                        "type": "array",
                        "maxItems": 500,
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "maxLength": 1000},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                "bbox": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "minItems": 4,
                                    "maxItems": 4,
                                },
                            },
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                    },
                    "allowed_actions": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["click", "type", "hotkey", "scroll", "noop"],
                        },
                        "maxItems": 5,
                    },
                    "reason": {"type": "string"},
                },
                "required": ["action", "objective"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="verify_ui_change",
            description="Validate expected UI state transition using OCR observations with bounded retries",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["verify"]},
                    "expected_text_any": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 200},
                        "maxItems": 20,
                    },
                    "expected_text_all": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 200},
                        "maxItems": 20,
                    },
                    "region": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "width": {"type": "integer", "minimum": 1},
                            "height": {"type": "integer", "minimum": 1},
                        },
                        "required": ["x", "y", "width", "height"],
                        "additionalProperties": False,
                    },
                    "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 10000},
                    "reason": {"type": "string"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="detect_visual_objects",
            description=(
                "Detect visual objects from a screenshot/image reference using OCR + optional CV contour detection; "
                "returns normalized candidates with confidence and labels"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["detect"]},
                    "image_ref": {"type": "string", "maxLength": 2048},
                    "region": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "width": {"type": "integer", "minimum": 1},
                            "height": {"type": "integer", "minimum": 1},
                        },
                        "required": ["x", "y", "width", "height"],
                        "additionalProperties": False,
                    },
                    "objective": {"type": "string", "maxLength": 500},
                    "include_ocr": {"type": "boolean"},
                    "max_objects": {"type": "integer", "minimum": 1, "maximum": 200},
                    "reason": {"type": "string"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            strict=False,
        ),
        HookSchema(
            name="validate_click_target",
            description=(
                "Validate click target deterministically before executing a UI click using OCR and bounds checks"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["validate"]},
                    "target": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["text", "bbox", "point"]},
                            "value": {"type": "string", "maxLength": 500},
                            "bbox": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "minItems": 4,
                                "maxItems": 4,
                            },
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                        },
                        "required": ["type"],
                        "additionalProperties": False,
                    },
                    "region": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "width": {"type": "integer", "minimum": 1},
                            "height": {"type": "integer", "minimum": 1},
                        },
                        "required": ["x", "y", "width", "height"],
                        "additionalProperties": False,
                    },
                    "min_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["action", "target"],
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



