from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from translator import (
    SubtitleTranslator, TranslationTimeoutError, TranslationRequestInFlightError,
    _TRANSLATION_REQUESTS, _TRANSLATION_REQUEST_LOCK,
)
from srt_utils import SrtBlock


class TranslationRequestOwnershipTest(unittest.TestCase):
    def test_model_unload_owns_endpoint_until_lifecycle_call_returns(self):
        calls = []
        instance = self.translator(lambda *args: calls.append(args) or "1\t完成")
        other = self.translator(lambda *args: calls.append(args) or "1\t完成")

        def unload(*args, **kwargs):
            with self.assertRaises(TranslationRequestInFlightError):
                other._request_translation_with_model_timeout("1\t原文", "system", "fallback")
            return ("primary",)

        with patch("translator.unload_managed_translation_models", side_effect=unload):
            self.assertEqual(instance.unload_requested_models(), ("primary",))
        self.assertEqual(calls, [])
        self.assertEqual(other._request_translation_with_model_timeout(
            "1\t原文", "system", "fallback"), "1\t完成")

    def test_pending_request_cannot_trigger_batch_split_or_context_retry(self):
        instance = self.translator(None)
        instance.config.translation_glossary = {}
        instance.config.translation_context_retry_without_context = True
        instance.config.translation_split_batch_on_timeout = True
        instance.config.translation_allow_source_fallback = False
        instance.config.max_retries = 3
        batch = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["原文"]),
                 SrtBlock(2, "00:00:02,000 --> 00:00:03,000", ["原文二"])]
        with patch.object(instance, "_request_translation",
                side_effect=TranslationRequestInFlightError("translation_request_in_flight")) as request:
            with self.assertRaises(TranslationRequestInFlightError):
                instance._translate_batch(batch, "1", "metadata context")
            request.assert_called_once()
        self.assertEqual([block.text for block in batch], [["原文"], ["原文二"]])

    def translator(self, request):
        instance = object.__new__(SubtitleTranslator)
        instance.config = SimpleNamespace(translator_base_url="http://ownership.invalid/" + self.id())
        instance.logger = logging.getLogger("test.translation.ownership")
        instance._progress_callback = None
        instance._translator_models = ("primary", "fallback")
        instance._translator_model_index = 0
        instance._translator_model = "primary"
        instance._request_translation_direct = request
        return instance

    def test_hard_timeout_does_not_launch_fallback_while_primary_still_runs(self):
        release = threading.Event()
        finished = threading.Event()
        calls = []

        def request(_source, _system, model):
            calls.append(model)
            if model == "primary":
                try:
                    release.wait(3)
                finally:
                    finished.set()
            return "1\t完成"

        instance = self.translator(request)
        try:
            with patch("translator._translation_request_hard_timeout_seconds", return_value=0.05):
                with self.assertRaises(TranslationTimeoutError):
                    instance._request_translation("1\t原文")
                self.assertEqual(calls, ["primary"])
        finally:
            release.set()
            self.assertTrue(finished.wait(2))

    def test_new_translator_cannot_overlap_a_timed_out_request_on_same_endpoint(self):
        release = threading.Event()
        calls = []
        finished = []

        def request(_source, _system, model):
            calls.append(model)
            release.wait(3)
            finished.append(model)
            return "1\t完成"

        first = self.translator(request)
        second = self.translator(request)
        try:
            with patch("translator._translation_request_hard_timeout_seconds", return_value=0.05):
                with self.assertRaises(TranslationTimeoutError):
                    first._request_translation_with_model_timeout("1\t原文", "system", "primary")
                with _TRANSLATION_REQUEST_LOCK:
                    owner = _TRANSLATION_REQUESTS[first.config.translator_base_url]
                with self.assertRaises(TranslationTimeoutError):
                    second._request_translation_with_model_timeout("1\t原文", "system", "fallback")
                self.assertEqual(calls, ["primary"])
                with patch("translator.unload_managed_translation_models") as unload:
                    self.assertEqual(second.unload_requested_models(), ())
                    unload.assert_not_called()
                release.set()
                owner.result(timeout=2)
                self.assertEqual(second._request_translation_with_model_timeout(
                    "1\t原文", "system", "fallback"), "1\t完成")
                self.assertEqual(calls, ["primary", "fallback"])
        finally:
            release.set()
            # All deliberately blocked test requests are released; no network,
            # production work or uncancellable fixture thread is left behind.


if __name__ == "__main__":
    unittest.main()
