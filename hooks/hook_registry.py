from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from ctypes import wintypes


def _resolve_executable_from_context(context: HookContext, executable: str) -> str:
    """Prefer absolute executable paths from context extras when available."""
    resolved = context.extras.get("resolved_executables")
    if not isinstance(resolved, dict):
        return executable

    normalized_name = Path(executable).name.lower()
    candidates = [resolved.get(executable), resolved.get(normalized_name)]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            candidate_path = Path(candidate)
            if candidate_path.exists() and candidate_path.is_file():
                return str(candidate_path)
    return executable


@dataclass
class HookContext:
    """Execution context shared across hooks."""

    app_data_dir: Path
    app_name: str = "LLMind"
    extras: Dict[str, object] = field(default_factory=dict)


@dataclass
class HookResult:
    """Structured result returned by every hook execution."""

    hook_name: str
    success: bool
    message: str
    details: Dict[str, object] = field(default_factory=dict)


class BaseHook:
    """Common execute(context) contract for all hooks."""

    name = "base"
    description = "Base hook"

    def execute(self, context: HookContext) -> HookResult:
        raise NotImplementedError


class FileSystemAccessHook(BaseHook):
    name = "filesystem_access"
    description = "Validate file write/read/delete in appdata"

    def execute(self, context: HookContext) -> HookResult:
        test_file = context.app_data_dir / "hook_fs_validation.tmp"
        test_value = f"LLMind filesystem hook validation {int(time.time())}"
        try:
            context.app_data_dir.mkdir(parents=True, exist_ok=True)
            with test_file.open("w", encoding="utf-8") as handle:
                handle.write(test_value)
            with test_file.open("r", encoding="utf-8") as handle:
                read_back = handle.read()
            if read_back != test_value:
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="Read-back content mismatch",
                )
            test_file.unlink(missing_ok=True)
            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Read/write validated at {context.app_data_dir}",
                details={"path": str(context.app_data_dir)},
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"{exc.__class__.__name__}: {exc}",
            )


class RegistrySettingsHook(BaseHook):
    name = "registry_settings"
    description = "Validate HKCU registry settings read/write"

    def execute(self, context: HookContext) -> HookResult:
        if os.name != "nt":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Registry hooks only available on Windows",
            )

        try:
            import winreg  # type: ignore
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"winreg unavailable: {exc}",
            )

        subkey = rf"Software\\{context.app_name}"
        value_name = "HookValidation"
        test_value = f"ok-{int(time.time())}"

        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, test_value)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ) as key:
                current_value, current_type = winreg.QueryValueEx(key, value_name)

            if current_type != winreg.REG_SZ:
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="Registry value type mismatch",
                )
            if current_value != test_value:
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="Registry value mismatch",
                )

            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"HKCU\\{subkey} {value_name} read/write validated",
                details={"subkey": subkey, "value_name": value_name},
            )
        except PermissionError:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Access denied writing HKCU registry key",
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"{exc.__class__.__name__}: {exc}",
            )


class WindowsUIManipulationHook(BaseHook):
    name = "windows_ui_action"
    description = "Perform guarded Win10/11 UI actions (find/activate/move/click/type)"

    _ALLOWED_ACTIONS = {"find_window", "activate_window", "move_window", "click", "type_text"}
    _ALLOWED_BUTTONS = {"left", "right"}
    _MAX_TEXT_LENGTH = 500

    def execute(self, context: HookContext) -> HookResult:
        if os.name != "nt":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Windows UI hook is only available on Windows 10/11",
            )

        # extras["allow_ui_actions"] can explicitly override the env var (used by self-test).
        if "allow_ui_actions" in context.extras:
            ui_allowed = bool(context.extras["allow_ui_actions"])
        else:
            ui_allowed = os.getenv("LLMIND_ENABLE_UI_HOOKS", "0") == "1"
        if not ui_allowed:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="UI action execution disabled. Set LLMIND_ENABLE_UI_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        try:
            if action == "find_window":
                return self._execute_find_window(args)
            if action == "activate_window":
                return self._execute_activate_window(args)
            if action == "move_window":
                return self._execute_move_window(args)
            if action == "click":
                return self._execute_click(args)
            return self._execute_type_text(args)
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"{exc.__class__.__name__}: {exc}",
            )

    def _execute_find_window(self, args: Dict[str, Any]) -> HookResult:
        title_contains = str(args.get("title_contains", "")).strip()
        class_name = str(args.get("class_name", "")).strip()
        handles = self._find_window_handles(title_contains=title_contains, class_name=class_name)
        if not handles:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="No matching window found",
            )
        hwnd = handles[0]
        return HookResult(
            hook_name=self.name,
            success=True,
            message=f"Matched {len(handles)} window(s)",
            details={
                "action": "find_window",
                "hwnd": hwnd,
                "title": self._get_window_text(hwnd),
                "matches": len(handles),
            },
        )

    def _execute_activate_window(self, args: Dict[str, Any]) -> HookResult:
        hwnd = self._resolve_target_hwnd(args)
        if hwnd is None:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="No matching target window for activate_window",
            )
        if not self._activate_window(hwnd):
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Failed to activate window hwnd={hwnd}",
            )
        return HookResult(
            hook_name=self.name,
            success=True,
            message=f"Activated window hwnd={hwnd}",
            details={"action": "activate_window", "hwnd": hwnd, "title": self._get_window_text(hwnd)},
        )

    def _execute_move_window(self, args: Dict[str, Any]) -> HookResult:
        hwnd = self._resolve_target_hwnd(args)
        if hwnd is None:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="No matching target window for move_window",
            )

        x = self._coerce_int(args.get("x"), "x")
        y = self._coerce_int(args.get("y"), "y")
        width = self._coerce_int(args.get("width"), "width")
        height = self._coerce_int(args.get("height"), "height")
        if width <= 0 or height <= 0:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="width and height must be positive integers",
            )

        ok = ctypes.windll.user32.MoveWindow(wintypes.HWND(hwnd), x, y, width, height, True)
        if not ok:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"MoveWindow failed for hwnd={hwnd}",
            )
        return HookResult(
            hook_name=self.name,
            success=True,
            message=f"Moved window hwnd={hwnd} to ({x}, {y}) {width}x{height}",
            details={
                "action": "move_window",
                "hwnd": hwnd,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            },
        )

    def _execute_click(self, args: Dict[str, Any]) -> HookResult:
        x = self._coerce_int(args.get("x"), "x")
        y = self._coerce_int(args.get("y"), "y")
        button = str(args.get("button", "left")).strip().lower()
        if button not in self._ALLOWED_BUTTONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported click button '{button}'",
            )

        # Make process DPI-aware so coordinates are treated as physical pixels.
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

        # Use SendInput with ABSOLUTE + VIRTUALDESK flags so coordinates are
        # correctly normalized across the full virtual desktop (DPI-safe on 4K).
        SM_XVIRTUALSCREEN  = 76
        SM_YVIRTUALSCREEN  = 77
        SM_CXVIRTUALSCREEN = 78
        SM_CYVIRTUALSCREEN = 79
        virt_x0 = ctypes.windll.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        virt_y0 = ctypes.windll.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        virt_w  = ctypes.windll.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) or 1
        virt_h  = ctypes.windll.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) or 1
        norm_x  = int((x - virt_x0) * 65536 // virt_w)
        norm_y  = int((y - virt_y0) * 65536 // virt_h)

        MOUSEEVENTF_MOVE        = 0x0001
        MOUSEEVENTF_ABSOLUTE    = 0x8000
        MOUSEEVENTF_VIRTUALDESK = 0x4000
        MOUSEEVENTF_LEFTDOWN    = 0x0002
        MOUSEEVENTF_LEFTUP      = 0x0004
        MOUSEEVENTF_RIGHTDOWN   = 0x0008
        MOUSEEVENTF_RIGHTUP     = 0x0010
        INPUT_MOUSE             = 0

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx",          ctypes.c_long),
                ("dy",          ctypes.c_long),
                ("mouseData",   ctypes.c_ulong),
                ("dwFlags",     ctypes.c_ulong),
                ("time",        ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        class _InputUnion(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("_u",)
            _fields_ = [("type", ctypes.c_ulong), ("_u", _InputUnion)]

        move_flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        if button == "left":
            down_flags = MOUSEEVENTF_LEFTDOWN  | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
            up_flags   = MOUSEEVENTF_LEFTUP    | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        else:
            down_flags = MOUSEEVENTF_RIGHTDOWN | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
            up_flags   = MOUSEEVENTF_RIGHTUP   | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

        inputs = (INPUT * 3)(
            INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=norm_x, dy=norm_y, mouseData=0, dwFlags=move_flags,  time=0, dwExtraInfo=0)),
            INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=norm_x, dy=norm_y, mouseData=0, dwFlags=down_flags,  time=0, dwExtraInfo=0)),
            INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=norm_x, dy=norm_y, mouseData=0, dwFlags=up_flags,    time=0, dwExtraInfo=0)),
        )
        sent = ctypes.windll.user32.SendInput(3, inputs, ctypes.sizeof(INPUT))
        if sent != 3:
            last_err = ctypes.get_last_error()
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"SendInput failed: {sent}/3 events sent (last_error={last_err})",
            )

        return HookResult(
            hook_name=self.name,
            success=True,
            message=f"Clicked {button} at ({x}, {y}) via SendInput [norm={norm_x},{norm_y}]",
            details={
                "action": "click",
                "button": button,
                "x": x,
                "y": y,
                "norm_x": norm_x,
                "norm_y": norm_y,
                "virtual_screen": {"x0": virt_x0, "y0": virt_y0, "w": virt_w, "h": virt_h},
            },
        )

    def _execute_type_text(self, args: Dict[str, Any]) -> HookResult:
        text = str(args.get("text", ""))
        if not text:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="type_text requires non-empty 'text'",
            )
        if len(text) > self._MAX_TEXT_LENGTH:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"text too long (max {self._MAX_TEXT_LENGTH} chars)",
            )

        hwnd = self._resolve_target_hwnd(args)
        if hwnd is not None:
            self._activate_window(hwnd)

        foreground = ctypes.windll.user32.GetForegroundWindow()
        if not foreground:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="No foreground window available for type_text",
            )

        wm_char = 0x0102
        for ch in text:
            ctypes.windll.user32.PostMessageW(wintypes.HWND(foreground), wm_char, ord(ch), 0)

        if bool(args.get("press_enter", False)):
            vk_return = 0x0D
            wm_keydown = 0x0100
            wm_keyup = 0x0101
            ctypes.windll.user32.PostMessageW(wintypes.HWND(foreground), wm_keydown, vk_return, 0)
            ctypes.windll.user32.PostMessageW(wintypes.HWND(foreground), wm_keyup, vk_return, 0)

        return HookResult(
            hook_name=self.name,
            success=True,
            message=f"Typed {len(text)} character(s) into foreground window",
            details={"action": "type_text", "length": len(text), "foreground_hwnd": int(foreground)},
        )

    @staticmethod
    def _coerce_int(value: Any, field_name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"Invalid {field_name}: bool is not allowed")
        if not isinstance(value, int):
            raise ValueError(f"Missing or invalid integer field: {field_name}")
        return value

    def _resolve_target_hwnd(self, args: Dict[str, Any]) -> Optional[int]:
        raw_hwnd = args.get("hwnd")
        if isinstance(raw_hwnd, int) and raw_hwnd > 0:
            if ctypes.windll.user32.IsWindow(wintypes.HWND(raw_hwnd)):
                return raw_hwnd
            return None

        title_contains = str(args.get("title_contains", "")).strip()
        class_name = str(args.get("class_name", "")).strip()
        if not title_contains and not class_name:
            return None
        matches = self._find_window_handles(title_contains=title_contains, class_name=class_name)
        return matches[0] if matches else None

    def _find_window_handles(self, title_contains: str = "", class_name: str = "") -> List[int]:
        title_filter = title_contains.lower()
        class_filter = class_name.lower()
        handles: List[int] = []

        enum_windows = ctypes.windll.user32.EnumWindows
        is_visible = ctypes.windll.user32.IsWindowVisible
        get_text_len = ctypes.windll.user32.GetWindowTextLengthW
        get_text = ctypes.windll.user32.GetWindowTextW
        get_class = ctypes.windll.user32.GetClassNameW

        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _enum_proc(hwnd: int, _lparam: int) -> bool:
            if not is_visible(hwnd):
                return True

            length = get_text_len(hwnd)
            title = ""
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                get_text(hwnd, buf, length + 1)
                title = buf.value

            class_buf = ctypes.create_unicode_buffer(256)
            get_class(hwnd, class_buf, 256)
            window_class = class_buf.value

            if title_filter and title_filter not in title.lower():
                return True
            if class_filter and class_filter not in window_class.lower():
                return True

            handles.append(int(hwnd))
            return True

        callback = enum_proc_type(_enum_proc)
        enum_windows(callback, 0)
        return handles

    @staticmethod
    def _get_window_text(hwnd: int) -> str:
        get_text_len = ctypes.windll.user32.GetWindowTextLengthW
        get_text = ctypes.windll.user32.GetWindowTextW
        length = get_text_len(wintypes.HWND(hwnd))
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        get_text(wintypes.HWND(hwnd), buf, length + 1)
        return buf.value

    @staticmethod
    def _activate_window(hwnd: int) -> bool:
        user32 = ctypes.windll.user32
        sw_restore = 9
        user32.ShowWindow(wintypes.HWND(hwnd), sw_restore)
        return bool(user32.SetForegroundWindow(wintypes.HWND(hwnd)))


