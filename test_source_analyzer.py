from __future__ import annotations

from dataclasses import replace
import inspect
import hashlib
import math
import unittest

import source_analyzer
from source_analyzer import (
    ANALYZER_VERSION,
    ASR_JA_AUDIO,
    CONVERT_ZH_CN,
    DECISION_SCHEMA_VERSION,
    DECISION_VERSION,
    NEEDS_REVIEW,
    NORMALIZE_ZH_HANT,
    TRANSLATE_JA_SUBTITLE,
    UNSUPPORTED,
    USE_EXISTING_ZH_TW,
    AnalyzerThresholds,
    AudioCandidateInput,
    SubtitleCandidateInput,
    analyze_audio_candidate,
    analyze_sources,
    analyze_subtitle_candidate,
    canonical_json,
    decision_sha256,
    fingerprint_inputs,
    normalize_language_tag,
)


MEDIA_DURATION = 1_440.0
TRADITIONAL_TEXT = (
    "這裡是臺灣繁體中文字幕，歡迎大家一起觀看動畫。"
    "我們會繼續努力，讓每個人都能學習並選擇優質內容。"
)
SIMPLIFIED_TEXT = (
    "这里是台湾简体中文字幕，欢迎大家一起观看动画。"
    "我们会继续努力，让每个人都能学习并选择优质内容。"
)
JAPANESE_TEXT = (
    "これは日本語の会話字幕です。みんなで一緒にアニメを見ましょう。"
    "今日は楽しいですね。続きのお話も楽しみにしています。"
)


def subtitle(
    index: int,
    text: str,
    language: str,
    **overrides: object,
) -> SubtitleCandidateInput:
    values: dict[str, object] = {
        "track_index": index,
        "codec": "ass",
        "source_kind": "embedded",
        "source_reference": f"stream:{index}",
        "source_size": None,
        "source_mtime_ns": None,
        "source_sha256": "",
        "content_sha256": "",
        "container_language_tag": language,
        "title": "",
        "default": False,
        "forced": False,
        "hearing_impaired": None,
        "event_count": 240,
        "first_timestamp_seconds": 12.0,
        "last_timestamp_seconds": 1_425.0,
        "valid_timing_count": 240,
        "empty_event_count": 0,
        "sample_text": text,
        "extraction_error": "",
    }
    values.update(overrides)
    return SubtitleCandidateInput(**values)  # type: ignore[arg-type]


def audio(index: int, language: str, **overrides: object) -> AudioCandidateInput:
    values: dict[str, object] = {
        "track_index": index,
        "codec": "aac",
        "source_kind": "embedded",
        "source_reference": f"stream:{index}",
        "container_language_tag": language,
        "title": "Japanese" if language in {"ja", "jpn"} else "",
        "default": True,
        "commentary": False,
        "channels": 2,
        "sample_rate": 48_000,
        "duration_seconds": MEDIA_DURATION,
        "detected_language": "",
        "language_confidence": 0.0,
        "detection_source": "",
        "probing_error": "",
    }
    values.update(overrides)
    return AudioCandidateInput(**values)  # type: ignore[arg-type]


