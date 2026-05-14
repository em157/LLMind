from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional, Tuple, Union

try:
    import requests as _requests
except Exception:
    _requests = None

from appdata.progress_output import ProgressOutput
from cache.cache_mgr import CacheManager
from appdata.data_writer import DataWriter


JsonDict = Dict[str, Any]


def perform_api_request(
    url: str,
    method: str = "POST",
    json_payload: Optional[JsonDict] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: Tuple[int, int] = (5, 60),  # connect, read
    retries: int = 2,
) -> Tuple[int, Union[str, JsonDict]]:
    """
    Robust LLM API request helper.

    Returns:
        (status_code, parsed_json_or_text)

    Suitable for:
        - OpenAI /v1/responses
        - OpenAI-compatible APIs
        - Anthropic-style APIs if extra_headers are supplied
        - local LLM servers
    """

    progress = ProgressOutput()
    writer = DataWriter()
    cache = CacheManager(writer)

    key = cache.load_api_key()
    if not key:
        progress.warn("No API key available for request")
        return 0, "no-api-key"

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "WinAgentCLI/0.1",
    }

    if extra_headers:
        headers.update(extra_headers)

    method = method.upper()

    for attempt in range(retries + 1):
        try:
            progress.step(f"Sending {method} request to {url}")

            if _requests is not None:
                r = _requests.request(
                    method,
                    url,
                    headers=headers,
                    json=json_payload if json_payload is not None else None,
                    timeout=timeout,
                )

                try:
                    body = r.json()
                except ValueError:
                    body = r.text

                if r.status_code >= 400:
                    return r.status_code, body

                return r.status_code, body

            # urllib fallback
            from urllib import request as _request
            from urllib.error import HTTPError, URLError

            data = None
            if json_payload is not None:
                data = json.dumps(json_payload).encode("utf-8")

            req = _request.Request(
                url,
                data=data,
                headers=headers,
                method=method,
            )

            with _request.urlopen(req, timeout=sum(timeout)) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return resp.getcode(), json.loads(raw)
                except json.JSONDecodeError:
                    return resp.getcode(), raw

        except Exception as exc:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return 0, f"request-error: {exc}"

    return 0, "unknown-request-error"
