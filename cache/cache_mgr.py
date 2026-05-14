from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from appdata.data_writer import DataWriter


CACHE_FILENAME = "api_cache.json"


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
