# M3 — configured model recovery

M3 is authorized and active. M2 production acceptance remains incomplete;
its frozen cohort, held sources and UNPROVEN preservation claims are unchanged.

## Scope

## Cross-process observation writer serialization (candidate)

Provider-bound Gate initialization and evidence refresh now share one nonblocking
kernel-owned writer lock in the existing work directory. Windows uses byte-range
locking; Linux uses `flock`. A busy writer fails closed with a bounded reason code.
The lock inode is retained, never removed or reclaimed based on age/PID. The OS
releases ownership when its process exits, including fixture termination.

Real spawned-process tests verify writer exclusion, both public entry points
refusing before mutation, and reacquisition after the disposable owner exits.
The initial Windows fixture hung because cleanup reused synchronization with a
terminated child; only the identified local test process was stopped, and the
fixture now uses a child-local wait object. Corrected Windows related suite: 31
PASS; server Linux related suite: 66 PASS (`provider-writer-lock-server.log`).
Final entry-point assertions: `provider-writer-lock-final-server.log`. No
Production process, lock or runtime was changed. Autonomous scheduling and the
post-response publication confirmation remain required.

## Bounded host observation publisher (candidate, not deployed)

`m2_guardrail_runtime.py provider-refresh --model-provider-container NAME` now
performs one context read, two Docker provider inspections and one local evidence
update. It does not scan Queue, arm a Gate, resume admission or settle requests.
The local publisher verifies the exact Gate baseline, effective endpoint and
inspection timestamp, then rechecks the runtime context before atomic publication.
It retains continuity loss as UNPROVEN; a later successful inspection cannot
erase a prior gap. Re-arming that same baseline also refuses to overwrite the gap.

Composed command-runner and isolated file/runtime tests cover the bounded command
sequence, wrong-Gate rejection without file mutation, gap persistence and re-arm
refusal. Server isolated related suite: 54 PASS, `provider-refresh-entry-server.log`
under `/logs/m3-baseline-20260908T070237Z/`.
The periodic host lifecycle/single-writer deployment handoff and the
post-response confirmation barrier remain open. This command has not been run
against Production and does not by itself establish autonomous observation.

## Provider observation admission contract (candidate, not deployed)

For a provider-bound baseline, runtime status now requires a host observation
bound to the same Gate baseline and exact provider identity. Missing evidence,
non-VERIFIED status, a different identity/baseline, future/non-finite timestamps
or an observation older than 90 seconds fails closed as DEGRADED. Existing
admission and terminal-observation callers consume that status. Legacy baselines
without provider binding are not silently retrofitted.

Host arm supplies its inspection timestamp, and initialization validates it and
atomically seeds `work/m3-provider-observation.json`. Re-attesting the same
baseline does not change cohort membership or Gate start. The timestamp is not
part of the frozen baseline identity. Tests cover fresh acceptance, missing,
expired, future, mismatched provider and mismatched Gate observations.
Server isolated related suite: 76 PASS (70.061 seconds),
`/logs/m3-baseline-20260908T070237Z/provider-observation-seed-server.log`.

This is the consumer/initial-seed contract only. The autonomous host publisher
and post-response verification barrier are not implemented yet. A 90-second
freshness window alone is NOT proof that no provider change occurred during a
request; it must not be presented as continuous generation validation or used
to accept mixed-version output. Do not deploy this increment before those
producer and publication/terminal boundaries are complete.

## Real isolated provider termination evidence (2026-09-08 09:16 UTC)

`m3_provider_restart_test.sh` and `m3_provider_restart_probe.py` exercised a real
HTTP request across disposable containers using the deployed Worker image, no
GPU, a read-only tracked-code snapshot and a dedicated fixture volume. No
Production media/work volume was mounted. The sender timed out while the fixture
provider was still handling the request, persisted UNKNOWN, exited, and was
confirmed exited by Docker before sender-exit evidence was recorded. Only that
label-verified fixture provider was restarted. Fresh Docker identity/port evidence
then allowed controlled ownership settlement, with idempotent replay, unchanged
source/checkpoint, unchanged consumed retry budget and no COMPLETED job.

