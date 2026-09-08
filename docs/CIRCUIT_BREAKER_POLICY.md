# M2 Circuit Breaker Policy

## Current deployment boundary (2026-09-08 06:33 UTC)

The previously tested final-ASR evidence repair is deployed in Worker
`a4829a5298304f9e2dc20dd02ecde883ea53ec23`. No policy/QC/schema relaxation.
Reconciliation `m2-recon-fragment-20260908T0620` retained 67 holds, 1255 retry
counters and all prior UNPROVEN evidence. A valid DISPATCHED but unclaimed
historical job was retained, not cleared to manufacture idle. Formal recovery
returned ARMED and released its own hold only after verification. New Gate uses
the new runtime; no old cohort score transfer. Full
[evidence and remaining blockers](M2_REMAINING_ACCEPTANCE_20260908.md).

## 2026-09-08 05:10 UTC boundary clarification

Final ASR rejection evidence and durable fragment repair-budget candidate
`de6759499eaa0576dc9b9affd2f842109781d4c5` changes no breaker/QC policy.
It is not deployed: two safe-idle checks found real active work. Only this
attempt's owned pause was released after unchanged runtime/Gate verification.
Existing ARMED protection and 67 logical holds remain in force. Details:
[bounded acceptance](M2_REMAINING_ACCEPTANCE_20260908.md).

## Current closeout: controlled recovery restored (2026-09-08 04:32 UTC)

- Actual Worker: `65a9670fb1cbeb459939df60755366bd32589e63`.
  WebUI: `175a02a7bad46e0b6fa2372c59f39e8dd272911e` (unchanged).
  Worker image: `sha256:24c23d2a16ce93c5e94d526070bd230de963c427fb5d3c499edd2bbcdbdc8ae8`.
- Safe deployment `20260908T041804Z-3967701` and controlled recovery exited 0.
  Worker 1926 / WebUI 231 deployment tests PASS; actual-image targeted 245 tests,
  isolated container crash/restart, source safety and idempotency PASS; fresh breakers 7/7 PASS.
- Original failed preservation claims remain UNPROVEN. Original Gates, receipts,
  failed test reports and backups are retained, not relabelled PASS. The failed
  460619e actual-image test remains linked through the checkpoint-race snapshot.
- Reconciliation `m2-recon-source-decision-20260908`, receipt
  `/logs/m2-reconciliation-m2-recon-source-decision-20260908.json`,
  SHA256 `7163453b1031bda7dbfdf9d9988f35bfed44c2a6d845ae2b65f09954aaa143a9`.
  Ancestor: `/logs/m2-reconciliation-m2-recon-leading-gap-20260908.json`.
  Planned receipt: `/logs/m2-planned-runtime-change-source-decision-lane-20260908.json`.
  Controlled recovery: `m2breakerrec_7d47749bf57a4831b2debb589374b67c`.
- Logical holds: 67 = original 27 UNKNOWN revisions + 24 unexplained removals
  + 9 unproven arrivals + 3 later identity changes + 4 later evidence incidents.
  All 66 preceding holds preserved; new hold is the exact untrusted source-decision
  attempt. No source files moved/renamed/deleted. Holds include obligations absent
  from the current Queue, so these counts must not be added as disjoint Queue rows.
  Actual read checks rejected publication for 67/67 held paths; post-Gate held claims 0.
- 7140 queued identities reconciled as recoverable, subject to unchanged source/QC
  admission; 6889 other-state identities retained. This does not prove historical
  continuity. Historical revalidation preserved 1255 retry counters and protected
  checkpoint/output identities; 154 READY, 149 recoverable-total metric at that snapshot.
  READY alone is not delivery, and exhausted budgets/holds still prevent dispatch.
- Breaker ARMED; Gate ACTIVE: `m2-gate-20260908T042203260424Z-ea2562f0dc`.
  Start `2026-09-08T04:22:03.260424Z`, baseline
  `m2-guardrail-v1:4bd8a3c2eb64004cfd7c6717`.
  Configuration fingerprint:
  `sha256:355300b197164801be4616a688d852c1b8b5274fe91e91e40a2a21f12a3c4dbc`.
  Decision schema 1; frozen-first-20 policy unchanged, initialized 0/20.
  Old cohort scores were not transferred; no Gate pass / M2_PRODUCTION_ACCEPTED claimed.
