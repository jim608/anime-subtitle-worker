from __future__ import annotations

import json
from pathlib import Path
import re
import time
from typing import Any, Iterable

from safe_files import atomic_write_text, sha256_file


PROMPT_LEAK_MARKERS = (
    "请逐行翻译下列字幕",
    "請逐行翻譯下列字幕",
    "每一行都必须输出",
    "每一行都必須輸出",
    "格式必须是：原编号<tab>",
    "格式必須是：原編號<tab>",
    "原编号<tab>中文字幕",
    "原編號<tab>中文字幕",
    "translation task:",
    "output format:",
    "system prompt:",
    "assistant preamble",
    "model preamble",
    "原始字幕：",
    "原始字幕:",
    "上次輸出：",
    "上次輸出:",
    "上次输出：",
    "上次输出:",
    "請只輸出修正後的一行中文字幕",
    "请只输出修正后的一行中文字幕",
    "不要包含日文假名",
    "不要保留日文假名",
    "問題行前後字幕參考",
    "问题行前后字幕参考",
    "只供理解，禁止輸出參考內容",
    "只供理解，禁止输出参考内容",
    "仍然只翻譯使用者訊息中的單一字幕行",
    "仍然只翻译使用者讯息中的单一字幕行",
)
RUNAWAY_REPEAT_RE = re.compile(r"(.{2,24})\1{5,}", re.DOTALL)
ROLE_PREFIX_RE = re.compile(r"(?:^|\s)(?:system|assistant|user|model)\s*:\s*", re.IGNORECASE)
KANA_WORD_RE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u30fc-\u30ff]+")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
TRANSLATION_SAFE_OMISSION = "translation_safe_omission"
TRANSLATION_SAFE_OMISSION_PLACEHOLDER = "……"
_HIRAGANA_RE = re.compile(r"[\u3041-\u3096]")
_KATAKANA_RE = re.compile(r"[\u30a1-\u30fa]")
_FRAGMENT_KANA_RE = re.compile(
    r"^[\u3041-\u3096\u30a1-\u30fa\u30fc・…!?！？。、〜～]+$"
)


class TranslationQualityEventsError(RuntimeError):
    pass


