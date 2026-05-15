"""Basic tests for response parameter extraction helpers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from appdata.data_writer import DataWriter
from cache.cache_mgr import CacheManager
from main.LLMind import LLMindCLI
import network.requests as network_requests
from network.providers import (
    build_payload_from_user_input,
    build_request_headers,
    detect_provider,
    get_default_payload,
    get_response_template_name,
    inject_api_key_into_url,
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


class ProviderPayloadTests(unittest.TestCase):
    def test_openai_default_payload(self) -> None:
        payload = get_default_payload("openai")
        self.assertIn("model", payload)
        self.assertIn("input", payload)

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


if __name__ == "__main__":
    unittest.main()