- Real historical claim `aiobl_710b0dea0f5d5d287a8e69d6b63a8c72c44ee67c2376bada0982f81b8738f803`
  started 1788841654.478999, produced a hash-valid SUBTITLE_DETECTION checkpoint
  `ffb4ccc5a1dedc054fa70a95eebd75bc1432223b549b609b0324a4eade307dae`,
  and correctly retained review (`candidate_analysis_inconclusive`), not COMPLETED.
- Next automatic-remediation Queue claim
  `aiobl_e7c26b6e56ea6b0076a572536f2993cd844a0160a8fd99470f99f231507061d2`
  started 1788841662.3464353, entered actual ASR with heartbeat 1788841665.8556786,
  and new source checkpoint
  `1984e8ace517a19ebc45d03978ab8fe35c53ccdebcca0778ae592720ef8d42e8`.
  Its source SHA256 matched the already recorded current-run snapshot; this is not
  evidence that an older unrecorded source never changed.
- Both claims preceded scheduler wake command `cmd_98762a03dd33b3c9a0f63c6e`
  at 1788841680.467118. Therefore the earlier displayed deployment_hold was stale,
  not proof of an active lock, and the supplemental wake did not cause those claims.
  No lock/latch was removed. Existing control-loop dispatcher (300s interval,
  single in-flight recovery, finite budgets) dispatched the following recovery.
  Final snapshot: lane CANARY_IN_FLIGHT, one in-flight; no ongoing Codex watcher needed.
- Verified new formal subtitle deliveries in this closeout: 0. Claim/checkpoint/review
  are not subtitle delivery. Real download canary `m2dl_9e2ac34428c091432381` reached
  qB but stayed zero-byte/zero-peers and timed out as `did not start` at
  2026-09-08T03:22:40.561911Z. No valid downloaded file/extraction/formal publish verified;
  failed source remains excluded under existing alternate/backoff budgets.
- Remaining: 67 held sources/obligations require trustworthy evidence; local source
  analysis review and external source/peer availability remain unresolved. Existing
  788 uncertain mappings and 356 source-backoff inventory are not declared repaired.
  No QC/model/schema relaxation, source mutation, valid-output overwrite, or M3 work.
- Evidence root: `/logs/m2-source-decision-lane-repair-20260908/`.
  Actual-image tests: `/logs/m2-asr-postprocess-isolated-20260908T042133632441Z/`.
  Claims: `claim-evidence-1788841743016227257.json`.
  Source: `source-verification-1788841852268534312/result.json`.
  Final status: `final-status-1788841953550180079.json`.
  These final document updates do not redeploy or rebuild the Gate.

## Source-decision lane isolation (2026-09-08 04:15 UTC)

`5fc079223a9943c8216db19fd2673fb3e01ee247` deployed successfully; controlled recovery
`m2breakerrec_9041e0635e124e518f563d8aff4c2fa9` armed Gate
`m2-gate-20260908T040840367821Z-b04fc1a504`, preserving 66 holds and 7140 recoverable
Queue identities. Historical reconciliation preserved 1176 retry counters and all
checkpoint/output identities. A real historical claim reached SUBTITLE_DETECTION,
then its untrustworthy old decision (`source_decision_attempt_reference_invalid`)
was incorrectly classified as a global system error, pausing the recovery lane.

The exact rejection remains a local review failure; only its failure classification
is corrected. Generic unknown system errors still pause the lane. No decision is
blessed or replaced, no checkpoint bypass or QC relaxation is introduced. The
specific failed obligation will be logically held by the existing reconciliation
flow; all prior holds and failed receipts remain intact. Candidate 245 tests plus
isolated actual container restart PASS:
`/logs/m2-asr-postprocess-isolated-20260908T041522712563Z/`.
Actual deployment/next claim for this correction is pending; no new subtitle claimed.

## Checkpoint publication race addendum (2026-09-08 04:02 UTC)

Leading-gap fix `460619e47f49aa3e67dacf66112663644c97ef77` was deployed safely
(deployment `20260908T035610Z-3705934`, exit 0), but actual-image tests reproduced
a separate checkpoint race. Recovery did not run; admission hold and breaker
remain protected. Failed proof is preserved under
`/logs/m2-leading-gap-evidence-repair-20260908/actual-image-validation.log`.
A deterministic interleaving test proves a complete concurrent publisher was
misclassified between manifest/parent existence checks. The minimal candidate
uses the unchanged strict loader for an existing directory; an incomplete checkpoint
still fails closed and is never repaired in place. Candidate 225 tests plus actual
isolated container restart PASS at
`/logs/m2-asr-postprocess-isolated-20260908T040238664284Z/`.
The next handoff retains the same owned admission hold and seals a new snapshot
under `/logs/m2-checkpoint-race-evidence-repair-20260908/`, linked to the failed
460619e handoff. Old snapshots, tests, receipts and cohort results are not rewritten.
No formal new subtitle is claimed.

