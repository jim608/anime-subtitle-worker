from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable

from asr_quality import asr_artifact_line_indexes, asr_prompt_echo_line_indexes
from safe_files import atomic_write_text
from srt_utils import SrtBlock, read_srt
from translation_quality import (
    TRANSLATION_SAFE_OMISSION,
    problematic_residual_kana,
    translation_pollution_reason,
)


ASS_OVERRIDE_RE = re.compile(r"\{[^{}]*\}")
ASS_DIALOGUE_PREFIX = "Dialogue:"
ASS_TIMESTAMP_RE = re.compile(r"^(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})\.(?P<cs>\d{2})$")
SRT_TIMESTAMP_RE = re.compile(
    r"^(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})\s*-->\s*"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})"
)
SPACE_RE = re.compile(r"\s+")
SIMPLIFIED_ONLY_RE = re.compile(
    r"[这发为么个们说对从还进过点时会让与门开关见听写学国气车东业书长"
    r"乐线网头条万无爱觉应实亲两并当动务优传伤价众余备变参层产称迟处触词"
    r"达带单担党导灯敌递电调顶订读独断队儿尔范飞该给构购观广归规汉号坏"
    r"获击际继价简将节仅尽剧据绝军离礼历丽联练粮疗临刘龙楼录虑轮马买卖满"
    r"猫梦灭难脑拟宁农欧盘贫凭启签庆权确认荣软赛删审声胜师识适树双虽随"
    r"态谈体铁统图团湾卫稳误习戏细显险协兴选压严验养样药页医义艺阴银饮拥"
    r"邮鱼远愿杂赞责战张赵阵争执质钟种总组钻]"
)
REPEATED_PUNCTUATION_RE = re.compile(r"([!?！？。．])\1{3,}")
QUALITY_REPORT_DIRECTORY = "subtitle_quality_reports"


class SubtitleQualityError(RuntimeError):
    """Raised when generated subtitle output must be rejected and rerun."""


