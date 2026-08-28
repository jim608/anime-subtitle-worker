from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import json
import logging
import subprocess
import tempfile
import time
from typing import Any
import wave

from audio import (
    AudioStreamInfo,
    _resolve_ffmpeg,
    extract_audio_stream_sample,
    probe_audio_streams,
    probe_media_duration,
)
from config import AppConfig
from transcriber import TranscriptionError, _add_nvidia_dll_directories, _build_vad_parameters
from whisper_runtime import get_whisper_model


@dataclass(frozen=True)
class LanguageDetectionSample:
    language: str
    probability: float
    start_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class LanguageDetectionResult:
    language: str
    probability: float
    allowed: bool
    confident: bool
    source: str
    reason: str = ""
    samples: list[LanguageDetectionSample] | None = None


@dataclass(frozen=True)
class AudioStreamLanguageDetection:
    stream: AudioStreamInfo
    result: LanguageDetectionResult

    def to_dict(self) -> dict[str, Any]:
        return {"stream": self.stream.to_dict(), "result": asdict(self.result)}


@dataclass(frozen=True)
class AudioStreamSelection:
    selected: AudioStreamInfo | None
    detections: list[AudioStreamLanguageDetection]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected.to_dict() if self.selected is not None else None,
            "detections": [item.to_dict() for item in self.detections],
            "reason": self.reason,
        }