## Current override: leading-gap evidence repair (2026-09-08)

Worker `47d522ab940f5b84ced7170397ce86fe7cd63782` was safely deployed
(`20260908T031328Z-3188843`, exit 0). Line-repair recovery receipt
`m2breakerrec_eaa178852a68466d976765b6e0ef64a2` armed a new Gate and retained 65 holds.
A subsequent normal claim reached TRANSLATING with a real heartbeat/checkpoint,
but tripped `incorrect_completion` at 1788837825.967674: cached leading-gap repair
deleted the fresh accepted ASR diagnostic written by finalization. It is a distinct
incident, not a reason to replay the old recovery receipt.

The minimal candidate retains current hash-bound accepted diagnostics and rejects
missing evidence before writing a reusable probe marker. No QC/model/source policy
is relaxed. Server isolated candidate: 223 tests and actual container restart PASS,
`/logs/m2-asr-postprocess-isolated-20260908T035250308238Z/`.
An earlier unchanged concurrent-checkpoint test failed once; its failure remains at
`/logs/m2-asr-postprocess-isolated-20260908T033945375727Z/tests.log`; two subsequent
candidate suites passed. Do not describe that first run as PASS.

Historical lane was still PAUSED on an older version. The existing indexed-history
reconciler passes on an isolated DB: all 65 holds, 1176 retry counters, Queue identity,
366 checkpoint identities and 1111 output records preserved; 158 READY, no dispatch.
Evidence `/logs/m2-historical-lane-isolated-20260908T035101105868Z/`.
Planned controlled handoff `m2-recon-leading-gap-20260908` will retain all original
receipts/Gates and add the exact new incident; historical continuity stays UNKNOWN.
New runtime deployment/recovery and real resumed claims are not yet complete.

The bounded real download attempt for `m2dl_9e2ac34428c091432381` passed source enqueue,
but qB reported zero bytes and zero peers, then no exact torrent at the follow-up.
No usable download, extraction or new formal subtitle was verified (all 0).
Current runtime remains TRIPPED; no Gate success or Production acceptance is claimed.

## Restricted line-repair incident recovery (candidate, 2026-09-08)

`authorized_reconciliation` may identify `line_repair_evidence_incomplete` only
for an exact `incorrect_completion` / `m2_strict_completion` incident. Its request
must bind `line_repair_incident`, the preserved review-required attempt, source hold,
original Gate/claim hash, exact breaker evidence, current runtime and frozen diff.
For claims outside the frozen cohort, an original TRANSLATING stage with an intact
input hash and exact delivery attempt, plus the matching failed review command and
incident time, supplies the binding instead. It does not create a Gate member or
fabricate a historical Gate claim hash. An unbound incident remains rejected.
The old ASR-loss proof contract is not accepted for this distinct incident.

The required `m2-line-repair-regression-v1` report binds actual-image/source revision,
hash-verified test/restart logs and exact main, line-repair, strict-verifier and runtime
code. It must prove source-history prerequisites, review settlement order, unchanged
strict verification, preserved incident, restart, checkpoint, idempotency and safety.
Missing proof or a new unrelated trip remains fail-closed. No latch/lock deletion,
historical receipt relabeling or old Gate result transfer is authorized by this mode.
The existing durable pause, idle checks and controlled recovery remain mandatory.

## Restricted ASR postprocess incident recovery (deployed, 2026-09-07)

Authorized reconciliation may explicitly identify `asr_postprocess_diagnostics_loss`
for `incorrect_completion` at `m2_strict_completion`. It is not a generic breaker
override. Existing durable pause, idle/lease, exact runtime, fresh seven-breaker FI,
immutable reconciliation, Queue/checkpoint/output preservation and new-Gate rules
remain mandatory. Additionally require hash-verified actual-image regression logs,
tested code identity, real container restart, unchanged quality gate and retained
review-required incident under a persistent source hold. Unrelated new trips,
missing evidence, unheld incident or changed request must refuse recovery.
Historical missing ASR diagnostics are not reconstructed as past acceptance;
the incident remains review-required, excluded from claims and publication.