@dataclass(frozen=True)
class SubtitleLine:
    index: int
    start: float
    end: float
    text: str
    primary_text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class SubtitleQualityIssue:
    code: str
    severity: str
    message: str
    count: int = 1
    samples: list[str] = field(default_factory=list)
    indexes: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class SubtitleQualityReport:
    path: str
    role: str
    status: str
    score: int
    dialogues: int
    avg_duration: float
    max_duration: float
    avg_primary_chars: float
    max_primary_chars: int
    gaps_over_limit: int
    largest_gap: float
    issues: list[SubtitleQualityIssue]
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def has_failures(self) -> bool:
        return any(issue.severity == "fail" for issue in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(issue.severity == "warn" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["has_failures"] = self.has_failures
        payload["has_warnings"] = self.has_warnings
        return payload


def analyze_subtitle_file(path: str | Path, config: Any | None = None, *, role: str | None = None) -> SubtitleQualityReport:
    subtitle_path = Path(path)
    resolved_role = role or infer_subtitle_role(subtitle_path)
    lines = _read_subtitle_lines(subtitle_path)
    return analyze_subtitle_lines(subtitle_path, lines, config, role=resolved_role)


def analyze_subtitle_lines(
    path: str | Path,
    lines: list[SubtitleLine],
    config: Any | None = None,
    *,
    role: str = "unknown",
) -> SubtitleQualityReport:
    issues: list[SubtitleQualityIssue] = []
    if not lines:
        issues.append(
            SubtitleQualityIssue(
                code="empty_subtitle",
                severity="fail",
                message="Subtitle has no dialogue lines.",
            )
        )

    max_duration_limit = float(getattr(config, "subtitle_quality_max_duration_seconds", 5.5))
    max_chars_limit = int(getattr(config, "subtitle_quality_max_primary_chars", 42))
    hard_chars_limit = int(getattr(config, "subtitle_quality_hard_max_primary_chars", 64))
    max_gap_limit = float(getattr(config, "subtitle_quality_max_gap_seconds", 45.0))
    max_leading_gap_limit = float(
        getattr(config, "subtitle_quality_max_leading_gap_seconds", 30.0)
    )
    warn_cps_limit = float(getattr(config, "subtitle_quality_warn_cps", 17.0))
    fail_cps_limit = float(getattr(config, "subtitle_quality_fail_cps", 25.0))
    min_duration_limit = float(
        getattr(config, "subtitle_quality_min_duration_seconds", 0.35)
    )
    hard_min_duration_limit = float(
        getattr(config, "subtitle_quality_hard_min_duration_seconds", 0.12)
    )
    max_overlap_limit = float(
        getattr(config, "subtitle_quality_max_overlap_seconds", 0.10)
    )

    durations = [line.duration for line in lines]
    primary_lengths = [_display_length(line.primary_text) for line in lines]
    long_durations = [line for line in lines if line.duration > max_duration_limit]
    overlong = [line for line in lines if _display_length(line.primary_text) > max_chars_limit]
    very_long = [line for line in lines if _display_length(line.primary_text) > hard_chars_limit]
    empty_text = [line for line in lines if not _clean_text(line.primary_text)]
    invalid_timing = [line for line in lines if line.end <= line.start]
    hard_short = [
        line
        for line in lines
        if line.end > line.start and line.duration < hard_min_duration_limit
    ]
    short_duration = [
        line
        for line in lines
        if hard_min_duration_limit <= line.duration < min_duration_limit
    ]
    fail_cps = [
        line
        for line in lines
        if _cps_exceeds(line, fail_cps_limit)
    ]
    warn_cps = [
        line
        for line in lines
        if _cps_exceeds(line, warn_cps_limit)
        and not _cps_exceeds(line, fail_cps_limit)
    ]
    overlaps = _overlapping_lines(lines, max_overlap_limit)
    prompt_echo_positions = (
        asr_prompt_echo_line_indexes((line.primary_text for line in lines), config)
        if role in {"japanese", "source"}
        else set()
    )
    artifact_positions = (
        asr_artifact_line_indexes((line.primary_text for line in lines), config)
        if role in {"japanese", "source"}
        else set()
    )
    prompt_echoes = [
        line
        for position, line in enumerate(lines)
        if position in prompt_echo_positions
    ]
    hallucinations = [
        line
        for position, line in enumerate(lines)
        if position not in prompt_echo_positions
        and (
            position in artifact_positions
            or _is_hallucination(line.text, config)
        )
    ]
    residual_kana = [
        line
        for line in lines
        if _should_check_translation_role(role) and problematic_residual_kana(line.primary_text)
    ]
    prompt_leaks = [
        line
        for line in lines
        if _should_check_translation_role(role) and translation_pollution_reason(line.primary_text) == "prompt_leak"
    ]
    runaway_repetitions = [
        line
        for line in lines
        if _should_check_translation_role(role)
        and translation_pollution_reason(line.primary_text) == "runaway_repetition"
    ]
    simplified_remnants = [
        line
        for line in lines
        if _should_check_traditional_chinese_role(role)
        and SIMPLIFIED_ONLY_RE.search(line.primary_text)
    ]
    repeated_punctuation = [
        line for line in lines if REPEATED_PUNCTUATION_RE.search(line.primary_text)
    ]
    inconsistent_glossary = _inconsistent_glossary_lines(lines, config, role)
    repeated_cues = _repeated_consecutive_cues(lines)
    gaps = _large_gaps(lines, max_gap_limit)
    first_line = min(lines, key=lambda line: line.start) if lines else None
    leading_gap = (
        first_line.start
        if first_line is not None
        and role in {"japanese", "source"}
        and first_line.start > max_leading_gap_limit
        else 0.0
    )

    if empty_text:
        issues.append(_issue("empty_dialogue_text", "fail", "Subtitle contains empty dialogue text.", empty_text))
    if invalid_timing:
        issues.append(_issue("invalid_timing", "fail", "Subtitle has a non-positive cue duration.", invalid_timing))
    if hard_short:
        issues.append(_issue("too_short", "fail", "Subtitle cue is too brief to be readable.", hard_short))
    elif short_duration:
        issues.append(_issue("short_duration", "warn", "Subtitle cue is shorter than the readability target.", short_duration))
    if fail_cps:
        issues.append(_issue("cps_too_high", "fail", "Subtitle reading speed exceeds the hard CPS limit.", fail_cps))
    elif warn_cps:
        issues.append(_issue("cps_high", "warn", "Subtitle reading speed exceeds the comfort CPS target.", warn_cps))
    if overlaps:
        issues.append(
            SubtitleQualityIssue(
                code="timing_overlap",
                severity="fail",
                message="Subtitle cues overlap beyond the configured tolerance.",
                count=len(overlaps),
                samples=[f"#{previous.index}->#{current.index} {seconds:.3f}s" for previous, current, seconds in overlaps[:5]],
                indexes=sorted({line.index for previous, current, _seconds in overlaps for line in (previous, current)}),
            )
        )
    if prompt_echoes:
        issues.append(
            _issue(
                "asr_prompt_echo",
                "fail",
                "ASR output contains an echoed transcription instruction.",
                prompt_echoes,
            )
        )
    if hallucinations:
        issues.append(_issue("hallucination_text", "fail", "Subtitle contains known ASR hallucination text.", hallucinations))
    if residual_kana:
        issues.append(_issue("residual_japanese_kana", "fail", "Translated subtitle still contains long Japanese kana text.", residual_kana))
    if prompt_leaks:
        issues.append(_issue("translation_prompt_leak", "fail", "Translated subtitle contains leaked model instructions.", prompt_leaks))
    if runaway_repetitions:
        issues.append(
            _issue(
                "translation_runaway_repetition",
                "fail",
                "Translated subtitle contains runaway repeated model output.",
                runaway_repetitions,
            )
        )
    if simplified_remnants:
        issues.append(
            _issue(
                "simplified_chinese_remnant",
                "fail",
                "Traditional Chinese output contains high-confidence Simplified Chinese characters.",
                simplified_remnants,
            )
        )
    if inconsistent_glossary:
        issues.append(
            _issue(
                "glossary_term_inconsistent",
                "fail",
                "A configured source term is present but its approved translated term is missing.",
                inconsistent_glossary,
            )
        )
    if repeated_punctuation:
        issues.append(
            _issue(
                "repeated_punctuation",
                "warn",
                "Subtitle contains excessive repeated punctuation.",
                repeated_punctuation,
            )
        )
    if repeated_cues:
        issues.append(
            _issue(
                "repeated_consecutive_cues",
                "fail",
                "Subtitle repeats the same cue text abnormally across consecutive events.",
                repeated_cues,
            )
        )
    if very_long:
        issues.append(_issue("very_long_line", "fail", "Subtitle line is too long to read comfortably.", very_long))
    elif overlong:
        issues.append(_issue("long_line", "warn", "Subtitle line is longer than the reading comfort target.", overlong))
    if long_durations:
        issues.append(_issue("long_duration", "warn", "Subtitle line stays on screen longer than the target.", long_durations))
    if gaps:
        issues.append(
            SubtitleQualityIssue(
                code="large_gap",
                severity="warn",
                message="Subtitle has large gaps; this can be OP/ED/silence, but should be visible in WebUI.",
                count=len(gaps),
                samples=[f"{gap:.2f}s" for gap in gaps[:5]],
            )
        )
    if leading_gap and first_line is not None:
        issues.append(
            SubtitleQualityIssue(
                code="leading_gap",
                severity="warn",
                message=(
                    "The first subtitle starts unusually late; the opening or first spoken lines may be missing."
                ),
                count=1,
                samples=[f"0.00s -> {leading_gap:.2f}s"],
                indexes=[first_line.index],
            )
        )

    status = "rerun" if any(issue.severity == "fail" for issue in issues) else "check" if issues else "watchable"
    score = _score(issues)
    return SubtitleQualityReport(
        path=str(path),
        role=role,
        status=status,
        score=score,
        dialogues=len(lines),
        avg_duration=round(sum(durations) / len(durations), 2) if durations else 0.0,
        max_duration=round(max(durations), 2) if durations else 0.0,
        avg_primary_chars=round(sum(primary_lengths) / len(primary_lengths), 1) if primary_lengths else 0.0,
        max_primary_chars=max(primary_lengths) if primary_lengths else 0,
        gaps_over_limit=len(gaps) + (1 if leading_gap else 0),
        largest_gap=round(max([*gaps, leading_gap]), 2) if gaps or leading_gap else 0.0,
        issues=issues,
    )


def quality_report_path(subtitle_path: str | Path) -> Path:
    path = Path(subtitle_path)
    return path.with_name(path.name + ".quality.json")


def managed_quality_report_path(subtitle_path: str | Path, work_path: str | Path) -> Path:
    path = Path(subtitle_path)
    # Keep this lexical so Worker and WebUI derive the same key even when the
    # WebUI container intentionally does not mount the media library.
    normalized = str(path.absolute())
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:24]
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name).strip("._-")[:96] or "subtitle"
    return Path(work_path) / QUALITY_REPORT_DIRECTORY / f"{safe_name}.{digest}.quality.json"


