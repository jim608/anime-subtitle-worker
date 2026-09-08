# Incremental Delivery Plan

## Latest incident: line-repair evidence boundary (2026-09-08)

- Production is still Worker `6c925858703bce48f9e5763ac48eddab2f3e441d`;
  mapping fix `42c48f68014effc9eb0a7c1790870c1ab9ec11ce` has not been deployed.
- A distinct `incorrect_completion` trip at 1788816496.9895241 blocks admission.
  Line repair retained a previous failed provenance and attempted strict completion
  before closing its quality review. Accepted ASR diagnostics and source hash are intact.
- Candidate archives the prior provenance, requires historical source/decision/transcript
  evidence before reuse, records a fresh run, and resolves only the exact quality review
  after every other strict check. The final strict validator is unchanged.
- Server candidate: 253 targeted/shared tests and actual isolated container restart PASS,
  `/logs/m2-line-repair-isolated-20260908T030048462657Z/`.
  Restart fixture stubs the model/publisher; it proves recovery safety, not subtitle delivery.
- Frozen current-state replay passes all 11 strict conditions with Production artifacts
  mounted read-only; `/logs/m2-line-repair-replay-20260908T025520099117Z/`.
- Final candidate source-reuse and noncohort claim binding also PASS; final 258 tests
  and restart evidence `/logs/m2-line-repair-isolated-20260908T030844473143Z/`.
- Still required: safe deployment, actual-image proof,
  new exact-incident reconciliation preserving all existing holds, and real subsequent claim.
  No new production subtitle delivery or new Gate is claimed by these tests.

## Download preparation repair candidate (2026-09-07 18:46 UTC)

- Actual cached-only preparation profile: 44.909s, 340,464 `Path.resolve` calls,
  exceeding the 30s source-discovery slice before useful source search.
- Candidate uses operation-scoped resolved path keys, refreshed next operation;
  matching, manual protection, season rules and failure budgets are unchanged.
- Frozen real-cache replay: 465 ordered mappings exactly identical (SHA256
  `75c8908472982ff26a8fc921c74c204c15986aea315abe47f806bc35b4662028`),
  old image 28.824s vs candidate 0.345s. No network or Production writes.
- Evidence `/logs/m2-mapping-preparation-parity-20260907T184635249598Z/`.
- Local 261 relevant tests plus one added identity-refresh/fallback test PASS.
  Candidate not yet deployed; next actual runtime change requires existing safe
  planned-change/reconciliation handoff, preserving all 64 current holds.

## Current recovery boundary (2026-09-07 18:30 UTC)

- ASR evidence repair deployed: `6c925858703bce48f9e5763ac48eddab2f3e441d`; safe deployment `20260907T135509Z-2106168` completed with EXIT=0 and no Production DB rollback.
- Formal controlled recovery `m2breakerrec_6d49295784be4c6cac661410efe09d73` returned ARMED; new Gate `m2-gate-20260907T183058455437Z-0bb33976e8`, initialized 0/20. No old member results transferred and no Gate pass claimed.
- Reconciliation `m2-recon-asr-evidence-20260907`: 63 original holds preserved plus one diagnostic-loss incident = 64. 7,122 current queued identities in recoverable scope; 6,882 other states retained. These are not completed/delivered counts.
- Three subsequent actual normal claims observed; the third produced a new valid SUBTITLE_DETECTION checkpoint before policy review. No held claim; 64/64 real publication guards reject held paths.
- Download E1 still has no actual torrent/extraction/import. One bounded cached-index preparation profile is in progress to diagnose repeated source-discovery deadline exhaustion. Full Goal remains incomplete.

## Superseded incident diagnosis (2026-09-07 before recovery)

- Actual d508 runtime subsequently TRIPPED on incorrect_completion; no reset or further deployment performed.
- Confirmed cause: `_postprocess_ja_srt` merged one short fragment and deleted the previously accepted ASR diagnostics. Missing hash-bound hallucination evidence then caused strict completion rejection. The earlier stage-history hypothesis was not the original cause.
- Candidate fix preserves an immutable pre-transform pair, revalidates using existing quality rules, commits bound diagnostics and restores the pair after interruption. Targeted tests and actual isolated-container restart passed; not yet deployed.
- Controlled reconciliation extension is restricted to this exact incident, requires persistent source hold and actual-image regression proof, and preserves the prior receipt/Gate. No generic breaker override.
- Source E1 still lacks actual torrent/extraction evidence; Goal remains incomplete.
- Preserve current Gate/63 holds, output artifacts and checkpoints. See latest observation incident and work/M2_UNBLOCK_HANDOFF.md; earlier ARMED closeout is historical.

