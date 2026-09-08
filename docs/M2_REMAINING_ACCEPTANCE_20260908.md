# M2 remaining acceptance — 2026-09-08

## Diagnostic deployment closeout — 2026-09-08 11:47 UTC

Worker runtime **3f800c8e5cd9a20110f14e8631dc8d380b988f02** is deployed,
WebUI175a02a7bad46e0b6fa2372c59f39e8dd272911e unchanged.
Image sha256:3359f9bf6577969310e8512e3763b5cd2cc3f4328f932df95a5c666e9bc4a179.
Safe deployment20260908T113952Z-993033 exit0, rollback backup retained.
Owned drain first refused non-idle; work naturally finished, no kill/state clearing.
Source/test verification and actual-image216 tests passed; fresh isolated breakers7/7.
Formal recovery exit0, ARMED, admission resumed. Reconciliation
m3-recon-sqlite-diagnostic-20260908T1137, receipt SHA
739a126da84abbb625fdebab6fdf8ea2592ee7da316a03a1fcf237ff2f98a902:
67 holds retained, zero new pending differences, 7087 recoverable and6942
other-state identities retained. Preservation remains UNPROVEN.
Original receipt SHA06c38c52e2196ae3002103cb7e64a52b41b0e08882b78d7949f0f13424bbd42d.
Old Gate archived under existing runtime-change policy, never relabeled PASS.
New Gate m2-gate-20260908T114539007821Z-7a4995553b,
start2026-09-08T11:45:39.007821Z, baseline
m2-guardrail-v1:0e6946e11adbf1bc0d532584, initialized0/20.
One bounded post-recovery sample proves first frozen claim at1788867976.2228823,
then NEEDS_REVIEW/strict0; not replaceable. Pipeline jobs da5eb7f40f3a4cf99095195e4ae295d7
and9d50d991ce72490391f32388a370b6c2 automatically started source analysis,
each retained a hash-valid checkpoint before review. No operator Retry.
New formal delivery0; M2/M3 acceptance still incomplete.
Bounded post-deploy lock excerpt contained only PRE-deployment failures through
11:37:43. It does not prove the defect is fixed or provide a new stack.
Next use the new original-error traceback when contention recurs; do not force
jobs, poll the Queue, or treat absence in this short sample as resolution.
Evidence root /logs/m3-sqlite-diagnostic-20260908T1137/:
safe-deploy.log/deploy.exit, actual-runtime-before-recovery.json,
actual-image-validation.log, attestation.json, recovery-closeout.json,
post-recovery-probe.json, post-deploy-lock-trace.log.
Fresh FI /logs/m2-guardrail-fi-20260908T114531868344Z-cd32368d/result.json.


11:35 UTC: pending diagnostic-only logger traceback patch, 5 isolated server
tests PASS. Not deployed; SQLite lock owner remains unproven. No change to Gate,
formal output count, source holds or M2 acceptance. See M3_MODEL_RECOVERY.md.

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


## Current M3 handoff — 2026-09-08 11:10 UTC

Worker 5f379e877ce59cb3207927e38aad2f6735b76287 / WebUI
175a02a7bad46e0b6fa2372c59f39e8dd272911e deployed successfully; ARMED, 7/7 fresh
isolated breakers, 67 source holds retained, preservation UNPROVEN. Existing
provider observer now runs through persistent host cron. New Gate
m2-gate-20260908T110433538511Z-b43d836495 starts 11:04:33.538511Z on baseline
m2-guardrail-v1:e365b1132703b8cd1ae7e063, initialized 0/20; no old results transferred.
First fixed member was actually claimed and reached source review with a hash-bound
checkpoint; next job began ASR. Review member is not replaced. No new formal subtitle
added, no download/extraction acceptance claim, no M2 acceptance or percentage SLO.
Evidence: `/logs/m3-runtime-handoff-20260908/`, especially `runtime-post-wake.json`.
Detailed successful and refused handoff boundaries are in docs/M3_MODEL_RECOVERY.md.