def quality_report_candidates(subtitle_path: str | Path, work_path: str | Path) -> tuple[Path, ...]:
    managed = managed_quality_report_path(subtitle_path, work_path)
    legacy = quality_report_path(subtitle_path)
    return tuple(dict.fromkeys((managed, legacy)))


def write_quality_report(report: SubtitleQualityReport, output_path: str | Path | None = None) -> Path:
    path = Path(output_path) if output_path is not None else quality_report_path(report.path)
    atomic_write_text(path, json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def summarize_quality_report(report: SubtitleQualityReport) -> str:
    if not report.issues:
        return f"subtitle quality watchable score={report.score} dialogues={report.dialogues}"
    worst = "fail" if report.has_failures else "warn"
    issue_codes = ",".join(issue.code for issue in report.issues[:4])
    return f"subtitle quality {worst} status={report.status} score={report.score} issues={issue_codes}"


def add_translation_quality_events(
    report: SubtitleQualityReport,
    events: Iterable[dict[str, Any]],
) -> SubtitleQualityReport:
    """Attach durable translator warnings to a translated subtitle report."""

    if not _should_check_translation_role(report.role):
        return report
    omissions: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or str(event.get("code") or "") != TRANSLATION_SAFE_OMISSION:
            continue
        try:
            index = int(event.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if index <= 0:
            continue
        omissions.append({**event, "index": index})
    if not omissions:
        return report

    omissions.sort(key=lambda event: int(event["index"]))
    indexes = list(dict.fromkeys(int(event["index"]) for event in omissions))
    samples: list[str] = []
    for event in omissions[:5]:
        source = _clean_text(str(event.get("source") or ""))
        output = _clean_text(str(event.get("output") or ""))
        sample = f"#{event['index']} {source}"
        if output:
            sample += f" → {output}"
        samples.append(sample[:240])
    issue = SubtitleQualityIssue(
        code=TRANSLATION_SAFE_OMISSION,
        severity="fail",
        message=f"有 {len(indexes)} 行翻譯未成功；此字幕不會發布，必須先重翻問題行。",
        count=len(indexes),
        samples=samples,
        indexes=indexes,
    )
    issues = [existing for existing in report.issues if existing.code != TRANSLATION_SAFE_OMISSION]
    issues.append(issue)
    status = "rerun" if any(item.severity == "fail" for item in issues) else "check"
    return replace(report, status=status, score=_score(issues), issues=issues)


def infer_subtitle_role(path: Path) -> str:
    name = path.name.casefold()
    if "原語言" in path.name or "原语言" in path.name or "source" in name:
        return "source"
    if ".ja." in name or "日本語" in path.name or "日語" in path.name or "日语" in path.name:
        return "japanese"
    if ".zh" in name or "繁" in path.name or "简" in path.name or "簡" in path.name:
        return "translated"
    return "unknown"


def _read_subtitle_lines(path: Path) -> list[SubtitleLine]:
    suffix = path.suffix.lower()
    if suffix == ".srt":
        return _srt_lines(read_srt(path))
    if suffix == ".ass":
        return _ass_lines(path)
    raise ValueError(f"Unsupported subtitle file extension: {path}")


def _srt_lines(blocks: Iterable[SrtBlock]) -> list[SubtitleLine]:
    lines: list[SubtitleLine] = []
    for block in blocks:
        timing = block.timing.strip()
        match = SRT_TIMESTAMP_RE.match(timing)
        if not match:
            continue
        start = _srt_seconds(match, "")
        end = _srt_seconds(match, "e")
        text = " ".join(line.strip() for line in block.text if line.strip())
        lines.append(SubtitleLine(block.index, start, end, text, text))
    return lines


def _ass_lines(path: Path) -> list[SubtitleLine]:
    lines: list[SubtitleLine] = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.startswith(ASS_DIALOGUE_PREFIX):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        start = _ass_seconds(parts[1].strip())
        end = _ass_seconds(parts[2].strip())
        text = parts[9].strip()
        primary = text.split(r"\N", 1)[0]
        lines.append(
            SubtitleLine(
                index=len(lines) + 1,
                start=start,
                end=end,
                text=_plain_ass_text(text),
                primary_text=_plain_ass_text(primary),
            )
        )
    return lines


def _ass_seconds(value: str) -> float:
    match = ASS_TIMESTAMP_RE.match(value)
    if not match:
        return 0.0
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(match.group("cs")) / 100
    )


def _srt_seconds(match: re.Match[str], prefix: str) -> float:
    return (
        int(match.group(f"{prefix}h")) * 3600
        + int(match.group(f"{prefix}m")) * 60
        + int(match.group(f"{prefix}s"))
        + int(match.group(f"{prefix}ms")) / 1000
    )


def _plain_ass_text(text: str) -> str:
    cleaned = ASS_OVERRIDE_RE.sub("", text)
    cleaned = cleaned.replace(r"\N", " ")
    cleaned = cleaned.replace(r"\n", " ")
    return _clean_text(cleaned)


def _clean_text(text: str) -> str:
    return SPACE_RE.sub(" ", str(text or "").strip())


def _display_length(text: str) -> int:
    return len(SPACE_RE.sub("", text or ""))


def _cps_exceeds(line: SubtitleLine, limit: float) -> bool:
    """Compare CPS without treating an exact boundary as a float overflow."""

    if line.duration <= 0:
        return False
    return _display_length(line.primary_text) > (float(limit) * line.duration) + 1e-6


def _large_gaps(lines: list[SubtitleLine], limit: float) -> list[float]:
    gaps: list[float] = []
    previous_end: float | None = None
    for line in sorted(lines, key=lambda item: item.start):
        if previous_end is not None:
            gap = line.start - previous_end
            if gap > limit:
                gaps.append(gap)
        previous_end = line.end
    return gaps


def _overlapping_lines(
    lines: list[SubtitleLine],
    tolerance: float,
) -> list[tuple[SubtitleLine, SubtitleLine, float]]:
    overlaps: list[tuple[SubtitleLine, SubtitleLine, float]] = []
    ordered = sorted(lines, key=lambda item: (item.start, item.end, item.index))
    previous: SubtitleLine | None = None
    for line in ordered:
        if previous is not None:
            overlap = previous.end - line.start
            if overlap > tolerance:
                overlaps.append((previous, line, overlap))
            if line.end > previous.end:
                previous = line
        else:
            previous = line
    return overlaps


def _inconsistent_glossary_lines(
    lines: list[SubtitleLine],
    config: Any | None,
    role: str,
) -> list[SubtitleLine]:
    if not _should_check_translation_role(role):
        return []
    glossary = dict(getattr(config, "translation_glossary", {}) or {})
    if not glossary:
        return []
    inconsistent: list[SubtitleLine] = []
    for line in lines:
        full_text = _clean_text(line.text)
        primary = _clean_text(line.primary_text)
        for source, target in glossary.items():
            source_text = str(source).strip()
            target_text = str(target).strip()
            if source_text and target_text and source_text in full_text and target_text not in primary:
                inconsistent.append(line)
                break
    return inconsistent


def _repeated_consecutive_cues(lines: list[SubtitleLine], minimum_run: int = 5) -> list[SubtitleLine]:
    repeated: list[SubtitleLine] = []
    run: list[SubtitleLine] = []
    previous_text = ""
    for line in sorted(lines, key=lambda item: (item.start, item.index)):
        text = _clean_text(line.primary_text).casefold()
        if text and text == previous_text:
            run.append(line)
        else:
            if len(run) >= minimum_run:
                repeated.extend(run)
            run = [line]
            previous_text = text
    if len(run) >= minimum_run:
        repeated.extend(run)
    return repeated


def _issue(code: str, severity: str, message: str, lines: list[SubtitleLine]) -> SubtitleQualityIssue:
    return SubtitleQualityIssue(
        code=code,
        severity=severity,
        message=message,
        count=len(lines),
        samples=[line.primary_text[:80] for line in lines[:5]],
        indexes=[line.index for line in lines],
    )


def _is_hallucination(text: str, config: Any | None) -> bool:
    try:
        from transcriber import _is_hallucination_text

        return _is_hallucination_text(text, config)  # type: ignore[arg-type]
    except Exception:
        return False


def _should_check_translation_role(role: str) -> bool:
    return role in {
        "translated",
        "translated_zh_cn",
        "translated_zh_tw",
        "zh",
        "zh-cn",
        "zh-tw",
    }


def _should_check_traditional_chinese_role(role: str) -> bool:
    return role in {"translated", "translated_zh_tw", "zh", "zh-tw"}


def _score(issues: list[SubtitleQualityIssue]) -> int:
    score = 100
    for issue in issues:
        weight = 25 if issue.severity == "fail" else 8
        score -= min(35, weight + max(0, issue.count - 1))
    return max(0, score)


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze subtitle viewing quality.")
    parser.add_argument("paths", nargs="+", help="ASS/SRT subtitle path(s).")
    parser.add_argument("--role", choices=["translated", "japanese", "source", "unknown"], default=None)
    parser.add_argument("--write", action="store_true", help="Write reports under the Worker work directory.")
    parser.add_argument("--work-path", default=os.environ.get("WORK_PATH", "/work"))
    args = parser.parse_args()

    reports = [analyze_subtitle_file(path, role=args.role) for path in args.paths]
    for report in reports:
        if args.write:
            write_quality_report(report, managed_quality_report_path(report.path, args.work_path))
        print(json.dumps(report.to_dict(), ensure_ascii=False))
    return 1 if any(report.has_failures for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