## External-sidecar discovery repair (2026-09-06; deployed, bounded source acceptance ongoing)

- [x] Real source metadata reproduces rejection of MP4 releases explicitly carrying Chinese external subtitles.
- [x] Minimal discovery-hint repair; no QC, parser, model, source identity or publication policy relaxation.
- [x] Local48 source/matcher/fairness tests; server-isolated305 shared-boundary tests before the final extra negative regression.
- [x] Real primary-source replay selects new hashes for No-Rin E1/E2/E3 without resetting failed/seen; E8 remains excluded.
- [x] Safe controlled runtime handoff preserving60 existing source holds and original evidence;3 new Queue identity changes additionally held.
- [ ] Real representative download/extraction/QC/publication or bounded explicit source failure.

Bounded post-deploy source check: No-Rin E1 request persisted, qB connection-reset
retry recorded, but no actual torrent/extraction claim after the retry timestamp
was observed. Counts remain0/0/0; end-to-end download acceptance is outstanding.
Do not mark it complete based on request acceptance. Normal AI claim/checkpoint
is verified separately; do not wait for full AI/backlog/Gate completion here.

Actual Worker d508b599291978cc5f6ab786c560823ea755e249; WebUI175a02a7bad46e0b6fa2372c59f39e8dd272911e.
Safe-update1874/231 PASS, actual-image94 PASS, fresh breakers7/7 PASS.
Reconciliation m2-recon-sidecar-20260906:63 holds,7139 recoverable Queue identities,6863 other retained states.
ARMED Gate m2-gate-20260906T224005061938Z-5b93117971 initialized0/20 with no backfill.
Real unheld normal Queue claim, source-decision checkpoint and ASR verified; no new subtitle counted for that claim.
Source metadata fixtures/replay: `/logs/m2-recovery-unblock-20260905T064508843990Z/external-sidecar-isolated-20260906T173812862585Z.json`.
The prior5 recorded completions remain unresolved:2 identity ambiguities and3 unique targets without validated official TC;
all5 source torrents absent in the bounded exact query. This is not proof of permanent source unavailability.

## Authorized reconciliation recovery (2026-09-06; production closeout)

- [x] Preserve failed original handoff; receive explicit authority for a new boundary.
- [x] Implement persistent source holds and controlled reconciliation mode in existing recovery mechanisms.
- [x] Server-isolated targeted/shared-boundary suite:405 PASS, including restart and fail-closed paths.
- [x] Safe deployment and complete actual runtime attestation: Worker b92a61fe086bb0cb48aef46f9b9efd967d33d969; 1,871/229 safe-update and78 actual-image tests PASS.
- [x] Persist27 UNKNOWN revisions,24 removed obligations and9 unproven arrivals (60 held); classify473 supported new scanner arrivals; preserve7,191 recoverable identities and6,807 other states.
- [x] Seal m2-recon-20260906-b92a61f, retaining old receipt/Gate, then fresh actual-image breakers7/7 PASS.
- [x] Controlled recovery; ARMED Gate m2-gate-20260906T115457465856Z-a0ccafa2ff initialized0/20; protected admission resumed.
- [x] Real normal/recovery claims, stage/heartbeat/digest-valid checkpoint and automatic next claim verified.
- [x] Final production evidence recorded below; synchronize documents without another deployment.

One autonomous newly published Traditional Chinese subtitle set has final-path
hash/parse/role-correct QC and source checksum verification, with final COMPLETED.
Five additional new manifest records remain unverified by this closeout. Download
chain acceptance and remaining quality/matching failures are not implied solved.
Original Gate's four members remain unchanged;60 source holds await new evidence.
Full evidence: `/logs/m2-recovery-unblock-20260905T064508843990Z/authorized-reconciliation-b92a61f/`.
See `docs/M2_PRODUCTION_OBSERVATION.md` for exact record IDs and proof paths.

