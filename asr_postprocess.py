"""Preserve accepted ASR evidence across deterministic SRT postprocessing.

Uses the existing ASR pending marker, not a separate job or recovery queue.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

from safe_files import atomic_write_text, sha256_file, verified_copy_replace
from srt_utils import write_srt


_OPERATION = "validated_asr_postprocess_v1"


def _checkpoint_paths(srt, config, input_sha, diagnostic_sha):
    from transcriber import asr_diagnostics_path, TranscriptionError

    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (input_sha, diagnostic_sha)):
        raise TranscriptionError("ASR postprocess checkpoint identity is invalid")
    directory = asr_diagnostics_path(srt, config).parent / "postprocess_checkpoints" / f"{input_sha}-{diagnostic_sha}"
    return directory, directory / "source.srt", directory / "diagnostics.json"


def recover_asr_postprocess(srt, config):
    """Restore a verified pre-transform pair after an interrupted pair commit."""
    from transcriber import asr_diagnostics_path, asr_transcription_hold_path, TranscriptionError

    srt = Path(srt)
    hold = asr_transcription_hold_path(srt, config)
    if not hold.is_file():
        return False
    try:
        marker = json.loads(hold.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False  # Existing generic ASR recovery owns malformed legacy markers.
    if not isinstance(marker, dict) or marker.get("operation") != _OPERATION:
        return False
    if Path(str(marker.get("srt_path"))).resolve() != srt.resolve():
        raise TranscriptionError("ASR postprocess pending marker targets a different SRT")
    original = str(marker.get("input_srt_sha256") or "")
    diagnostic_sha = str(marker.get("input_diagnostics_sha256") or "")
    _, source_backup, diagnostic_backup = _checkpoint_paths(srt, config, original, diagnostic_sha)
    if sha256_file(source_backup) != original or sha256_file(diagnostic_backup) != diagnostic_sha:
        raise TranscriptionError("ASR postprocess checkpoint integrity failed; hold retained")
    payload = json.loads(diagnostic_backup.read_text(encoding="utf-8"))
    if payload.get("srt_sha256") != original or Path(str(payload.get("srt_path"))).resolve() != srt.resolve():
        raise TranscriptionError("ASR postprocess checkpoint binding failed; hold retained")
    verified_copy_replace(source_backup, srt)
    verified_copy_replace(diagnostic_backup, asr_diagnostics_path(srt, config))
    hold.unlink()
    return True


def commit_asr_postprocess(srt, blocks, config, logger):
    """Validate new bytes before replacing an accepted SRT/diagnostic pair.

False is reserved for legacy non-M2 inputs without diagnostics. No acceptance
record is synthesized when the original ASR evidence is missing or untrusted.
"""
    from transcriber import (
        asr_diagnostics_path, asr_transcription_hold_path, asr_file_fingerprint,
        read_asr_diagnostics, validate_transcription_srt_quality,
        _canonical_sha256, _is_hallucination_text, TranscriptionError,
    )

    srt = Path(srt)
    diagnostic = asr_diagnostics_path(srt, config)
    payload = read_asr_diagnostics(srt, config)
    if not payload:
        if diagnostic.exists() or bool(getattr(config, "m2_server_canary_observer_enabled", False)):
            raise TranscriptionError("ASR postprocess requires existing hash-bound acceptance diagnostics")
        return False
    input_sha = sha256_file(srt)
    if payload.get("status") not in {"accepted", "accepted_after_selective_retry"} or payload.get("srt_sha256") != input_sha:
        raise TranscriptionError("ASR postprocess input diagnostics are not accepted or hash-matched")
    if Path(str(payload.get("srt_path") or "")).resolve() != srt.resolve():
        raise TranscriptionError("ASR postprocess input diagnostics target a different SRT")
    audio = Path(str(payload.get("audio_path") or ""))
    if not audio.is_file():
        raise TranscriptionError("ASR postprocess requires the validated audio for revalidation")
    audio_before = asr_file_fingerprint(audio)
    prior_audio = payload.get("audio_fingerprint")
    if isinstance(prior_audio, dict) and prior_audio.get("fingerprint") != audio_before["fingerprint"]:
        raise TranscriptionError("ASR postprocess audio identity changed")
    diagnostic_sha = sha256_file(diagnostic)
    directory, source_backup, diagnostic_backup = _checkpoint_paths(srt, config, input_sha, diagnostic_sha)
    directory.mkdir(parents=True, exist_ok=True)
    hold = asr_transcription_hold_path(srt, config)
    if hold.exists():
        raise TranscriptionError("ASR postprocess cannot replace a pending ASR commit")
    staging = directory / f"candidate-{time.time_ns()}.srt"
    try:
        write_srt(staging, blocks)
        validate_transcription_srt_quality(audio, staging, config, logger)
        if any(_is_hallucination_text(" ".join(block.text), config) for block in blocks):
            raise TranscriptionError("ASR postprocess retained hallucination text")
        if asr_file_fingerprint(audio) != audio_before or sha256_file(srt) != input_sha or sha256_file(diagnostic) != diagnostic_sha:
            raise TranscriptionError("ASR postprocess inputs changed during validation")
        for source, backup, expected in ((srt, source_backup, input_sha), (diagnostic, diagnostic_backup, diagnostic_sha)):
            if backup.exists():
                if sha256_file(backup) != expected:
                    raise TranscriptionError("ASR postprocess immutable checkpoint conflicts")
            else:
                verified_copy_replace(source, backup)
        atomic_write_text(hold, json.dumps({"operation": _OPERATION, "srt_path": str(srt),
            "input_srt_sha256": input_sha, "input_diagnostics_sha256": diagnostic_sha}, sort_keys=True))
        try:
            verified_copy_replace(staging, srt)
            payload["srt_sha256"] = sha256_file(srt)
            payload["segments"] = len(blocks)
            payload["postprocess"] = {"contract": _OPERATION, "input_srt_sha256": input_sha,
                "input_diagnostics_sha256": diagnostic_sha, "structural_revalidation": "PASS",
                "hallucination_text_check": "PASS", "validated_at": time.time()}
            if isinstance(payload.get("repair_basis"), dict):
                payload["cache_fingerprint"] = asr_file_fingerprint(srt, full_hash=True)
                payload["repair_basis"] = {**payload["repair_basis"], "cache_fingerprint": payload["cache_fingerprint"]["fingerprint"]}
                payload["repair_fingerprint"] = _canonical_sha256(payload["repair_basis"])
            atomic_write_text(diagnostic, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            if read_asr_diagnostics(srt, config).get("srt_sha256") != sha256_file(srt):
                raise TranscriptionError("ASR postprocess acceptance commit could not be verified")
            hold.unlink()
        except Exception:
            recover_asr_postprocess(srt, config)
            raise
    finally:
        staging.unlink(missing_ok=True)
    return True
