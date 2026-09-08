# M3 — configured model recovery

## Pending diagnostic patch — 2026-09-08 11:35 UTC

The repeated runtime SQLite error still has no proven lock owner. Inspection
confirmed logger.log_failure discarded the original traceback. Candidate now
preserves the original exception stack in the existing rotating app.log, without
locals; failed.log retains its one-line compatibility and bounded rotation.
String errors never attach an unrelated ambient exception. Regression reproduced
the missing stack before the fix, then all 5 logger tests passed locally and in
an isolated server container using the deployed image (read-only candidate,
no network or Production media). Evidence: /logs/m3-runtime-handoff-20260908/
traceback-targeted-20260908T1135.log and .exit (0).
This is diagnostic readiness, NOT a fix/proof for SQLite contention. Not deployed;
Worker5f379e8/Gate/67holds unchanged. Next deploy only through the existing owned
safe handoff if needed for real stack evidence; never patch live container code.

## Retry and automatic continuation proof — 2026-09-08 11:31 UTC

Exact known job 1fc3bd293f5e4f86be7fc0cbb589d9ee settled NEEDS_REVIEW;
its Queue row is paused/asr_review, attempts=3, deterministic_asr_quality,
retry_strategy=manual_review, next_retry_at=0. No unbounded retry is established.
The existing main.py transcription_review branch explicitly preserves review.

Server automatically started successors after this review: job
85b16c697dcb4e97a0fc1962506219c9 performed SUBTITLE_DETECTION and retained a
hash-valid source-decision checkpoint, then NEEDS_REVIEW for
candidate_analysis_inconclusive. Job a98b82c521084e4fb54d33f68b534d42 also
started SUBTITLE_DETECTION, then RETRYABLE_FAILURE: database is locked.
A single bounded 256 KiB log excerpt confirms a second database-lock failure
at 11:31:17. Root transaction/lock owner is NOT yet identified; no traceback
was logged. Investigate this reproducible failure before claiming M3 complete;
do not clear locks/DB fields or classify lock contention as bad media.
Recovery-lane dispatch and actual isolated subprocess start at 11:29:34 are
also recorded, distinct from normal queue continuation; neither is delivery.

Read-only status at 11:30:10: Worker5f379e8, ARMED/runtime_baseline_match,
admission unpaused, 67 holds, Gate m2-gate-20260908T110433538511Z-b43d836495
unchanged. No new deployment, QC relaxation, formal delivery or Gate pass.
Evidence under /logs/m3-runtime-handoff-20260908/: known-retry-authority.json,
known-retry-process.txt, post-review-continuation.json,
following-stage-reasons.json, database-lock-trace.log.


## Bounded post-deploy evidence — 2026-09-08 11:26 UTC

No additional runtime change, deployment, Gate reset or hold release in this
check. Worker remains the deployed 5f379e8 baseline described above.

- Four exact historical download entries (260:1/2/3/8) were present in SQLite;
  their full payloads matched the pre-M3 deployment backup before this check.
  A missing optional `status` key is not a missing obligation.
- One bounded primary-source query succeeded (206 releases, 8.59 seconds).
  Exact candidate-hash qB query found no reusable downloads. Episodes 1/2/8
  had no eligible choice under existing failure/seen rules; episode 3 did.
- Obligation `m2dl_62b18022da8c630ffb2d`: unique formal target, no verified TC,
  source not held; existing controlled replacement entry added one candidate
  `439ca54daae64082d358e64404bd94ee3b87b496` at 11:25:20 UTC. Failed/seen
  history retained. This is dispatch evidence, not autonomous claim or delivery.
- One subsequent exact sample: `stalledDL`, 0 bytes, no extraction job. Do not
  delete/re-add it or count HTTP/qB acceptance as usable subtitles. Existing
  retry/source-alternative policy owns continuation. Source SHA-256 before/after
  equals `c5de59a7183a586dfbf87657e207dd19c5932bb71d2d13da8213e539c7faf4b7`;
  existing subtitles unchanged, new formal subtitles **0**.
- Known AI job `1fc3bd293f5e4f86be7fc0cbb589d9ee` reached ASR review for
  Japanese single-character/punctuation fragments. A subsequent attempt
  `0aa89ea8bcc84866887eee2247f75b74` is ASR RUNNING, with start/heartbeat
  1788866383.6462607. No MODEL events or provider-confirmed formal output yet.
  Its repeated ASR attempt needs bounded retry-policy verification; do not
  assume quality recovery succeeded or force it past review.