No M3, no QC relaxation, no cohort substitution, no claim counted as subtitle delivery.

## M2 Recovery bounded closeout update (2026-09-05)

- [x] Actually deploy `60d6b2361a54a76730c5a943dfd3fac8b98cca19` using safe-update;
  verify 1,821 Worker / 229 WebUI tests, 73 running-image tests and fresh 7/7 FI.
- [x] Complete event-bound recovery, preserve old invalidated cohort, arm
  `m2-gate-20260905T084608410661Z-d618a17882` at initial 0/20 and resume safely.
- [x] Prove prior recovery safely reviewed, then distinct next automatic claim
  at 09:00:41 UTC, actual stage/heartbeat/checkpoint (not queued-only evidence).
- [x] Observe existing Amaburi partial download complete and normal extraction
  run; unchanged hard QC rejects unusable subtitles with bounded replacement.
- [x] Preserve source/prior sidecars; separately report 0 new formal subtitles.
- [x] Resolve 83 trustworthy local target bindings; retain explicit 788 evidence
  gaps and separate 356 source-backoff obligations without mass fallback.
- [ ] Deploy the separately tested malformed-SRT candidate boundary correction
  only after an evidence-bound normal ARMED runtime/Gate handoff is available.
- [ ] Obtain successful usable extraction/formal output or actual selected JA
  fallback completion; diagnostics and reviewed claims do not prove delivery.
- [ ] Complete frozen 20-job observation server-side (not awaited this session).

Evidence: `docs/M2_PRODUCTION_OBSERVATION.md` and server
`/logs/m2-recovery-unblock-20260905T064508843990Z/`. M3 remains out of scope.

## M0 — Baseline and low-I/O ingestion

- [x] Audit current Watcher, task creation, stages, temporary/final outputs, error paths, Docker and configuration.
- [x] Record focused regression baseline.
- [x] Identify recursive disk-I/O sources.
- [x] Make filesystem events the normal ingestion authority.
- [x] Persist discovery/stability observations across restart.
- [x] Reject temporary/incomplete files and require a write-complete gate.
- [x] Keep startup reconciliation and low-frequency bounded fallback only.
- [x] Stop idle AI and Mikan loops from repeatedly walking the whole library.

## M1 — Durable resumable pipeline state

- [x] Add stable Job/media revision identity to the existing WAL SQLite store.
- [x] Enforce the documented states and auditable transitions.
- [x] Persist per-stage attempts, inputs, outputs, model, timeout, retry and checkpoint.
- [x] Recover interrupted work from the last valid checkpoint.
- [x] Preserve existing queue/WebUI and subtitle pipeline compatibility.
- [x] Keep source video immutable and publish verified staged outputs atomically.
- [x] Pass all M1 acceptance and focused regression tests.
- [x] Complete architecture, design, state-machine, recovery and test-result documents.

M0/M1 implementation and acceptance are complete locally. These checkboxes do not claim an UNRAID deployment, the 100-input release gate, the rolling-500 Production SLO, or a measured 99%/99.9% autonomy rate.

M2 translation quality, Translation Memory, full QC redesign and WebUI work are explicitly out of scope until every M0/M1 checkbox is complete.

## M2 — Deterministic source analysis and durable decisions

- [ ] Finish the reproduced M2 recovery blockers: bounded cold/source scheduling,
  evidence-backed season scopes, full official-subtitle admission and staged hard
  QC. Complete combined server tests, safe deployment and guarded runtime handoff.
- [ ] Deploy the additional reproduced scanner writer-transaction and generic
  failure-collision fixes; retain typed source-review classification and recover
  only the exact 07:15 UTC incident with immutable evidence. The combined
  server-isolated candidate suite passed 744 tests; runtime is not yet updated.
- [ ] Complete the narrowly evidenced deployment-drift recovery handoff. First
  ea0baaf deployment passed 1,813 Worker / 229 WebUI tests, actual-image 65 tests
  and 7/7 FI, but recovery refused the separate expected runtime-change trip.
  Keep its old Gate invalidated and all receipts; arm only after final repair.
