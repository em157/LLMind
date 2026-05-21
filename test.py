"""Basic tests for response parameter extraction helpers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appdata.data_writer import DataWriter
from cache.cache_mgr import CacheManager
from main.LLMind import LLMindCLI
from hooks.hook_registry import HookRegistry, HookResult
from hooks.provider_adapters import render_provider_tools, render_openai_tools
import network.requests as network_requests
from network.providers import (
    build_payload_from_user_input,
    build_request_headers,
    detect_provider,
    get_default_payload,
    get_response_template_name,
    inject_api_key_into_url,
    normalize_provider_url,
    requires_post,
)
from response.response_handler import (
    extract_file_artifact_candidates,
    extract_file_artifact_candidates_from_text,
    format_parameterized_response,
    get_download_filename,
    is_downloadable_response,
    parameterize_json_response,
)
from response.model_hook_processor import (
    extract_hook_calls_from_response,
    get_model_capability_table,
    process_model_response_with_hooks,
)
from utils.utilities import normalize_response_params, parse_json_text, resolve_param_path


class SpyProgress:
    """Test double that captures progress messages instead of printing them."""

    def __init__(self) -> None:
        self.infos = []
        self.oks = []
        self.warns = []
        self.errors = []

    def info(self, message: str) -> None:
        self.infos.append(message)

    def ok(self, message: str) -> None:
        self.oks.append(message)

    def warn(self, message: str) -> None:
        self.warns.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)


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

    def test_download_filename_uses_content_type_extension_without_disposition(self) -> None:
        headers = {"Content-Type": "image/png"}
        self.assertEqual(get_download_filename(headers), "artifact.png")

    def test_downloadable_response_detects_binary_content_type(self) -> None:
        headers = {"Content-Type": "image/png"}
        self.assertTrue(is_downloadable_response(headers))

    def test_downloadable_response_rejects_json_without_disposition(self) -> None:
        headers = {"Content-Type": "application/json"}
        self.assertFalse(is_downloadable_response(headers))

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

    def test_extract_file_artifact_candidate_from_header_reference(self) -> None:
        # Mirrors the exact response shape from the problem statement: a
        # header line introducing a backticked filename, then a fenced code
        # block carrying the file content.
        text = (
            "Created `positive_sentiment.txt`:\n\n"
            "```txt\n"
            "Today brings bright opportunities, calm confidence, kind moments, "
            "steady progress, grateful thoughts, renewed energy, meaningful "
            "smiles, and hopeful beginnings ahead.\n"
            "```"
        )
        candidates = list(extract_file_artifact_candidates_from_text(text))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["filename"], "positive_sentiment.txt")
        self.assertEqual(candidates[0]["mime"], "text/plain")
        self.assertIsNone(candidates[0]["source_url"])
        self.assertIn(b"bright opportunities", candidates[0]["content"])
        self.assertTrue(candidates[0]["content"].endswith(b"hopeful beginnings ahead."))

    def test_extract_file_artifact_candidates_visitor_includes_header_artifact(self) -> None:
        # Verify "include" detection through the dict/list visitor used by the
        # network layer when formatting OpenAI responses.
        payload = {
            "response_params": {
                "message_text": (
                    "Created `positive_sentiment.txt`:\n\n"
                    "```txt\nToday brings bright opportunities.\n```"
                )
            },
            "raw_response": {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "Created `positive_sentiment.txt`:\n\n"
                                    "```txt\nToday brings bright opportunities.\n```"
                                ),
                            }
                        ],
                    }
                ]
            },
        }
        candidates = list(extract_file_artifact_candidates(payload))
        # Same filename + content appears in two places: deduped to a single
        # artifact by the visitor's seen-key tracking.
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["filename"], "positive_sentiment.txt")
        self.assertEqual(candidates[0]["content"], b"Today brings bright opportunities.")

    def test_extract_file_artifact_candidates_header_preserves_multiline_body(self) -> None:
        text = (
            "Saved `notes.md`:\n\n"
            "```markdown\n"
            "# Title\n\n"
            "- one\n"
            "- two\n"
            "```\n"
        )
        candidates = list(extract_file_artifact_candidates_from_text(text))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["filename"], "notes.md")
        self.assertEqual(candidates[0]["mime"], "text/markdown")
        self.assertEqual(
            candidates[0]["content"],
            b"# Title\n\n- one\n- two",
        )

    def test_extract_file_artifact_candidates_from_openai_image_generation_output(self) -> None:
        png_bytes = b"\x89PNG\r\n\x1a\n"
        payload = {
            "output": [
                {
                    "type": "image_generation_call",
                    "result": "iVBORw0KGgo=",
                }
            ]
        }
        candidates = list(extract_file_artifact_candidates(payload))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["filename"], "image_generation_1.png")
        self.assertEqual(candidates[0]["mime"], "image/png")
        self.assertEqual(candidates[0]["content"], png_bytes)

    def test_extract_file_artifact_candidates_from_openai_images_b64_json(self) -> None:
        png_bytes = b"\x89PNG\r\n\x1a\n"
        payload = {
            "data": [
                {
                    "b64_json": "iVBORw0KGgo=",
                }
            ]
        }
        candidates = list(extract_file_artifact_candidates(payload))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["filename"], "image_generation_1.png")
        self.assertEqual(candidates[0]["mime"], "image/png")
        self.assertEqual(candidates[0]["content"], png_bytes)

    def test_extract_file_artifact_candidates_from_openai_images_url(self) -> None:
        payload = {
            "data": [
                {
                    "url": "https://example.test/images/generated_star.png",
                }
            ]
        }
        candidates = list(extract_file_artifact_candidates(payload))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["filename"], "generated_star.png")
        self.assertTrue(candidates[0]["remote_fetch"])
        self.assertIsNone(candidates[0]["content"])

    def test_extract_file_artifact_candidate_remote_image_link(self) -> None:
        text = "Generated image: [output.png](https://example.test/generated/output.png)"
        candidates = list(extract_file_artifact_candidates_from_text(text))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["filename"], "output.png")
        self.assertTrue(candidates[0]["remote_fetch"])
        self.assertIsNone(candidates[0]["content"])

    def test_extract_file_artifact_candidate_from_plain_text_with_file_cue(self) -> None:
        text = (
            "Please save this file as `essay.txt`. "
            "This is a long essay body that should be persisted as an artifact when explicit "
            "file intent is present in the response text so users can download it locally."
        )
        candidates = list(extract_file_artifact_candidates_from_text(text))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["filename"], "essay.txt")
        self.assertEqual(candidates[0]["mime"], "text/plain")
        self.assertIn(b"Please save this file", candidates[0]["content"])

    def test_extract_file_artifact_candidate_plain_text_without_file_cue_is_skipped(self) -> None:
        text = (
            "This response is intentionally long but does not include an explicit persistence instruction and "
            "should remain ordinary assistant text rather than being written as an artifact candidate."
        )
        candidates = list(extract_file_artifact_candidates_from_text(text))
        self.assertEqual(candidates, [])


class NetworkDownloadTests(unittest.TestCase):
    def test_perform_api_request_returns_http_error_for_requests_4xx(self) -> None:
        original_appdata = os.environ.get("APPDATA")

        class FakeResponse:
            status_code = 400
            reason = "Bad Request"
            headers = {"Content-Type": "application/json"}
            content = b'{"error":{"message":"Missing required parameter: prompt"}}'
            text = '{"error":{"message":"Missing required parameter: prompt"}}'

        class FakeRequests:
            @staticmethod
            def request(*_args, **_kwargs):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            CacheManager(writer).save_api_key("sk_test_key_for_http_error")

            try:
                with patch("network.requests._requests", FakeRequests):
                    status, body = network_requests.perform_api_request(
                        "https://api.openai.com/v1/responses",
                        method="POST",
                        json_payload={"model": "gpt-4.1-mini", "input": "hello"},
                    )
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        self.assertEqual(status, 400)
        self.assertIn("http-error: HTTP 400 Bad Request", body)
        self.assertIn("Missing required parameter", body)

    def test_openai_images_request_uses_extended_timeout(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        captured_timeout = {"value": None}

        class FakeResponse:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            content = b'{"data":[]}'
            text = '{"data":[]}'

        class FakeRequests:
            @staticmethod
            def request(*_args, **kwargs):
                captured_timeout["value"] = kwargs.get("timeout")
                return FakeResponse()

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            CacheManager(writer).save_api_key("sk_test_key_for_timeout")

            try:
                with patch("network.requests._requests", FakeRequests):
                    status, _ = network_requests.perform_api_request(
                        "https://api.openai.com/v1/images/generations",
                        method="POST",
                        json_payload={"model": "gpt-image-1", "prompt": "make stars"},
                    )
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        self.assertEqual(status, 200)
        self.assertEqual(captured_timeout["value"], 120)

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

    def test_perform_api_request_saves_binary_response_without_disposition(self) -> None:
        original_appdata = os.environ.get("APPDATA")

        class FakeResponse:
            status_code = 200
            headers = {
                "Content-Type": "image/png",
                "Content-Length": "8",
            }
            content = b"\x89PNG\r\n\x1a\n"
            text = ""

        class FakeRequests:
            @staticmethod
            def get(*_args, **_kwargs):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            CacheManager(writer).save_api_key("sk_test_key_for_binary_download")

            try:
                with patch("network.requests._requests", FakeRequests):
                    status, body = network_requests.perform_api_request("https://example.test/image")
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

            self.assertEqual(status, 200)
            payload = json.loads(body)
            artifact = payload["artifact"]
            self.assertEqual(artifact["filename"], "artifact.png")
            self.assertEqual(artifact["mime"], "image/png")
            self.assertTrue(os.path.exists(artifact["path"]))
            with open(artifact["path"], "rb") as handle:
                self.assertEqual(handle.read(), b"\x89PNG\r\n\x1a\n")

    def test_perform_openai_request_fetches_remote_image_artifact(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        response_body = {
            "id": "resp_remote_123",
            "model": "gpt-4.1-mini",
            "status": "completed",
            "output": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Generated image: [render.png](https://example.test/files/render.png)",
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

        class FakeRequestResponse:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            content = json.dumps(response_body).encode("utf-8")
            text = json.dumps(response_body)

        class FakeImageResponse:
            status_code = 200
            headers = {
                "Content-Type": "image/png",
                "Content-Disposition": 'attachment; filename="rendered.png"',
            }
            content = b"\x89PNG\r\n\x1a\n"

        class FakeRequests:
            @staticmethod
            def request(*_args, **_kwargs):
                return FakeRequestResponse()

            @staticmethod
            def get(*_args, **_kwargs):
                return FakeImageResponse()

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            CacheManager(writer).save_api_key("sk_test_key_for_remote_image")

            try:
                with patch("network.requests._requests", FakeRequests):
                    status, body = network_requests.perform_api_request(
                        "https://api.openai.com/v1/responses",
                        method="POST",
                        json_payload={"model": "gpt-4.1-mini", "input": "make an image"},
                    )
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

            self.assertEqual(status, 200)
            payload = json.loads(body)
            artifact = payload["artifacts"][0]
            self.assertEqual(artifact["filename"], "rendered.png")
            self.assertEqual(artifact["mime"], "image/png")
            self.assertEqual(artifact["source_url"], "https://example.test/files/render.png")
            self.assertTrue(os.path.exists(artifact["path"]))

    def test_perform_openai_request_saves_base64_image_generation_artifact(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        response_body = {
            "id": "resp_img_123",
            "model": "gpt-4.1-mini",
            "status": "completed",
            "output": [
                {
                    "type": "image_generation_call",
                    "result": "iVBORw0KGgo=",
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
            CacheManager(writer).save_api_key("sk_test_key_for_openai_image_generation")

            try:
                with patch("network.requests._requests", FakeRequests):
                    status, body = network_requests.perform_api_request(
                        "https://api.openai.com/v1/responses",
                        method="POST",
                        json_payload={
                            "model": "gpt-4.1-mini",
                            "input": "Generate an image",
                            "tools": [{"type": "image_generation"}],
                        },
                    )
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertIn("artifacts", payload)
            artifact = payload["artifacts"][0]
            self.assertEqual(artifact["filename"], "image_generation_1.png")
            self.assertEqual(artifact["mime"], "image/png")
            self.assertTrue(os.path.exists(artifact["path"]))
            with open(artifact["path"], "rb") as handle:
                self.assertEqual(handle.read(), b"\x89PNG\r\n\x1a\n")


class LoggingTests(unittest.TestCase):
    def test_write_artifact_logs_storage_steps(self) -> None:
        original_appdata = os.environ.get("APPDATA")

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            writer.progress = SpyProgress()

            try:
                path = writer.write_artifact("artifact-123", "output.txt", b"hello")
                self.assertTrue(path.exists())
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        self.assertIn("Resolving appdata directory for artifact storage...", writer.progress.infos)
        self.assertTrue(any("Creating artifact directory:" in message for message in writer.progress.infos))
        self.assertTrue(any("Writing artifact file to temporary path:" in message for message in writer.progress.infos))
        self.assertTrue(any("Artifact file moved to final destination:" in message for message in writer.progress.oks))

    def test_store_response_artifacts_logs_search_and_summary(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        body = json.dumps(
            {
                "response_params": {
                    "message_text": "Created `notes.txt`:\n\n```txt\nhello world\n```"
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            cli = LLMindCLI()
            cli.progress = SpyProgress()
            cli.writer.progress = cli.progress

            try:
                paths = cli._store_response_artifacts(body)
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        self.assertEqual(len(paths), 1)
        self.assertIn("Searching for artifacts in the response...", cli.progress.infos)
        self.assertIn("Found artifact candidate.", cli.progress.infos)
        self.assertTrue(any("Downloading and caching artifact with ID:" in message for message in cli.progress.infos))
        self.assertTrue(any("Artifact saved to:" in message for message in cli.progress.oks))
        self.assertIn("Total artifacts downloaded and saved: 1", cli.progress.oks)

    def test_run_logs_appdata_initialization(self) -> None:
        original_appdata = os.environ.get("APPDATA")

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            cli = LLMindCLI()
            cli.progress = SpyProgress()
            cli.writer.progress = cli.progress

            try:
                with patch.object(cli, "show_banner"), patch("builtins.input", side_effect=["q"]):
                    exit_code = cli.run()
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        self.assertEqual(exit_code, 0)
        self.assertIn("Initializing appdata...", cli.progress.infos)
        self.assertTrue(any("AppData directory resolved to:" in message for message in cli.progress.infos))


class WindowsHookValidationTests(unittest.TestCase):
    def test_filesystem_hook_validation_success(self) -> None:
        original_appdata = os.environ.get("APPDATA")

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            cli = LLMindCLI()
            ok, message = cli._validate_filesystem_hook()

        if original_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = original_appdata

        self.assertTrue(ok)
        self.assertIn("Read/write validated", message)

    def test_registry_hook_validation_non_windows(self) -> None:
        cli = LLMindCLI()
        with patch("hooks.hook_registry.os.name", "posix"):
            ok, message = cli._validate_registry_hook()
        self.assertFalse(ok)
        self.assertIn("only available on Windows", message)

    def test_run_windows_hook_self_test_logs_status(self) -> None:
        original_appdata = os.environ.get("APPDATA")

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            cli = LLMindCLI()
            cli.progress = SpyProgress()
            with patch.object(
                cli.hook_registry,
                "execute_many",
                return_value=[
                    HookResult("filesystem_access", True, "simulated fs ok"),
                    HookResult("registry_settings", False, "simulated registry unavailable"),
                ],
            ):
                cli.run_windows_hook_self_test()

        if original_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = original_appdata

        self.assertIn("Filesystem Hook Success: simulated fs ok", cli.progress.oks)
        self.assertIn("Registry Hook Failure: simulated registry unavailable", cli.progress.errors)


class HookRegistryTests(unittest.TestCase):
    def test_execute_unknown_hook_returns_validation_error(self) -> None:
        registry = HookRegistry(app_name="LLMind")
        registry.register_builtin_hooks()
        ctx = registry.build_context(Path("."))
        result = registry.execute("does_not_exist", ctx)
        self.assertFalse(result.success)
        self.assertIn("Unknown hook", result.message)

    def test_generate_persistent_hook_module_validates_and_writes_file(self) -> None:
        registry = HookRegistry(app_name="LLMind")
        registry.register_builtin_hooks()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "persistent_hooks.py"
            generated = registry.generate_persistent_hook_module(
                ["filesystem_access", "registry_settings"],
                output_file,
            )

            self.assertEqual(generated, output_file)
            self.assertTrue(output_file.exists())
            content = output_file.read_text(encoding="utf-8")
            self.assertIn("def build_registry", content)
            self.assertIn("FileSystemAccessHook", content)
            self.assertIn("RegistrySettingsHook", content)

    def test_generate_persistent_hook_module_rejects_unknown_hook(self) -> None:
        registry = HookRegistry(app_name="LLMind")
        registry.register_builtin_hooks()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "persistent_hooks.py"
            with self.assertRaises(ValueError):
                registry.generate_persistent_hook_module(["bogus_hook"], output_file)

    def test_parse_html_content_detects_comment_inputs(self) -> None:
        registry = HookRegistry(app_name="LLMind")
        registry.register_builtin_hooks()
        html = (
            "<html><body>"
            "<input type='text' id='search-box' placeholder='Search'>"
            "<textarea id='comment-box' name='comment' placeholder='Write a comment'></textarea>"
            "</body></html>"
        )
        ctx = registry.build_context(
            Path("."),
            extras={
                "hook_args": {
                    "action": "comment_inputs",
                    "html": html,
                }
            },
        )
        result = registry.execute("parse_html_content", ctx)
        self.assertTrue(result.success)
        self.assertGreaterEqual(int(result.details.get("comment_candidate_count", 0)), 1)
        candidates = result.details.get("comment_input_candidates", [])
        self.assertTrue(any(str(item.get("tag", "")) == "textarea" for item in candidates))
    def test_fetch_webpage_html_downloads_and_parses_comment_inputs(self) -> None:
        registry = HookRegistry(app_name="LLMind")
        registry.register_builtin_hooks()

        html = (
            "<html><body>"
            "<input type='text' id='search-box' placeholder='Search'>"
            "<textarea id='comment-box' name='comment' placeholder='Write a comment'></textarea>"
            "</body></html>"
        )

        class _Headers(dict):
            def get_content_charset(self):
                return "utf-8"

        class _Response:
            def __init__(self, body: str) -> None:
                self.status = 200
                self.headers = _Headers({"Content-Type": "text/html; charset=utf-8"})
                self._body = body.encode("utf-8")

            def read(self, _size: int = -1):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("hooks.hook_registry.urlopen", return_value=_Response(html)):
                ctx = registry.build_context(
                    Path(tmpdir),
                    extras={
                        "hook_args": {
                            "action": "download_parse",
                            "url": "https://example.test/post",
                            "parse_action": "comment_inputs",
                            "parser_engine": "html_parser",
                            "save_filename": "post_page.html",
                        }
                    },
                )
                result = registry.execute("fetch_webpage_html", ctx)
                artifact_path = Path(str(result.details.get("artifact_path", "")))
                artifact_exists_during_run = artifact_path.exists()

        self.assertTrue(result.success)
        self.assertGreaterEqual(int(result.details.get("interactive_count", 0)), 1)
        self.assertGreaterEqual(int(result.details.get("comment_candidate_count", 0)), 1)
        self.assertTrue(artifact_exists_during_run)


class ProviderAdapterTests(unittest.TestCase):
    def test_openai_adapter_renders_function_tools(self) -> None:
        tools = render_openai_tools()
        self.assertTrue(tools)
        first = tools[0]
        self.assertEqual(first.get("type"), "function")
        self.assertIn("function", first)

    def test_provider_adapter_routes_openai_style(self) -> None:
        rendered = render_provider_tools("openai")
        self.assertIn("tools", rendered)
        self.assertTrue(rendered["tools"])
        self.assertEqual(rendered.get("tool_choice"), "auto")

    def test_provider_adapter_routes_anthropic_tools(self) -> None:
        rendered = render_provider_tools("anthropic")
        self.assertIn("tools", rendered)
        self.assertTrue(rendered["tools"])
        self.assertIn("input_schema", rendered["tools"][0])

    def test_provider_adapter_routes_gemini_tools(self) -> None:
        rendered = render_provider_tools("gemini")
        self.assertIn("tools", rendered)
        self.assertTrue(rendered["tools"])
        self.assertIn("function_declarations", rendered["tools"][0])

    def test_provider_adapter_includes_parse_html_content_schema(self) -> None:
        rendered = render_provider_tools("gemini")
        declarations = rendered["tools"][0]["function_declarations"]
        names = [item.get("name") for item in declarations]
        self.assertIn("parse_html_content", names)
    def test_provider_adapter_includes_fetch_webpage_html_schema(self) -> None:
        rendered = render_provider_tools("gemini")
        declarations = rendered["tools"][0]["function_declarations"]
        names = [item.get("name") for item in declarations]
        self.assertIn("fetch_webpage_html", names)


class ProviderDetectionTests(unittest.TestCase):
    def test_detects_openai(self) -> None:
        self.assertEqual(detect_provider("https://api.openai.com/v1/responses"), "openai")
        self.assertEqual(detect_provider("https://api.openai.com/v1/chat/completions"), "openai")

    def test_detects_anthropic(self) -> None:
        self.assertEqual(detect_provider("https://api.anthropic.com/v1/messages"), "anthropic")

    def test_detects_xai(self) -> None:
        self.assertEqual(detect_provider("https://api.x.ai/v1/chat/completions"), "xai")

    def test_detects_gemini(self) -> None:
        self.assertEqual(
            detect_provider(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
            ),
            "gemini",
        )

    def test_detects_generic(self) -> None:
        self.assertEqual(detect_provider("https://httpbin.org/get"), "generic")

    def test_openai_responses_template(self) -> None:
        self.assertEqual(
            get_response_template_name("openai", "https://api.openai.com/v1/responses"),
            "openai_responses",
        )

    def test_openai_chat_template(self) -> None:
        self.assertEqual(
            get_response_template_name("openai", "https://api.openai.com/v1/chat/completions"),
            "openai_chat",
        )

    def test_openai_images_template(self) -> None:
        self.assertEqual(
            get_response_template_name("openai", "https://api.openai.com/v1/images/generations"),
            "openai_images",
        )

    def test_openai_images_template_base_path(self) -> None:
        self.assertEqual(
            get_response_template_name("openai", "https://api.openai.com/v1/images"),
            "openai_images",
        )

    def test_anthropic_template(self) -> None:
        self.assertEqual(get_response_template_name("anthropic"), "anthropic_messages")

    def test_xai_template(self) -> None:
        self.assertEqual(get_response_template_name("xai"), "xai_chat")

    def test_gemini_template(self) -> None:
        self.assertEqual(get_response_template_name("gemini"), "gemini_generate")

    def test_requires_post_anthropic(self) -> None:
        self.assertTrue(requires_post("anthropic", "https://api.anthropic.com/v1/messages"))

    def test_requires_post_xai(self) -> None:
        self.assertTrue(requires_post("xai", "https://api.x.ai/v1/chat/completions"))

    def test_requires_post_gemini(self) -> None:
        self.assertTrue(
            requires_post(
                "gemini",
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            )
        )

    def test_requires_post_openai_responses(self) -> None:
        self.assertTrue(requires_post("openai", "https://api.openai.com/v1/responses"))

    def test_requires_post_openai_images(self) -> None:
        self.assertTrue(requires_post("openai", "https://api.openai.com/v1/images"))
        self.assertTrue(requires_post("openai", "https://api.openai.com/v1/images/generations"))

    def test_generic_does_not_require_post(self) -> None:
        self.assertFalse(requires_post("generic", "https://httpbin.org/get"))


class ProviderHeaderTests(unittest.TestCase):
    def test_openai_uses_bearer_auth(self) -> None:
        headers = build_request_headers("openai", "sk-test-key")
        self.assertEqual(headers["Authorization"], "Bearer sk-test-key")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_anthropic_uses_x_api_key(self) -> None:
        headers = build_request_headers("anthropic", "ant-test-key")
        self.assertEqual(headers["x-api-key"], "ant-test-key")
        self.assertIn("anthropic-version", headers)
        self.assertNotIn("Authorization", headers)

    def test_xai_uses_bearer_auth(self) -> None:
        headers = build_request_headers("xai", "xai-test-key")
        self.assertEqual(headers["Authorization"], "Bearer xai-test-key")

    def test_gemini_has_no_auth_header(self) -> None:
        headers = build_request_headers("gemini", "gm-test-key")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("x-api-key", headers)
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_gemini_key_injected_into_url(self) -> None:
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
        result = inject_api_key_into_url(url, "gemini", "MY_GEM_KEY")
        self.assertIn("key=MY_GEM_KEY", result)

    def test_non_gemini_url_unchanged(self) -> None:
        url = "https://api.openai.com/v1/responses"
        result = inject_api_key_into_url(url, "openai", "sk-key")
        self.assertEqual(result, url)

    def test_normalize_openai_images_url(self) -> None:
        normalized, warning = normalize_provider_url("openai", "https://api.openai.com/v1/images")
        self.assertEqual(normalized, "https://api.openai.com/v1/images/generations")
        self.assertIsNotNone(warning)

    def test_normalize_non_images_url_unchanged(self) -> None:
        url = "https://api.openai.com/v1/responses"
        normalized, warning = normalize_provider_url("openai", url)
        self.assertEqual(normalized, url)
        self.assertIsNone(warning)


class ProviderPayloadTests(unittest.TestCase):
    def test_openai_default_payload(self) -> None:
        payload = get_default_payload("openai")
        self.assertIn("model", payload)
        self.assertIn("input", payload)

    def test_openai_images_default_payload(self) -> None:
        payload = get_default_payload("openai", url="https://api.openai.com/v1/images/generations")
        self.assertEqual(payload["model"], "gpt-image-1")
        self.assertIn("prompt", payload)

    def test_anthropic_default_payload_has_messages(self) -> None:
        payload = get_default_payload("anthropic")
        self.assertIn("messages", payload)
        self.assertIn("max_tokens", payload)
        self.assertEqual(payload["messages"][0]["role"], "user")

    def test_xai_default_payload_has_messages(self) -> None:
        payload = get_default_payload("xai")
        self.assertIn("messages", payload)

    def test_gemini_default_payload_has_contents(self) -> None:
        payload = get_default_payload("gemini")
        self.assertIn("contents", payload)
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "Hello from LLMind")

    def test_build_anthropic_payload_includes_system(self) -> None:
        payload = build_payload_from_user_input(
            "anthropic", "claude-opus-4-5", "Hello", system_instructions="Be concise."
        )
        self.assertEqual(payload["system"], "Be concise.")
        self.assertEqual(payload["messages"][0]["content"], "Hello")

    def test_build_xai_payload_uses_chat_format(self) -> None:
        payload = build_payload_from_user_input(
            "xai", "grok-3", "Hello", system_instructions="Be helpful.", temperature=0.5
        )
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["role"], "user")
        self.assertEqual(payload["temperature"], 0.5)

    def test_build_openai_chat_payload(self) -> None:
        payload = build_payload_from_user_input("openai_chat", "gpt-4o", "Hi", max_tokens=100)
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["max_tokens"], 100)

    def test_build_gemini_payload_with_generation_config(self) -> None:
        payload = build_payload_from_user_input(
            "gemini", "gemini-2.0-flash", "Hello", temperature=0.7, max_tokens=256
        )
        self.assertEqual(payload["generationConfig"]["temperature"], 0.7)
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 256)

    def test_build_openai_responses_payload(self) -> None:
        payload = build_payload_from_user_input(
            "openai", "gpt-4.1-mini", "Hello", system_instructions="You are helpful.", max_tokens=512
        )
        self.assertEqual(payload["input"], "Hello")
        self.assertEqual(payload["instructions"], "You are helpful.")
        self.assertEqual(payload["max_output_tokens"], 512)

    def test_build_openai_image_responses_payload_uses_multimodal_input(self) -> None:
        payload = build_payload_from_user_input(
            "openai",
            "gpt-image-2",
            "Create a temple of happiness with tall arches and a mysterious location.",
        )
        self.assertEqual(payload["model"], "gpt-image-2")
        self.assertEqual(payload["input"][0]["role"], "user")
        self.assertEqual(
            payload["input"][0]["content"][0]["text"],
            "Create a temple of happiness with tall arches and a mysterious location.",
        )

    def test_build_openai_images_payload(self) -> None:
        payload = build_payload_from_user_input(
            "openai_images",
            "gpt-image-1",
            "Make a star pattern",
            system_instructions="High contrast",
        )
        self.assertEqual(payload["model"], "gpt-image-1")
        self.assertIn("High contrast", payload["prompt"])
        self.assertIn("Make a star pattern", payload["prompt"])


class AnthropicRequestTests(unittest.TestCase):
    """Mock-based tests for Anthropic /v1/messages requests."""

    _ANTHROPIC_RESPONSE = {
        "id": "msg_abc123",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-5",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Hello from Claude!"}],
        "usage": {"input_tokens": 10, "output_tokens": 6},
    }

    def _run_request(self, tmpdir: str) -> tuple:
        os.environ["APPDATA"] = tmpdir
        writer = DataWriter()
        CacheManager(writer).save_api_key("ant-test-key-for-anthropic")

        class FakeResponse:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            content = json.dumps(self._ANTHROPIC_RESPONSE).encode("utf-8")
            text = json.dumps(self._ANTHROPIC_RESPONSE)

        class FakeRequests:
            @staticmethod
            def request(*_args, **_kwargs):
                return FakeResponse()

        with patch("network.requests._requests", FakeRequests):
            return network_requests.perform_api_request(
                "https://api.anthropic.com/v1/messages",
                method="POST",
                json_payload={
                    "model": "claude-opus-4-5",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

    def test_anthropic_response_extracts_message_text(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                status, body = self._run_request(tmpdir)
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["response_params"]["message_text"], "Hello from Claude!")
        self.assertEqual(payload["response_params"]["stop_reason"], "end_turn")
        self.assertEqual(payload["response_params"]["input_tokens"], 10)
        self.assertIn("hook_processing", payload)
        self.assertEqual(payload["hook_processing"]["provider"], "anthropic")
        self.assertIn("validated_hook_calls", payload["hook_processing"])


class XAIRequestTests(unittest.TestCase):
    """Mock-based tests for xAI /v1/chat/completions requests."""

    _XAI_RESPONSE = {
        "id": "chatcmpl-xai-123",
        "object": "chat.completion",
        "model": "grok-3",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello from Grok!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
    }

    def _run_request(self, tmpdir: str) -> tuple:
        os.environ["APPDATA"] = tmpdir
        writer = DataWriter()
        CacheManager(writer).save_api_key("xai-test-key-for-grok")

        class FakeResponse:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            content = json.dumps(self._XAI_RESPONSE).encode("utf-8")
            text = json.dumps(self._XAI_RESPONSE)

        class FakeRequests:
            @staticmethod
            def request(*_args, **_kwargs):
                return FakeResponse()

        with patch("network.requests._requests", FakeRequests):
            return network_requests.perform_api_request(
                "https://api.x.ai/v1/chat/completions",
                method="POST",
                json_payload={
                    "model": "grok-3",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

    def test_xai_response_extracts_message_text(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                status, body = self._run_request(tmpdir)
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["response_params"]["message_text"], "Hello from Grok!")
        self.assertEqual(payload["response_params"]["finish_reason"], "stop")
        self.assertEqual(payload["response_params"]["total_tokens"], 9)
        self.assertIn("hook_processing", payload)
        self.assertEqual(payload["hook_processing"]["provider"], "xai")
        self.assertIn("validated_hook_calls", payload["hook_processing"])


class GeminiRequestTests(unittest.TestCase):
    """Mock-based tests for Google Gemini generateContent requests."""

    _GEMINI_RESPONSE = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Hello from Gemini!"}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 3,
            "candidatesTokenCount": 4,
            "totalTokenCount": 7,
        },
    }

    def _run_request(self, tmpdir: str) -> tuple:
        os.environ["APPDATA"] = tmpdir
        writer = DataWriter()
        CacheManager(writer).save_api_key("gm-test-key-for-gemini")

        class FakeResponse:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            content = json.dumps(self._GEMINI_RESPONSE).encode("utf-8")
            text = json.dumps(self._GEMINI_RESPONSE)

        class FakeRequests:
            @staticmethod
            def request(*_args, **_kwargs):
                return FakeResponse()

        with patch("network.requests._requests", FakeRequests):
            return network_requests.perform_api_request(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                method="POST",
                json_payload={"contents": [{"parts": [{"text": "Hello"}]}]},
            )

    def test_gemini_response_extracts_message_text(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                status, body = self._run_request(tmpdir)
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["response_params"]["message_text"], "Hello from Gemini!")
        self.assertEqual(payload["response_params"]["finish_reason"], "STOP")
        self.assertEqual(payload["response_params"]["total_tokens"], 7)
        self.assertIn("hook_processing", payload)
        self.assertEqual(payload["hook_processing"]["provider"], "gemini")
        self.assertIn("validated_hook_calls", payload["hook_processing"])

    def test_gemini_key_injected_before_request(self) -> None:
        """Verify the Gemini API key is appended to the URL as ?key=."""
        original_appdata = os.environ.get("APPDATA")
        captured_urls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            CacheManager(writer).save_api_key("gm-test-key-url-inject")

            class FakeResponse:
                status_code = 200
                headers = {"Content-Type": "application/json"}
                content = json.dumps(self._GEMINI_RESPONSE).encode("utf-8")
                text = json.dumps(self._GEMINI_RESPONSE)

            class FakeRequests:
                @staticmethod
                def request(method, url, **_kwargs):
                    captured_urls.append(url)
                    return FakeResponse()

            try:
                with patch("network.requests._requests", FakeRequests):
                    network_requests.perform_api_request(
                        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                        method="POST",
                        json_payload={"contents": [{"parts": [{"text": "Hi"}]}]},
                    )
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        self.assertEqual(len(captured_urls), 1)
        self.assertIn("key=gm-test-key-url-inject", captured_urls[0])


class HookOrchestrationDebugTests(unittest.TestCase):
    def test_openai_orchestration_reports_no_followup_payload(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            cache = CacheManager(writer)

            def _send_followup(_payload):
                raise AssertionError("send_followup_request should not be called")

            request_payload = {"messages": [{"role": "user", "content": "Hello"}]}
            initial_bundle = {
                "raw_response": {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "No tool call in this response",
                            }
                        }
                    ]
                },
                "hook_processing": {"hook_results": []},
            }

            try:
                result = network_requests._run_openai_chat_hook_orchestration(
                    request_payload=request_payload,
                    initial_bundle=initial_bundle,
                    send_followup_request=_send_followup,
                    provider="openai",
                    response_params=None,
                    response_template="openai_chat",
                    writer=writer,
                    cache=cache,
                    resolved_executables=None,
                )
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        orchestration = result.get("orchestration", {})
        self.assertEqual(orchestration.get("stopped_reason"), "no_followup_payload")
        self.assertEqual(orchestration.get("stop_step"), 1)
        self.assertEqual(orchestration.get("iterations"), [])

    def test_openai_orchestration_reports_no_executed_hooks(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            cache = CacheManager(writer)

            def _send_followup(_payload):
                response = {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Final answer without tool calls",
                            }
                        }
                    ]
                }
                return 200, json.dumps(response), "OK"

            request_payload = {"messages": [{"role": "user", "content": "Hello"}]}
            initial_bundle = {
                "raw_response": {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "filesystem_access",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                "hook_processing": {
                    "hook_results": [
                        {
                            "hook_name": "filesystem_access",
                            "success": True,
                            "message": "ok",
                            "details": {},
                        }
                    ]
                },
            }

            try:
                result = network_requests._run_openai_chat_hook_orchestration(
                    request_payload=request_payload,
                    initial_bundle=initial_bundle,
                    send_followup_request=_send_followup,
                    provider="openai",
                    response_params=None,
                    response_template="openai_chat",
                    writer=writer,
                    cache=cache,
                    resolved_executables=None,
                )
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        orchestration = result.get("orchestration", {})
        self.assertEqual(orchestration.get("stopped_reason"), "no_executed_hooks")
        self.assertEqual(orchestration.get("stop_step"), 1)
        self.assertEqual(len(orchestration.get("iterations", [])), 1)
        self.assertEqual(orchestration["iterations"][0].get("executed_hook_calls"), 0)

    def test_openai_orchestration_reports_followup_http_error_step(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            cache = CacheManager(writer)

            def _send_followup(_payload):
                return 500, '{"error":"boom"}', "Internal Server Error"

            request_payload = {"messages": [{"role": "user", "content": "Hello"}]}
            initial_bundle = {
                "raw_response": {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "filesystem_access",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                "hook_processing": {
                    "hook_results": [
                        {
                            "hook_name": "filesystem_access",
                            "success": True,
                            "message": "ok",
                            "details": {},
                        }
                    ]
                },
            }

            try:
                result = network_requests._run_openai_chat_hook_orchestration(
                    request_payload=request_payload,
                    initial_bundle=initial_bundle,
                    send_followup_request=_send_followup,
                    provider="openai",
                    response_params=None,
                    response_template="openai_chat",
                    writer=writer,
                    cache=cache,
                    resolved_executables=None,
                )
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        orchestration = result.get("orchestration", {})
        self.assertEqual(orchestration.get("stopped_reason"), "followup_http_error")
        self.assertEqual(orchestration.get("stop_step"), 1)
        self.assertIn("HTTP 500", orchestration.get("error", ""))

    def test_gemini_orchestration_attempts_recovery_after_zero_hooks(self) -> None:
        original_appdata = os.environ.get("APPDATA")
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["APPDATA"] = tmpdir
            writer = DataWriter()
            cache = CacheManager(writer)
            followup_count = {"value": 0}

            def _send_followup(_payload):
                followup_count["value"] += 1
                if followup_count["value"] == 1:
                    response = {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": "Done."}],
                                },
                                "finishReason": "STOP",
                            }
                        ]
                    }
                    return 200, json.dumps(response), "OK"
                if followup_count["value"] == 2:
                    response = {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "name": "filesystem_access",
                                                "args": {},
                                            }
                                        }
                                    ],
                                },
                                "finishReason": "STOP",
                            }
                        ]
                    }
                    return 200, json.dumps(response), "OK"

                response = {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": "Complete."}],
                            },
                            "finishReason": "STOP",
                        }
                    ]
                }
                return 200, json.dumps(response), "OK"

            request_payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": "Hello"}],
                    }
                ]
            }
            initial_bundle = {
                "raw_response": {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "filesystem_access",
                                            "args": {},
                                        }
                                    }
                                ],
                            }
                        }
                    ]
                },
                "hook_processing": {
                    "hook_results": [
                        {
                            "hook_name": "filesystem_access",
                            "success": True,
                            "message": "ok",
                            "details": {},
                        }
                    ]
                },
            }

            try:
                result = network_requests._run_gemini_hook_orchestration(
                    request_payload=request_payload,
                    initial_bundle=initial_bundle,
                    send_followup_request=_send_followup,
                    provider="gemini",
                    response_params=None,
                    response_template="gemini_generate",
                    writer=writer,
                    cache=cache,
                    resolved_executables=None,
                )
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata

        orchestration = result.get("orchestration", {})
        self.assertTrue(orchestration.get("recovery_attempted"))
        self.assertGreaterEqual(followup_count["value"], 2)
        self.assertTrue(any(isinstance(item, dict) and item.get("recovery") for item in orchestration.get("iterations", [])))


class ModelHookProcessorTests(unittest.TestCase):
    def test_capability_table_contains_expected_providers(self) -> None:
        providers = {row["provider"] for row in get_model_capability_table()}
        self.assertIn("OpenAI", providers)
        self.assertIn("Anthropic", providers)
        self.assertIn("Google Gemini", providers)
        self.assertIn("xAI", providers)

    def test_extract_openai_tool_call(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "filesystem_access",
                                    "arguments": "{}",
                                }
                            }
                        ]
                    }
                }
            ]
        }
        calls, warnings = extract_hook_calls_from_response(payload, provider="openai")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["hook_name"], "filesystem_access")
        self.assertEqual(calls[0]["args"], {})
        self.assertEqual(warnings, [])

    def test_extract_anthropic_tool_use(self) -> None:
        payload = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "filesystem_access",
                    "input": {"reason": "healthcheck"},
                }
            ]
        }
        calls, _warnings = extract_hook_calls_from_response(payload, provider="anthropic")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["hook_name"], "filesystem_access")
        self.assertEqual(calls[0]["args"]["reason"], "healthcheck")

    def test_extract_gemini_function_call(self) -> None:
        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "filesystem_access",
                                    "args": {},
                                }
                            }
                        ]
                    }
                }
            ]
        }
        calls, _warnings = extract_hook_calls_from_response(payload, provider="gemini")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["hook_name"], "filesystem_access")

    def test_process_model_response_validates_unknown_hook(self) -> None:
        registry = HookRegistry()
        registry.register_builtin_hooks()
        payload = {
            "hook_calls": [
                {"hook": "not_a_real_hook", "args": {}},
            ]
        }
        result = process_model_response_with_hooks(
            payload=payload,
            provider="openai",
            registry=registry,
            app_data_dir=Path("."),
            execute=False,
        )
        self.assertEqual(len(result["validated_hook_calls"]), 0)
        self.assertTrue(any("Unknown hook" in item for item in result["validation_errors"]))

    def test_process_model_response_executes_valid_hook(self) -> None:
        registry = HookRegistry()
        registry.register_builtin_hooks()
        payload = {
            "hook_calls": [
                {"hook": "filesystem_access", "args": {}},
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result = process_model_response_with_hooks(
                payload=payload,
                provider="openai",
                registry=registry,
                app_data_dir=Path(tmpdir),
                execute=True,
            )
        self.assertEqual(len(result["validated_hook_calls"]), 1)
        self.assertEqual(len(result["hook_results"]), 1)
        self.assertTrue(result["hook_results"][0]["success"])


if __name__ == "__main__":
    unittest.main()


