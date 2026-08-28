from __future__ import annotations

from pathlib import Path
from typing import Any
import re


_THEME_PARENT_NAMES = frozenset({"extra", "extras", "special", "specials", "theme", "themes", "themesongs"})
_EXACT_THEME_STEM_RE = re.compile(
    r"(?:s\d{1,3})?(?:nc)?(?:op|ed)(?:\d{1,3})?(?:v\d+)?",
    re.IGNORECASE,
)
_THEME_TOKEN_RE = re.compile(
    r"(?<![a-z0-9])(?:s\d{1,3})?(?:nc)?(?:op|ed)(?:\d{1,3})?(?:v\d+)?(?![a-z0-9])",
    re.IGNORECASE,
)


def is_standalone_theme_video(path: Path, config: Any) -> bool:
    """Return whether a video is a standalone OP/ED asset, not a normal episode."""

    if not bool(getattr(config, "scanner_skip_standalone_op_ed", True)):
        return False

    stem = str(path.stem or "").strip()
    compact_stem = re.sub(r"[^a-z0-9]+", "", stem.casefold())
    if _EXACT_THEME_STEM_RE.fullmatch(compact_stem):
        return True
    if compact_stem in {"opening", "ending", "creditlessopening", "creditlessending", "oped"}:
        return True

    normalized_parents = {
        re.sub(r"[^a-z0-9]+", "", str(part).casefold())
        for part in path.parent.parts
    }
    if not normalized_parents.intersection(_THEME_PARENT_NAMES):
        return False

    normalized_stem = re.sub(r"[_\.]+", " ", stem.casefold())
    if _THEME_TOKEN_RE.search(normalized_stem):
        return True
    return bool(re.search(r"\bcreditless[\s-]+(?:opening|ending)\b", normalized_stem))