def translation_pollution_reason(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
    if not normalized:
        return None
    if any(marker in normalized for marker in PROMPT_LEAK_MARKERS):
        return "prompt_leak"
    # Context-repair echoes are not always verbatim. Catch the stable label
    # structure while requiring all three classes to avoid matching ordinary
    # dialogue that merely mentions Japanese or Chinese.
    if (
        any(label in normalized for label in ("問題行", "问题行"))
        and "日文" in normalized
        and "中文" in normalized
        and any(
            label in normalized
            for label in ("前一句", "下一句", "上一句", "前句", "後一句", "后一句")
        )
    ):
        return "prompt_leak"
    if ROLE_PREFIX_RE.search(normalized):
        return "prompt_leak"
    if len(normalized) >= 80 and RUNAWAY_REPEAT_RE.search(normalized):
        return "runaway_repetition"
    return None


def problematic_residual_kana(text: str) -> str | None:
    """Return kana that should not remain in a translated Chinese line.

    Short kana immediately following Chinese text can be an intentional name.
    This policy is shared by the translator and the final subtitle quality gate
    so a line cannot pass translation and then fail under a different rule.
    """
    value = str(text or "")
    for match in KANA_WORD_RE.finditer(value):
        kana = match.group(0)
        if len(kana) <= 5:
            left = value[max(0, match.start() - 3) : match.start()]
            if CJK_RE.search(left):
                continue
        return kana
    return None


def is_repetitive_kana_source(text: str) -> bool:
    """Return True for a kana-heavy refrain that needs repetition-safe repair."""

    compact = "".join(str(text or "").split())
    if _FRAGMENT_KANA_RE.fullmatch(compact) is None:
        return False
    kana = [
        char
        for char in compact
        if _HIRAGANA_RE.fullmatch(char) is not None
        or _KATAKANA_RE.fullmatch(char) is not None
    ]
    return (
        len(kana) >= 12
        and len(set(kana)) / len(kana) <= 0.35
    )


def translation_quality_events_path(translated_srt_path: str | Path) -> Path:
    """Return the durable quality-event sidecar for a translated SRT cache."""

    path = Path(translated_srt_path)
    return path.with_name(path.name + ".translation-events.json")


def translation_quality_hold_path(translated_srt_path: str | Path) -> Path:
    path = Path(translated_srt_path)
    return path.with_name(path.name + ".translation-hold.json")


def make_safe_omission_event(
    *,
    index: int,
    source: str,
    rejected_output: str,
    reason: str,
    batch_index: str,
) -> dict[str, Any]:
    return {
        "code": TRANSLATION_SAFE_OMISSION,
        "severity": "fail",
        "index": int(index),
        "message": "Translation omitted an unresolved line; the subtitle must not be published.",
        "source": _bounded_event_text(source, 240),
        "output": _bounded_event_text(rejected_output, 240),
        "reason": _bounded_event_text(reason, 320),
        "batch_index": str(batch_index),
    }


def read_translation_quality_events(translated_srt_path: str | Path) -> list[dict[str, Any]]:
    path = translation_quality_events_path(translated_srt_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_events = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(raw_events, list):
        return []
    return [event for raw in raw_events if (event := _normalize_quality_event(raw)) is not None]


def read_translation_quality_events_strict(
    translated_srt_path: str | Path,
) -> list[dict[str, Any]]:
    """Read the publication-gate sidecar without treating corruption as empty."""

    path = translation_quality_events_path(translated_srt_path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise
    except json.JSONDecodeError as exc:
        raise TranslationQualityEventsError(
            f"Translation quality event sidecar is corrupt: {path}"
        ) from exc
    raw_events = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(raw_events, list):
        raise TranslationQualityEventsError(
            f"Translation quality event sidecar has no valid events list: {path}"
        )
    expected_sha256 = str(payload.get("srt_sha256") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise TranslationQualityEventsError(
            f"Translation quality event sidecar lacks a canonical SRT SHA-256: {path}"
        )
    translated = Path(translated_srt_path)
    try:
        actual_sha256 = sha256_file(translated)
    except OSError as exc:
        raise TranslationQualityEventsError(
            f"Translation quality event sidecar target is unavailable: {translated}"
        ) from exc
    if actual_sha256 != expected_sha256:
        raise TranslationQualityEventsError(
            f"Translation quality event sidecar hash mismatch: {path}"
        )
    normalized: list[dict[str, Any]] = []
    for raw in raw_events:
        event = _normalize_quality_event(raw)
        if event is None:
            raise TranslationQualityEventsError(
                f"Translation quality event sidecar contains an invalid event: {path}"
            )
        normalized.append(event)
    return normalized


def write_translation_quality_events(
    translated_srt_path: str | Path,
    events: Iterable[dict[str, Any]],
    *,
    srt_sha256: str | None = None,
) -> Path:
    path = translation_quality_events_path(translated_srt_path)
    normalized = [event for raw in events if (event := _normalize_quality_event(raw)) is not None]
    if not normalized:
        path.unlink(missing_ok=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    expected_sha256 = str(srt_sha256 or "").strip()
    if not expected_sha256 and Path(translated_srt_path).is_file():
        expected_sha256 = sha256_file(translated_srt_path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise TranslationQualityEventsError(
            "Non-empty translation quality events require a canonical SRT SHA-256"
        )
    _write_translation_quality_event_payload(
        path,
        normalized,
        srt_sha256=expected_sha256,
    )
    return path


def write_translation_quality_hold(
    translated_srt_path: str | Path,
    *,
    srt_sha256: str,
    reason: str,
) -> Path:
    expected_sha256 = str(srt_sha256 or "").strip()
    if not expected_sha256:
        raise TranslationQualityEventsError(
            "Translation hold requires the planned SRT SHA-256"
        )
    path = translation_quality_hold_path(translated_srt_path)
    atomic_write_text(
        path,
        json.dumps(
            {
                "schema_version": 1,
                "status": "translation_commit_pending",
                "srt_path": str(Path(translated_srt_path)),
                "srt_sha256": expected_sha256,
                "reason": _bounded_event_text(reason, 320),
                "updated_at": time.time(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return path


def read_translation_quality_hold_strict(
    translated_srt_path: str | Path,
) -> dict[str, Any] | None:
    path = translation_quality_hold_path(translated_srt_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TranslationQualityEventsError(
            f"Translation hold marker is unreadable: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise TranslationQualityEventsError(
            f"Translation hold marker is invalid: {path}"
        )
    expected_sha256 = str(payload.get("srt_sha256") or "").strip()
    if (
        str(payload.get("status") or "") != "translation_commit_pending"
        or not expected_sha256
    ):
        raise TranslationQualityEventsError(
            f"Translation hold marker has an invalid contract: {path}"
        )
    translated = Path(translated_srt_path)
    try:
        actual_sha256 = sha256_file(translated)
    except OSError as exc:
        raise TranslationQualityEventsError(
            f"Translation hold target is unavailable: {translated}"
        ) from exc
    if actual_sha256 != expected_sha256:
        raise TranslationQualityEventsError(
            f"Translation hold target hash mismatch: {path}"
        )
    return payload


def fail_closed_translation_output(
    translated_srt_path: str | Path,
    *,
    reason: str,
) -> None:
    """Best-effort removal that never leaves a remaining SRT looking clean.

    This helper intentionally never raises so callers can preserve and
    re-raise the original sidecar persistence error.
    """

    output = Path(translated_srt_path)
    sidecar = translation_quality_events_path(output)
    hold = translation_quality_hold_path(output)
    try:
        output.unlink(missing_ok=True)
    except OSError:
        pass

    if not output.exists():
        for metadata in (sidecar, hold):
            try:
                metadata.unlink(missing_ok=True)
            except OSError:
                pass
        return

    invalid_event = {
        "code": TRANSLATION_SAFE_OMISSION,
        "severity": "fail",
        "index": 1,
        "message": (
            "Translation cache is invalid because its quality-event sidecar "
            "could not be persisted."
        ),
        "source": "",
        "output": "",
        "reason": _bounded_event_text(reason, 320),
        "batch_index": "sidecar-write-failure",
    }
    try:
        _write_translation_quality_event_payload(
            sidecar,
            [invalid_event],
            srt_sha256=sha256_file(output),
        )
        return
    except OSError:
        pass

    quarantine = output.with_name(
        f"{output.name}.untrusted-{time.time_ns()}"
    )
    try:
        output.replace(quarantine)
    except OSError:
        pass
    if not output.exists():
        for metadata in (sidecar, hold):
            try:
                metadata.unlink(missing_ok=True)
            except OSError:
                pass
        return

    # Last resort: an empty/invalid SRT cannot pass the normal SRT and
    # publication gates even when deletion, sidecar persistence, and rename
    # are all unavailable.
    try:
        output.write_text("", encoding="utf-8")
    except OSError:
        pass


def _write_translation_quality_event_payload(
    path: Path,
    events: list[dict[str, Any]],
    *,
    srt_sha256: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "updated_at": time.time(),
        "events": events,
    }
    if srt_sha256:
        payload["srt_sha256"] = str(srt_sha256)
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _normalize_quality_event(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        index = int(value.get("index") or 0)
    except (TypeError, ValueError):
        return None
    if index <= 0:
        return None
    code = str(value.get("code") or TRANSLATION_SAFE_OMISSION).strip()
    if not code:
        return None
    return {
        "code": code,
        "severity": str(value.get("severity") or "warn").strip() or "warn",
        "index": index,
        "message": _bounded_event_text(value.get("message") or "Translation quality warning.", 320),
        "source": _bounded_event_text(value.get("source") or "", 240),
        "output": _bounded_event_text(value.get("output") or "", 240),
        "reason": _bounded_event_text(value.get("reason") or "", 320),
        "batch_index": str(value.get("batch_index") or ""),
    }


def _bounded_event_text(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit] + "…"
