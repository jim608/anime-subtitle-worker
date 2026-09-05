from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import glob
import hashlib
import json
import re

from config import AppConfig
from subtitle_extract import (
    SIDECAR_SUBTITLE_EXTENSIONS,
    classify_subtitle_content_file,
)


@dataclass(frozen=True)
class SubtitlePaths:
    ja_srt: Path
    zh_cn_srt: Path
    zh_tw_srt: Path
    ai_ja_ass: Path
    ai_zh_cn_ass: Path
    ai_zh_tw_ass: Path


@dataclass(frozen=True)
class SourceTranscriptPaths:
    srt: Path
    ass: Path
    language: str


def paths_for_video(video_path: str | Path, config: AppConfig) -> SubtitlePaths:
    video = Path(video_path)
    srt_base = _ai_srt_cache_base(video, config)
    return SubtitlePaths(
        ja_srt=_with_base_suffix(srt_base, _srt_suffix_from_ass_suffix(config.ai_japanese_ass_suffix)),
        zh_cn_srt=_with_base_suffix(srt_base, _srt_suffix_from_ass_suffix(config.ai_simplified_chinese_ass_suffix)),
        zh_tw_srt=_with_base_suffix(srt_base, _srt_suffix_from_ass_suffix(config.ai_traditional_chinese_ass_suffix)),
        ai_ja_ass=_with_video_suffix(video, config.ai_japanese_ass_suffix),
        ai_zh_cn_ass=_with_video_suffix(video, config.ai_simplified_chinese_ass_suffix),
        ai_zh_tw_ass=_with_video_suffix(video, config.ai_traditional_chinese_ass_suffix),
    )


def source_transcript_paths_for_video(video_path: str | Path, config: AppConfig, language: str) -> SourceTranscriptPaths:
    video = Path(video_path)
    normalized_language = _safe_language_tag(language)
    ass_suffix = source_transcript_ass_suffix_for_language(config, normalized_language)
    srt_base = _ai_srt_cache_base(video, config)
    return SourceTranscriptPaths(
        srt=_with_base_suffix(srt_base, _srt_suffix_from_ass_suffix(ass_suffix)),
        ass=_with_video_suffix(video, ass_suffix),
        language=normalized_language,
    )


def source_transcript_artifacts_for_video(
    video_path: str | Path,
    config: AppConfig,
) -> list[Path]:
    """Return existing dynamic-language source SRT/ASS artifacts for one video."""

    video = Path(video_path)
    template = str(
        getattr(
            config,
            "ai_source_transcript_ass_suffix_template",
            ".AI{label}.{language}.ass",
        )
    )
    suffix_pattern = template.replace("{language}", "*").replace("{label}", "*")
    srt_base = _ai_srt_cache_base(video, config)
    canonical = paths_for_video(video, config)
    normal_ai_outputs = {
        canonical.ja_srt,
        canonical.zh_cn_srt,
        canonical.zh_tw_srt,
        canonical.ai_ja_ass,
        canonical.ai_zh_cn_ass,
        canonical.ai_zh_tw_ass,
    }
    searches = (
        (video.parent, f"{glob.escape(video.stem)}{suffix_pattern}"),
        (
            srt_base.parent,
            f"{glob.escape(srt_base.name)}{_srt_suffix_from_ass_suffix(suffix_pattern)}",
        ),
    )
    artifacts: set[Path] = set()
    for parent, pattern in searches:
        try:
            artifacts.update(
                path
                for path in parent.glob(pattern)
                if path.is_file() and path not in normal_ai_outputs
            )
        except OSError:
            continue
    return sorted(artifacts, key=lambda path: str(path).casefold())


def finished_subtitle_paths(video_path: str | Path, config: AppConfig) -> list[Path]:
    video = Path(video_path)
    paths = paths_for_video(video, config)
    suffixes = [config.ai_traditional_chinese_ass_suffix, *config.finished_subtitle_suffixes]
    finished_paths = [_with_video_suffix(video, suffix) for suffix in dict.fromkeys(suffixes)]
    if not config.export_ai_ass:
        finished_paths.append(paths.zh_tw_srt)
        finished_paths.append(_with_video_suffix(video, ".zh-TW.srt"))

    return list(dict.fromkeys(finished_paths))


