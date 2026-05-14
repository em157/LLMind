"""Basic tests for response parameter extraction helpers."""

from __future__ import annotations

import json
import unittest

from response.response_handler import format_parameterized_response, parameterize_json_response
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


if __name__ == "__main__":
    unittest.main()
