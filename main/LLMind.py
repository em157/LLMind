"""LLMind CLI template for Windows 10/11 CMD.

This file intentionally contains the full template codebase requested in test.py,
including cache management, appdata writer behavior, progress output hooks, and
API key onboarding flow.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse
from uuid import uuid4

# When running the script from the `main/` directory, sibling packages (network, cache, appdata)
# aren't on sys.path by default. Add the repository root to sys.path so imports like
# `from network.requests import ...` work in both development and packaged runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from network.requests import perform_api_request
from scripts.script_mgr import get_response_param_template
from response.response_handler import extract_file_artifact_candidates


APP_NAME = "LLMind"
CACHE_FILENAME = "api_cache.json"
SETTINGS_FILENAME = "settings.json"
ARTIFACTS_DIRNAME = "artifacts"


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
			self.progress.ok(f"Artifact file moved to final destination: {target}")
		except OSError as exc:
			try:
				tmp.rename(target)
				self.progress.ok(f"Artifact file moved to final destination: {target}")
			except OSError:
				self.progress.error(f"Failed to move artifact to {target}. Error: {exc}")
				raise exc
		return target


class CacheManager:
	"""API key cache manager backed by appdata JSON."""

	def __init__(self, writer: DataWriter) -> None:
		self.writer = writer

	def save_api_key(self, api_key: str) -> Path:
		payload = {
			"api_key": api_key,
			"updated_at": int(time.time()),
		}
		return self.writer.write_json(CACHE_FILENAME, payload)

	def load_api_key(self) -> Optional[str]:
		payload = self.writer.read_json(CACHE_FILENAME)
		value = payload.get("api_key")
		if not isinstance(value, str) or not value.strip():
			return None
		return value.strip()


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

	def show_banner(self) -> None:
		print("### HELLO, WELCOME TO LLMind CLI ###\n")
		existing = self.cache.load_api_key()
		if existing:
			masked = self._mask_key(existing)
			print(f"TEXT:\nModel API key loaded: {masked}\n")
		else:
			print("TEXT:\nNo Models Active! Press b to manage APIs!\n")

	@staticmethod
	def _mask_key(key: str) -> str:
		if len(key) <= 8:
			return "*" * len(key)
		return f"{key[:4]}...{key[-4:]}"

	@staticmethod
	def _is_openai_responses_url(url: str) -> bool:
		parsed = urlparse(url)
		return parsed.netloc.lower() == "api.openai.com" and parsed.path.rstrip("/") == "/v1/responses"

	def _build_responses_payload_prompt(self) -> dict:
		print("\nOpenAI /v1/responses payload")
		model = input("Model (default gpt-4.1-mini): ").strip() or "gpt-4.1-mini"
		prompt_text = input("Prompt/Input text: ").strip() or "Hello from LLMind"
		instructions = input("System instructions (optional): ").strip()
		temperature_raw = input("Temperature (optional, e.g. 0.7): ").strip()
		max_output_tokens_raw = input("Max output tokens (optional): ").strip()

		payload = {
			"model": model,
			"input": prompt_text,
		}
		if instructions:
			payload["instructions"] = instructions
		if temperature_raw:
			try:
				payload["temperature"] = float(temperature_raw)
			except ValueError:
				self.progress.warn("Invalid temperature value; skipping.")
		if max_output_tokens_raw:
			try:
				payload["max_output_tokens"] = int(max_output_tokens_raw)
			except ValueError:
				self.progress.warn("Invalid max_output_tokens value; skipping.")
		return payload

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
				response_params = None
				if self._is_openai_responses_url(url):
					payload = self._build_responses_payload_prompt()
					response_params = get_response_param_template()
				status, body = perform_api_request(
					url,
					method=method,
					json_payload=payload,
					response_params=response_params,
				)
				if status == 0:
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
			elif choice == "q":
				self.progress.ok("Goodbye.")
				return 0
			else:
				self.progress.warn("Unknown option. Try again.")

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
		key = self.cache.load_api_key()
		if key:
			self.progress.ok(f"API key present: {self._mask_key(key)}")
		else:
			self.progress.warn("No API key configured.")

		self.progress.info(f"AppData path: {self.writer.app_data_dir}")

	def api_manager_menu(self) -> None:
		print("\nAPI Manager")
		print("  1 - Import key from file (picker/manual path)")
		print("  2 - Enter key manually")
		print("  3 - Clear key")
		print("  x - Back")
		choice = input("Select option: ").strip().lower()

		if choice == "1":
			self._handle_file_key_import()
		elif choice == "2":
			self._handle_manual_key_input()
		elif choice == "3":
			self._clear_key()
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
		self._validate_and_store_key(key)

	def _validate_and_store_key(self, key: str) -> None:
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

		saved_to = self.cache.save_api_key(key)
		self.progress.ok(f"API key saved to {saved_to}")

	def _clear_key(self) -> None:
		target = self.writer.app_data_dir / CACHE_FILENAME
		if target.exists():
			target.unlink()
			self.progress.ok("API key cache removed.")
			return
		self.progress.warn("No API cache file to clear.")

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
				self.progress.warn(f"Artifact content is empty or invalid. Skipping artifact: {candidate}")
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
