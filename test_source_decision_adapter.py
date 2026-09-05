from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audio import AudioStreamInfo, AudioStreamManifest
from source_analyzer import ASR_JA_AUDIO, USE_EXISTING_ZH_TW, analyze_sources
from source_decision import USE_ZH_TW
from source_decision_adapter import (
    SourceDecisionAdapterError,
    SourceDecisionReviewError,
    resolve_source_decision,
)
from source_inventory import build_source_input_identity, inventory_sources
from test_worker import _config


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_complete_ass(path: Path) -> None:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for index in range(24):
        start = index * 4
        end = start + 3
        lines.append(
            "Dialogue: 0,0:00:{:02d}.00,0:00:{:02d}.00,Default,,0,0,0,,"
            "這裡會選擇開啟網路連線並顯示完整繁體中文字幕{}".format(
                start,
                end,
                index,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _media_job_identity(video: Path, config: object) -> dict[str, object]:
    identity = build_source_input_identity(video, "job-test", config=config)
    return dict(identity.media_job_identity)


def _asr_decision(candidate_fingerprint: str, *, index: int = 4) -> dict[str, object]:
    return {
        "strategy": ASR_JA_AUDIO,
        "candidate_fingerprint": candidate_fingerprint,
        "selected_subtitle_track": None,
        "selected_audio_track": {
            "kind": "audio",
            "index": index,
            "detected_language": "ja",
            "normalized_language_tag": "ja",
            "container_language_tag": "jpn",
            "title": "Japanese Main",
            "default": True,
            "commentary": False,
            "codec": "aac",
            "channels": 2,
            "source_kind": "embedded",
            "source_reference": f"stream:{index}",
        },
    }


class SourceDecisionAdapterTest(unittest.TestCase):
    def test_materialized_content_rejections_have_typed_review_without_source_changes(self) -> None:
        cases = (
            ("unknown language", "unknown", False, 24, "language conflicts"),
            ("wrong language", "ja", False, 24, "language conflicts"),
            ("structural failure", "zh-tw", True, 24, "structural QC"),
            ("no dialogue", "zh-tw", False, 0, "structural QC"),
        )
        for name, language, has_failures, dialogues, message in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                video = root / "episode.mkv"
                video.write_bytes(b"immutable-media")
                sidecar = root / "episode.zh-TW.ass"
                _write_complete_ass(sidecar)
                before = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in (video, sidecar)
                }
                config = _config(root)
                job = _media_job_identity(video, config)
                with patch(
                    "source_inventory._probe_media",
                    return_value={"format": {"duration": "100"}, "streams": []},
                ):
                    inventory = inventory_sources(video, job, config=config, sidecar_paths=[sidecar])
                payload = analyze_sources(**inventory.analyzer_arguments()).to_dict()
                payload["candidate_fingerprint"] = inventory.candidate_fingerprint
                with (
                    patch(
                        "source_decision_adapter.classify_subtitle_content_file",
                        return_value=SimpleNamespace(language=language),
                    ),
                    patch(
                        "source_decision_adapter.analyze_subtitle_file",
                        return_value=SimpleNamespace(has_failures=has_failures, dialogues=dialogues),
                    ) as quality,
                    self.assertRaisesRegex(SourceDecisionReviewError, message),
                ):
                    resolve_source_decision(video, {"decision": payload}, job, config)
                if language != "zh-tw":
                    quality.assert_not_called()
                else:
                    quality.assert_called_once_with(sidecar, config, role="unknown")
                self.assertEqual(
                    before,
                    {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before},
                )

    def test_sidecar_inventory_decision_resolves_without_modifying_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"immutable-media")
            sidecar = root / "episode.zh-TW.ass"
            _write_complete_ass(sidecar)
            video_before = (video.read_bytes(), video.stat().st_size, video.stat().st_mtime_ns)
            sidecar_before = (
                sidecar.read_bytes(),
                sidecar.stat().st_size,
                sidecar.stat().st_mtime_ns,
            )
            config = _config(root)
            job = _media_job_identity(video, config)
            probe = {"format": {"duration": "100"}, "streams": []}
            with patch("source_inventory._probe_media", return_value=probe):
                inventory = inventory_sources(
                    video,
                    job,
                    config=config,
                    sidecar_paths=[sidecar],
                )
            formal = analyze_sources(**inventory.analyzer_arguments())
            self.assertEqual(USE_EXISTING_ZH_TW, formal.strategy)
            payload = formal.to_dict()
            payload["candidate_fingerprint"] = inventory.candidate_fingerprint

            resolved = resolve_source_decision(
                video,
                {"decision_id": "decision-test", "decision": payload},
                job,
                config,
            )
            self.assertIsNotNone(resolved.subtitle)
            assert resolved.subtitle is not None
            self.assertEqual(USE_ZH_TW, resolved.subtitle.strategy)
            self.assertEqual(sidecar, resolved.subtitle.source_path)
            self.assertEqual(
                video_before,
                (video.read_bytes(), video.stat().st_size, video.stat().st_mtime_ns),
            )
            self.assertEqual(
                sidecar_before,
                (sidecar.read_bytes(), sidecar.stat().st_size, sidecar.stat().st_mtime_ns),
            )

    def test_asr_strategy_routes_the_exact_non_commentary_audio_track(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"immutable-media")
            config = _config(root)
            job = _media_job_identity(video, config)
            identity = build_source_input_identity(video, job, config=config)
            decision = _asr_decision(identity.candidate_fingerprint)
            before = (video.read_bytes(), video.stat().st_size, video.stat().st_mtime_ns)
            current = AudioStreamInfo(
                index=4,
                language="jpn",
                title="Japanese Main",
                default=True,
                commentary=False,
                codec_name="aac",
                channels=2,
            )
            with patch(
                "source_decision_adapter.probe_audio_stream_manifest",
                return_value=AudioStreamManifest((current,), True),
            ) as probe:
                resolved = resolve_source_decision(
                    video,
                    {"decision_id": "audio-decision", "decision": decision},
                    job,
                    config,
                )
            self.assertIsNone(resolved.subtitle)
            self.assertIsNotNone(resolved.audio)
            assert resolved.audio is not None
            self.assertEqual(4, resolved.audio.index)
            self.assertEqual("ja", resolved.audio.language)
            self.assertEqual("Japanese Main", resolved.audio.title)
            self.assertEqual("aac", resolved.audio.codec_name)
            self.assertEqual(2, resolved.audio.channels)
            probe.assert_called_once_with(video)
            self.assertEqual(
                before,
                (video.read_bytes(), video.stat().st_size, video.stat().st_mtime_ns),
            )

    def test_commentary_audio_is_rejected_even_if_record_is_tampered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"immutable-media")
            config = _config(root)
            job = _media_job_identity(video, config)
            identity = build_source_input_identity(video, job, config=config)
            decision = _asr_decision(identity.candidate_fingerprint, index=7)
            selected = dict(decision["selected_audio_track"])
            selected["commentary"] = True
            decision["selected_audio_track"] = selected
            with patch("source_decision_adapter.probe_audio_stream_manifest") as probe:
                with self.assertRaises(SourceDecisionAdapterError):
                    resolve_source_decision(
                        video,
                        {"decision": decision},
                        job,
                        config,
                    )
            probe.assert_not_called()

    def test_executable_decision_rejects_changed_or_missing_source_before_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"immutable-media")
            config = _config(root)
            job = _media_job_identity(video, config)
            identity = build_source_input_identity(video, job, config=config)

            with self.subTest(case="candidate fingerprint changed"):
                decision = _asr_decision(_sha256("different-candidate-set"))
                with patch("source_decision_adapter.probe_audio_stream_manifest") as probe:
                    with self.assertRaisesRegex(
                        SourceDecisionAdapterError,
                        "candidate fingerprint",
                    ):
                        resolve_source_decision(video, {"decision": decision}, job, config)
                probe.assert_not_called()

            with self.subTest(case="source removed"):
                decision = _asr_decision(identity.candidate_fingerprint)
                video.unlink()
                with patch("source_decision_adapter.probe_audio_stream_manifest") as probe:
                    with self.assertRaisesRegex(
                        SourceDecisionAdapterError,
                        "cannot verify",
                    ):
                        resolve_source_decision(video, {"decision": decision}, job, config)
                probe.assert_not_called()

    def test_asr_ffprobe_revalidation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"immutable-media")
            config = _config(root)
            job = _media_job_identity(video, config)
            identity = build_source_input_identity(video, job, config=config)
            decision = _asr_decision(identity.candidate_fingerprint)
            cases = (
                (
                    "probe incomplete",
                    AudioStreamManifest((), False, "ffprobe_timeout"),
                    "inventory is incomplete",
                ),
                (
                    "exact index missing",
                    AudioStreamManifest(
                        (AudioStreamInfo(5, "jpn", "Japanese", True, False, "aac", 2),),
                        True,
                    ),
                    "index is missing or ambiguous",
                ),
                (
                    "current commentary",
                    AudioStreamManifest(
                        (AudioStreamInfo(4, "jpn", "Commentary", False, True, "aac", 2),),
                        True,
                    ),
                    "commentary",
                ),
                (
                    "current non-Japanese",
                    AudioStreamManifest(
                        (AudioStreamInfo(4, "eng", "English", True, False, "aac", 2),),
                        True,
                    ),
                    "no Japanese",
                ),
            )
            for label, manifest, message in cases:
                with self.subTest(case=label):
                    with patch(
                        "source_decision_adapter.probe_audio_stream_manifest",
                        return_value=manifest,
                    ):
                        with self.assertRaisesRegex(SourceDecisionAdapterError, message):
                            resolve_source_decision(
                                video,
                                {"decision": decision},
                                job,
                                config,
                            )

    def test_asr_requires_persisted_sha256_candidate_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"immutable-media")
            config = _config(root)
            job = _media_job_identity(video, config)
            for value in (None, "", "not-a-digest"):
                with self.subTest(value=value):
                    decision = _asr_decision(_sha256("placeholder"))
                    if value is None:
                        decision.pop("candidate_fingerprint")
                    else:
                        decision["candidate_fingerprint"] = value
                    with patch("source_decision_adapter.probe_audio_stream_manifest") as probe:
                        with self.assertRaisesRegex(
                            SourceDecisionAdapterError,
                            "candidate_fingerprint",
                        ):
                            resolve_source_decision(video, {"decision": decision}, job, config)
                    probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
