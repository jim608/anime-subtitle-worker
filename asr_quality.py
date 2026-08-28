from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Iterable


# Keep historical prompts here even after they are removed from the active
# configuration.  Older generated subtitles must remain detectable so they can
# be quarantined and rebuilt safely.
KNOWN_ASR_PROMPT_TEXTS = (
    (
        "日本語のアニメの会話と、オープニング・エンディング・挿入歌の歌詞。"
        "台詞と歌詞を省略せず、句読点（。、！？）を自然に付けて正確に転写してください。"
    ),
    (
        "日本語のアニメのオープニング・エンディング・挿入歌の歌詞。"
        "歌詞を省略せず、聞こえた日本語を正確に転写してください。"
    ),
)

# These phrases are not part of our prompt, but Whisper frequently invents
# them over silence, title cards, and music.  They are kept separate from the
# prompt list so diagnostics can explain whether an instruction or a generic
# platform phrase polluted the transcript.
KNOWN_ASR_HALLUCINATION_TEXTS = (
    "ご視聴ありがとうございました。",
    "チャンネル登録をお願いいたします。",
    "字幕をオンにしてご視聴ください。チャンネル登録をお願いいたします。",
    "字幕オンしてご視聴ください。チャンネル登録をお願いいたします。",
)


def compact_asr_text(text: str) -> str:
    return "".join(str(text or "").split()).casefold()


def asr_prompt_echo_reason(text: str, config: Any | None = None) -> str | None:
    """Return a reason when ASR text is probably an echoed instruction.

    Whisper can emit an ``initial_prompt`` during silence or music.  The echo
    may be split into several short subtitle chunks, so the matcher accepts a
    compact ordered fragment only when nearly every candidate character comes
    from a known prompt.  Ordinary dialogue that merely mentions a song or
    anime does not satisfy that coverage requirement.
    """

    markers = tuple(
        marker
        for prompt in _configured_prompt_texts(config)
        if len(marker := compact_asr_text(prompt)) >= 8
    )
    return _artifact_match_reason(
        text,
        markers,
        signal=_has_prompt_echo_signal,
        full_reason="full_prompt",
        fragment_reason="prompt_fragment",
        ordered_reason="ordered_prompt_fragment",
        minimum_candidate_coverage=0.88,
    )


def asr_artifact_reason(text: str, config: Any | None = None) -> str | None:
    """Return a reason for either a prompt echo or a known ASR hallucination."""

    prompt_reason = asr_prompt_echo_reason(text, config)
    if prompt_reason is not None:
        return prompt_reason
    markers = tuple(compact_asr_text(value) for value in KNOWN_ASR_HALLUCINATION_TEXTS)
    return _artifact_match_reason(
        text,
        markers,
        signal=_has_hallucination_signal,
        full_reason="full_hallucination",
        fragment_reason="hallucination_fragment",
        ordered_reason="ordered_hallucination_fragment",
        minimum_candidate_coverage=0.70,
    )


def asr_prompt_echo_line_indexes(
    texts: Iterable[str],
    config: Any | None = None,
    *,
    max_window: int = 8,
) -> set[int]:
    """Return zero-based line indexes belonging to a prompt echo.

    A shortest-window scan catches output such as ``日本ニメ`` / ``ング`` /
    ``挿入`` / ``歌`` while avoiding an unbounded concatenation with unrelated
    dialogue later in the episode.
    """

    return _artifact_line_indexes(
        texts,
        reason_for=lambda value: asr_prompt_echo_reason(value, config),
        window_signal=_has_prompt_window_signal,
        max_window=max_window,
    )


def asr_artifact_line_indexes(
    texts: Iterable[str],
    config: Any | None = None,
    *,
    max_window: int = 8,
) -> set[int]:
    """Return indexes belonging to prompt echoes or common ASR inventions."""

    return _artifact_line_indexes(
        texts,
        reason_for=lambda value: asr_artifact_reason(value, config),
        window_signal=_has_artifact_window_signal,
        max_window=max_window,
    )


def _configured_prompt_texts(config: Any | None) -> tuple[str, ...]:
    prompts = list(KNOWN_ASR_PROMPT_TEXTS)
    if config is not None:
        for name in ("whisper_initial_prompt", "op_ed_initial_prompt"):
            value = getattr(config, name, None)
            if isinstance(value, str) and value.strip():
                prompts.append(value)
    # Preserve order while avoiding duplicate fuzzy comparisons.
    return tuple(dict.fromkeys(prompts))