class WindowsMetricsHook(BaseHook):
    name = "windows_metrics"
    description = "Get Windows 10/11 display metrics including work area"

    _ALLOWED_ACTIONS = {"get_display_metrics"}
    _SPI_GETWORKAREA = 0x0030

    def execute(self, context: HookContext) -> HookResult:
        if os.name != "nt":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Windows metrics hook is only available on Windows 10/11",
            )

        if os.getenv("LLMIND_ENABLE_UI_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Windows metrics disabled. Set LLMIND_ENABLE_UI_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        try:
            user32 = ctypes.windll.user32
            primary_width = int(user32.GetSystemMetrics(0))
            primary_height = int(user32.GetSystemMetrics(1))
            virtual_x = int(user32.GetSystemMetrics(76))
            virtual_y = int(user32.GetSystemMetrics(77))
            virtual_width = int(user32.GetSystemMetrics(78))
            virtual_height = int(user32.GetSystemMetrics(79))

            work_rect = wintypes.RECT()
            work_area_ok = bool(
                user32.SystemParametersInfoW(
                    self._SPI_GETWORKAREA,
                    0,
                    ctypes.byref(work_rect),
                    0,
                )
            )

            dpi = None
            get_dpi_for_system = getattr(user32, "GetDpiForSystem", None)
            if callable(get_dpi_for_system):
                try:
                    dpi = int(get_dpi_for_system())
                except Exception:
                    dpi = None

            work_area = {
                "x": int(work_rect.left),
                "y": int(work_rect.top),
                "width": int(work_rect.right - work_rect.left),
                "height": int(work_rect.bottom - work_rect.top),
            }

            return HookResult(
                hook_name=self.name,
                success=True,
                message="Display metrics collected",
                details={
                    "action": action,
                    "primary_screen": {
                        "width": primary_width,
                        "height": primary_height,
                    },
                    "virtual_screen": {
                        "x": virtual_x,
                        "y": virtual_y,
                        "width": virtual_width,
                        "height": virtual_height,
                    },
                    "work_area": work_area,
                    "work_area_available": work_area_ok,
                    "dpi": dpi,
                },
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"{exc.__class__.__name__}: {exc}",
            )


class LaunchProcessHook(BaseHook):
    name = "launch_process"
    description = "Launch allowlisted Windows applications for UI workflows"

    _ALLOWED_ACTIONS = {"start"}
    _APP_EXECUTABLES = {
        "notepad": "notepad.exe",
        "wordpad": "write.exe",
        "paint": "mspaint.exe",
        "mspaint": "mspaint.exe",
        "edge": "msedge.exe",
        "chrome": "chrome.exe",
        "firefox": "firefox.exe",
    }
    _BROWSER_APPS = {"edge", "chrome", "firefox"}
    _ALLOWED_BROWSER_ARGS = {
        "--new-window",
        "--new-tab",
        "--start-maximized",
        "--inprivate",
        "--incognito",
        "-private-window",
    }

    def execute(self, context: HookContext) -> HookResult:
        if os.name != "nt":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="launch_process is only available on Windows 10/11",
            )

        if os.getenv("LLMIND_ENABLE_LAUNCH_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Process launch disabled. Set LLMIND_ENABLE_LAUNCH_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        app = str(args.get("app", "")).strip().lower()
        executable = self._APP_EXECUTABLES.get(app)
        if executable is None:
            allowed = ", ".join(sorted(self._APP_EXECUTABLES))
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Application is not allowlisted. Allowed: {allowed}",
            )

        launch_url = str(args.get("url", "")).strip()
        if launch_url:
            parsed = urlparse(launch_url)
            if parsed.scheme not in {"http", "https"}:
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="Only http/https URLs are allowed for launch_process",
                )
            if app not in self._BROWSER_APPS:
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="'url' is only supported for browser apps: chrome, edge, firefox",
                )

        launch_args: List[str] = []
        raw_args = args.get("args", [])
        if raw_args:
            if not isinstance(raw_args, list):
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="'args' must be a list of strings",
                )
            if len(raw_args) > 12:
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="Too many launch args (max 12)",
                )
            for raw_arg in raw_args:
                if not isinstance(raw_arg, str):
                    return HookResult(
                        hook_name=self.name,
                        success=False,
                        message="All launch args must be strings",
                    )
                arg = raw_arg.strip()
                if not arg:
                    continue
                if len(arg) > 256:
                    return HookResult(
                        hook_name=self.name,
                        success=False,
                        message="Each launch arg must be <= 256 characters",
                    )
                if app in self._BROWSER_APPS and arg not in self._ALLOWED_BROWSER_ARGS:
                    allowed_args = ", ".join(sorted(self._ALLOWED_BROWSER_ARGS))
                    return HookResult(
                        hook_name=self.name,
                        success=False,
                        message=f"Unsupported browser arg '{arg}'. Allowed: {allowed_args}",
                    )
                launch_args.append(arg)

        resolved_executable = _resolve_executable_from_context(context, executable)
        command = [resolved_executable]
        command.extend(launch_args)
        if launch_url:
            command.append(launch_url)

        try:
            process = subprocess.Popen(command, shell=False)
            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Launched {executable}",
                details={
                    "action": action,
                    "app": app,
                    "exe": executable,
                    "resolved_exe": resolved_executable,
                    "command": command,
                    "url": launch_url or None,
                    "pid": process.pid,
                },
            )
        except FileNotFoundError:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Executable not found: {resolved_executable}",
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"{exc.__class__.__name__}: {exc}",
            )


class CaptureScreenshotHook(BaseHook):
    name = "capture_screenshot"
    description = "Capture a full-screen screenshot and store it in appdata artifacts"

    _ALLOWED_ACTIONS = {"capture_fullscreen", "capture_window", "capture_region"}

    def execute(self, context: HookContext) -> HookResult:
        if os.name != "nt":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="capture_screenshot is only available on Windows 10/11",
            )

        if os.getenv("LLMIND_ENABLE_UI_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Screenshot capture disabled. Set LLMIND_ENABLE_UI_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        raw_filename = str(args.get("filename", "")).strip()
        filename = self._sanitize_filename(raw_filename)
        target_dir = context.app_data_dir / "artifacts" / "screenshots"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filename

        try:
            x, y, width, height = self._resolve_capture_area(args, action)
        except ValueError as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=str(exc),
            )

        ps_path = str(target_path).replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            f"$x={x}; $y={y}; $w={width}; $h={height}; "
            "if($w -le 0 -or $h -le 0){ throw 'Invalid capture dimensions' }; "
            "$bmp=New-Object System.Drawing.Bitmap $w,$h; "
            "$g=[System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen($x,$y,0,0,$bmp.Size); "
            f"$bmp.Save('{ps_path}',[System.Drawing.Imaging.ImageFormat]::Png); "
            "$g.Dispose(); $bmp.Dispose();"
        )

        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                check=False,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message=f"Screenshot capture command failed: {stderr or 'unknown error'}",
                )
            if not target_path.exists() or target_path.stat().st_size <= 0:
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="Screenshot capture did not create a valid output file",
                )
            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Screenshot captured to {target_path}",
                details={
                    "action": action,
                    "capture_area": {"x": x, "y": y, "width": width, "height": height},
                    "path": str(target_path),
                    "filename": filename,
                    "size": target_path.stat().st_size,
                },
            )
        except subprocess.TimeoutExpired:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Screenshot capture timed out",
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"{exc.__class__.__name__}: {exc}",
            )

    @staticmethod
    def _sanitize_filename(raw: str) -> str:
        cleaned = Path(raw).name.strip() if raw else ""
        if not cleaned:
            cleaned = f"screenshot_{int(time.time())}.png"
        if not cleaned.lower().endswith(".png"):
            cleaned = f"{cleaned}.png"
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
        return cleaned or f"screenshot_{int(time.time())}.png"

    def _resolve_capture_area(self, args: Dict[str, Any], action: str) -> tuple:
        if action == "capture_fullscreen":
            user32 = ctypes.windll.user32
            x = int(user32.GetSystemMetrics(76))
            y = int(user32.GetSystemMetrics(77))
            width = int(user32.GetSystemMetrics(78))
            height = int(user32.GetSystemMetrics(79))
            if width <= 0 or height <= 0:
                width = int(user32.GetSystemMetrics(0))
                height = int(user32.GetSystemMetrics(1))
                x, y = 0, 0
            return x, y, width, height

        if action == "capture_region":
            x = self._coerce_int(args.get("x"), "x")
            y = self._coerce_int(args.get("y"), "y")
            width = self._coerce_int(args.get("width"), "width")
            height = self._coerce_int(args.get("height"), "height")
            if width <= 0 or height <= 0:
                raise ValueError("width and height must be positive integers")
            return x, y, width, height

        hwnd = None
        raw_hwnd = args.get("hwnd")
        if isinstance(raw_hwnd, int) and raw_hwnd > 0:
            hwnd = raw_hwnd
        if hwnd is None:
            title_contains = str(args.get("title_contains", "")).strip().lower()
            if title_contains:
                hwnd = self._find_hwnd_by_title_contains(title_contains)
        if hwnd is None:
            hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if hwnd <= 0:
            raise ValueError("No target window available for capture_window")

        rect = wintypes.RECT()
        ok = ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
        if not ok:
            raise ValueError(f"Failed to read window bounds for hwnd={hwnd}")
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            raise ValueError("Target window has invalid bounds")
        return int(rect.left), int(rect.top), width, height

    @staticmethod
    def _coerce_int(value: Any, field_name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"Invalid {field_name}: bool is not allowed")
        if not isinstance(value, int):
            raise ValueError(f"Missing or invalid integer field: {field_name}")
        return value

    @staticmethod
    def _find_hwnd_by_title_contains(title_contains: str) -> Optional[int]:
        matches: List[int] = []
        title_filter = title_contains.lower()
        is_visible = ctypes.windll.user32.IsWindowVisible
        get_text_len = ctypes.windll.user32.GetWindowTextLengthW
        get_text = ctypes.windll.user32.GetWindowTextW
        enum_windows = ctypes.windll.user32.EnumWindows
        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _enum_proc(hwnd: int, _lparam: int) -> bool:
            if not is_visible(hwnd):
                return True
            length = get_text_len(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            get_text(hwnd, buf, length + 1)
            title = buf.value.lower()
            if title_filter in title:
                matches.append(int(hwnd))
            return True

        callback = enum_proc_type(_enum_proc)
        enum_windows(callback, 0)
        return matches[0] if matches else None


class BrowserNavigationHook(BaseHook):
    name = "browser_navigation"
    description = "Open an allowlisted browser to a specific URL with safe launch args"

    _ALLOWED_ACTIONS = {"open_url"}
    _BROWSER_EXECUTABLES = {
        "edge": "msedge.exe",
        "chrome": "chrome.exe",
        "firefox": "firefox.exe",
    }
    _ALLOWED_BROWSER_ARGS = {
        "--new-window",
        "--new-tab",
        "--start-maximized",
        "--inprivate",
        "--incognito",
        "-private-window",
    }

    def execute(self, context: HookContext) -> HookResult:
        if os.name != "nt":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="browser_navigation is only available on Windows 10/11",
            )

        if os.getenv("LLMIND_ENABLE_LAUNCH_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Browser launch disabled. Set LLMIND_ENABLE_LAUNCH_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        browser = str(args.get("browser", "")).strip().lower()
        executable = self._BROWSER_EXECUTABLES.get(browser)
        if executable is None:
            allowed = ", ".join(sorted(self._BROWSER_EXECUTABLES))
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Browser is not allowlisted. Allowed: {allowed}",
            )

        url = str(args.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Only http/https URLs are allowed",
            )

        launch_args: List[str] = []
        raw_args = args.get("args", [])
        if raw_args:
            if not isinstance(raw_args, list):
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="'args' must be a list of strings",
                )
            if len(raw_args) > 12:
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="Too many launch args (max 12)",
                )
            for raw_arg in raw_args:
                if not isinstance(raw_arg, str):
                    return HookResult(
                        hook_name=self.name,
                        success=False,
                        message="All launch args must be strings",
                    )
                candidate = raw_arg.strip()
                if not candidate:
                    continue
                if candidate not in self._ALLOWED_BROWSER_ARGS:
                    allowed_args = ", ".join(sorted(self._ALLOWED_BROWSER_ARGS))
                    return HookResult(
                        hook_name=self.name,
                        success=False,
                        message=f"Unsupported browser arg '{candidate}'. Allowed: {allowed_args}",
                    )
                launch_args.append(candidate)

        resolved_executable = _resolve_executable_from_context(context, executable)
        command = [resolved_executable, *launch_args, url]
        try:
            process = subprocess.Popen(command, shell=False)
            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Opened URL in {browser}",
                details={
                    "action": action,
                    "browser": browser,
                    "exe": executable,
                    "resolved_exe": resolved_executable,
                    "url": url,
                    "command": command,
                    "pid": process.pid,
                },
            )
        except FileNotFoundError:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Executable not found: {resolved_executable}",
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"{exc.__class__.__name__}: {exc}",
            )