- [ ] Complete bounded real-case closeout with separate download/extraction/formal
  publication and AI route/completion evidence. Candidate mapping proves 83 local
  bindings; Amaburi E12 currently fails hard QC and is not a new subtitle success.

- [x] Perform bounded acceptance on the exact four unsuccessful and five old-completed download obligations without redeployment/Gate recreation: reverify four existing formal completions, retain one 6-versus-6.5 identity review, and preserve four failed-source/insufficient-subtitle cases without a false new-publication count.
- [x] Prove two subsequent M2 recovery claims, SUBTITLE_DETECTION execution/heartbeats, safe local disposition and continued automatic claim; preserve the actual database-contention retry and source-quality review evidence separately from success.
- [x] Freeze and document routing for the existing 1,227 blocked keys: 356 source-backoff and 871 unresolved identity/mapping/index records; zero newly proven ready-for-alternative or AI-fallback obligations in that blocked subset. Keep the separate 887 historical replacement targets and all recovery metrics outside Gate backfill.
- [ ] Prove a different subsequent download/extraction claim and genuinely new formal subtitle publication: current source-enqueue cycle is still processing, and the four bounded missing-valid-subtitle cases have no permitted untried source. Preserve normal waiting, failed hashes, QC and existing formal outputs.

- [x] Audit download/extraction/import history separately from AI recovery; verify qB authentication and actual mount parity with bounded server evidence.
- [x] Repair four-digit episode parsing, partial queuedDL timeout handling, piece preservation, pre-publication subtitle validation and durable bounded replacement/backoff; pass 283 server candidate tests.
- [x] Safely deploy the M2 download extension and recovery closeout as Worker `b911794ed0ec872cb475f714e1385e20e8ac4388`; pass 1,737 Worker and 229 WebUI tests, fresh 7/7 breakers, and attest ARMED without changing QC/models/configuration.
- [x] Apply all 2,120 deduplicated historical decisions using existing state/locks; enqueue 887 bounded replacement targets, preserve one existing download, and record real download/extraction/matching/isolated-import PASS with safe Production no-op, not a false new-import claim.
- [x] Repair the controlled DISARMED Gate-publication race and orphan pre-claim canary reconciliation; preserve strict invalidation, single-canary limits, retry budgets and local-failure isolation. Pass 74 focused integration tests.
- [x] Preserve superseded Gates and initialize `m2-gate-20260905T045640981079Z-08147de925` at `2026-09-05T04:56:40.981079Z`, baseline `m2-guardrail-v1:5b4d2a88f2d5c0c5749f6747`, initial `0/20`; resume claims without waiting for the Gate.
- [x] Record bounded server continuation evidence: 12 download targets consumed, eight existing outputs verified, and an independently dispatched next AI canary after one pre-claim exclusion; retain its queued/not-yet-claimed boundary.
- [ ] Verify a genuinely new Production subtitle import when an eligible external source is available; the representative target already had valid outputs, and missing-output sample 304:10 had no eligible untried source. Do not redownload failed hashes or relax validation to close this item.