## Verified owned deployment mode (2026-09-06 22:40 UTC)

The existing safe updater now supports optional `RECONCILIATION_HOLD_ID`.
It verifies the matching durable paused reconciliation hold before maintenance
and at scheduler closeout. `deployment_hold` is accepted only with that verified
ownership and healthy Worker/heartbeat checks; the updater does not release
reconciliation admission. Failure after retiring containers preserves current
databases/images/checkpoints and protection, reports nonzero, and does not run
the legacy Production database rollback. Normal deployment mode is unchanged.

This mode was applied with `m2-recon-sidecar-20260906`; formal controlled recovery,
not the deployer or direct state edits, then resumed admission. All prior 60
holds survived and three newly changed Queue identities were additionally held.
Runtime is ARMED on Worker `d508b599291978cc5f6ab786c560823ea755e249` and frozen
baseline `m2-guardrail-v1:4909e2dd94c41ae93f96a169`. Earlier receipts and the
UNKNOWN historical continuity evidence are retained. See the observation
closeout for actual identity, Gate and immutable evidence paths.

## Applied authorization record (2026-09-06)

`m2-recon-20260906-b92a61f` applied the policy below: 27 UNKNOWN revisions,
24 unresolved removals and 9 unsupported new identities remain held (60 total).
The original failed receipt/Gate were not rewritten; the new boundary represents
verified current Queue identity, not proven historical source continuity.
Runtime `b92a61fe086bb0cb48aef46f9b9efd967d33d969` is ARMED after fresh 7/7 FI.
New Gate starts at `2026-09-06T11:54:57.465856Z`, frozen baseline
`m2-guardrail-v1:5777c0c5ec237c4b07cb56b4`; only its own first eligible 20 claims
can qualify, with failures retained and no historical recovery backfill.

During this deployment the legacy scheduler closeout called reconciliation hold
`deployment_hold`; the deployment owner was preserved until formal controlled
recovery released admission, then its existing final verification completed.
Do not solve this sequencing condition by deleting control files, releasing holds
manually, accepting fake scheduler status, or rolling back the Production database.
Future deployment automation must distinguish the two holds before reusing this
maintenance sequence. Document-only synchronization must not redeploy or rearm.

## Authorized current-state reconciliation (2026-09-06)

The user explicitly authorized a new reconciliation boundary after the failed
planned handoff. The original receipt/Gate/backups remain immutable and their
historical preservation is **UNPROVEN**, not PASS. This is not a retry of that receipt.

`pause-reconciliation` persists admission hold in existing control state.
`prepare-reconciliation` requires a paused idle boundary, the original Queue
membership digest, disposition of identity differences, complete current Queue
coverage, and immutable source holds in the existing recovery database.
Unexplained revisions and removals remain quarantined/pending obligations.
New hashes describe current bytes only; incompatible old checkpoints are not resumed.

Normal/recovery claims and Worker/official publication reject held source paths.
Filesystem events remain durable during maintenance but cannot promote into Queue.
`recover --reconciliation-record ... --reconciliation-record-sha256 ...` uses
the controlled breaker lifecycle, matching actual runtime and fresh fault tests.
Changes after sealing are refused, never silently absorbed. Resume additionally
requires ARMED matching Gate, unchanged holds/Queue/checkpoints/output records,
and a durable controlled recovery event. No direct latch deletion is authorized.

Production execution and final record/Gate IDs remain pending below; implementation
tests alone do not establish ARMED or successful recovery.

## Verified recovery boundary and next deployment constraint (2026-09-05)

The exact collision/quality-pause recovery completed on Worker `60d6b236...`,
with fresh running-image 7/7 evidence and ARMED replacement Gate. Its immutable
incident receipt is consumed history, not authority for later normal updates.
The next automatic claim safely reached NEEDS_REVIEW with durable evidence;
quality refusal must not be relabeled completed or treated as a system streak.

A normal ARMED runtime change needs a separately bound planned handoff. The
safe stack updater preserves a pre-existing durable pause and waits for idle,
but it does not itself retire an M2 Gate. Do not mutate runtime JSON, clear a
latch, reuse collision evidence, or feed a changed baseline to an already-ARMED
manifest. Preserve the active cohort until actual runtime change is proven;
any supported handoff must bind old Gate and immutable evidence, unchanged
configuration/schema, absence of intervening claims or unrelated incidents,
new runtime attestation and fresh isolated 7/7 before a replacement 0/20 and
claim resume. Documentation-only changes never trigger this workflow.