class ParseHTMLContentHook(BaseHook):
    name = "parse_html_content"
    description = "Parse HTML and extract interactive elements and likely comment inputs"

    _ALLOWED_ACTIONS = {"extract", "comment_inputs"}
    _SAFE_BASE_DIRS = [
        Path.home() / "Desktop",
        Path.home() / "Desktop" / "test_dir",
        Path.home() / "AppData" / "Roaming" / "LLMind",
        Path.home() / "AppData" / "Local" / "Temp",
    ]
    _MAX_HTML_CHARS = 500000
    _DEFAULT_MAX_ELEMENTS = 120

    def execute(self, context: HookContext) -> HookResult:
        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        source_label = "inline_html"
        html_text = str(args.get("html", ""))
        filepath = str(args.get("filepath", "")).strip()
        if filepath:
            if not self._is_safe_path(filepath):
                safe_dirs = ", ".join(str(d) for d in self._SAFE_BASE_DIRS)
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message=f"Access denied. HTML file must be in: {safe_dirs}",
                )
            file_path = Path(filepath)
            if not file_path.exists() or not file_path.is_file():
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message=f"HTML file not found: {filepath}",
                )
            html_text = file_path.read_text(encoding="utf-8", errors="replace")
            source_label = str(file_path)

        if not html_text.strip():
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Provide either non-empty 'html' or a valid 'filepath'",
            )

        if len(html_text) > self._MAX_HTML_CHARS:
            html_text = html_text[: self._MAX_HTML_CHARS]

        include_hidden = bool(args.get("include_hidden", False))
        max_elements = self._DEFAULT_MAX_ELEMENTS
        raw_limit = args.get("max_elements")
        if raw_limit is not None:
            try:
                max_elements = int(raw_limit)
            except Exception:
                max_elements = self._DEFAULT_MAX_ELEMENTS
        max_elements = min(max(max_elements, 1), 500)

        parser = self._InteractiveElementParser(include_hidden=include_hidden, max_elements=max_elements)
        parser.feed(html_text)
        parser.close()

        interactive_elements = parser.elements
        comment_candidates = [
            node for node in interactive_elements if self._is_comment_candidate(node)
        ]

        details: Dict[str, Any] = {
            "action": action,
            "source": source_label,
            "html_chars": len(html_text),
            "interactive_count": len(interactive_elements),
            "interactive_elements": interactive_elements,
            "comment_candidate_count": len(comment_candidates),
            "comment_input_candidates": comment_candidates,
            "discrimination": {
                "selected_strategy": "html.parser_interactive_extract",
                "include_hidden": include_hidden,
                "max_elements": max_elements,
                "stopped_early": parser.stopped_early,
            },
        }

        if action == "comment_inputs":
            return HookResult(
                hook_name=self.name,
                success=True,
                message=(
                    f"Found {len(comment_candidates)} likely comment input(s)"
                    if comment_candidates
                    else "No likely comment inputs found"
                ),
                details=details,
            )

        return HookResult(
            hook_name=self.name,
            success=True,
            message=f"Extracted {len(interactive_elements)} interactive element(s)",
            details=details,
        )

    def _is_safe_path(self, filepath: str) -> bool:
        try:
            resolved = Path(filepath).resolve()
            for safe_dir in self._SAFE_BASE_DIRS:
                safe_resolved = safe_dir.resolve()
                try:
                    resolved.relative_to(safe_resolved)
                    return True
                except ValueError:
                    continue
            return False
        except Exception:
            return False

    @staticmethod
    def _is_comment_candidate(node: Dict[str, Any]) -> bool:
        text_bits = [
            str(node.get("name", "")),
            str(node.get("id", "")),
            str(node.get("placeholder", "")),
            str(node.get("aria_label", "")),
            str(node.get("class", "")),
            str(node.get("autocomplete", "")),
            str(node.get("role", "")),
        ]
        blob = " ".join(text_bits).lower()
        comment_hints = [
            "comment",
            "reply",
            "message",
            "feedback",
            "discussion",
            "chat",
        ]
        has_hint = any(hint in blob for hint in comment_hints)

        tag = str(node.get("tag", "")).lower()
        input_type = str(node.get("input_type", "")).lower()
        contenteditable = bool(node.get("contenteditable", False))

        if tag == "textarea":
            return True
        if contenteditable and (has_hint or tag in {"div", "p", "span"}):
            return True
        if tag == "input" and input_type in {"text", "search", ""} and has_hint:
            return True
        if str(node.get("role", "")).lower() in {"textbox", "searchbox"} and has_hint:
            return True
        return False

    class _InteractiveElementParser(HTMLParser):
        _INTERACTIVE_TAGS = {"input", "textarea", "select", "button", "option"}

        def __init__(self, include_hidden: bool, max_elements: int) -> None:
            super().__init__(convert_charrefs=True)
            self.include_hidden = include_hidden
            self.max_elements = max_elements
            self.stopped_early = False
            self.elements: List[Dict[str, Any]] = []

        def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
            self._capture(tag, attrs)

        def handle_startendtag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
            self._capture(tag, attrs)

        def _capture(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
            if len(self.elements) >= self.max_elements:
                self.stopped_early = True
                return

            normalized = {str(k).lower(): ("" if v is None else str(v)) for k, v in attrs}
            role = normalized.get("role", "").strip().lower()
            contenteditable_raw = normalized.get("contenteditable")
            is_contenteditable = False
            if contenteditable_raw is not None:
                is_contenteditable = contenteditable_raw.strip().lower() in {"", "true"}
            is_interactive = (
                tag.lower() in self._INTERACTIVE_TAGS
                or role in {"textbox", "searchbox", "combobox"}
                or is_contenteditable
            )
            if not is_interactive:
                return

            input_type = normalized.get("type", "").strip().lower()
            hidden_raw = normalized.get("hidden")
            aria_hidden_raw = normalized.get("aria-hidden", "").strip().lower()
            is_hidden = (
                input_type == "hidden"
                or (hidden_raw is not None and hidden_raw.strip().lower() in {"", "hidden", "true"})
                or aria_hidden_raw == "true"
            )
            if is_hidden and not self.include_hidden:
                return

            self.elements.append(
                {
                    "tag": tag.lower(),
                    "input_type": input_type,
                    "name": normalized.get("name", "").strip(),
                    "id": normalized.get("id", "").strip(),
                    "class": normalized.get("class", "").strip(),
                    "placeholder": normalized.get("placeholder", "").strip(),
                    "aria_label": normalized.get("aria-label", "").strip(),
                    "role": role,
                    "autocomplete": normalized.get("autocomplete", "").strip(),
                    "contenteditable": is_contenteditable,
                }
            )


class FetchWebpageHTMLHook(BaseHook):
    name = "fetch_webpage_html"
    description = "Download webpage HTML, optionally parse interactive elements, and save artifacts"

    _ALLOWED_ACTIONS = {"download_parse"}
    _ALLOWED_PARSE_ACTIONS = {"none", "extract", "comment_inputs"}
    _ALLOWED_PARSERS = {"auto", "beautifulsoup", "html_parser"}
    _MAX_HTML_CHARS = 2000000
    _DEFAULT_HTML_CHARS = 500000
    _DEFAULT_MAX_ELEMENTS = 120

    def execute(self, context: HookContext) -> HookResult:
        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        url = str(args.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Only http/https URLs are allowed",
            )

        parse_action = str(args.get("parse_action", "extract")).strip().lower()
        if parse_action not in self._ALLOWED_PARSE_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=(
                    f"Unsupported parse_action '{parse_action}'. "
                    f"Allowed: {', '.join(sorted(self._ALLOWED_PARSE_ACTIONS))}"
                ),
            )

        parser_engine = str(args.get("parser_engine", "auto")).strip().lower()
        if parser_engine not in self._ALLOWED_PARSERS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported parser_engine '{parser_engine}'. Allowed: {', '.join(sorted(self._ALLOWED_PARSERS))}",
            )

        include_hidden = bool(args.get("include_hidden", False))

        max_elements = self._DEFAULT_MAX_ELEMENTS
        raw_max_elements = args.get("max_elements")
        if raw_max_elements is not None:
            try:
                max_elements = int(raw_max_elements)
            except Exception:
                max_elements = self._DEFAULT_MAX_ELEMENTS
        max_elements = min(max(max_elements, 1), 500)

        max_chars = self._DEFAULT_HTML_CHARS
        raw_max_chars = args.get("max_chars")
        if raw_max_chars is not None:
            try:
                max_chars = int(raw_max_chars)
            except Exception:
                max_chars = self._DEFAULT_HTML_CHARS
        max_chars = min(max(max_chars, 1000), self._MAX_HTML_CHARS)

        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )

        try:
            with urlopen(request, timeout=30) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                content_type = str(response.headers.get("Content-Type", ""))
                raw = response.read(max_chars + 1)
                charset = "utf-8"
                try:
                    maybe_charset = response.headers.get_content_charset()
                    if isinstance(maybe_charset, str) and maybe_charset.strip():
                        charset = maybe_charset.strip()
                except Exception:
                    charset = "utf-8"
        except HTTPError as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"HTTP error {exc.code}: {exc.reason}",
            )
        except URLError as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"URL error: {exc.reason}",
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"{exc.__class__.__name__}: {exc}",
            )

        truncated = len(raw) > max_chars
        if truncated:
            raw = raw[:max_chars]

        html_text = raw.decode(charset, errors="replace")

        safe_filename = "page.html"
        raw_name = str(args.get("save_filename", "")).strip()
        if raw_name:
            candidate = Path(raw_name).name.strip()
            if candidate:
                safe_filename = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in candidate)
                if not safe_filename:
                    safe_filename = "page.html"
        if not safe_filename.lower().endswith(".html"):
            safe_filename += ".html"

        run_dir = context.app_data_dir / "artifacts" / "html" / f"run_{uuid4().hex[:10]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        html_path = run_dir / safe_filename
        html_path.write_text(html_text, encoding="utf-8")

        interactive_elements: List[Dict[str, Any]] = []
        comment_candidates: List[Dict[str, Any]] = []
        parser_used = "none"
        bs4_available = False

        if parse_action != "none":
            effective_parser = parser_engine
            if effective_parser == "auto":
                try:
                    from bs4 import BeautifulSoup  # type: ignore
                    _ = BeautifulSoup
                    bs4_available = True
                    effective_parser = "beautifulsoup"
                except ImportError:
                    effective_parser = "html_parser"

            if effective_parser == "beautifulsoup":
                try:
                    from bs4 import BeautifulSoup  # type: ignore

                    parser_used = "beautifulsoup"
                    soup = BeautifulSoup(html_text, "html.parser")
                    tags = ["input", "textarea", "select", "button", "option"]
                    nodes = list(soup.find_all(tags))
                    nodes.extend(soup.find_all(attrs={"role": ["textbox", "searchbox", "combobox"]}))
                    nodes.extend(soup.find_all(attrs={"contenteditable": True}))

                    for node in nodes:
                        if len(interactive_elements) >= max_elements:
                            break
                        tag = str(getattr(node, "name", "") or "").lower()
                        attrs = getattr(node, "attrs", {}) or {}
                        role = str(attrs.get("role", "") or "").strip().lower()
                        input_type = str(attrs.get("type", "") or "").strip().lower()
                        contenteditable_raw = attrs.get("contenteditable")
                        is_contenteditable = contenteditable_raw is not None and str(contenteditable_raw).strip().lower() in {"", "true"}

                        hidden_raw = attrs.get("hidden")
                        aria_hidden_raw = str(attrs.get("aria-hidden", "") or "").strip().lower()
                        style = str(attrs.get("style", "") or "").lower()
                        is_hidden = (
                            input_type == "hidden"
                            or hidden_raw is not None
                            or aria_hidden_raw == "true"
                            or "display:none" in style.replace(" ", "")
                        )
                        if is_hidden and not include_hidden:
                            continue

                        item = {
                            "tag": tag,
                            "input_type": input_type,
                            "name": str(attrs.get("name", "") or "").strip(),
                            "id": str(attrs.get("id", "") or "").strip(),
                            "class": " ".join(attrs.get("class", [])) if isinstance(attrs.get("class"), list) else str(attrs.get("class", "") or "").strip(),
                            "placeholder": str(attrs.get("placeholder", "") or "").strip(),
                            "aria_label": str(attrs.get("aria-label", "") or "").strip(),
                            "role": role,
                            "autocomplete": str(attrs.get("autocomplete", "") or "").strip(),
                            "contenteditable": bool(is_contenteditable),
                        }
                        interactive_elements.append(item)
                except (ImportError, Exception) as exc:
                    parser_used = "html_parser"

            if parser_used == "html_parser" or not interactive_elements:
                parser = ParseHTMLContentHook._InteractiveElementParser(
                    include_hidden=include_hidden,
                    max_elements=max_elements,
                )
                parser.feed(html_text)
                parser.close()
                interactive_elements = parser.elements
                parser_used = "html_parser"

            comment_candidates = [
                node for node in interactive_elements if ParseHTMLContentHook._is_comment_candidate(node)
            ]

        details: Dict[str, Any] = {
            "action": action,
            "url": url,
            "status_code": status_code,
            "content_type": content_type,
            "html_chars": len(html_text),
            "truncated": truncated,
            "artifact_path": str(html_path),
            "parse_action": parse_action,
            "parser_requested": parser_engine,
            "parser_used": parser_used,
            "beautifulsoup_available": bs4_available,
            "interactive_count": len(interactive_elements),
            "comment_candidate_count": len(comment_candidates),
            "interactive_elements": interactive_elements,
            "comment_input_candidates": comment_candidates,
        }

        if parse_action == "comment_inputs":
            message = (
                f"Downloaded HTML and found {len(comment_candidates)} likely comment input(s)"
                if comment_candidates
                else "Downloaded HTML; no likely comment inputs found"
            )
        elif parse_action == "extract":
            message = f"Downloaded HTML and extracted {len(interactive_elements)} interactive element(s)"
        else:
            message = "Downloaded HTML artifact"

        return HookResult(
            hook_name=self.name,
            success=True,
            message=message,
            details=details,
        )

class SystemCommandHook(BaseHook):
    name = "system_command"
    description = "Run guarded allowlisted system commands for diagnostics"

    _ALLOWED_ACTIONS = {"execute"}
    _ALLOWED_COMMANDS = {"whoami", "hostname", "ipconfig", "tasklist", "systeminfo", "ping"}
    _SAFE_ARG_PATTERN = re.compile(r"^[A-Za-z0-9._:/=-]{1,64}$")

    def execute(self, context: HookContext) -> HookResult:
        if os.name != "nt":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="system_command is only available on Windows 10/11",
            )

        if os.getenv("LLMIND_ENABLE_COMMAND_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="System command execution disabled. Set LLMIND_ENABLE_COMMAND_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        command = str(args.get("command", "")).strip().lower()
        if command not in self._ALLOWED_COMMANDS:
            allowed = ", ".join(sorted(self._ALLOWED_COMMANDS))
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Command is not allowlisted. Allowed: {allowed}",
            )

        command_args: List[str] = []
        raw_args = args.get("args", [])
        if raw_args:
            if not isinstance(raw_args, list):
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="'args' must be a list of strings",
                )
            if len(raw_args) > 8:
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="Too many command args (max 8)",
                )
            for raw_arg in raw_args:
                if not isinstance(raw_arg, str):
                    return HookResult(
                        hook_name=self.name,
                        success=False,
                        message="All command args must be strings",
                    )
                candidate = raw_arg.strip()
                if not candidate:
                    continue
                if not self._SAFE_ARG_PATTERN.match(candidate):
                    return HookResult(
                        hook_name=self.name,
                        success=False,
                        message=f"Unsafe command argument rejected: {candidate}",
                    )
                command_args.append(candidate)

        if command == "ping" and not command_args:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="ping requires at least one target argument",
            )

        cmdline = [command, *command_args]
        try:
            completed = subprocess.run(
                cmdline,
                shell=False,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=20,
            )
            output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
            output = output.strip()
            if len(output) > 4000:
                output = output[:4000] + "\n... [truncated]"
            return HookResult(
                hook_name=self.name,
                success=completed.returncode == 0,
                message=f"Command exited with code {completed.returncode}",
                details={
                    "action": action,
                    "command": command,
                    "args": command_args,
                    "returncode": completed.returncode,
                    "output": output,
                },
            )
        except subprocess.TimeoutExpired:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="System command timed out",
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"{exc.__class__.__name__}: {exc}",
            )