- [x] Inventory supported subtitle sidecars, embedded subtitle streams and audio streams without modifying source media.
- [x] Normalize language metadata and combine it with subtitle content, script and quality evidence.
- [x] Score completeness, timing, empty-content, forced, signs-only, songs-only and commentary risks.
- [x] Apply deterministic candidate ordering and all seven formal M2 strategies.
- [x] Persist immutable Decision Records with candidates, scores, reason codes, evidence, versions and source context.
- [x] Bind Decisions to verified Stage checkpoints with idempotent restart reuse and exact-context invalidation.
- [x] Revalidate selected subtitle/audio sources before routing them into the existing worker.
- [x] Materialize embedded subtitles through validated temporary files and atomic cache publication.
- [x] Keep Whisper and other speech models outside the M2 decision stage.
- [x] Pass the dedicated 22-case M2 acceptance suite locally, including the nested M1 twelve-case gate and 106-case M2 integration gate.
- [x] Cover all ten required representative source-selection fixture classes.
- [x] Record focused local results and evidence boundaries in `docs/TEST_RESULTS_M2.md`.
- [x] Run the complete existing repository regression suite against the combined M2 diff: 1,649 tests run, OK with one conditional skip.
- [x] Complete the M2 design, source-selection, Decision-schema and confidence-policy documents.
- [x] Run final changed-file compile, diff checks and release review with no private deployment details in tracked files.
- [x] Verify the deployed Worker/WebUI runtime baseline and loaded guardrail configuration, pass all seven isolated breaker cases in the server image, and arm `M2_GUARDRAILS_ARMED`.
- [x] Implement exact preservation/import for the former observation baseline `m2-guardrail-v1:276fdef781528ba2059c114e`, started at `2026-09-04T11:00:12.736033Z`, with required final status `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`; keep its eight pre-gate attempts as supplemental history only.
- [x] Implement the frozen first-20 claim cohort with claim-time `BEGIN IMMEDIATE`, no success backfill, immutable ordinals, stable-job uniqueness, WAL persistence, and restart-safe continuation.
- [x] Persist the complete required terminal record for every frozen member, retain immutable per-attempt incident events, and journal one summary/outbox record only after all 20 members are terminal.
- [x] Durably invalidate an active gate as `INVALIDATED_BY_RUNTIME_CHANGE`, with expected/actual evidence and no mixed-baseline admission.
- [x] Add the atomic observation schema-v2 migration, including container-instance identity and canonical result-event payload/digest verification, while preserving version-1 Gate history.
- [x] Pass the focused repository candidate suites: 24 frozen-cohort/schema/event-journal cases, 11 recovery/replay cases, and the isolated 7-breaker harness.
- [x] Isolate the live `repeated_identical_stage_failure` cause to three distinct `source_selection_needs_review` outcomes that the pipeline correctly held in `NEEDS_REVIEW` but the legacy queue adapter mislabeled as retryable failures.
- [x] Add the six-way failure taxonomy, version-aware historical reconciliation, compatible-checkpoint/minimum-safe-stage decisions, bounded no-progress budget, and a restart-persistent single-canary recovery lane using indexed Job Store state only.
- [x] Make repeated OOM/identical-stage streaks distinct-job aware and replay-idempotent, while keeping three distinct systemic failures fail-closed.
- [x] Add the evidence-bound `m2_guardrail_runtime.py recover` flow: preserve trip evidence, verify runtime/queue/checkpoint/source/output identities, invalidate the old Gate, journal recovery, and require a new attested `0/20` Gate before claims resume.
- [x] Pass the 2026-09-05 recovery candidate regression: 1,720 tests run, OK with one conditional skip.
- [x] Make controlled recovery restart-safe by durably pausing claims before Gate initialization, validating a pending recovery across deployment, and resuming claims only after an `ARMED` matching baseline; pass the complete 1,721-test regression with one conditional skip.
- [x] While claims were paused, safely deploy the repaired Worker, preserve the exact former-Gate invalidation, run a fresh seven-breaker suite in the new server image, and verify the live runtime is `ARMED`.
- [x] Create deployment-bound Gate `m2-gate-20260905T020845085531Z-7d0c5c7333` with baseline `m2-guardrail-v1:0180b8779ee97524bf0150d2` at `2026-09-05T02:08:45.085531Z` and `0/20`, then resume normal claims and dispatch only one recovery canary without waiting for either workflow to finish.

The former `0/20` observation baseline is not a valid Gate and has the final
status `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`. Its eight pre-gate
attempts remain supplemental records and cannot be enrolled retroactively.
The replacement frozen-cohort implementation is deployed at Worker runtime
`d9dfcd01aa9ebeffe65c8367f4e1bbace56d5bcc`; the fresh server-image breaker
suite passed 7/7 and the bounded Production recovery reported no source-media
or formal-output change. The new frozen Gate is active at `0/20`, but this plan
does not claim any member result or wait for it. M2 is not
`M2_PRODUCTION_ACCEPTED`; the 20-job Gate, 100-input release gate, rolling-500
Production SLO, recovery-canary result, and any measured 99%/99.9% autonomy
claim remain unverified.

Translation-quality improvements, Translation Memory, full QC redesign, WebUI redesign and M3 model-fallback work remain out of scope for this milestone.