Evidence root: `/logs/m3-runtime-handoff-20260908/`:
`download-target-history.json`, `download-four-source-probe.log`,
`download-four-case-20260908T112309892299Z.json`,
`source-case-result-1788866693948861863.json`, `download-e3-safety.log`,
`source-status-1788866766811468632.json`, `known-job-final-probe.json`.
No full Queue polling, library rescan, or wait for download/Gate completion.
M2 and M3 acceptance remain incomplete.


M3 is authorized and active. M2 production acceptance remains incomplete;
its frozen cohort, held sources and UNPROVEN preservation claims are unchanged.

## Scope

## Current deployed closeout — 2026-09-08 11:10 UTC

M3 candidate **is now deployed**, but M3 model-output acceptance and M2 production
acceptance remain incomplete. Earlier candidate/no-deploy notes below are history.

- Worker runtime SHA: `5f379e877ce59cb3207927e38aad2f6735b76287`.
- WebUI runtime SHA: `175a02a7bad46e0b6fa2372c59f39e8dd272911e`.
- Worker image: `sha256:d5757e05517f67da98b56976cdf14d04d962f0d5f884e5a7fae98041d50ff8f1`.
- Safe deployment `20260908T104945Z-351566`, exit 0; all old backups retained.
- Actual-image targeted tests: 211 PASS; full candidate regression: 2023 PASS.
- Fresh isolated breaker suite: 7/7 PASS, no Production resources affected.
- Runtime status verified ARMED; host provider binding and cron observation active.

The owned admission drain preserved existing work and 67 source holds. Sealing
initially waited for actual idle; no running row or lock was cleared. The original
planned receipt hash is
`sha256:79079437ca0df8c69b81b53d7066481933a7fd442c799d235e98099f43c1dfef`.
First reconciliation refused after 24 new transitions appeared. These were exactly
12 unclaimed filesystem ingest jobs (DISCOVERED/STABILIZING), proven by the existing
planned-snapshot validator with no Queue/checkpoint/output/cohort changes. The
planned recovery mode itself also refused changed frozen policy, as it should.
Neither rejection nor receipt was altered.

Our own reconciliation hold was transferred while continuously paused to seal
`m3-recon-model-recovery-20260908-late-ingest`, hash
`sha256:8011f9db1e4e34df382293a7ee37898e8e21184dc65a3d3b079b60693cdcf96d`.
It links the failed record and classified ingest evidence: 67 holds retained,
7,097 recoverable identities, 6,932 retained other-state identities; no unexplained
Queue identity differences. Historical preservation remains UNPROVEN.

Official recovery succeeded with this record and fresh fault evidence; no direct
latch/lock/DB edits. New frozen Gate:
`m2-gate-20260908T110433538511Z-b43d836495`, start
`2026-09-08T11:04:33.538511Z`, baseline
`m2-guardrail-v1:e365b1132703b8cd1ae7e063`, initialized 0/20. Old Gate evidence retained.

Pinned observer installed via the existing persistent Dynamix cron mechanism;
other cron entries verified unchanged. Script SHA
`3a6c1808bb12f80e53948b3aaadc1badcfc58080b62f18b0f0d55e15f8fb35c1`.
At 11:06:24 UTC, the observation was automatically refreshed ~2.4 seconds earlier,
well after arming. Host reboot itself was not performed or claimed as tested.

Bounded live proof: cohort ordinal 1, claim hash
`59f004457c47c8da924be9be3c4559bc6dd67b8ef19641a43e6a2ac5ccb6c71d`,
claimed epoch 1788865803.455295, reached SUBTITLE_DETECTION then NEEDS_REVIEW.
Checkpoint `7acf956cf9c4e6433fde94520c03559a97830e859915e99297cdded8ea9cc4b1`
matches stored bytes. It remains a failed/review cohort member, not replaceable.
The next pipeline job `1fc3bd293f5e4f86be7fc0cbb589d9ee` started ASR at epoch
1788865828.8114753, with a verified source-decision checkpoint from detection.
The first claim preceded the one-time scheduler wake request at 1788865806.5347097;
do not attribute automatic continuation to that later command.

Evidence root `/logs/m3-runtime-handoff-20260908/`: `safe-deploy.log`,
`actual-runtime-before-recovery.json`, `actual-image-validation.log`,
`late-fresh-fault.log`, `recovery-closeout.json`, `runtime-claim-closeout.json`,
`runtime-post-wake.json`, `observer-installed.sha256`, and all failed attempt logs.
Fault detail `/logs/m2-guardrail-fi-20260908T110426747663Z-f04674dd/`.