class OrchestrateWorkflowHook(BaseHook):
    name = "orchestrate_workflow"
    description = "Execute a short sequence of allowed hooks in-order"

    _ALLOWED_ACTIONS = {"run_sequence"}
    _ALLOWED_STEP_HOOKS = {
        "windows_metrics",
        "launch_process",
        "browser_navigation",
        "fetch_webpage_html",
        "parse_html_content",
        "windows_ui_action",
        "validate_click_target",
        "capture_screenshot",
        "system_command",
        "read_file",
        "list_directory",
        "write_file",
        "send_email_smtp",
        "send_email_outlook",
    }

    def execute(self, context: HookContext) -> HookResult:
        if os.getenv("LLMIND_ENABLE_WORKFLOW_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Workflow execution disabled. Set LLMIND_ENABLE_WORKFLOW_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        steps = args.get("steps", [])
        if not isinstance(steps, list) or not steps:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="steps must be a non-empty list",
            )
        if len(steps) > 5:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Too many workflow steps (max 5)",
            )

        registry = context.extras.get("registry")
        if registry is None or not isinstance(registry, HookRegistry):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Workflow registry unavailable in execution context",
            )

        stop_on_error = bool(args.get("stop_on_error", True))
        step_results: List[Dict[str, Any]] = []
        all_success = True

        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message=f"Invalid step at index {index}: expected object",
                )

            hook_name = str(step.get("hook", "")).strip()
            if hook_name not in self._ALLOWED_STEP_HOOKS:
                allowed = ", ".join(sorted(self._ALLOWED_STEP_HOOKS))
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message=f"Step hook '{hook_name}' is not allowed. Allowed: {allowed}",
                )

            hook_args = step.get("args", {})
            if not isinstance(hook_args, dict):
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message=f"Invalid args for step {index}: expected object",
                )

            # Safety gate: every workflow click must be preceded by a successful
            # validate_click_target step with details.valid == True.
            if hook_name == "windows_ui_action" and str(hook_args.get("action", "")).strip().lower() == "click":
                validation = self._find_latest_click_validation(step_results)
                if validation is None:
                    result = HookResult(
                        hook_name=hook_name,
                        success=False,
                        message=(
                            "Blocked click step: no prior successful validate_click_target step found. "
                            "Run validate_click_target first in the workflow."
                        ),
                    )
                    step_results.append(
                        {
                            "step": index,
                            "hook": hook_name,
                            "success": result.success,
                            "message": result.message,
                            "details": result.details,
                        }
                    )
                    all_success = False
                    if stop_on_error:
                        break
                    continue

                rec_click = validation.get("recommended_click")
                if isinstance(rec_click, dict):
                    rx = rec_click.get("x")
                    ry = rec_click.get("y")
                    if isinstance(rx, int) and isinstance(ry, int):
                        if "x" not in hook_args:
                            hook_args["x"] = rx
                        if "y" not in hook_args:
                            hook_args["y"] = ry

                        supplied_x = hook_args.get("x")
                        supplied_y = hook_args.get("y")
                        if isinstance(supplied_x, int) and isinstance(supplied_y, int):
                            if abs(supplied_x - rx) > 3 or abs(supplied_y - ry) > 3:
                                result = HookResult(
                                    hook_name=hook_name,
                                    success=False,
                                    message=(
                                        "Blocked click step: supplied click coordinates do not match "
                                        "validated recommended_click coordinates."
                                    ),
                                    details={
                                        "supplied": {"x": supplied_x, "y": supplied_y},
                                        "recommended_click": {"x": rx, "y": ry},
                                    },
                                )
                                step_results.append(
                                    {
                                        "step": index,
                                        "hook": hook_name,
                                        "success": result.success,
                                        "message": result.message,
                                        "details": result.details,
                                    }
                                )
                                all_success = False
                                if stop_on_error:
                                    break
                                continue

            child_context = HookContext(
                app_data_dir=context.app_data_dir,
                app_name=context.app_name,
                extras=dict(context.extras),
            )
            child_context.extras["hook_args"] = hook_args
            result = registry.execute(hook_name, child_context)

            step_results.append(
                {
                    "step": index,
                    "hook": hook_name,
                    "success": result.success,
                    "message": result.message,
                    "details": result.details,
                }
            )

            if not result.success:
                all_success = False
                if stop_on_error:
                    break

        return HookResult(
            hook_name=self.name,
            success=all_success,
            message="Workflow completed" if all_success else "Workflow completed with failures",
            details={
                "action": action,
                "steps_run": len(step_results),
                "stop_on_error": stop_on_error,
                "step_results": step_results,
            },
        )

    @staticmethod
    def _find_latest_click_validation(step_results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for step in reversed(step_results):
            if not isinstance(step, dict):
                continue
            if step.get("hook") != "validate_click_target":
                continue
            if not bool(step.get("success", False)):
                continue
            details = step.get("details")
            if not isinstance(details, dict):
                continue
            if bool(details.get("valid", False)):
                return details
        return None


class ReadFileHook(BaseHook):
    name = "read_file"
    description = "Read text file contents from safe directories"

    _ALLOWED_ACTIONS = {"read"}
    _SAFE_BASE_DIRS = [
        Path.home() / "Desktop",
        Path.home() / "Desktop" / "test_dir",
        Path.home() / "AppData" / "Roaming" / "LLMind",
        Path.home() / "AppData" / "Local" / "Temp",
    ]

    def _is_safe_path(self, filepath: str) -> bool:
        """Validate that the filepath is within an allowed base directory."""
        try:
            file_path = Path(filepath).resolve()
            for safe_dir in self._SAFE_BASE_DIRS:
                safe_resolved = safe_dir.resolve()
                try:
                    file_path.relative_to(safe_resolved)
                    return True
                except ValueError:
                    continue
            return False
        except Exception:
            return False

    def execute(self, context: HookContext) -> HookResult:
        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        filepath = str(args.get("filepath", "")).strip()
        if not filepath:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="filepath is required",
            )

        if not self._is_safe_path(filepath):
            safe_dirs = ", ".join(str(d) for d in self._SAFE_BASE_DIRS)
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Access denied. File must be in: {safe_dirs}",
            )

        file_path = Path(filepath)
        if not file_path.exists():
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"File not found: {filepath}",
            )

        if not file_path.is_file():
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Not a file: {filepath}",
            )

        try:
            max_chars = args.get("max_chars")
            if max_chars is None:
                max_chars = 10000
            else:
                max_chars = int(max_chars)
                if max_chars < 100 or max_chars > 50000:
                    max_chars = 10000

            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_chars)

            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Read {len(content)} characters from file",
                details={
                    "filepath": str(file_path),
                    "content": content,
                    "truncated": len(content) >= max_chars,
                    "max_chars": max_chars,
                },
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Failed to read file: {exc}",
            )


class ListDirectoryHook(BaseHook):
    name = "list_directory"
    description = "List files in a directory"

    _ALLOWED_ACTIONS = {"list"}
    _SAFE_BASE_DIRS = [
        Path.home() / "Desktop",
        Path.home() / "Desktop" / "test_dir",
        Path.home() / "AppData" / "Roaming" / "LLMind",
        Path.home() / "AppData" / "Local" / "Temp",
    ]

    def _is_safe_path(self, dirpath: str) -> bool:
        """Validate that the dirpath is within an allowed base directory."""
        try:
            dir_path = Path(dirpath).resolve()
            for safe_dir in self._SAFE_BASE_DIRS:
                safe_resolved = safe_dir.resolve()
                try:
                    dir_path.relative_to(safe_resolved)
                    return True
                except ValueError:
                    continue
            return False
        except Exception:
            return False

    def execute(self, context: HookContext) -> HookResult:
        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        dirpath = str(args.get("dirpath", "")).strip()
        if not dirpath:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="dirpath is required",
            )

        if not self._is_safe_path(dirpath):
            safe_dirs = ", ".join(str(d) for d in self._SAFE_BASE_DIRS)
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Access denied. Directory must be in: {safe_dirs}",
            )

        dir_path = Path(dirpath)
        if not dir_path.exists():
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Directory not found: {dirpath}",
            )

        if not dir_path.is_dir():
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Not a directory: {dirpath}",
            )

        try:
            extension = str(args.get("extension", "")).strip().lower()
            if extension and not extension.startswith("."):
                extension = f".{extension}"

            files: List[Dict[str, Any]] = []
            for item in sorted(dir_path.iterdir()):
                if not item.is_file():
                    continue
                if extension and not item.suffix.lower() == extension:
                    continue
                files.append(
                    {
                        "name": item.name,
                        "path": str(item),
                        "size": item.stat().st_size,
                    }
                )

            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Listed {len(files)} file(s)",
                details={
                    "dirpath": str(dir_path),
                    "extension_filter": extension or "(no filter)",
                    "files": files,
                },
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Failed to list directory: {exc}",
            )


class QueryBrowserHistoryHook(BaseHook):
    name = "query_browser_history"
    description = "Query Chrome/Edge History SQLite database with granular time-window actions"

    _ALLOWED_ACTIONS = {"summary", "top_domains", "top_pages", "daily_timeline"}
    _WINDOW_PRESETS = {
        "last_24_hours": timedelta(hours=24),
        "last_7_days": timedelta(days=7),
        "last_14_days": timedelta(days=14),
        "last_30_days": timedelta(days=30),
    }
    _SAFE_BASE_DIRS = [
        Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
        Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data",
        Path.home() / "Desktop",
        Path.home() / "AppData" / "Local" / "Temp",
    ]

    def execute(self, context: HookContext) -> HookResult:
        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        filepath = str(args.get("filepath", "")).strip()
        if not filepath:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="filepath is required",
            )

        db_path = Path(filepath)
        if not db_path.exists() or not db_path.is_file():
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"History database not found: {filepath}",
            )

        if not self._is_safe_path(db_path):
            safe_dirs = ", ".join(str(d) for d in self._SAFE_BASE_DIRS)
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Access denied. History database must be in: {safe_dirs}",
            )

        if not self._is_sqlite_file(db_path):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="File is not a valid SQLite database",
            )

        limit = self._coerce_limit(args.get("limit", 15))
        try:
            range_start, range_end, window_label = self._resolve_time_window(args)
        except ValueError as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=str(exc),
            )

        start_us = self._to_chrome_microseconds(range_start)
        end_us = self._to_chrome_microseconds(range_end)

        temp_copy_path = Path(tempfile.gettempdir()) / f"llmind_history_{int(time.time() * 1000)}.db"
        conn: Optional[sqlite3.Connection] = None
        try:
            shutil.copy2(db_path, temp_copy_path)
            conn = sqlite3.connect(str(temp_copy_path))
            conn.row_factory = sqlite3.Row

            if action == "summary":
                details = self._query_summary(conn, start_us, end_us, limit)
            elif action == "top_domains":
                details = self._query_top_domains(conn, start_us, end_us, limit)
            elif action == "top_pages":
                details = self._query_top_pages(conn, start_us, end_us, limit)
            else:
                details = self._query_daily_timeline(conn, start_us, end_us)

            details.update(
                {
                    "action": action,
                    "window": window_label,
                    "range_start_utc": range_start.isoformat().replace("+00:00", "Z"),
                    "range_end_utc": range_end.isoformat().replace("+00:00", "Z"),
                    "filepath": str(db_path),
                }
            )

            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Browser history query complete for {window_label}",
                details=details,
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"History query failed: {exc.__class__.__name__}: {exc}",
            )
        finally:
            if conn is not None:
                conn.close()
            try:
                if temp_copy_path.exists():
                    temp_copy_path.unlink()
            except Exception:
                pass

    def _is_safe_path(self, filepath: Path) -> bool:
        try:
            resolved = filepath.resolve()
            for safe_dir in self._SAFE_BASE_DIRS:
                safe_resolved = safe_dir.resolve()
                try:
                    resolved.relative_to(safe_resolved)
                    return True
                except ValueError:
                    continue
        except Exception:
            return False
        return False

    @staticmethod
    def _is_sqlite_file(filepath: Path) -> bool:
        try:
            with filepath.open("rb") as handle:
                magic = handle.read(16)
            return magic.startswith(b"SQLite format 3")
        except Exception:
            return False

    @staticmethod
    def _coerce_limit(raw_limit: Any) -> int:
        try:
            limit = int(raw_limit)
        except Exception:
            return 15
        return min(max(limit, 1), 100)

    def _resolve_time_window(self, args: Dict[str, Any]) -> tuple[datetime, datetime, str]:
        now = datetime.now(timezone.utc)
        window = str(args.get("window", "last_14_days")).strip().lower()

        if window in self._WINDOW_PRESETS:
            delta = self._WINDOW_PRESETS[window]
            return now - delta, now, window

        if window == "custom_days":
            raw_days = args.get("days")
            try:
                days = int(raw_days)
            except Exception:
                raise ValueError("custom_days requires integer 'days' between 1 and 365")
            if days < 1 or days > 365:
                raise ValueError("'days' must be between 1 and 365")
            return now - timedelta(days=days), now, f"last_{days}_days"

        if window == "custom_range":
            start_raw = str(args.get("start_date", "")).strip()
            end_raw = str(args.get("end_date", "")).strip()
            if not start_raw or not end_raw:
                raise ValueError("custom_range requires both 'start_date' and 'end_date'")

            start_dt = self._parse_utc_datetime(start_raw, end_of_day=False)
            end_dt = self._parse_utc_datetime(end_raw, end_of_day=True)
            if end_dt < start_dt:
                raise ValueError("end_date must be greater than or equal to start_date")
            return start_dt, end_dt, "custom_range"

        raise ValueError(
            "Unsupported window. Allowed: last_24_hours, last_7_days, last_14_days, "
            "last_30_days, custom_days, custom_range"
        )

    @staticmethod
    def _parse_utc_datetime(raw: str, end_of_day: bool) -> datetime:
        value = raw.strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

        date_only = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw.strip())
        if not date_only:
            raise ValueError(f"Invalid datetime format: {raw}")
        year, month, day = [int(part) for part in date_only.groups()]
        if end_of_day:
            return datetime(year, month, day, 23, 59, 59, 999999, tzinfo=timezone.utc)
        return datetime(year, month, day, 0, 0, 0, 0, tzinfo=timezone.utc)

    @staticmethod
    def _to_chrome_microseconds(dt: datetime) -> int:
        windows_epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
        delta = dt - windows_epoch
        return int(delta.total_seconds() * 1_000_000)

    def _query_summary(self, conn: sqlite3.Connection, start_us: int, end_us: int, limit: int) -> Dict[str, object]:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) AS visit_count,
                   COUNT(DISTINCT v.url) AS unique_url_ids
            FROM visits v
            WHERE v.visit_time BETWEEN ? AND ?
            """,
            (start_us, end_us),
        )
        row = cursor.fetchone()
        visit_count = int(row["visit_count"] or 0)
        unique_url_ids = int(row["unique_url_ids"] or 0)

        top_domains = self._query_top_domains(conn, start_us, end_us, limit).get("top_domains", [])
        top_pages = self._query_top_pages(conn, start_us, end_us, limit).get("top_pages", [])

        return {
            "visit_count": visit_count,
            "unique_url_ids": unique_url_ids,
            "top_domains": top_domains,
            "top_pages": top_pages,
        }

    def _query_top_domains(self, conn: sqlite3.Connection, start_us: int, end_us: int, limit: int) -> Dict[str, object]:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT u.url, COUNT(*) AS visits
            FROM visits v
            JOIN urls u ON u.id = v.url
            WHERE v.visit_time BETWEEN ? AND ?
            GROUP BY u.id
            ORDER BY visits DESC
            LIMIT 500
            """,
            (start_us, end_us),
        )
        domain_counts: Dict[str, int] = {}
        for row in cursor.fetchall():
            raw_url = str(row["url"] or "")
            domain = urlparse(raw_url).netloc or raw_url[:120] or "(unknown)"
            domain_counts[domain] = domain_counts.get(domain, 0) + int(row["visits"] or 0)

        sorted_domains = sorted(domain_counts.items(), key=lambda item: item[1], reverse=True)
        top_domains = [{"domain": domain, "visits": visits} for domain, visits in sorted_domains[:limit]]

        return {
            "unique_domains": len(domain_counts),
            "top_domains": top_domains,
        }

    def _query_top_pages(self, conn: sqlite3.Connection, start_us: int, end_us: int, limit: int) -> Dict[str, object]:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT u.url,
                   COALESCE(u.title, '') AS title,
                   COUNT(*) AS visits,
                   MAX(v.visit_time) AS latest_visit_time
            FROM visits v
            JOIN urls u ON u.id = v.url
            WHERE v.visit_time BETWEEN ? AND ?
            GROUP BY u.id
            ORDER BY visits DESC
            LIMIT ?
            """,
            (start_us, end_us, limit),
        )

        pages: List[Dict[str, object]] = []
        for row in cursor.fetchall():
            pages.append(
                {
                    "url": str(row["url"] or ""),
                    "title": str(row["title"] or ""),
                    "visits": int(row["visits"] or 0),
                    "last_visit_utc": self._chrome_microseconds_to_iso(int(row["latest_visit_time"] or 0)),
                }
            )

        return {
            "top_pages": pages,
        }

    def _query_daily_timeline(self, conn: sqlite3.Connection, start_us: int, end_us: int) -> Dict[str, object]:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT date((visit_time / 1000000) - 11644473600, 'unixepoch') AS visit_day,
                   COUNT(*) AS visits
            FROM visits
            WHERE visit_time BETWEEN ? AND ?
            GROUP BY visit_day
            ORDER BY visit_day ASC
            """,
            (start_us, end_us),
        )

        timeline = [
            {"date": str(row["visit_day"]), "visits": int(row["visits"] or 0)}
            for row in cursor.fetchall()
            if row["visit_day"] is not None
        ]

        return {
            "days": timeline,
            "total_visits": sum(item["visits"] for item in timeline),
        }

    @staticmethod
    def _chrome_microseconds_to_iso(value: int) -> Optional[str]:
        if value <= 0:
            return None
        windows_epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
        dt = windows_epoch + timedelta(microseconds=value)
        return dt.isoformat().replace("+00:00", "Z")