## Recovery unblock candidate safety boundary (2026-09-05)

An expected deployment can append a runtime-change trip while the original
failure remains latched. Controlled recovery must bind that separate transition
to the immutable first attestation receipt and exact old-Gate invalidation,
matching container/source identities and unchanged config/Decision Schema.
Preserve the original failure time, all trip occurrences and frozen members.
Any later unknown trip, result event or claim (including non-cohort claims)
refuses recovery. This evidence does not replace final running-image attestation.

Generic Worker failures are grouped by normalized underlying detail, not only
the generic error code; media paths and attempt IDs cannot split one recurring
cause. Typed materialized-subtitle content refusals use existing NEEDS_REVIEW
semantics. Three distinct jobs with a genuinely identical system failure still
trip. Each trip occurrence retains its current event/member evidence.

The narrowly supported `generic_failure_signature_collision` recovery requires
the exact current Gate, immutable three-attempt/event/digest evidence, matching
current counter and trip epochs, distinct normalized cause groups below the
threshold, unchanged sources/Queue/checkpoints, a safe claim boundary, changed
verified runtime, fresh 7/7 fault injection and hashed running-image regression
evidence. It cannot clear a genuinely identical streak or run blanket historical
reconciliation. Normal retry/backoff and existing recovery-lane limits remain.

A local hard-display-limit QC refusal cannot pause unrelated recovery as a
permanent system failure. Repair of the already-paused lane requires exact last
canary dispatch/settlement hashes, current run/version, review-required result,
unchanged paused Queue entry and checkpoint, and no inflight recovery. Preserve
the rejected canary; only the proven lane-level pause can become CANARY_READY.
Dispatch checks both recovery and Queue deadlines plus remaining budget again
at the atomic update; normal backoff cannot be cleared to manufacture a claim.

Normal discovery and due recovery share bounded, restartable source scheduling.
Elapsed-budget preemption preserves pending requests and retry budgets; incomplete
matching evidence cannot become a confirmed miss or a selected winner. Source
network calls must not hold SQLite write transactions. This is cooperative time
bounding with existing HTTP timeouts, not a hard kill of arbitrary filesystem or
trickling network operations.

Official import must pass complete parsing, existing source validation and the
unchanged hard QC. Actual staged bytes are checked again before any formal write,
backup or receipt. Bad downloaded ASS files are not repaired by byte-copy
normalization. Failed content cannot overwrite valid output or become COMPLETED.
Verified season-scoped metadata does not authorize unseasoned legacy batches.

The candidate does not change breaker thresholds, Decision Schema, models,
frozen-cohort membership or runtime configuration. Actual deployment must preserve
the old Gate as INVALIDATED_BY_RUNTIME_CHANGE and attest the new running image
with fresh isolated 7/7 evidence before claims resume. Read-only verification and
document synchronization never trigger that transition.

## Acceptance-only follow-up, 2026-09-05

Read-only target validation, bounded source lookup, immutable-event inspection
and documentation synchronization did not change runtime baseline or recreate
the Gate. The b911 runtime was still `ARMED / runtime_baseline_match` and Gate
`m2-gate-20260905T045640981079Z-08147de925` remained ACTIVE. Historical download
dispositions and two observed recovery claims do not replace cohort members.
An isolated `database is locked` retry and a source-selection NEEDS_REVIEW
retained their existing budgets/checkpoints; neither was relabeled COMPLETED.
The four missing-valid-subtitle targets remained source-blocked under current
QC/dedup policy. No breaker thresholds, source decisions or claim protections
were changed. Detailed acceptance boundaries are in M2_PRODUCTION_OBSERVATION.

## M2 download recovery boundary (2026-09-05)

The final M2 runtime is Worker
`b911794ed0ec872cb475f714e1385e20e8ac4388`. Runtime `ARMED` was re-attested
after safe deployment and fresh 7/7 fault injection; the frozen baseline is
`m2-guardrail-v1:5b4d2a88f2d5c0c5749f6747`, initially `0/20`.

A controlled recovery publishes the SQLite Gate and runtime manifest under a
durable claim pause. During the narrowly identified DISARMED handoff, valid
recovery contract/record and pause evidence deny admission without invalidating
the replacement Gate. A delayed sample is ignored only after transactional
revalidation of the current ARMED runtime against the exact active Gate.
Ordinary DISARMED states, damaged evidence and genuine runtime changes remain
fail-closed. This exception never permits a claim or relaxes a breaker.

