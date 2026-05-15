"""Basic tests for response parameter extraction helpers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from appdata.data_writer import DataWriter
from cache.cache_mgr import CacheManager
import network.requests as network_requests
from response.response_handler import (
    extract_file_artifact_candidates_from_text,
    format_parameterized_response,
    get_download_filename,
    is_downloadable_response,
    parameterize_json_response,
)
from utils.utilities import normalize_response_params, parse_json_text, resolve_param_path


class ResponseHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sample_response = {
            "id": "resp_123",
            "model": "gpt-4.1-mini",
            "status": "completed",
            "output": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "text": "Hello world",
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        }
        self.sample_text = json.dumps(self.sample_response)

    def test_parse_json_text_returns_none_for_invalid_input(self) -> None:
        self.assertIsNone(parse_json_text("not-json"))

    def test_resolve_param_path_reads_nested_values(self) -> None:
        self.assertEqual(
            resolve_param_path(self.sample_response, "output.0.content.0.text"),
            "Hello world",
        )

    def test_resolve_param_path_returns_default_for_invalid_index(self) -> None:
        self.assertEqual(
            resolve_param_path(self.sample_response, "output.9.content.0.text", default="missing"),
            "missing",
        )

    def test_normalize_response_params_uses_path_when_name_missing(self) -> None:
        normalized = normalize_response_params([{"path": "usage.total_tokens", "default": 0}])
        self.assertEqual(
            normalized,
            [{"name": "usage.total_tokens", "path": "usage.total_tokens", "default": 0}],
        )

    def test_parameterize_json_response_extracts_defaults(self) -> None:
        result = parameterize_json_response(
            self.sample_text,
            response_params=[
                {"name": "message_text", "path": "output.0.content.0.text", "default": ""},
                {"name": "missing_field", "path": "output.0.content.1.text", "default": "fallback"},
            ],
        )
        self.assertEqual(result["response_params"]["message_text"], "Hello world")
        self.assertEqual(result["response_params"]["missing_field"], "fallback")

    def test_parameterize_json_response_preserves_non_json_text(self) -> None:
        result = parameterize_json_response("plain-text-response")
        self.assertEqual(result["response_params"], {})
        self.assertEqual(result["raw_response"], "plain-text-response")

    def test_format_parameterized_response_returns_pretty_json(self) -> None:
        rendered = format_parameterized_response(self.sample_text)
        self.assertIn('\n  "response_params"', rendered)
        self.assertIn("Hello world", rendered)

    def test_download_header_helpers_detect_attachment_filename(self) -> None:
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Disposition": 'attachment; filename="analysis.txt"',
        }
        self.assertTrue(is_downloadable_response(headers))
        self.assertEqual(get_download_filename(headers), "analysis.txt")

    def test_download_filename_strips_path_segments(self) -> None:
        headers = {"Content-Disposition": 'attachment; filename="../nested/evil.txt"'}
        self.assertEqual(get_download_filename(headers), "evil.txt")

    def test_extract_file_artifact_candidate_from_sandbox_link(self) -> None:
        text = (
            'Here is the file:\n\n```\nAi for humanity\n```\n\n'
            "[Download Ai_for_humanity.txt](sandbox:/mnt/data/Ai_for_humanity.txt)"
        )
        candidates = list(extract_file_artifact_candidates_from_text(text))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["filename"], "Ai_for_humanity.txt")
        self.assertEqual(candidates[0]["mime"], "text/plain")
        self.assertEqual(candidates[0]["content"], b"Ai for humanity")


class NetworkDownloadTests(unittest.TestCase):
    def test_perform_api_request_saves_downloadable_response_artifact(self) -> None:
        original_appdata = os.environ.get("APPDATA")

        class FakeResponse:
            status_code = 200
            headers = {
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Disposition": 'attachment; filename="output.txt"',
                "Content-Length": "11",
            }
            content = b"Hello world"
            text = "Hello world"

        class FakeRequests:
            @staticmethod
            def get(*_args, **_kwargs):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            CacheManager(writer).save_api_key("sk_test_key_for_downloads")

            try:
                with patch("network.requests._requests", FakeRequests):
                    status, body = network_requests.perform_api_request("https://example.test/file.txt")
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

            self.assertEqual(status, 200)
            payload = json.loads(body)
            artifact = payload["artifact"]
            self.assertEqual(artifact["filename"], "output.txt")
            self.assertEqual(artifact["mime"], "text/plain")
            self.assertTrue(os.path.exists(artifact["path"]))
            records = CacheManager(writer).load_artifact_records()
            self.assertEqual(records[-1]["id"], artifact["id"])
            with open(artifact["path"], "rb") as handle:
                self.assertEqual(handle.read(), b"Hello world")

    def test_perform_openai_request_saves_artifact_link_from_response_text(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        response_body = {
            "id": "resp_123",
            "model": "gpt-4.1-mini",
            "status": "completed",
            "output": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                'Sure, here is the file:\n\n```\nAi for humanity\n```\n\n'
                                "[Download Ai_for_humanity.txt](sandbox:/mnt/data/Ai_for_humanity.txt)"
                            ),
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
            },
        }

        class FakeResponse:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            content = json.dumps(response_body).encode("utf-8")
            text = json.dumps(response_body)

        class FakeRequests:
            @staticmethod
            def request(*_args, **_kwargs):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            CacheManager(writer).save_api_key("sk_test_key_for_openai")

            try:
                with patch("network.requests._requests", FakeRequests):
                    status, body = network_requests.perform_api_request(
                        "https://api.openai.com/v1/responses",
                        method="POST",
                        json_payload={"model": "gpt-4.1-mini", "input": "make a text file"},
                    )
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

            self.assertEqual(status, 200)
            payload = json.loads(body)
            artifact = payload["artifacts"][0]
            self.assertEqual(artifact["filename"], "Ai_for_humanity.txt")
            self.assertEqual(artifact["mime"], "text/plain")
            self.assertEqual(artifact["source_url"], "sandbox:/mnt/data/Ai_for_humanity.txt")
            self.assertTrue(os.path.exists(artifact["path"]))
            with open(artifact["path"], "rb") as handle:
                self.assertEqual(handle.read(), b"Ai for humanity")
            records = CacheManager(writer).load_artifact_records()
            self.assertEqual(records[-1]["id"], artifact["id"])


if __name__ == "__main__":
    unittest.main()