class CaptureAndOCRScreenHook(BaseHook):
    name = "capture_and_ocr_screen"
    description = "Capture screen/region and run OCR with normalized output"

    _ALLOWED_ACTIONS = {"capture"}
    _MAX_BLOCKS = 500

    def execute(self, context: HookContext) -> HookResult:
        if os.getenv("LLMIND_ENABLE_VISION_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Vision hooks disabled. Set LLMIND_ENABLE_VISION_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(hook_name=self.name, success=False, message="Invalid hook args: expected object/dict")

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Unsupported action for capture_and_ocr_screen. Allowed: capture",
            )

        region = args.get("region") if isinstance(args.get("region"), dict) else None
        include_image = bool(args.get("include_image", False))
        ocr_engine = str(args.get("ocr_engine", "auto")).strip().lower() or "auto"
        if ocr_engine not in {"auto", "paddle", "tesseract", "winrt"}:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="ocr_engine must be one of: auto, paddle, tesseract, winrt",
            )

        run_dir = context.app_data_dir / "artifacts" / "vision" / f"run_{uuid4().hex[:10]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        image_path = run_dir / "before.png"
        ocr_json_path = run_dir / "ocr.json"

        try:
            screen_size = self._capture_image(image_path=image_path, region=region)
            ocr_blocks, strategy, warnings = self._run_ocr(image_path=image_path, engine=ocr_engine)
            ocr_blocks = ocr_blocks[: self._MAX_BLOCKS]
            ocr_json_path.write_text(json.dumps({"ocr_blocks": ocr_blocks}, ensure_ascii=False, indent=2), encoding="utf-8")

            details: Dict[str, Any] = {
                "screen": screen_size,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "ocr_blocks": ocr_blocks,
                "ocr_blocks_count": len(ocr_blocks),
                "strategy": strategy,
                "ocr_ref": str(ocr_json_path),
                "discrimination": {
                    "selected_strategy": strategy,
                    "engine_requested": ocr_engine,
                    "warnings": warnings,
                },
            }
            if include_image:
                details["image_ref"] = str(image_path)

            return HookResult(
                hook_name=self.name,
                success=True,
                message="Screen capture + OCR completed",
                details=details,
            )
        except Exception as exc:
            # Integration decision: capture backend absence is treated as non-fatal so the
            # planner can discriminate and choose a no-op/fallback path instead of crashing.
            details = {
                "screen": {"width": 0, "height": 0},
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "ocr_blocks": [],
                "ocr_blocks_count": 0,
                "strategy": "capture:none",
                "discrimination": {
                    "selected_strategy": "capture:none",
                    "engine_requested": ocr_engine,
                    "warnings": [f"Capture unavailable: {exc}"],
                },
            }
            return HookResult(
                hook_name=self.name,
                success=True,
                message="Vision capture unavailable; returned empty observations",
                details=details,
            )

    def _capture_image(self, image_path: Path, region: Optional[Dict[str, Any]]) -> Dict[str, int]:
        if region is not None:
            required = {"x", "y", "width", "height"}
            if not required.issubset(region.keys()):
                raise ValueError("region requires x, y, width, height")
            x, y = int(region["x"]), int(region["y"])
            w, h = int(region["width"]), int(region["height"])
            if w <= 0 or h <= 0:
                raise ValueError("region width/height must be positive")
            bbox = (x, y, x + w, y + h)
        else:
            bbox = None

        try:
            import mss
            import mss.tools

            with mss.mss() as sct:
                if bbox is None:
                    monitor = sct.monitors[1]
                else:
                    monitor = {"left": bbox[0], "top": bbox[1], "width": bbox[2] - bbox[0], "height": bbox[3] - bbox[1]}
                shot = sct.grab(monitor)
                mss.tools.to_png(shot.rgb, shot.size, output=str(image_path))
                return {"width": int(shot.width), "height": int(shot.height)}
        except Exception:
            try:
                from PIL import ImageGrab

                img = ImageGrab.grab(bbox=bbox)
                img.save(image_path)
                return {"width": int(img.width), "height": int(img.height)}
            except Exception:
                pass

        # PowerShell GDI+ fallback â€” works on any Windows 10/11 with no Python deps.
        ps_path = str(image_path).replace("'", "''")
        if bbox is None:
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$s=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
                "$bmp=New-Object System.Drawing.Bitmap $s.Width,$s.Height; "
                "$g=[System.Drawing.Graphics]::FromImage($bmp); "
                "$g.CopyFromScreen(0,0,0,0,$bmp.Size); "
                f"$bmp.Save('{ps_path}',[System.Drawing.Imaging.ImageFormat]::Png); "
                "$g.Dispose(); $bmp.Dispose();"
            )
            w_hint, h_hint = 0, 0
        else:
            bx, by, bx2, by2 = bbox
            bw, bh = max(bx2 - bx, 1), max(by2 - by, 1)
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                f"$bmp=New-Object System.Drawing.Bitmap {bw},{bh}; "
                "$g=[System.Drawing.Graphics]::FromImage($bmp); "
                f"$g.CopyFromScreen({bx},{by},0,0,$bmp.Size); "
                f"$bmp.Save('{ps_path}',[System.Drawing.Imaging.ImageFormat]::Png); "
                "$g.Dispose(); $bmp.Dispose();"
            )
            w_hint, h_hint = bw, bh
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                check=False, capture_output=True, text=True, errors="replace", timeout=30,
            )
            if proc.returncode != 0 or not image_path.exists() or image_path.stat().st_size <= 0:
                raise RuntimeError(
                    f"PowerShell GDI+ capture failed (rc={proc.returncode}): {proc.stderr.strip()}"
                )
            try:
                from PIL import Image as _PILImg
                with _PILImg.open(image_path) as _img:
                    return {"width": int(_img.width), "height": int(_img.height)}
            except Exception:
                return {"width": w_hint, "height": h_hint}
        except Exception as exc:
            raise RuntimeError(f"No capture backend available (mss/Pillow/PowerShell): {exc}")

    def _run_ocr(self, image_path: Path, engine: str) -> tuple[List[Dict[str, Any]], str, List[str]]:
        warnings: List[str] = []

        if engine in {"auto", "paddle"}:
            try:
                from paddleocr import PaddleOCR

                ocr = PaddleOCR(use_angle_cls=True, lang="en")
                result = ocr.ocr(str(image_path), cls=True)
                blocks: List[Dict[str, Any]] = []
                for line_group in result or []:
                    for line in line_group or []:
                        if not isinstance(line, (list, tuple)) or len(line) < 2:
                            continue
                        bbox_points = line[0]
                        text_conf = line[1]
                        if not isinstance(text_conf, (list, tuple)) or len(text_conf) < 2:
                            continue
                        text = str(text_conf[0] or "").strip()
                        confidence = float(text_conf[1] or 0.0)
                        if not text:
                            continue
                        xs = [int(p[0]) for p in bbox_points]
                        ys = [int(p[1]) for p in bbox_points]
                        blocks.append(
                            {
                                "text": text,
                                "confidence": max(0.0, min(1.0, confidence)),
                                "bbox": [min(xs), min(ys), max(xs), max(ys)],
                            }
                        )
                return blocks, "ocr:paddle", warnings
            except Exception as exc:
                warnings.append(f"PaddleOCR unavailable/failed: {exc}")

        if engine in {"auto", "tesseract"}:
            try:
                import pytesseract
                from PIL import Image

                if not shutil.which("tesseract"):
                    candidate_bins = [
                        Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
                        Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
                        Path(os.getenv("LOCALAPPDATA", "")) / "Programs/Tesseract-OCR/tesseract.exe",
                    ]
                    for candidate in candidate_bins:
                        if candidate.exists() and candidate.is_file():
                            pytesseract.pytesseract.tesseract_cmd = str(candidate)
                            break

                img = Image.open(image_path)
                data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
                blocks: List[Dict[str, Any]] = []
                total = len(data.get("text", []))
                for i in range(total):
                    text = str(data["text"][i] or "").strip()
                    if not text:
                        continue
                    conf_raw = str(data.get("conf", ["0"])[i])
                    try:
                        conf_num = float(conf_raw)
                    except Exception:
                        conf_num = 0.0
                    confidence = max(0.0, min(1.0, conf_num / 100.0))
                    left = int(data.get("left", [0])[i])
                    top = int(data.get("top", [0])[i])
                    width = int(data.get("width", [0])[i])
                    height = int(data.get("height", [0])[i])
                    blocks.append(
                        {
                            "text": text,
                            "confidence": confidence,
                            "bbox": [left, top, left + max(width, 0), top + max(height, 0)],
                        }
                    )
                return blocks, "ocr:tesseract", warnings
            except Exception as exc:
                warnings.append(f"Tesseract unavailable/failed: {exc}")

        # Windows built-in OCR via WinRT â€” no Tesseract binary required (Win 10/11).
        # Keep this as a fallback even when paddle/tesseract were explicitly requested.
        if engine in {"auto", "winrt", "paddle", "tesseract"}:
            _ps1 = Path(__file__).resolve().parent.parent / "utils" / "win_ocr_engine.ps1"
            if _ps1.exists():
                try:
                    proc = subprocess.run(
                        [
                            "powershell",
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(_ps1),
                            "-ImagePath",
                            str(image_path),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        errors="replace",
                        timeout=60,
                    )
                    raw = (proc.stdout or "").strip()
                    if proc.returncode == 0 and raw:
                        # PowerShell 5.1 ConvertTo-Json may embed literal control chars
                        # inside string values without escaping them; sanitize before parse.
                        import re as _re
                        raw = _re.sub(r'(?<!\\)[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', raw)
                        parsed = json.loads(raw)
                        if not isinstance(parsed, list):
                            parsed = [parsed]
                        blocks: List[Dict[str, Any]] = []
                        for item in parsed:
                            text = str(item.get("text", "")).strip()
                            if not text:
                                continue
                            conf = float(item.get("confidence", 0.88) or 0.88)
                            raw_bbox = item.get("bbox")
                            bbox_val = (
                                [int(v) for v in raw_bbox]
                                if isinstance(raw_bbox, list) and len(raw_bbox) == 4
                                else None
                            )
                            blocks.append(
                                {
                                    "text": text,
                                    "confidence": max(0.0, min(1.0, conf)),
                                    "bbox": bbox_val,
                                }
                            )
                        return blocks, "ocr:winrt", warnings
                    else:
                        warnings.append(
                            f"WinRT OCR exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}"
                        )
                except Exception as exc:
                    warnings.append(f"WinRT OCR failed: {exc}")
                if engine == "winrt":
                    return [], "ocr:none", warnings
            else:
                warnings.append(f"win_ocr_engine.ps1 not found at {_ps1}")

        return [], "ocr:none", warnings


class AnalyzeUIWithVisionModelHook(BaseHook):
    name = "analyze_ui_with_vision_model"
    description = "Generate structured UI action decision with confidence and fallback"

    _ALLOWED_ACTIONS = {"analyze"}
    _DEFAULT_ALLOWED_DECISIONS = ["click", "type", "hotkey", "scroll", "noop"]

    def execute(self, context: HookContext) -> HookResult:
        if os.getenv("LLMIND_ENABLE_VISION_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Vision hooks disabled. Set LLMIND_ENABLE_VISION_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(hook_name=self.name, success=False, message="Invalid hook args: expected object/dict")

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(hook_name=self.name, success=False, message="Unsupported action. Allowed: analyze")

        objective = str(args.get("objective", "")).strip()
        if not objective:
            return HookResult(hook_name=self.name, success=False, message="objective is required")

        allowed_decisions = args.get("allowed_actions")
        if isinstance(allowed_decisions, list) and allowed_decisions:
            decisions = [str(x).strip().lower() for x in allowed_decisions if str(x).strip()]
            decisions = [d for d in decisions if d in self._DEFAULT_ALLOWED_DECISIONS]
            if not decisions:
                decisions = list(self._DEFAULT_ALLOWED_DECISIONS)
        else:
            decisions = list(self._DEFAULT_ALLOWED_DECISIONS)

        raw_blocks = args.get("ocr_blocks", [])
        if not isinstance(raw_blocks, list):
            return HookResult(hook_name=self.name, success=False, message="ocr_blocks must be an array")

        objective_tokens = [tok for tok in re.split(r"[^A-Za-z0-9]+", objective.lower()) if len(tok) >= 3]
        min_confidence = self._read_min_confidence()

        best_block: Optional[Dict[str, Any]] = None
        best_score = -1.0
        for block in raw_blocks:
            if not isinstance(block, dict):
                continue
            text = str(block.get("text", "")).strip()
            if not text:
                continue
            text_lower = text.lower()
            matched = [tok for tok in objective_tokens if tok in text_lower]
            token_score = (len(matched) / len(objective_tokens)) if objective_tokens else 0.0
            ocr_conf = self._coerce_float(block.get("confidence"), default=0.5)
            score = (0.65 * token_score) + (0.35 * ocr_conf)
            if score > best_score:
                best_score = score
                best_block = {
                    "text": text,
                    "bbox": block.get("bbox"),
                    "ocr_confidence": ocr_conf,
                    "token_score": token_score,
                    "matched_tokens": matched,
                }

        if best_block is None:
            decision = "noop"
            confidence = 0.0
            reason = "No OCR candidates available"
            target = {"type": "none", "value": None}
        else:
            confidence = max(0.0, min(1.0, best_score))
            if confidence >= min_confidence and "click" in decisions:
                decision = "click"
                reason = "OCR target matched objective above threshold"
                target = {
                    "type": "text",
                    "value": best_block["text"],
                    "bbox": best_block.get("bbox"),
                }
            else:
                decision = "noop"
                reason = "No high-confidence actionable candidate"
                target = {
                    "type": "text",
                    "value": best_block["text"],
                    "bbox": best_block.get("bbox"),
                }

        # Decision discrimination explicitly records why this branch was chosen.
        discrimination = {
            "objective_tokens": objective_tokens,
            "threshold": min_confidence,
            "best_score": max(0.0, best_score),
            "best_candidate": best_block,
            "allowed_actions": decisions,
            "selected_strategy": "ocr_semantic_scoring",
        }

        details = {
            "decision": decision,
            "target": target,
            "confidence": confidence,
            "fallback": {"decision": "noop"},
            "reason": reason,
            "discrimination": discrimination,
            "image_ref": str(args.get("image_ref", "")).strip() or None,
        }
        return HookResult(
            hook_name=self.name,
            success=True,
            message="Vision analysis decision generated",
            details=details,
        )

    @staticmethod
    def _coerce_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _read_min_confidence() -> float:
        raw = os.getenv("LLMIND_VISION_MIN_CONFIDENCE", "0.72").strip()
        try:
            val = float(raw)
        except Exception:
            val = 0.72
        return min(max(val, 0.0), 1.0)


class VerifyUIChangeHook(BaseHook):
    name = "verify_ui_change"
    description = "Verify expected UI text transition using bounded OCR retries"

    _ALLOWED_ACTIONS = {"verify"}

    def execute(self, context: HookContext) -> HookResult:
        if os.getenv("LLMIND_ENABLE_VISION_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Vision hooks disabled. Set LLMIND_ENABLE_VISION_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(hook_name=self.name, success=False, message="Invalid hook args: expected object/dict")

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(hook_name=self.name, success=False, message="Unsupported action. Allowed: verify")

        expected_any = args.get("expected_text_any") if isinstance(args.get("expected_text_any"), list) else []
        expected_all = args.get("expected_text_all") if isinstance(args.get("expected_text_all"), list) else []
        expected_any = [str(x).strip() for x in expected_any if str(x).strip()]
        expected_all = [str(x).strip() for x in expected_all if str(x).strip()]
        if not expected_any and not expected_all:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="At least one of expected_text_any or expected_text_all is required",
            )

        timeout_ms = int(args.get("timeout_ms", 2000) or 2000)
        timeout_ms = min(max(timeout_ms, 100), 10000)
        retries = self._read_max_retries()
        region = args.get("region") if isinstance(args.get("region"), dict) else None

        run_dir = context.app_data_dir / "artifacts" / "vision" / f"verify_{uuid4().hex[:10]}"
        run_dir.mkdir(parents=True, exist_ok=True)

        deadline = time.time() + (timeout_ms / 1000.0)
        attempt = 0
        observed_text = ""
        best_ratio = 0.0

        while time.time() <= deadline and attempt <= retries:
            attempt += 1
            image_path = run_dir / f"verify_attempt_{attempt}.png"
            cap = CaptureAndOCRScreenHook()
            try:
                cap._capture_image(image_path=image_path, region=region)
                blocks, strategy, warnings = cap._run_ocr(image_path=image_path, engine="auto")
            except Exception as exc:
                observed_text = f"[capture_failed] {exc}"
                strategy = "capture:none"
                warnings = [str(exc)]
                blocks = []

            observed_text = "\n".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict)).lower()
            match_any = any(token.lower() in observed_text for token in expected_any) if expected_any else True
            match_all = all(token.lower() in observed_text for token in expected_all) if expected_all else True
            verified = match_any and match_all

            match_total = len(expected_any) + len(expected_all)
            match_hits = 0
            match_hits += sum(1 for token in expected_any if token.lower() in observed_text)
            match_hits += sum(1 for token in expected_all if token.lower() in observed_text)
            ratio = (match_hits / match_total) if match_total else 0.0
            best_ratio = max(best_ratio, ratio)

            if verified:
                return HookResult(
                    hook_name=self.name,
                    success=True,
                    message="UI verification succeeded",
                    details={
                        "verified": True,
                        "confidence": ratio,
                        "attempt": attempt,
                        "observed_text": observed_text[:5000],
                        "discrimination": {
                            "match_any_required": expected_any,
                            "match_all_required": expected_all,
                            "selected_strategy": strategy,
                            "warnings": warnings,
                        },
                    },
                )

            time.sleep(0.35)

        return HookResult(
            hook_name=self.name,
            success=True,
            message="UI verification did not meet expected state within timeout",
            details={
                "verified": False,
                "confidence": best_ratio,
                "attempts": attempt,
                "observed_text": observed_text[:5000],
                "discrimination": {
                    "match_any_required": expected_any,
                    "match_all_required": expected_all,
                    "selected_strategy": "ocr_text_contains",
                    "max_retries": retries,
                    "timeout_ms": timeout_ms,
                },
            },
        )

    @staticmethod
    def _read_max_retries() -> int:
        raw = os.getenv("LLMIND_VISION_MAX_RETRIES", "2").strip()
        try:
            val = int(raw)
        except Exception:
            val = 2
        return min(max(val, 0), 5)


