"""Narrow host attestation for a directly published local Docker model endpoint.

This module captures identity only. A binding is never a cancellation receipt.
No service restart, request release, Queue write, or Docker mutation occurs here.
"""
from datetime import datetime
import hashlib
import ipaddress
import json
import math
import re
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


class ModelProviderEvidenceError(RuntimeError):
    pass


PROVIDER_OBSERVATION_MAX_AGE = 90.0


def validate_provider_observation(baseline: Mapping[str, Any], observation: Mapping[str, Any], *,
                                  gate_baseline_version: str, now: float) -> None:
    """Fresh host inspection is necessary, not proof of request completion."""
    if (observation.get('contract') != 'm3-provider-observation-v1' or
            observation.get('gate_baseline_version') != gate_baseline_version):
        raise ModelProviderEvidenceError('provider_observation_baseline_mismatch')
    checked = observation.get('checked_at')
    if (type(now) not in (int, float) or not math.isfinite(now) or
            type(checked) not in (int, float) or not math.isfinite(checked) or
            checked <= 0 or not 0 <= now - checked <= PROVIDER_OBSERVATION_MAX_AGE):
        raise ModelProviderEvidenceError('provider_observation_expired_or_clock_invalid')
    if observation.get('status') != 'VERIFIED' or observation.get('model_provider') != dict(baseline):
        raise ModelProviderEvidenceError('provider_observation_identity_unproven')


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def direct_endpoint_descriptor(endpoint: str) -> dict[str, Any]:
    parsed = urlsplit(endpoint.strip().rstrip('/'))
    if (parsed.scheme != 'http' or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path != '/v1'):
        raise ModelProviderEvidenceError('provider_direct_endpoint_unproven')
    try:
        address = ipaddress.ip_address(parsed.hostname or '')
        port = parsed.port
    except ValueError as exc:
        raise ModelProviderEvidenceError('provider_direct_endpoint_unproven') from exc
    if address.version != 4 or address.is_loopback or address.is_unspecified or port is None:
        raise ModelProviderEvidenceError('provider_direct_endpoint_unproven')
    return {'host': str(address), 'port': port,
            'endpoint_sha256': hashlib.sha256(endpoint.strip().rstrip('/').encode()).hexdigest()}


def redacted_endpoint_descriptor(endpoint: str) -> dict[str, Any]:
    descriptor = direct_endpoint_descriptor(endpoint)
    return {'host_sha256': hashlib.sha256(descriptor['host'].encode()).hexdigest(),
            'port': descriptor['port'], 'endpoint_sha256': descriptor['endpoint_sha256']}


def capture_provider_binding(inspection: Mapping[str, Any], endpoint: Mapping[str, Any],
                             local_addresses: Sequence[str], *, observed_at: float) -> dict[str, Any]:
    """Caller supplies fresh Docker inspect + host address output, not user JSON.

    Supports only the direct host IPv4 published-port topology verified in M3.
    DNS, proxies, host networking, indirect routes and ambiguous port mappings
    remain unproved; they must not be guessed into this cancellation domain.
    """
    if type(observed_at) not in (int, float) or not math.isfinite(observed_at) or observed_at <= 0:
        raise ModelProviderEvidenceError('provider_clock_invalid')
    container = inspection.get('Id', '')
    image = inspection.get('Image', '')
    if not isinstance(container, str) or not re.fullmatch('[0-9a-f]{64}', container):
        raise ModelProviderEvidenceError('provider_container_id_invalid')
    if not isinstance(image, str) or not re.fullmatch('sha256:[0-9a-f]{64}', image):
        raise ModelProviderEvidenceError('provider_image_id_invalid')
    state = inspection.get('State', {})
    if state.get('Running') is not True or state.get('Paused') or state.get('Restarting') or state.get('Dead'):
        raise ModelProviderEvidenceError('provider_not_running_stably')
    try:
        started = datetime.fromisoformat(state['StartedAt'].replace('Z', '+00:00'))
        if started.tzinfo is None or not 0 < started.timestamp() <= observed_at:
            raise ValueError('invalid start time')
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ModelProviderEvidenceError('provider_generation_unproven') from exc
    host, port = endpoint.get('host'), endpoint.get('port')
    if host is None:
        matches = [address for address in local_addresses
                   if hashlib.sha256(address.encode()).hexdigest() == endpoint.get('host_sha256')]
        host = matches[0] if len(matches) == 1 else None
    if host not in local_addresses or type(port) is not int or not 1 <= port <= 65535:
        raise ModelProviderEvidenceError('provider_endpoint_not_on_attested_host')
    endpoint_hash = endpoint.get('endpoint_sha256', '')
    if not isinstance(endpoint_hash, str) or not re.fullmatch('[0-9a-f]{64}', endpoint_hash):
        raise ModelProviderEvidenceError('provider_endpoint_digest_invalid')
    bindings = inspection.get('NetworkSettings', {}).get('Ports', {}).get('11434/tcp') or []
    matches = [item for item in bindings if item.get('HostIp') in {host, '0.0.0.0'}
               and item.get('HostPort') == str(port)]
    if len(matches) != 1:
        raise ModelProviderEvidenceError('provider_published_port_unproven')
    payload = {'contract': 'm3-model-provider-binding-v1', 'container_id': container,
        'image_id': image, 'started_at': state['StartedAt'],
        'endpoint_sha256': endpoint_hash,
        'topology': 'direct-host-ipv4-docker-11434'}
    return {**payload, 'binding_sha256': _digest(payload)}


