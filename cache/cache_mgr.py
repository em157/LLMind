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
    - save/load one or more labelled API keys
    - optionally store a copied key file under appdata
    """

    def __init__(self, writer: DataWriter) -> None:
        self.writer = writer

    # ------------------------------------------------------------------
    # Multi-key helpers
    # ------------------------------------------------------------------

    def load_all_api_keys(self) -> List[Dict[str, Any]]:
        """Return all stored API key records as a list of dicts.

        Automatically migrates the legacy single-key format
        ``{"api_key": "..."}`` to the new multi-key list on first access.
        Each record has the shape ``{"label": str, "key": str, "updated_at": int}``.
        """
        payload = self.writer.read_json(CACHE_FILENAME)
        # Legacy single-key migration
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
        """Add or update an API key in the multi-key store.

        If the exact key value already exists the record is updated in-place;
        otherwise a new record is appended. *label* defaults to ``"Key N"``
        where N is the new list length.
        """
        keys = self.load_all_api_keys()
        # Migrate: wipe legacy single-key field by reconstructing under new schema
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
        """Return the first stored API key for backward compatibility."""
        keys = self.load_all_api_keys()
        return keys[0]["key"] if keys else None

    def remove_api_key(self, label: str) -> bool:
        """Remove the key with the given label. Returns True when removed."""
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
        """Save artifact metadata with a timestamp and cap retained records.

        Args:
            artifact: Artifact metadata returned to the caller.

        Returns:
            Path to the artifact cache JSON file.
        """
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
        """Load artifact metadata records and ignore malformed cache entries."""
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