class LanguageDetector:
    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger

    def detect(
        self,
        audio_path: str | Path,
        video_path: str | Path,
        *,
        cache_variant: str | int | None = None,
        force_refresh: bool = False,
    ) -> LanguageDetectionResult:
        audio = Path(audio_path)
        video = Path(video_path)
        variant = str(cache_variant or "default")
        cached = None if force_refresh else self._read_cached(video, variant)
        if cached is not None:
            return cached

        result = self._detect_with_faster_whisper(audio)
        self._write_cached(video, result, variant)
        return result

    def select_japanese_audio_stream(self, video_path: str | Path) -> AudioStreamSelection:
        video = Path(video_path)
        streams = probe_audio_streams(video)
        max_streams = max(1, int(getattr(self.config, "audio_content_probe_max_streams", 8) or 8))
        allowed_languages = set(_normalized_allowed_languages(self.config))
        streams = sorted(
            streams,
            key=lambda stream: (
                0 if stream.commentary else 1,
                1 if _normalize_language(stream.language) in allowed_languages else 0,
                1 if stream.default else 0,
                int(stream.channels or 0),
                -stream.index,
            ),
            reverse=True,
        )[:max_streams]
        if len(streams) <= 1:
            return AudioStreamSelection(streams[0] if streams else None, [], "single_or_missing_stream")

        _add_nvidia_dll_directories(self.logger)
        model_name = getattr(self.config, "language_detect_model", None) or self.config.whisper_model
        model = get_whisper_model(
            model_name,
            device=self.config.whisper_device,
            compute_type=self.config.whisper_compute_type,
            cache_enabled=bool(getattr(self.config, "whisper_model_cache_enabled", True)),
            logger=self.logger,
        )
        duration = probe_media_duration(video)
        sample_count = max(1, int(getattr(self.config, "audio_content_probe_sample_count", 3) or 3))
        sample_seconds = max(5, int(getattr(self.config, "audio_content_probe_sample_seconds", 12) or 12))
        specs = _sample_specs(duration, sample_count, sample_seconds)
        detections: list[AudioStreamLanguageDetection] = []

        with tempfile.TemporaryDirectory(prefix="anime-subtitle-stream-lang-") as temp_dir:
            root = Path(temp_dir)
            for stream in streams:
                samples: list[LanguageDetectionSample] = []
                for sample_index, (start, seconds) in enumerate(specs, start=1):
                    sample_path = root / f"stream-{stream.index}-sample-{sample_index}.wav"
                    try:
                        extract_audio_stream_sample(
                            video,
                            sample_path,
                            stream_index=stream.index,
                            start_seconds=start,
                            duration_seconds=int(seconds or sample_seconds),
                        )
                        language, probability = self._detect_single_sample(model, sample_path)
                    except Exception as exc:  # noqa: BLE001 - one bad stream must not block other streams.
                        self.logger.warning(
                            "Audio stream language sample failed video=%s stream=%s start=%.1fs error=%s",
                            video,
                            stream.index,
                            start,
                            exc,
                        )
                        continue
                    samples.append(
                        LanguageDetectionSample(
                            language=language or "unknown",
                            probability=probability,
                            start_seconds=start,
                            duration_seconds=float(seconds or sample_seconds),
                        )
                    )
                if samples:
                    detections.append(AudioStreamLanguageDetection(stream, _aggregate_samples(samples, self.config)))

        allowed = [item for item in detections if item.result.allowed]
        if not allowed:
            return AudioStreamSelection(None, detections, "no_japanese_stream_detected")
        selected_detection = max(
            allowed,
            key=lambda item: (
                0 if item.stream.commentary else 1,
                sum(1 for sample in (item.result.samples or []) if sample.language == "ja"),
                item.result.probability,
                1 if item.stream.default else 0,
                -item.stream.index,
            ),
        )
        return AudioStreamSelection(selected_detection.stream, detections, "content_detected_japanese")

    def _detect_with_faster_whisper(self, audio_path: Path) -> LanguageDetectionResult:
        _add_nvidia_dll_directories(self.logger)

        try:
            model_name = getattr(self.config, "language_detect_model", None) or self.config.whisper_model
            model = get_whisper_model(
                model_name,
                device=self.config.whisper_device,
                compute_type=self.config.whisper_compute_type,
                cache_enabled=bool(getattr(self.config, "whisper_model_cache_enabled", True)),
                logger=self.logger,
            )
            samples = self._detect_samples(model, audio_path)
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"Whisper language detection failed for {audio_path}: {exc}") from exc

        return _aggregate_samples(samples, self.config)

    def _detect_samples(self, model: Any, audio_path: Path) -> list[LanguageDetectionSample]:
        sample_count = max(1, int(getattr(self.config, "language_detect_sample_count", 3)))
        sample_seconds = max(1, int(getattr(self.config, "language_detect_sample_seconds", 30)))
        duration = _wav_duration_seconds(audio_path)
        specs = _sample_specs(duration, sample_count, sample_seconds)
        detected: list[LanguageDetectionSample] = []

        with tempfile.TemporaryDirectory(prefix="anime-subtitle-lang-") as temp_dir:
            temp_root = Path(temp_dir)
            for index, (start, seconds) in enumerate(specs, start=1):
                sample_path = audio_path
                if seconds is not None:
                    sample_path = temp_root / f"sample-{index}.wav"
                    _extract_audio_sample(audio_path, sample_path, start, seconds)
                language, probability = self._detect_single_sample(model, sample_path)
                detected.append(
                    LanguageDetectionSample(
                        language=language or "unknown",
                        probability=probability,
                        start_seconds=start,
                        duration_seconds=float(seconds or duration or sample_seconds),
                    )
                )

        if not detected:
            detected.append(LanguageDetectionSample("unknown", 0.0, 0.0, 0.0))
        return detected

    def _detect_single_sample(self, model: Any, sample_path: Path) -> tuple[str, float]:
        segments, info = model.transcribe(
            str(sample_path),
            language=None,
            task="transcribe",
            vad_filter=bool(getattr(self.config, "whisper_vad_filter", False)),
            vad_parameters=_build_vad_parameters(self.config),
            condition_on_previous_text=False,
            temperature=0,
            beam_size=1,
            best_of=1,
            word_timestamps=False,
            no_speech_threshold=getattr(self.config, "whisper_no_speech_threshold", None),
            log_prob_threshold=getattr(self.config, "whisper_log_prob_threshold", None),
            compression_ratio_threshold=getattr(self.config, "whisper_compression_ratio_threshold", None),
        )
        # Some faster-whisper versions delay work until the generator is consumed.
        try:
            next(iter(segments), None)
        except StopIteration:
            pass
        language = _normalize_language(getattr(info, "language", "") or "")
        probability = _safe_probability(getattr(info, "language_probability", 0.0))
        return language, probability

    def _read_cached(self, video_path: Path, cache_variant: str = "default") -> LanguageDetectionResult | None:
        if not bool(getattr(self.config, "language_detect_cache_enabled", True)):
            return None
        cache_path = _resolve_cache_path(self.config, "language_detect_cache_path", "language_detection_cache.json")
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        item = payload.get("items", {}).get(_cache_key(video_path, self.config, cache_variant))
        if not isinstance(item, dict):
            return None
        try:
            return _result_from_cached_item(item)
        except (TypeError, ValueError):
            return None

    def _write_cached(
        self,
        video_path: Path,
        result: LanguageDetectionResult,
        cache_variant: str = "default",
    ) -> None:
        if not bool(getattr(self.config, "language_detect_cache_enabled", True)):
            return
        cache_path = _resolve_cache_path(self.config, "language_detect_cache_path", "language_detection_cache.json")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {"version": 2, "items": {}}
            payload["version"] = 2
            payload.setdefault("items", {})[_cache_key(video_path, self.config, cache_variant)] = {
                **asdict(result),
                "video_path": str(video_path),
                "cache_variant": cache_variant,
                "updated_at": time.time(),
            }
            _atomic_write_json(cache_path, payload)
        except OSError as exc:
            self.logger.debug("Failed to write language detection cache %s: %s", cache_path, exc)


