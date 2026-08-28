from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from processing_provenance import processing_config_signature
from safe_files import atomic_write_text, sha256_file
from scan_state import ai_delivery_identity


MANIFEST_SCHEMA_VERSION = 2
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = frozenset({1, MANIFEST_SCHEMA_VERSION})
DELIVERY_EVIDENCE_CONTRACT = "ai-delivery-v1"
PUBLICATION_SEMANTICS_CONTRACT = "ai-publication-semantics-v2"
SOURCE_TRANSCRIPTION_PROVENANCE_CONTRACT = "ai-source-transcription-v1"
TRANSLATED_PUBLICATION_KIND = "translated_trilingual"
SOURCE_LANGUAGE_PUBLICATION_KIND = "source_language"
ADOPTED_ZH_TW_PUBLICATION_KIND = "adopted_zh_tw"
CONVERTED_ZH_CN_PUBLICATION_KIND = "converted_zh_cn"
TRANSLATED_OUTPUT_LANGUAGES = ("ja", "zh-CN", "zh-TW")
TRADITIONAL_CHINESE_OUTPUT_LANGUAGES = ("zh-TW",)
_NON_DELIVERABLE_SOURCE_LANGUAGES = frozenset(
    {"", "auto", "ja", "jpn", "und", "unknown"}
)


def output_manifest_root(config: Any) -> Path:
    configured = Path(str(getattr(config, "ai_output_manifest_path", "ai_output_manifests") or "ai_output_manifests"))
    if configured.is_absolute():
        return configured
    return Path(config.work_path) / configured


def output_manifest_path(video: str | Path, config: Any) -> Path:
    normalized = str(Path(video).resolve())
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()
    return output_manifest_root(config) / digest[:2] / f"{digest}.json"


def output_publication_marker_path(video: str | Path, config: Any) -> Path:
    path = output_manifest_path(video, config)
    return path.with_suffix(".publishing")


def begin_output_publication(video: str | Path, config: Any) -> Path:
    marker = output_publication_marker_path(video, config)
    atomic_write_text(
        marker,
        json.dumps({"video": str(Path(video)), "started_at": time.time()}, ensure_ascii=False) + "\n",
    )
    return marker


def delivery_identity(video: str | Path, config: Any) -> dict[str, Any]:
    """Build the stable identity used by the delivery SLO and manifest.

    A retry of the same media revision under the same processing policy returns
    the same obligation id.  Replacing the media or changing a semantic AI
    policy produces a new obligation instead of rewriting historical results.
    """

    path = Path(video)
    stat = path.stat()
    policy_revision = processing_config_signature(config)
    identity = ai_delivery_identity(
        path,
        media_size=int(stat.st_size),
        media_mtime_ns=int(stat.st_mtime_ns),
        policy_revision=policy_revision,
    )
    return {
        "obligation_id": identity["obligation_id"],
        "policy_revision": identity["policy_revision"],
        "media": {
            "canonical_path": identity["canonical_path"],
            "media_fingerprint": identity["media_fingerprint"],
            "media_size": identity["media_size"],
            "media_mtime_ns": identity["media_mtime_ns"],
        },
    }


def write_output_manifest(
    video: str | Path,
    config: Any,
    outputs: list[str | Path],
    *,
    provenance: dict[str, Any] | None = None,
    obligation_id: str | None = None,
    publication_kind: str = "ai_subtitle",
    output_languages: list[str] | tuple[str, ...] | None = None,
) -> Path:
    publication = _publication_semantics(
        publication_kind,
        output_languages,
        output_count=len(outputs),
    )
    files = []
    for index, output in enumerate(outputs):
        path = Path(output)
        if not path.is_file():
            raise FileNotFoundError(f"Cannot publish incomplete AI output set; missing {path}")
        stat = path.stat()
        entry = {
                "path": str(path),
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "sha256": sha256_file(path),
            }
        if publication is not None:
            entry["language"] = publication["output_languages"][index]
        files.append(entry)
    identity = delivery_identity(video, config)
    if obligation_id is not None and str(obligation_id) != identity["obligation_id"]:
        raise ValueError("Delivery obligation id does not match the current media and policy revision")
    completed_at = time.time()
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "video": str(Path(video)),
        "media": identity["media"],
        "delivery": {
            "contract": DELIVERY_EVIDENCE_CONTRACT,
            "obligation_id": identity["obligation_id"],
            "policy_revision": identity["policy_revision"],
            "verified_at": completed_at,
        },
        "quality_gate": {
            "passed": True,
            "contract": "worker-prepublication-v1",
        },
        "publication_kind": str(publication_kind or "ai_subtitle"),
        "completed_at": completed_at,
        "outputs": files,
        "provenance": provenance or {},
    }
    if publication is not None:
        payload["publication"] = publication
        if not _publication_source_provenance_matches(
            video,
            config,
            publication,
            files,
            payload,
        ):
            raise ValueError(
                f"Strict publication source provenance is missing or invalid for {publication['kind']}"
            )
    path = output_manifest_path(video, config)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path


