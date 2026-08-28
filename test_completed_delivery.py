from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import completed_delivery as delivery
import main as worker_main


class CompletedDeliveryTests(unittest.TestCase):
    def test_delivery_only_attempt_uses_fresh_completed_commit_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"video")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "delivery": {
                            "contract": "ai-delivery-v1",
                            "policy_revision": "policy",
                            "verified_at": 100.0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({"committed_at": 200.0}), encoding="utf-8")
            config = SimpleNamespace(completed_delivery_enabled=True)
            publication = {
                "contract": "ai-publication-semantics-v2",
                "kind": "adopted_zh_tw",
                "output_languages": ["zh-TW"],
            }
            with (
                patch("output_manifest.output_manifest_path", return_value=manifest),
                patch("output_manifest.validate_output_manifest", return_value=True),
                patch("output_manifest.manifest_publication_semantics", return_value=publication),
                patch("output_manifest.publication_is_traditional_chinese_delivery", return_value=True),
                patch("completed_delivery.validate_completed_delivery", return_value=True),
                patch("completed_delivery.completed_delivery_receipt_path", return_value=receipt),
            ):
                evidence = worker_main._verified_ai_delivery_evidence(
                    video,
                    config,
                    obligation_id="obligation",
                    expected_policy_revision="policy",
                    attempt_started_at=150.0,
                )
            self.assertIsNotNone(evidence)
            self.assertEqual(evidence["verified_at"], 200.0)
            self.assertEqual(evidence["verification"]["subtitle_manifest_verified_at"], 100.0)

    def test_destination_preserves_relative_path_and_rejects_input_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input" / "Series" / "Season 1" / "Episode.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"video")
            config = _config(root)
            expected = root / "completed" / "Series" / "Season 1" / "Episode.mp4.mkv"
            self.assertEqual(delivery.completed_delivery_destination(source, config), expected.resolve())

            unsafe = _config(root, completed_delivery_path=str(root / "input" / "completed"))
            with self.assertRaises(delivery.CompletedDeliveryError):
                delivery.completed_delivery_destination(source, unsafe)

    def test_existing_destination_without_receipt_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input" / "Episode.mkv"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            destination = root / "completed" / "Episode.mkv"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"unrelated")
            config = _config(root)
            publication = {
                "semantics": {"contract": "ai-publication-semantics-v2", "kind": "adopted_zh_tw", "output_languages": ["zh-TW"]},
                "tracks": [],
            }
            identity = {
                "obligation_id": "o",
                "policy_revision": "p",
                "media": {"media_fingerprint": "f"},
            }
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            with (
                patch.object(delivery, "_strict_publication", return_value=publication),
                patch.object(delivery, "delivery_identity", return_value=identity),
                patch.object(delivery, "output_manifest_path", return_value=manifest),
                self.assertRaises(delivery.CompletedDeliveryCollisionError),
            ):
                delivery.deliver_completed_mkv(source, config)
            self.assertEqual(destination.read_bytes(), b"unrelated")
            self.assertTrue(source.is_file())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg tools unavailable")
    def test_real_ffmpeg_mux_preserves_av_and_embeds_verified_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mkv"
            attachment = root / "font.ttf"
            attachment.write_bytes(b"fixture-font-attachment")
            subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x180:d=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=2",
                    "-c:v",
                    "mpeg4",
                    "-c:a",
                    "aac",
                    "-attach",
                    str(attachment),
                    "-metadata:s:t",
                    "mimetype=application/x-truetype-font",
                    "-metadata:s:t",
                    "filename=font.ttf",
                    "-shortest",
                    str(source),
                ],
                check=True,
                timeout=30,
            )
            tracks = []
            for language, text in (("ja", "こんにちは"), ("zh-CN", "你好"), ("zh-TW", "您好")):
                subtitle = root / f"{language}.ass"
                subtitle.write_text(_ass(text), encoding="utf-8")
                tracks.append(
                    {
                        "path": str(subtitle),
                        "language": language,
                        "title": delivery._TITLE_BY_LANGUAGE[language],
                        "sha256": delivery.sha256_file(subtitle),
                    }
                )
            publication = {
                "semantics": {
                    "contract": "ai-publication-semantics-v2",
                    "kind": "translated_trilingual",
                    "output_languages": ["ja", "zh-CN", "zh-TW"],
                },
                "tracks": tracks,
            }
            output = root / "completed.mkv"
            delivery._run_mux(source, output, publication, timeout=30, logger=None)
            delivery._verify_muxed_output(source, output, publication, timeout=30)
            probe = delivery._probe(output, timeout=30)
            generated = [
                stream for stream in probe["streams"] if stream.get("codec_type") == "subtitle"
            ][-3:]
            self.assertEqual([stream["tags"]["language"] for stream in generated], ["ja", "zh-CN", "zh-TW"])
            self.assertEqual(generated[-1]["disposition"]["default"], 1)
            self.assertEqual(
                sum(1 for stream in probe["streams"] if stream.get("codec_type") == "attachment"),
                1,
            )

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg tools unavailable")
    def test_commit_crash_recovers_final_without_remux_or_source_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input" / "Series" / "Episode.mkv"
            source.parent.mkdir(parents=True)
            _make_source(source)
            subtitle = root / "Episode.zh-TW.ass"
            subtitle.write_text(_ass("安全交付"), encoding="utf-8")
            publication = {
                "semantics": {
                    "contract": "ai-publication-semantics-v2",
                    "kind": "adopted_zh_tw",
                    "output_languages": ["zh-TW"],
                },
                "tracks": [
                    {
                        "path": str(subtitle),
                        "language": "zh-TW",
                        "title": delivery._TITLE_BY_LANGUAGE["zh-TW"],
                        "sha256": delivery.sha256_file(subtitle),
                    }
                ],
            }
            stat = source.stat()
            identity = {
                "obligation_id": "obligation-1",
                "policy_revision": "policy-1",
                "media": {"media_fingerprint": "media-1"},
            }
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"source_size": stat.st_size}), encoding="utf-8")
            config = _config(root)
            receipt = delivery.completed_delivery_receipt_path(source, config)
            original_atomic_write = delivery.atomic_write_text

            def fail_receipt(path: Path, content: str, **kwargs: object) -> Path:
                if Path(path) == receipt:
                    raise OSError("simulated crash after final rename")
                return original_atomic_write(path, content, **kwargs)

            common = (
                patch.object(delivery, "_strict_publication", return_value=publication),
                patch.object(delivery, "delivery_identity", return_value=identity),
                patch.object(delivery, "output_manifest_path", return_value=manifest),
            )
            with common[0], common[1], common[2], patch.object(
                delivery, "atomic_write_text", side_effect=fail_receipt
            ):
                with self.assertRaises(OSError):
                    delivery.deliver_completed_mkv(source, config)

            destination = delivery.completed_delivery_destination(source, config)
            self.assertTrue(source.is_file())
            self.assertTrue(destination.is_file())
            self.assertTrue(delivery.completed_delivery_marker_path(source, config).is_file())
            self.assertFalse(receipt.exists())

            with (
                patch.object(delivery, "_strict_publication", return_value=publication),
                patch.object(delivery, "delivery_identity", return_value=identity),
                patch.object(delivery, "output_manifest_path", return_value=manifest),
            ):
                result = delivery.deliver_completed_mkv(source, config)
                again = delivery.deliver_completed_mkv(source, config)
            self.assertTrue(result.recovered)
            self.assertTrue(again.recovered)
            self.assertTrue(receipt.is_file())
            self.assertFalse(delivery.completed_delivery_marker_path(source, config).exists())
            self.assertTrue(source.is_file())
            with destination.open("ab") as handle:
                handle.write(b"tamper")
            with (
                patch.object(delivery, "_strict_publication", return_value=publication),
                patch.object(delivery, "delivery_identity", return_value=identity),
                patch.object(delivery, "output_manifest_path", return_value=manifest),
                self.assertRaises(delivery.CompletedDeliveryError),
            ):
                delivery.deliver_completed_mkv(source, config)


def _config(root: Path, **overrides: object) -> SimpleNamespace:
    values = {
        "input_path": root / "input",
        "work_path": root / "work",
        "completed_delivery_enabled": True,
        "completed_delivery_path": str(root / "completed"),
        "completed_delivery_manifest_path": str(root / "work" / "completed_delivery_manifests"),
        "completed_delivery_source_policy": "retain",
        "completed_delivery_timeout_seconds": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ass(text: str) -> str:
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 320\n"
        "PlayResY: 180\n"
        "\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n"
        "\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.20,0:00:01.50,Default,,0,0,0,,{text}\n"
    )


def _make_source(path: Path) -> None:
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        timeout=30,
    )


if __name__ == "__main__":
    unittest.main()