def _artifact_match_reason(
    text: str,
    markers: tuple[str, ...],
    *,
    signal,
    full_reason: str,
    fragment_reason: str,
    ordered_reason: str,
    minimum_candidate_coverage: float,
) -> str | None:
    candidate = compact_asr_text(text)
    if len(candidate) < 8:
        return None

    for marker in markers:
        if marker in candidate:
            return full_reason
        if len(candidate) >= 12 and candidate in marker:
            return fragment_reason

    # SequenceMatcher is intentionally behind this cheap discriminator.  A
    # full episode contains hundreds of ordinary lines and would otherwise
    # require thousands of fuzzy comparisons during library audits.
    if not signal(candidate):
        return None

    for marker in markers:
        matcher = SequenceMatcher(None, candidate, marker, autojunk=False)
        matching_blocks = [block for block in matcher.get_matching_blocks() if block.size]
        matching_chars = sum(block.size for block in matching_blocks)
        candidate_coverage = matching_chars / len(candidate)
        length_ratio = len(candidate) / len(marker)
        if (
            candidate_coverage >= minimum_candidate_coverage
            and length_ratio <= 1.20
            and (matcher.ratio() >= 0.45 or len(matching_blocks) >= 3)
        ):
            return ordered_reason
    return None


def _artifact_line_indexes(
    texts: Iterable[str],
    *,
    reason_for,
    window_signal,
    max_window: int,
) -> set[int]:
    values = [str(text or "") for text in texts]
    if not values:
        return set()

    maximum = max(1, int(max_window))
    compact_values = [compact_asr_text(value) for value in values]
    matched: set[int] = set()

    for index, value in enumerate(values):
        if reason_for(value) is not None:
            matched.add(index)

    candidate_starts = {
        index
        for index, compact in enumerate(compact_values)
        if len(compact) <= 24 and window_signal(compact)
    }
    for start in sorted(candidate_starts):
        combined = ""
        found_fragment = False
        for end in range(start, min(len(values), start + maximum)):
            combined += values[end]
            if len(compact_asr_text(combined)) > 280:
                break
            reason = reason_for(combined)
            if reason is None:
                if found_fragment:
                    break
                continue
            matched.update(range(start, end + 1))
            found_fragment = True
            if reason.startswith("full_"):
                break
            next_index = end + 1
            if next_index >= len(values):
                break
            next_value = compact_values[next_index]
            if len(next_value) > 8 or (
                len(next_value) >= 6 and not _has_artifact_tail_signal(next_value)
            ):
                break
    return matched


def _has_prompt_echo_signal(candidate: str) -> bool:
    if any(
        token in candidate
        for token in (
            "転写",
            "省略",
            "句読点",
            "オープニング",
            "エンディング",
            "挿入歌",
        )
    ):
        return True
    if "挿入" in candidate and any(
        token in candidate for token in ("日本", "ニメ", "ング", "歌詞")
    ):
        return True
    return "日本" in candidate and "ニメ" in candidate and "ング" in candidate


def _has_prompt_window_signal(candidate: str) -> bool:
    return any(
        token in candidate
        for token in (
            "日本",
            "ニメ",
            "ング",
            "挿入",
            "歌詞",
            "台詞",
            "転写",
            "省略",
            "句読",
            "オープ",
            "エンディ",
        )
    )


def _has_hallucination_signal(candidate: str) -> bool:
    return any(
        token in candidate
        for token in (
            "ご視",
            "視聴",
            "聴",
            "聴有",
            "座いました",
            "チャンネル",
            "登録",
            "字幕オン",
            "字幕をオン",
        )
    )


def _has_artifact_window_signal(candidate: str) -> bool:
    return _has_prompt_window_signal(candidate) or _has_hallucination_signal(candidate)


def _has_artifact_tail_signal(candidate: str) -> bool:
    return any(
        token in candidate
        for token in (
            "聴",
            "座いました",
            "チャンネル",
            "登録",
            "お願",
            "ください",
            "転写",
            "句読",
            "正確",
            "省略",
            "オープ",
            "エンディ",
            "挿入",
            "歌詞",
        )
    )