def has_finished_subtitle(video_path: str | Path, config: AppConfig) -> bool:
    paths = paths_for_video(video_path, config)
    for path in finished_subtitle_paths(video_path, config):
        if not path.exists():
            continue
        if (
            config.export_ai_ass
            and bool(getattr(config, "ass_style_versioning_enabled", False))
            and path == paths.ai_zh_tw_ass
        ):
            from ass_utils import ass_style_from_config, ass_style_is_current

            if ass_style_is_current(paths.ai_zh_tw_ass, ass_style_from_config(config)):
                return _is_usable_traditional_chinese_subtitle(path, config, allow_ai=True)
            continue
        if _is_usable_traditional_chinese_subtitle(
            path,
            config,
            allow_ai=path == paths.ai_zh_tw_ass,
        ):
            if (
                path != paths.ai_zh_tw_ass
                and bool(getattr(config, "source_analyzer_enabled", False))
            ):
                # Legacy text-only QC cannot establish that an official
                # sidecar covers this video. Keep the AI publication contract
                # unchanged, but apply the existing M2 import/source policy
                # before an official sidecar can suppress Worker admission.
                return has_ai_finished_subtitle(video_path, config) or _has_verified_official_traditional_subtitle(
                    Path(video_path), config
                )
            return True
    return _has_traditional_chinese_sidecar(video_path, config)


def has_ai_finished_subtitle(video_path: str | Path, config: AppConfig) -> bool:
    from output_manifest import (
        manifest_publication_semantics,
        output_manifest_path,
        output_publication_marker_path,
        validate_output_manifest,
    )

    if output_publication_marker_path(video_path, config).exists():
        return False
    manifest = output_manifest_path(video_path, config)
    paths = paths_for_video(video_path, config)
    if config.export_ai_ass:
        if manifest.exists():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            publication = (
                manifest_publication_semantics(payload)
                if isinstance(payload, dict)
                else None
            )
            if publication is not None:
                if not validate_output_manifest(
                    video_path,
                    config,
                    require_publication_semantics=True,
                ):
                    return False
            elif not validate_output_manifest(
                video_path,
                config,
                required_outputs=(
                    paths.ai_ja_ass,
                    paths.ai_zh_cn_ass,
                    paths.ai_zh_tw_ass,
                ),
            ):
                return False
        if not paths.ai_zh_tw_ass.exists():
            return False
        if not _is_usable_traditional_chinese_subtitle(paths.ai_zh_tw_ass, config, allow_ai=True):
            return False
        if bool(getattr(config, "ass_style_versioning_enabled", False)):
            from ass_utils import ass_style_from_config, ass_style_is_current

            return ass_style_is_current(paths.ai_zh_tw_ass, ass_style_from_config(config))
        return True
    return paths.zh_tw_srt.exists() and _is_usable_traditional_chinese_subtitle(
        paths.zh_tw_srt,
        config,
        allow_ai=True,
    )


def ai_finished_subtitle_mtime(video_path: str | Path, config: AppConfig) -> float | None:
    video = Path(video_path)
    paths = paths_for_video(video, config)
    candidates: list[Path] = []

    if config.export_ai_ass:
        if paths.ai_zh_tw_ass.exists():
            if bool(getattr(config, "ass_style_versioning_enabled", False)):
                from ass_utils import ass_style_from_config, ass_style_is_current

                if ass_style_is_current(paths.ai_zh_tw_ass, ass_style_from_config(config)):
                    candidates.append(paths.ai_zh_tw_ass)
            else:
                candidates.append(paths.ai_zh_tw_ass)
    else:
        candidates.append(paths.zh_tw_srt)

    mtimes: list[float] = []
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            mtimes.append(candidate.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else None


def _with_video_suffix(video: Path, suffix: str) -> Path:
    return video.with_name(f"{video.stem}{suffix}")


def _with_base_suffix(base: Path, suffix: str) -> Path:
    return base.with_name(f"{base.name}{suffix}")


def _ai_srt_cache_base(video: Path, config: AppConfig) -> Path:
    digest = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:16]
    safe_stem = _safe_cache_name(video.stem)
    return Path(config.work_path) / "ai_srt_cache" / f"{safe_stem}.{digest}"


def _safe_cache_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned[:80] or "video"


def _srt_suffix_from_ass_suffix(suffix: str) -> str:
    if suffix.casefold().endswith(".ass"):
        return f"{suffix[:-4]}.srt"
    if suffix.casefold().endswith(".srt"):
        return suffix
    return f"{suffix}.srt"


def source_transcript_ass_suffix_for_language(config: AppConfig, language: str) -> str:
    tag = _safe_language_tag(language)
    template = str(getattr(config, "ai_source_transcript_ass_suffix_template", ".AI{label}.{language}.ass"))
    return template.replace("{language}", tag).replace("{label}", _source_language_label(tag))


def _has_source_transcript_output(video_path: str | Path, config: AppConfig) -> bool:
    return bool(_source_transcript_outputs(video_path, config))


