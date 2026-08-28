"""Bounded lifecycle control for Worker-managed Ollama translation models."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


def unload_managed_translation_models(
    config: Any,
    logger: logging.Logger,
    *,
    model_names: Iterable[str] | None = None,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[str, ...]:
    """Unload only configured translation models that are already resident.

    Querying ``/api/ps`` first prevents an unload request from loading a model
    that is not resident.  Failures are best-effort: resource admission remains
    fail-closed and will defer the next ASR rather than over-committing VRAM.
    """

    if not bool(getattr(config, "translator_ollama_auto_unload_enabled", False)):
        return ()

    requested = _requested_models(config, model_names)
    if not requested:
        return ()

    timeout = max(
        0.1,
        float(getattr(config, "translator_ollama_unload_timeout_seconds", 15.0) or 15.0),
    )
    active_opener = opener or urlopen
    ps_url, generate_url = _ollama_urls(str(getattr(config, "translator_base_url", "")))
    try:
        running = _running_models(ps_url, timeout, active_opener)
    except Exception as exc:  # noqa: BLE001 - cleanup must not hide the job result.
        logger.warning("Unable to inspect managed Ollama models before unload: %s", exc)
        return ()

    requested_identities = {_model_identity(name) for name in requested}
    targets = tuple(name for name in running if _model_identity(name) in requested_identities)
    if not targets:
        return ()

    accepted: list[str] = []
    for model in targets:
        try:
            _post_json(
                generate_url,
                {"model": model, "keep_alive": 0, "stream": False},
                timeout,
                active_opener,
            )
            accepted.append(model)
        except Exception as exc:  # noqa: BLE001 - another target may still release successfully.
            logger.warning("Unable to request Ollama model unload model=%s error=%s", model, exc)

    if not accepted:
        return ()

    deadline = monotonic() + timeout
    remaining = {_model_identity(name) for name in accepted}
    while remaining:
        try:
            resident = {
                _model_identity(name)
                for name in _running_models(ps_url, timeout, active_opener)
            }
        except Exception as exc:  # noqa: BLE001 - admission will independently verify VRAM.
            logger.warning("Unable to confirm Ollama model unload: %s", exc)
            break
        remaining.intersection_update(resident)
        if not remaining or monotonic() >= deadline:
            break
        sleep(min(0.25, max(0.0, deadline - monotonic())))

    released = tuple(name for name in accepted if _model_identity(name) not in remaining)
    if released:
        logger.info("Released managed Ollama translation model(s): %s", ", ".join(released))
    if remaining:
        logger.warning(
            "Managed Ollama model unload did not finish before timeout: %s",
            ", ".join(name for name in accepted if _model_identity(name) in remaining),
        )
    return released


def _requested_models(config: Any, model_names: Iterable[str] | None) -> tuple[str, ...]:
    configured = (
        list(model_names)
        if model_names is not None
        else [
            str(getattr(config, "translator_model", "")),
            *list(getattr(config, "translator_fallback_models", ()) or ()),
        ]
    )
    seen: set[str] = set()
    result: list[str] = []
    for value in configured:
        name = str(value).strip()
        identity = _model_identity(name)
        if not name or identity in seen:
            continue
        seen.add(identity)
        result.append(name)
    return tuple(result)


def _model_identity(name: str) -> str:
    normalized = str(name).strip().casefold()
    leaf = normalized.rsplit("/", 1)[-1]
    if normalized and ":" not in leaf:
        normalized += ":latest"
    return normalized


def _ollama_urls(base_url: str) -> tuple[str, str]:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("translator_base_url is not a valid Ollama HTTP endpoint")
    path = parsed.path.rstrip("/")
    if path.casefold().endswith("/v1"):
        path = path[:-3].rstrip("/")
    root = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return f"{root}/api/ps", f"{root}/api/generate"


def _running_models(url: str, timeout: float, opener: Callable[..., Any]) -> tuple[str, ...]:
    payload = _request_json(Request(url, method="GET"), timeout, opener)
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ValueError("Ollama /api/ps response has no models list")
    result: list[str] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "").strip()
        if name:
            result.append(name)
    return tuple(result)


def _post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    return _request_json(request, timeout, opener)


def _request_json(request: Request, timeout: float, opener: Callable[..., Any]) -> dict[str, Any]:
    with opener(request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return {}
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Ollama response is not a JSON object")
    return payload


__all__ = ["unload_managed_translation_models"]
