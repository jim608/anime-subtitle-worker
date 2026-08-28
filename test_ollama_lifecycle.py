from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main as main_module
from ollama_lifecycle import unload_managed_translation_models


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


class _OllamaOpener:
    def __init__(self):
        self.calls = []
        self.ps_payloads = [
            {
                "models": [
                    {"name": "unrelated:latest"},
                    {"name": "SakuraLLM:latest"},
                    {"model": "qwen2.5:7b-instruct-q4_K_M"},
                ]
            },
            {"models": [{"name": "unrelated:latest"}]},
        ]

    def __call__(self, request, *, timeout):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.calls.append((request.full_url, request.get_method(), body, timeout))
        if request.full_url.endswith("/api/ps"):
            return _Response(self.ps_payloads.pop(0))
        return _Response({"done": True})


class OllamaLifecycleTests(unittest.TestCase):
    def test_unloads_only_resident_configured_models_and_confirms_release(self):
        opener = _OllamaOpener()
        config = SimpleNamespace(
            translator_ollama_auto_unload_enabled=True,
            translator_ollama_unload_timeout_seconds=1,
            translator_base_url="http://ollama:11434/v1",
            translator_model="SakuraLLM",
            translator_fallback_models=["qwen2.5:7b-instruct-q4_K_M"],
        )

        released = unload_managed_translation_models(
            config,
            logging.getLogger("test"),
            opener=opener,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(
            released,
            ("SakuraLLM:latest", "qwen2.5:7b-instruct-q4_K_M"),
        )
        posts = [call for call in opener.calls if call[1] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(
            {call[2]["model"] for call in posts},
            {"SakuraLLM:latest", "qwen2.5:7b-instruct-q4_K_M"},
        )
        self.assertTrue(all(call[2]["keep_alive"] == 0 for call in posts))
        self.assertTrue(all(call[2]["stream"] is False for call in posts))
        self.assertTrue(all(call[0] == "http://ollama:11434/api/generate" for call in posts))

    def test_disabled_lifecycle_makes_no_network_request(self):
        opener = Mock(side_effect=AssertionError("network must not be called"))
        config = SimpleNamespace(
            translator_ollama_auto_unload_enabled=False,
            translator_base_url="http://ollama:11434/v1",
            translator_model="SakuraLLM",
            translator_fallback_models=[],
        )

        self.assertEqual(
            unload_managed_translation_models(config, logging.getLogger("test"), opener=opener),
            (),
        )
        opener.assert_not_called()

    def test_resource_admission_retries_after_managed_model_release(self):
        config = SimpleNamespace(resource_admission_enabled=True)
        blocked = {
            "admitted": False,
            "reason_codes": ["vram_primary_insufficient", "vram_no_model_route_fits"],
        }
        admitted = {"admitted": True, "reason_codes": []}
        logger = Mock()

        with patch(
            "resource_runtime.build_resource_launch_plan",
            side_effect=[blocked, admitted],
        ) as build, patch(
            "ollama_lifecycle.unload_managed_translation_models",
            return_value=("SakuraLLM:latest",),
        ) as unload:
            plan = main_module._resource_launch_plan_for_video(
                config,
                Path("episode.mkv"),
                logger,
            )

        self.assertIs(plan, admitted)
        self.assertEqual(build.call_count, 2)
        unload.assert_called_once_with(config, logger)


if __name__ == "__main__":
    unittest.main()