def validate_provider_binding(binding: Mapping[str, Any], *, endpoint: str) -> dict[str, Any]:
    payload = dict(binding)
    digest = payload.pop('binding_sha256', None)
    if payload.get('contract') != 'm3-model-provider-binding-v1' or digest != _digest(payload):
        raise ModelProviderEvidenceError('provider_binding_integrity_invalid')
    if set(payload) != {'contract', 'container_id', 'image_id', 'started_at',
                        'endpoint_sha256', 'topology'}:
        raise ModelProviderEvidenceError('provider_binding_shape_invalid')
    descriptor = direct_endpoint_descriptor(endpoint)
    # Reapply all structural/temporal checks to the sanitized descriptor.
    reconstructed = capture_provider_binding({'Id': payload['container_id'], 'Image': payload['image_id'],
        'State': {'Running': True, 'StartedAt': payload['started_at']},
        'NetworkSettings': {'Ports': {'11434/tcp': [{'HostIp': descriptor['host'],
                                                   'HostPort': str(descriptor['port'])}]}}},
        descriptor, [descriptor['host']], observed_at=time.time())
    if reconstructed != dict(binding):
        raise ModelProviderEvidenceError('provider_binding_endpoint_mismatch')
    return dict(binding)


def prove_provider_restart_after_sender_exit(request: Mapping[str, Any],
                                            sender_exit: Mapping[str, Any],
                                            current_binding: Mapping[str, Any], *,
                                            endpoint: str) -> dict[str, Any]:
    """Validate ordering only; caller must obtain fresh authoritative host evidence.

    This does not mutate receipts, authorize dispatch, or certify output quality.
    Replacement containers are intentionally unsupported: starting a new container
    does not prove the old one stopped. Historical unbound requests stay unknown.
    """
    original = request.get('provider_binding')
    if not isinstance(original, Mapping):
        raise ModelProviderEvidenceError('provider_original_binding_missing')
    old = validate_provider_binding(original, endpoint=endpoint)
    new = validate_provider_binding(current_binding, endpoint=endpoint)
    if (old['container_id'] != new['container_id'] or old['image_id'] != new['image_id']):
        raise ModelProviderEvidenceError('provider_same_container_restart_unproven')
    sender_id = request.get('sender_id')
    token = request.get('token')
    runtime = request.get('runtime_sha256')
    if (not isinstance(sender_id, str) or not re.fullmatch('[0-9a-f]{32}', sender_id)
            or not isinstance(runtime, str) or not re.fullmatch('[0-9a-f]{64}', runtime)
            or not isinstance(token, str) or not token
            or sender_exit.get('sender_id') != sender_id or sender_exit.get('token') != token
            or sender_exit.get('runtime_sha256') != runtime
            or sender_exit.get('provider_binding') != original
            or sender_exit.get('endpoint') != request.get('endpoint')
            or request.get('endpoint') != old['endpoint_sha256']):
        raise ModelProviderEvidenceError('provider_sender_exit_binding_mismatch')
    reaped = sender_exit.get('timeout_reaped')
    code = sender_exit.get('returncode')
    if type(reaped) is not bool or (reaped and code is not None) or (not reaped and type(code) is not int):
        raise ModelProviderEvidenceError('provider_sender_exit_unproven')
    created = request.get('created_at')
    exited = sender_exit.get('sender_exited_no_later_than')
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (created, exited)):
        raise ModelProviderEvidenceError('provider_restart_order_unproven')
    old_start = datetime.fromisoformat(old['started_at'].replace('Z', '+00:00')).timestamp()
    new_start = datetime.fromisoformat(new['started_at'].replace('Z', '+00:00')).timestamp()
    if not 0 < old_start <= created <= exited < new_start:
        raise ModelProviderEvidenceError('provider_restart_order_unproven')
    return {'contract': 'm3-provider-restart-order-v1', 'token': token,
            'sender_id': sender_id, 'sender_exited_no_later_than': exited,
            'previous_binding_sha256': old['binding_sha256'],
            'current_binding_sha256': new['binding_sha256'],
            'provider_started_at': new['started_at'],
            'reason_code': 'same_provider_restarted_after_sender_exit'}