Formal subtitle additions this handoff: **0**. Real model response/fallback and
provider-confirmed formal output, M2 download/extraction acceptance, and frozen
20-job completion remain unverified. Do not wait for all jobs or call M2 accepted.

## Complete candidate regression — 2026-09-08 10:42 UTC

The full candidate suite completed in an isolated container using the deployed
image's dependencies, candidate code read-only, network disabled and no Production
media mounts: **2023 tests PASS**, exit 0, 45.992 seconds. This is not a newly built
or deployed runtime image. Evidence:
`logs/m3-baseline-20260908T070237Z/candidate-full-predeploy-final.log` and `.exit`.

Initial full run retained in `candidate-full-predeploy.log`: 2023 tests,
three failures/one error in host-observer shell fixtures. Test import order sets
global tempfile storage to Docker's noexec `/dev/shm`; executable fake commands
then failed with permission denied. Only the shell fixture now explicitly uses
`/tmp`. The full corrected run verifies it without skipping or weakening tests.

The complete safe-update-stack flow was reviewed. This handoff must use an owned
`RECONCILIATION_HOLD_ID` so post-retirement failure preserves live databases and
evidence, not the generic database rollback branch. Do not run prior immutable
fragment helpers again. Prepare a fresh owned drain/idle handoff, preserve all
existing holds/Gate evidence, and install the pinned host observer as part of the
controlled transition. No admission pause or Production deployment occurred during
this regression step.

## Predeployment live boundary — 2026-09-08 10:36 UTC

Read-only runtime verification still reports ARMED / runtime_baseline_match,
Worker `a4829a5298304f9e2dc20dd02ecde883ea53ec23`, WebUI
`175a02a7bad46e0b6fa2372c59f39e8dd272911e`, original Gate baseline
`m2-guardrail-v1:6a39511185854d6d968c637b`. The Gate fields in runtime state are
initialization metadata, not a newly measured live cohort count.

A single lite-status read at 10:35:33 UTC shows an active transcription task with
fresh heartbeat, scheduler processing, no deployment hold, zero reported running
extractions and Mikan not busy. Do not kill the active task or treat this snapshot
as a stable deployment window. Evidence: `predeployment-live-status.json` and
`predeployment-runtime.json` in the M3 baseline log directory.

Existing ASS restyling is called only by `refresh_ass`, whose CLI orchestrator
`_refresh_ass_exports` scans the full library and reports refreshed/skipped, rather
than claiming normal/recovery jobs. It is not executed in this recovery scope and
is not counted as new model processing or formal delivery. No restyling change or
full-library refresh is required to deploy the normal Queue repair candidate.
Provider-bound standalone cache refresh still fails closed without pipeline
authority; do not claim that optional command has M3 acceptance.

Host scheduler discovery confirms `/usr/local/sbin/update_cron` and existing
Dynamix/User Scripts. The actual script builds root cron from plugin `.cron`
files, including `/boot/config/plugins/dynamix/*.cron`. Evidence is retained in
`host-scheduler-entry.txt` and `host-update-cron-entry.txt`. No scheduler installed
yet; use this existing persistence mechanism during controlled deployment, with
an owned, pinned observer script and rollback evidence, not a parallel daemon.

## Targeted model repair merge lineage (candidate)

Safe-omission and CPS repair already use the durable Translator commit for their
temporary, partial SRT. The missing boundary was the subsequent merge into the
full cache. Both Worker paths now capture original lineage before requesting repair
and record a two-parent preparation before replacing any cache.

`record_model_output_merge` requires known original/repair preparations, matching
job and runtime/provider identity, exact original/repair byte hashes, unchanged
replacement cue timings and an exact reconstruction of the final SRT. Unselected
cues cannot change. The repair request-event watermark must still match, including
on replay. The child retains both parent tokens and replaced indexes and remains
`publication_verified:false`; existing quality holds/QC and the post-preparation
provider publication barrier still apply. No old preparation is rewritten.

Server related suites: 211 PASS in
`logs/m3-baseline-20260908T070237Z/provider-targeted-merge-lineage-server.log`.
Tests cover missing/changed ancestors, changed unselected content, wrong target,
replay, later inference refusal and the Worker helper with real SQLite. Existing
Worker repair regressions remain passing. This is isolated proof, not a real model
translation or formal delivery; existing ASS restyling authority and deployment
scheduler/runtime acceptance remain outstanding. Production changes/additions: 0.

## Deterministic prepublication repair lineage (candidate)