M3 full candidate server regression: 2023 PASS, exit 0, retained at
`logs/m3-baseline-20260908T070237Z/candidate-full-predeploy-final.log`.
Only the host-observer test fixture's executable tempdir changed. No Production
deployment, Gate change, source hold release or formal subtitle addition.

2026-09-08 10:36 UTC: runtime ARMED/baseline match at Worker a4829a5 / WebUI
175a02a, original Gate retained. Active transcription with fresh heartbeat observed
once via lite status; no deployment or interruption. Evidence under
`logs/m3-baseline-20260908T070237Z/predeployment-runtime.json` and
`predeployment-live-status.json`. Runtime Gate metadata is not live cohort progress.

M3 targeted repair-merge lineage candidate: 211 server related tests PASS in
`logs/m3-baseline-20260908T070237Z/provider-targeted-merge-lineage-server.log`.
No Production deployment, Gate change or formal subtitle added. Not M2
download/extraction or strict frozen-cohort acceptance.

M3 deterministic repair-lineage candidate: 210 server related tests PASS in
`logs/m3-baseline-20260908T070237Z/provider-deterministic-lineage-server.log`.
No Production deployment, formal subtitle addition or Gate change; not M2
download/extraction acceptance evidence.

M3 publication-wait classification candidate: 375 server related tests PASS in
`logs/m3-baseline-20260908T070237Z/provider-publication-retry-server.log`.
Existing attempt limit/backoff retained; no Production deployment, Gate change or
formal subtitle added. M2 acceptance remains incomplete.

M3 AI publication barrier candidate: 208 server isolated tests PASS in
`logs/m3-baseline-20260908T070237Z/provider-publication-barrier-final-server.log`.
No Production deployment, Gate change, or additional formal subtitle; M2 remains
incomplete. These tests are not download/extraction acceptance evidence.

## M3 isolation work boundary (2026-09-08 08:10 UTC)

Subsequent M3 work added isolated provider restart proof, a not-installed host
observation adapter, provider-bound batch checkpoints and complete-SRT preparation
lineage. These remain candidate evidence, not Production deployment or formal
subtitle delivery. M2 download/extraction and strict Gate acceptance are unchanged
and incomplete; no fixture or preparation event is counted toward them.

Subsequent provider-binding candidate tests also remain isolated: 258 related
and 77 final targeted server tests passed (overlapping suites). Read-only provider
identity capture did not arm/recover/redeploy production or recreate its Gate.
These results add no M2 formal subtitles and do not satisfy M2 acceptance.

The explicitly authorized M3 candidate now has isolated model ownership,
fallback/resource and real process/container restart evidence; see
`docs/M3_MODEL_RECOVERY.md`. None of these fixtures count toward M2 formal AI,
download/extraction delivery or the frozen cohort. The restart targeted only a
dedicated fixture container, not Production Worker/Ollama. No production
deployment, configuration edit, Gate reconstruction or source-hold release was
performed in this increment. The historical runtime snapshot below is not a
fresh Gate-progress poll. M2 remains incomplete.

## Current closeout: deployed and recovered (2026-09-08 06:28 UTC)

This section supersedes the earlier deployment deferral below. **M2 remains incomplete**:
download/extraction formal publication and the new strict Gate are not accepted.

### Actual deployment and repair verification

- Runtime Worker: `a4829a5298304f9e2dc20dd02ecde883ea53ec23`, containing repair
  `de6759499eaa0576dc9b9affd2f842109781d4c5`; WebUI unchanged:
  `175a02a7bad46e0b6fa2372c59f39e8dd272911e`.
- Worker image: `sha256:4497656bc63ada24a4f76d721c81af07a8150d037382314be97c180558ba3849`.
  Source revision: `1b9c2038283362c7102f25ce24f17eb08a02e3dfe76bbc58dd95a7e699f2e37f`.
- Safe deployment `20260908T062131Z-1253513`: completed, exit 0. Backup and
  SHA256 manifest retained under `/work/deployment_backups/20260908T062131Z-1253513`.
  No process force-stop was needed; real work drained through existing policy.