def remove_output_manifest(video: str | Path, config: Any) -> None:
    output_manifest_path(video, config).unlink(missing_ok=True)


def finish_output_publication(video: str | Path, config: Any) -> None:
    output_publication_marker_path(video, config).unlink(missing_ok=True)


def validate_output_manifest(
    video: str | Path,
    config: Any,
    *,
    verify_hashes: bool = False,
    required_outputs: list[str | Path] | tuple[str | Path, ...] | None = None,
    require_delivery_evidence: bool = False,
    expected_obligation_id: str | None = None,
    expected_policy_revision: str | None = None,
    expected_publication_kind: str | None = None,
    expected_output_languages: list[str] | tuple[str, ...] | None = None,
    require_publication_semantics: bool = False,
) -> bool:
    path = output_manifest_path(video, config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("schema_version") not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        return False
    schema_version = int(payload.get("schema_version") or 0)
    if require_delivery_evidence and schema_version != MANIFEST_SCHEMA_VERSION:
        return False
    if require_delivery_evidence and (
        not str(expected_obligation_id or "").strip()
        or not str(expected_policy_revision or "").strip()
    ):
        # Strict success evidence must be bound to the exact ledger identity;
        # deriving it only from mutable caller config is not enough.
        return False
    if str(payload.get("video") or "") != str(Path(video)):
        return False
    if schema_version == MANIFEST_SCHEMA_VERSION:
        try:
            identity = delivery_identity(video, config)
        except OSError:
            return False
        media = payload.get("media")
        delivery = payload.get("delivery")
        quality_gate = payload.get("quality_gate")
        if not isinstance(media, dict) or media != identity["media"]:
            return False
        if not isinstance(delivery, dict):
            return False
        if delivery.get("contract") != DELIVERY_EVIDENCE_CONTRACT:
            return False
        if str(delivery.get("obligation_id") or "") != identity["obligation_id"]:
            return False
        if str(delivery.get("policy_revision") or "") != identity["policy_revision"]:
            return False
        if expected_obligation_id is not None and str(delivery.get("obligation_id") or "") != str(
            expected_obligation_id
        ):
            return False
        if expected_policy_revision is not None and str(delivery.get("policy_revision") or "") != str(
            expected_policy_revision
        ):
            return False
        if not isinstance(quality_gate, dict) or quality_gate.get("passed") is not True:
            return False
        try:
            if float(delivery.get("verified_at") or 0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    elif expected_obligation_id is not None:
        return False
    if require_delivery_evidence and output_publication_marker_path(video, config).exists():
        return False
    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return False
    manifested_outputs: set[str] = set()
    for entry in outputs:
        if not isinstance(entry, dict):
            return False
        output = Path(str(entry.get("path") or ""))
        try:
            stat = output.stat()
            if not output.is_file() or int(entry.get("size") or -1) != stat.st_size:
                return False
            # Version 1 manifests created before mtime tracking remain valid.
            # Every newly written manifest records mtime_ns so same-size edits
            # are rejected without hashing every ASS during a library scan.
            if "mtime_ns" in entry and int(entry.get("mtime_ns") or -1) != int(stat.st_mtime_ns):
                return False
            if (verify_hashes or require_delivery_evidence) and str(entry.get("sha256") or "") != sha256_file(output):
                return False
        except (OSError, TypeError, ValueError):
            return False
        manifested_outputs.add(str(output.resolve()))
    if required_outputs is not None:
        required = {str(Path(output).resolve()) for output in required_outputs}
        if manifested_outputs != required:
            return False
    if require_delivery_evidence or require_publication_semantics:
        publication = manifest_publication_semantics(payload)
        if publication is None:
            return False
        if not _publication_outputs_match_policy(video, config, publication, outputs):
            return False
        if not _publication_source_provenance_matches(
            video,
            config,
            publication,
            outputs,
            payload,
        ):
            return False
        if expected_publication_kind is not None and publication["kind"] != str(
            expected_publication_kind
        ):
            return False
        if expected_output_languages is not None:
            try:
                expected_languages = tuple(
                    _normalize_language_tag(language)
                    for language in expected_output_languages
                )
            except (TypeError, ValueError):
                return False
            if tuple(publication["output_languages"]) != expected_languages:
                return False
    return True


def manifest_publication_semantics(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return audited publication semantics or ``None`` for unsafe evidence."""

    publication = payload.get("publication")
    outputs = payload.get("outputs")
    if not isinstance(publication, dict) or not isinstance(outputs, list):
        return None
    if publication.get("contract") != PUBLICATION_SEMANTICS_CONTRACT:
        return None
    kind = str(publication.get("kind") or "").strip()
    if str(payload.get("publication_kind") or "").strip() != kind:
        return None
    languages = publication.get("output_languages")
    if not isinstance(languages, list) or len(languages) != len(outputs):
        return None
    try:
        normalized_languages = tuple(_normalize_language_tag(item) for item in languages)
    except (TypeError, ValueError):
        return None
    if tuple(languages) != normalized_languages:
        return None
    output_entry_languages: list[str] = []
    for output in outputs:
        if not isinstance(output, dict):
            return None
        try:
            output_entry_languages.append(_normalize_language_tag(output.get("language")))
        except (TypeError, ValueError):
            return None
    if tuple(output_entry_languages) != normalized_languages:
        return None
    if kind == TRANSLATED_PUBLICATION_KIND:
        if not _translated_output_languages_are_valid(normalized_languages):
            return None
    elif kind in {ADOPTED_ZH_TW_PUBLICATION_KIND, CONVERTED_ZH_CN_PUBLICATION_KIND}:
        if normalized_languages != TRADITIONAL_CHINESE_OUTPUT_LANGUAGES:
            return None
    elif kind == SOURCE_LANGUAGE_PUBLICATION_KIND:
        if len(normalized_languages) != 1 or not _is_deliverable_source_language(
            normalized_languages[0]
        ):
            return None
    else:
        return None
    return {
        "contract": PUBLICATION_SEMANTICS_CONTRACT,
        "kind": kind,
        "output_languages": list(normalized_languages),
    }


def _publication_semantics(
    publication_kind: str,
    output_languages: list[str] | tuple[str, ...] | None,
    *,
    output_count: int,
) -> dict[str, Any] | None:
    """Build strict semantics when a caller explicitly supplies languages.

    Generic/legacy manifest writers may omit languages for non-delivery cache
    bookkeeping. Such manifests remain readable but can never be strict SLO
    success evidence.
    """

    if output_languages is None:
        return None
    try:
        normalized_languages = tuple(
            _normalize_language_tag(language) for language in output_languages
        )
    except TypeError as exc:
        raise ValueError("output_languages must be an ordered language sequence") from exc
    if len(normalized_languages) != int(output_count):
        raise ValueError("Every manifested output must have exactly one output language")
    kind = str(publication_kind or "").strip()
    if kind == TRANSLATED_PUBLICATION_KIND:
        if not _translated_output_languages_are_valid(normalized_languages):
            raise ValueError(
                "translated_trilingual publication requires one explicit source language followed by zh-CN and zh-TW"
            )
    elif kind in {ADOPTED_ZH_TW_PUBLICATION_KIND, CONVERTED_ZH_CN_PUBLICATION_KIND}:
        if normalized_languages != TRADITIONAL_CHINESE_OUTPUT_LANGUAGES:
            raise ValueError(
                f"{kind} publication requires exactly one zh-TW output"
            )
    elif kind == SOURCE_LANGUAGE_PUBLICATION_KIND:
        if len(normalized_languages) != 1 or not _is_deliverable_source_language(
            normalized_languages[0]
        ):
            raise ValueError(
                "source_language publication requires one explicit non-Japanese source language"
            )
    else:
        raise ValueError(f"Unsupported strict publication kind: {kind or '<blank>'}")
    return {
        "contract": PUBLICATION_SEMANTICS_CONTRACT,
        "kind": kind,
        "output_languages": list(normalized_languages),
    }


def _normalize_language_tag(value: Any) -> str:
    raw = str(value or "").strip().replace("_", "-")
    if not raw or not all(re.fullmatch(r"[A-Za-z0-9]{1,8}", part) for part in raw.split("-")):
        raise ValueError(f"Invalid output language tag: {value!r}")
    parts = raw.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def _is_deliverable_source_language(language: str) -> bool:
    primary = str(language or "").split("-", 1)[0].casefold()
    return primary not in _NON_DELIVERABLE_SOURCE_LANGUAGES


def _translated_output_languages_are_valid(languages: tuple[str, ...]) -> bool:
    if len(languages) != 3 or languages[1:] != ("zh-CN", "zh-TW"):
        return False
    return languages[0] == "ja" or _is_deliverable_source_language(languages[0])


def _publication_outputs_match_policy(
    video: str | Path,
    config: Any,
    publication: dict[str, Any],
    outputs: list[Any],
) -> bool:
    """Bind declared languages to the policy-defined output paths."""

    try:
        manifested = [str(Path(str(item["path"])).resolve()) for item in outputs]
        if publication["kind"] == TRANSLATED_PUBLICATION_KIND:
            from subtitle_paths import paths_for_video

            paths = paths_for_video(video, config)
            source_language = str(publication["output_languages"][0])
            if source_language == "ja":
                source_output = paths.ai_ja_ass
            else:
                from subtitle_paths import source_transcript_paths_for_video

                source_output = source_transcript_paths_for_video(
                    video,
                    config,
                    source_language,
                ).ass
            expected = [
                str(source_output.resolve()),
                str(paths.ai_zh_cn_ass.resolve()),
                str(paths.ai_zh_tw_ass.resolve()),
            ]
        elif publication["kind"] == SOURCE_LANGUAGE_PUBLICATION_KIND:
            from subtitle_paths import source_transcript_paths_for_video

            language = str(publication["output_languages"][0])
            expected = [
                str(source_transcript_paths_for_video(video, config, language).ass.resolve())
            ]
        else:
            expected = [
                str(Path(video).with_name(f"{Path(video).stem}.zh-TW.ass").resolve())
            ]
        return manifested == expected
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False


def publication_is_traditional_chinese_delivery(publication: dict[str, Any] | None) -> bool:
    if not isinstance(publication, dict):
        return False
    return (
        str(publication.get("kind") or "")
        in {
            TRANSLATED_PUBLICATION_KIND,
            ADOPTED_ZH_TW_PUBLICATION_KIND,
            CONVERTED_ZH_CN_PUBLICATION_KIND,
        }
        and "zh-TW" in tuple(publication.get("output_languages") or ())
    )


def _publication_source_provenance_matches(
    video: str | Path,
    config: Any,
    publication: dict[str, Any],
    outputs: list[Any],
    payload: dict[str, Any],
) -> bool:
    kind = str(publication.get("kind") or "")
    provenance = payload.get("provenance")
    source = provenance.get("subtitle_source") if isinstance(provenance, dict) else None

    translated_source_language = (
        str((publication.get("output_languages") or [""])[0])
        if kind == TRANSLATED_PUBLICATION_KIND
        else ""
    )
    if kind == TRANSLATED_PUBLICATION_KIND and translated_source_language != "ja":
        return _source_transcription_provenance_matches(
            video,
            config,
            publication,
            payload,
        )
    if kind == TRANSLATED_PUBLICATION_KIND and source is None:
        # The normal Japanese-audio ASR route has no pre-existing subtitle
        # source.  Its ASR diagnostics and output QC are validated elsewhere.
        return True
    if kind == SOURCE_LANGUAGE_PUBLICATION_KIND:
        return True
    if not isinstance(source, dict):
        return False

    try:
        from source_decision import (
            CONVERT_ZH_CN,
            SOURCE_DECISION_CONTRACT,
            TRANSLATE_JAPANESE,
            USE_ZH_TW,
        )
        from subtitle_extract import classify_subtitle_content_file
        from subtitle_quality import analyze_subtitle_file

        expected_strategy = {
            ADOPTED_ZH_TW_PUBLICATION_KIND: USE_ZH_TW,
            CONVERTED_ZH_CN_PUBLICATION_KIND: CONVERT_ZH_CN,
            TRANSLATED_PUBLICATION_KIND: TRANSLATE_JAPANESE,
        }.get(kind)
        expected_language = {
            ADOPTED_ZH_TW_PUBLICATION_KIND: "zh-tw",
            CONVERTED_ZH_CN_PUBLICATION_KIND: "zh-cn",
            TRANSLATED_PUBLICATION_KIND: "ja",
        }.get(kind)
        if expected_strategy is None or expected_language is None:
            return False
        if source.get("contract") != SOURCE_DECISION_CONTRACT:
            return False
        if str(source.get("strategy") or "") != expected_strategy:
            return False
        if source.get("asr_used") is not False:
            return False
        if str(source.get("source_kind") or "").strip() == "":
            return False
        if str(source.get("source_kind") or "") not in {
            "sidecar",
            "embedded",
            "sidecar_or_embedded",
            "sidecar_or_extracted",
        }:
            return False
        if int(source.get("stream_index")) < -1:
            return False
        if str(source.get("source_language") or "").casefold() != expected_language:
            return False

        source_path = Path(str(source.get("source_path") or ""))
        source_stat = source_path.stat()
        if not source_path.is_file() or source_stat.st_size <= 0:
            return False
        if int(source.get("source_size") or -1) != int(source_stat.st_size):
            return False
        if int(source.get("source_mtime_ns") or -1) != int(source_stat.st_mtime_ns):
            return False
        if str(source.get("source_sha256") or "") != sha256_file(source_path):
            return False
        classification = classify_subtitle_content_file(source_path)
        if str(classification.language or "").casefold() != expected_language:
            return False
        if source.get("classification") != classification.as_dict():
            return False

        source_quality = source.get("source_quality")
        if not isinstance(source_quality, dict):
            return False
        source_role = "japanese" if expected_language == "ja" else "unknown"
        current_source_quality = analyze_subtitle_file(
            source_path,
            config,
            role=source_role,
        )
        if not _quality_snapshot_matches(source_quality, current_source_quality):
            return False

        if kind in {ADOPTED_ZH_TW_PUBLICATION_KIND, CONVERTED_ZH_CN_PUBLICATION_KIND}:
            if len(outputs) != 1 or not isinstance(outputs[0], dict):
                return False
            output_path = Path(str(outputs[0].get("path") or ""))
            if classify_subtitle_content_file(output_path).language != "zh-tw":
                return False
            output_quality = source.get("output_quality")
            if not isinstance(output_quality, dict):
                return False
            current_output_quality = analyze_subtitle_file(
                output_path,
                config,
                role="unknown",
            )
            if not _quality_snapshot_matches(output_quality, current_output_quality):
                return False
        return True
    except Exception:
        return False


def _source_transcription_provenance_matches(
    video: str | Path,
    config: Any,
    publication: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    provenance = payload.get("provenance")
    evidence = (
        provenance.get("source_transcription")
        if isinstance(provenance, dict)
        else None
    )
    if not isinstance(evidence, dict):
        return False
    try:
        language = _normalize_language_tag(publication["output_languages"][0])
        if not _is_deliverable_source_language(language):
            return False
        if evidence.get("contract") != SOURCE_TRANSCRIPTION_PROVENANCE_CONTRACT:
            return False
        if _normalize_language_tag(evidence.get("language")) != language:
            return False
        if evidence.get("asr_used") is not True:
            return False

        from subtitle_paths import source_transcript_paths_for_video
        from transcriber import (
            asr_diagnostics_path,
            asr_transcription_hold_path,
            read_asr_diagnostics,
        )

        expected_path = source_transcript_paths_for_video(
            video,
            config,
            language,
        ).srt.resolve()
        source_path = Path(str(evidence.get("path") or "")).resolve()
        if source_path != expected_path or not source_path.is_file():
            return False
        stat = source_path.stat()
        if int(evidence.get("size") or -1) != int(stat.st_size):
            return False
        if int(evidence.get("mtime_ns") or -1) != int(stat.st_mtime_ns):
            return False
        source_sha256 = sha256_file(source_path)
        if str(evidence.get("sha256") or "") != source_sha256:
            return False
        if asr_transcription_hold_path(source_path, config).exists():
            return False
        diagnostic_path = asr_diagnostics_path(source_path, config)
        if diagnostic_path.is_file():
            diagnostic = read_asr_diagnostics(source_path, config)
            if str(diagnostic.get("status") or "") not in {
                "accepted",
                "accepted_after_selective_retry",
            }:
                return False
            if str(diagnostic.get("srt_sha256") or "") != source_sha256:
                return False
        return True
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False


def _quality_snapshot_matches(snapshot: dict[str, Any], current: Any) -> bool:
    try:
        current_snapshot = current.to_dict()
        return (
            snapshot == current_snapshot
            and snapshot.get("has_failures") is False
            and int(snapshot.get("dialogues") or 0) > 0
        )
    except (AttributeError, TypeError, ValueError):
        return False