class DetectVisualObjectsHook(BaseHook):
    name = "detect_visual_objects"
    description = "Detect visual objects from screen/image using OCR + optional CV"

    _ALLOWED_ACTIONS = {"detect"}

    def execute(self, context: HookContext) -> HookResult:
        if os.getenv("LLMIND_ENABLE_VISION_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Vision hooks disabled. Set LLMIND_ENABLE_VISION_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(hook_name=self.name, success=False, message="Invalid hook args: expected object/dict")

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(hook_name=self.name, success=False, message="Unsupported action. Allowed: detect")

        include_ocr = bool(args.get("include_ocr", True))
        max_objects = int(args.get("max_objects", 50) or 50)
        max_objects = min(max(max_objects, 1), 200)
        objective = str(args.get("objective", "")).strip().lower()

        image_ref_raw = str(args.get("image_ref", "")).strip()
        image_path: Optional[Path] = None
        source_strategy = "image_ref"

        if image_ref_raw:
            candidate = Path(image_ref_raw)
            if candidate.exists() and candidate.is_file():
                image_path = candidate
            else:
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message=f"image_ref does not exist or is not a file: {image_ref_raw}",
                )
        else:
            cap = CaptureAndOCRScreenHook()
            run_dir = context.app_data_dir / "artifacts" / "vision" / f"objects_{uuid4().hex[:10]}"
            run_dir.mkdir(parents=True, exist_ok=True)
            image_path = run_dir / "before.png"
            source_strategy = "capture"
            try:
                cap._capture_image(image_path=image_path, region=args.get("region") if isinstance(args.get("region"), dict) else None)
            except Exception as exc:
                return HookResult(
                    hook_name=self.name,
                    success=True,
                    message="Visual detection capture unavailable; returned empty candidates",
                    details={
                        "objects": [],
                        "image_ref": None,
                        "count": 0,
                        "discrimination": {
                            "selected_strategy": "capture:none",
                            "include_ocr": include_ocr,
                            "objective": objective or None,
                            "warnings": [f"Capture unavailable: {exc}"],
                        },
                    },
                )

        assert image_path is not None

        objects: List[Dict[str, Any]] = []
        warnings: List[str] = []

        if include_ocr:
            cap = CaptureAndOCRScreenHook()
            ocr_blocks, ocr_strategy, ocr_warnings = cap._run_ocr(image_path=image_path, engine="auto")
            warnings.extend(ocr_warnings)
            for block in ocr_blocks:
                text = str(block.get("text", "")).strip()
                if not text:
                    continue
                label = "text"
                lowered = text.lower()
                if any(token in lowered for token in ["ok", "submit", "save", "next", "apply", "cancel", "continue"]):
                    label = "button_text"
                score = float(block.get("confidence", 0.0) or 0.0)
                if objective and objective in lowered:
                    score = min(1.0, score + 0.2)
                objects.append(
                    {
                        "label": label,
                        "type": "ocr_text",
                        "text": text,
                        "confidence": max(0.0, min(1.0, score)),
                        "bbox": block.get("bbox"),
                    }
                )
        else:
            ocr_strategy = "ocr:disabled"

        # Optional CV contour candidates for non-text visual regions.
        cv_strategy = "cv:none"
        try:
            import cv2  # type: ignore

            image = cv2.imread(str(image_path))
            if image is not None:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                edges = cv2.Canny(gray, 80, 160)
                contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                h, w = gray.shape[:2]
                min_area = max(100, int((w * h) * 0.0002))
                for contour in contours[:500]:
                    x, y, cw, ch = cv2.boundingRect(contour)
                    area = cw * ch
                    if area < min_area:
                        continue
                    if cw < 8 or ch < 8:
                        continue
                    aspect = cw / max(ch, 1)
                    label = "icon_candidate" if 0.5 <= aspect <= 2.0 else "image_region"
                    score = min(0.95, 0.35 + min(area / max(w * h, 1), 0.6))
                    objects.append(
                        {
                            "label": label,
                            "type": "cv_region",
                            "text": None,
                            "confidence": float(score),
                            "bbox": [int(x), int(y), int(x + cw), int(y + ch)],
                        }
                    )
                cv_strategy = "cv:opencv_contours"
            else:
                warnings.append("OpenCV could not read image for contour detection")
        except Exception as exc:
            warnings.append(f"OpenCV unavailable/failed: {exc}")

        # Keep best candidates first, favoring objective-related text and higher confidence.
        def _sort_key(item: Dict[str, Any]) -> float:
            base = float(item.get("confidence", 0.0) or 0.0)
            txt = str(item.get("text") or "").lower()
            if objective and objective in txt:
                base += 0.25
            if item.get("label") == "button_text":
                base += 0.1
            return base

        objects = sorted(objects, key=_sort_key, reverse=True)[:max_objects]

        return HookResult(
            hook_name=self.name,
            success=True,
            message=f"Detected {len(objects)} visual object candidate(s)",
            details={
                "objects": objects,
                "image_ref": str(image_path),
                "count": len(objects),
                "discrimination": {
                    "selected_strategy": f"{source_strategy}+{ocr_strategy}+{cv_strategy}",
                    "include_ocr": include_ocr,
                    "objective": objective or None,
                    "warnings": warnings,
                },
            },
        )