PASS evidence: `/logs/m3-baseline-20260908T070237Z/provider-termination-O2PvZp/`:
`full.log`, `bind-old-inspect.json`, `bind-new-inspect.json`,
`container-evidence.log`, `cleanup.log`, and `fixture/result.json` with its bound
receipt/source/checkpoint evidence. Both dedicated containers were removed after
label checks; fixture files and logs remain. Provider container identity was
`57a2a2d8b2496164443c1ace7d188f644cd8469ee51356d315ec49a1a8fc5265`,
with start generations `2026-09-08T09:15:52.64574374Z` and
`2026-09-08T09:16:00.67946566Z`.

The earlier `provider-termination-rMcthi/` attempt is retained as a failed test:
after restart its original published-port binding could not be proven and no
settlement was allowed. The fixture switched from Docker-assigned to an explicit
temporary high port and retained before/after inspection. No Production provider
restart or protection override was used to make the test pass.

This is real transport/container lifecycle evidence for the state primitive, not
actual model inference, an end-to-end Production recovery, or formal subtitle
delivery. Ongoing provider drift enforcement and safe Production deployment are
still required; M2 acceptance remains incomplete.

## Current controlled entry increment (candidate, not deployed)

The existing host `recover` command accepts one optional `--model-request-token`
only with an explicit model-provider container and planned/authorized
reconciliation. It captures the provider before recovery, rechecks before the
single-request resolution, and rechecks again through the existing arm flow.
Resolution occurs after controlled breaker recovery and before arm/resume, not
through a new queue or a general-purpose clear command.

The internal `resolve-model-local` command requires a DISARMED pending-handoff
state, the controlled-recovery pause owner, durable admission pause, the exact
hash-bound recovery log inside the configured log directory and matching current
code/configuration/container-instance identity. It preserves the pause, old Gate,
recovery log and source/output data. Interrupted recovery selects the original
recovery log, not the subsequent resume log. Persisted sender proof and provider
restart ordering are still required; unbound historical requests remain held.

Server isolated related suite: 152 PASS (`provider-controlled-entry-server.log`).
Final additional runtime/fence checks: 65 PASS in
`provider-controlled-entry-final-server.log`. These tests exercise actual isolated
SQLite transactions and local files with fixture host identity; they are not a
live Production recovery. Host-command composition is now covered by the existing
Docker-command runner fixture: exact recover/resolve/arm/resume order, original
log selection on resume, invalid resolution rejection, provider drift before
resolution and provider drift before arm. Every rejected case omits resume.
Server suite: 30 PASS, `provider-host-composition-server.log`; overlapping suites
are not additive. Real provider termination fixture, ongoing provider drift
enforcement and safe deployment remain open.
Earlier paragraphs saying no controlled entry exists describe prior increments.

## Provider identity binding (candidate, not deployed)

The host arm/recover path now optionally accepts `--model-provider-container`.
It binds the effective Worker endpoint to an explicitly published, local Docker
Ollama instance using container ID, image ID and start generation. Unsupported
or indirect topology fails closed; two inspections must agree. Recovery captures
the identity before mutation and rechecks it when arming. An unchanged binding
does not recreate the frozen Gate merely because observation time changed.

The Worker copies this frozen binding into immutable request context and durable
reservation evidence. It does not backfill historical requests. Binding is
point-in-time identity evidence, not a cancellation certificate or proof that the
provider stayed unchanged after capture. Live drift enforcement and controlled
post-crash UNKNOWN resolution remain open; no ownership hold is cleared here.

Read-only server capture: `provider-binding-readonly.json`, binding SHA-256
`b91c298cd5186f24dbf03a45db19ad64f46f297ea8ce5a6d19d9b64d41c45415`.
Related server tests: 258 PASS (`provider-binding-integration-server.log`);
after final recovery ordering protection: 77 PASS
(`provider-handoff-final-server.log`). Suites overlap; counts are not additive.
All evidence is under `/logs/m3-baseline-20260908T070237Z/`. No production
restart, deployment, arm, recovery or Gate mutation occurred for this increment.

### Existing scope