def should_skip_for_language(result: LanguageDetectionResult, config: AppConfig) -> bool:
    if not bool(getattr(config, "skip_non_allowed_language", True)):
        return False
    if result.allowed:
        return False
    if result.confident:
        return True
    return str(getattr(config, "language_uncertain_policy", "skip")).lower() == "skip"


def should_fail_for_language(result: LanguageDetectionResult, config: AppConfig) -> bool:
    if not bool(getattr(config, "skip_non_allowed_language", True)):
        return False
    if result.allowed or result.confident:
        return False
    return str(getattr(config, "language_uncertain_policy", "skip")).lower() == "fail"


def format_language_result(result: LanguageDetectionResult, config: AppConfig) -> str:
    allowed = ",".join(_normalized_allowed_languages(config))
    return (
        "Detected source language: "
        f"reason={result.reason or '-'} "
        f"language={result.language} "
        f"probability={result.probability:.2f} "
        f"allowed={allowed or '-'} "
        f"confident={int(result.confident)} "
        f"samples={_format_samples(result.samples)}"
    )


def format_language_skip(result: LanguageDetectionResult, config: AppConfig) -> str:
    allowed = ",".join(_normalized_allowed_languages(config))
    return (
        "Skipped source language gate: "
        f"reason={result.reason or '-'} "
        f"language={result.language} "
        f"probability={result.probability:.2f} "
        f"allowed={allowed or '-'} "
        f"confident={int(result.confident)} "
        f"policy={getattr(config, 'language_uncertain_policy', 'skip')} "
        f"samples={_format_samples(result.samples)}"
    )


def _aggregate_samples(samples: list[LanguageDetectionSample], config: AppConfig) -> LanguageDetectionResult:
    allowed_languages = _normalized_allowed_languages(config)
    min_probability = float(getattr(config, "language_detect_min_probability", 0.70))
    best = max(samples, key=lambda sample: sample.probability, default=LanguageDetectionSample("unknown", 0.0, 0.0, 0.0))
    confident_samples = [sample for sample in samples if sample.probability >= min_probability]
    allowed_confident = [sample for sample in confident_samples if sample.language in allowed_languages]
    blocked_confident = [sample for sample in confident_samples if sample.language not in allowed_languages]
    best_allowed = max(allowed_confident, key=lambda sample: sample.probability, default=None)
    best_blocked = max(blocked_confident, key=lambda sample: sample.probability, default=None)
    allowed = bool(best_allowed and len(allowed_confident) > len(blocked_confident))
    confident = bool(confident_samples)

    if allowed and best_allowed is not None:
        language = best_allowed.language
        probability = best_allowed.probability
        reason = "allowed_language_detected"
    elif blocked_confident and len(blocked_confident) > len(allowed_confident):
        language = best_blocked.language if best_blocked else best.language
        probability = best_blocked.probability if best_blocked else best.probability
        reason = "non_allowed_language_detected"
        confident = True
    else:
        language = best.language or "unknown"
        probability = best.probability
        reason = "language_uncertain"
        confident = False

    return LanguageDetectionResult(
        language=language or "unknown",
        probability=probability,
        allowed=allowed,
        confident=confident,
        source="faster-whisper_multi_sample",
        reason=reason,
        samples=samples,
    )