class ValidateClickTargetHook(BaseHook):
    name = "validate_click_target"
    description = "Validate click target by OCR/text match and coordinate bounds checks"

    _ALLOWED_ACTIONS = {"validate"}

    def execute(self, context: HookContext) -> HookResult:
        if os.getenv("LLMIND_ENABLE_VISION_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Vision hooks disabled. Set LLMIND_ENABLE_VISION_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(hook_name=self.name, success=False, message="Invalid hook args: expected object/dict")

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(hook_name=self.name, success=False, message="Unsupported action. Allowed: validate")

        target = args.get("target")
        if not isinstance(target, dict):
            return HookResult(hook_name=self.name, success=False, message="target object is required")

        target_type = str(target.get("type", "")).strip().lower()
        min_conf = args.get("min_confidence")
        if min_conf is None:
            min_conf_val = 0.55
        else:
            try:
                min_conf_val = float(min_conf)
            except Exception:
                min_conf_val = 0.55
        min_conf_val = min(max(min_conf_val, 0.0), 1.0)

        cap = CaptureAndOCRScreenHook()
        run_dir = context.app_data_dir / "artifacts" / "vision" / f"clickval_{uuid4().hex[:10]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        image_path = run_dir / "validate.png"
        region = args.get("region") if isinstance(args.get("region"), dict) else None

        screen: Dict[str, int] = {"width": 0, "height": 0}
        image_ref: Optional[str] = None

        # Discrimination decision: point/bbox validation can run deterministically
        # from display metrics without OCR/capture dependencies.
        if target_type in {"point", "bbox"}:
            screen = self._get_screen_size_from_metrics()
        else:
            try:
                screen = cap._capture_image(image_path=image_path, region=region)
                image_ref = str(image_path)
            except Exception as exc:
                return HookResult(
                    hook_name=self.name,
                    success=True,
                    message="Click target validation capture unavailable",
                    details={
                        "valid": False,
                        "confidence": 0.0,
                        "recommended_click": None,
                        "reason": "Capture backend unavailable",
                        "image_ref": None,
                        "discrimination": {
                            "target_type": str(target.get("type", "")).strip().lower(),
                            "min_confidence": min_conf_val,
                            "selected_strategy": "capture:none",
                            "warnings": [str(exc)],
                        },
                    },
                )

        sw = int(screen.get("width", 0) or 0)
        sh = int(screen.get("height", 0) or 0)

        recommended_click: Dict[str, Any] = {}
        valid = False
        confidence = 0.0
        reason = ""
        discrimination: Dict[str, Any] = {
            "target_type": target_type,
            "min_confidence": min_conf_val,
            "screen": {"width": sw, "height": sh},
        }

        if target_type == "point":
            x = target.get("x")
            y = target.get("y")
            if not isinstance(x, int) or not isinstance(y, int):
                return HookResult(hook_name=self.name, success=False, message="point target requires integer x and y")
            in_bounds = 0 <= x < sw and 0 <= y < sh
            valid = in_bounds
            confidence = 1.0 if in_bounds else 0.0
            reason = "Point is within screen bounds" if in_bounds else "Point is outside screen bounds"
            recommended_click = {"x": x, "y": y}
            discrimination["in_bounds"] = in_bounds

        elif target_type == "bbox":
            bbox = target.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                return HookResult(hook_name=self.name, success=False, message="bbox target requires bbox=[x1,y1,x2,y2]")
            try:
                x1, y1, x2, y2 = [int(v) for v in bbox]
            except Exception:
                return HookResult(hook_name=self.name, success=False, message="bbox values must be integers")
            valid_shape = (x2 > x1) and (y2 > y1)
            in_bounds = (0 <= x1 < sw) and (0 <= y1 < sh) and (0 < x2 <= sw) and (0 < y2 <= sh)
            valid = valid_shape and in_bounds
            confidence = 0.95 if valid else 0.0
            cx = int((x1 + x2) / 2)
            cy = int((y1 + y2) / 2)
            recommended_click = {"x": cx, "y": cy}
            reason = "Bounding box is valid and in bounds" if valid else "Bounding box invalid or out of bounds"
            discrimination.update({"valid_shape": valid_shape, "in_bounds": in_bounds, "bbox": [x1, y1, x2, y2]})

        elif target_type == "text":
            target_text = str(target.get("value", "")).strip().lower()
            if not target_text:
                return HookResult(hook_name=self.name, success=False, message="text target requires non-empty value")

            blocks, ocr_strategy, warnings = cap._run_ocr(image_path=image_path, engine="auto")
            best_block: Optional[Dict[str, Any]] = None
            best_score = -1.0
            target_tokens = [t for t in re.split(r"[^a-z0-9]+", target_text) if t]

            for block in blocks:
                if not isinstance(block, dict):
                    continue
                text = str(block.get("text", "")).strip().lower()
                if not text:
                    continue
                ocr_conf = float(block.get("confidence", 0.0) or 0.0)
                exact = 1.0 if text == target_text else 0.0
                contains = 1.0 if (target_text in text or text in target_text) else 0.0
                tok_hits = 0
                if target_tokens:
                    tok_hits = sum(1 for tok in target_tokens if tok in text)
                    token_score = tok_hits / len(target_tokens)
                else:
                    token_score = 0.0
                score = max(exact, contains, token_score)
                score = (0.65 * score) + (0.35 * ocr_conf)
                if score > best_score:
                    best_score = score
                    best_block = block

            confidence = max(0.0, min(1.0, best_score if best_score >= 0 else 0.0))
            valid = confidence >= min_conf_val and best_block is not None
            reason = "Text target matched OCR above threshold" if valid else "Text target did not reach confidence threshold"
            if best_block and isinstance(best_block.get("bbox"), list) and len(best_block.get("bbox")) == 4:
                x1, y1, x2, y2 = [int(v) for v in best_block["bbox"]]
                recommended_click = {"x": int((x1 + x2) / 2), "y": int((y1 + y2) / 2)}
            discrimination.update(
                {
                    "selected_strategy": f"{ocr_strategy}+text_match",
                    "warnings": warnings,
                    "target_text": target_text,
                    "best_score": confidence,
                    "best_block": best_block,
                }
            )

        else:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="target.type must be one of: text, bbox, point",
            )

        details = {
            "valid": valid,
            "confidence": confidence,
            "recommended_click": recommended_click or None,
            "reason": reason,
            "image_ref": image_ref,
            "discrimination": discrimination,
        }

        return HookResult(
            hook_name=self.name,
            success=True,
            message="Click target validation complete",
            details=details,
        )

    @staticmethod
    def _get_screen_size_from_metrics() -> Dict[str, int]:
        try:
            width = int(ctypes.windll.user32.GetSystemMetrics(0))
            height = int(ctypes.windll.user32.GetSystemMetrics(1))
            return {"width": max(width, 0), "height": max(height, 0)}
        except Exception:
            return {"width": 0, "height": 0}


class WriteFileHook(BaseHook):
    name = "write_file"
    description = "Write text file contents to safe directories"

    _ALLOWED_ACTIONS = {"write"}
    _SAFE_BASE_DIRS = [
        Path.home() / "Desktop",
        Path.home() / "Desktop" / "test_dir",
        Path.home() / "AppData" / "Roaming" / "LLMind",
        Path.home() / "AppData" / "Local" / "Temp",
    ]

    def _is_safe_path(self, filepath: str) -> bool:
        """Validate that the filepath is within an allowed base directory."""
        try:
            file_path = Path(filepath).resolve()
            for safe_dir in self._SAFE_BASE_DIRS:
                safe_resolved = safe_dir.resolve()
                try:
                    file_path.relative_to(safe_resolved)
                    return True
                except ValueError:
                    continue
            return False
        except Exception:
            return False

    def execute(self, context: HookContext) -> HookResult:
        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )

        filepath = str(args.get("filepath", "")).strip()
        if not filepath:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="filepath is required",
            )

        if not self._is_safe_path(filepath):
            safe_dirs = ", ".join(str(d) for d in self._SAFE_BASE_DIRS)
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Access denied. File must be in: {safe_dirs}",
            )

        content = str(args.get("content", ""))
        overwrite = bool(args.get("overwrite", True))

        file_path = Path(filepath)
        if file_path.exists() and not overwrite:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"File already exists and overwrite=false: {filepath}",
            )

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Written {len(content)} characters to file",
                details={
                    "filepath": str(file_path),
                    "size": len(content),
                    "overwrite": overwrite,
                },
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Failed to write file: {exc}",
            )


class SendEmailSMTPHook(BaseHook):
    name = "send_email_smtp"
    description = "Send an email via SMTP using credentials from environment variables"

    _ALLOWED_ACTIONS = {"send"}
    _MAX_RECIPIENTS = 10
    _MAX_SUBJECT_LENGTH = 256
    _MAX_BODY_LENGTH = 50000
    _EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def execute(self, context: HookContext) -> HookResult:
        if os.getenv("LLMIND_ENABLE_EMAIL_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Email hooks disabled. Set LLMIND_ENABLE_EMAIL_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(hook_name=self.name, success=False, message="Invalid hook args: expected object/dict")

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: send",
            )

        # Credentials are NEVER accepted from tool args â€” env vars only.
        smtp_host = os.getenv("LLMIND_SMTP_HOST", "").strip()
        smtp_port_raw = os.getenv("LLMIND_SMTP_PORT", "587").strip()
        smtp_user = os.getenv("LLMIND_SMTP_USER", "").strip()
        smtp_password = os.getenv("LLMIND_SMTP_PASSWORD", "").strip()
        smtp_from = os.getenv("LLMIND_SMTP_FROM", smtp_user).strip()
        use_tls = os.getenv("LLMIND_SMTP_TLS", "1").strip() == "1"

        if not smtp_host:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="SMTP not configured. Set LLMIND_SMTP_HOST (and optionally LLMIND_SMTP_PORT, LLMIND_SMTP_USER, LLMIND_SMTP_PASSWORD, LLMIND_SMTP_FROM).",
            )

        try:
            smtp_port = int(smtp_port_raw)
        except ValueError:
            smtp_port = 587

        to_raw = str(args.get("to", "")).strip()
        subject = str(args.get("subject", "")).strip()
        body = str(args.get("body", "")).strip()
        cc_raw = str(args.get("cc", "")).strip()
        bcc_raw = str(args.get("bcc", "")).strip()
        is_html = bool(args.get("html", False))

        if not to_raw:
            return HookResult(hook_name=self.name, success=False, message="'to' is required")
        if not subject:
            return HookResult(hook_name=self.name, success=False, message="'subject' is required")
        if not body:
            return HookResult(hook_name=self.name, success=False, message="'body' is required")
        if len(subject) > self._MAX_SUBJECT_LENGTH:
            return HookResult(hook_name=self.name, success=False, message=f"Subject too long (max {self._MAX_SUBJECT_LENGTH})")
        if len(body) > self._MAX_BODY_LENGTH:
            return HookResult(hook_name=self.name, success=False, message=f"Body too long (max {self._MAX_BODY_LENGTH})")

        to_list = self._parse_addresses(to_raw)
        cc_list = self._parse_addresses(cc_raw) if cc_raw else []
        bcc_list = self._parse_addresses(bcc_raw) if bcc_raw else []
        all_recipients = to_list + cc_list + bcc_list

        if not to_list:
            return HookResult(hook_name=self.name, success=False, message="No valid 'to' addresses found")
        if len(all_recipients) > self._MAX_RECIPIENTS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Too many recipients (max {self._MAX_RECIPIENTS})",
            )
        for addr in all_recipients:
            if not self._EMAIL_PATTERN.match(addr):
                return HookResult(hook_name=self.name, success=False, message=f"Invalid email address: {addr}")

        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            if is_html:
                msg: Any = MIMEMultipart("alternative")
                msg.attach(MIMEText(body, "html", "utf-8"))
            else:
                msg = MIMEText(body, "plain", "utf-8")

            msg["Subject"] = subject
            msg["From"] = smtp_from
            msg["To"] = ", ".join(to_list)
            if cc_list:
                msg["Cc"] = ", ".join(cc_list)

            if use_tls:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
                server.starttls()
            else:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)

            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)

            server.sendmail(smtp_from, all_recipients, msg.as_string())
            server.quit()

            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Email sent via SMTP to {len(to_list)} recipient(s)",
                details={
                    "action": action,
                    "to": to_list,
                    "cc": cc_list,
                    "bcc": bcc_list,
                    "subject": subject,
                    "html": is_html,
                    "smtp_host": smtp_host,
                    "smtp_port": smtp_port,
                },
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"{exc.__class__.__name__}: {exc}",
            )

    def _parse_addresses(self, raw: str) -> List[str]:
        return [addr.strip() for addr in re.split(r"[,;]", raw) if addr.strip()]


class SendEmailOutlookHook(BaseHook):
    name = "send_email_outlook"
    description = "Send an email via local Microsoft Outlook COM interface (Windows only)"

    _ALLOWED_ACTIONS = {"send"}
    _MAX_RECIPIENTS = 10
    _MAX_SUBJECT_LENGTH = 256
    _MAX_BODY_LENGTH = 50000
    _EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def execute(self, context: HookContext) -> HookResult:
        if os.name != "nt":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="send_email_outlook is only available on Windows 10/11",
            )

        if os.getenv("LLMIND_ENABLE_EMAIL_HOOKS", "0").strip() != "1":
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Email hooks disabled. Set LLMIND_ENABLE_EMAIL_HOOKS=1 to enable.",
            )

        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(hook_name=self.name, success=False, message="Invalid hook args: expected object/dict")

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: send",
            )

        to_raw = str(args.get("to", "")).strip()
        subject = str(args.get("subject", "")).strip()
        body = str(args.get("body", "")).strip()
        cc_raw = str(args.get("cc", "")).strip()
        bcc_raw = str(args.get("bcc", "")).strip()
        is_html = bool(args.get("html", False))

        if not to_raw:
            return HookResult(hook_name=self.name, success=False, message="'to' is required")
        if not subject:
            return HookResult(hook_name=self.name, success=False, message="'subject' is required")
        if not body:
            return HookResult(hook_name=self.name, success=False, message="'body' is required")
        if len(subject) > self._MAX_SUBJECT_LENGTH:
            return HookResult(hook_name=self.name, success=False, message=f"Subject too long (max {self._MAX_SUBJECT_LENGTH})")
        if len(body) > self._MAX_BODY_LENGTH:
            return HookResult(hook_name=self.name, success=False, message=f"Body too long (max {self._MAX_BODY_LENGTH})")

        to_list = self._parse_addresses(to_raw)
        cc_list = self._parse_addresses(cc_raw) if cc_raw else []
        bcc_list = self._parse_addresses(bcc_raw) if bcc_raw else []
        all_recipients = to_list + cc_list + bcc_list

        if not to_list:
            return HookResult(hook_name=self.name, success=False, message="No valid 'to' addresses found")
        if len(all_recipients) > self._MAX_RECIPIENTS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Too many recipients (max {self._MAX_RECIPIENTS})",
            )
        for addr in all_recipients:
            if not self._EMAIL_PATTERN.match(addr):
                return HookResult(hook_name=self.name, success=False, message=f"Invalid email address: {addr}")

        try:
            import win32com.client  # type: ignore
        except ImportError:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="pywin32 not installed. Run: pip install pywin32",
            )

        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            mail = outlook.CreateItem(0)  # olMailItem = 0
            mail.To = "; ".join(to_list)
            mail.Subject = subject
            if cc_list:
                mail.CC = "; ".join(cc_list)
            if bcc_list:
                mail.BCC = "; ".join(bcc_list)
            if is_html:
                mail.HTMLBody = body
            else:
                mail.Body = body
            mail.Send()

            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Outlook email sent to {len(to_list)} recipient(s)",
                details={
                    "action": action,
                    "to": to_list,
                    "cc": cc_list,
                    "bcc": bcc_list,
                    "subject": subject,
                    "html": is_html,
                },
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"{exc.__class__.__name__}: {exc}",
            )

    def _parse_addresses(self, raw: str) -> List[str]:
        return [addr.strip() for addr in re.split(r"[,;]", raw) if addr.strip()]


