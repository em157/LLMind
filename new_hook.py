from __future__ import annotations

from hooks.hook_registry import HookRegistry, FileSystemAccessHook, LaunchProcessHook, RegistrySettingsHook, WindowsUIManipulationHook


def build_registry(app_name: str = "LLMind") -> HookRegistry:
    registry = HookRegistry(app_name=app_name)
    registry.register(FileSystemAccessHook())
    registry.register(LaunchProcessHook())
    registry.register(RegistrySettingsHook())
    registry.register(WindowsUIManipulationHook())
    return registry


ENABLED_HOOKS = [
    "filesystem_access",
    "launch_process",
    "registry_settings",
    "windows_ui_action",
]
