from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from appdata.data_writer import DataWriter


CACHE_FILENAME = "api_cache.json"
ARTIFACT_CACHE_FILENAME = "artifact_cache.json"
MAX_ARTIFACT_RECORDS = 100


class CacheManager:
    """Simple cache manager for API keys backed by DataWriter.

    Responsibilities:
    - save/load a single API key
    - optionally store a copied key file under appdata
    """

    def __init__(self, writer: DataWriter) -> None:
        self.writer = writer

    def save_api_key(self, api_key: str) -> Path:
        payload = {"api_key": api_key, "updated_at": int(time.time())}
        return self.writer.write_json(CACHE_FILENAME, payload)

    def load_api_key(self) -> Optional[str]:
        payload = self.writer.read_json(CACHE_FILENAME)
        value = payload.get("api_key")
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    def save_key_file(self, source_path: str, dest_name: Optional[str] = None) -> Optional[Path]:
        try:
            p = Path(source_path)
            if not p.exists() or not p.is_file():
                return None
            data = p.read_bytes()
            dest = dest_name or p.name
            return self.writer.write_file(dest, data)
        except Exception:
            return None

    def save_artifact_record(self, artifact: Dict[str, Any]) -> Path:
        lock_path = self._acquire_artifact_cache_lock()
        try:
            payload = self.writer.read_json(ARTIFACT_CACHE_FILENAME)
            records = payload.get("artifacts", [])
            if not isinstance(records, list):
                records = []
            record = dict(artifact)
            record["created_at"] = int(time.time())
            records.append(record)
            return self.writer.write_json(ARTIFACT_CACHE_FILENAME, {"artifacts": records[-MAX_ARTIFACT_RECORDS:]})
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass

    def load_artifact_records(self) -> List[Dict[str, Any]]:
        payload = self.writer.read_json(ARTIFACT_CACHE_FILENAME)
        records = payload.get("artifacts", [])
        if not isinstance(records, list):
            return []
        return [record for record in records if isinstance(record, dict)]

    def _acquire_artifact_cache_lock(self) -> Path:
        self.writer.ensure_appdata()
        lock_path = self.writer.app_data_dir / f"{ARTIFACT_CACHE_FILENAME}.lock"
        deadline = time.time() + 5
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return lock_path
            except FileExistsError:
                if time.time() >= deadline:
                    raise TimeoutError("timed out waiting for artifact cache lock")
                time.sleep(0.05)
