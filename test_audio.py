from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import wave

from audio import (
    AudioExtractionError,
    AudioStreamInfo,
    AudioStreamManifest,
    _find_demucs_stem,
    _new_audio_partial_path,
    audio_cache_metadata_path,
    cleanup_audio_partials,
    extract_audio,
    manifest_confirms_no_non_commentary_japanese_audio,
    probe_audio_stream_manifest,
    validate_cached_audio,
)


def _write_pcm_wave(path: Path, *, duration_seconds: float = 1.0, fill: bytes = b"\x00\x00") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = max(0, int(round(16000 * duration_seconds)))
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(fill * frame_count)


def _extract_valid_test_audio(
    source: Path,
    output: Path,
    *,
    duration_seconds: float = 1.0,
    stream_index: int = 1,
    fill: bytes = b"\x00\x00",
) -> None:
    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        _write_pcm_wave(Path(command[-1]), duration_seconds=duration_seconds, fill=fill)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch("audio._resolve_ffmpeg", return_value="ffmpeg"),
        patch("audio.subprocess.run", side_effect=run),
        patch("audio.probe_media_duration", return_value=duration_seconds),
    ):
        extract_audio(
            source,
            output,
            stream_index=stream_index,
            source_duration_seconds=duration_seconds,
        )


class AudioTest(unittest.TestCase):
    def test_live_english_only_manifests_confirm_no_japanese_main_track(self) -> None:
        live_manifests = (
            (
                "S01E20",
                {
                    "index": 1,
                    "codec_name": "truehd",
                    "channels": 6,
                    "tags": {"language": "eng", "title": ""},
                    "disposition": {"default": 1},
                },
            ),
            (
                "S02E01",
                {
                    "index": 1,
                    "codec_name": "flac",
                    "channels": 6,
                    "tags": {"language": "eng", "title": "5.1Ch FLAC 24bit"},
                    "disposition": {"default": 1},
                },
            ),
            (
                "S02E02",
                {
                    "index": 1,
                    "codec_name": "flac",
                    "channels": 6,
                    "tags": {"language": "eng", "title": "5.1Ch FLAC 24bit"},
                    "disposition": {"default": 1},
                },
            ),
        )
        for episode, stream in live_manifests:
            with self.subTest(episode=episode), patch(
                "audio.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"streams": [stream]}),
                    stderr="",
                ),
            ):
                manifest = probe_audio_stream_manifest(Path(f"{episode}.mkv"))

            self.assertTrue(manifest.complete)
            self.assertEqual(len(manifest.streams), 1)
            self.assertTrue(manifest_confirms_no_non_commentary_japanese_audio(manifest))

    def test_audio_manifest_ignores_explicit_japanese_commentary_when_english_main_is_certain(self) -> None:
        manifest = AudioStreamManifest(
            (
                AudioStreamInfo(1, "jpn", "Japanese commentary", False, True),
                AudioStreamInfo(2, "eng", "English main", True, False),
            ),
            True,
        )

        self.assertTrue(manifest_confirms_no_non_commentary_japanese_audio(manifest))

    def test_audio_manifest_keeps_unknown_or_potentially_mistagged_main_tracks(self) -> None:
        cases = (
            AudioStreamInfo(1, "", "", True, False),
            AudioStreamInfo(1, "und", "Main", True, False),
            AudioStreamInfo(1, "eng", "Japanese main", True, False),
        )
        for stream in cases:
            with self.subTest(language=stream.language, title=stream.title):
                manifest = AudioStreamManifest((stream,), True)
                self.assertFalse(manifest_confirms_no_non_commentary_japanese_audio(manifest))

    def test_failed_audio_probe_is_incomplete_not_an_empty_success(self) -> None:
        with patch(
            "audio.subprocess.run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="probe failed"),
        ):
            manifest = probe_audio_stream_manifest(Path("episode.mkv"))

        self.assertFalse(manifest.complete)
        self.assertEqual(manifest.streams, ())
        self.assertIn("probe failed", manifest.error)

    def test_extract_audio_prefers_japanese_track_over_default_non_japanese_track(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "video.mkv"
            video.write_bytes(b"video")
            output = root / "out.wav"
            ffmpeg_commands: list[list[str]] = []

            def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
                if command[0] == "ffprobe":
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "streams": [
                                    {
                                        "index": 1,
                                        "tags": {"language": "eng", "title": "English"},
                                        "disposition": {"default": 1},
                                    },
                                    {
                                        "index": 2,
                                        "tags": {"language": "jpn", "title": "Japanese"},
                                        "disposition": {"default": 0},
                                    },
                                ]
                            }
                        ),
                        stderr="",
                    )
                ffmpeg_commands.append(command)
                _write_pcm_wave(Path(command[-1]))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("audio._resolve_ffprobe", return_value="ffprobe"),
                patch("audio._resolve_ffmpeg", return_value="ffmpeg"),
                patch("audio.subprocess.run", side_effect=run),
                patch("audio.probe_media_duration", return_value=1.0),
            ):
                extract_audio(video, output)

            self.assertEqual(ffmpeg_commands[0][ffmpeg_commands[0].index("-map") + 1], "0:2")
            self.assertNotEqual(Path(ffmpeg_commands[0][-1]), output)
            self.assertEqual(Path(ffmpeg_commands[0][-1]).parent, output.parent)
            self.assertTrue(output.is_file())
            self.assertTrue(audio_cache_metadata_path(output).is_file())
            self.assertTrue(validate_cached_audio(output, video, stream_index=2))

    def test_extract_audio_prefers_default_japanese_track_when_multiple_japanese_tracks_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "video.mkv"
            video.write_bytes(b"video")
            output = root / "out.wav"
            ffmpeg_commands: list[list[str]] = []

            def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
                if command[0] == "ffprobe":
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "streams": [
                                    {
                                        "index": 1,
                                        "tags": {"language": "jpn", "title": "Japanese commentary"},
                                        "disposition": {"default": 0},
                                    },
                                    {
                                        "index": 2,
                                        "tags": {"language": "jpn", "title": "Japanese main"},
                                        "disposition": {"default": 1},
                                    },
                                ]
                            }
                        ),
                        stderr="",
                    )
                ffmpeg_commands.append(command)
                _write_pcm_wave(Path(command[-1]))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("audio._resolve_ffprobe", return_value="ffprobe"),
                patch("audio._resolve_ffmpeg", return_value="ffmpeg"),
                patch("audio.subprocess.run", side_effect=run),
                patch("audio.probe_media_duration", return_value=1.0),
            ):
                extract_audio(video, output)

            self.assertEqual(ffmpeg_commands[0][ffmpeg_commands[0].index("-map") + 1], "0:2")

    def test_extract_audio_falls_back_to_ffmpeg_default_when_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "video.mkv"
            video.write_bytes(b"video")
            output = root / "out.wav"
            ffmpeg_commands: list[list[str]] = []

            def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
                if command[0] == "ffprobe":
                    return SimpleNamespace(returncode=1, stdout="", stderr="probe failed")
                ffmpeg_commands.append(command)
                _write_pcm_wave(Path(command[-1]))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("audio._resolve_ffprobe", return_value="ffprobe"),
                patch("audio._resolve_ffmpeg", return_value="ffmpeg"),
                patch("audio.subprocess.run", side_effect=run),
                patch("audio.probe_media_duration", return_value=1.0),
            ):
                extract_audio(video, output)

            self.assertNotIn("-map", ffmpeg_commands[0])

    def test_extract_audio_failure_preserves_existing_final_and_cleans_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "video.mkv"
            source.write_bytes(b"video")
            output = root / "audio.wav"
            output.write_bytes(b"existing-final")
            metadata = audio_cache_metadata_path(output)
            metadata.write_bytes(b"existing-metadata")

            def fail(command: list[str], **_kwargs: object) -> SimpleNamespace:
                Path(command[-1]).write_bytes(b"partial-data")
                return SimpleNamespace(returncode=1, stdout="", stderr="injected failure")

            with (
                patch("audio._resolve_ffmpeg", return_value="ffmpeg"),
                patch("audio.subprocess.run", side_effect=fail),
                patch("audio.probe_media_duration", return_value=1.0),
            ):
                with self.assertRaisesRegex(AudioExtractionError, "injected failure"):
                    extract_audio(source, output, stream_index=1)

            self.assertEqual(output.read_bytes(), b"existing-final")
            self.assertEqual(metadata.read_bytes(), b"existing-metadata")
            self.assertEqual(list(root.glob("*.partial.wav")), [])

    def test_extract_audio_timeout_preserves_existing_final_and_cleans_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "video.mkv"
            source.write_bytes(b"video")
            output = root / "audio.wav"
            output.write_bytes(b"existing-final")

            def timeout(command: list[str], **_kwargs: object) -> SimpleNamespace:
                Path(command[-1]).write_bytes(b"partial-data")
                raise subprocess.TimeoutExpired(command, 1)

            with (
                patch("audio._resolve_ffmpeg", return_value="ffmpeg"),
                patch("audio.subprocess.run", side_effect=timeout),
                patch("audio.probe_media_duration", return_value=1.0),
            ):
                with self.assertRaisesRegex(AudioExtractionError, "timed out"):
                    extract_audio(source, output, stream_index=1, timeout_seconds=1)

            self.assertEqual(output.read_bytes(), b"existing-final")
            self.assertEqual(list(root.glob("*.partial.wav")), [])

    def test_extract_audio_rejects_header_only_wav_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "video.mkv"
            source.write_bytes(b"video")
            output = root / "audio.wav"

            def header_only(command: list[str], **_kwargs: object) -> SimpleNamespace:
                _write_pcm_wave(Path(command[-1]), duration_seconds=0)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("audio._resolve_ffmpeg", return_value="ffmpeg"),
                patch("audio.subprocess.run", side_effect=header_only),
                patch("audio.probe_media_duration", return_value=1.0),
            ):
                with self.assertRaisesRegex(AudioExtractionError, "no audio frames"):
                    extract_audio(source, output, stream_index=1)

            self.assertFalse(output.exists())
            self.assertFalse(audio_cache_metadata_path(output).exists())
            self.assertEqual(list(root.glob("*.partial.wav")), [])

    def test_extract_audio_rejects_source_duration_mismatch_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "video.mkv"
            source.write_bytes(b"video")
            output = root / "audio.wav"

            def short_audio(command: list[str], **_kwargs: object) -> SimpleNamespace:
                _write_pcm_wave(Path(command[-1]), duration_seconds=1.0)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("audio._resolve_ffmpeg", return_value="ffmpeg"),
                patch("audio.subprocess.run", side_effect=short_audio),
                patch("audio.probe_media_duration", return_value=1.0),
            ):
                with self.assertRaisesRegex(AudioExtractionError, "does not match source duration"):
                    extract_audio(
                        source,
                        output,
                        stream_index=1,
                        source_duration_seconds=120.0,
                    )

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob("*.partial.wav")), [])

    def test_extract_audio_replace_failure_restores_previous_metadata_and_final(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "video.mkv"
            source.write_bytes(b"video")
            output = root / "audio.wav"
            _extract_valid_test_audio(source, output, fill=b"\x01\x00")
            previous_audio = output.read_bytes()
            metadata_path = audio_cache_metadata_path(output)
            previous_metadata = metadata_path.read_bytes()
            real_replace = os.replace

            def fail_final_replace(source_path: str | bytes, destination_path: str | bytes) -> None:
                if str(source_path).endswith(".partial.wav"):
                    raise PermissionError("injected final replace failure")
                real_replace(source_path, destination_path)

            def write_replacement(command: list[str], **_kwargs: object) -> SimpleNamespace:
                _write_pcm_wave(Path(command[-1]), fill=b"\x02\x00")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("audio._resolve_ffmpeg", return_value="ffmpeg"),
                patch("audio.subprocess.run", side_effect=write_replacement),
                patch("audio.probe_media_duration", return_value=1.0),
                patch("audio.os.replace", side_effect=fail_final_replace),
            ):
                with self.assertRaisesRegex(AudioExtractionError, "injected final replace failure"):
                    extract_audio(
                        source,
                        output,
                        stream_index=1,
                        source_duration_seconds=1.0,
                    )

            self.assertEqual(output.read_bytes(), previous_audio)
            self.assertEqual(metadata_path.read_bytes(), previous_metadata)
            self.assertTrue(validate_cached_audio(output, source, stream_index=1))
            self.assertEqual(list(root.glob("*.partial.wav")), [])

    def test_next_extraction_cleans_sigkill_orphan_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "video.mkv"
            source.write_bytes(b"video")
            output = root / "audio.wav"
            orphan = _new_audio_partial_path(output)
            orphan.write_bytes(b"orphan-from-killed-process")

            _extract_valid_test_audio(source, output)

            self.assertFalse(orphan.exists())
            self.assertEqual(cleanup_audio_partials(output), 0)
            self.assertTrue(validate_cached_audio(output, source, stream_index=1))

    def test_killed_extractor_never_overwrites_final_and_orphan_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "video.mkv"
            source.write_bytes(b"video")
            output = root / "audio.wav"
            output.write_bytes(b"existing-final")
            ready = root / "partial-ready"
            child_code = "\n".join(
                [
                    "from pathlib import Path",
                    "import sys, time",
                    "from types import SimpleNamespace",
                    "import audio",
                    "root = Path(sys.argv[1])",
                    "source = root / 'video.mkv'",
                    "output = root / 'audio.wav'",
                    "ready = root / 'partial-ready'",
                    "def fake_run(command, **kwargs):",
                    "    Path(command[-1]).write_bytes(b'partial-from-killed-extractor')",
                    "    ready.write_text('ready', encoding='utf-8')",
                    "    time.sleep(60)",
                    "    return SimpleNamespace(returncode=0, stdout='', stderr='')",
                    "audio._resolve_ffmpeg = lambda: 'ffmpeg'",
                    "audio.probe_media_duration = lambda _path: 1.0",
                    "audio.subprocess.run = fake_run",
                    "audio.extract_audio(source, output, stream_index=1, source_duration_seconds=1.0)",
                ]
            )
            process = subprocess.Popen(
                [sys.executable, "-c", child_code, str(root)],
                cwd=Path(__file__).resolve().parent,
            )
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "child did not create its extraction partial")
                process.kill()
                process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)

            self.assertEqual(output.read_bytes(), b"existing-final")
            self.assertEqual(len(list(root.glob("*.partial.wav"))), 1)
            self.assertEqual(cleanup_audio_partials(output), 1)
            self.assertEqual(list(root.glob("*.partial.wav")), [])

    def test_validate_cached_audio_rejects_truncated_and_header_only_wavs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid = root / "valid.wav"
            _write_pcm_wave(valid)
            self.assertTrue(validate_cached_audio(valid))

            truncated = root / "truncated.wav"
            truncated.write_bytes(valid.read_bytes()[:-100])
            self.assertFalse(validate_cached_audio(truncated))

            header_only = root / "header-only.wav"
            _write_pcm_wave(header_only, duration_seconds=0)
            self.assertFalse(validate_cached_audio(header_only))

    def test_validate_cached_audio_requires_current_identity_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "video.mkv"
            source.write_bytes(b"video")
            output = root / "audio.wav"
            _extract_valid_test_audio(source, output, stream_index=2)
            metadata = audio_cache_metadata_path(output)

            self.assertTrue(validate_cached_audio(output, source, stream_index=2))
            self.assertFalse(validate_cached_audio(output, source, stream_index=3))

            saved_metadata = metadata.read_bytes()
            metadata.unlink()
            self.assertFalse(validate_cached_audio(output, source, stream_index=2))
            metadata.write_bytes(saved_metadata)

            source.write_bytes(b"changed-source-identity")
            self.assertFalse(validate_cached_audio(output, source, stream_index=2))

    def test_validate_cached_audio_rejects_hash_bound_audio_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "video.mkv"
            source.write_bytes(b"video")
            output = root / "audio.wav"
            _extract_valid_test_audio(source, output, fill=b"\x01\x00")
            self.assertTrue(validate_cached_audio(output, source, stream_index=1))

            _write_pcm_wave(output, fill=b"\x02\x00")

            self.assertFalse(validate_cached_audio(output, source, stream_index=1))

    def test_find_demucs_vocal_stem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vocal = root / "htdemucs" / "episode" / "vocals.wav"
            vocal.parent.mkdir(parents=True)
            vocal.write_bytes(b"wav")

            self.assertEqual(_find_demucs_stem(root, Path("episode.wav"), "vocals"), vocal)


if __name__ == "__main__":
    unittest.main()