An orphan DISPATCHED recovery item with no claim, no Queue item and no running
job/delivery is recorded as `EXCLUDED / KEEP_NEEDS_REVIEW`, never succeeded.
Its immutable pre-claim event preserves media identity evidence and advances
only the existing single-canary lane. No Queue item is blindly recreated.
Quality/bad-input exclusions and budgeted retries leave the lane CANARY_READY
with existing backoff; a success is still required for ACTIVE. Permanent
system faults remain paused and TRIPPED runtime always blocks dispatch.
These records and all download recovery metrics cannot backfill a frozen Gate.

Download repair leaves breaker thresholds, Decision Schema, AI QC, hallucination
validation and model routing unchanged. External subtitle import applies the
existing Source Analyzer policy plus full timestamp parsing before atomic
publication or AI subtitle retirement. Rejected candidates are
`subtitle_validation_failed`, never COMPLETED, and use existing failed-hash,
seen-source and retry/backoff policies.

qB queued/checking/moving/paused states remain normal after partial download.
Stalled partial torrents and pieces remain available for qB resume while a
different allowed source may be considered. Timeout cleanup never deletes
downloaded files. Untrusted project torrents are not reassigned. Only an actual
runtime change invalidates the frozen Gate; history cannot fill cohort slots.

## Purpose and scope

The M2 circuit breaker is a fail-closed admission guard. It protects source
media, published outputs, queue state, and completed stage checkpoints while
the M2 strict observation gate is active.

The breaker controls only the admission of a new AI job. Opening the breaker
must not cancel an already-running job, remove or rewrite a queue item, delete
a checkpoint, modify source media, or publish a replacement output.

This policy does not establish production acceptance, an observation-gate
result, or an autonomy-rate result.

## Runtime states

| Status | Meaning | New job admission |
| --- | --- | --- |
| `DISARMED` | The observer or circuit-breaker feature is disabled. No M2 guardrail claim is made. | Allowed only when the observer itself is intentionally disabled; otherwise denied until the runtime is armed. |
| `ARMED` | The effective runtime settings, immutable runtime baseline, container/source evidence, decision contract, and required isolated breaker evidence all match. | Allowed while the latch remains closed and the disk check passes. |
| `TRIPPED` | A breaker reason is durably latched. | Denied. Running work may finish and valid checkpoints remain available. |
| `DEGRADED` | Guardrails are intended to be active, but runtime state is missing, unreadable, inconsistent, unsafe, or no longer matches the armed baseline. | Denied fail-closed. |

`ARMED` describes the guardrail runtime only. It is not a claim that a later
quality or production observation gate has passed.

## Immutable runtime baseline

Arming creates one immutable baseline containing the Worker and WebUI commit
SHAs, their running container image IDs and source revisions, the effective
configuration fingerprint, Decision schema/version/contract, gate start time,
and derived gate baseline version. The runtime must verify this evidence from
the running containers and loaded configuration, not merely from files present
in Git.

A missing, unreadable, or mismatched runtime baseline is `DEGRADED` and denies
new claims. A different source revision, configuration fingerprint, decision
contract, or formal baseline requires a new explicit validation and arm; it
must never inherit the prior `ARMED` assertion silently.

For a formal observation cohort, any result-affecting runtime, program, image,
configuration, or Decision-contract change must durably mark the active gate
`INVALIDATED_BY_RUNTIME_CHANGE`. Collection for that gate stops immediately,
and results from different baselines must never be combined. Re-arming requires
a new baseline and gate start.

## Admission boundary

Every production claim path calls the fail-closed Worker admission wrapper
immediately before the queue can be mutated or work can start. The wrapper
delegates to the circuit-breaker admission decision and returns false on a
runtime validation error.

The same boundary applies to initial dispatch, serial dispatch, concurrent
dispatch, and guarded remediation dispatch. A trip does not interrupt an
already-running job. The persisted policy is:

- action: `stop_claiming_new_jobs`
- running job policy: `finish_without_interruption`
- checkpoint policy: `preserve`

## Breaker conditions

