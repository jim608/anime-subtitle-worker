from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import json
import subprocess
import tempfile
import unittest

from subtitle_extract import (
    SubtitleExtractCancelled,
    SubtitleExtractError,
    _classify_subtitle_content_detail,
    _classify_subtitle_language,
    _extract_subtitle_stream,
    _probe_subtitle_streams,
    _publish_official_subtitle_set,
    _read_subtitle_sample,
    _SubtitleCandidate,
    _validated_import_candidates,
    _subtitle_text_for_classification,
    classify_sidecar_subtitle,
    classify_sidecar_subtitle_language,
    extract_available_subtitles,
    normalize_sidecar_subtitles,
    normalize_sidecar_subtitles_for_output,
    verified_official_subtitle_languages,
    remove_ai_subtitle_outputs,
    remove_ai_srt_outputs,
)
from safe_files import verified_copy_replace as real_verified_copy_replace
from subtitle_paths import paths_for_video


class SubtitleExtractTest(unittest.TestCase):
    def test_malformed_srt_candidate_does_not_hide_valid_sibling_or_modify_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "Episode.mkv"
            video.write_bytes(b"unchanged-video")
            malformed = root / "Episode.bad.zh-TW.srt"
            malformed.write_text(_malformed_guard_srt(), encoding="utf-8")
            valid = root / "Episode.good.zh-TW.ass"
            valid.write_text(_guard_ass(), encoding="utf-8")
            before = {path: path.read_bytes() for path in (video, malformed, valid)}
            candidates = [
                _SubtitleCandidate(path, "zh-tw", -1, classify_sidecar_subtitle(path), (0, 0), path.stem)
                for path in (malformed, valid)
            ]
            diagnostics = []
            with patch("source_inventory._probe_media", return_value={"format": {"duration": "1000"}}):
                accepted = _validated_import_candidates(candidates, video, SimpleNamespace(), diagnostics, None)
                self.assertEqual([item.source_path for item in accepted], [valid])
                self.assertEqual(verified_official_subtitle_languages(video, SimpleNamespace()), {"zh-tw"})
            self.assertEqual(diagnostics[0]["output_parse"], "FAIL")
            self.assertEqual(diagnostics[0]["hard_qc"], "FAIL")
            self.assertIsNone(diagnostics[0]["hard_qc_report"])
            self.assertIn("SrtFormatError", diagnostics[0]["hard_qc_error"])
            self.assertIn("invalid_subtitle_parse", diagnostics[0]["detail"])
            self.assertEqual(diagnostics[1]["hard_qc"], "PASS")
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_publisher_malformed_srt_rejects_before_prior_output_or_receipt_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "Episode.mkv"
            video.write_bytes(b"unchanged-video")
            staged = root / "staged.srt"
            staged.write_text(_malformed_guard_srt(), encoding="utf-8")
            output = root / "Episode.zh-TW.ass"
            output.write_text(_guard_ass(), encoding="utf-8")
            before = {path: path.read_bytes() for path in (video, staged, output)}
            config = SimpleNamespace(work_path=root / "work")
            for _ in range(2):
                with self.assertRaisesRegex(SubtitleExtractError, "staged parse failed"):
                    _publish_official_subtitle_set(video, [(staged, output, "zh-tw")], config)
                self.assertFalse(config.work_path.exists())
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_unsupported_raw_qc_format_is_not_mislabeled_bad_content_or_hides_valid_candidate(self) -> None:
        for suffix in (".ssa", ".vtt"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                video = root / "Episode.mkv"
                video.write_bytes(b"unchanged-video")
                unsupported = root / f"Episode.zh-TW{suffix}"
                unsupported.write_text(_guard_ass() if suffix == ".ssa" else _guard_vtt(), encoding="utf-8")
                valid = root / "Episode.zh-TW.ass"
                valid.write_text(_guard_ass(), encoding="utf-8")
                candidates = [
                    _SubtitleCandidate(path, "zh-tw", -1, classify_sidecar_subtitle(path), (0, 0), path.stem)
                    for path in (unsupported, valid)
                ]
                before = unsupported.read_bytes()
                diagnostics = []
                with patch("source_inventory._probe_media", return_value={"format": {"duration": "1000"}}):
                    accepted = _validated_import_candidates(candidates, video, SimpleNamespace(), diagnostics, None)
                self.assertEqual([item.source_path for item in accepted], [valid])
                self.assertEqual(diagnostics[0]["output_parse"], "PASS")
                self.assertEqual(diagnostics[0]["hard_qc"], "NOT_EVALUATED")
                self.assertEqual(diagnostics[0]["hard_qc_error"], "unsupported_validation")
                self.assertNotIn("hard_qc_failed", diagnostics[0]["detail"])
                config = SimpleNamespace(work_path=root / "work")
                valid_before = valid.read_bytes()
                with self.assertRaisesRegex(SubtitleExtractError, "staged validation unsupported"):
                    _publish_official_subtitle_set(video, [(unsupported, valid, "zh-tw")], config)
                self.assertEqual(unsupported.read_bytes(), before)
                self.assertEqual(valid.read_bytes(), valid_before)
                self.assertFalse(config.work_path.exists())

    def test_import_coverage_pass_cannot_override_existing_hard_qc_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "Episode.mkv"
            video.write_bytes(b"unchanged-video")
            sidecar = root / "Episode.zh-TW.ass"
            sidecar.write_text(_guard_ass(overlap=True), encoding="utf-8")
            before = sidecar.read_bytes()
            classification = classify_sidecar_subtitle(sidecar)
            candidate = _SubtitleCandidate(sidecar, "zh-tw", -1, classification, (0, 0), sidecar.stem)
            diagnostics = []
            with patch("source_inventory._probe_media", return_value={"format": {"duration": "1000"}}):
                self.assertEqual(_validated_import_candidates([candidate], video, SimpleNamespace(), diagnostics, None), [])
            self.assertEqual(diagnostics[0]["output_parse"], "PASS")
            self.assertTrue(diagnostics[0]["source_analysis"]["eligible"])
            self.assertEqual(diagnostics[0]["hard_qc"], "FAIL")
            self.assertIn("hard_qc_failed", diagnostics[0]["detail"])
            self.assertEqual(sidecar.read_bytes(), before)
            self.assertEqual(video.read_bytes(), b"unchanged-video")

    def test_publisher_staged_hard_qc_rejects_before_any_output_or_receipt_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "Episode.mkv"
            video.write_bytes(b"unchanged-video")
            staged = root / "staged.ass"
            staged.write_text(_guard_ass(overlap=True), encoding="utf-8")
            output = root / "Episode.zh-TW.ass"
            output.write_text(_guard_ass(), encoding="utf-8")
            before = output.read_bytes()
            source_before = staged.read_bytes()
            config = SimpleNamespace(work_path=root / "work")
            for _restart in range(2):
                with self.assertRaisesRegex(SubtitleExtractError, "staged hard QC failed.*timing_overlap"):
                    _publish_official_subtitle_set(video, [(staged, output, "zh-tw")], config)
                self.assertEqual(output.read_bytes(), before)
                self.assertEqual(staged.read_bytes(), source_before)
                self.assertFalse(config.work_path.exists())
            self.assertEqual(video.read_bytes(), b"unchanged-video")

    def test_publisher_hard_qc_passing_output_still_replays_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "Episode.mkv"
            video.write_bytes(b"unchanged-video")
            staged = root / "staged.ass"
            staged.write_text(_guard_ass(), encoding="utf-8")
            output = root / "Episode.zh-TW.ass"
            config = SimpleNamespace(work_path=root / "work")
            _publish_official_subtitle_set(video, [(staged, output, "zh-tw")], config)
            first = output.read_bytes()
            _publish_official_subtitle_set(video, [(staged, output, "zh-tw")], config)
            self.assertEqual(output.read_bytes(), first)
            self.assertEqual(first, staged.read_bytes())
            self.assertEqual(len(list(config.work_path.rglob("manifest.json"))), 1)
            self.assertEqual(video.read_bytes(), b"unchanged-video")

    def test_official_subtitle_set_publish_failure_restores_every_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work = root / "work"
            work.mkdir()
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            source_cn = root / "staged-cn.ass"
            source_tw = root / "staged-tw.ass"
            output_cn = root / "Anime S01E01.zh.ass"
            output_tw = root / "Anime S01E01.zh-TW.ass"
            source_cn.write_text(
                "Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,明天选班长",
                encoding="utf-8",
            )
            source_tw.write_text(
                "Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,明天選班長",
                encoding="utf-8",
            )
            output_cn.write_text("old-cn", encoding="utf-8")
            output_tw.write_text("old-tw", encoding="utf-8")
            config = SimpleNamespace(work_path=work, official_subtitle_versions_keep=3)

            def fail_second_publish(source: Path, destination: Path) -> Path:
                if Path(source) == source_tw and Path(destination) == output_tw:
                    real_verified_copy_replace(source, destination)
                    raise OSError("injected second official output failure")
                return real_verified_copy_replace(source, destination)

            with patch("subtitle_extract.verified_copy_replace", side_effect=fail_second_publish):
                with self.assertRaisesRegex(OSError, "injected second official output failure"):
                    _publish_official_subtitle_set(
                        video,
                        [
                            (source_cn, output_cn, "zh-cn"),
                            (source_tw, output_tw, "zh-tw"),
                        ],
                        config,
                    )

            self.assertEqual(output_cn.read_text(encoding="utf-8"), "old-cn")
            self.assertEqual(output_tw.read_text(encoding="utf-8"), "old-tw")
            manifests = list((work / "official_subtitle_versions").rglob("manifest.json"))
            self.assertEqual(len(manifests), 1)
            self.assertEqual(json.loads(manifests[0].read_text(encoding="utf-8"))["status"], "rolled_back")

    def test_sidecar_conversion_failure_never_touches_existing_official_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            sidecar = root / "Anime S01E01.zh-hant.srt"
            sidecar.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n明天選班長\n",
                encoding="utf-8",
            )
            output = root / "Anime S01E01.zh-TW.ass"
            output.write_text("old-official", encoding="utf-8")
            config = SimpleNamespace(
                work_path=root / "work",
                ai_japanese_ass_suffix=".AI.ja.ass",
                ai_simplified_chinese_ass_suffix=".AI.zh.ass",
                ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
                mikan_remove_ai_after_extract=False,
            )

            def fail_conversion(_source: Path, staged: Path) -> None:
                staged.write_text("partial", encoding="utf-8")
                raise SubtitleExtractError("injected sidecar conversion failure")

            with patch("subtitle_extract._copy_or_convert_sidecar", side_effect=fail_conversion):
                with self.assertRaisesRegex(SubtitleExtractError, "injected sidecar conversion failure"):
                    normalize_sidecar_subtitles(video, config)

            self.assertEqual(output.read_text(encoding="utf-8"), "old-official")

    def test_official_subtitle_versions_are_bounded_but_incomplete_evidence_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work = root / "work"
            work.mkdir()
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            source = root / "staged.ass"
            output = root / "Anime S01E01.zh-TW.ass"
            config = SimpleNamespace(work_path=work, official_subtitle_versions_keep=2)

            for index in range(5):
                source.write_text(
                    f"Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,明天選班長{index}",
                    encoding="utf-8",
                )
                _publish_official_subtitle_set(video, [(source, output, "zh-tw")], config)

            digest = hashlib.sha1(
                str(video.resolve()).encode("utf-8", errors="replace")
            ).hexdigest()[:16]
            root_versions = work / "official_subtitle_versions" / digest
            prepared = root_versions / "999999999999999999999999999999"
            prepared.mkdir()
            (prepared / "manifest.json").write_text(
                json.dumps({"status": "prepared"}),
                encoding="utf-8",
            )
            source.write_text(
                "Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,明天選班長最後",
                encoding="utf-8",
            )
            _publish_official_subtitle_set(video, [(source, output, "zh-tw")], config)

            statuses = [
                json.loads((path / "manifest.json").read_text(encoding="utf-8"))["status"]
                for path in root_versions.iterdir()
                if path.is_dir()
            ]
            self.assertEqual(statuses.count("completed"), 2)
            self.assertEqual(statuses.count("prepared"), 1)

    def test_subtitle_sample_uses_bounded_stream_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle = Path(temp_dir) / "bounded.ass"
            subtitle.write_bytes(b"x" * 500_000)

            with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read is forbidden")):
                sample = _read_subtitle_sample(subtitle, max_chars=100)

            self.assertEqual(sample, "x" * 100)

    def test_remove_ai_subtitle_outputs_keeps_human_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                ai_japanese_ass_suffix=".AI日本語.ja.ass",
                ai_simplified_chinese_ass_suffix=".AI简日双语.zh.ass",
                ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
                work_path=Path(temp_dir) / "work",
                ai_output_versions_keep=3,
            )
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            ai_sub = Path(temp_dir) / "Anime S01E01.AI繁日雙語.zh-TW.ass"
            human_sub = Path(temp_dir) / "Anime S01E01.繁體中文.zh-TW.ass"
            ai_sub.write_text("ai", encoding="utf-8")
            human_sub.write_text("human", encoding="utf-8")

            removed = remove_ai_subtitle_outputs(video, config)

            self.assertEqual(removed, [ai_sub])
            self.assertFalse(ai_sub.exists())
            self.assertTrue(human_sub.exists())
            manifests = list((config.work_path / "retired_ai_outputs").rglob("manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "completed")
            archived = Path(manifest["files"][0]["archive"])
            self.assertEqual(archived.read_text(encoding="utf-8"), "ai")
            self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(b"ai").hexdigest())

    def test_ai_subtitle_archive_failure_preserves_every_media_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                ai_japanese_ass_suffix=".AI.ja.ass",
                ai_simplified_chinese_ass_suffix=".AI.zh.ass",
                ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
                work_path=root / "work",
                ai_output_versions_keep=3,
            )
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            japanese = root / "Anime S01E01.AI.ja.ass"
            translated = root / "Anime S01E01.AI.zh.ass"
            japanese.write_text("old-ja", encoding="utf-8")
            translated.write_text("old-zh", encoding="utf-8")

            with patch("subtitle_extract.verified_copy_replace", side_effect=OSError("archive offline")):
                with self.assertRaisesRegex(SubtitleExtractError, "original outputs were preserved"):
                    remove_ai_subtitle_outputs(video, config)

            self.assertEqual(japanese.read_text(encoding="utf-8"), "old-ja")
            self.assertEqual(translated.read_text(encoding="utf-8"), "old-zh")

    def test_ai_subtitle_retirement_failure_rolls_back_already_removed_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                ai_japanese_ass_suffix=".AI.ja.ass",
                ai_simplified_chinese_ass_suffix=".AI.zh.ass",
                ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
                work_path=root / "work",
                ai_output_versions_keep=3,
            )
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            japanese = root / "Anime S01E01.AI.ja.ass"
            translated = root / "Anime S01E01.AI.zh.ass"
            japanese.write_text("old-ja", encoding="utf-8")
            translated.write_text("old-zh", encoding="utf-8")

            def fail_second_retirement(path: Path) -> None:
                if path == translated:
                    raise OSError("second unlink failed")
                path.unlink()

            with patch("subtitle_extract._unlink_retired_ai_output", side_effect=fail_second_retirement):
                with self.assertRaisesRegex(SubtitleExtractError, "original outputs were preserved"):
                    remove_ai_subtitle_outputs(video, config)

            self.assertEqual(japanese.read_text(encoding="utf-8"), "old-ja")
            self.assertEqual(translated.read_text(encoding="utf-8"), "old-zh")
            manifests = list((config.work_path / "retired_ai_outputs").rglob("manifest.json"))
            self.assertEqual(len(manifests), 1)
            self.assertEqual(
                json.loads(manifests[0].read_text(encoding="utf-8"))["status"],
                "rolled_back",
            )

    def test_classifies_chi_sidecar_by_simplified_or_traditional_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            simplified = Path(temp_dir) / "Anime S01E01.stream_15.chi.ass"
            traditional = Path(temp_dir) / "Anime S01E01.stream_16.chi.ass"
            simplified.write_text("Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,\u660e\u5929\u9009\u73ed\u957f", encoding="utf-8")
            traditional.write_text("Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,\u660e\u5929\u9078\u73ed\u9577", encoding="utf-8")

            self.assertEqual(classify_sidecar_subtitle_language(simplified), "zh-cn")
            self.assertEqual(classify_sidecar_subtitle_language(traditional), "zh-tw")

    def test_ffprobe_timeout_reports_extract_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")

            with patch("subtitle_extract.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 7)):
                with self.assertRaisesRegex(Exception, "ffprobe timed out after 7s"):
                    _probe_subtitle_streams(video, timeout_seconds=7)

    def test_extract_subtitle_stream_prefers_mkvextract_for_matroska(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            output = Path(temp_dir) / "Anime S01E01.zh.ass"
            video.write_text("video", encoding="utf-8")

            def fake_run(command, **_kwargs):
                if "mkvextract" in str(command[0]):
                    Path(str(command[-1]).split(":", 1)[1]).write_text(
                        "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,中文",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 1, "", "ffmpeg boom")

            with (
                patch("subtitle_extract._resolve_ffmpeg", return_value="ffmpeg"),
                patch("subtitle_extract.shutil.which", side_effect=lambda name: "mkvextract" if name == "mkvextract" else None),
                patch("subtitle_extract.subprocess.run", side_effect=fake_run) as run,
            ):
                _extract_subtitle_stream(video, 5, output, "ass", timeout_seconds=5)

            self.assertTrue(output.exists())
            self.assertIn("中文", output.read_text(encoding="utf-8"))
            self.assertEqual(run.call_count, 1)

    def test_extract_subtitle_stream_falls_back_to_ffmpeg_without_mkvextract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            output = Path(temp_dir) / "Anime S01E01.zh.ass"
            video.write_text("video", encoding="utf-8")

            def fake_run(command, **_kwargs):
                output.write_text(
                    "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,中文",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch("subtitle_extract._resolve_ffmpeg", return_value="ffmpeg"),
                patch("subtitle_extract.shutil.which", return_value=None),
                patch("subtitle_extract.subprocess.run", side_effect=fake_run) as run,
            ):
                _extract_subtitle_stream(video, 5, output, "ass", timeout_seconds=5)

            self.assertTrue(output.exists())
            self.assertEqual(run.call_count, 1)

    def test_mkvextract_fallback_uses_mkvmerge_track_id_when_stream_index_differs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            output = Path(temp_dir) / "Anime S01E01.zh.ass"
            video.write_text("video", encoding="utf-8")
            mkvextract_specs: list[str] = []

            def fake_run(command, **_kwargs):
                executable = str(command[0])
                if executable == "ffmpeg":
                    return subprocess.CompletedProcess(command, 1, "", "ffmpeg boom")
                if executable == "mkvmerge":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "tracks": [
                                    {"id": 0, "type": "video"},
                                    {"id": 1, "type": "audio"},
                                    {"id": 2, "type": "subtitles", "properties": {"number": 5}},
                                ]
                            }
                        ),
                        "",
                    )
                if executable == "mkvextract":
                    spec = str(command[-1])
                    mkvextract_specs.append(spec)
                    if spec.startswith("2:"):
                        Path(spec.split(":", 1)[1]).write_text(
                            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,繁體中文字幕",
                            encoding="utf-8",
                        )
                        return subprocess.CompletedProcess(command, 0, "", "")
                    return subprocess.CompletedProcess(command, 1, "", "bad track")
                return subprocess.CompletedProcess(command, 1, "", "unexpected")

            with (
                patch("subtitle_extract._resolve_ffmpeg", return_value="ffmpeg"),
                patch("subtitle_extract.shutil.which", side_effect=lambda name: name if name in {"mkvextract", "mkvmerge"} else None),
                patch("subtitle_extract.subprocess.run", side_effect=fake_run),
            ):
                _extract_subtitle_stream(video, 4, output, "ass", timeout_seconds=5, subtitle_ordinal=0)

            self.assertTrue(output.exists())
            self.assertEqual(mkvextract_specs, [f"2:{output}"])

    def test_extract_deadline_is_shared_across_mkvmerge_mkvextract_and_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            output = Path(temp_dir) / "Anime S01E01.zh.ass"
            video.write_text("video", encoding="utf-8")
            clock = [100.0]
            commands: list[str] = []
            timeouts: list[float] = []

            def fake_run(command, **kwargs):
                executable = str(command[0])
                command_timeout = float(kwargs["timeout"])
                commands.append(executable)
                timeouts.append(command_timeout)
                if executable == "mkvmerge":
                    clock[0] += 1.0
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "tracks": [
                                    {
                                        "id": 2,
                                        "type": "subtitles",
                                        "properties": {"number": 5},
                                    }
                                ]
                            }
                        ),
                        "",
                    )
                if executable == "mkvextract":
                    clock[0] += 1.0
                    return subprocess.CompletedProcess(command, 1, "", "bad track")
                if executable == "ffmpeg":
                    clock[0] += command_timeout
                    raise subprocess.TimeoutExpired(command, command_timeout)
                raise AssertionError(f"unexpected command: {command}")

            with (
                patch("subtitle_extract._resolve_ffmpeg", return_value="ffmpeg"),
                patch(
                    "subtitle_extract.shutil.which",
                    side_effect=lambda name: name if name in {"mkvextract", "mkvmerge"} else None,
                ),
                patch("subtitle_extract.time.monotonic", side_effect=lambda: clock[0]),
                patch("subtitle_extract.subprocess.run", side_effect=fake_run),
            ):
                with self.assertRaises(SubtitleExtractCancelled):
                    _extract_subtitle_stream(
                        video,
                        4,
                        output,
                        "ass",
                        timeout_seconds=300,
                        subtitle_ordinal=0,
                        deadline_monotonic=106.0,
                    )

            self.assertEqual(commands, ["mkvmerge", "mkvextract", "mkvextract", "ffmpeg"])
            self.assertEqual(timeouts, [6.0, 5.0, 4.0, 3.0])
            self.assertEqual(clock[0], 106.0)

    def test_mkvextract_sidecar_conversion_uses_remaining_absolute_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            output = Path(temp_dir) / "Anime S01E01.zh.ass"
            video.write_text("video", encoding="utf-8")
            clock = [100.0]
            commands: list[str] = []
            timeouts: list[float] = []

            def fake_run(command, **kwargs):
                executable = str(command[0])
                command_timeout = float(kwargs["timeout"])
                commands.append(executable)
                timeouts.append(command_timeout)
                if executable == "mkvmerge":
                    clock[0] += 1.0
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "tracks": [
                                    {
                                        "id": 2,
                                        "type": "subtitles",
                                        "properties": {"number": 5},
                                    }
                                ]
                            }
                        ),
                        "",
                    )
                if executable == "mkvextract":
                    clock[0] += 1.0
                    Path(str(command[-1]).split(":", 1)[1]).write_text(
                        "1\n00:00:01,000 --> 00:00:02,000\n明天選班長\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0, "", "")
                if executable == "ffmpeg":
                    clock[0] += command_timeout
                    raise subprocess.TimeoutExpired(command, command_timeout)
                raise AssertionError(f"unexpected command: {command}")

            with (
                patch("subtitle_extract._resolve_ffmpeg", return_value="ffmpeg"),
                patch(
                    "subtitle_extract.shutil.which",
                    side_effect=lambda name: name if name in {"mkvextract", "mkvmerge"} else None,
                ),
                patch("subtitle_extract.time.monotonic", side_effect=lambda: clock[0]),
                patch("subtitle_extract.subprocess.run", side_effect=fake_run),
            ):
                with self.assertRaises(SubtitleExtractCancelled):
                    _extract_subtitle_stream(
                        video,
                        4,
                        output,
                        "subrip",
                        timeout_seconds=300,
                        subtitle_ordinal=0,
                        deadline_monotonic=105.0,
                    )

            self.assertEqual(commands, ["mkvmerge", "mkvextract", "ffmpeg"])
            self.assertEqual(timeouts, [5.0, 4.0, 3.0])
            self.assertEqual(clock[0], 105.0)

    def test_classification_ignores_ass_format_sections(self) -> None:
        ass = """
[Script Info]
Title: 这里对时会说们来让还后着与学国台声点体关开间见经过无发将样话
[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,这里对时会说们来让还后着与学国台声点体关开间见经过无发将样话,72
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\an8}明天選班長\\N這裡會說話
"""

        cleaned = _subtitle_text_for_classification(ass)

        self.assertIn("明天選班長", cleaned)
        self.assertNotIn("Style:", cleaned)
        self.assertNotIn("\\an8", cleaned)

    def test_sidecar_classification_uses_content_before_filename_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle = Path(temp_dir) / "Anime S01E01.zh-hans.ass"
            subtitle.write_text(
                "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,明天選班長\\N這裡會說話",
                encoding="utf-8",
            )

            self.assertEqual(classify_sidecar_subtitle_language(subtitle), "zh-tw")

    def test_bilingual_sidecar_prefers_chinese_when_chinese_evidence_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle = Path(temp_dir) / "Anime S01E01.bilingual.ass"
            subtitle.write_text(
                "\n".join(
                    [
                        "[Events]",
                        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                        "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\an8}\u3053\u3093\u306b\u3061\u306f\u3001\u3042\u308a\u304c\u3068\u3046\u3054\u3056\u3044\u307e\u3059",
                        "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,\u9019\u662f\u7e41\u9ad4\u4e2d\u6587\u5b57\u5e55",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(classify_sidecar_subtitle_language(subtitle), "zh-tw")

    def test_classification_exposes_scores_and_reason(self) -> None:
        classification = _classify_subtitle_content_detail(
            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,明天選班長\\N這裡會說話",
            metadata_language=None,
        )

        self.assertEqual(classification.language, "zh-tw")
        self.assertEqual(classification.reason, "chinese_script_score")
        self.assertGreater(classification.traditional_score, classification.simplified_score)
        self.assertGreater(classification.quality_score, 0)

    def test_chinese_metadata_does_not_override_japanese_content(self) -> None:
        classification = _classify_subtitle_content_detail(
            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,これは日本語の字幕です。",
            metadata_language="zh-tw",
        )

        self.assertEqual(classification.language, "ja")
        self.assertIn("japanese_kana", classification.reason)

    def test_chinese_metadata_without_content_evidence_is_rejected(self) -> None:
        classification = _classify_subtitle_content_detail(
            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Plain English subtitle.",
            metadata_language="zh-cn",
        )

        self.assertIsNone(classification.language)
        self.assertEqual(classification.reason, "metadata_ignored_no_content_evidence")

    def test_chinese_metadata_can_tiebreak_cjk_content_without_script_markers(self) -> None:
        classification = _classify_subtitle_content_detail(
            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,世界和平大家一起吃飯",
            metadata_language="zh-tw",
        )

        self.assertEqual(classification.language, "zh-tw")
        self.assertEqual(classification.reason, "metadata_chinese_with_cjk_content")

    def test_chinese_metadata_bilingual_cjk_line_beats_strong_kana_score(self) -> None:
        classification = _classify_subtitle_content_detail(
            "\n".join(
                [
                    "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,\u3053\u308c\u306f\u65e5\u672c\u8a9e\u306e\u5b57\u5e55\u3067\u3059\u3002\u3042\u308a\u304c\u3068\u3046\u3054\u3056\u3044\u307e\u3059\u3002\u3088\u308d\u3057\u304f\u304a\u9858\u3044\u3057\u307e\u3059\u3002",
                    "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,\u5c71\u5ddd\u7530\u4e2d\u5927\u5c0f\u4e0a\u4e0b\u5de6\u53f3\u5357\u5317\u6625\u590f\u79cb\u51ac\u671d\u663c\u591c\u96e8\u96ea\u6708\u82b1\u65e5\u672c\u8a9e\u5b57\u5e55",
                ]
            ),
            metadata_language="zh-tw",
        )

        self.assertEqual(classification.language, "zh-tw")
        self.assertEqual(classification.reason, "metadata_chinese_bilingual_cjk_content")

    def test_japanese_metadata_does_not_mark_kanji_only_unknown_text_as_japanese(self) -> None:
        classification = _classify_subtitle_content_detail(
            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,世界和平大家一起吃飯",
            metadata_language="ja",
        )

        self.assertIsNone(classification.language)
        self.assertEqual(classification.reason, "metadata_ignored_no_content_evidence")

    def test_extracts_every_text_stream_then_imports_only_detected_chinese(self) -> None:
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".AI日本語.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI简日双语.zh.ass",
            ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
            mikan_remove_ai_after_extract=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")

            def extract_stream(_video: Path, stream_index: int, output: Path, _codec: str, **_kwargs: object) -> None:
                text = (
                    "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,これは日本語の字幕です。"
                    if stream_index == 5
                    else "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,这个视频已经开始播放，请不要关闭窗口。"
                )
                output.write_text(text, encoding="utf-8")

            diagnostics: list[dict] = []
            with (
                patch(
                    "subtitle_extract._probe_subtitle_streams",
                    return_value=[
                        {"index": 5, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "chi"}},
                        {"index": 6, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "chi"}},
                    ],
                ),
                patch("subtitle_extract._extract_subtitle_stream", side_effect=extract_stream) as extractor,
            ):
                extracted = extract_available_subtitles(
                    video,
                    config,
                    diagnostics=diagnostics,
                    allowed_languages={"zh-tw", "zh-cn"},
                )

            self.assertEqual(extractor.call_count, 2)
            self.assertEqual([item.language for item in extracted], ["zh-cn"])
            detected_languages = {
                item.get("classification", {}).get("language")
                for item in diagnostics
                if isinstance(item.get("classification"), dict)
            }
            self.assertEqual(detected_languages, {"ja", "zh-cn"})

    def test_extract_available_subtitles_picks_best_same_language_candidate(self) -> None:
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".AI日本語.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI简日双语.zh.ass",
            ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
            mikan_remove_ai_after_extract=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")

            def extract_stream(_video: Path, stream_index: int, output: Path, _codec: str, **_kwargs: object) -> None:
                if stream_index == 5:
                    output.write_text(
                        "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,明天選班長",
                        encoding="utf-8",
                    )
                    return
                output.write_text(
                    "\n".join(
                        [
                            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,明天選班長",
                            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,這裡會說話",
                            "Dialogue: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,後面還有內容",
                        ]
                    ),
                    encoding="utf-8",
                )

            diagnostics: list[dict] = []
            with (
                patch(
                    "subtitle_extract._probe_subtitle_streams",
                    return_value=[
                        {"index": 5, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "chi"}},
                        {"index": 6, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "chi"}},
                    ],
                ),
                patch("subtitle_extract._extract_subtitle_stream", side_effect=extract_stream),
            ):
                extracted = extract_available_subtitles(video, config, diagnostics=diagnostics)

            self.assertEqual([item.language for item in extracted], ["zh-tw"])
            self.assertEqual(extracted[0].stream_index, 6)
            self.assertGreaterEqual(len(diagnostics), 2)
            output = Path(temp_dir) / "Anime S01E01.zh-TW.ass"
            self.assertIn("後面還有內容", output.read_text(encoding="utf-8"))

    def test_extract_available_subtitles_classifies_unknown_internal_stream_by_content(self) -> None:
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".AI日本語.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI简日双语.zh.ass",
            ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
            mikan_remove_ai_after_extract=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")

            def extract_stream(_video: Path, _stream_index: int, output: Path, _codec: str, **_kwargs: object) -> None:
                output.write_text(
                    """
[Script Info]
Title: 这里对时会说们来让还后着与学国台声点体关开间见经过无发将样话
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\fn微软雅黑}明天選班長\\N這裡會說話
""",
                    encoding="utf-8",
                )

            with (
                patch(
                    "subtitle_extract._probe_subtitle_streams",
                    return_value=[
                        {
                            "index": 5,
                            "codec_type": "subtitle",
                            "codec_name": "ass",
                            "tags": {"language": "und", "title": ""},
                        }
                    ],
                ),
                patch("subtitle_extract._extract_subtitle_stream", side_effect=extract_stream) as extractor,
            ):
                extracted = extract_available_subtitles(video, config)

            self.assertEqual([item.language for item in extracted], ["zh-tw"])
            self.assertEqual(extractor.call_count, 1)
            self.assertTrue((Path(temp_dir) / "Anime S01E01.zh-TW.ass").exists())
            self.assertFalse((Path(temp_dir) / "Anime S01E01.zh.ass").exists())

    def test_classifies_internal_zh_hans_and_zh_hant_titles(self) -> None:
        self.assertEqual(_classify_subtitle_language({"tags": {"language": "chi", "title": "zh-hans"}}), "zh-cn")
        self.assertEqual(_classify_subtitle_language({"tags": {"language": "chi", "title": "zh-hant"}}), "zh-tw")

    def test_normalize_sidecar_subtitles_writes_requested_names(self) -> None:
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".AI日本語.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI简日双语.zh.ass",
            ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
            mikan_remove_ai_after_extract=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            simplified = Path(temp_dir) / "Anime S01E01.stream_15.chi.ass"
            traditional = Path(temp_dir) / "Anime S01E01.stream_16.chi.ass"
            simplified.write_text("Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,\u660e\u5929\u9009\u73ed\u957f", encoding="utf-8")
            traditional.write_text("Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,\u660e\u5929\u9078\u73ed\u9577", encoding="utf-8")

            normalized = normalize_sidecar_subtitles(video, config)

            self.assertEqual({item.language for item in normalized}, {"zh-cn", "zh-tw"})
            self.assertTrue((Path(temp_dir) / "Anime S01E01.zh.ass").exists())
            self.assertTrue((Path(temp_dir) / "Anime S01E01.zh-TW.ass").exists())

    def test_normalize_sidecar_conversion_uses_absolute_deadline(self) -> None:
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".AI.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI.zh.ass",
            ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
            mikan_remove_ai_after_extract=False,
            subtitle_extract_timeout_seconds=300,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")
            sidecar = Path(temp_dir) / "Anime S01E01.zh-hant.srt"
            sidecar.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n明天選班長\n",
                encoding="utf-8",
            )
            clock = [200.0]
            observed_timeouts: list[float] = []

            def timeout_conversion(command, **kwargs):
                command_timeout = float(kwargs["timeout"])
                observed_timeouts.append(command_timeout)
                clock[0] += command_timeout
                raise subprocess.TimeoutExpired(command, command_timeout)

            with (
                patch("subtitle_extract._resolve_ffmpeg", return_value="ffmpeg"),
                patch("subtitle_extract.time.monotonic", side_effect=lambda: clock[0]),
                patch("subtitle_extract.subprocess.run", side_effect=timeout_conversion),
            ):
                with self.assertRaises(SubtitleExtractCancelled):
                    normalize_sidecar_subtitles(
                        video,
                        config,
                        deadline_monotonic=204.0,
                    )

            self.assertEqual(observed_timeouts, [4.0])
            self.assertEqual(clock[0], 204.0)

    def test_normalize_sidecar_subtitles_can_write_to_target_video_path(self) -> None:
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".AI日本語.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI简日双语.zh.ass",
            ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
            mikan_remove_ai_after_extract=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "download"
            target_dir = root / "library"
            source_dir.mkdir()
            target_dir.mkdir()
            source_video = source_dir / "Source Show - 01.mkv"
            target_video = target_dir / "Target Show - S01E01.mkv"
            source_video.write_text("", encoding="utf-8")
            target_video.write_text("", encoding="utf-8")
            sidecar = source_dir / "Source Show - 01.zh-hant.ass"
            sidecar.write_text("Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,\u660e\u5929\u9078\u73ed\u9577", encoding="utf-8")

            normalized = normalize_sidecar_subtitles_for_output(
                source_video,
                config,
                output_video_path=target_video,
            )

            self.assertEqual([item.language for item in normalized], ["zh-tw"])
            self.assertEqual(normalized[0].path.parent, target_dir)
            self.assertTrue(normalized[0].path.name.startswith("Target Show - S01E01."))
            self.assertTrue(normalized[0].path.exists())

    def test_normalize_sidecar_subtitles_keeps_ai_ass_when_ai_is_required(self) -> None:
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".AI.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI.zh.ass",
            ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
            mikan_remove_ai_after_extract=True,
            require_ai_subtitles=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            ai_ass = Path(temp_dir) / "Anime S01E01.AI.zh-TW.ass"
            ai_ass.write_text("ai", encoding="utf-8")
            sidecar = Path(temp_dir) / "Anime S01E01.zh-hant.ass"
            sidecar.write_text("Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,\u660e\u5929\u9078\u73ed\u9577", encoding="utf-8")

            normalized = normalize_sidecar_subtitles(video, config)

            self.assertEqual([item.language for item in normalized], ["zh-tw"])
            self.assertTrue(ai_ass.exists())

    def test_normalize_sidecar_subtitles_keeps_human_srt_after_ass_export(self) -> None:
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".AI日本語.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI简日双语.zh.ass",
            ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
            mikan_remove_ai_after_extract=True,
            export_ai_ass=True,
            keep_intermediate_files=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            sidecar = Path(temp_dir) / "Anime S01E01.zh-hant.srt"
            sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\n明天選班長\n", encoding="utf-8")

            def convert(_source: Path, output: Path) -> None:
                output.write_text(
                    "Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,明天選班長",
                    encoding="utf-8",
                )

            with patch("subtitle_extract._copy_or_convert_sidecar", side_effect=convert):
                normalized = normalize_sidecar_subtitles(video, config)

            self.assertEqual([item.language for item in normalized], ["zh-tw"])
            self.assertTrue(sidecar.exists())
            self.assertTrue(normalized[0].path.exists())

    def test_remove_ai_srt_outputs_keeps_human_srt(self) -> None:
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".AI.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI.zh.ass",
            ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
            keep_intermediate_files=False,
            work_path=Path(tempfile.gettempdir()) / "anime-subtitle-worker-test-work",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            srt = Path(temp_dir) / "Anime S01E01.zh-TW.srt"
            ai_srt = Path(temp_dir) / "Anime S01E01.AI.zh-TW.srt"
            video.write_text("", encoding="utf-8")
            srt.write_text("srt", encoding="utf-8")
            ai_srt.write_text("ai", encoding="utf-8")
            config.work_path = Path(temp_dir) / "work"
            paths = paths_for_video(video, config)
            paths.zh_tw_srt.parent.mkdir(parents=True, exist_ok=True)
            paths.ja_srt.write_text("japanese cache", encoding="utf-8")
            paths.zh_tw_srt.write_text("cache", encoding="utf-8")

            removed = remove_ai_srt_outputs(video, config)

            self.assertEqual(set(removed), {paths.zh_tw_srt, ai_srt})
            self.assertTrue(paths.ja_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertFalse(ai_srt.exists())
            self.assertTrue(srt.exists())

    def test_sidecar_with_chs_cht_marker_and_long_ass_header_is_detected_as_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle = Path(temp_dir) / "Show S01E01 [CHS&CHT][简繁外挂].ass"
            long_header = "\n".join(f"Style: Dummy{i},Arial,20" for i in range(900))
            subtitle.write_text(
                "\n".join(
                    [
                        "[Script Info]",
                        "Title: long header",
                        "[V4+ Styles]",
                        long_header,
                        "[Events]",
                        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                        "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,這是一段繁體中文字幕，用來確認長 ASS header 之後仍能被讀到。",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(classify_sidecar_subtitle_language(subtitle), "zh-tw")

    def test_sidecar_ai_skip_does_not_match_non_ai_release_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle = Path(temp_dir) / "Railgun S01E01.chs.ass"
            subtitle.write_text(
                "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,这是一个简体中文字幕测试。",
                encoding="utf-8",
            )

            self.assertEqual(classify_sidecar_subtitle_language(subtitle), "zh-cn")

    def test_metadata_chinese_bilingual_subtitle_keeps_chinese_candidate(self) -> None:
        classification = _classify_subtitle_content_detail(
            "\n".join(
                [
                    "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,こんにちは、今日はいい天気ですね。",
                    "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,這是中文翻譯，不應該因為同時有日文假名就被整份判成日文。",
                ]
            ),
            metadata_language="zh-tw",
        )

        self.assertEqual(classification.language, "zh-tw")
        self.assertEqual(classification.reason, "chinese_script_score")

    def test_trusted_english_sidecar_requires_explicit_metadata_and_content_evidence(self) -> None:
        english_dialogues = "\n".join(
            (
                "Dialogue: 0,0:00:01.00,0:00:03.00,English,,0,0,0,,"
                "This is the place where you and I are going, but we should not leave without them."
            )
            for _index in range(20)
        )
        spanish_dialogues = "\n".join(
            (
                "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,"
                "Esta es la historia de una joven que busca su familia durante toda la noche."
            )
            for _index in range(20)
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            trusted = root / "Anime S01E01.English.eng.ass"
            trusted.write_text(english_dialogues, encoding="utf-8")
            classification = classify_sidecar_subtitle(trusted)
            self.assertEqual(classification.language, "en")
            self.assertEqual(classification.metadata_language, "en")
            self.assertEqual(classification.reason, "metadata_english_latin_content")

            title_only = root / "English Show S01E01.ass"
            title_only.write_text(english_dialogues, encoding="utf-8")
            self.assertIsNone(classify_sidecar_subtitle_language(title_only))

            mislabeled = root / "Anime S01E02.en.ass"
            mislabeled.write_text(spanish_dialogues, encoding="utf-8")
            self.assertIsNone(classify_sidecar_subtitle_language(mislabeled))

    def test_ai_english_sidecars_are_never_trusted_as_official_sources(self) -> None:
        content = "\n".join(
            (
                "Dialogue: 0,0:00:01.00,0:00:03.00,English,,0,0,0,,"
                "This is the place where you and I are going, but we should not leave without them."
            )
            for _index in range(20)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in (
                "Anime S01E01.AIEnglish.en.ass",
                "Anime S01E01.AI原語言.English.eng.ass",
            ):
                with self.subTest(name=name):
                    subtitle = root / name
                    subtitle.write_text(content, encoding="utf-8")
                    classification = classify_sidecar_subtitle(subtitle)
                    self.assertIsNone(classification.language)
                    self.assertEqual(classification.reason, "ai_sidecar_skipped")

    def test_normalize_english_sidecar_uses_canonical_english_eng_name(self) -> None:
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".AI日本語.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI简日双语.zh.ass",
            ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
            mikan_remove_ai_after_extract=False,
        )
        content = "\n".join(
            (
                f"Dialogue: 0,0:{_index:02}:01.00,0:{_index:02}:05.00,English,,0,0,0,,"
                f"We should wait here for our friends {_index + 1}."
            )
            for _index in range(20)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E03.mkv"
            video.write_bytes(b"video")
            source = root / "Anime S01E03.en.ass"
            source.write_text(content, encoding="utf-8")

            normalized = normalize_sidecar_subtitles(video, config)

            self.assertEqual([item.language for item in normalized], ["en"])
            self.assertEqual(
                normalized[0].path,
                root / "Anime S01E03.English.eng.ass",
            )
            self.assertTrue(normalized[0].path.is_file())

    def test_normalize_english_sidecar_rejects_configured_ai_source_template(self) -> None:
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".AI日本語.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI简日双语.zh.ass",
            ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
            ai_source_transcript_ass_suffix_template=".Source{label}.{language}.ass",
            mikan_remove_ai_after_extract=False,
        )
        content = "\n".join(
            (
                "Dialogue: 0,0:00:01.00,0:00:03.00,English,,0,0,0,,"
                "This is the place where you and I are going, but we should not leave without them."
            )
            for _index in range(20)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E04.mkv"
            video.write_bytes(b"video")
            generated = root / "Anime S01E04.SourceEnglish.en.ass"
            generated.write_text(content, encoding="utf-8")

            normalized = normalize_sidecar_subtitles(video, config)

            self.assertEqual(normalized, [])
            self.assertFalse((root / "Anime S01E04.English.eng.ass").exists())


def _guard_ass(*, overlap: bool = False) -> str:
    def stamp(seconds: int) -> str:
        return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}.00"

    return "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" + "".join(
        f"Dialogue: 0,{stamp(index * 30)},{stamp(index * 30 + (40 if overlap else 3))},Default,,0,0,0,,"
        f"這裡會選擇開啟網路連線並顯示訊息，第{index + 1}段。\n"
        for index in range(30)
    )


def _malformed_guard_srt() -> str:
    return (
        "1\n00:00:00,000 --> 00:00:03,000\n這裡會選擇開啟網路連線並顯示訊息。\n\n"
        "317\n00:01:49,880 --> 00:01:49,880\n"
    )


def _guard_vtt() -> str:
    def stamp(seconds: int) -> str:
        return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}.000"

    return "WEBVTT\n\n" + "\n".join(
        f"{stamp(index * 30)} --> {stamp(index * 30 + 3)}\n"
        f"這裡會選擇開啟網路連線並顯示訊息，第{index + 1}段。\n"
        for index in range(30)
    )


if __name__ == "__main__":
    unittest.main()
