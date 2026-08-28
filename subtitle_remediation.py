from __future__ import annotations

"""Deterministic, bounded remediation for structurally valid SRT files.

The engine deliberately does not run QC and never declares an artifact
publishable.  A successful call produces a new, hash-bound candidate and sets
``requires_qc`` on the result; the caller must run the normal subtitle QC again
before publication.  Only the explicitly enumerated, semantics-preserving
rules below are supported.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import codecs
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from safe_files import atomic_write_bytes, atomic_write_text, sha256_file
from srt_utils import (
    SrtBlock,
    format_srt,
    parse_srt,
    read_srt,
    validate_same_numbering,
    validate_srt_structure,
    validate_translation,
)


MAX_REMEDIATION_ROUNDS = 2
DIAGNOSTIC_SCHEMA_VERSION = 1
DIAGNOSTIC_DIRECTORY_NAME = "subtitle_remediation"
SUBTITLE_REMEDIATION_CONTRACT = "subtitle-remediation-v1"

_SAFE_ISSUE_CODES = frozenset(
    {
        "simplified_chinese_remnant",
        "glossary_term_inconsistent",
        "repeated_punctuation",
        "long_line",
        "very_long_line",
        "timing_overlap",
        "too_short",
        "short_duration",
    }
)
_ZH_TW_ROLES = frozenset({"zh-tw", "zh_tw", "traditional", "translated", "translated_zh_tw"})
_ZH_CN_ROLES = frozenset({"zh-cn", "zh_cn", "simplified", "translated_zh_cn"})
_JA_ROLES = frozenset({"ja", "japanese"})
_SOURCE_ROLES = frozenset({"source", "source_language"})
_TRANSLATED_ROLES = frozenset({"zh-TW", "zh-CN"})

_PUNCTUATION_RUN_RE = re.compile(r"([.!?。！？…，,；;：:～~])\1{3,}")
_WRAP_PUNCTUATION = frozenset("，。！？；：、,.!?;:")
_TIMING_RE = re.compile(
    r"^(?P<start>\d{2}:\d{2}:\d{2},\d{3})"
    r"(?P<arrow>\s+-->\s+)"
    r"(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
    r"(?P<suffix>.*)$"
)

# Configuration can make the engine stricter, but never broader than these
# hard safety ceilings.
_HARD_MAX_PUNCTUATION_REPEAT = 3
_HARD_MAX_VISUAL_LINES = 3
_HARD_MAX_SINGLE_TIMING_SHIFT_MS = 2000
_HARD_MAX_TOTAL_TIMING_SHIFT_MS = 3000
_HARD_MAX_OVERLAP_REPAIR_MS = 2000


class SubtitleRemediationError(RuntimeError):
    """The requested remediation could not be performed safely."""


class RemediationRoundLimitError(SubtitleRemediationError):
    """The two-round remediation budget or its hash chain was violated."""


@dataclass(frozen=True)
class RemediationPolicy:
    opencc_config: str
    punctuation_repeat_limit: int
    wrap_max_chars: int
    max_visual_lines: int
    overlap_tolerance_ms: int
    hard_min_duration_ms: int
    target_min_duration_ms: int
    max_single_timing_shift_ms: int
    max_total_timing_shift_ms: int
    max_overlap_repair_ms: int


@dataclass(frozen=True)
class RemediationResult:
    status: str
    input_path: str
    output_path: str
    diagnostic_path: str
    role: str
    round_number: int
    issue_fingerprint: str
    input_sha256: str
    output_sha256: str
    changed: bool
    applied_rules: tuple[str, ...]
    changed_indexes: tuple[int, ...]
    requires_qc: bool


@dataclass
class _CueTiming:
    position: int
    index: int
    start_ms: int
    end_ms: int
    arrow: str
    suffix: str


def remediate_srt(
    input_srt: str | Path,
    output_srt: str | Path,
    *,
    role: str,
    issue_codes: Iterable[str] | str,
    config: Any | None = None,
    glossary: Mapping[str, str] | None = None,
    work_path: str | Path,
    round_number: int = 1,
    timing_duration_indexes: Iterable[int] | None = None,
) -> RemediationResult:
    """Create one safe remediation candidate and its immutable diagnostic.

    ``round_number`` must be 1 or 2.  Round two is accepted only when the same
    work directory contains this output lineage's round-one diagnostic and its
    candidate SHA-256 equals the current input SHA-256.  Thus every round uses
    a newly QC-observed artifact rather than retrying the same issue fingerprint.

    The diagnostic is atomically published *before* the candidate.  If writing
    the diagnostic fails, neither ``output_srt`` nor an in-place ``input_srt``
    is touched.  An existing diagnostic makes the operation idempotent.
    """

    source = Path(input_srt)
    output = Path(output_srt)
    work = Path(work_path)
    if source.suffix.casefold() != ".srt" or output.suffix.casefold() != ".srt":
        raise SubtitleRemediationError("subtitle remediation only supports .srt files")
    if not source.is_file():
        raise SubtitleRemediationError(f"input SRT does not exist: {source}")

    normalized_role = _normalize_role(role)
    normalized_codes = _normalize_issue_codes(issue_codes)
    normalized_duration_indexes = _normalize_timing_indexes(timing_duration_indexes)
    round_value = _normalize_round(round_number)
    policy = _policy_from_config(config)
    active_glossary = _normalize_glossary(config, glossary)

    input_sha256 = sha256_file(source)
    original_blocks = read_srt(source)
    lineage_key = _lineage_key(output)
    fingerprint_payload = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "input_path": _normalized_path(source),
        "output_path": _normalized_path(output),
        "input_sha256": input_sha256,
        "lineage_key": lineage_key,
        "role": normalized_role,
        "round": round_value,
        "issue_codes": list(normalized_codes),
        "timing_duration_indexes": list(normalized_duration_indexes or ()),
        "policy": asdict(policy),
        "glossary": [[key, value] for key, value in active_glossary],
    }
    issue_fingerprint = _sha256_json(fingerprint_payload)
    diagnostic_directory = work / DIAGNOSTIC_DIRECTORY_NAME
    diagnostic_path = diagnostic_directory / f"{issue_fingerprint}.json"

    if diagnostic_path.exists():
        return _existing_result(
            diagnostic_path,
            expected_fingerprint=issue_fingerprint,
            expected_input_sha256=input_sha256,
            output=output,
        )

    _validate_round_chain(
        diagnostic_directory,
        lineage_key=lineage_key,
        input_sha256=input_sha256,
        round_number=round_value,
    )

    blocks = list(original_blocks)
    changes: list[dict[str, Any]] = []

    if (
        "simplified_chinese_remnant" in normalized_codes
        and normalized_role == "zh-TW"
    ):
        converted = _convert_srt_with_opencc(blocks, policy.opencc_config)
        _record_block_changes(changes, "opencc_s2twp", blocks, converted)
        blocks = converted

    if (
        "glossary_term_inconsistent" in normalized_codes
        and normalized_role in _TRANSLATED_ROLES
        and active_glossary
    ):
        replaced = _apply_glossary(blocks, active_glossary)
        _record_block_changes(changes, "glossary_exact_replacement", blocks, replaced)
        blocks = replaced

    if "repeated_punctuation" in normalized_codes:
        clamped = _clamp_repeated_punctuation(blocks, policy.punctuation_repeat_limit)
        _record_block_changes(changes, "repeated_punctuation_clamp", blocks, clamped)
        blocks = clamped

    if {"long_line", "very_long_line"}.intersection(normalized_codes):
        wrapped = _wrap_long_lines(
            blocks,
            max_chars=policy.wrap_max_chars,
            max_visual_lines=policy.max_visual_lines,
        )
        _record_block_changes(changes, "visual_line_wrap", blocks, wrapped)
        blocks = wrapped

    timing_changed = False
    if {"timing_overlap", "too_short", "short_duration"}.intersection(normalized_codes):
        blocks, timing_changed = _repair_timings(
            blocks,
            issue_codes=normalized_codes,
            policy=policy,
            changes=changes,
            duration_indexes=(
                set(normalized_duration_indexes)
                if normalized_duration_indexes is not None
                else None
            ),
        )

    _validate_candidate(original_blocks, blocks, timing_changed=timing_changed)

    applied_rules = tuple(dict.fromkeys(str(item["rule"]) for item in changes))
    changed_indexes = tuple(
        sorted(
            {
                int(index)
                for item in changes
                for index in item.get("indexes", [])
            }
        )
    )

    if not changes:
        payload = _diagnostic_payload(
            status="no_safe_change",
            source=source,
            output=output,
            normalized_role=normalized_role,
            round_number=round_value,
            normalized_codes=normalized_codes,
            issue_fingerprint=issue_fingerprint,
            lineage_key=lineage_key,
            input_sha256=input_sha256,
            output_sha256=input_sha256,
            policy=policy,
            changes=[],
        )
        _write_diagnostic(diagnostic_path, payload)
        return RemediationResult(
            status="no_safe_change",
            input_path=str(source),
            output_path=str(output),
            diagnostic_path=str(diagnostic_path),
            role=normalized_role,
            round_number=round_value,
            issue_fingerprint=issue_fingerprint,
            input_sha256=input_sha256,
            output_sha256=input_sha256,
            changed=False,
            applied_rules=(),
            changed_indexes=(),
            requires_qc=False,
        )

    candidate_bytes = codecs.BOM_UTF8 + format_srt(blocks).encode("utf-8")
    output_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    if output_sha256 == input_sha256:
        raise SubtitleRemediationError(
            "remediation reported changes but did not produce a new SHA-256"
        )

    payload = _diagnostic_payload(
        status="candidate_ready",
        source=source,
        output=output,
        normalized_role=normalized_role,
        round_number=round_value,
        normalized_codes=normalized_codes,
        issue_fingerprint=issue_fingerprint,
        lineage_key=lineage_key,
        input_sha256=input_sha256,
        output_sha256=output_sha256,
        policy=policy,
        changes=changes,
    )
    # This ordering is the fail-closed publication gate: no diagnostic, no
    # output mutation.  The diagnostic describes a hash-bound candidate rather
    # than claiming that downstream QC or publication has completed.
    _write_diagnostic(diagnostic_path, payload)
    atomic_write_bytes(output, candidate_bytes)
    if sha256_file(output) != output_sha256:
        raise SubtitleRemediationError(
            f"published remediation candidate hash mismatch: {output}"
        )

    return RemediationResult(
        status="remediated",
        input_path=str(source),
        output_path=str(output),
        diagnostic_path=str(diagnostic_path),
        role=normalized_role,
        round_number=round_value,
        issue_fingerprint=issue_fingerprint,
        input_sha256=input_sha256,
        output_sha256=output_sha256,
        changed=True,
        applied_rules=applied_rules,
        changed_indexes=changed_indexes,
        requires_qc=True,
    )


def remediate_srt_in_place(
    input_srt: str | Path,
    *,
    role: str,
    issue_codes: Iterable[str] | str,
    config: Any | None = None,
    glossary: Mapping[str, str] | None = None,
    work_path: str | Path,
    round_number: int = 1,
) -> RemediationResult:
    """Atomically replace an SRT with a diagnosed remediation candidate.

    This is the preferred Worker integration entrypoint.  It intentionally
    delegates to the same fail-closed transaction as :func:`remediate_srt`:
    parsing and candidate construction occur in memory, the hash-bound
    diagnostic is atomically committed first, and only then is the SRT
    atomically replaced.  Diagnostic failure therefore cannot modify the
    original SRT, and a candidate write failure cannot expose a partial SRT.
    """

    source = Path(input_srt)
    return remediate_srt(
        source,
        source,
        role=role,
        issue_codes=issue_codes,
        config=config,
        glossary=glossary,
        work_path=work_path,
        round_number=round_number,
    )


def next_remediation_round(
    input_srt: str | Path,
    *,
    work_path: str | Path,
) -> int:
    """Return the next hash-bound round for a durable in-place candidate.

    A process may stop after round one atomically replaced the SRT but before
    the caller re-ran QC.  Recomputing this value from immutable diagnostics
    lets the next Worker resume at round two without resetting the bounded
    retry budget.  A lineage that already consumed round two raises rather
    than silently starting over.
    """

    source = Path(input_srt)
    if source.suffix.casefold() != ".srt" or not source.is_file():
        raise SubtitleRemediationError(
            f"remediation round lookup requires an existing SRT: {source}"
        )
    diagnostic_directory = Path(work_path) / DIAGNOSTIC_DIRECTORY_NAME
    lineage_key = _lineage_key(source)
    input_sha256 = sha256_file(source)
    try:
        _validate_round_chain(
            diagnostic_directory,
            lineage_key=lineage_key,
            input_sha256=input_sha256,
            round_number=1,
        )
        return 1
    except RemediationRoundLimitError:
        _validate_round_chain(
            diagnostic_directory,
            lineage_key=lineage_key,
            input_sha256=input_sha256,
            round_number=2,
        )
        return 2


# Descriptive alias for callers that do not use the shorter historical naming.
remediate_subtitle_file = remediate_srt


def _normalize_role(role: str) -> str:
    normalized = str(role or "").strip().casefold().replace("_", "-")
    if normalized in {item.replace("_", "-") for item in _ZH_TW_ROLES}:
        return "zh-TW"
    if normalized in {item.replace("_", "-") for item in _ZH_CN_ROLES}:
        return "zh-CN"
    if normalized in _JA_ROLES:
        return "ja"
    if normalized in {item.replace("_", "-") for item in _SOURCE_ROLES}:
        return "source"
    raise SubtitleRemediationError(f"unsupported subtitle remediation role: {role!r}")


def _normalize_issue_codes(issue_codes: Iterable[str] | str) -> tuple[str, ...]:
    values: Iterable[Any]
    if isinstance(issue_codes, str):
        values = (issue_codes,)
    else:
        values = issue_codes
    normalized: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("code", "")
        elif not isinstance(value, str) and hasattr(value, "code"):
            value = getattr(value, "code")
        code = str(value or "").strip().casefold()
        if code:
            normalized.add(code)
    return tuple(sorted(normalized))


def _normalize_round(round_number: int) -> int:
    if isinstance(round_number, bool):
        raise RemediationRoundLimitError("round_number must be 1 or 2")
    try:
        value = int(round_number)
    except (TypeError, ValueError) as exc:
        raise RemediationRoundLimitError("round_number must be 1 or 2") from exc
    if value < 1 or value > MAX_REMEDIATION_ROUNDS:
        raise RemediationRoundLimitError(
            f"subtitle remediation is limited to {MAX_REMEDIATION_ROUNDS} rounds"
        )
    return value


def _normalize_timing_indexes(values: Iterable[int] | None) -> tuple[int, ...] | None:
    if values is None:
        return None
    normalized: set[int] = set()
    for value in values:
        if isinstance(value, bool):
            raise SubtitleRemediationError("timing duration indexes must be positive integers")
        try:
            index = int(value)
        except (TypeError, ValueError) as exc:
            raise SubtitleRemediationError(
                "timing duration indexes must be positive integers"
            ) from exc
        if index <= 0:
            raise SubtitleRemediationError("timing duration indexes must be positive integers")
        normalized.add(index)
        if len(normalized) > 500:
            raise SubtitleRemediationError("timing duration repair is limited to 500 cues")
    if not normalized:
        raise SubtitleRemediationError("timing duration indexes cannot be empty")
    return tuple(sorted(normalized))


def _policy_from_config(config: Any | None) -> RemediationPolicy:
    opencc_config = str(_config_value(config, "opencc_config", "s2twp.json") or "").strip()
    if opencc_config.casefold() not in {"s2twp", "s2twp.json"}:
        raise SubtitleRemediationError(
            "safe Traditional-Chinese remediation requires OpenCC s2twp"
        )

    punctuation_limit = _bounded_int(
        _config_value(config, "subtitle_remediation_punctuation_repeat_limit", 3),
        name="subtitle_remediation_punctuation_repeat_limit",
        minimum=1,
        maximum=_HARD_MAX_PUNCTUATION_REPEAT,
    )
    wrap_max_chars = _bounded_int(
        _config_value(
            config,
            "subtitle_remediation_wrap_max_chars",
            _config_value(config, "subtitle_quality_max_primary_chars", 42),
        ),
        name="subtitle_remediation_wrap_max_chars",
        minimum=8,
        maximum=80,
    )
    max_visual_lines = _bounded_int(
        _config_value(config, "subtitle_remediation_max_visual_lines", 2),
        name="subtitle_remediation_max_visual_lines",
        minimum=2,
        maximum=_HARD_MAX_VISUAL_LINES,
    )

    overlap_tolerance_ms = _seconds_to_bounded_ms(
        _config_value(config, "subtitle_quality_max_overlap_seconds", 0.10),
        name="subtitle_quality_max_overlap_seconds",
        minimum=0,
        maximum=100,
    )
    hard_min_duration_ms = _seconds_to_bounded_ms(
        _config_value(config, "subtitle_quality_hard_min_duration_seconds", 0.12),
        name="subtitle_quality_hard_min_duration_seconds",
        minimum=1,
        maximum=500,
    )
    target_min_duration_ms = _seconds_to_bounded_ms(
        _config_value(config, "subtitle_quality_min_duration_seconds", 0.35),
        name="subtitle_quality_min_duration_seconds",
        minimum=hard_min_duration_ms,
        maximum=1000,
    )
    max_single_shift_ms = _seconds_to_bounded_ms(
        _config_value(config, "subtitle_remediation_max_timing_shift_seconds", 0.20),
        name="subtitle_remediation_max_timing_shift_seconds",
        minimum=1,
        maximum=_HARD_MAX_SINGLE_TIMING_SHIFT_MS,
    )
    max_total_shift_ms = _seconds_to_bounded_ms(
        _config_value(config, "subtitle_remediation_max_total_timing_shift_seconds", 0.50),
        name="subtitle_remediation_max_total_timing_shift_seconds",
        minimum=1,
        maximum=_HARD_MAX_TOTAL_TIMING_SHIFT_MS,
    )
    max_overlap_repair_ms = _seconds_to_bounded_ms(
        _config_value(config, "subtitle_remediation_max_overlap_repair_seconds", 0.20),
        name="subtitle_remediation_max_overlap_repair_seconds",
        minimum=1,
        maximum=_HARD_MAX_OVERLAP_REPAIR_MS,
    )
    return RemediationPolicy(
        opencc_config=opencc_config,
        punctuation_repeat_limit=punctuation_limit,
        wrap_max_chars=wrap_max_chars,
        max_visual_lines=max_visual_lines,
        overlap_tolerance_ms=overlap_tolerance_ms,
        hard_min_duration_ms=hard_min_duration_ms,
        target_min_duration_ms=target_min_duration_ms,
        max_single_timing_shift_ms=max_single_shift_ms,
        max_total_timing_shift_ms=max_total_shift_ms,
        max_overlap_repair_ms=max_overlap_repair_ms,
    )


def _config_value(config: Any | None, name: str, default: Any) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(name, default)
    return getattr(config, name, default)


def _bounded_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise SubtitleRemediationError(f"{name} must be an integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise SubtitleRemediationError(f"{name} must be an integer") from exc
    if normalized < minimum or normalized > maximum:
        raise SubtitleRemediationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return normalized


def _seconds_to_bounded_ms(
    value: Any,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise SubtitleRemediationError(f"{name} must be a number")
    try:
        milliseconds = int(round(float(value) * 1000))
    except (TypeError, ValueError, OverflowError) as exc:
        raise SubtitleRemediationError(f"{name} must be a finite number") from exc
    if milliseconds < minimum or milliseconds > maximum:
        raise SubtitleRemediationError(
            f"{name} must be between {minimum / 1000:.3f}s and {maximum / 1000:.3f}s"
        )
    return milliseconds


def _normalize_glossary(
    config: Any | None,
    glossary: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    combined: dict[str, str] = {}
    configured = _config_value(config, "translation_glossary", {}) or {}
    if not isinstance(configured, Mapping):
        raise SubtitleRemediationError("translation_glossary must be a mapping")
    for source, target in configured.items():
        combined[str(source)] = str(target)
    if glossary is not None:
        if not isinstance(glossary, Mapping):
            raise SubtitleRemediationError("glossary must be a mapping")
        for source, target in glossary.items():
            combined[str(source)] = str(target)

    normalized: list[tuple[str, str]] = []
    for source, target in combined.items():
        if not source or not source.strip() or not target or not target.strip():
            raise SubtitleRemediationError("glossary terms and approved targets must be non-empty")
        if any(character in source or character in target for character in ("\r", "\n", "\x00")):
            raise SubtitleRemediationError("glossary entries cannot contain line breaks or NUL")
        if source != target:
            normalized.append((source, target))
    return tuple(sorted(normalized, key=lambda item: item[0]))


def _convert_srt_with_opencc(blocks: Sequence[SrtBlock], config_name: str) -> list[SrtBlock]:
    try:
        from opencc import OpenCC
    except ImportError as exc:
        raise SubtitleRemediationError("OpenCC is unavailable for s2twp remediation") from exc

    candidates = (config_name, config_name[:-5]) if config_name.endswith(".json") else (config_name,)
    converter: Any | None = None
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            converter = OpenCC(candidate)
            break
        except Exception as exc:  # pragma: no cover - depends on OpenCC packaging
            last_error = exc
    if converter is None:
        raise SubtitleRemediationError(
            f"unable to load OpenCC s2twp configuration: {last_error}"
        )

    try:
        converted_content = converter.convert(format_srt(list(blocks)))
        converted = parse_srt(converted_content)
        validate_translation(list(blocks), converted)
        return converted
    except Exception as exc:
        raise SubtitleRemediationError(f"OpenCC s2twp remediation failed: {exc}") from exc


def _apply_glossary(
    blocks: Sequence[SrtBlock],
    glossary: Sequence[tuple[str, str]],
) -> list[SrtBlock]:
    if not glossary:
        return list(blocks)
    mapping = dict(glossary)
    # One regex pass prevents mappings such as A->B and B->C from cascading.
    sources = sorted(mapping, key=lambda value: (-len(value), value))
    pattern = re.compile("|".join(re.escape(source) for source in sources))
    output: list[SrtBlock] = []
    for block in blocks:
        text = [pattern.sub(lambda match: mapping[match.group(0)], line) for line in block.text]
        output.append(replace(block, text=text))
    return output


def _clamp_repeated_punctuation(
    blocks: Sequence[SrtBlock],
    repeat_limit: int,
) -> list[SrtBlock]:
    output: list[SrtBlock] = []
    for block in blocks:
        text = [
            _PUNCTUATION_RUN_RE.sub(
                lambda match: match.group(1) * repeat_limit,
                line,
            )
            for line in block.text
        ]
        output.append(replace(block, text=text))
    return output


def _wrap_long_lines(
    blocks: Sequence[SrtBlock],
    *,
    max_chars: int,
    max_visual_lines: int,
) -> list[SrtBlock]:
    output: list[SrtBlock] = []
    for block in blocks:
        lines = list(block.text)
        original_semantic_text = _semantic_text(lines)
        while len(lines) < max_visual_lines:
            selected: tuple[int, tuple[str, str]] | None = None
            for position, line in enumerate(lines):
                if _display_length(line) <= max_chars:
                    continue
                split = _safe_visual_split(line, max_chars=max_chars)
                if split is not None:
                    selected = (position, split)
                    break
            if selected is None:
                break
            position, split = selected
            lines[position : position + 1] = list(split)
        if _semantic_text(lines) != original_semantic_text:
            raise SubtitleRemediationError("visual wrapping changed subtitle characters")
        output.append(replace(block, text=lines))
    return output


def _safe_visual_split(line: str, *, max_chars: int) -> tuple[str, str] | None:
    candidates: list[tuple[tuple[int, int, int], str, str]] = []
    for position, character in enumerate(line):
        if character in _WRAP_PUNCTUATION and position + 1 < len(line):
            left = line[: position + 1]
            right = line[position + 1 :]
            _append_wrap_candidate(candidates, left, right, max_chars=max_chars, position=position)

    for match in re.finditer(r"\s+", line):
        if match.start() <= 0 or match.end() >= len(line):
            continue
        left = line[: match.start()]
        right = line[match.end() :]
        _append_wrap_candidate(
            candidates,
            left,
            right,
            max_chars=max_chars,
            position=match.start(),
        )

    if not candidates:
        return None
    _score, left, right = min(candidates, key=lambda item: item[0])
    return left, right


def _append_wrap_candidate(
    candidates: list[tuple[tuple[int, int, int], str, str]],
    left: str,
    right: str,
    *,
    max_chars: int,
    position: int,
) -> None:
    left_length = _display_length(left)
    right_length = _display_length(right)
    if not left.strip() or not right.strip():
        return
    if left_length > max_chars or right_length > max_chars:
        return
    score = (max(left_length, right_length), abs(left_length - right_length), position)
    candidates.append((score, left, right))


def _repair_timings(
    blocks: Sequence[SrtBlock],
    *,
    issue_codes: Sequence[str],
    policy: RemediationPolicy,
    changes: list[dict[str, Any]],
    duration_indexes: set[int] | None = None,
) -> tuple[list[SrtBlock], bool]:
    output = list(blocks)
    timings = [_parse_timing(position, block) for position, block in enumerate(output)]
    ordered = sorted(timings, key=lambda item: (item.start_ms, item.end_ms, item.index))
    total_shift_ms = 0
    timing_changed = False

    if "timing_overlap" in issue_codes and ordered:
        active = ordered[0]
        for current in ordered[1:]:
            overlap_ms = active.end_ms - current.start_ms
            if (
                overlap_ms > policy.overlap_tolerance_ms
                and overlap_ms <= policy.max_overlap_repair_ms
                and total_shift_ms + overlap_ms <= policy.max_total_timing_shift_ms
            ):
                active_slack = max(0, active.end_ms - active.start_ms - policy.hard_min_duration_ms)
                current_slack = max(0, current.end_ms - current.start_ms - policy.hard_min_duration_ms)
                active_duration_ms = active.end_ms - active.start_ms
                current_duration_ms = current.end_ms - current.start_ms
                if overlap_ms > 250 and active_duration_ms <= current_duration_ms:
                    # Preserve the shorter cue when both allocations are safe.
                    # This matters for brief signs/credits immediately before a
                    # longer dialogue cue: trimming the short cue can create a
                    # new CPS or minimum-duration failure.
                    shift_current = min(
                        overlap_ms,
                        current_slack,
                        policy.max_single_timing_shift_ms,
                    )
                    shrink_active = overlap_ms - shift_current
                else:
                    shrink_active = min(
                        overlap_ms,
                        active_slack,
                        policy.max_single_timing_shift_ms,
                    )
                    shift_current = overlap_ms - shrink_active
                if (
                    shrink_active <= active_slack
                    and shrink_active <= policy.max_single_timing_shift_ms
                    and shift_current <= current_slack
                    and shift_current <= policy.max_single_timing_shift_ms
                ):
                    before = [_snapshot(output[active.position]), _snapshot(output[current.position])]
                    active.end_ms -= shrink_active
                    current.start_ms += shift_current
                    output[active.position] = replace(
                        output[active.position],
                        timing=_render_timing(active),
                    )
                    output[current.position] = replace(
                        output[current.position],
                        timing=_render_timing(current),
                    )
                    after = [_snapshot(output[active.position]), _snapshot(output[current.position])]
                    changes.append(
                        {
                            "rule": "timing_overlap_trim",
                            "indexes": [active.index, current.index],
                            "before": before,
                            "after": after,
                            "shift_ms": overlap_ms,
                        }
                    )
                    total_shift_ms += overlap_ms
                    timing_changed = True
            if current.end_ms > active.end_ms:
                active = current

    target_duration_ms: int | None = None
    if "too_short" in issue_codes:
        target_duration_ms = policy.hard_min_duration_ms
    elif "short_duration" in issue_codes:
        target_duration_ms = policy.target_min_duration_ms

    if target_duration_ms is not None:
        ordered = sorted(timings, key=lambda item: (item.start_ms, item.end_ms, item.index))
        for position, current in enumerate(ordered):
            if duration_indexes is not None and current.index not in duration_indexes:
                continue
            duration_ms = current.end_ms - current.start_ms
            if duration_ms <= 0:
                # A non-positive cue is an invalid-timing incident, not a safe
                # duration extension.  Guessing its intended boundary is out of
                # scope for this engine.
                continue
            deficit_ms = target_duration_ms - duration_ms
            if deficit_ms <= 0:
                continue
            if (
                deficit_ms > policy.max_total_timing_shift_ms - total_shift_ms
                or deficit_ms > policy.max_single_timing_shift_ms * 2
            ):
                continue

            previous = ordered[position - 1] if position > 0 else None
            following = ordered[position + 1] if position + 1 < len(ordered) else None
            room_after = max(0, following.start_ms - current.end_ms) if following else 0
            room_before = max(0, current.start_ms - previous.end_ms) if previous else 0
            extend_end_ms = min(deficit_ms, room_after, policy.max_single_timing_shift_ms)
            shift_start_ms = deficit_ms - extend_end_ms
            if (
                shift_start_ms > room_before
                or shift_start_ms > policy.max_single_timing_shift_ms
                or (extend_end_ms <= 0 and shift_start_ms <= 0)
            ):
                continue

            before = [_snapshot(output[current.position])]
            current.end_ms += extend_end_ms
            current.start_ms -= shift_start_ms
            if current.end_ms - current.start_ms < policy.hard_min_duration_ms:
                raise SubtitleRemediationError("timing repair violated hard minimum duration")
            output[current.position] = replace(
                output[current.position],
                timing=_render_timing(current),
            )
            changes.append(
                {
                    "rule": "too_short_expand",
                    "indexes": [current.index],
                    "before": before,
                    "after": [_snapshot(output[current.position])],
                    "shift_ms": deficit_ms,
                }
            )
            total_shift_ms += deficit_ms
            timing_changed = True

    if total_shift_ms > policy.max_total_timing_shift_ms:
        raise SubtitleRemediationError("timing repair exceeded total shift budget")
    return output, timing_changed


def _parse_timing(position: int, block: SrtBlock) -> _CueTiming:
    match = _TIMING_RE.match(block.timing)
    if not match:
        raise SubtitleRemediationError(f"unsupported SRT timing at index {block.index}")
    return _CueTiming(
        position=position,
        index=block.index,
        start_ms=_timestamp_to_ms(match.group("start")),
        end_ms=_timestamp_to_ms(match.group("end")),
        arrow=match.group("arrow"),
        suffix=match.group("suffix"),
    )


def _timestamp_to_ms(value: str) -> int:
    hours, minutes, remainder = value.split(":")
    seconds, milliseconds = remainder.split(",")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1000
        + int(milliseconds)
    )


def _ms_to_timestamp(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _render_timing(timing: _CueTiming) -> str:
    return (
        f"{_ms_to_timestamp(timing.start_ms)}{timing.arrow}"
        f"{_ms_to_timestamp(timing.end_ms)}{timing.suffix}"
    )


def _record_block_changes(
    changes: list[dict[str, Any]],
    rule: str,
    before: Sequence[SrtBlock],
    after: Sequence[SrtBlock],
) -> None:
    if len(before) != len(after):
        raise SubtitleRemediationError(f"{rule} changed SRT cue count")
    for old, new in zip(before, after, strict=True):
        if old == new:
            continue
        changes.append(
            {
                "rule": rule,
                "indexes": [old.index],
                "before": [_snapshot(old)],
                "after": [_snapshot(new)],
            }
        )


def _snapshot(block: SrtBlock) -> dict[str, Any]:
    return {
        "index": block.index,
        "timing": block.timing,
        "text": list(block.text),
    }


def _validate_candidate(
    original: Sequence[SrtBlock],
    candidate: Sequence[SrtBlock],
    *,
    timing_changed: bool,
) -> None:
    validate_srt_structure(list(candidate))
    if len(original) != len(candidate):
        raise SubtitleRemediationError("remediation changed SRT cue count")
    validate_same_numbering(list(original), list(candidate))
    if not timing_changed:
        # The shared production invariant is intentionally called here rather
        # than reimplemented.
        validate_translation(list(original), list(candidate))


def _diagnostic_payload(
    *,
    status: str,
    source: Path,
    output: Path,
    normalized_role: str,
    round_number: int,
    normalized_codes: Sequence[str],
    issue_fingerprint: str,
    lineage_key: str,
    input_sha256: str,
    output_sha256: str,
    policy: RemediationPolicy,
    changes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "role": normalized_role,
        "round": round_number,
        "issue_codes": list(normalized_codes),
        "safe_issue_codes": sorted(_SAFE_ISSUE_CODES.intersection(normalized_codes)),
        "issue_fingerprint": issue_fingerprint,
        "lineage_key": lineage_key,
        "input": {
            "path": str(source),
            "sha256": input_sha256,
        },
        "candidate_output": {
            "path": str(output),
            "sha256": output_sha256,
        },
        "policy": asdict(policy),
        "rules": list(dict.fromkeys(str(item["rule"]) for item in changes)),
        "changed_indexes": sorted(
            {
                int(index)
                for item in changes
                for index in item.get("indexes", [])
            }
        ),
        "changes": list(changes),
        "caller_must_re_qc": bool(changes),
    }


def _write_diagnostic(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _existing_result(
    diagnostic_path: Path,
    *,
    expected_fingerprint: str,
    expected_input_sha256: str,
    output: Path,
) -> RemediationResult:
    try:
        payload = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SubtitleRemediationError(
            f"existing remediation diagnostic is unreadable: {diagnostic_path}"
        ) from exc
    if (
        payload.get("schema_version") != DIAGNOSTIC_SCHEMA_VERSION
        or payload.get("issue_fingerprint") != expected_fingerprint
        or (payload.get("input") or {}).get("sha256") != expected_input_sha256
    ):
        raise SubtitleRemediationError(
            f"existing remediation diagnostic failed identity validation: {diagnostic_path}"
        )

    candidate = payload.get("candidate_output") or {}
    output_sha256 = str(candidate.get("sha256") or "")
    candidate_exists = output.is_file() and sha256_file(output) == output_sha256
    has_changes = bool(payload.get("changes"))
    status = "already_applied" if has_changes and candidate_exists else "already_attempted"
    return RemediationResult(
        status=status,
        input_path=str((payload.get("input") or {}).get("path") or ""),
        output_path=str(candidate.get("path") or output),
        diagnostic_path=str(diagnostic_path),
        role=str(payload.get("role") or ""),
        round_number=int(payload.get("round") or 0),
        issue_fingerprint=expected_fingerprint,
        input_sha256=expected_input_sha256,
        output_sha256=output_sha256 or expected_input_sha256,
        changed=False,
        applied_rules=tuple(str(value) for value in payload.get("rules") or []),
        changed_indexes=tuple(int(value) for value in payload.get("changed_indexes") or []),
        requires_qc=bool(has_changes and candidate_exists),
    )


def _validate_round_chain(
    diagnostic_directory: Path,
    *,
    lineage_key: str,
    input_sha256: str,
    round_number: int,
) -> None:
    predecessors: list[dict[str, Any]] = []
    if diagnostic_directory.is_dir():
        for path in diagnostic_directory.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            candidate = payload.get("candidate_output") or {}
            if (
                payload.get("schema_version") == DIAGNOSTIC_SCHEMA_VERSION
                and payload.get("lineage_key") == lineage_key
                and payload.get("status") == "candidate_ready"
                and candidate.get("sha256") == input_sha256
            ):
                predecessors.append(payload)

    predecessor_rounds = {int(payload.get("round") or 0) for payload in predecessors}
    if round_number == 1:
        if predecessor_rounds:
            raise RemediationRoundLimitError(
                "current input is a prior remediation candidate; caller must use round 2 after QC"
            )
        return
    if predecessor_rounds == {1}:
        return
    if 2 in predecessor_rounds:
        raise RemediationRoundLimitError("subtitle remediation already consumed two rounds")
    raise RemediationRoundLimitError(
        "round 2 requires a hash-matching round-1 diagnostic in the same work path"
    )


def _lineage_key(output: Path) -> str:
    return hashlib.sha256(_normalized_path(output).encode("utf-8")).hexdigest()


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _sha256_json(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _display_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _semantic_text(lines: Sequence[str]) -> str:
    return re.sub(r"\s+", "", "".join(lines))
