from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

from ctypes import wintypes

try:
    import requests as _requests
except Exception:
    _requests = None

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    BeautifulSoup = None


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


def _resolve_user_path_alias(path_value: str) -> Path:
    """Resolve Desktop/AppData relative aliases to absolute user paths."""
    raw = str(path_value or "").strip()
    if not raw:
        return Path(raw)

    normalized = raw.replace("/", "\\")
    lower = normalized.lower()

    if lower.startswith("desktop\\"):
        return Path.home() / "Desktop" / normalized[len("desktop\\") :]
    if lower == "desktop":
        return Path.home() / "Desktop"

    if lower.startswith("appdata\\roaming\\"):
        return Path.home() / "AppData" / "Roaming" / normalized[len("appdata\\roaming\\") :]
    if lower == "appdata\\roaming":
        return Path.home() / "AppData" / "Roaming"

    if lower.startswith("appdata\\local\\"):
        return Path.home() / "AppData" / "Local" / normalized[len("appdata\\local\\") :]
    if lower == "appdata\\local":
        return Path.home() / "AppData" / "Local"

    return Path(raw)


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

        if ctypes.windll.user32.SetCursorPos(x, y) == 0:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"SetCursorPos failed for ({x}, {y})",
            )

        if button == "left":
            down, up = 0x0002, 0x0004
        else:
            down, up = 0x0008, 0x0010
        ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)

        return HookResult(
            hook_name=self.name,
            success=True,
            message=f"Clicked {button} at ({x}, {y})",
            details={"action": "click", "button": button, "x": x, "y": y},
        )

    @staticmethod
    def _click_window_center(hwnd: int) -> None:
        rect = wintypes.RECT()
        ok = ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
        if not ok:
            return

        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return

        x = int(rect.left + (width * 0.5))
        y = int(rect.top + (height * 0.5))
        if ctypes.windll.user32.SetCursorPos(x, y) == 0:
            return

        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.04)

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

        target_hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if not target_hwnd and hwnd is not None:
            target_hwnd = int(hwnd)

        if not target_hwnd:
            return HookResult(
                hook_name=self.name,
                success=False,
                message="No target window available for type_text",
            )

        # Rich editors such as WordPad often require a click in the document surface
        # to place caret focus before keyboard input arrives.
        self._click_window_center(target_hwnd)

        if self._insert_text_direct(target_hwnd, text, bool(args.get("press_enter", False))):
            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Inserted {len(text)} character(s) into target document control",
                details={"action": "type_text", "length": len(text), "target_hwnd": target_hwnd, "method": "direct_control"},
            )

        sent = self._send_text_via_sendinput(target_hwnd, text, bool(args.get("press_enter", False)))
        if not sent:
            self._post_text_via_messages(target_hwnd, text, bool(args.get("press_enter", False)))

        return HookResult(
            hook_name=self.name,
            success=True,
            message=f"Typed {len(text)} character(s) into target window",
            details={"action": "type_text", "length": len(text), "target_hwnd": target_hwnd},
        )

    def _send_text_via_sendinput(self, target_hwnd: int, text: str, press_enter: bool) -> bool:
        # SendInput targets the active control in the foreground window, which is
        # more reliable for modern Notepad/WordPad than posting WM_CHAR to the top-level hwnd.
        user32 = ctypes.windll.user32
        self._activate_window(target_hwnd)
        time.sleep(0.06)

        current_foreground = int(user32.GetForegroundWindow() or 0)
        if current_foreground != int(target_hwnd):
            return False

        keyeventf_keyup = 0x0002
        keyeventf_unicode = 0x0004
        input_keyboard = 1

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", wintypes.LPARAM),
            ]

        class _INPUT_UNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_UNION)]

        send_input = user32.SendInput
        send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        send_input.restype = wintypes.UINT

        inputs: List[INPUT] = []
        for ch in text:
            code = ord(ch)
            inputs.append(
                INPUT(
                    type=input_keyboard,
                    union=_INPUT_UNION(ki=KEYBDINPUT(0, code, keyeventf_unicode, 0, 0)),
                )
            )
            inputs.append(
                INPUT(
                    type=input_keyboard,
                    union=_INPUT_UNION(ki=KEYBDINPUT(0, code, keyeventf_unicode | keyeventf_keyup, 0, 0)),
                )
            )

        if press_enter:
            vk_return = 0x0D
            inputs.append(
                INPUT(
                    type=input_keyboard,
                    union=_INPUT_UNION(ki=KEYBDINPUT(vk_return, 0, 0, 0, 0)),
                )
            )
            inputs.append(
                INPUT(
                    type=input_keyboard,
                    union=_INPUT_UNION(ki=KEYBDINPUT(vk_return, 0, keyeventf_keyup, 0, 0)),
                )
            )

        if not inputs:
            return False

        arr = (INPUT * len(inputs))(*inputs)
        sent_count = int(send_input(len(arr), arr, ctypes.sizeof(INPUT)))
        return sent_count == len(arr)

    def _post_text_via_messages(self, target_hwnd: int, text: str, press_enter: bool) -> None:
        message_hwnd = self._resolve_text_message_hwnd(target_hwnd)
        wm_char = 0x0102
        for ch in text:
            ctypes.windll.user32.PostMessageW(wintypes.HWND(message_hwnd), wm_char, ord(ch), 0)

        if press_enter:
            vk_return = 0x0D
            wm_keydown = 0x0100
            wm_keyup = 0x0101
            ctypes.windll.user32.PostMessageW(wintypes.HWND(message_hwnd), wm_keydown, vk_return, 0)
            ctypes.windll.user32.PostMessageW(wintypes.HWND(message_hwnd), wm_keyup, vk_return, 0)

    def _insert_text_direct(self, target_hwnd: int, text: str, press_enter: bool) -> bool:
        message_hwnd = self._resolve_text_message_hwnd(target_hwnd)
        class_name = self._get_window_class_name(message_hwnd).upper()
        if class_name not in {"EDIT", "RICHEDIT20W", "RICHEDIT50W", "RICHEDITD2DPT"}:
            return False

        em_setsel = 0x00B1
        em_replacesel = 0x00C2
        payload = text + ("\r\n" if press_enter else "")
        user32 = ctypes.windll.user32

        # Move caret to document end and insert text directly into the editor control.
        user32.SendMessageW(wintypes.HWND(message_hwnd), em_setsel, -1, -1)
        inserted = user32.SendMessageW(
            wintypes.HWND(message_hwnd),
            em_replacesel,
            1,
            ctypes.c_wchar_p(payload),
        )
        return bool(inserted)

    @staticmethod
    def _get_window_class_name(hwnd: int) -> str:
        if hwnd <= 0:
            return ""
        get_class_name = ctypes.windll.user32.GetClassNameW
        buf = ctypes.create_unicode_buffer(256)
        count = int(get_class_name(wintypes.HWND(hwnd), buf, 256) or 0)
        if count <= 0:
            return ""
        return str(buf.value)

    @staticmethod
    def _resolve_text_message_hwnd(parent_hwnd: int) -> int:
        user32 = ctypes.windll.user32
        find_window_ex = user32.FindWindowExW
        for class_name in ("RICHEDIT50W", "RICHEDIT20W", "RichEditD2DPT", "Edit"):
            child = int(find_window_ex(wintypes.HWND(parent_hwnd), wintypes.HWND(0), class_name, None) or 0)
            if child > 0:
                return child
        return parent_hwnd

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
        kernel32 = ctypes.windll.kernel32
        sw_restore = 9
        user32.ShowWindow(wintypes.HWND(hwnd), sw_restore)
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        user32.SetActiveWindow(wintypes.HWND(hwnd))

        if user32.SetForegroundWindow(wintypes.HWND(hwnd)):
            return True

        foreground = int(user32.GetForegroundWindow() or 0)
        current_thread_id = int(kernel32.GetCurrentThreadId())
        target_thread_id = int(user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None) or 0)
        foreground_thread_id = int(user32.GetWindowThreadProcessId(wintypes.HWND(foreground), None) or 0)

        if foreground_thread_id:
            user32.AttachThreadInput(current_thread_id, foreground_thread_id, True)
        if target_thread_id:
            user32.AttachThreadInput(current_thread_id, target_thread_id, True)

        try:
            user32.ShowWindow(wintypes.HWND(hwnd), sw_restore)
            user32.BringWindowToTop(wintypes.HWND(hwnd))
            user32.SetForegroundWindow(wintypes.HWND(hwnd))
            return int(user32.GetForegroundWindow() or 0) == int(hwnd)
        finally:
            if target_thread_id:
                user32.AttachThreadInput(current_thread_id, target_thread_id, False)
            if foreground_thread_id:
                user32.AttachThreadInput(current_thread_id, foreground_thread_id, False)


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

        expected_text_any = self._coerce_text_list(args.get("expected_text_any"), "expected_text_any")
        expected_text_all = self._coerce_text_list(args.get("expected_text_all"), "expected_text_all")
        ocr_notes = str(args.get("ocr_notes", "")).strip()

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
                    "expected_text_any": expected_text_any,
                    "expected_text_all": expected_text_all,
                    "ocr_notes": ocr_notes,
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
    def _coerce_text_list(value: Any, field_name: str) -> List[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"Invalid {field_name}: expected array of strings")
        normalized: List[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"Invalid {field_name}: all entries must be strings")
            text = item.strip()
            if text:
                normalized.append(text)
        return normalized

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