State-level controlled resolution now uses the same Pipeline transaction and
immutable request events. `resolve_model_request_after_provider_restart` loads
sender-exit evidence from the database (not caller JSON), validates the exact
request/runtime/provider ordering, and links a recovery-record digest. It records
`PROVIDER_TERMINATED` in a SETTLED ownership receipt, not a successful inference or
COMPLETED job. Original UNKNOWN events and consumed route budget remain intact.
The ordinary result API rejects this special outcome. Transaction interruption
rolls back both settlement and ownership; restart/replay is idempotent and an old
replay cannot release a new owner's request.

This is an internal state primitive, not yet a production recovery command. Its
caller still must fence admission, capture trusted fresh host evidence, persist
the linked record and enforce Gate/runtime transition policy. No such production
resolution was invoked. Server image isolated suite: 74 PASS in
`provider-resolution-state-server.log`; final history-retention assertion checked
in `provider-resolution-final-server.log` under the same evidence log root.

The next candidate adds `prove_provider_restart_after_sender_exit`, a pure
ordering validator, not an ownership-release entry point. It requires the exact
request token/sender/runtime/endpoint and original provider binding, supervised
sender-exit evidence, and the same container/image with a start generation
strictly after sender exit. A replacement container does not prove termination
of the original; unbound history, unchanged generations and invalid clocks fail.
The caller must still obtain fresh authoritative host evidence. This helper is
not yet wired to controlled recovery or live drift enforcement and authorizes no
dispatch, request settlement or QC result by itself. Local related suite: 48 PASS,
`/logs/m3-baseline-20260908T070237Z/provider-restart-order-local.log` (Windows
execution with log on the server share, not server-runtime verification).

Build on the existing ASR/translation entry points, model configuration,
resource admission and persistent stage/checkpoint stores. Do not create a
parallel queue or lower output QC to make a fallback appear successful.
Model discovery is evidence of availability, not authorization to use unrelated
models. Model route attempts and their failure/recovery reasons must be bounded
and attributable to the actual processing runtime.

## Initial verified change

The existing translator resolver substituted any sole advertised model for a
missing configured model. It could also collapse primary/fallback into just the
advertised fallback. Two new regressions reproduced both failures on the server
in a read-only candidate mount, without production data or network access.

The implicit sole-model substitution is removed. Configured routes remain in
order when discovery lacks the primary; the existing request failure path then
advances only through that configured chain. Existing unique alias matching is
retained for compatibility and still needs an explicit M3 authorization contract.
This first change does not claim the entire model adapter design is finished.

- Baseline: 118 tests PASS.
- New failing regressions: 2 expected failures before the fix.
- After fix: 121 resource admission/runtime/Worker integration, translator and
  translation checkpoint tests PASS, including composed discovery-to-request
  rejection of the unapproved model and bounded configured-chain exhaustion.
- Server logs: `/logs/m3-baseline-20260908T070237Z/` (`tests.log`,
  `model-route-before.log`, `model-route-after.log`).
- Production runtime/configuration not changed by these isolated tests.

## Request ownership increment (candidate, not deployed)

Two real-thread/event regressions reproduced overlapping calls after a hard
timeout: immediate fallback in the same Translator, and a new Translator instance
calling the same endpoint while the first request was still executing.

The candidate retains endpoint ownership until the actual Future settles, not
just until the caller's timeout expires. Ownership is shared across Translator
instances in the **same process**, acquired atomically, and released by Future
identity so a delayed callback cannot release a newer request. An explicit
`TranslationRequestInFlightError` prevents immediate model fallback and batch
split/context retry. Model unload also participates in endpoint ownership and
does not unload a model under an outstanding request.

255 related tests PASS, including actual blocked executor threads (no network),
cross-instance refusal, completion followed by successful next request, no
split/context bypass, and request/unload mutual exclusion. Fixture requests are
bounded and released. Logs: `request-ownership-before.log` (two reproduced failures)
and `request-ownership-release.log` under `/logs/m3-baseline-20260908T070237Z/`.

This does **not** establish cross-process exclusion, crash/restart ownership,
or remote server cancellation after a socket closes. Those remain required before
M3 deployment/acceptance; the process-local registry is not a durable replacement
for the existing job/checkpoint/recovery stores.

