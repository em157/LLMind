from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict


class DataWriter:
    """Small helper to read/write JSON and raw files into an app-specific
    appdata directory. Designed for simple CLI tools and tests.

    - creates the appdata directory if missing
    - writes files with restricted permissions when possible
    - provides sane fallbacks on non-POSIX systems
    """

    def __init__(self, app_name: str = "LLMind") -> None:
        self.app_name = app_name
        self.app_data_dir = self._resolve_appdata_dir()

    def _resolve_appdata_dir(self) -> Path:
        # Follow environment conventions: APPDATA on Windows, otherwise ~/.config
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / self.app_name
        return Path.home() / ".config" / self.app_name

    def ensure_appdata(self) -> Path:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        return self.app_data_dir

    def write_json(self, filename: str, payload: Dict) -> Path:
        """Write a JSON file under appdata. Attempts to set file mode to 600 on POSIX.

        Returns the Path written to.
        """
        self.ensure_appdata()
        target = self.app_data_dir / filename
        # Write to a temp file then move for safer writes
        tmp = target.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        try:
            os.replace(str(tmp), str(target))
        except Exception:
            # best-effort fallback
            tmp.rename(target)

        # Restrict permissions on POSIX systems
        try:
            if os.name == "posix":
                target.chmod(0o600)
        except Exception:
            pass
        return target

    def read_json(self, filename: str) -> Dict:
        target = self.app_data_dir / filename
        if not target.exists():
            return {}
        try:
            with target.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def write_file(self, filename: str, data: bytes) -> Path:
        """Write raw bytes to a file under appdata with restricted permissions.

        Useful for storing a key file copy or similar.
        """
        self.ensure_appdata()
        target = self.app_data_dir / filename
        tmp = target.with_suffix(".tmp")
        with tmp.open("wb") as fh:
            fh.write(data)
        try:
            os.replace(str(tmp), str(target))
        except Exception:
            tmp.rename(target)
        try:
            if os.name == "posix":
                target.chmod(0o600)
        except Exception:
            pass
        return target

    @staticmethod
    def sanitize_filename(filename: str, default: str = "artifact.bin") -> str:
        cleaned = Path(filename or default).name.strip()
        if not cleaned or cleaned in {".", ".."}:
            cleaned = default
        return "".join(char if char.isalnum() or char in "._-" else "_" for char in cleaned)

    def write_artifact(self, artifact_id: str, filename: str, data: bytes) -> Path:
        """Write generated/downloaded response bytes under appdata artifacts."""
        self.ensure_appdata()
        safe_id = self.sanitize_filename(artifact_id, default="artifact")
        safe_name = self.sanitize_filename(filename)
        artifact_dir = self.app_data_dir / "artifacts" / safe_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        target = artifact_dir / safe_name
        tmp = target.with_suffix(target.suffix + ".tmp")
        with tmp.open("wb") as fh:
            fh.write(data)
        try:
            os.replace(str(tmp), str(target))
        except Exception:
            tmp.rename(target)
        try:
            if os.name == "posix":
                target.chmod(0o600)
        except Exception:
            pass
        return target

    @staticmethod
    def file_url(path: Path) -> str:
        return path.resolve().as_uri()
