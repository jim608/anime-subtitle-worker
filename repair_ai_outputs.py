from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import errno
import hashlib
import sys
from pathlib import Path
import re
import time
from typing import Any, Callable

from config import load_config
from safe_files import verified_move
from subtitle_paths import source_transcript_ass_suffix_for_language
from transcriber import _is_hallucination_text
from worker import _canonical_ai_role_from_name


ASS_OVERRIDE_RE = re.compile(r"\{[^{}]*\}")


@dataclass
class RepairAction:
    action: str
    path: Path
    target: Path | None = None
    detail: str = ""


@dataclass
class RepairSummary:
    scanned_ass: int = 0
    renamed: int = 0
    rename_conflicts: int = 0
    conflict_archived: int = 0
    conflict_source_deleted: int = 0
    duplicate_removed: int = 0
    rewritten: int = 0
    hallucination_lines_removed: int = 0
    invalid_name_quarantined: int = 0
    quarantine_failures: int = 0
    scan_errors: int = 0
    stopped_early: bool = False
    actions: list[RepairAction] = field(default_factory=list)


ActionCallback = Callable[[RepairAction], None]
ProgressCallback = Callable[[RepairSummary], None]


def repair_tree(
    root: str | Path,
    config: Any,
    *,
    apply: bool = False,
    include_non_ai: bool = False,
    max_seconds: float | None = None,
    action_callback: ActionCallback | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_interval_seconds: float = 10.0,
    conflict_policy: str = "keep",
    conflict_backup_root: str | Path | None = None,
) -> RepairSummary:
    root_path = Path(root)
    summary = RepairSummary()
    normalized_conflict_policy = str(conflict_policy or "keep").strip().lower()
    backup_root = Path(conflict_backup_root) if conflict_backup_root is not None else None
    work_path = Path(getattr(config, "work_path", root_path.parent))
    # ``delete-source`` existed in older maintenance commands.  Preserve the
    # CLI/API spelling for compatibility, but never permanently delete a
    # conflicting subtitle whose bytes differ from the canonical target.
    # Archive it under /work so every repair remains reversible.
    if normalized_conflict_policy == "delete-source":
        normalized_conflict_policy = "archive-source"
    if normalized_conflict_policy == "archive-source" and backup_root is None:
        backup_root = work_path / "repair_ai_conflicts" / time.strftime("%Y%m%d-%H%M%S")
    quarantine_root = work_path / "repair_ai_quarantine" / time.strftime("%Y%m%d-%H%M%S")
    excluded_roots = tuple(
        candidate.resolve()
        for candidate in (backup_root, quarantine_root)
        if candidate is not None
    )
    deadline = time.monotonic() + max_seconds if max_seconds and max_seconds > 0 else None
    next_progress = (
        time.monotonic() + progress_interval_seconds
        if progress_callback is not None and progress_interval_seconds > 0
        else None
    )
    for ass_path in root_path.rglob("*.ass"):
        try:
            resolved_ass = ass_path.resolve()
        except OSError:
            resolved_ass = ass_path.absolute()
        if any(_is_relative_to(resolved_ass, excluded) for excluded in excluded_roots):
            continue
        if deadline is not None and time.monotonic() >= deadline:
            summary.stopped_early = True
            break
        try:
            is_file = ass_path.is_file()
        except OSError as exc:
            summary.scan_errors += 1
            _record_action(
                summary,
                RepairAction("skip_source_error", ass_path, detail=_compact_os_error(exc)),
                action_callback,
            )
            continue
        if not is_file:
            continue
        summary.scanned_ass += 1
        try:
            current_path = _repair_ass_name(
                ass_path,
                config,
                apply=apply,
                summary=summary,
                action_callback=action_callback,
                conflict_policy=normalized_conflict_policy,
                conflict_backup_root=backup_root,
                quarantine_root=quarantine_root,
                root_path=root_path,
            )
            _repair_ass_hallucination_lines(
                current_path,
                config,
                apply=apply,
                include_non_ai=include_non_ai,
                summary=summary,
                action_callback=action_callback,
            )
        except OSError as exc:
            summary.scan_errors += 1
            _record_action(
                summary,
                RepairAction("skip_source_error", ass_path, detail=_compact_os_error(exc)),
                action_callback,
            )
        if next_progress is not None and time.monotonic() >= next_progress:
            progress_callback(summary)
            next_progress = time.monotonic() + progress_interval_seconds
    return summary


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _repair_ass_name(
    path: Path,
    config: Any,
    *,
    apply: bool,
    summary: RepairSummary,
    action_callback: ActionCallback | None = None,
    conflict_policy: str = "keep",
    conflict_backup_root: Path | None = None,
    quarantine_root: Path | None = None,
    root_path: Path | None = None,
) -> Path:
    target = _canonical_source_transcript_ass_target(path, config) or _canonical_ai_ass_target(path, config)
    if target is None or target == path:
        return path
    try:
        target_exists = target.exists()
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG and quarantine_root is not None:
            quarantine_target = _invalid_name_quarantine_path(
                path,
                root_path or path.parent,
                quarantine_root,
            )
            _record_action(
                summary,
                RepairAction(
                    "quarantine_invalid_name",
                    path,
                    quarantine_target,
                    f"canonical target is invalid: {_compact_os_error(exc)}",
                ),
                action_callback,
            )
            if apply:
                try:
                    quarantine_target.parent.mkdir(parents=True, exist_ok=True)
                    _move_file(path, quarantine_target)
                except OSError as move_exc:
                    summary.quarantine_failures += 1
                    _record_action(
                        summary,
                        RepairAction(
                            "quarantine_failed",
                            path,
                            quarantine_target,
                            _compact_os_error(move_exc),
                        ),
                        action_callback,
                    )
                    return path
            summary.invalid_name_quarantined += 1
            return quarantine_target if apply else path
        _record_action(
            summary,
            RepairAction("skip_target_error", path, target, _compact_os_error(exc)),
            action_callback,
        )
        return path
    if target_exists:
        if _same_file_content(path, target):
            summary.duplicate_removed += 1
            _record_action(
                summary,
                RepairAction("remove_duplicate", path, target, "target already has identical content"),
                action_callback,
            )
            if apply:
                path.unlink()
            return target
        summary.rename_conflicts += 1
        effective_conflict_policy = "archive-source" if conflict_policy == "delete-source" else conflict_policy
        if effective_conflict_policy == "archive-source":
            archive_target = _conflict_archive_path(
                path,
                root_path or path.parent,
                conflict_backup_root or path.parent / ".repair_ai_conflicts",
            )
            summary.conflict_archived += 1
            _record_action(
                summary,
                RepairAction("archive_conflict_source", path, archive_target, "target already exists"),
                action_callback,
            )
            if apply:
                archive_target.parent.mkdir(parents=True, exist_ok=True)
                _move_file(path, archive_target)
            return target
        _record_action(summary, RepairAction("rename_conflict", path, target, "target already exists"), action_callback)
        return path
    summary.renamed += 1
    _record_action(summary, RepairAction("rename", path, target), action_callback)
    if apply:
        path.replace(target)
        return target
    return path