## Remaining engineering and acceptance

### Sender quiescence and semantic unload completion (candidate)

Review confirms the existing host runtime attestation binds Worker/WebUI, not
the model provider generation. A provider restart timestamp after reservation
alone would be insufficient: a still-live sender could dispatch after that
restart. Controlled resolution must also prove the sender can issue no later
request. No provider reset or UNKNOWN-clear operation was performed.

The existing `_process_video_subprocess` supervisor now supplies a unique
`ANIME_MODEL_SENDER_ID`, captured in each request. Only after `subprocess.run`
returns, or its timeout kill-and-wait completes, the supervisor appends immutable
`MODEL_REQUEST_SENDER_EXITED` events to the existing stage event store. A launch
OSError does not assert exit. Events retain a time upper bound, return code or
reaped-timeout status, and request token. Replays do not duplicate evidence;
conflicting exit claims fail closed. This evidence never settles remote requests
or replenishes their budgets. Missing evidence keeps the original hold.

The actual process-loss HTTP test now records this exit evidence after join and
still proves no duplicate send. A separate real `subprocess.run` timeout test
exercises the production supervision wrapper using only an isolated child.
Server logs: `sender-exit-evidence-server.log` (214 related tests PASS), and
`sender-real-reap-server.log` (5 final targeted tests PASS; overlapping, not summed).

