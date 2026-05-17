"""LLMind CLI template for Windows 10/11 CMD.

This file intentionally contains the full template codebase requested in test.py,
including cache management, appdata writer behavior, progress output hooks, and
API key onboarding flow.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

# When running the script from the `main/` directory, sibling packages (network, cache, appdata)
# aren't on sys.path by default. Add the repository root to sys.path so imports like
# `from network.requests import ...` work in both development and packaged runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from network.requests import perform_api_request
from network.providers import (
    DEFAULT_MODELS,
    build_payload_from_user_input,
    detect_provider,
    fetch_anthropic_models,
    fetch_gemini_models,
    fetch_openai_models,
    fetch_xai_models,
    get_response_template_name,
)
from hooks.hook_registry import HookRegistry
from response.response_handler import extract_file_artifact_candidates


APP_NAME = "LLMind"
CACHE_FILENAME = "api_cache.json"
SETTINGS_FILENAME = "settings.json"
ARTIFACTS_DIRNAME = "artifacts"
EXECUTABLE_CACHE_FILENAME = "executable_cache.json"

os.environ.setdefault("LLMIND_ENABLE_UI_HOOKS", "1")
os.environ.setdefault("LLMIND_ENABLE_LAUNCH_HOOKS", "1")
os.environ.setdefault("LLMIND_ENABLE_COMMAND_HOOKS", "1")
os.environ.setdefault("LLMIND_ENABLE_WORKFLOW_HOOKS", "1")

@dataclass
class ValidationResult:
	is_valid: bool
	message: str


class ProgressOutput:
	"""Simple status output helper used by CLI flow."""

	@staticmethod
	def info(message: str) -> None:
		print(f"[INFO] {message}")

	@staticmethod
	def ok(message: str) -> None:
		print(f"[ OK ] {message}")

	@staticmethod
	def warn(message: str) -> None:
		print(f"[WARN] {message}")

	@staticmethod
	def error(message: str) -> None:
		print(f"[FAIL] {message}")

	@staticmethod
	def step(message: str, duration_seconds: float = 0.35) -> None:
		print(f"[....] {message}", end="", flush=True)
		time.sleep(duration_seconds)
		print(" done")


class DataWriter:
	"""AppData and JSON file creator/loader."""

	def __init__(self, app_name: str = APP_NAME) -> None:
		self.app_name = app_name
		self.app_data_dir = self._resolve_appdata_dir()
		self.progress = ProgressOutput()

	def _resolve_appdata_dir(self) -> Path:
		# Windows-first behavior; sensible fallback keeps this runnable on Linux/macOS.
		appdata = os.getenv("APPDATA")
		if appdata:
			return Path(appdata) / self.app_name
		return Path.home() / ".config" / self.app_name

	def ensure_appdata(self) -> Path:
		self.app_data_dir.mkdir(parents=True, exist_ok=True)
		return self.app_data_dir

	def write_json(self, filename: str, payload: dict) -> Path:
		self.ensure_appdata()
		target = self.app_data_dir / filename
		with target.open("w", encoding="utf-8") as handle:
			json.dump(payload, handle, indent=2)
		return target

	def read_json(self, filename: str) -> dict:
		target = self.app_data_dir / filename
		if not target.exists():
			return {}
		with target.open("r", encoding="utf-8") as handle:
			return json.load(handle)

	@staticmethod
	def sanitize_filename(filename: str, default: str = "artifact.bin") -> str:
		cleaned = Path(filename or default).name.strip()
		if not cleaned or cleaned in {".", ".."}:
			cleaned = default

		def replace_unsafe_chars(value: str) -> str:
			return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)

		sanitized = replace_unsafe_chars(cleaned)
		if sanitized == cleaned:
			return sanitized

		digest = sha256(cleaned.encode("utf-8")).hexdigest()[:8]
		original = Path(cleaned)
		stem = replace_unsafe_chars(original.stem) or "artifact"
		suffix = replace_unsafe_chars(original.suffix)
		return f"{stem}_{digest}{suffix}"

	def write_artifact(self, artifact_id: str, filename: str, data: bytes) -> Path:
		self.ensure_appdata()
		self.progress.info("Resolving appdata directory for artifact storage...")
		safe_id = self.sanitize_filename(artifact_id, default="artifact")
		safe_name = self.sanitize_filename(filename)
		artifact_dir = self.app_data_dir / ARTIFACTS_DIRNAME / safe_id
		self.progress.info(f"Creating artifact directory: {artifact_dir}")
		artifact_dir.mkdir(parents=True, exist_ok=True)
		target = artifact_dir / safe_name
		tmp = target.with_suffix(target.suffix + ".tmp")
		self.progress.info(f"Writing artifact file to temporary path: {tmp}")
		with tmp.open("wb") as handle:
			handle.write(data)
		try:
			os.replace(str(tmp), str(target))
		except OSError as exc:
			try:
				tmp.rename(target)
			except OSError as rename_exc:
				self.progress.error(
					f"Failed to move artifact to {target}. replace error: {exc}; rename error: {rename_exc}"
				)
				raise exc from rename_exc
		self.progress.ok(f"Artifact file moved to final destination: {target}")
		return target


class RuntimeExecutableResolver:
	"""Resolve and cache executable paths, then inject them into this process PATH."""

	def __init__(self, writer: DataWriter, progress: ProgressOutput) -> None:
		self.writer = writer
		self.progress = progress
		self.cache_filename = EXECUTABLE_CACHE_FILENAME
		self._resolved: Dict[str, str] = {}

	@staticmethod
	def _normalize_exe_name(executable: str) -> str:
		name = (executable or "").strip().lower()
		if not name:
			return ""
		if not name.endswith(".exe"):
			name += ".exe"
		return name

	def _load_cache(self) -> Dict[str, dict]:
		payload = self.writer.read_json(self.cache_filename)
		records = payload.get("executables", {})
		if not isinstance(records, dict):
			return {}
		cleaned: Dict[str, dict] = {}
		for name, meta in records.items():
			if not isinstance(name, str) or not isinstance(meta, dict):
				continue
			path = meta.get("path")
			if isinstance(path, str) and path.strip():
				cleaned[self._normalize_exe_name(name)] = {
					"path": path.strip(),
					"updated_at": int(meta.get("updated_at", 0) or 0),
				}
		return cleaned

	def _save_cache(self, records: Dict[str, dict]) -> None:
		self.writer.write_json(self.cache_filename, {"executables": records})

	def _record(self, normalized_name: str, resolved_path: str) -> str:
		records = self._load_cache()
		records[normalized_name] = {
			"path": resolved_path,
			"updated_at": int(time.time()),
		}
		self._save_cache(records)
		self._resolved[normalized_name] = resolved_path
		return resolved_path

	def _from_cache(self, normalized_name: str) -> Optional[str]:
		records = self._load_cache()
		meta = records.get(normalized_name)
		if not isinstance(meta, dict):
			return None
		path = meta.get("path")
		if not isinstance(path, str) or not path.strip():
			return None
		candidate = Path(path)
		if candidate.exists() and candidate.is_file():
			self._resolved[normalized_name] = str(candidate)
			return str(candidate)
		return None

	def _scan_known_locations(self, normalized_name: str) -> List[Path]:
		base_name = normalized_name.lower()
		known: Dict[str, List[Path]] = {
			"chrome.exe": [
				Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
				Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
				Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
			],
			"msedge.exe": [
				Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
				Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
			],
			"firefox.exe": [
				Path("C:/Program Files/Mozilla Firefox/firefox.exe"),
				Path("C:/Program Files (x86)/Mozilla Firefox/firefox.exe"),
			],
			"write.exe": [
				Path("C:/Program Files/Windows NT/Accessories/write.exe"),
			],
			"notepad.exe": [
				Path("C:/Windows/System32/notepad.exe"),
			],
		}
		return [p for p in known.get(base_name, []) if p.exists() and p.is_file()]

	@staticmethod
	def _scan_with_where(normalized_name: str) -> List[Path]:
		try:
			result = subprocess.run(
				["where", normalized_name],
				check=False,
				capture_output=True,
				text=True,
				timeout=3,
			)
		except Exception:
			return []
		if result.returncode != 0:
			return []
		matches: List[Path] = []
		for line in (result.stdout or "").splitlines():
			candidate = Path(line.strip())
			if candidate.exists() and candidate.is_file():
				matches.append(candidate)
		return matches

	@staticmethod
	def _bounded_scan_roots(normalized_name: str, max_depth: int = 4, max_hits: int = 5) -> List[Path]:
		roots = [
			Path(os.getenv("ProgramFiles", "")),
			Path(os.getenv("ProgramFiles(x86)", "")),
			Path(os.getenv("LOCALAPPDATA", "")),
		]
		valid_roots = [root for root in roots if str(root) and root.exists() and root.is_dir()]
		hits: List[Path] = []
		for root in valid_roots:
			root_depth = len(root.parts)
			for current, dirs, files in os.walk(root):
				current_path = Path(current)
				depth = len(current_path.parts) - root_depth
				if depth > max_depth:
					dirs[:] = []
					continue
				for filename in files:
					if filename.lower() == normalized_name:
						candidate = current_path / filename
						if candidate.exists() and candidate.is_file():
							hits.append(candidate)
							if len(hits) >= max_hits:
								return hits
		return hits

	def resolve_executable(self, executable: str, allow_scan: bool = True) -> Optional[str]:
		normalized = self._normalize_exe_name(executable)
		if not normalized:
			return None

		if normalized in self._resolved and Path(self._resolved[normalized]).exists():
			return self._resolved[normalized]

		which_match = shutil.which(normalized)
		if which_match:
			return self._record(normalized, str(Path(which_match)))

		cached = self._from_cache(normalized)
		if cached:
			return cached

		candidates = self._scan_known_locations(normalized)
		if not candidates:
			candidates = self._scan_with_where(normalized)
		if not candidates and allow_scan:
			candidates = self._bounded_scan_roots(normalized)

		if candidates:
			return self._record(normalized, str(candidates[0]))
		return None

	def _inject_runtime_path(self) -> None:
		if not self._resolved:
			return
		new_dirs: List[str] = []
		for resolved_path in self._resolved.values():
			parent = str(Path(resolved_path).parent)
			if parent and parent not in new_dirs:
				new_dirs.append(parent)

		existing = [part for part in os.getenv("PATH", "").split(os.pathsep) if part]
		ordered = new_dirs + [part for part in existing if part not in new_dirs]
		os.environ["PATH"] = os.pathsep.join(ordered)

	def warmup(self, executables: List[str]) -> Dict[str, str]:
		for executable in executables:
			resolved = self.resolve_executable(executable, allow_scan=True)
			if resolved:
				self.progress.info(f"Executable resolved: {executable} -> {resolved}")
		self._inject_runtime_path()
		return dict(self._resolved)

	def get_resolved_map(self) -> Dict[str, str]:
		records = self._load_cache()
		for name, meta in records.items():
			path = meta.get("path")
			if isinstance(path, str) and Path(path).exists():
				self._resolved[name] = path
		return dict(self._resolved)


class CacheManager:
	"""API key cache manager backed by appdata JSON."""

	def __init__(self, writer: DataWriter) -> None:
		self.writer = writer

	# ------------------------------------------------------------------
	# Multi-key store
	# ------------------------------------------------------------------

	def load_all_api_keys(self) -> list:
		"""Return all stored key records [{label, key, updated_at}].

		Automatically migrates the legacy single-key format on first call.
		"""
		payload = self.writer.read_json(CACHE_FILENAME)
		# Migrate legacy single-key format
		if "api_key" in payload and isinstance(payload.get("api_key"), str):
			raw = payload["api_key"].strip()
			if raw:
				return [{"label": "Default", "key": raw, "updated_at": payload.get("updated_at", 0)}]
		keys = payload.get("api_keys", [])
		if not isinstance(keys, list):
			return []
		return [
			k for k in keys
			if isinstance(k, dict) and isinstance(k.get("key"), str) and k["key"].strip()
		]

	def save_api_key(self, api_key: str, label: Optional[str] = None) -> Path:
		"""Add or update an API key. Deduplicates by key value."""
		keys = self.load_all_api_keys()
		for record in keys:
			if record.get("key") == api_key:
				if label:
					record["label"] = label
				record["updated_at"] = int(time.time())
				return self.writer.write_json(CACHE_FILENAME, {"api_keys": keys})
		effective_label = label or f"Key {len(keys) + 1}"
		keys.append({"label": effective_label, "key": api_key, "updated_at": int(time.time())})
		return self.writer.write_json(CACHE_FILENAME, {"api_keys": keys})

	def load_api_key(self) -> Optional[str]:
		"""Return the first stored API key (backward compatibility)."""
		keys = self.load_all_api_keys()
		return keys[0]["key"] if keys else None

	def remove_api_key(self, label: str) -> bool:
		"""Remove key by label. Returns True when a record was removed."""
		keys = self.load_all_api_keys()
		filtered = [k for k in keys if k.get("label") != label]
		if len(filtered) == len(keys):
			return False
		self.writer.write_json(CACHE_FILENAME, {"api_keys": filtered})
		return True

	def remove_all_api_keys(self) -> None:
		"""Delete the entire API key cache file."""
		target = self.writer.app_data_dir / CACHE_FILENAME
		if target.exists():
			target.unlink()


class APIKeyValidator:
	"""Local API key validation template.

	Replace or extend this with provider-specific server validation when needed.
	"""

	MIN_LENGTH = 20
	SIMPLE_PATTERN = re.compile(r"^[A-Za-z0-9_\-\.]+$")

	@classmethod
	def validate_format(cls, key: str) -> ValidationResult:
		cleaned = key.strip()
		if not cleaned:
			return ValidationResult(False, "API key is empty.")
		if len(cleaned) < cls.MIN_LENGTH:
			return ValidationResult(False, f"API key is too short (min {cls.MIN_LENGTH}).")
		if not cls.SIMPLE_PATTERN.match(cleaned):
			return ValidationResult(False, "API key contains unsupported characters.")
		return ValidationResult(True, "API key format looks valid.")

	@staticmethod
	def validate_remote_stub(_key: str) -> ValidationResult:
		# Template hook for real network validation.
		return ValidationResult(True, "Remote validation hook passed (stub).")


class LLMindCLI:
	def __init__(self) -> None:
		self.progress = ProgressOutput()
		self.writer = DataWriter()
		self.cache = CacheManager(self.writer)
		self.exec_resolver = RuntimeExecutableResolver(self.writer, self.progress)
		self.exec_resolver.warmup(self._default_executables())
		self.hook_registry = HookRegistry(app_name=APP_NAME)
		self.hook_registry.register_builtin_hooks()

	@staticmethod
	def _default_executables() -> List[str]:
		return ["notepad.exe", "write.exe", "msedge.exe", "chrome.exe", "firefox.exe", "powershell.exe"]

	def show_banner(self) -> None:
		print("### HELLO, WELCOME TO LLMind CLI ###\n")
		keys = self.cache.load_all_api_keys()
		if keys:
			print(f"TEXT:\n{len(keys)} API key(s) loaded:")
			for record in keys:
				masked = self._mask_key(record["key"])
				print(f"  - {record['label']}: {masked}")
			print()
		else:
			print("TEXT:\nNo Models Active! Press b to manage APIs!\n")

	@staticmethod
	def _mask_key(key: str) -> str:
		if len(key) <= 8:
			return "*" * len(key)
		return f"{key[:4]}...{key[-4:]}"

	@staticmethod
	def _is_llm_provider_url(url: str) -> bool:
		"""Return True for any recognised LLM provider URL."""
		return detect_provider(url) != "generic"

	def _select_api_key_for_provider(self, provider: str) -> Optional[str]:
		"""Prompt the user to pick an API key when multiple are stored.

		Returns the selected key string, or ``None`` when no keys are stored.
		"""
		all_keys = self.cache.load_all_api_keys()
		if not all_keys:
			return None
		if len(all_keys) == 1:
			return all_keys[0]["key"]
		print(f"\nMultiple API keys stored. Select one to use for {provider.upper()}:")
		for idx, record in enumerate(all_keys, 1):
			masked = self._mask_key(record["key"])
			print(f"  {idx} - {record['label']} ({masked})")
		while True:
			raw = input(f"Select key [1-{len(all_keys)}]: ").strip()
			if raw.isdigit() and 1 <= int(raw) <= len(all_keys):
				return all_keys[int(raw) - 1]["key"]
			self.progress.warn("Invalid selection. Try again.")

	def _build_provider_payload_prompt(self, url: str) -> tuple:
		"""Interactively build a provider-appropriate JSON request payload.

		Returns ``(payload_dict, selected_api_key_or_None)``.
		For OpenAI endpoints, fetches the available model list from the API
		so the user can choose from real model IDs instead of typing one.
		"""
		provider = detect_provider(url)
		template_name = get_response_template_name(provider, url)
		# For OpenAI /v1/chat/completions, use the chat completions payload format.
		if template_name == "openai_chat":
			payload_provider = "openai_chat"
		elif template_name == "openai_images":
			payload_provider = "openai_images"
		else:
			payload_provider = provider

		selected_key: Optional[str] = None
		default_model = DEFAULT_MODELS.get(provider, "gpt-4.1-mini")
		if payload_provider == "openai_images":
			default_model = "gpt-image-1"

		print(f"\n{provider.upper()} API payload builder")

		# --- OpenAI: key selection + live model list ---
		if provider == "openai":
			selected_key = self._select_api_key_for_provider(provider)
			if selected_key:
				self.progress.step("Fetching available models from OpenAI", duration_seconds=0)
				available_models = fetch_openai_models(selected_key)
				if available_models:
					print(f"\nAvailable models ({len(available_models)} found):")
					for idx, mid in enumerate(available_models, 1):
						print(f"  {idx:3}. {mid}")
					while True:
						raw = input(
							f"Select model by number, or type a model name (default {default_model}): "
						).strip()
						if not raw:
							model = default_model
							break
						if raw.isdigit() and 1 <= int(raw) <= len(available_models):
							model = available_models[int(raw) - 1]
							break
						# Allow freeform model name entry too
						model = raw
						break
				else:
					self.progress.warn("Could not retrieve model list from OpenAI. Falling back to manual entry.")
					model = input(f"Model (default {default_model}): ").strip() or default_model
			else:
				self.progress.warn("No API key configured. Enter model manually.")
				model = input(f"Model (default {default_model}): ").strip() or default_model
		elif provider == "anthropic":
			selected_key = self._select_api_key_for_provider(provider)
			if selected_key:
				self.progress.step("Fetching available models from Anthropic", duration_seconds=0)
				available_models = fetch_anthropic_models(selected_key)
				if available_models:
					print(f"\nAvailable models ({len(available_models)} found):")
					for idx, mid in enumerate(available_models, 1):
						print(f"  {idx:3}. {mid}")
					while True:
						raw = input(
							f"Select model by number, or type a model name (default {default_model}): "
						).strip()
						if not raw:
							model = default_model
							break
						if raw.isdigit() and 1 <= int(raw) <= len(available_models):
							model = available_models[int(raw) - 1]
							break
						model = raw
						break
				else:
					self.progress.warn("Could not retrieve model list from Anthropic. Falling back to manual entry.")
					model = input(f"Model (default {default_model}): ").strip() or default_model
			else:
				self.progress.warn("No API key configured. Enter model manually.")
				model = input(f"Model (default {default_model}): ").strip() or default_model
		elif provider == "xai":
			selected_key = self._select_api_key_for_provider(provider)
			if selected_key:
				self.progress.step("Fetching available models from xAI", duration_seconds=0)
				available_models = fetch_xai_models(selected_key)
				if available_models:
					print(f"\nAvailable models ({len(available_models)} found):")
					for idx, mid in enumerate(available_models, 1):
						print(f"  {idx:3}. {mid}")
					while True:
						raw = input(
							f"Select model by number, or type a model name (default {default_model}): "
						).strip()
						if not raw:
							model = default_model
							break
						if raw.isdigit() and 1 <= int(raw) <= len(available_models):
							model = available_models[int(raw) - 1]
							break
						model = raw
						break
				else:
					self.progress.warn("Could not retrieve model list from xAI. Falling back to manual entry.")
					model = input(f"Model (default {default_model}): ").strip() or default_model
			else:
				self.progress.warn("No API key configured. Enter model manually.")
				model = input(f"Model (default {default_model}): ").strip() or default_model
		elif provider == "gemini":
			selected_key = self._select_api_key_for_provider(provider)
			if selected_key:
				self.progress.step("Fetching available models from Gemini", duration_seconds=0)
				available_models = fetch_gemini_models(selected_key)
				if available_models:
					print(f"\nAvailable models ({len(available_models)} found):")
					for idx, mid in enumerate(available_models, 1):
						print(f"  {idx:3}. {mid}")
					while True:
						raw = input(
							f"Select model by number, or type a model name (default {default_model}): "
						).strip()
						if not raw:
							model = default_model
							break
						if raw.isdigit() and 1 <= int(raw) <= len(available_models):
							model = available_models[int(raw) - 1]
							break
						model = raw
						break
				else:
					self.progress.warn("Could not retrieve model list from Gemini. Falling back to manual entry.")
					model = input(f"Model (default {default_model}): ").strip() or default_model
			else:
				self.progress.warn("No API key configured. Enter model manually.")
				model = input(f"Model (default {default_model}): ").strip() or default_model
		else:
			model = input(f"Model (default {default_model}): ").strip() or default_model

		prompt_text = input("Prompt/Input text: ").strip() or "Hello from LLMind"
		instructions = input("System instructions (optional): ").strip()
		temperature_raw = input("Temperature (optional, e.g. 0.7): ").strip()
		max_tokens_raw = input("Max tokens (optional): ").strip()

		temperature = None
		if temperature_raw:
			try:
				temperature = float(temperature_raw)
			except ValueError:
				self.progress.warn("Invalid temperature value; skipping.")

		max_tokens = None
		if max_tokens_raw:
			try:
				max_tokens = int(max_tokens_raw)
			except ValueError:
				self.progress.warn("Invalid max_tokens value; skipping.")

		return build_payload_from_user_input(
			provider=payload_provider,
			model=model,
			prompt_text=prompt_text,
			system_instructions=instructions or None,
			temperature=temperature,
			max_tokens=max_tokens,
		), selected_key

	def run(self) -> int:
		self.progress.info("Initializing appdata...")
		self.writer.ensure_appdata()
		if not self.writer.app_data_dir.exists():
			self.progress.error("Appdata directory does not exist!")
			return 1
		self.progress.info(f"AppData directory resolved to: {self.writer.app_data_dir}")
		self._ensure_default_settings()
		self.show_banner()

		while True:
			print("Main Menu")
			print("  b - API key manager")
			print("  1 - Show status")
			print("  2 - Test API request")
			print("  3 - Run Windows OS hook self-test")
			print("  4 - Generate persistent hook code")
			print("  5 - Refresh executable scan/PATH cache")
			print("  q - Quit")
			choice = input("Select option: ").strip().lower()

			if choice == "b":
				self.api_manager_menu()
			elif choice == "1":
				self.show_status()
			elif choice == "2":
				# Simple example hostname to test against. The user can edit this.
				url = input("Enter URL to request (default https://httpbin.org/get): ").strip() or "https://httpbin.org/get"
				method = input("HTTP method (default GET): ").strip().upper() or "GET"
				payload = None
				selected_key: Optional[str] = None
				if self._is_llm_provider_url(url):
					payload, selected_key = self._build_provider_payload_prompt(url)
				status, body = perform_api_request(
					url,
					method=method,
					json_payload=payload,
					api_key=selected_key,
					execute_hook_calls=True,
					resolved_executables=self.exec_resolver.get_resolved_map(),
				)
				if status == 0 or status >= 400:
					self.progress.error(f"Request failed: {body}")
				else:
					self.progress.ok(f"Request returned {status}")
					print(body)
					downloaded_artifacts = self._store_response_artifacts(body)
					downloaded_artifacts_dir = self.writer.app_data_dir / ARTIFACTS_DIRNAME
					if downloaded_artifacts:
						self.progress.ok(
							f"Downloaded {len(downloaded_artifacts)} artifact(s) to {downloaded_artifacts_dir}"
						)
			elif choice == "3":
				self.run_windows_hook_self_test()
			elif choice == "4":
				self.generate_persistent_hook_code()
			elif choice == "5":
				resolved = self.exec_resolver.warmup(self._default_executables())
				if resolved:
					self.progress.ok(f"Executable cache refreshed ({len(resolved)} entries).")
				else:
					self.progress.warn("No executable paths were resolved during refresh.")
			elif choice == "q":
				self.progress.ok("Goodbye.")
				return 0
			else:
				self.progress.warn("Unknown option. Try again.")

	def run_windows_hook_self_test(self) -> None:
		"""Run Windows 10/11 hook checks for filesystem, registry, and UI hooks."""
		print("\nWindows Hook Self-Test")
		print("  - File system access: validating write/read/delete in AppData")
		print("  - Registry access: validating HKCU settings read/write")
		print("  - UI hooks: validating guardrails and non-destructive window discovery")
		context = self.hook_registry.build_context(self.writer.app_data_dir)
		results = self.hook_registry.execute_many(["filesystem_access", "registry_settings"], context)
		for result in results:
			pretty = "Filesystem" if result.hook_name == "filesystem_access" else "Registry"
			if result.success:
				self.progress.ok(f"{pretty} Hook Success: {result.message}")
			else:
				self.progress.error(f"{pretty} Hook Failure: {result.message}")

		self._run_ui_hook_self_test()

	def _run_ui_hook_self_test(self) -> None:
		"""Run guardrail-focused, non-destructive checks for UI hook behavior."""
		print("\nUI Hook Self-Test")

		# Test 1: verify the gate blocks execution when allow_ui_actions is explicitly False.
		blocked_context = self.hook_registry.build_context(
			self.writer.app_data_dir,
			extras={
				"hook_args": {"action": "find_window", "title_contains": ""},
				"allow_ui_actions": False,
			},
		)
		blocked_result = self.hook_registry.execute("windows_ui_action", blocked_context)
		if (not blocked_result.success) and ("disabled" in blocked_result.message.lower()):
			self.progress.ok("UI Gate Check: Execution gate correctly blocks when disabled")
		else:
			self.progress.error(f"UI Gate Check Failed: {blocked_result.message}")

		# Test 2: invalid action should be rejected even when UI actions are enabled.
		invalid_action_context = self.hook_registry.build_context(
			self.writer.app_data_dir,
			extras={
				"hook_args": {"action": "invalid_action"},
				"allow_ui_actions": True,
			},
		)
		invalid_action_result = self.hook_registry.execute("windows_ui_action", invalid_action_context)
		if (not invalid_action_result.success) and ("unsupported action" in invalid_action_result.message.lower()):
			self.progress.ok("UI Validation Check: Invalid action correctly rejected by allowlist")
		else:
			self.progress.error(f"UI Validation Check Failed: {invalid_action_result.message}")

		# Test 3: broad visible-window enumeration — no title filter, always succeeds on a live desktop.
		probe_context = self.hook_registry.build_context(
			self.writer.app_data_dir,
			extras={
				"hook_args": {"action": "find_window", "title_contains": ""},
				"allow_ui_actions": True,
			},
		)
		probe_result = self.hook_registry.execute("windows_ui_action", probe_context)
		if probe_result.success:
			matches = probe_result.details.get("matches", "?")
			title = probe_result.details.get("title", "")
			self.progress.ok(
				f"UI Discovery Probe: {matches} visible window(s) found"
				+ (f" — first: [{title}]" if title else "")
			)
		else:
			self.progress.error(f"UI Discovery Probe Failed: {probe_result.message}")

	def _validate_filesystem_hook(self) -> tuple:
		"""Return (success, message) for filesystem hook validation."""
		context = self.hook_registry.build_context(self.writer.app_data_dir)
		result = self.hook_registry.execute("filesystem_access", context)
		return result.success, result.message

	def _validate_registry_hook(self) -> tuple:
		"""Return (success, message) for registry hook validation in HKCU."""
		context = self.hook_registry.build_context(self.writer.app_data_dir)
		result = self.hook_registry.execute("registry_settings", context)
		return result.success, result.message

	def generate_persistent_hook_code(self) -> None:
		"""Generate persistent Python code for validated hooks."""
		available = self.hook_registry.list_hook_names()
		ui_hooks = [name for name in ["windows_ui_action", "launch_process"] if name in available]
		print("\nPersistent Hook Code Generation")
		print(f"Available hooks: {', '.join(available)}")
		if ui_hooks:
			print(f"UI hooks available: {', '.join(ui_hooks)}")
		raw = input("Enter hook names (comma-separated, blank for all): ").strip()
		if raw:
			selected = [part.strip() for part in raw.split(",") if part.strip()]
		else:
			selected = available

		if ui_hooks:
			include_ui_raw = input("Include UI hooks in generated module? (Y/n): ").strip().lower()
			if include_ui_raw in {"", "y", "yes"}:
				selected = list(dict.fromkeys(selected + ui_hooks))

		default_output = Path(__file__).resolve().parent.parent / "hooks" / "generated" / "persistent_hooks.py"
		output_raw = input(f"Output file (default {default_output}): ").strip()
		output_file = Path(output_raw) if output_raw else default_output

		try:
			generated = self.hook_registry.generate_persistent_hook_module(selected, output_file)
			self.progress.ok(f"Persistent hook code generated: {generated}")
		except ValueError as exc:
			self.progress.error(f"Hook validation failed: {exc}")
		except Exception as exc:
			self.progress.error(f"Code generation failed: {exc}")

	def _ensure_default_settings(self) -> None:
		existing = self.writer.read_json(SETTINGS_FILENAME)
		if existing:
			return
		self.writer.write_json(
			SETTINGS_FILENAME,
			{
				"version": 1,
				"platform_hint": "windows-cmd",
			},
		)

	def show_status(self) -> None:
		keys = self.cache.load_all_api_keys()
		if keys:
			self.progress.ok(f"{len(keys)} API key(s) configured:")
			for record in keys:
				self.progress.info(f"  [{record['label']}] {self._mask_key(record['key'])}")
		else:
			self.progress.warn("No API key configured.")
		self.progress.info(f"AppData path: {self.writer.app_data_dir}")

	def api_manager_menu(self) -> None:
		while True:
			keys = self.cache.load_all_api_keys()
			print("\nAPI Manager")
			if keys:
				print("  Stored keys:")
				for record in keys:
					print(f"    [{record['label']}] {self._mask_key(record['key'])}")
			else:
				print("  No keys stored.")
			print("  1 - Import key from file (picker/manual path)")
			print("  2 - Enter key manually")
			print("  3 - Remove a key by label")
			print("  4 - Clear all keys")
			print("  x - Back")
			choice = input("Select option: ").strip().lower()

			if choice == "1":
				self._handle_file_key_import()
			elif choice == "2":
				self._handle_manual_key_input()
			elif choice == "3":
				self._remove_key_by_label()
			elif choice == "4":
				self._clear_all_keys()
			elif choice == "x":
				return
			else:
				self.progress.warn("Unknown option.")

	def _handle_file_key_import(self) -> None:
		path = self._pick_file_path() or input("Enter path to key file: ").strip()
		if not path:
			self.progress.warn("No file selected.")
			return

		try:
			with open(path, "r", encoding="utf-8") as handle:
				for line in handle:
					candidate = line.strip()
					if candidate:
						self._validate_and_store_key(candidate)
						return
		except OSError as exc:
			self.progress.error(f"Could not read file: {exc}")
			return

		self.progress.warn("No non-empty key line found in file.")

	def _handle_manual_key_input(self) -> None:
		key = input("Paste API key: ").strip()
		label = input("Label for this key (optional, e.g. 'Work', 'Personal'): ").strip() or None
		self._validate_and_store_key(key, label=label)

	def _validate_and_store_key(self, key: str, label: Optional[str] = None) -> None:
		self.progress.step("Validating key format")
		local = APIKeyValidator.validate_format(key)
		if not local.is_valid:
			self.progress.error(local.message)
			return

		self.progress.step("Running remote validation hook")
		remote = APIKeyValidator.validate_remote_stub(key)
		if not remote.is_valid:
			self.progress.error(remote.message)
			return

		saved_to = self.cache.save_api_key(key, label=label)
		self.progress.ok(f"API key saved to {saved_to}")

	def _remove_key_by_label(self) -> None:
		keys = self.cache.load_all_api_keys()
		if not keys:
			self.progress.warn("No keys stored.")
			return
		print("Select key to remove:")
		for idx, record in enumerate(keys, 1):
			print(f"  {idx} - {record['label']} ({self._mask_key(record['key'])})")
		raw = input(f"Select [1-{len(keys)}] or press Enter to cancel: ").strip()
		if not raw:
			return
		if raw.isdigit() and 1 <= int(raw) <= len(keys):
			label = keys[int(raw) - 1]["label"]
			if self.cache.remove_api_key(label):
				self.progress.ok(f"Removed key: {label}")
			else:
				self.progress.warn(f"Could not remove key: {label}")
		else:
			self.progress.warn("Invalid selection.")

	def _clear_all_keys(self) -> None:
		confirm = input("Remove ALL stored API keys? (yes/no): ").strip().lower()
		if confirm == "yes":
			self.cache.remove_all_api_keys()
			self.progress.ok("All API keys removed.")
		else:
			self.progress.info("Cancelled.")

	def _store_response_artifacts(self, body: str) -> List[Path]:
		self.progress.info("Searching for artifacts in the response...")
		try:
			payload = json.loads(body)
		except (TypeError, ValueError, json.JSONDecodeError):
			payload = body

		saved_paths: List[Path] = []
		for candidate in extract_file_artifact_candidates(payload):
			self.progress.info("Found artifact candidate.")
			content = candidate.get("content")
			if content is None or not isinstance(content, bytes) or not content:
				candidate_filename = candidate.get("filename", "artifact.bin")
				self.progress.warn(
					f"Artifact content is empty or invalid. Skipping artifact: {candidate_filename}"
				)
				continue
			filename = candidate.get("filename", "artifact.bin")
			artifact_id = uuid4().hex
			self.progress.info(
				f"Downloading and caching artifact with ID: {artifact_id}, Filename: {filename}"
			)
			try:
				saved_path = self.writer.write_artifact(artifact_id, filename, content)
				saved_paths.append(saved_path)
				self.progress.ok(f"Artifact saved to: {saved_path}")
			except Exception as exc:
				self.progress.error(f"Error saving artifact {filename}. Exception: {exc}")
				continue
		if not saved_paths:
			self.progress.warn("No artifacts were downloaded or saved.")
		else:
			self.progress.ok(f"Total artifacts downloaded and saved: {len(saved_paths)}")
		return saved_paths

	@staticmethod
	def _pick_file_path() -> Optional[str]:
		# Optional GUI file picker requested in requirements; safe fallback in CMD.
		try:
			from tkinter import Tk, filedialog
		except Exception:
			return None

		root = Tk()
		root.withdraw()
		root.update()
		selected = filedialog.askopenfilename(title="Select API key file")
		root.destroy()
		return selected or None


def main() -> int:
	cli = LLMindCLI()
	return cli.run()


if __name__ == "__main__":
	sys.exit(main())