AI publication now captures exact immutable parent lineage before deterministic
SRT remediation. If zh-CN bytes change, the existing Pipeline event store records
a child `MODEL_OUTPUT_PREPARED`, retaining the parent token, runtime/provider,
request watermark and full repair evidence plus its hash. The supplied diagnostic
chain must connect the parent's recorded content hash through every repair to the
candidate hash, identify applied rules, and end with successful QC recheck.
Unknown parents, different paths, broken hash chains, failed QC or intervening
inference requests are refused. No historical preparation record is rewritten.

The child is still `publication_verified:false` and needs a newer provider
observation through the existing publication barrier. Worker compares actual
output bytes; the state primitive alone is not a filesystem or QC attestation.
This covers deterministic prepublication remediation, not model-based targeted
retranslation or existing formal ASS restyling, which remain open.

Server related suites: 210 PASS in
`logs/m3-baseline-20260908T070237Z/provider-deterministic-lineage-server.log`.
SQLite tests cover parent retention, exact-chain rejection, new-connection replay,
immutability and inference-history change. Worker integration verifies derivation
is invoked before confirmation/replacement and that confirmation refusal restores
the cache while preserving source and prior formal outputs. Production deployment
and formal subtitle additions: none.

## Publication wait recovery classification (candidate)

The exact Worker failure `model_output_provider_confirmation_pending` now maps
to existing `transient_timeout / same_pipeline` admission recovery, including
legacy failure-code ingestion. The reproduced prior mapping was `worker_unknown /
bounded_retry`; this was ambiguous evidence, not proof that every consumer would
permanently fail it (some consumers also consider retry strategy).

Queue-result integration verifies the existing configured attempt limit and
cooldown remain in force, without COMPLETED or review transitions. Provider drift,
unresolved requests, unreadable observations and similar-but-not-exact messages
are not reclassified as ordinary waiting. No latch, ownership or Gate is cleared.
Local related suites: 227 PASS. Server Worker/Queue/SQLite/recovery suites:
375 PASS, `logs/m3-baseline-20260908T070237Z/provider-publication-retry-server.log`.
This closes the waiting-classification item below; repair lineage, restyling,
scheduler installation and real safe deployment acceptance remain outstanding.

## AI publication provider barrier (candidate)

The AI ASS path now consumes `confirm_model_output_provider` after staged QC and
before formal replacement. It waits at most 45 seconds for a host observation
strictly newer than SRT preparation, reading only the observation file at bounded
two-second intervals. Only `model_output_provider_confirmation_pending` is waited
on; malformed, UNPROVEN, drifted or otherwise invalid evidence fails immediately.
It pins the runtime state and rechecks actual SRT lineage after waiting. Worker
does not refresh observations, arm protection or modify the Gate.

Direct extracted-source publication is unchanged and does not assert model
provenance. This is not full M3 acceptance: repair-derived lineage, existing-AI
restyling authority, and recoverable classification of observation-wait failures
still need completion before deployment. A changed SRT without exact preparation
lineage currently fails closed rather than being relabeled as verified.

Real SQLite/Worker fixture checks cover bounded timeout, newer-observation success,
immutable replay and immediate UNPROVEN refusal. Publication integration checks
verify rejection before replacement with source, checkpoint and existing outputs
unchanged. Server suite: **208 PASS**, `provider-publication-barrier-final-server.log`
under `logs/m3-baseline-20260908T070237Z/`. Initial failed run is retained separately
in `provider-publication-barrier-server.log` (test module typo and missing fixture
directory, both corrected). No Production deployment or new subtitle delivery.

## Worker complete-cache admission (candidate)

`_validate_translation_cache_chain` now invokes a read-only exact lineage check
before any existing zh-CN cache can be reused or invalidated. For provider-bound
baselines it requires verified runtime state, the exact source identity's formal
job, actual SRT content/path hashes, and matching runtime/provider execution
identity. It uses the same identity function as request/checkpoint creation.

An unproven cache raises the existing `SourceSelectionReviewError` and remains
on disk. Existing stage mapping records `source_selection_needs_review`, which
the recovery taxonomy classifies as QUALITY_BLOCKED, not a generic system crash.
No new queue or source mutation is involved. Explicit legacy baselines without a
provider binding retain existing behavior, without acquiring M3 provenance.

Composed Worker/SQLite tests verify valid lookup, missing/changed-provider rejection,
cache/source preservation and the existing review classification. Server related
suite: 207 PASS (`provider-worker-cache-guard-server.log`); final review integration
is in `provider-worker-cache-review-final-server.log`. Publication waiting and
confirmation consumption, plus repair-derived lineage, remain open. This is not
a Production deployment or complete output-publication acceptance.

## Post-preparation provider confirmation primitive (candidate)