- Existing updater required Worker **1929 PASS**, WebUI **231 PASS**. New actual
  image: **312 related tests PASS** and isolated container crash/restart PASS.
  This includes the final-fragment rejection/checkpoint/consumed-budget regression
  using the deployed code, not the earlier candidate mount. Real production
  recurrence of this particular ASR rejection has not yet been observed.
- Actual-image evidence: `/logs/m2-asr-postprocess-isolated-20260908T062524107986Z/`.
  Fresh breakers **7/7 PASS**, no production resources affected by injection:
  `/logs/m2-guardrail-fi-20260908T062807894290Z-c5c155cd/`.

### Reconciliation and autonomous continuation

- Reconciliation: `m2-recon-fragment-20260908T0620`;
  receipt `/logs/m2-reconciliation-m2-recon-fragment-20260908T0620.json`, SHA256
  `9ee21d2879575ae85f71458269d25b707e237a6b3303939594a526e0020215db`.
- Linked planned receipt: `/logs/m2-planned-runtime-change-fragment-20260908T0620.json`,
  SHA256 `d16fef829df8a82824ae3580ff8084d78ca4590fd2197fe0d1bbb55f81b64b88`.
  Prior reconciliation and failed preservation evidence remain linked and UNPROVEN.
- **67 holds preserved**, zero new unexplained differences; **7131 queued identities**
  reconciled for existing admission policy, **6898 other-state identities** retained.
  These counts are neither new deliveries nor a waiver of source/QC validation.
  All **1255 existing recovery attempt counters** remained unchanged.
- The host helper initially stopped because it expected CANARY_READY, while the
  official reconciler correctly retained CANARY_IN_FLIGHT. Exact evidence showed
  one DISPATCHED/queued job, no claim ID and no running attempt. Continuation used
  the persisted successful test/revalidation evidence without repeating that
  committed operation or clearing any lease. Failed helper log retained.
- Formal controlled recovery completed: **ARMED**, claims resumed, own hold released.
- Retained recovery obligation `aiobl_a2883d98ec06ab89668596e9b7fae19743f10443c21152a64ecb96f2733f76ba`
  actually claimed at epoch `1788848899.3516853`; SUBTITLE_DETECTION checkpoint
  `aa929365e9ad25e9d07468f014299de028a2109c2b36622c32efaf0c2ba9d5a9` persisted.
  It correctly entered review, not COMPLETED.
- Next obligation `aiobl_6b870417ac3278b388d347e554b1f217c03cec69bd2c120e7354267550a9560d`
  automatically claimed at `1788848908.1206245`, began ASR with heartbeat, and has
  valid source-analysis checkpoint `74a6ea52216b8a6d1a97fbb07f8defca82e95c673e25da18679ac5be0de3cae5`.
  Source checksum matches its already recorded current-run snapshot; this does
  not establish historical continuity of any held source.
- Claim evidence: `claim-evidence-1788848966983041159.json`; source evidence:
  `source-verification-1788849054322803417/result.json` under the evidence root below.
  Held claim count after the new Gate: 0; held publish guard rejected 67/67.

### Strict Gate boundary

- Prior Gate `m2-gate-20260908T042203260424Z-ea2562f0dc`: fixed 20 selected,
  17 settled (15 NEEDS_REVIEW, 2 FAILED), 3 not settled, 0 strict-verified at the
  pre-deployment snapshot. It did **not** pass. Original cohort/evidence preserved;
  the runtime change invalidated it through the formal handoff.
- New Gate: `m2-gate-20260908T062814708431Z-4a4b7a2546`;
  start `2026-09-08T06:28:14.708431Z`, initialized **0/20**;
  baseline `m2-guardrail-v1:6a39511185854d6d968c637b`.
- Config fingerprint unchanged:
  `sha256:355300b197164801be4616a688d852c1b8b5274fe91e91e40a2a21f12a3c4dbc`;
  decision schema 1. No QC/model/routing/breaker policy changes in this turn.
- First eligible 20 claims stay frozen; no backfill, mixed-version success,
  historical-download substitution or Gate-passed claim. Observation remains
  server-owned; this session does not wait for the 20-job Gate.