class SourceAnalyzerDecisionTests(unittest.TestCase):
    def decide(self, subtitles=(), audios=(), **kwargs):
        return analyze_sources(
            subtitles,
            audios,
            media_duration_seconds=MEDIA_DURATION,
            **kwargs,
        )

    def test_complete_zh_tw_uses_existing_subtitle(self) -> None:
        decision = self.decide([subtitle(3, TRADITIONAL_TEXT, "zh-TW", title="臺灣繁體")])
        self.assertEqual(decision.strategy, USE_EXISTING_ZH_TW)
        self.assertEqual(decision.selected_subtitle_track, 3)
        self.assertGreaterEqual(decision.confidence, 0.90)

    def test_complete_zh_hant_is_normalized(self) -> None:
        decision = self.decide([subtitle(4, TRADITIONAL_TEXT, "zh-Hant")])
        self.assertEqual(decision.strategy, NORMALIZE_ZH_HANT)
        self.assertEqual(decision.selected_subtitle_track, 4)

    def test_complete_zh_cn_is_converted(self) -> None:
        decision = self.decide([subtitle(5, SIMPLIFIED_TEXT, "zh-CN")])
        self.assertEqual(decision.strategy, CONVERT_ZH_CN)
        self.assertEqual(decision.selected_subtitle_track, 5)

    def test_complete_japanese_subtitle_is_translated(self) -> None:
        decision = self.decide([subtitle(6, JAPANESE_TEXT, "jpn")])
        self.assertEqual(decision.strategy, TRANSLATE_JA_SUBTITLE)
        self.assertEqual(decision.selected_subtitle_track, 6)

    def test_no_subtitle_with_japanese_audio_uses_asr_strategy(self) -> None:
        decision = self.decide([], [audio(1, "jpn")])
        self.assertEqual(decision.strategy, ASR_JA_AUDIO)
        self.assertEqual(decision.selected_audio_track, 1)
        self.assertFalse(decision.evidence["asr_invoked"])

    def test_zh_tw_precedes_japanese_subtitle(self) -> None:
        decision = self.decide(
            [subtitle(7, JAPANESE_TEXT, "ja", default=True), subtitle(8, TRADITIONAL_TEXT, "zh-TW")]
        )
        self.assertEqual(decision.strategy, USE_EXISTING_ZH_TW)
        self.assertEqual(decision.selected_subtitle_track, 8)

    def test_zh_cn_precedes_japanese_subtitle(self) -> None:
        decision = self.decide(
            [subtitle(9, JAPANESE_TEXT, "ja", default=True), subtitle(10, SIMPLIFIED_TEXT, "zh-CN")]
        )
        self.assertEqual(decision.strategy, CONVERT_ZH_CN)
        self.assertEqual(decision.selected_subtitle_track, 10)

    def test_forced_chinese_does_not_override_complete_japanese(self) -> None:
        decision = self.decide(
            [
                subtitle(11, TRADITIONAL_TEXT, "zh-TW", forced=True, default=True),
                subtitle(12, JAPANESE_TEXT, "ja"),
            ]
        )
        self.assertEqual(decision.strategy, TRANSLATE_JA_SUBTITLE)
        forced = next(item for item in decision.candidates if item.index == 11)
        self.assertIn("forced_track_risk", forced.rejection_reasons)

    def test_signs_only_is_not_complete_chinese(self) -> None:
        signs = subtitle(
            13,
            "出口　入口　營業中",
            "zh-TW",
            title="Signs & Songs",
            event_count=7,
            valid_timing_count=7,
            first_timestamp_seconds=300.0,
            last_timestamp_seconds=900.0,
        )
        decision = self.decide([signs], [audio(2, "ja")])
        self.assertEqual(decision.strategy, ASR_JA_AUDIO)
        analyzed = next(item for item in decision.candidates if item.kind == "subtitle")
        self.assertIn("signs_only_risk", analyzed.rejection_reasons)

    def test_songs_only_is_not_complete_dialogue(self) -> None:
        songs = subtitle(
            14,
            "♪ 歡迎來到這裡 ♪\n♪ 我們一起唱歌 ♪",
            "zh-TW",
            title="Karaoke Lyrics",
        )
        analyzed = analyze_subtitle_candidate(songs, media_duration_seconds=MEDIA_DURATION)
        self.assertFalse(analyzed.eligible)
        self.assertIn("songs_only_risk", analyzed.rejection_reasons)

    def test_default_flag_cannot_beat_materially_better_track(self) -> None:
        weak_default = subtitle(
            15,
            TRADITIONAL_TEXT,
            "zh-TW",
            default=True,
            event_count=25,
            valid_timing_count=25,
            first_timestamp_seconds=500.0,
            last_timestamp_seconds=1_400.0,
        )
        complete = subtitle(16, TRADITIONAL_TEXT, "zh-TW", default=False)
        decision = self.decide([weak_default, complete])
        self.assertEqual(decision.strategy, USE_EXISTING_ZH_TW)
        self.assertEqual(decision.selected_subtitle_track, 16)

    def test_metadata_chinese_but_japanese_content_detects_conflict(self) -> None:
        decision = self.decide([subtitle(17, JAPANESE_TEXT, "zh-CN")])
        self.assertEqual(decision.strategy, TRANSLATE_JA_SUBTITLE)
        analyzed = decision.candidates[0]
        self.assertEqual(analyzed.detected_language, "ja")
        self.assertTrue(analyzed.evidence["metadata_content_conflict"])

    def test_missing_metadata_uses_traditional_content(self) -> None:
        decision = self.decide([subtitle(18, TRADITIONAL_TEXT, "und")])
        self.assertEqual(decision.strategy, NORMALIZE_ZH_HANT)
        self.assertEqual(decision.candidates[0].detected_language, "zh-hant")

    def test_generic_chinese_tags_use_content_variant(self) -> None:
        traditional = self.decide([subtitle(19, TRADITIONAL_TEXT, "zho")])
        simplified = self.decide([subtitle(20, SIMPLIFIED_TEXT, "chi")])
        self.assertEqual(traditional.strategy, NORMALIZE_ZH_HANT)
        self.assertEqual(simplified.strategy, CONVERT_ZH_CN)

    def test_close_same_class_candidates_trigger_checks_and_review_if_tied(self) -> None:
        first = subtitle(21, TRADITIONAL_TEXT, "zh-TW")
        second = subtitle(22, TRADITIONAL_TEXT, "zh-TW")
        decision = self.decide([second, first])
        self.assertEqual(decision.strategy, NEEDS_REVIEW)
        checks = decision.evidence["additional_checks"]
        self.assertTrue(checks["required"])
        self.assertEqual(checks["result"], "insufficient")
        self.assertIsNone(decision.selected_subtitle_track)

    def test_close_different_priority_is_resolved_by_language_policy(self) -> None:
        decision = self.decide(
            [subtitle(24, JAPANESE_TEXT, "ja"), subtitle(23, TRADITIONAL_TEXT, "zh-TW")]
        )
        self.assertEqual(decision.strategy, USE_EXISTING_ZH_TW)
        self.assertTrue(decision.evidence["additional_checks"]["required"])

    def test_low_confidence_supported_candidate_needs_review(self) -> None:
        neutral_han = "天地玄黃宇宙洪荒日月盈昃辰宿列張寒來暑往"
        decision = self.decide([subtitle(25, neutral_han, "zho")])
        self.assertEqual(decision.strategy, NEEDS_REVIEW)
        self.assertLess(decision.confidence, 0.90)
        self.assertEqual(decision.reason_code, "candidate_analysis_inconclusive")

    def test_explicitly_unsupported_sources_are_unsupported(self) -> None:
        english_subtitle = subtitle(26, "This is a complete English dialogue subtitle.", "eng")
        english_audio = audio(3, "eng", title="English")
        decision = self.decide([english_subtitle], [english_audio])
        self.assertEqual(decision.strategy, UNSUPPORTED)
        self.assertEqual(decision.reason_code, "no_supported_subtitle_or_audio_source")

    def test_image_subtitle_extraction_failure_is_unsupported(self) -> None:
        image_subtitle = replace(
            subtitle(261, "", "zh-TW"),
            codec="hdmv_pgs_subtitle",
            extraction_error="unsupported text extraction",
        )
        decision = self.decide([image_subtitle])
        self.assertEqual(decision.strategy, UNSUPPORTED)
        self.assertIn(
            "unsupported_subtitle_codec",
            decision.candidates[0].rejection_reasons,
        )

    def test_empty_complete_inventory_is_unsupported(self) -> None:
        decision = self.decide()
        self.assertEqual(decision.strategy, UNSUPPORTED)

    def test_incomplete_inventory_fails_to_review(self) -> None:
        decision = self.decide(subtitle_inventory_complete=False)
        self.assertEqual(decision.strategy, NEEDS_REVIEW)
        self.assertEqual(decision.reason_code, "source_inventory_incomplete")

    def test_timing_and_empty_content_anomalies_are_rejected(self) -> None:
        damaged = subtitle(
            27,
            TRADITIONAL_TEXT,
            "zh-TW",
            valid_timing_count=50,
            empty_event_count=180,
            first_timestamp_seconds=900.0,
            last_timestamp_seconds=100.0,
        )
        analyzed = analyze_subtitle_candidate(damaged, media_duration_seconds=MEDIA_DURATION)
        self.assertFalse(analyzed.eligible)
        self.assertIn("invalid_timing_ratio", analyzed.rejection_reasons)
        self.assertIn("too_many_empty_events", analyzed.rejection_reasons)
        self.assertIn("insufficient_coverage", analyzed.rejection_reasons)

    def test_audio_ranking_rejects_commentary_and_prefers_main_track(self) -> None:
        commentary = audio(4, "ja", commentary=True, title="Japanese Commentary")
        main = audio(5, "ja", default=False, title="Japanese Main")
        decision = self.decide([], [commentary, main])
        self.assertEqual(decision.strategy, ASR_JA_AUDIO)
        self.assertEqual(decision.selected_audio_track, 5)
        rejected = next(item for item in decision.candidates if item.index == 4)
        self.assertIn("commentary_audio", rejected.rejection_reasons)

    def test_precomputed_audio_content_detection_can_override_wrong_metadata(self) -> None:
        analyzed = analyze_audio_candidate(
            audio(
                6,
                "eng",
                title="Main",
                detected_language="jpn",
                language_confidence=0.98,
                detection_source="deterministic-fixture-classifier-v1",
            ),
            media_duration_seconds=MEDIA_DURATION,
        )
        self.assertEqual(analyzed.detected_language, "ja")
        self.assertTrue(analyzed.evidence["metadata_content_conflict"])
        self.assertTrue(analyzed.eligible)

    def test_audio_title_alone_cannot_auto_accept_language(self) -> None:
        decision = self.decide([], [audio(61, "und", title="Japanese Main")])
        self.assertEqual(decision.strategy, NEEDS_REVIEW)
        self.assertEqual(decision.reason_code, "audio_selection_ambiguous")
        self.assertEqual(decision.evidence["additional_checks"]["result"], "insufficient")

    def test_missing_subtitle_metrics_fail_closed(self) -> None:
        candidate = subtitle(
            62,
            TRADITIONAL_TEXT,
            "zh-TW",
            valid_timing_count=None,
            empty_event_count=None,
        )
        analyzed = analyze_subtitle_candidate(candidate, media_duration_seconds=MEDIA_DURATION)
        self.assertFalse(analyzed.eligible)
        self.assertIn("timing_metrics_missing", analyzed.rejection_reasons)
        self.assertIn("empty_event_metrics_missing", analyzed.rejection_reasons)

    def test_configurable_event_threshold_changes_eligibility(self) -> None:
        short = subtitle(
            28,
            TRADITIONAL_TEXT,
            "zh-TW",
            event_count=10,
            valid_timing_count=10,
        )
        strict = analyze_subtitle_candidate(short, media_duration_seconds=MEDIA_DURATION)
        relaxed = analyze_subtitle_candidate(
            short,
            media_duration_seconds=MEDIA_DURATION,
            thresholds=AnalyzerThresholds(min_subtitle_events=5),
        )
        self.assertFalse(strict.eligible)
        self.assertTrue(relaxed.eligible)

    def test_decision_contract_preserves_full_candidate_evidence(self) -> None:
        decision = self.decide(
            [subtitle(29, TRADITIONAL_TEXT, "zh-TW"), subtitle(30, JAPANESE_TEXT, "ja")],
            [audio(7, "ja")],
        )
        payload = decision.to_dict()
        self.assertEqual(
            set(payload),
            {
                "strategy",
                "confidence",
                "reason_code",
                "evidence",
                "selected_subtitle_track",
                "selected_audio_track",
                "candidates",
                "unselected_reasons",
            },
        )
        self.assertEqual(len(payload["candidates"]), 3)
        for candidate in payload["candidates"]:
            self.assertTrue({"kind", "index", "score", "selected", "evidence"} <= set(candidate))
        self.assertEqual(sum(bool(item["selected"]) for item in payload["candidates"]), 1)
        self.assertIsInstance(payload["selected_subtitle_track"], dict)
        self.assertEqual(payload["selected_subtitle_track"]["index"], 29)
        self.assertEqual(payload["selected_subtitle_track"]["source_kind"], "embedded")
        self.assertEqual(payload["selected_subtitle_track"]["source_reference"], "stream:29")
        self.assertIsNone(payload["selected_audio_track"])
        unselected = {item["candidate"]: item["reasons"] for item in payload["unselected_reasons"]}
        self.assertIn("subtitle:30", unselected)
        self.assertIn("audio:7", unselected)

    def test_review_serialization_has_no_selected_candidate_mapping(self) -> None:
        decision = self.decide(
            [subtitle(63, TRADITIONAL_TEXT, "zh-TW"), subtitle(64, TRADITIONAL_TEXT, "zh-TW")]
        )
        payload = decision.to_dict()
        self.assertEqual(decision.strategy, NEEDS_REVIEW)
        self.assertIsNone(payload["selected_subtitle_track"])
        self.assertIsNone(payload["selected_audio_track"])
        self.assertIsInstance(payload["unselected_reasons"], list)

    def test_versions_are_present_in_canonical_evidence(self) -> None:
        decision = self.decide([subtitle(31, TRADITIONAL_TEXT, "zh-TW")])
        self.assertEqual(decision.evidence["analyzer_version"], ANALYZER_VERSION)
        self.assertEqual(decision.evidence["decision_schema_version"], DECISION_SCHEMA_VERSION)
        self.assertEqual(decision.evidence["decision_version"], DECISION_VERSION)

    def test_decision_and_candidate_order_are_deterministic(self) -> None:
        subtitles = [subtitle(32, JAPANESE_TEXT, "ja"), subtitle(33, TRADITIONAL_TEXT, "zh-TW")]
        audios = [audio(8, "eng"), audio(9, "ja")]
        first = self.decide(subtitles, audios)
        second = self.decide(reversed(subtitles), reversed(audios))
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(decision_sha256(first), decision_sha256(second))

    def test_inventory_fingerprint_is_canonical_and_detects_changes(self) -> None:
        subtitles = [subtitle(34, TRADITIONAL_TEXT, "zh-TW"), subtitle(35, JAPANESE_TEXT, "ja")]
        audios = [audio(10, "ja"), audio(11, "eng")]
        forward = fingerprint_inputs(subtitles, audios, media_duration_seconds=MEDIA_DURATION)
        reordered = fingerprint_inputs(
            reversed(subtitles), reversed(audios), media_duration_seconds=MEDIA_DURATION
        )
        changed = fingerprint_inputs(
            [subtitles[0], subtitle(35, JAPANESE_TEXT + "変更", "ja")],
            audios,
            media_duration_seconds=MEDIA_DURATION,
        )
        self.assertEqual(forward, reordered)
        self.assertNotEqual(forward, changed)
        self.assertRegex(forward, r"^[0-9a-f]{64}$")

        changed_reference = fingerprint_inputs(
            [
                subtitles[0],
                subtitle(
                    35,
                    JAPANESE_TEXT,
                    "ja",
                    source_kind="sidecar",
                    source_reference="relative/episode.ja.ass",
                ),
            ],
            audios,
            media_duration_seconds=MEDIA_DURATION,
        )
        self.assertNotEqual(forward, changed_reference)

    def test_duplicate_semantic_content_prefers_sidecar_without_false_tie(self) -> None:
        digest = hashlib.sha256(b"same-semantic-subtitle").hexdigest()
        source_digest = hashlib.sha256(b"sidecar-bytes").hexdigest()
        embedded = subtitle(
            65,
            TRADITIONAL_TEXT,
            "zh-TW",
            source_kind="embedded",
            source_reference="stream:65",
            content_sha256=digest,
            default=True,
        )
        sidecar = subtitle(
            -1,
            TRADITIONAL_TEXT,
            "zh-TW",
            source_kind="sidecar",
            source_reference="episode.zh-TW.ass",
            source_size=12_345,
            source_mtime_ns=1_725_000_000_123_456_789,
            source_sha256=source_digest.upper(),
            content_sha256=digest.upper(),
        )

        decision = self.decide([embedded, sidecar])

        self.assertEqual(decision.strategy, USE_EXISTING_ZH_TW)
        self.assertEqual(decision.selected_subtitle_track, -1)
        selected = decision.to_dict()["selected_subtitle_track"]
        self.assertEqual(selected["content_sha256"], digest)
        self.assertEqual(selected["source_kind"], "sidecar")
        self.assertEqual(selected["source_reference"], "episode.zh-TW.ass")
        self.assertEqual(selected["source_size"], 12_345)
        self.assertEqual(selected["source_mtime_ns"], 1_725_000_000_123_456_789)
        self.assertEqual(selected["source_sha256"], source_digest)
        duplicate = next(item for item in decision.candidates if item.index == 65)
        self.assertFalse(duplicate.eligible)
        self.assertIn("duplicate_subtitle_content", duplicate.rejection_reasons)
        self.assertFalse(decision.evidence["additional_checks"]["required"])

    def test_invalid_content_sha256_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "content_sha256"):
            subtitle(66, TRADITIONAL_TEXT, "zh-TW", content_sha256="not-a-digest")

    def test_invalid_sidecar_identity_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_sha256"):
            subtitle(67, TRADITIONAL_TEXT, "zh-TW", source_sha256="not-a-digest")
        with self.assertRaisesRegex(ValueError, "source_size"):
            SubtitleCandidateInput.from_mapping(
                {
                    **subtitle(68, TRADITIONAL_TEXT, "zh-TW").to_dict(),
                    "source_size": -1,
                }
            )
        with self.assertRaisesRegex(ValueError, "source_mtime_ns"):
            SubtitleCandidateInput.from_mapping(
                {
                    **subtitle(69, TRADITIONAL_TEXT, "zh-TW").to_dict(),
                    "source_mtime_ns": True,
                }
            )

    def test_mapping_inputs_are_supported_without_mutation(self) -> None:
        raw = subtitle(36, TRADITIONAL_TEXT, "zh-TW").to_dict()
        before = dict(raw)
        decision = self.decide([raw])
        self.assertEqual(decision.strategy, USE_EXISTING_ZH_TW)
        self.assertEqual(raw, before)

    def test_language_tag_aliases_are_normalized(self) -> None:
        expected = {
            "zh-TW": "zh-tw",
            "zh_Hant": "zh-hant",
            "cht": "zh-hant",
            "zh-CN": "zh-cn",
            "zh_Hans": "zh-hans",
            "chs": "zh-hans",
            "chi": "zh",
            "zho": "zh",
            "ja": "ja",
            "jpn": "ja",
            "unknown": "und",
            "": "und",
        }
        self.assertEqual({key: normalize_language_tag(key) for key in expected}, expected)

    def test_duplicate_track_indices_fail_deterministically(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate subtitle track index"):
            self.decide(
                [subtitle(37, TRADITIONAL_TEXT, "zh-TW"), subtitle(37, JAPANESE_TEXT, "ja")]
            )

    def test_threshold_validation_rejects_invalid_confidence_order(self) -> None:
        with self.assertRaises(ValueError):
            AnalyzerThresholds(auto_accept_confidence=0.50, review_confidence=0.60)

    def test_canonical_json_rejects_non_finite_values(self) -> None:
        with self.assertRaises(ValueError):
            canonical_json({"value": math.nan})

    def test_decision_module_has_no_model_or_transcriber_dependency(self) -> None:
        source = inspect.getsource(source_analyzer)
        self.assertNotIn("get_whisper_model", source)
        self.assertNotIn("from transcriber", source)
        decision = self.decide([], [audio(12, "ja")])
        self.assertFalse(decision.evidence["asr_invoked"])


if __name__ == "__main__":
    unittest.main()