`confirm_model_output_provider` loads immutable preparation evidence, requires a
fresh matching host observation strictly later than preparation, checks the exact
Gate baseline, and appends an immutable provider-confirmation event. It rejects
changed inference history and unresolved inference ownership, including on replay.
Confirmation is replayable across restart but remains `publication_verified:false`:
the caller still must verify actual bytes, QC, source and atomic publication.

Preparation itself now cross-checks stage request runtime/provider identities and
rejects unresolved inference, rather than trusting a supplied context alone.
Inference watermarks deliberately exclude UNLOAD resource-cleanup requests.
Tests cover old/equal/future observations, wrong provider/Gate, UNPROVEN evidence,
restart replay, immutable confirmation and subsequent-request invalidation.
Local related suite: 47 PASS. Server related suite: 206 PASS. Evidence:
`provider-output-confirmation-server.log` under the M3 evidence root.

Worker cache/publication consumption is still not wired. This primitive is not
an end-to-end publication barrier, and no production deployment is authorized by
these component results alone.

## Complete SRT preparation lineage (candidate)

Translator's existing guarded SRT commit now records `MODEL_OUTPUT_PREPARED` in
the existing Pipeline event store before writing a provider-bound SRT cache. The
record binds job/stage, destination digest, planned content digest, execution
identity, provider binding and the model-request event watermark. It explicitly
records `publication_verified: false`; preparation does not settle the job or
prove output QC. Legacy/unbound output commits do not acquire retroactive proof.

The append-only record is transactionally durable and replayable. A subsequent
model request changes the watermark even if SRT bytes are identical, so it cannot
reuse an older preparation time. Exact per-job lookup rejects wrong output bytes,
path/execution identity or job. Composed Translator tests compare the planned hash
to the actual written SRT, verify restart lookup and immutable events, and retain
source content and RUNNING stage status.

Server related suite: 204 PASS (`provider-srt-lineage-server.log`); final metadata
and restart tests are in `provider-srt-lineage-final-server.log` under the M3
evidence root. This is the lineage producer/lookup, not yet Worker cache-admission
or publication enforcement. The latter must also wait for provider evidence newer
than preparation and persist that confirmation before claiming formal acceptance.
No Production deployment, source/output modification or Gate change occurred.

## Provider-bound translation checkpoint identity (candidate)

Publication review found that batch checkpoint signatures previously contained
configured model names but not the executing provider generation. A later fresh
provider observation cannot validate older restored batches by itself.

Provider-bound request contexts now derive a checkpoint identity from validated
provider binding and runtime code/configuration digest. Translator passes it into
the existing checkpoint signature. Same execution identity resumes at the next
batch; changed runtime/provider identity refuses the old batches, even when the
configured model name is unchanged. A rejected signature read does not delete or
rewrite the old checkpoint. Legacy/unbound signatures retain their original
format, rather than retroactively claiming provider provenance.

Composed Translator/file-checkpoint tests verify same-provider resume versus
changed-provider full batch processing; context tests distinguish runtime/provider
changes and reject wrong endpoint evidence. Local related suite: 40 PASS. Server
related suite: 184 PASS, `provider-checkpoint-binding-server.log` under the M3 log root.
Final SRT-cache lineage and post-response publication confirmation are still open;
this increment is not a complete publication barrier or deployment acceptance.

## UNRAID host scheduler adapter (candidate, not installed)

`m3-provider-observer.sh` uses host Bash/Docker and the Worker Python CLI, matching
the existing UNRAID deployment environment without requiring host Python or a
Docker socket mount. `--once` performs one bounded refresh;
`--scheduled-minute` performs two bounded refreshes with a 20-second interval.
Individual Docker calls have deadlines. A host `flock` prevents overlapping
scheduler invocations, while the existing runtime writer lock protects Gate
initialization/publication. Busy ownership is not removed or preempted.

The adapter sends a size-bounded context/two-inspections/host-addresses/start-time
envelope to `provider-refresh-inspected`; that entry revalidates context, direct
endpoint, stable provider identity and timestamp before using the same publisher.
It contains no Queue scan, restart, arm, recovery or self-install command.

Server isolated adapter/runtime suite: 36 PASS,
`provider-host-scheduler-adapter-server.log`. Tests execute the real Bash script
with a disposable Docker command fixture and verify timeout/upstream failure,
invalid mode, scheduler lock and correct stdin envelope; they are not installed
Production scheduler proof. The final two-refresh scheduling assertion is in
`provider-host-scheduler-final-server.log` under the same M3 evidence root.
Installation/reboot persistence verification and post-response publication
confirmation remain open; do not deploy this candidate before those contracts
are complete.

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