An additional reproduced unload defect is fixed: HTTP response without boolean
`done=true` retains UNKNOWN (`provider_completion_unproven`) instead of claiming
release. Related server tests: `unload-terminal-evidence-server.log`, 41 PASS.
The [Ollama generation API](https://docs.ollama.com/api/generate) defines `done` as
generation completion; [the ps API](https://docs.ollama.com/api/ps) lists resident
models, not a per-request cancellation certificate. Neither residency nor an
unload of another request may substitute for the missing cancellation evidence.

Still required: bind provider identity before dispatch through trusted host
evidence, verify provider quiescence/termination after the proven sender exit,
and commit an authorized resolution via the existing recovery boundary. Provider
generation changes must respect baseline/Gate policy. No naive stopped-PID,
elapsed-time or restart-date release is allowed. Production deployment pending.

### Real process-loss and Docker restart evidence (isolated, not production)

`test_model_request_restart.py` uses a real loopback HTTP server and spawned
sender processes. The server receives the request before the first sender is
terminated. A second process is refused without a second HTTP request; the
server's later unobserved response does not clear ownership. The source hash,
mtime and committed non-empty checkpoint hash remain unchanged. This test PASSes
locally and in the actual Worker image on the server (`http-process-loss-server.log`).

`m3_model_request_restart_probe.py` also passed a real Docker restart across two
boots with the same dedicated fixture volume. The named test container was
verified running, with only `/candidate:ro` and `/fixture:rw` mounts, before
restart. Second boot exited 0, preserved UNKNOWN/token/checkpoint/source, and
refused duplicate dispatch. Evidence directory:
`/logs/m3-baseline-20260908T070237Z/docker-request-restart-U48jOk/` includes mount
evidence, pre-restart receipt, restart log, final state, result JSON and cleanup.
Only that exited, label-verified test container was removed; all fixture and log
evidence remains on the server. Production Worker/Ollama were not restarted.

These tests prove safe refusal after lost ownership, not a remote cancellation
certificate or successful production delivery. The known positive resume paths
remain a response observed and committed by the original callback, or confirmed
Future cancellation before execution. Controlled post-crash resolution still
needs authoritative provider evidence; no age/PID/VRAM inference was added.

### Route authorization and cancellation evidence (candidate, not deployed)

Read-only model discovery on the actual configured provider confirms that both
configured full IDs are advertised exactly (`model-route-readonly.log`). No
model load/inference or provider restart was requested by this check. The two
former implicit-substring compatibility tests were changed to the explicit
configured-only contract and reproduced unauthorized substitution before the
fix. Discovery no longer expands model authorization through a short name,
namespace omission or unique substring. Configure full provider IDs; missing
IDs follow the configured fallback chain. Current production names need no edit.

`executor.submit` failure no longer claims cancellation without a Future: its
receipt remains UNKNOWN. Conversely, a successful `Future.cancel()` on a pending
call records NOT_DISPATCHED and releases that request through the existing
receipt transaction. This is a specific verified cancellation condition, not
permission to expire UNKNOWN ownership. Two new composed tests cover both paths.

345 related tests PASS in server isolation, log
`route-cancellation-boundary-server.log`. Lost connections after dispatch still
cannot be declared cancelled from elapsed time, Worker death, model residency,
or free VRAM. Existing provider integration has no per-request cancellation
attestation captured in the receipt; that controlled recovery evidence and real
restart validation remain incomplete. Do not add an operator “clear owner” path
or claim this increment completes UNKNOWN recovery or M3 acceptance.

### Shared-resource admission increment (candidate, not deployed)

Read-only server topology evidence identifies the configured translation endpoint
as the Tower Ollama container, with Worker and Ollama both assigned the host's
single RTX 3060. Evidence: `endpoint-topology-readonly.log` and
`endpoint-gpu-binding-readonly.log` under the M3 log root. No provider/container
was restarted or reconfigured to gather this evidence.

New `translator_resource_scope` accepts `unverified` (compatible conservative
default), `shared_gpu`, or `independent`. Deployment must prove independence
before choosing it; an endpoint URL alone is not sufficient. The scope is saved
with each request, so changing endpoint/configuration does not erase old holds.
Production `config.yaml` was not changed; the example fragment documents the
setting. Actual baseline/configuration handoff is still pending.

Existing resource admission now defers GPU launches for unresolved shared or
unverified requests, even with free VRAM, using existing bounded retry intervals.
Explicit independent requests do not block local GPU; their own translation
endpoint is still protected. Unrelated CPU stages are not blocked. Worker checks
again through `validate_authorized_resource_launch_plan` after acquiring its
existing GPU kernel lease. No second resource lock or Queue was introduced.

The authority lookup is read-only, LIMIT 1, and requires the pending-owner partial
index rather than scanning Queue/history. Index/trigger installation now runs in
the existing Pipeline schema migration savepoint before first admission. Missing
or unreadable authority fails closed. Rollback must retain these additive receipt
protections; do not delete historical receipts to make an older consumer work.

342 focused/shared server-isolated tests PASS in
`durable-resource-admission-server.log`; final configuration validation and seven
new resource-boundary tests PASS (16 targeted tests) in
`durable-resource-config-final-server.log`. These runs overlap and are not summed.
Still required: controlled UNKNOWN resolution based on actual remote completion
or cancellation evidence, explicit route/alias contracts, restart acceptance,
runtime attestation, safe deployment and bounded live proof. No M3 acceptance yet.

### Worker/Translator binding increment (candidate, not deployed)

Lifecycle follow-up: managed Ollama unload now uses the same durable endpoint
reservation as inference. UNLOAD receipts may reference their completed attempt
(cleanup runs after processing); this does not permit a new inference on a
finished attempt. Required Pipeline mode refuses unbound cleanup. Each unload
POST is reserved before transmission and saves a response digest; ambiguous
failures retain UNKNOWN ownership. `/api/ps` remains bounded read-only inspection,
and existing VRAM admission still determines whether ASR can start. No long SQL
transaction is held across HTTP calls. No independent lifecycle Queue was added.

327 focused/shared tests PASS in server isolation:
`durable-unload-server.log`. Three composed lifecycle tests verify refusal while
an UNKNOWN inference exists, endpoint ownership after stage completion, and
retained UNKNOWN after unload transport failure. This is fixture evidence;
shared GPU admission and controlled resolution of UNKNOWN still require work.

The durable receipt component is now connected to all four Worker Translator
entry points. Required Pipeline mode resolves the committed active TRANSLATING
attempt using the source identity; a missing/mismatched stage fails closed.
The immutable context contains database, attempt, effective configuration/code
digest and retry limit. Executor work captures that context, using independent
short SQLite connections instead of sharing the Worker's stage transaction.

Every model call commits a reservation before dispatch. Complete responses save
a response digest before content parsing (transport completion is not QC PASS).
Definitive HTTP rejections can use configured fallback; exhausted primary budget
does not suppress an unused fallback budget. Transport errors and ambiguous
gateway/server errors retain UNKNOWN ownership, including HTTP 504. SDK internal
retries are disabled so adapter budgets cannot hide multiple network attempts.
Missing required context or failed receipt persistence prevents further calls.

A reproduced stage-idempotency regression was fixed: request lifecycle metadata
does not change configured model identity, while an actual model change still
conflicts. Callers cannot inject receipts through stage model configuration.

324 focused/shared tests PASS in server isolation on the actual Worker image:
`durable-binding-transport-final-server.log` under the M3 log root. Earlier logs
`durable-binding-server.log` (299) and `durable-binding-stage-identity-server.log`
(323) are intermediate evidence. Nine new composed tests cover the real
Translator/SQLite boundary and Worker stage binding with isolated HTTP fixtures.
The transport is a fixture, **not a live production/model-server acceptance**.

Remaining before deployment: persistent model unload/resource exclusion;
controlled resolution of UNKNOWN requests using real cancellation/completion
evidence; route alias/endpoint identity review; restart/container integration
and required runtime/baseline/Gate attestation. Existing holds remain untouched.

Durable receipt component work-in-progress: `model_request_state.py` uses the
existing Pipeline Job Store stage model metadata and stage events, not another
Queue. Reservations commit before dispatch, retain UNKNOWN ownership across
reopen, reject replay dispatch, and count model/request attempts across stage
retries. Receipts do not imply output QC success. Twelve isolated component
tests plus 21 existing Pipeline Job Store tests PASS on local Windows Python
(33 total); the concurrency test uses two real spawned processes. Source bytes,
mtime, model metadata and checkpoint fields remain unchanged. The log is stored
on the server share at `/logs/m3-baseline-20260908T070237Z/durable-request-local.log`;
execution was local, not in the production image.

This component is **not wired into Worker or deployed**. Remaining before wiring:
review metadata/event retention and mutation boundaries; bind active stage,
request identity and runtime identity at every translation/repair entry; classify
remote completion versus ambiguous transport failure; integrate resource and
controlled recovery evidence. Then run server isolated integration/restart tests.
No production DB was opened or modified for these component tests.

Follow-up boundary verification: three additional regressions first reproduced
metadata erasure of an unresolved owner, receipt deletion/rewrite, and expansion
of an already established request budget. Additive SQLite triggers now preserve
owned metadata unless its matching result event exists, and prohibit mutation or
deletion of request receipts (including retention cascades). Existing budgets can
be tightened but not replenished by adapter re-creation. Other metadata remains
preserved. Receipt retention needs an explicit future archival policy; it must
not silently discard pending ownership or spent attempts.

Final component suite: 15 request-state tests plus 21 existing Pipeline tests,
**36 PASS locally and in server isolation using the current Worker image**.
Server runs used a read-only candidate mount, no production work/media mount,
and no network. Logs: `durable-request-server.log` (35) and
`durable-request-boundaries-server.log` (36), under the same M3 log root.
This proves the component on the actual image, not live Worker integration.

Integration inspection located all four `_get_translator` call sites: ordinary
Japanese/non-Japanese translation and two targeted repairs. Binding must use the
committed active Pipeline attempt, not `_translator_progress_video` alone (one
path sets it after creating the Translator, and repairs cannot rely on it).
The existing stage-finish/checkpoint methods do not overwrite `model_json`.

- Explicit compatible alias/route identity and durable route-attempt evidence.
- Verify failure-budget persistence when failure occurs before a completed batch:
  current translation checkpoint writes follow successful batches and restore
  `last_model` only alongside completed batches.
- Extend verified process-local request ownership through existing durable
  checkpoint/resource/lease mechanisms. After a crash, lack of local Future is
  not proof that a remote request stopped; preserve unresolved ownership evidence
  and fail closed until the applicable recovery condition is proven.
- Exercise crash/OOM, timeout, exhausted fallback, missing resources and restart,
  keeping valid checkpoints and source/valid-output safety intact.
- Actual-image integration, safe deployment/controlled Gate handoff and bounded
  live evidence remain required. No production completion or M3 acceptance yet.