class DownloadUrlHook(BaseHook):
    """Download a URL to a local file with media type validation."""
    
    name = "download_url"
    description = "Download URL content to local file with media type filtering"
    
    _ALLOWED_ACTIONS = {"download"}
    _CREDIBLE_MEDIA_TYPES = {
        "image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp", "image/bmp", "image/svg+xml",
        "audio/mpeg", "audio/wav", "audio/ogg", "audio/aac", "audio/flac",
        "video/mp4", "video/mpeg", "video/quicktime", "video/x-msvideo", "video/webm",
        "application/pdf", "application/msword", "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    _MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
    _DOWNLOAD_TIMEOUT_SECONDS = 45
    _MAX_RETRIES = 3
    _CHUNK_SIZE = 64 * 1024

    @staticmethod
    def _normalize_save_path(raw_path: str) -> Path:
        expanded = os.path.expandvars(os.path.expanduser(raw_path.strip()))
        candidate = Path(expanded)
        if not candidate.is_absolute():
            candidate = Path(tempfile.gettempdir()) / candidate
        return candidate

    @staticmethod
    def _is_allowed_scheme(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme.lower() in {"http", "https"}

    def _download_once(
        self,
        request: Request,
        target_path: Path,
        allowed_types_set: set,
    ) -> HookResult:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_target = target_path.with_suffix(target_path.suffix + ".part")
        file_size = 0
        content_type = ""
        try:
            with urlopen(request, timeout=self._DOWNLOAD_TIMEOUT_SECONDS) as response:
                content_type = str(response.headers.get("Content-Type", "")).split(";")[0].lower().strip()

                if allowed_types_set and content_type and content_type not in allowed_types_set:
                    return HookResult(
                        hook_name=self.name,
                        success=False,
                        message=f"Content-Type '{content_type}' not in allowed types: {allowed_types_set}",
                    )

                with open(temp_target, "wb") as f:
                    while True:
                        chunk = response.read(self._CHUNK_SIZE)
                        if not chunk:
                            break
                        file_size += len(chunk)
                        if file_size > self._MAX_FILE_SIZE:
                            f.close()
                            temp_target.unlink(missing_ok=True)
                            return HookResult(
                                hook_name=self.name,
                                success=False,
                                message=f"File size exceeds limit ({self._MAX_FILE_SIZE} bytes)",
                            )
                        f.write(chunk)

            if file_size <= 0:
                temp_target.unlink(missing_ok=True)
                return HookResult(
                    hook_name=self.name,
                    success=False,
                    message="Downloaded file is empty",
                )

            temp_target.replace(target_path)
            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Downloaded {file_size} bytes to {target_path}",
                details={
                    "save_path": str(target_path),
                    "file_size": file_size,
                    "content_type": content_type,
                },
            )
        except Exception as exc:
            temp_target.unlink(missing_ok=True)
            raise exc
    
    def execute(self, context: HookContext) -> HookResult:
        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )
        
        url = str(args.get("url", "")).strip()
        if not url:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="'url' parameter is required",
            )
        if not self._is_allowed_scheme(url):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Only http/https URLs are supported",
            )
        
        save_path = str(args.get("save_path", "")).strip()
        if not save_path:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="'save_path' parameter is required",
            )
        
        overwrite = bool(args.get("overwrite", True))
        allowed_types = args.get("allowed_types", [])
        if isinstance(allowed_types, str):
            allowed_types = [allowed_types]
        elif not isinstance(allowed_types, list):
            allowed_types = []
        
        allowed_types_set = set()
        for item in allowed_types:
            if isinstance(item, str):
                allowed_types_set.add(item.lower().strip())
        
        if "image/*" in allowed_types_set:
            allowed_types_set.update(t for t in self._CREDIBLE_MEDIA_TYPES if t.startswith("image/"))
        if "audio/*" in allowed_types_set:
            allowed_types_set.update(t for t in self._CREDIBLE_MEDIA_TYPES if t.startswith("audio/"))
        if "video/*" in allowed_types_set:
            allowed_types_set.update(t for t in self._CREDIBLE_MEDIA_TYPES if t.startswith("video/"))
        
        allowed_types_set.discard("*/*")
        
        target_path = self._normalize_save_path(save_path)
        if target_path.exists() and not overwrite:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"File already exists and overwrite=False: {target_path}",
            )
        
        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )

        last_error = ""
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                outcome = self._download_once(request, target_path, allowed_types_set)
                if outcome.success:
                    outcome.details["url"] = url
                    outcome.details["attempt"] = attempt
                    outcome.details["max_retries"] = self._MAX_RETRIES
                return outcome
            except HTTPError as exc:
                if 400 <= int(exc.code) < 500:
                    return HookResult(
                        hook_name=self.name,
                        success=False,
                        message=f"HTTP error {exc.code}: {exc.reason}",
                    )
                last_error = f"HTTP error {exc.code}: {exc.reason}"
            except URLError as exc:
                last_error = f"URL error: {exc.reason}"
            except TimeoutError as exc:
                last_error = f"Timeout: {exc}"
            except Exception as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"

            if attempt < self._MAX_RETRIES:
                time.sleep(0.5 * attempt)

        return HookResult(
            hook_name=self.name,
            success=False,
            message=f"Download failed after {self._MAX_RETRIES} attempts: {last_error}",
            details={
                "url": url,
                "save_path": str(target_path),
                "max_retries": self._MAX_RETRIES,
            },
        )


class ParseHtmlForMediaHook(BaseHook):
    """Parse HTML for media URLs (images, audio, video, documents)."""
    
    name = "parse_html_for_media"
    description = "Extract media URLs from HTML content"
    
    _ALLOWED_ACTIONS = {"extract"}
    _MEDIA_PATTERNS = {
        "image": [
            r'src=(["\'])([^"\']+\.(?:jpg|jpeg|png|gif|webp|bmp|svg))\1',
            r'href=(["\'])([^"\']+\.(?:jpg|jpeg|png|gif|webp|bmp|svg))\1',
            r'<img[^>]+src=(["\'])([^"\']+)\1',
            r'(?:url|image|src|background):\s*["\']?([^\s"\'>;]+\.(?:jpg|jpeg|png|gif|webp|bmp|svg))',
        ],
        "audio": [
            r'src=(["\'])([^"\']+\.(?:mp3|wav|ogg|aac|flac|m4a))\1',
            r'href=(["\'])([^"\']+\.(?:mp3|wav|ogg|aac|flac|m4a))\1',
        ],
        "video": [
            r'src=(["\'])([^"\']+\.(?:mp4|webm|mpeg|mov|avi))\1',
            r'href=(["\'])([^"\']+\.(?:mp4|webm|mpeg|mov|avi))\1',
            r'<video[^>]+src=(["\'])([^"\']+)\1',
        ],
        "document": [
            r'href=(["\'])([^"\']+\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx))\1',
            r'src=(["\'])([^"\']+\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx))\1',
        ],
    }
    
    def execute(self, context: HookContext) -> HookResult:
        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(
                hook_name=self.name,
                success=False,
                message="Invalid hook args: expected object/dict",
            )

        # Backward compatibility: treat missing/blank action as default extract.
        action = str(args.get("action") or "extract").strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: {', '.join(sorted(self._ALLOWED_ACTIONS))}",
            )
        
        html = str(args.get("html", "")).strip()
        if not html:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="'html' parameter is required",
            )
        
        media_types = args.get("media_types", ["image"])
        if isinstance(media_types, str):
            media_types = [media_types]
        elif not isinstance(media_types, list):
            media_types = ["image"]
        
        media_types = [str(t).lower().strip() for t in media_types if t]
        if not media_types:
            media_types = ["image"]
        
        max_results = args.get("max_results", 50)
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = 50
        max_results = min(max(max_results, 1), 500)
        
        results = {}
        for media_type in media_types:
            if media_type not in self._MEDIA_PATTERNS:
                continue
            
            urls = []
            seen = set()
            
            for pattern in self._MEDIA_PATTERNS[media_type]:
                matches = re.finditer(pattern, html, re.IGNORECASE)
                for match in matches:
                    if len(urls) >= max_results:
                        break
                    url = match.group(2) if len(match.groups()) >= 2 else match.group(1)
                    url = url.strip().strip("'\"")
                    if url and url not in seen:
                        seen.add(url)
                        urls.append(url)
            
            results[media_type] = urls[:max_results]
        
        return HookResult(
            hook_name=self.name,
            success=True,
            message=f"Extracted media URLs from HTML",
            details={
                "media_types": media_types,
                "results": results,
                "total_found": sum(len(urls) for urls in results.values()),
            },
        )


class HookRegistry:
    """Registry that validates and executes hooks via a shared contract."""

    def __init__(self, app_name: str = "LLMind") -> None:
        self.app_name = app_name
        self._hooks: Dict[str, BaseHook] = {}

    def register(self, hook: BaseHook) -> None:
        self._hooks[hook.name] = hook

    def register_builtin_hooks(self) -> None:
        self.register(FileSystemAccessHook())
        self.register(RegistrySettingsHook())
        self.register(WindowsUIManipulationHook())
        self.register(WindowsMetricsHook())
        self.register(LaunchProcessHook())
        self.register(CaptureScreenshotHook())
        self.register(BrowserNavigationHook())
        self.register(FetchWebpageHTMLHook())
        self.register(ParseHTMLContentHook())
        self.register(SystemCommandHook())
        self.register(OrchestrateWorkflowHook())
        self.register(ReadFileHook())
        self.register(ListDirectoryHook())
        self.register(QueryBrowserHistoryHook())
        self.register(CaptureAndOCRScreenHook())
        self.register(DetectVisualObjectsHook())
        self.register(AnalyzeUIWithVisionModelHook())
        self.register(VerifyUIChangeHook())
        self.register(ValidateClickTargetHook())
        self.register(WriteFileHook())
        self.register(SendEmailSMTPHook())
        self.register(SendEmailOutlookHook())
        self.register(DownloadUrlHook())
        self.register(ParseHtmlForMediaHook())

    def list_hook_names(self) -> List[str]:
        return sorted(self._hooks.keys())

    def build_context(self, app_data_dir: Path, extras: Optional[Dict[str, object]] = None) -> HookContext:
        return HookContext(app_data_dir=app_data_dir, app_name=self.app_name, extras=extras or {})

    def execute(self, hook_name: str, context: HookContext) -> HookResult:
        hook = self._hooks.get(hook_name)
        if hook is None:
            available = ", ".join(self.list_hook_names())
            return HookResult(
                hook_name=hook_name,
                success=False,
                message=f"Unknown hook '{hook_name}'. Available: {available}",
            )
        return hook.execute(context)

    def execute_many(self, hook_names: Iterable[str], context: HookContext) -> List[HookResult]:
        return [self.execute(name, context) for name in hook_names]

    def validate_hook_names(self, hook_names: Iterable[str]) -> List[str]:
        names = list(hook_names)
        missing = [name for name in names if name not in self._hooks]
        if missing:
            raise ValueError(f"Unknown hooks requested for generation: {', '.join(missing)}")
        return names

    def generate_persistent_hook_module(self, hook_names: Iterable[str], output_file: Path) -> Path:
        """Create a persistent Python module that registers validated hooks.

        This avoids on-the-fly internal wiring by writing explicit, reviewable
        code that can be checked in and versioned.
        """
        selected = self.validate_hook_names(hook_names)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        class_map = {
            "filesystem_access": "FileSystemAccessHook",
            "registry_settings": "RegistrySettingsHook",
            "windows_ui_action": "WindowsUIManipulationHook",
            "windows_metrics": "WindowsMetricsHook",
            "launch_process": "LaunchProcessHook",
            "capture_screenshot": "CaptureScreenshotHook",
            "browser_navigation": "BrowserNavigationHook",
            "fetch_webpage_html": "FetchWebpageHTMLHook",
            "parse_html_content": "ParseHTMLContentHook",
            "system_command": "SystemCommandHook",
            "orchestrate_workflow": "OrchestrateWorkflowHook",
            "read_file": "ReadFileHook",
            "list_directory": "ListDirectoryHook",
            "query_browser_history": "QueryBrowserHistoryHook",
            "capture_and_ocr_screen": "CaptureAndOCRScreenHook",
            "detect_visual_objects": "DetectVisualObjectsHook",
            "analyze_ui_with_vision_model": "AnalyzeUIWithVisionModelHook",
            "verify_ui_change": "VerifyUIChangeHook",
            "validate_click_target": "ValidateClickTargetHook",
            "write_file": "WriteFileHook",
            "send_email_smtp": "SendEmailSMTPHook",
            "send_email_outlook": "SendEmailOutlookHook",
            "download_url": "DownloadUrlHook",
            "parse_html_for_media": "ParseHtmlForMediaHook",
        }
        imports = sorted({class_map[name] for name in selected})

        lines = [
            "from __future__ import annotations",
            "",
            "from hooks.hook_registry import HookRegistry, " + ", ".join(imports),
            "",
            "",
            "def build_registry(app_name: str = \"LLMind\") -> HookRegistry:",
            "    registry = HookRegistry(app_name=app_name)",
        ]
        for name in selected:
            lines.append(f"    registry.register({class_map[name]}())")
        lines.extend(
            [
                "    return registry",
                "",
                "",
                "ENABLED_HOOKS = [",
            ]
        )
        for name in selected:
            lines.append(f"    \"{name}\",")
        lines.extend(["]", ""])

        output_file.write_text("\n".join(lines), encoding="utf-8")
        return output_file