def _invalid_name_quarantine_path(path: Path, root_path: Path, quarantine_root: Path) -> Path:
    try:
        relative = path.relative_to(root_path)
    except ValueError:
        relative = Path(path.name)
    safe_parents = [_short_archive_component(part, max_bytes=96) for part in relative.parts[:-1]]
    digest = hashlib.sha256(str(relative).encode("utf-8", errors="replace")).hexdigest()[:16]
    suffix = path.suffix if path.suffix and len(path.suffix.encode("utf-8")) <= 20 else ".ass"
    stem = path.name[: -len(path.suffix)] if path.suffix else path.name
    safe_stem = _short_archive_component(stem, max_bytes=120)
    safe_name = f"{safe_stem}.invalid-{digest}{suffix}"
    return quarantine_root.joinpath(*safe_parents, safe_name)


def _short_archive_component(value: str, *, max_bytes: int) -> str:
    normalized = str(value or "_").replace("/", "_").replace("\\", "_").strip() or "_"
    encoded = normalized.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return normalized
    digest = hashlib.sha1(encoded).hexdigest()[:10]
    budget = max(8, max_bytes - len(digest) - 1)
    shortened = encoded[:budget]
    while shortened:
        try:
            prefix = shortened.decode("utf-8")
            break
        except UnicodeDecodeError:
            shortened = shortened[:-1]
    else:
        prefix = "file"
    return f"{prefix.rstrip() or 'file'}-{digest}"


def _conflict_archive_path(path: Path, root_path: Path, backup_root: Path) -> Path:
    try:
        relative = path.relative_to(root_path)
    except ValueError:
        relative = Path(path.name)
    archive_path = backup_root / relative
    if not archive_path.exists():
        return archive_path
    stem = archive_path.stem
    suffix = archive_path.suffix
    for index in range(1, 10000):
        candidate = archive_path.with_name(f"{stem}.conflict{index}{suffix}")
        if not candidate.exists():
            return candidate
    digest = hashlib.sha1(str(path).encode("utf-8", errors="replace")).hexdigest()[:12]
    return archive_path.with_name(f"{stem}.conflict-{digest}{suffix}")


def _move_file(source: Path, target: Path) -> None:
    verified_move(source, target)


def _record_action(
    summary: RepairSummary,
    action: RepairAction,
    action_callback: ActionCallback | None = None,
) -> None:
    summary.actions.append(action)
    if action_callback is not None:
        action_callback(action)


