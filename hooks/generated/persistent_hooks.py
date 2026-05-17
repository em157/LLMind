from __future__ import annotations

from hooks.hook_registry import HookRegistry, FileSystemAccessHook, RegistrySettingsHook, WindowsUIManipulationHook, LaunchProcessHook


def build_registry(app_name: str = "LLMind") -> HookRegistry:
    registry = HookRegistry(app_name=app_name)
    registry.register(FileSystemAccessHook())
    registry.register(RegistrySettingsHook())
    registry.register(WindowsUIManipulationHook())
    registry.register(LaunchProcessHook())
    return registry


ENABLED_HOOKS = [
    "filesystem_access",
    "registry_settings",
    "windows_ui_action",
    "launch_process",
]
