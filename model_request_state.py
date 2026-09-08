"""M3 request receipts within the existing Pipeline Job Store, not a queue.

Each operation owns a short SQLite transaction. Never keep this transaction
open while doing network/model work. No PID/age-based ownership reclamation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from typing import Any, Iterator, Mapping


DEFINITIVE_HTTP_REJECTIONS = frozenset({400, 401, 403, 404, 405, 406, 413, 415, 422, 429})


class ModelRequestStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelRequestContext:
    """Immutable attempt binding safe to capture in an executor callback.

    Each operation opens its own connection. Never share the Worker's stage
    transaction/connection with a model thread, or hold SQL locks over network IO.
    The existing Pipeline store must already exist and contain the active stage.
    """
    database: Path
    stage_attempt_id: str
    runtime_sha256: str
    attempt_limit: int
    resource_scope: str = 'unverified'
    sender_id: str = field(default_factory=lambda: os.environ.get('ANIME_MODEL_SENDER_ID', ''))

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(Path(self.database).resolve().as_uri() + '?mode=rw',
                               uri=True, timeout=60)
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def reserve(self, *, endpoint: str, request: Mapping[str, Any], model: str,
                operation_kind: str = 'INFERENCE') -> dict[str, Any]:
        conn = self._connection()
        try:
            return reserve_model_request(conn, stage_attempt_id=self.stage_attempt_id,
                operation_id=uuid.uuid4().hex,
                endpoint=hashlib.sha256(endpoint.encode('utf-8')).hexdigest(),
                request_sha256=hashlib.sha256(_json(request).encode('utf-8')).hexdigest(),
                model=model, runtime_sha256=self.runtime_sha256, attempt_limit=self.attempt_limit,
                operation_kind=operation_kind, resource_scope=self.resource_scope,
                sender_id=self.sender_id)
        finally:
            conn.close()

    def record(self, token: str, outcome: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
        conn = self._connection()
        try:
            return record_model_request_result(conn, token=token,
                runtime_sha256=self.runtime_sha256, outcome=outcome, evidence=evidence)
        finally:
            conn.close()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def pending_model_resource_request(database: Path, *, endpoint: str | None = None) -> dict[str, Any] | None:
    """Bounded read-only authority check, including old endpoint revisions.

    Without an endpoint, independent remote requests do not reserve local GPU.
    Missing historical scope is unverified, not proof of resource independence.
    The pending-owner partial index covers the state predicate; no Queue scan.
    """
    conn = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        predicate = "COALESCE(json_extract(model_json,'$.m3_request.resource_scope'),'unverified') != 'independent'"
        parameters = ()
        if endpoint is not None:
            predicate = "json_extract(model_json,'$.m3_request.endpoint')=?"
            parameters = (hashlib.sha256(endpoint.strip().rstrip('/').encode('utf-8')).hexdigest(),)
        row = conn.execute("""SELECT json_extract(model_json,'$.m3_request')
            FROM pipeline_stage_attempts INDEXED BY idx_m3_owned_endpoint
            WHERE json_extract(model_json,'$.m3_request.state') IN ('RESERVED','UNKNOWN')
            AND """ + predicate + ' LIMIT 1', parameters).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def _digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ModelRequestStateError("model_request_invalid_digest")
    return value


def _timestamp() -> float:
    now = time.time()
    if not math.isfinite(now) or now <= 0:
        raise ModelRequestStateError("model_request_invalid_clock")
    return now


def ensure_model_request_schema(conn: sqlite3.Connection) -> None:
    """Additive migration under the caller's existing schema transaction."""
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_m3_owned_endpoint
            ON pipeline_stage_attempts(json_extract(model_json,'$.m3_request.endpoint'))
            WHERE json_extract(model_json,'$.m3_request.state') IN ('RESERVED','UNKNOWN')""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_m3_request_budget
            ON pipeline_stage_events(job_id,event_type,json_extract(payload_json,'$.budget_key'))""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_m3_request_operation
            ON pipeline_stage_events(json_extract(payload_json,'$.operation_id'))
            WHERE event_type='MODEL_REQUEST_RESERVED'""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_m3_request_token
            ON pipeline_stage_events(event_type,json_extract(payload_json,'$.token'))""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS m3_preserve_owned_request
            BEFORE DELETE ON pipeline_stage_attempts
            WHEN json_extract(OLD.model_json,'$.m3_request.state') IN ('RESERVED','UNKNOWN')
            BEGIN SELECT RAISE(ABORT,'model_request_ownership_unresolved'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS m3_preserve_request_metadata
            BEFORE UPDATE OF model_json ON pipeline_stage_attempts
            WHEN json_extract(OLD.model_json,'$.m3_request.state') IN ('RESERVED','UNKNOWN')
            AND json_extract(OLD.model_json,'$.m3_request') IS NOT json_extract(NEW.model_json,'$.m3_request')
            AND (
                json_extract(OLD.model_json,'$.m3_request.token') IS NOT json_extract(NEW.model_json,'$.m3_request.token')
                OR NOT EXISTS (
                    SELECT 1 FROM pipeline_stage_events e
                    WHERE e.stage_attempt_id=OLD.stage_attempt_id
                    AND e.event_type IN ('MODEL_REQUEST_UNKNOWN','MODEL_REQUEST_SETTLED')
                    AND e.payload_json=json_extract(NEW.model_json,'$.m3_request')
                )
            )
            BEGIN SELECT RAISE(ABORT,'model_request_ownership_unresolved'); END""")
    for action in ('DELETE', 'UPDATE'):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS m3_request_receipt_no_{action.lower()}
                BEFORE {action} ON pipeline_stage_events
                WHEN OLD.event_type IN ('MODEL_REQUEST_RESERVED','MODEL_REQUEST_UNKNOWN','MODEL_REQUEST_SETTLED')
                BEGIN SELECT RAISE(ABORT,'model_request_receipt_immutable'); END""")
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS m3_sender_receipt_no_{action.lower()}
            BEFORE {action} ON pipeline_stage_events
            WHEN OLD.event_type='MODEL_REQUEST_SENDER_EXITED'
            BEGIN SELECT RAISE(ABORT,'model_request_receipt_immutable'); END""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_m3_sender_exit_token
        ON pipeline_stage_events(json_extract(payload_json,'$.token'))
        WHERE event_type='MODEL_REQUEST_SENDER_EXITED'""")


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        raise ModelRequestStateError("model_request_requires_own_short_transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure_model_request_schema(conn)
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _event(conn: sqlite3.Connection, request: Mapping[str, Any], kind: str) -> None:
    row = conn.execute("SELECT stage,status FROM pipeline_stage_attempts WHERE stage_attempt_id=?",
                       (request['stage_attempt_id'],)).fetchone()
    if row is None:
        raise ModelRequestStateError("model_request_stage_missing")
    conn.execute("""INSERT INTO pipeline_stage_events
        (job_id,stage_attempt_id,event_type,stage,status,reason_code,evidence_json,confidence,payload_json,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", (
        request['job_id'], request['stage_attempt_id'], kind, row[0], row[1],
        kind.lower(), _json({'contract':'m3-model-request-v1','token':request['token']}),
        1.0, _json(request), _timestamp()))