def _result_from_cached_item(item: dict[str, Any]) -> LanguageDetectionResult:
    samples: list[LanguageDetectionSample] = []
    sample_items = item.get("samples")
    if isinstance(sample_items, list):
        for sample in sample_items:
            if not isinstance(sample, dict):
                continue
            samples.append(
                LanguageDetectionSample(
                    language=str(sample.get("language") or "unknown"),
                    probability=_safe_probability(sample.get("probability")),
                    start_seconds=_safe_float(sample.get("start_seconds")),
                    duration_seconds=_safe_float(sample.get("duration_seconds")),
                )
            )
    return LanguageDetectionResult(
        language=str(item.get("language") or "unknown"),
        probability=_safe_probability(item.get("probability")),
        allowed=bool(item.get("allowed")),
        confident=bool(item.get("confident")),
        source=str(item.get("source") or "cache"),
        reason=str(item.get("reason") or ""),
        samples=samples or None,
    )


def _sample_specs(duration: float | None, count: int, sample_seconds: int) -> list[tuple[float, int | None]]:
    if duration is None or duration <= 0:
        return [(0.0, None)]
    if duration <= sample_seconds or count <= 1:
        return [(0.0, min(sample_seconds, int(max(1, duration))))]

    centers = _sample_centers(duration, count)
    starts: list[float] = []
    for center in centers:
        start = min(max(0.0, center - sample_seconds / 2), max(0.0, duration - sample_seconds))
        rounded = round(start, 2)
        if rounded not in starts:
            starts.append(rounded)
    return [(start, sample_seconds) for start in starts] or [(0.0, sample_seconds)]


def _sample_centers(duration: float, count: int) -> list[float]:
    if count <= 1:
        return [duration / 2]
    return [duration * (index + 1) / (count + 1) for index in range(count)]


def _wav_duration_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            if frame_rate <= 0:
                return None
            return wav_file.getnframes() / float(frame_rate)
    except (OSError, wave.Error):
        return None


def _extract_audio_sample(source: Path, output: Path, start_seconds: float, duration_seconds: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _resolve_ffmpeg(),
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        str(duration_seconds),
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(output),
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise TranscriptionError(f"Language detection sample extraction failed for {source}: {stderr}")


def _format_samples(samples: list[LanguageDetectionSample] | None) -> str:
    if not samples:
        return "-"
    return ",".join(f"{sample.language}:{sample.probability:.2f}@{int(sample.start_seconds)}s" for sample in samples)


def _normalized_allowed_languages(config: AppConfig) -> list[str]:
    return [_normalize_language(item) for item in getattr(config, "allowed_source_languages", ["ja"]) if item]


def _normalize_language(language: str) -> str:
    value = str(language or "").strip().lower().replace("_", "-")
    aliases = {
        "jpn": "ja",
        "jp": "ja",
        "japanese": "ja",
        "eng": "en",
        "english": "en",
        "kor": "ko",
        "korean": "ko",
        "cmn": "zh",
        "chi": "zh",
        "zho": "zh",
        "chinese": "zh",
    }
    return aliases.get(value, value)


def _safe_probability(value: Any) -> float:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, probability))


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _cache_key(video_path: Path, config: AppConfig, cache_variant: str = "default") -> str:
    try:
        stat = video_path.stat()
        size = stat.st_size
        mtime_ns = stat.st_mtime_ns
    except OSError:
        size = 0
        mtime_ns = 0
    raw = "|".join(
        [
            str(video_path.resolve()),
            str(size),
            str(mtime_ns),
            str(getattr(config, "language_detect_model", None) or config.whisper_model),
            str(config.whisper_device),
            str(getattr(config, "language_detect_sample_count", 3)),
            str(getattr(config, "language_detect_sample_seconds", 30)),
            str(getattr(config, "language_detect_min_probability", 0.70)),
            str(cache_variant or "default"),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _resolve_cache_path(config: AppConfig, attr: str, default_name: str) -> Path:
    configured = Path(str(getattr(config, attr, default_name) or default_name))
    if configured.is_absolute():
        return configured
    return config.work_path / configured


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
