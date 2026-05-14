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
from pathlib import Path
from typing import Optional

# Ensure repository root is on sys.path so local sibling packages import correctly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from network.requests import perform_api_request


APP_NAME = "LLMind"
CACHE_FILENAME = "api_cache.json"
SETTINGS_FILENAME = "settings.json"


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

	def run(self) -> int:
		self.progress.step("Initializing appdata")
		self.writer.ensure_appdata()
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
				status, body = perform_api_request(url)
				if status == 0:
					self.progress.error(f"Request failed: {body}")
				else:
					self.progress.ok(f"Request returned {status}")
					print(body)
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