def record_model_sender_exit(database: Path, sender_id: str, *,
                             returncode: int | None, timeout_reaped: bool = False) -> int:
    """Supervisor-only: called after run() returned or killed AND waited.

    This proves only that this sender cannot issue a later request. It is NOT
    proof that remote work stopped, and never changes ownership or retry budget.
    """
    if not isinstance(sender_id, str) or not re.fullmatch('[0-9a-f]{32}', sender_id):
        raise ModelRequestStateError('model_request_invalid_sender_id')
    if (type(timeout_reaped) is not bool or
            (timeout_reaped and returncode is not None) or
            (not timeout_reaped and type(returncode) is not int)):
        raise ModelRequestStateError('model_request_sender_exit_unproven')
    observed = _timestamp()
    conn = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=rw', uri=True, timeout=10)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        with _transaction(conn):
            rows = conn.execute("""SELECT json_extract(model_json,'$.m3_request')
                FROM pipeline_stage_attempts INDEXED BY idx_m3_owned_endpoint
                WHERE json_extract(model_json,'$.m3_request.state') IN ('RESERVED','UNKNOWN')
                AND json_extract(model_json,'$.m3_request.sender_id')=?""", (sender_id,)).fetchall()
            written = 0
            for row in rows:
                request = json.loads(row[0])
                previous = conn.execute("""SELECT payload_json FROM pipeline_stage_events
                    WHERE event_type='MODEL_REQUEST_SENDER_EXITED'
                    AND json_extract(payload_json,'$.token')=?""", (request['token'],)).fetchone()
                if previous:
                    saved = json.loads(previous[0])
                    if saved['returncode'] != returncode or saved['timeout_reaped'] != timeout_reaped:
                        raise ModelRequestStateError('model_request_sender_exit_conflict')
                    continue
                if observed < request['created_at']:
                    raise ModelRequestStateError('model_request_sender_exit_clock_invalid')
                _event(conn, {**request, 'sender_exited_no_later_than': observed,
                    'returncode': returncode, 'timeout_reaped': timeout_reaped}, 'MODEL_REQUEST_SENDER_EXITED')
                written += 1
            return written
    finally:
        conn.close()


