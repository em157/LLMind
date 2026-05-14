from __future__ import annotations

import json
from typing import Optional, Tuple

try:
    import requests as _requests
except Exception:
    _requests = None

from appdata.progress_output import ProgressOutput
from cache.cache_mgr import CacheManager
from appdata.data_writer import DataWriter


def perform_api_request(url: str, method: str = "GET", json_payload: Optional[dict] = None) -> Tuple[int, str]:
    """Perform a simple API request using stored API key.

    Returns (status_code, response_text). If no requests library is available,
    falls back to urllib. Uses CacheManager to load the stored key and passes
    it in an Authorization header as 'Bearer <key>'.
    """
    progress = ProgressOutput()
    writer = DataWriter()
    cache = CacheManager(writer)

    key = cache.load_api_key()
    if not key:
        progress.warn("No API key available for request")
        return 0, "no-api-key"

    headers = {"Authorization": f"Bearer {key}"}

    progress.step(f"Sending {method} request to {url}")

    # Prefer requests if installed
    if _requests is not None:
        try:
            if method.upper() == "GET":
                r = _requests.get(url, headers=headers, timeout=10)
            else:
                r = _requests.request(method.upper(), url, headers=headers, json=json_payload, timeout=10)
            return r.status_code, r.text
        except Exception as exc:
            return 0, f"requests-error: {exc}"

    # Fallback to urllib
    try:
        from urllib import request as _request

        data = None
        if json_payload is not None:
            data = json.dumps(json_payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = _request.Request(url, data=data, headers=headers, method=method.upper())
        with _request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            try:
                return resp.getcode(), body.decode("utf-8")
            except Exception:
                return resp.getcode(), str(body)
    except Exception as exc:
        return 0, f"urllib-error: {exc}"