| Reason code | Trigger | Threshold |
| --- | --- | --- |
| `source_mutation` | Source identity, revision, or checksum changes during processing. | Immediate |
| `duplicate_publish` | A destination collision or second publication cannot be matched to the same verified delivery evidence. | Immediate |
| `output_parse_failure` | A prospective final output cannot be parsed or fails final publication revalidation. | Immediate |
| `incorrect_completion` | Work reports success without the evidence required for a valid completed delivery. | Immediate |
| `repeated_oom` | Distinct jobs consecutively report an eligible GPU/process out-of-memory failure. | 3 distinct jobs |
| `repeated_identical_stage_failure` | Distinct jobs consecutively report the same eligible stage and normalized failure signature. | 3 distinct jobs |
| `insufficient_disk_space` | Any required runtime volume is below the configured free-space floor or its capacity cannot be read. | Immediate at admission |

A successful or non-system outcome resets both repeated-failure streaks. A
non-OOM system failure resets the OOM streak, and a different eligible
stage/failure signature starts a new identical-failure streak. Each streak
retains bounded stable-job identities. Retry replay, process-restart replay,
terminal-observation replay, duplicate delivery-attempt evidence, and a new
attempt for the same delivery obligation cannot increment it. The two
distinct-job thresholds are configured as `3` for this milestone.

## Failure classification and historical reconciliation

| Category | Automatic disposition |
| --- | --- |
| `TRANSIENT` | Bounded retry/backoff from a compatible checkpoint or the failed stage. |
| `RESOURCE` | Existing lower-memory/resource fallback, then bounded checkpoint/stage retry. |
| `CODE_VERSION_FIXED` | Re-evaluate on the attested new runtime and resume the nearest compatible safe stage. |
| `BAD_INPUT` | `UNSUPPORTED`, `QUARANTINED`, or `NEEDS_REVIEW`; never an unbounded retry. |
| `QUALITY_BLOCKED` | `NEEDS_REVIEW` with preserved evidence; never false `COMPLETED`. |
| `PERMANENT_SYSTEM_ERROR` | `FAILED` with evidence after recovery is not proven safe. |

The durable recovery decisions are `RECOVER_FROM_CHECKPOINT`, `RETRY_STAGE`,
`REPROCESS_FROM_SAFE_STAGE`, `REQUEUE_WITH_NEW_RUNTIME`,
`KEEP_NEEDS_REVIEW`, `KEEP_QUARANTINED`, `MARK_UNSUPPORTED`, and
`KEEP_FAILED`. Checkpoint JSON must be canonical and match its stored SHA-256.
Stage-specific schema compatibility is checked before claiming checkpoint
resume; an incompatible checkpoint is preserved as evidence while only its
minimum safe stage is rerun.

The recovery lane is a separate indexed SQLite ledger. It dispatches one exact
item into the normal queue, marks the first item as the mandatory canary, and
never enumerates the media tree. A repeated same-stage/signature failure on the
same runtime with no new checkpoint is no progress. The job is not requeued
after its bounded budget and the rest of the normal queue remains eligible.

## Persistence and evidence

Trips are atomically persisted with a normalized reason code, timestamp,
bounded evidence, action, running-job policy, and checkpoint policy. A
malformed or unreadable latch fails closed instead of silently clearing.

The full isolated validation stream is retained on the server as
`events.jsonl` together with `result.json` in a newly created timestamped run
directory. A successful command writes only a bounded summary to stdout. A
failure writes only a bounded log tail to stdout; the complete traceback and
per-assertion evidence remain in the server log.

The armed state retains the sanitized validation window, pass/required counts,
Worker source revision, result digest, and full event-log digest. This makes
the accepted breaker proof independently checkable without copying private
paths or raw logs into tracked documents.

Tracked documentation and bounded summaries must not contain media names,
source/output paths, server addresses, ports, credentials, or raw logs.

## Frozen observation cohort contract

The formal cohort is the first 20 distinct eligible jobs claimed after the
immutable gate start, ordered and durably assigned at claim time. Once all 20
slots are assigned, later claims are outside that cohort. Failed,
`NEEDS_REVIEW`, and `QUARANTINED` members retain their slots and cannot be
replaced by later successful jobs. The eight recorded pre-gate attempts remain
available as historical observations but are never cohort members.

Every cohort member must durably retain claimed time, final state,
strict-verification result, output-parse result, hard-QC result, hallucination
result, source-checksum result, duplicate-job and duplicate-publish results,
and checkpoint/retry/fallback result. Exactly one cohort summary is produced
only after all 20 fixed members have reached terminal states.