def _write_current(conn: sqlite3.Connection, request: Mapping[str, Any]) -> None:
    row = conn.execute('SELECT model_json FROM pipeline_stage_attempts WHERE stage_attempt_id=?',
                       (request['stage_attempt_id'],)).fetchone()
    if row is None:
        raise ModelRequestStateError("model_request_stage_missing")
    model = json.loads(row[0])
    if not isinstance(model, dict):
        raise ModelRequestStateError("model_request_invalid_stage_model")
    model['m3_request'] = dict(request)
    conn.execute('UPDATE pipeline_stage_attempts SET model_json=? WHERE stage_attempt_id=?',
                 (_json(model), request['stage_attempt_id']))


def reserve_model_request(conn: sqlite3.Connection, *, stage_attempt_id: str,
                          operation_id: str, endpoint: str, request_sha256: str,
                          model: str, runtime_sha256: str, attempt_limit: int,
                          operation_kind: str = 'INFERENCE',
                          resource_scope: str = 'unverified', sender_id: str = '') -> dict[str, Any]:
    """Commit before dispatch. A replay is evidence only, never a resend permit."""
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", operation_id):
        raise ModelRequestStateError("model_request_invalid_operation")
    for value in (endpoint, request_sha256, runtime_sha256):
        _digest(value)
    if not isinstance(model, str) or not model.strip() or len(model) > 512:
        raise ModelRequestStateError("model_request_invalid_model")
    if type(attempt_limit) is not int or not 1 <= attempt_limit <= 128:
        raise ModelRequestStateError("model_request_invalid_budget")
    if operation_kind not in {'INFERENCE', 'UNLOAD'}:
        raise ModelRequestStateError('model_request_invalid_operation_kind')
    if resource_scope not in {'unverified', 'shared_gpu', 'independent'}:
        raise ModelRequestStateError('model_request_invalid_resource_scope')
    if not isinstance(sender_id, str) or (sender_id and not re.fullmatch('[0-9a-f]{32}', sender_id)):
        raise ModelRequestStateError('model_request_invalid_sender_id')
    basis = dict(stage_attempt_id=stage_attempt_id, operation_id=operation_id,
                 endpoint=endpoint, request_sha256=request_sha256, model=model,
                 runtime_sha256=runtime_sha256, attempt_limit=attempt_limit,
                 operation_kind=operation_kind, resource_scope=resource_scope, sender_id=sender_id)
    with _transaction(conn):
        replay = conn.execute("""SELECT payload_json FROM pipeline_stage_events
            WHERE event_type='MODEL_REQUEST_RESERVED' AND json_extract(payload_json,'$.operation_id')=?""",
            (operation_id,)).fetchone()
        if replay:
            original = json.loads(replay[0])
            if any(original.get(key) != value for key,value in basis.items()):
                raise ModelRequestStateError("model_request_operation_conflict")
            return {**original, 'dispatch':False}
        stage = conn.execute("""SELECT a.job_id,a.status,j.active_stage_attempt_id
            FROM pipeline_stage_attempts a JOIN pipeline_jobs j ON j.job_id=a.job_id
            WHERE a.stage_attempt_id=?""", (stage_attempt_id,)).fetchone()
        if not stage or (operation_kind == 'INFERENCE' and
                         (stage[1] != 'RUNNING' or stage[2] != stage_attempt_id)):
            raise ModelRequestStateError("model_request_stage_not_active")
        owned = conn.execute("""SELECT stage_attempt_id FROM pipeline_stage_attempts
            WHERE json_extract(model_json,'$.m3_request.state') IN ('RESERVED','UNKNOWN')
            AND (json_extract(model_json,'$.m3_request.endpoint')=? OR stage_attempt_id=?) LIMIT 1""",
            (endpoint,stage_attempt_id)).fetchone()
        if owned:
            raise ModelRequestStateError("model_request_ownership_unresolved")
        budget_key = hashlib.sha256(_json([endpoint,request_sha256,model]).encode()).hexdigest()
        spent, previous_limit = conn.execute("""SELECT COUNT(*),
            MIN(json_extract(payload_json,'$.attempt_limit')) FROM pipeline_stage_events
            WHERE job_id=? AND event_type='MODEL_REQUEST_RESERVED'
            AND json_extract(payload_json,'$.budget_key')=?""", (stage[0],budget_key)).fetchone()
        # A recreated adapter cannot replenish a request's persisted budget.
        # Policy changes do not erase earlier attempts or expand this obligation.
        effective_limit = min(attempt_limit, int(previous_limit)) if previous_limit is not None else attempt_limit
        if spent >= effective_limit:
            raise ModelRequestStateError("model_request_budget_exhausted")
        request = {**basis, 'contract':'m3-model-request-v1', 'job_id':stage[0],
                   'token':uuid.uuid4().hex, 'budget_key':budget_key,
                   'attempt_number':spent+1, 'effective_limit':effective_limit,
                   'state':'RESERVED', 'created_at':_timestamp()}
        _write_current(conn,request)
        _event(conn,request,'MODEL_REQUEST_RESERVED')
        return {**request,'dispatch':True}