class WebFetchParseHook(BaseHook):
    name = "web_fetch_parse"
    description = "Fetch an HTTP/HTTPS page and parse links/images using BeautifulSoup"

    _ALLOWED_ACTIONS = {"fetch_parse"}
    _MAX_HTML_CHARS = 2000000
    _MAX_RESULTS = 100

    def execute(self, context: HookContext) -> HookResult:
        args = context.extras.get("hook_args", {})
        if not isinstance(args, dict):
            return HookResult(hook_name=self.name, success=False, message="Invalid hook args: expected object/dict")

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: fetch_parse",
            )

        url = str(args.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return HookResult(hook_name=self.name, success=False, message="Only http/https URLs are allowed")

        max_items = args.get("max_items", 20)
        try:
            max_items = int(max_items)
        except (TypeError, ValueError):
            max_items = 20
        max_items = max(1, min(self._MAX_RESULTS, max_items))

        try:
            html_text, final_url, status_code, content_type = self._fetch_page(url)
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Fetch failed: {exc.__class__.__name__}: {exc}",
            )

        title = ""
        links: List[str] = []
        image_urls: List[str] = []
        listing_candidates: List[Dict[str, str]] = []

        if BeautifulSoup is not None:
            soup = BeautifulSoup(html_text, "html.parser")
            if soup.title and soup.title.string:
                title = soup.title.string.strip()

            seen_links = set()
            for anchor in soup.find_all("a", href=True):
                href = str(anchor.get("href", "")).strip()
                if not href:
                    continue
                absolute = urljoin(final_url, href)
                if absolute in seen_links:
                    continue
                seen_links.add(absolute)
                links.append(absolute)
                if len(links) >= max_items:
                    break

            seen_images = set()
            for img in soup.find_all("img", src=True):
                src = str(img.get("src", "")).strip()
                if not src:
                    continue
                absolute = urljoin(final_url, src)
                if absolute in seen_images:
                    continue
                seen_images.add(absolute)
                image_urls.append(absolute)
                if len(image_urls) >= max_items:
                    break

            seen_candidate_urls = set()
            for anchor in soup.find_all("a", href=True):
                link_text = " ".join(anchor.get_text(" ", strip=True).split())
                href = str(anchor.get("href", "")).strip()
                if not href:
                    continue
                absolute = urljoin(final_url, href)
                parsed_link = urlparse(absolute)
                if parsed_link.scheme not in {"http", "https"}:
                    continue
                if absolute in seen_candidate_urls:
                    continue

                anchor_classes = " ".join(anchor.get("class", []) if isinstance(anchor.get("class", []), list) else [])
                hay = f"{link_text} {absolute} {anchor_classes}".lower()
                is_listing_like = (
                    "/d/" in parsed_link.path
                    or parsed_link.path.endswith(".html")
                    or "result-title" in hay
                    or "listing" in hay
                )
                if not is_listing_like:
                    continue

                hay = f"{link_text} {absolute}".lower()
                if "bicycle" not in hay and "bike" not in hay and "bia" not in parsed_link.path.lower():
                    continue
                seen_candidate_urls.add(absolute)
                listing_candidates.append(
                    {
                        "title": link_text[:200] if link_text else absolute,
                        "url": absolute,
                    }
                )
                if len(listing_candidates) >= max_items:
                    break

            if not listing_candidates:
                for absolute in links:
                    parsed_link = urlparse(absolute)
                    if parsed_link.scheme not in {"http", "https"}:
                        continue
                    if "/d/" not in parsed_link.path and not parsed_link.path.endswith(".html"):
                        continue
                    if absolute in seen_candidate_urls:
                        continue
                    seen_candidate_urls.add(absolute)
                    listing_candidates.append({"title": absolute, "url": absolute})
                    if len(listing_candidates) >= max_items:
                        break
        else:
            title_match = re.search(r"<title>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
            if title_match:
                title = " ".join(title_match.group(1).split())

        snippet = re.sub(r"\s+", " ", html_text)[:500]
        return HookResult(
            hook_name=self.name,
            success=True,
            message="Fetched and parsed HTML page",
            details={
                "action": action,
                "url": url,
                "final_url": final_url,
                "status_code": status_code,
                "content_type": content_type,
                "title": title,
                "html_char_count": len(html_text),
                "snippet": snippet,
                "links": links,
                "image_urls": image_urls,
                "listing_candidates": listing_candidates,
                "parser": "bs4" if BeautifulSoup is not None else "regex_fallback",
            },
        )

    def _fetch_page(self, url: str) -> tuple[str, str, int, str]:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LLMind/1.0"}
        if _requests is not None:
            response = _requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            content_type = str(response.headers.get("content-type", "")).lower()
            if "html" not in content_type and "xml" not in content_type:
                raise ValueError(f"Unsupported content-type for HTML parsing: {content_type or 'unknown'}")
            text = response.text[: self._MAX_HTML_CHARS]
            return text, str(response.url), int(response.status_code), content_type

        from urllib import request as _request

        req = _request.Request(url, headers=headers)
        with _request.urlopen(req, timeout=15) as resp:
            raw = resp.read(self._MAX_HTML_CHARS + 1)
            text = raw[: self._MAX_HTML_CHARS].decode("utf-8", errors="replace")
            final_url = str(getattr(resp, "url", url))
            status = int(getattr(resp, "status", 200))
            content_type = str(resp.headers.get("content-type", "")).lower()
            if "html" not in content_type and "xml" not in content_type:
                raise ValueError(f"Unsupported content-type for HTML parsing: {content_type or 'unknown'}")
            return text, final_url, status, content_type


class DownloadRemoteFileHook(BaseHook):
    name = "download_remote_file"
    description = "Download a remote HTTP/HTTPS file into Desktop/AppData safe directories"

    _ALLOWED_ACTIONS = {"download"}
    _SAFE_BASE_DIRS = [
        Path.home() / "Desktop",
        Path.home() / "Desktop" / "test_dir",
        Path.home() / "AppData" / "Roaming" / "LLMind",
        Path.home() / "AppData" / "Local" / "Temp",
    ]
    _MAX_BYTES = 10 * 1024 * 1024

    def _is_safe_path(self, filepath: str) -> bool:
        try:
            file_path = _resolve_user_path_alias(filepath).resolve()
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
            return HookResult(hook_name=self.name, success=False, message="Invalid hook args: expected object/dict")

        action = str(args.get("action", "")).strip().lower()
        if action not in self._ALLOWED_ACTIONS:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Unsupported action '{action}'. Allowed: download",
            )

        url = str(args.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return HookResult(hook_name=self.name, success=False, message="Only http/https URLs are allowed")

        filepath = str(args.get("filepath", "")).strip()
        if not filepath:
            return HookResult(hook_name=self.name, success=False, message="filepath is required")
        if not self._is_safe_path(filepath):
            safe_dirs = ", ".join(str(d) for d in self._SAFE_BASE_DIRS)
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Access denied. File must be in: {safe_dirs}",
            )

        overwrite = bool(args.get("overwrite", True))
        max_bytes = args.get("max_bytes", self._MAX_BYTES)
        try:
            max_bytes = int(max_bytes)
        except (TypeError, ValueError):
            max_bytes = self._MAX_BYTES
        max_bytes = max(1, min(self._MAX_BYTES, max_bytes))

        output_path = _resolve_user_path_alias(filepath)
        if output_path.exists() and not overwrite:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"File already exists and overwrite=false: {output_path}",
            )

        try:
            content, status_code, content_type = self._download_bytes(url, max_bytes)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as handle:
                handle.write(content)
            return HookResult(
                hook_name=self.name,
                success=True,
                message=f"Downloaded {len(content)} bytes",
                details={
                    "action": action,
                    "url": url,
                    "filepath": str(output_path),
                    "bytes": len(content),
                    "status_code": status_code,
                    "content_type": content_type,
                    "overwrite": overwrite,
                },
            )
        except Exception as exc:
            return HookResult(
                hook_name=self.name,
                success=False,
                message=f"Download failed: {exc.__class__.__name__}: {exc}",
            )

    def _download_bytes(self, url: str, max_bytes: int) -> tuple[bytes, int, str]:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LLMind/1.0"}
        if _requests is not None:
            response = _requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            content = bytes(response.content)
            if not content:
                raise ValueError("Empty response body")
            if len(content) > max_bytes:
                raise ValueError(f"Remote file exceeds max_bytes={max_bytes}")
            return content, int(response.status_code), str(response.headers.get("content-type", ""))

        from urllib import request as _request

        req = _request.Request(url, headers=headers)
        with _request.urlopen(req, timeout=20) as resp:
            content = resp.read(max_bytes + 1)
            if not content:
                raise ValueError("Empty response body")
            if len(content) > max_bytes:
                raise ValueError(f"Remote file exceeds max_bytes={max_bytes}")
            status = int(getattr(resp, "status", 200))
            content_type = str(resp.headers.get("content-type", ""))
            return content, status, content_type


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
        "web_fetch_parse",
        "download_remote_file",
        "windows_ui_action",
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
            file_path = _resolve_user_path_alias(filepath).resolve()
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

        file_path = _resolve_user_path_alias(filepath)
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
            dir_path = _resolve_user_path_alias(dirpath).resolve()
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

        dir_path = _resolve_user_path_alias(dirpath)
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
            file_path = _resolve_user_path_alias(filepath).resolve()
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

        file_path = _resolve_user_path_alias(filepath)
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

        # Credentials are NEVER accepted from tool args — env vars only.
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
        self.register(WebFetchParseHook())
        self.register(DownloadRemoteFileHook())
        self.register(SystemCommandHook())
        self.register(OrchestrateWorkflowHook())
        self.register(ReadFileHook())
        self.register(ListDirectoryHook())
        self.register(WriteFileHook())
        self.register(SendEmailSMTPHook())
        self.register(SendEmailOutlookHook())

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
            "launch_process": "LaunchProcessHook",
            "capture_screenshot": "CaptureScreenshotHook",
            "browser_navigation": "BrowserNavigationHook",
            "web_fetch_parse": "WebFetchParseHook",
            "download_remote_file": "DownloadRemoteFileHook",
            "system_command": "SystemCommandHook",
            "orchestrate_workflow": "OrchestrateWorkflowHook",
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