The persisted, sanitized observation summary includes `gate_progress`,
`claimed_after_gate_start`, `completed_strict_verified`, `needs_review`,
`failed`, `quarantined`, `hallucination_blocked`, `output_parse_failures`,
`source_mutation_incidents`, `duplicate_jobs`, `duplicate_publishes`,
`breaker_trips`, `checkpoint_resumes`, `oom_events`, and
`processing_strategy_counts`.

Producing the summary must not scan or poll the full queue. A breaker trip is a
separate durable event: it immediately denies new claims and persists bounded
reason/evidence without requiring a full-queue scan.

This section is the required contract. The 2026-09-04 closeout audit invalidated
the historical success-count observer as
`INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`. The replacement implementation
uses a durable frozen first-20 cohort, immutable per-job evidence, result-event
journaling, and `INVALIDATED_BY_RUNTIME_CHANGE`; repository tests do not make it
Production-ready until the repaired Worker image passes fresh server validation
and initializes a new deployment-bound Gate. Breaker 7/7 evidence alone never
proves the cohort contract or accepts the Production Gate.

## Isolation contract

The fault-injection harness never loads the production configuration. Each
case creates fresh temporary input, output, queue, running-job, checkpoint,
work, and log fixtures. The only caller-provided location is the parent for a
new timestamped validation artifact directory.

Every one of the seven cases must prove all ten invariants:

1. The production admission wrapper was called.
2. A new claim was stopped.
3. The queued job remained unchanged.
4. The valid checkpoint remained unchanged.
5. The running job was not interrupted.
6. No false `COMPLETED` state was produced.
7. The expected reason and non-empty evidence were persisted.
8. The isolated latch could be safely recovered.
9. The synthetic source remained byte-identical.
10. The output fixture remained untouched.

Disk exhaustion is injected through an isolated capacity hook rather than by
filling a real volume. OOM and repeated-stage failures are injected as
synthetic terminal observations rather than by crashing production models.

## Recovery policy

The breaker is latched and must not auto-reset. Recovery requires the cause to
be understood and remediated first. Evidence must be archived, not discarded;
fresh running work must reach a safe boundary; cutoff-proven stale work is
terminalized as interrupted without deleting its checkpoint; and the runtime
must be probed again before admission resumes.

Production recovery uses `m2_guardrail_runtime.py recover`. It refuses an
unsupported cause, unchanged Worker runtime, missing fresh 7/7 evidence,
mismatched runtime/config/Decision identity, active recent work, changed source
identity, changed queue identity, changed checkpoint content, or changed
formal-output evidence. It writes and validates a durable operator claim pause,
appends a recovery event and timestamped full log, invalidates the old Gate,
and writes `DISARMED` before closing the latch. A second attested arm creates a
new immutable `0/20` Gate. The durable pause is cleared only after the runtime
is `ARMED`, the new Gate is active, and observation reports a matching baseline;
no deletion of the breaker file or observation rows is part of recovery.

If deployment or process restart occurs after the latch is cleared but before
the replacement Gate is armed, recovery remains fail-closed. A pending-resume
operation must revalidate the original recovery record and log, old-Gate
invalidation, current runtime/source/config/Decision identity, and fresh fault
evidence before re-establishing the durable pause and continuing. It must not
repeat historical reconciliation or mix Worker versions in one Gate.

The isolated harness proves this sequence by requiring persisted reason
evidence, moving only the sandbox latch to a recovery archive, writing a
recovery record, clearing only the sandbox process latch, re-running the real
admission wrapper, and rechecking all fixture hashes. This sandbox proof does
not by itself validate a live-container restart or production recovery.

## 2026-09-05 live controlled recovery

The Production incident was caused by three distinct
`source_selection_needs_review` outcomes at `source_selection_review`. Durable
pipeline state correctly held them as `NEEDS_REVIEW`, while the legacy adapter
misclassified them as retryable system failures and the breaker counted that
signature. The repaired classification excludes `QUALITY_BLOCKED` review
outcomes, and streak membership is now keyed to distinct stable jobs.

The final running Worker
`d9dfcd01aa9ebeffe65c8367f4e1bbace56d5bcc` passed the fresh server-image 7/7
suite, completed the restart-safe pending recovery, transitioned the breaker
from `TRIPPED` to `ARMED`, invalidated the prior Gate as
`INVALIDATED_BY_RUNTIME_CHANGE`, and created
`m2-gate-20260905T020845085531Z-7d0c5c7333` at `0/20`. Normal claims resumed
only after the active baseline matched. Exactly one recovery canary was
dispatched. The bounded checks reported no Production source-media or formal-
output change.