### Download and extraction evidence (separate from AI)

- Prior verified AI delivery remains **1**, reused as historical AI-route evidence,
  not new-runtime Gate evidence and not a download/extraction delivery.
- Original 5 old-completed cases rechecked using both historical failed hashes
  and latest completed hashes: no project torrents present. `3380:6` and `2911:10`
  retain identity review (special/episode or season ambiguity); `2565:12`,
  `3085:13`, `3932:3` have unique indexed targets but no verified TC completion.
  None was falsely completed or treated as permanently subtitle-less.
- Exact 4-case source refresh succeeded in 3.21 seconds (206 releases); current
  selection accepts E2 and E3. E1/E8 remain excluded under existing failed/seen
  evidence; no failed/seen reset. Source-service access is not the same as swarm
  availability or subtitle usability.
- Representative E2 obligation `m2dl_0e107117422ec1a044b0`: verified missing official
  TC and not held. Existing controlled replacement entry point added exactly one
  new candidate `d2d7177013feb02c3137caa594cbd537560b20f0` at
  `2026-09-08T06:28:51.051740Z`. Initial qB state stalledDL, 0 bytes; no extraction
  job or formal publication evidence yet. This is not counted as a completed download.
- Existing replacement request retains elapsed-budget yield and retry deadline;
  its last qB error is ConnectionResetError. Exact qB reads succeeded. No source
  files, valid subtitles, unrelated torrents or retry ledgers were cleared.
- All current full logs: `/logs/m2-fragment-deploy-20260908T0620/`.

### Bounded final result (2026-09-08 06:33 UTC)

- E2 reached the existing `did not start` failure path. The zero-data torrent is
  no longer present; its hash was added to the retained failed-hash evidence,
  and the target is again in the existing replacement request. No manual torrent
  removal, failed-list reset or same-hash re-add was performed by this verifier.
  No partial data were recorded. This is a swarm/download-start failure, not a
  corrupt-video or extraction-crash finding.
- Trial counts: **1 torrent added, 0 completed downloads, 0 extractions,
  0 download-derived formal subtitles**. No output exists for final QC/manifest
  acceptance; these checks are **not reached**, not PASS.
- Source checksum before/after matches; all existing subtitles preserved; no new
  sidecar exists. Evidence: `download-safety-1788849229318419708.json`.
  Terminal bounded status: `source-status-1788849148592847081.json`.
- Latest runtime ARMED, new frozen cohort **1/20**, settled **1/20**, strict
  verified **0**. Automatic observer enabled, CANARY_IN_FLIGHT retained, 67 holds.
  Evidence: `final-status-1788849229895360422.json`. No Gate completion was awaited.
- Prior AI delivery count remains 1 (old baseline, already accepted evidence),
  **newly verified formal AI delivery this turn: 0**. New-runtime claim/stage/source
  checksum evidence is separate and is not counted as subtitle delivery.

### Remaining acceptance conditions

1. Download E2 must obtain complete supported media/sidecars within existing
   bounded policies, then match, parse, pass unchanged QC, publish safely, and
   be re-read from the formal location with publication/manifest evidence.
   If no peers/data arrive, keep the normal unavailable-source outcome/backoff;
   do not repeatedly re-add this hash or count queued as delivered.
2. The two ambiguous old targets require season/special-episode identity evidence;
   the other three require a verified reusable/downloaded source or valid formal
   output evidence. Absent historical paths alone prove neither corruption nor
   permanent lack of subtitles. Existing uncertain groups and all 67 holds remain.
3. New strict Gate must settle its actual fixed cohort and satisfy every strict
   criterion. Until then M2 is incomplete. No M3 or acceptance-percentage claim.

## Verified autonomous formal delivery

- Anonymous obligation: `aiobl_17110f05e900b186c5dcf3b24103d05bbd1b08636ef526f0773696d818a75be7`.
- Existing deployed Worker `65a9670fb1cbeb459939df60755366bd32589e63`
  automatically produced one new traditional-Chinese delivery (three language ASS artifacts).