def record_model_request_result(conn: sqlite3.Connection, *, token: str,
                                runtime_sha256: str, outcome: str,
                                evidence: Mapping[str, Any]) -> dict[str, Any]:
    """UNKNOWN stays owned even across restart; a response is not a QC PASS."""
    _digest(runtime_sha256)
    proof = dict(evidence)
    if outcome == 'RESPONSE':
        _digest(proof.get('response_sha256'))
    elif outcome == 'HTTP_ERROR':
        if type(proof.get('status_code')) is not int or proof['status_code'] not in DEFINITIVE_HTTP_REJECTIONS:
            raise ModelRequestStateError("model_request_invalid_http_evidence")
    elif outcome == 'NOT_DISPATCHED':
        if proof.get('cancelled_before_start') is not True:
            raise ModelRequestStateError("model_request_cancellation_unproven")
    elif outcome == 'UNKNOWN':
        if proof.get('reason_code') not in {'transport_error','hard_timeout','process_interrupted',
                                          'provider_completion_unproven'}:
            raise ModelRequestStateError("model_request_unknown_reason_invalid")
    else:
        raise ModelRequestStateError("model_request_invalid_outcome")
    _json(proof)
    with _transaction(conn):
        reserved = conn.execute("""SELECT payload_json FROM pipeline_stage_events
            WHERE event_type='MODEL_REQUEST_RESERVED' AND json_extract(payload_json,'$.token')=?""",
            (token,)).fetchone()
        if not reserved:
            raise ModelRequestStateError("model_request_token_unknown")
        original = json.loads(reserved[0])
        if original['runtime_sha256'] != runtime_sha256:
            raise ModelRequestStateError("model_request_owner_runtime_mismatch")
        settled = conn.execute("""SELECT payload_json FROM pipeline_stage_events
            WHERE event_type='MODEL_REQUEST_SETTLED' AND json_extract(payload_json,'$.token')=?""",
            (token,)).fetchone()
        if settled:
            previous = json.loads(settled[0])
            if previous['outcome'] != outcome or previous['result_evidence'] != proof:
                raise ModelRequestStateError("model_request_result_conflict")
            return {**previous,'replay':True}
        row = conn.execute('SELECT model_json FROM pipeline_stage_attempts WHERE stage_attempt_id=?',
                           (original['stage_attempt_id'],)).fetchone()
        current = json.loads(row[0]).get('m3_request',{}) if row else {}
        if current.get('token') != token or current.get('state') not in {'RESERVED','UNKNOWN'}:
            raise ModelRequestStateError("model_request_current_owner_mismatch")
        if current.get('state') == 'UNKNOWN' and outcome == 'NOT_DISPATCHED':
            raise ModelRequestStateError("model_request_cannot_relabel_unknown_as_unsent")
        if (current.get('state') == 'UNKNOWN' and outcome == 'UNKNOWN'
                and current.get('result_evidence') == proof):
            return {**current, 'replay':True}
        result = {**original, 'state':'UNKNOWN' if outcome=='UNKNOWN' else 'SETTLED',
                  'outcome':outcome,'result_evidence':proof,'observed_at':_timestamp()}
        _event(conn,result,'MODEL_REQUEST_UNKNOWN' if outcome=='UNKNOWN' else 'MODEL_REQUEST_SETTLED')
        _write_current(conn,result)
        return {**result,'replay':False}