def _source_transcript_outputs(video_path: str | Path, config: AppConfig) -> list[Path]:
    """Return non-translation source transcripts without matching normal AI ASS files.

    The configurable template is intentionally broad (for example
    ``.AI{label}.{language}.ass``).  Its glob also matches the canonical
    Japanese and Chinese outputs, so those paths must be excluded explicitly;
    otherwise a lone Japanese ASS can incorrectly make an unfinished three-file
    AI publication look complete.
    """

    video = Path(video_path)
    template = str(getattr(config, "ai_source_transcript_ass_suffix_template", ".AI{label}.{language}.ass"))
    if "{language}" not in template and "{label}" not in template:
        return []
    suffix_pattern = template.replace("{language}", "*").replace("{label}", "*")
    pattern = f"{glob.escape(video.stem)}{suffix_pattern}"
    canonical = paths_for_video(video, config)
    normal_ai_outputs = {
        canonical.ai_ja_ass,
        canonical.ai_zh_cn_ass,
        canonical.ai_zh_tw_ass,
    }
    generated_language_tags = {"ja", "jpn", "zh", "zho", "cmn", "zh-cn", "zh-tw"}
    try:
        return [
            path
            for path in video.parent.glob(pattern)
            if path.is_file()
            and path not in normal_ai_outputs
            and (
                (language := _source_transcript_language_from_name(video, path, template)) is not None
                and language not in generated_language_tags
            )
        ]
    except OSError:
        return []


def _source_transcript_language_from_name(video: Path, subtitle: Path, template: str) -> str | None:
    if "{language}" not in template:
        return None
    suffix_expression = re.escape(template)
    suffix_expression = suffix_expression.replace(re.escape("{label}"), r".+?")
    suffix_expression = suffix_expression.replace(
        re.escape("{language}"),
        r"(?P<language>[A-Za-z0-9_-]+)",
    )
    match = re.fullmatch(re.escape(video.stem) + suffix_expression, subtitle.name, flags=re.IGNORECASE)
    if match is None:
        return None
    return _safe_language_tag(match.group("language"))


def _safe_language_tag(language: str) -> str:
    tag = str(language or "unknown").strip().lower().replace("_", "-")
    tag = re.sub(r"[^a-z0-9-]+", "-", tag).strip("-")
    return tag or "unknown"


def _source_language_label(language: str) -> str:
    tag = _safe_language_tag(language)
    labels = {
        "ja": "日本語",
        "jpn": "日本語",
        "en": "English",
        "eng": "English",
        "ko": "한국어",
        "kor": "한국어",
        "zh": "中文",
        "zh-cn": "简体中文",
        "zh-tw": "繁體中文",
        "es": "Español",
        "fr": "Français",
        "de": "Deutsch",
        "it": "Italiano",
        "pt": "Português",
        "ru": "Русский",
    }
    return labels.get(tag, tag.upper())


def _has_traditional_chinese_sidecar(video_path: str | Path, config: AppConfig) -> bool:
    video = Path(video_path)
    for subtitle in video.parent.glob(f"{video.stem}.*"):
        if not subtitle.is_file() or subtitle.suffix.lower() not in SIDECAR_SUBTITLE_EXTENSIONS:
            continue
        if _is_usable_traditional_chinese_subtitle(subtitle, config):
            if bool(getattr(config, "source_analyzer_enabled", False)):
                return _has_verified_official_traditional_subtitle(video, config)
            return True
    return False


def _has_verified_official_traditional_subtitle(video: Path, config: AppConfig) -> bool:
    from subtitle_extract import verified_official_subtitle_languages

    try:
        return "zh-tw" in verified_official_subtitle_languages(video, config)
    except Exception:
        # An unavailable probe is not proof of completion. Preserve admission
        # so the durable source decision can retry/review through normal policy.
        return False


def _is_usable_traditional_chinese_subtitle(
    subtitle: Path,
    config: AppConfig,
    *,
    allow_ai: bool = False,
) -> bool:
    try:
        if not subtitle.is_file() or subtitle.stat().st_size <= 0:
            return False
        detected_language = classify_subtitle_content_file(subtitle).language
        if detected_language != "zh-tw":
            return False
        if allow_ai:
            # AI filenames are deliberately excluded by the sidecar classifier.
            # The canonical Traditional-Chinese AI path is selected by the caller,
            # while content-only language classification and translated-output QC
            # jointly prevent a mislabeled non-Chinese artifact from completing
            # the Traditional-Chinese delivery contract.
            role = "translated"
        else:
            role = "unknown"
        from subtitle_quality import analyze_subtitle_file

        report = analyze_subtitle_file(subtitle, config, role=role)
        return not report.has_failures and report.dialogues > 0
    except Exception:
        return False