def _same_file_content(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        return _file_sha256(left) == _file_sha256(right)
    except OSError:
        return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_ai_ass_target(path: Path, config: Any) -> Path | None:
    name = path.name
    lowered = name.casefold()
    if ".ai" not in lowered or not lowered.endswith(".ass"):
        return None
    role = _canonical_ai_role_from_name(lowered)
    if role is None:
        return None
    ai_marker_index = lowered.find(".ai")
    if ai_marker_index < 0:
        return None
    video_stem = name[:ai_marker_index]
    suffix = {
        "ja": config.ai_japanese_ass_suffix,
        "zh_cn": config.ai_simplified_chinese_ass_suffix,
        "zh_tw": config.ai_traditional_chinese_ass_suffix,
    }[role]
    target = path.with_name(f"{video_stem}{suffix}")
    return target


def _canonical_source_transcript_ass_target(path: Path, config: Any) -> Path | None:
    name = path.name
    lowered = name.casefold()
    if ".ai" not in lowered or not lowered.endswith(".ass"):
        return None
    if _canonical_ai_role_from_name(lowered) is not None:
        return None
    ai_marker_index = lowered.find(".ai")
    if ai_marker_index < 0:
        return None
    video_stem = name[:ai_marker_index]
    raw_language = name[ai_marker_index + len(".ai") :]
    language = _canonical_source_language_tag(raw_language)
    if not language or language in {"ja", "zh", "zh-cn", "zh-tw"}:
        return None
    return path.with_name(f"{video_stem}{source_transcript_ass_suffix_for_language(config, language)}")


def _canonical_source_language_tag(value: str) -> str:
    normalized = str(value or "").strip().casefold().replace("_", "-")
    if not normalized or len(normalized) > 80:
        return ""
    tokens = [token for token in re.split(r"[^a-z0-9]+", normalized) if token]
    if not tokens:
        return normalized if len(normalized) <= 32 else ""
    if len(tokens) > 6:
        return ""
    mappings = {
        "en": "en",
        "eng": "en",
        "english": "en",
        "ja": "ja",
        "jp": "ja",
        "jpn": "ja",
        "japanese": "ja",
        "zh": "zh",
        "zho": "zh",
        "chi": "zh",
        "chinese": "zh",
        "ko": "ko",
        "kor": "ko",
        "korean": "ko",
        "es": "es",
        "spa": "es",
        "spanish": "es",
        "fr": "fr",
        "fra": "fr",
        "fre": "fr",
        "french": "fr",
        "de": "de",
        "deu": "de",
        "ger": "de",
        "german": "de",
        "it": "it",
        "ita": "it",
        "italian": "it",
        "pt": "pt",
        "por": "pt",
        "portuguese": "pt",
        "ru": "ru",
        "rus": "ru",
        "russian": "ru",
    }
    for token in reversed(tokens):
        if token in mappings:
            return mappings[token]
    for token in tokens:
        if token in mappings:
            return mappings[token]
    fallback = "-".join(tokens)
    return fallback if len(fallback) <= 32 else ""


def _repair_ass_hallucination_lines(
    path: Path,
    config: Any,
    *,
    apply: bool,
    include_non_ai: bool,
    summary: RepairSummary,
    action_callback: ActionCallback | None = None,
) -> None:
    try:
        if not path.exists() or not path.is_file():
            return
    except OSError as exc:
        _record_action(summary, RepairAction("skip_source_error", path, detail=_compact_os_error(exc)), action_callback)
        return
    if not include_non_ai and ".ai" not in path.name.casefold():
        return
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        _record_action(summary, RepairAction("skip_decode_error", path), action_callback)
        return

    output_lines: list[str] = []
    removed = 0
    for line in content.splitlines():
        if _is_hallucination_ass_dialogue(line, config):
            removed += 1
            continue
        output_lines.append(line)

    if removed <= 0:
        return
    summary.hallucination_lines_removed += removed
    summary.rewritten += 1
    _record_action(
        summary,
        RepairAction("remove_hallucination_lines", path, detail=f"removed={removed}"),
        action_callback,
    )
    if apply:
        path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8-sig", newline="\n")


def _is_hallucination_ass_dialogue(line: str, config: Any) -> bool:
    if not line.startswith("Dialogue:"):
        return False
    parts = line.split(",", 9)
    if len(parts) < 10:
        return False
    text = _plain_ass_text(parts[9])
    return _is_hallucination_text(text, config)


def _plain_ass_text(text: str) -> str:
    cleaned = ASS_OVERRIDE_RE.sub("", text)
    cleaned = cleaned.replace(r"\N", " ").replace(r"\n", " ")
    cleaned = cleaned.replace("\\N", " ").replace("\\n", " ")
    return " ".join(cleaned.split())


def _compact_os_error(exc: OSError, *, limit: int = 180) -> str:
    message = " ".join(str(exc).split())
    if not message:
        message = exc.__class__.__name__
    if len(message) > limit:
        return message[: limit - 3].rstrip() + "..."
    return message


def main() -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(description="Repair generated AI ASS outputs.")
    parser.add_argument("--root", default=None, help="Root folder to scan. Defaults to config input_path.")
    parser.add_argument("--config", default="config.yaml", help="Config YAML path.")
    parser.add_argument("--apply", action="store_true", help="Apply repairs. Without this flag the command is dry-run.")
    parser.add_argument(
        "--include-non-ai",
        action="store_true",
        help="Also remove hallucination-looking dialogue from non-AI ASS files. Default only repairs AI ASS files.",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="Stop scanning after this many seconds and print a partial summary.",
    )
    parser.add_argument("--show-actions", action="store_true", help="Print every planned or applied action.")
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=10.0,
        help="When --show-actions is used, print scan progress every N seconds. Use 0 to disable progress lines.",
    )
    parser.add_argument(
        "--conflict-policy",
        choices=("keep", "archive-source", "delete-source"),
        default="keep",
        help=(
            "How to handle rename conflicts when the canonical target already exists. "
            "keep leaves the legacy source in place; archive-source moves it to a backup folder; "
            "delete-source is retained as a compatibility alias for archive-source and never deletes "
            "different subtitle content."
        ),
    )
    parser.add_argument(
        "--conflict-backup-root",
        default=None,
        help="Backup folder used by --conflict-policy archive-source. Defaults to <work_path>/repair_ai_conflicts/<timestamp>.",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    root = Path(args.root) if args.root else Path(config.input_path)
    conflict_backup_root = _conflict_backup_root(args.conflict_backup_root, config)
    mode = "apply" if args.apply else "dry-run"
    action_callback: ActionCallback | None = None
    progress_callback: ProgressCallback | None = None
    if args.show_actions:
        print(f"scan_start: mode={mode} root={root}", flush=True)
        action_callback = lambda action: print(_format_action(action, root), flush=True)
        if args.progress_interval and args.progress_interval > 0:
            progress_callback = lambda current: print(_format_progress(current), flush=True)
    summary = repair_tree(
        root,
        config,
        apply=args.apply,
        include_non_ai=args.include_non_ai,
        max_seconds=args.max_seconds,
        action_callback=action_callback,
        progress_callback=progress_callback,
        progress_interval_seconds=args.progress_interval,
        conflict_policy=args.conflict_policy,
        conflict_backup_root=conflict_backup_root,
    )
    print(
        "repair_ai_outputs "
        f"mode={mode} root={root} scanned_ass={summary.scanned_ass} renamed={summary.renamed} "
        f"rename_conflicts={summary.rename_conflicts} conflict_archived={summary.conflict_archived} "
        f"conflict_source_deleted={summary.conflict_source_deleted} duplicate_removed={summary.duplicate_removed} rewritten={summary.rewritten} "
        f"hallucination_lines_removed={summary.hallucination_lines_removed} "
        f"invalid_name_quarantined={summary.invalid_name_quarantined} "
        f"quarantine_failures={summary.quarantine_failures} scan_errors={summary.scan_errors} "
        f"stopped_early={summary.stopped_early}",
        flush=True,
    )
    return 0


def _format_action(action: RepairAction, root: Path) -> str:
    target = f" -> {_display_path(action.target, root)}" if action.target is not None else ""
    detail = f" {action.detail}" if action.detail else ""
    return f"{action.action}: {_display_path(action.path, root)}{target}{detail}"


def _format_progress(summary: RepairSummary) -> str:
    return (
        "progress: "
        f"scanned_ass={summary.scanned_ass} renamed={summary.renamed} "
        f"rename_conflicts={summary.rename_conflicts} conflict_archived={summary.conflict_archived} "
        f"conflict_source_deleted={summary.conflict_source_deleted} duplicate_removed={summary.duplicate_removed} "
        f"rewritten={summary.rewritten} hallucination_lines_removed={summary.hallucination_lines_removed} "
        f"invalid_name_quarantined={summary.invalid_name_quarantined} "
        f"quarantine_failures={summary.quarantine_failures} scan_errors={summary.scan_errors}"
    )


def _conflict_backup_root(value: str | None, config: Any) -> Path | None:
    if value:
        return Path(value)
    work_path = getattr(config, "work_path", None)
    if work_path is None:
        return None
    return Path(work_path) / "repair_ai_conflicts" / time.strftime("%Y%m%d-%H%M%S")


def _display_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
        return str(relative)
    except ValueError:
        return path.name


def _configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