- The read-only verifier reopened the final files: manifest/delivery/publication
  semantics, parsing and existing hard QC PASS. Source SHA256 before/after the
  verification matches the checksum recorded during processing. Latest pipeline
  state is COMPLETED; the pre-deployment database had no verified manifest for
  this target. The verifier performed zero downloads and zero publications.
- Evidence: `/logs/m2-remaining-acceptance-20260908/delivery-final-verify-1788843960408162885.json`.
- This is **AI delivery**, not download/extraction acceptance or a Gate PASS.
  A duplicate-trigger production replay was not performed by this verifier.

## Reproduced final ASR evidence defect

- Existing failure: `aiobl_e7c26b6e56ea6b0076a572536f2993cd844a0160a8fd99470f99f231507061d2`.
  The shared final Japanese fragment gate correctly rejected the transcript,
  but did not persist that rejection before fail-closed cache cleanup. The inline
  fragment repair also bypassed the existing durable one-shot repair claim.
  Review consequently showed checkpoint unavailable and repair_attempted=false.
- Fix commit: `de6759499eaa0576dc9b9affd2f842109781d4c5`.
  Persist final rejection bound to the actual SRT checksum. Retain existing
  confidence/repair history only for matching bytes. Active-source fragment
  repair requires the existing durable budget; unavailable/consumed evidence
  fails closed. No QC, model, routing or Decision Schema change.
- Baseline reproduction: `fragment-rejection-before.log` FAILED at the expected
  repair_attempted assertion. The first broader invocation named a nonexistent
  test module; that invocation is retained as failed, not called a PASS.
- Correct focused suite: **209 PASS** (`fragment-rejection-regression.log`).
- Related guardrail/recovery/checkpoint suite: **312 PASS** and actual isolated
  container crash/restart PASS, source/audio unchanged, no ASR re-decode.
  `/logs/m2-asr-postprocess-isolated-20260908T050742138969Z/`.
  This tested candidate source mounted read-only in the prior image, not a new
  deployed image. Source revision: `1b9c2038283362c7102f25ce24f17eb08a02e3dfe76bbc58dd95a7e699f2e37f`.

## Deployment boundary

The candidate is committed and pushed; actual deployment and controlled recovery
are not claimed. Two bounded safe-idle checks returned
`planned_change_work_not_idle`: the preceding real Worker job remains active
with heartbeat. No force-stop, lock deletion or stale relabelling was performed.
At 2026-09-08T05:10:55Z the existing controlled claim helper released only this
attempt's owned admission pause, after verifying ARMED, unchanged baseline/Gate,
no prepared receipt and no deployment start. Server automation remains enabled.
Evidence: `/logs/m2-fragment-evidence-repair-20260908/deferred-closeout.json`.

- Actual Worker remains `65a9670fb1cbeb459939df60755366bd32589e63`;
  WebUI remains `175a02a7bad46e0b6fa2372c59f39e8dd272911e`.
- Breaker ARMED; existing Gate `m2-gate-20260908T042203260424Z-ea2562f0dc`
  and baseline `m2-guardrail-v1:4bd8a3c2eb64004cfd7c6717` retained.
- No new reconciliation record, source holds, Gate or runtime deployment was made.
  The prior 67 holds and old evidence remain in force.
- Next deployment must use a fresh timestamped handoff attempt; do not rerun or
  overwrite the closed attempt's immutable pause/target files. Recheck actual
  runtime and safe idle, preserve all current differences/holds, then use
  safe-update-stack, actual-image verification and formal controlled recovery.
  The prepared helper scripts are not evidence of a completed deployment.

## Still open

- A genuinely missing-subtitle target verified through the download → extraction
  → matching → formal-publication chain. The AI delivery above does not substitute.
- Existing external-source/backoff and uncertain identity obligations, including
  the retained 67 logical holds. Nothing is silently removed from failure totals.
- Production verification of the new final-ASR evidence behavior after safe deployment.
- Strict frozen 20-job acceptance remains server-side; no waiting, cohort replacement,
  acceptance/99-percent claim, or M3 work was performed in this closeout.
