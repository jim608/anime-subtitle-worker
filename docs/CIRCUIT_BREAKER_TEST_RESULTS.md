# M2 Circuit Breaker Test Results

## 2026-09-13 — source review fixes, actual deployed runtime

Worker `b6473598830b909cc37f139d8422c1f13697b43e` actual-image395 related tests PASS.
Fresh isolated seven-breaker suite **7/7 PASS**, Production resources affected:false.
Full result `/logs/m2-guardrail-fi-20260913T005926893770Z-8c8192b1/result.json`;
events.jsonl in the same timestamped directory. Controlled recovery verifies
runtime identity, retained101source holds/UNPROVEN and immutable old Gate history,
then reports ARMED and resumes claims. No direct latch/lock/DB edits.
Source-QC fallback has candidate384tests, seven historical artifact checks,
five historical decision-transition checks and separate-container restart PASS.
Real automatic task crosses earlyQC review into trustedJAASR with validcheckpoint
and heartbeat; no QC, hallucination, source or breaker protection was relaxed.
Evidence `/logs/m2-review-qc-deploy-20260913T0055/bounded-closeout-1789261455478301553.json`.
M2 Production accepted:NO; noGate20wait or M3.

## Actual deployed closeout — 2026-09-12 13:03 UTC

Worker1d5d737 actual image27a04d993a7f653c7392adc057e84a36fb108868c99d02b30f86818d0a9e7b02:
deployment2146 Worker/231 WebUI tests PASS; actual-image329 targeted PASS,
publication crash73/restart0, recovery DB/file crash74/restart0 PASS, real recovered
history prefix28 proof PASS. Fresh breaker7/7 PASS, production_resources_affected=false:
`/logs/m2-guardrail-fi-20260912T125648236691Z-9820cd01/result.json`.
Suites overlap; counts are not summed as unique coverage.
Actual-image evidence `/logs/m2-opencc-history-recovery-20260912/actual-image-proof-1789217740872998762/`.
Controlled recovery completed ARMED, receipt/source/oldGate preservation verified,
69 held guards reject and0 held claims. Two normal automatic historical claims
completed with valid checkpoints; correctly excluded from Gate0/20. Formal download
acceptance remains0 and M2 incomplete. No deliberate Production fault injection.

## Sep12 recovered-history regression

Deployed0a2b1ab actual-image323 tests, both isolated Docker restarts and7/7 FI PASS,
but Production controlled recovery safely refused the28 already-recovered reasons.
Original failed attempt remains `/logs/m2-opencc-runtime-recovery-20260912/host-recovery.log`.
New positive regression first failed with the exact unresolved-breaker error.
Candidate local85/server110 PASS; six added tests cover proven retained history,
receipt hash, retired archive, exact export, and extra earlier/later faults.
Real unchanged ancestor evidence prefix28 PASS via exact exported DB row plus
receipt/archive/runtime, at `m2-opencc-history-candidate-20260912-kNqiLx/evidence-only-68lQYA`.
No Production recovery was invoked by that probe; new correction not deployed.

## 2026-09-12 — subtitle-format candidate and durable handoff restart

Server `/logs/m2-opencc-recovery-20260911-wVrqzj/`: targeted323 PASS, exit0;
format crash73/restart0, recovery DB-COMMIT crash74/restart0 PASS. The latter
asserts one recovery record, unchanged attempts/sources/receipt/Gate, held-source
refusal and safe non-held admission. Earlier6KdaMv failed with two recovery records;
failure remains preserved. Local104 shared-boundary tests PASS (overlapping).
New exact-incident suite20 tests includes missing proof, wrong code/claim/detail,
source drift, unknown continuity holds, unrelated trip, unclassified difference,
DB/file interruption, changed replay snapshot and conflicting immutable export.

`actual-sources-corrected-RJTiCN/` exit0: three real incident SRT copies pass
original conversion/QC/manifest/replay;216/230/338 cues. Earlier uninitialized
isolated store refused source_hold_store_unavailable and is not a passed run.
Candidate overlays on old image only; no Production media mounts, formal outputs0.
Runtime remains5f116c3/TRIPPED. Fresh deployed-image tests and seven-breaker proof
must precede controlled recovery; this report does not claim current ARMED.

## Language-vote repair actual runtime — 2026-09-10

Worker db9265201bd6620ece8a72b2f4ea9a75bcfd82ff deployed via safe updater.
Actual-image targeted288 PASS plus real isolated container restart/checkpoint/
idempotency/source preservation PASS. Fresh isolated breakers **7/7 PASS**, with
Production resources unaffected by fault injection:
`/logs/m2-guardrail-fi-20260909T173941365219Z-b9864f23/result.json` and `events.jsonl`.
Full image proof `/logs/m2-language-vote-recovery-20260909T1720/actual-image-proof-1788975490191221381/`.
Controlled recovery returned ARMED; exact incident remains held and unresolved.
This does not validate real subtitle delivery or pass the strict20-job Gate.

## M3 deployed runtime validation (2026-09-08 11:04 UTC)

Worker `5f379e877ce59cb3207927e38aad2f6735b76287`, image
`sha256:d5757e05517f67da98b56976cdf14d04d962f0d5f884e5a7fae98041d50ff8f1`.
Actual-image targeted tests 211 PASS. Fresh isolated breaker suite 7/7 PASS,
Production resources affected false. Detail:
`/logs/m2-guardrail-fi-20260908T110426747663Z-f04674dd/result.json` and `events.jsonl`.
Official controlled recovery reports ARMED; `/logs/m3-runtime-handoff-20260908/recovery-closeout.json`.
This is not a completed 20-job Gate or model-output production acceptance.

## Deployed image verification (2026-09-08 06:33 UTC)

Actual Worker `a4829a5298304f9e2dc20dd02ecde883ea53ec23`, image
`sha256:4497656bc63ada24a4f76d721c81af07a8150d037382314be97c180558ba3849`:
312 related tests and isolated actual-image crash/restart PASS, including final-ASR
rejection/repair-budget regression. Evidence:
`/logs/m2-asr-postprocess-isolated-20260908T062524107986Z/`.
Fresh breakers 7/7 PASS, production resources unaffected by injection:
`/logs/m2-guardrail-fi-20260908T062807894290Z-c5c155cd/`.
Existing safe updater also passed Worker 1929 / WebUI 231 tests. A host-helper
CANARY_READY-only assertion failed after a successful historical reconciliation;
its log is retained. Evidence-bound continuation preserved the valid unclaimed
DISPATCHED job and completed recovery, without repeating committed revalidation.
See [closeout](M2_REMAINING_ACCEPTANCE_20260908.md); none of these tests means the
new frozen Gate or download formal-publication acceptance has passed.

## 2026-09-08 candidate evidence (not deployed)

`de6759499eaa0576dc9b9affd2f842109781d4c5`: final-ASR rejection baseline reproduced;
corrected focused suite 209 PASS; related suite 312 PASS and isolated container
crash/restart PASS. Existing QC unchanged, missing/consumed repair evidence fails
closed; source/audio unchanged. Logs and failed invocation retained in
[bounded acceptance evidence](M2_REMAINING_ACCEPTANCE_20260908.md).
No new production image or fresh production-runtime 7/7 claim is made. The prior
runtime's recorded breaker verification remains historical/current-runtime evidence.

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

## Line-repair candidate regression (2026-09-08, not deployed)

- Final candidate: 258 tests and actual isolated container restart PASS,
  `/logs/m2-line-repair-isolated-20260908T030844473143Z/`.
- Final read-only artifact/copy replay verifies historical source reuse, original
  noncohort stage/command binding, all 11 strict conditions and unchanged original
  provenance: `/logs/m2-line-repair-replay-20260908T030852409400Z/replay.json`.

- Before fix: one regression fails because successful line repair leaves provenance
  `failed`; `/logs/m2-line-repair-targeted-20260908T024741947576Z/tests.log`.
- Candidate: 253 targeted/shared tests PASS, including exact held incident recovery,
  restart/idempotent receipt reuse, wrong code, wrong proof contract, missing historical
  source proof, unheld incident and unrelated trip rejection.
- Real isolated container exit 73 / restart PASS; original source, transcript checkpoint
  and archived prior provenance preserved. The model and publisher are fixture stubs,
  so this is not a real subtitle delivery count.
- Logs and code fingerprints: `/logs/m2-line-repair-isolated-20260908T030048462657Z/`.
  `tested_runtime_image=false`; this report cannot authorize Production recovery.
- Read-only Production artifact/copy replay passes all 11 strict flags:
  `/logs/m2-line-repair-replay-20260908T025520099117Z/replay.json`.
- Actual-image attestation, fresh seven-breaker FI and controlled handoff remain pending.

## Actual 6c92585 image and formal recovery (2026-09-07 18:30 UTC)

- Safe updater: 1,893 Worker tests + 231 WebUI/deployment tests PASS; EXIT=0,
  deployment `20260907T135509Z-2106168`; no Production DB rollback.
- Actual new Worker image: 222 targeted/shared tests PASS, plus actual container
  exit after SRT replacement and restart continuation; quality gate and idempotency
  PASS. `/logs/m2-asr-postprocess-isolated-20260907T135811202515Z/`.
- Fresh seven-breaker isolation suite **7/7 PASS**, no Production resources affected:
  `/logs/m2-guardrail-fi-20260907T183052308766Z-d2a2df4d/{result.json,events.jsonl}`.
- Exact controlled recovery returned ARMED; `/logs/m2-production-recovery-20260907T183055524357Z-efe09d73.json`.
- Live hold check: original 63 rows preserved, incident added (64); 64/64 formal
  publication guards reject and zero held post-start claims. Successive normal
  claims and one new source-detection checkpoint proven, not new subtitle delivery.

## ASR diagnostic-loss repair candidate (2026-09-07; not deployed)

- Exact two-block regression first reproduced accepted diagnostics deletion.
- 10 targeted ASR tests PASS; 139 shared Worker/ASR review tests PASS.
- Server-isolated 149 tests PASS; actual container terminated after SRT replace
  and before diagnostics commit, then restarted and restored the original pair.
  Unmocked structural quality gate and repeated postprocess passed; zero ASR reruns.
- Evidence: `/logs/m2-asr-postprocess-isolated-20260907T134201658435Z/`.
- Restricted incident-recovery and existing reconciliation/guardrail tests:
  73 local tests PASS. Server candidate/actual-image validation still required
  for the subsequently added recovery boundary. Production breaker remains latched.

## Actual sidecar-discovery image and handoff (2026-09-06 22:40 UTC)

Worker `d508b599291978cc5f6ab786c560823ea755e249`, WebUI
`175a02a7bad46e0b6fa2372c59f39e8dd272911e`: safe-update **1,874 Worker + 231
WebUI tests PASS**; immutable actual-image targeted suite **94 PASS**.
Fresh post-reconciliation breaker injection **7/7 PASS**, Production resources
affected false. Full FI:
`/logs/m2-guardrail-fi-20260906T223959047915Z-8166f43b/{result.json,events.jsonl}`.
Owned deployment mode additionally passed **9 server-isolated shell/probe tests**.
The source fix reproduced the real MP4 Chinese-external rejection, passed local
48 targeted tests and the 305-test server candidate suite, followed by the final
actual-image/safe-update suites including the extra negative regression.

Server evidence:
`/logs/m2-recovery-unblock-20260905T064508843990Z/external-sidecar-handoff-20260906/`.
Actual checks: 63 publication guards blocked, zero held-source claims; formal
controlled recovery ARMED; real unheld Queue claim with completed source-decision
checkpoint and subsequent ASR. Real source metadata selection is not a verified
download or subtitle publication. No first-20 acceptance is asserted.

## Authorized runtime closeout (2026-09-06)

Worker `b92a61fe086bb0cb48aef46f9b9efd967d33d969` is actually deployed.
Safe-update: **1,871 Worker + 229 WebUI tests PASS**. Final candidate server
isolation: **405 PASS**. Immutable actual deployed image targeted suite: **78 PASS**.
Fresh post-reconciliation FI: **7/7 PASS**, no Production resources affected;
`/logs/m2-guardrail-fi-20260906T115451045785Z-853ce952/{result.json,events.jsonl}`.

The required isolation/restart/refusal/preservation cases are covered by
`test_m2_reconciliation_source_holds`, `test_m2_authorized_reconciliation`,
`test_event_watcher` and related shared-boundary suites. Actual Production checks
confirm 60 durable holds reject publication, zero held-source claims, old receipt
and old Gate/four-member evidence unchanged, original backup retained, SQLite `ok`.
Real normal/recovery claims, stage heartbeats and digest-valid checkpoints are
recorded, followed by autonomous next claims. One autonomous new formal subtitle
set was revalidated from final paths (COMPLETED, hashes/parse/role-correct QC PASS,
source checksum matches). Neither mock tests nor claims are counted as delivery.

Full deployment, image tests, proof and final output verification logs remain at
`/logs/m2-recovery-unblock-20260905T064508843990Z/authorized-reconciliation-b92a61f/`.
See current observation closeout for exact filenames and remaining boundaries.
No frozen first-20 completion or production autonomy rate is claimed.

## Authorized reconciliation candidate (2026-09-06; not yet production closeout)

Server-isolated verification: **405 tests PASS**, 13 targeted/shared-boundary
modules. Source mount read-only, network disabled, no Production media mounts.
Full log: `/logs/m2-recovery-unblock-20260905T064508843990Z/server-parser-planned-tests-20260906T113242073200Z.log`.

Coverage includes held normal/recovery claims and publication refusal, safe peer
dispatch, persistent missing obligations, atomic rollback/idempotent restart,
new-difference refusal, unchanged old receipt, controlled recovery/new Gate/resume,
and durable filesystem event deferral across restart. These are isolated tests;
actual deployment, fresh running-image 7/7 FI and real claim evidence are pending.

## Verified second recovery runtime (2026-09-05)

Deployment `20260905T083811Z-380719` runs Worker
`60d6b2361a54a76730c5a943dfd3fac8b98cca19`: 1,821 Worker and 229 WebUI
safe-update tests passed. Fresh actual-image evidence in
`runtime-handoff-v2/running-image-regression.log` is 73 PASS; its separate
`running-image-fault-suite.log` attests **7/7 PASS**, unchanged Production
resources and exact deployed source/container. Recovery completed and runtime
reported ARMED at the new Gate start `2026-09-05T08:46:08.410661Z`.
Do not sum overlapping suites or call these observation-cohort completions.

`actual-next-claim-0908.json` proves the first safely reviewed recovery was
followed by a distinct automatic claim and SUBTITLE_DETECTION checkpoint.
Neither was falsely COMPLETED; quality review did not re-pause the lane.
The normal Amaburi extraction attempt rejected real E13 hard-QC failures,
preserved source and prior sidecars, and requested bounded replacement:
formal publication count remains zero.

The additional parser-boundary candidate passed 341 server-isolated tests in
`server-parser-boundary-tests.log`: malformed SRT does not hide a valid sibling;
repeated invalid staged publication preserves prior output/source/receipt;
SSA/VTT unsupported validation is explicit and cannot bypass staged QC.
These tests are candidate evidence, not yet deployed-image attestation.
The exact real E13 final-file RO check additionally completed without parser
exception and with all source/prior subtitle hashes unchanged; it found no
verified final subtitle or new sidecar (`amaburi-candidate-file-validation.json`).

## Recovery unblock candidate evidence (2026-09-05)

First actual deployment `ea0baaf...`: safe-update completed with 1,813 Worker
and 229 WebUI tests; the immutable running image passed 65 focused tests and
fresh 7/7 fault injection. Controlled recovery then refused the legitimate
new-container drift because the old event-only contract did not cover deployment
handoff. Its refusal and all original receipts remain in the server log root.
The new handoff contract has passed the actual RO event/config/identity parity
probe; final deployed-image verification must be recorded separately.

The final frozen server-isolated candidate suite passed **744 tests**
(`server-final-freeze-tests.log`), including both recovery/Queue deadlines,
retry-budget selection, exact quality-pause release and refusal contracts,
scanner second-writer concurrency,
completion-proof mutation/restart safety, typed materialized-source review,
mixed generic cause separation, same-cause distinct-job trips, source mutation,
and negative event-bound recovery contracts. Earlier 87/418/147 counts overlap
and are not additional tests. Actual read-only incident binding also passed:
three exact immutable attempts, normalized cause clusters 2 + 1, unchanged source
checksums. Deployment and fresh running-image evidence remain required.

Full server logs: `/logs/m2-recovery-unblock-20260905T064508843990Z/`.
The admission/checkpoint isolated container suite passed **87 tests**. The frozen
mapping probe used only the existing specified keys, 96 stored profiles/cache
keys and exact indexed targets/NFOs: 83 unique local bindings, not source-content
or publication passes. Production SQLite was read-only for these probes.

Real-artifact fault reproduction in `e12-publication-guard-old.json` confirmed
that the old normalizer published the downloaded hard-QC-failing ASS unchanged
over a valid isolated fixture. Production source/subtitle and download hashes
were unchanged. This is a reproduced import defect, not a Production incident.
Candidate and combined regression results, deployment parity and fresh running-
image breaker evidence must be recorded in closeout before claiming completion.

## Acceptance follow-up without redeployment (2026-09-05)

No code change, container restart, Gate rebuild, full audit or repeat full
regression/fault-injection suite was performed. The preceding deployed-image
7/7 result remains historical evidence for the unchanged b911 image, not a
fresh test count from this follow-up. Runtime still reported ARMED and the
same Gate remained ACTIVE.

Bounded server evidence in
`/logs/m2-recovery-acceptance-20260905T051642581908Z/` includes:

- Full validation of the exact four unsuccessful and five old-completed
  obligations (11 indexed target paths); 4/5 old completions have verified
  existing formal subtitles, one has unresolved 6 versus 6.5 identity.
- Four genuinely missing-valid-subtitle targets; existing QC rejects their
  current content. Source discovery was reachable but selected no permitted
  replacement. **New formal publications: 0; new-publication E2E NOT PASSED.**
- Two subsequent M2 recovery claims with SUBTITLE_DETECTION start, heartbeat
  and durable terminal/retry evidence; a complete checkpoint for the
  quality-isolated case. No AI success or translation completion was credited.
- Download source-search activity is separate; the earlier enqueue cycle is
  still executing. No different next download/extraction claim was proven.
- All checked source-file identities and existing subtitle hash sets stayed
  unchanged. Source-video full checksums were not recomputed during this
  read-only follow-up. No runtime/protection state was manually overridden.

## Latest deployed M2 download/recovery closeout (2026-09-05)

- Worker runtime: `b911794ed0ec872cb475f714e1385e20e8ac4388`.
- Worker image: `sha256:300a394ebd181f57fa7f7d6e017957cbef86d3ce44eae747b37a092503361afa`.
- Final source-image regression: **1,737 Worker + 229 WebUI tests PASS**.
- Focused recovery/guardrail/observation regression: **74 PASS**.
- Final deployed-image seven-breaker harness: **7/7 PASS**;
  `/logs/m2-guardrail-fi-20260905T045613693910Z-334ff38d/result.json`
  and the corresponding complete `events.jsonl`.
- Controlled recovery: **TRIPPED -> ARMED**, claims resumed;
  `/logs/m2-download-recovery-audit-20260905T030809930683Z/controlled-recovery-closeout.log`.
- New Gate: `m2-gate-20260905T045640981079Z-08147de925`, start
  `2026-09-05T04:56:40.981079Z`, baseline
  `m2-guardrail-v1:5b4d2a88f2d5c0c5749f6747`, initial **0/20**.
- No Production source/output was used as destructive fault-injection data.

The Gate-publication regression inserts admission after the real SQLite Gate
creation but before manifest publication. It proves denied claims without a
false invalidation, and covers stale snapshots, missing pause, invalid recovery
records and true drift. Recovery tests cover removed unclaimed items, exact
restart persistence, no duplicate dispatch, running/CLAIMED preservation,
local quality/bad-input isolation, retry budgets and automatic next canary.

Real downloaded Dark Gathering S01E01 passed file-completion verification,
actual extraction of both Chinese tracks, target matching and isolated atomic
import (`real-source-proof.json` under the audit root). Existing Production
outputs were already valid and preserved: **new Production imports 0**.
The missing-output 304:10 sample is source-blocked; no failed hash was retried
to fabricate an E2E pass. No backlog or 20-job Gate completion was awaited.

The final bounded server snapshot also proves autonomous continuation: 12
download targets were consumed (eight verified existing outputs, four still
unsuccessful), 875 remained durable, and the server dispatched a different
AI canary after recording one pre-claim exclusion. The next AI item remained
queued with zero claims; its eventual claim/result is explicitly unverified.

All sections below retain earlier candidate/historical results, not the current
runtime identity. Full final deployment output is `safe-deploy-5.log` in the
same timestamped audit directory. No M3 or production-acceptance claim is made.

## M2 download recovery candidate, 2026-09-05

Full log: `/logs/m2-download-recovery-audit-20260905T030809930683Z/isolated-tests.log`.
283 focused cases passed against the candidate snapshot in the server container.
Coverage includes partial queuedDL/restart, API-error durable backoff, path
mapping, no usable subtitles, extraction cancellation, atomic import/replay,
and duplicate-download prevention. Generated real ffmpeg media exercised
extraction and validated publication with unchanged source checksums.
Fixtures used isolated temporary directories, never Production source/output.
This is not evidence of a real historical Production download. Fresh 7/7
post-deployment breaker evidence is still required before re-arming.

## Result boundary

Recorded on 2026-09-04.

Recovery candidate revalidated on 2026-09-05.

- Local focused harness: **PASS (7/7 breaker cases)**
- Focused unit and CLI contract tests: **PASS (6/6 tests)**
- Frozen cohort/schema/event-journal tests: **PASS (24/24 tests)**
- Recovery/replay tests: **PASS (11/11 tests)**
- Complete Worker regression: **OK (1,649 run; 1 conditional skip)**
- Recovery candidate targeted regression: **OK**
- Recovery candidate complete Worker regression: **OK (1,720 run; 1 conditional skip)**
- Restart-safe recovery complete Worker regression: **OK (1,721 run; 1 conditional skip)**
- Historical pre-repair server image/runtime execution: **PASS (7/7 breaker cases)**
- Historical server timestamped full log validation: **PASS**
- Historical runtime status after initialization: **ARMED**
- Final repaired server image/runtime execution: **PASS (7/7 breaker cases)**
- Live controlled recovery: **PASS (`TRIPPED` -> `ARMED`)**
- Replacement frozen Gate: **ACTIVE (`0/20`)**
- Production source media or formal outputs affected by fault injection: **No**

The server run below was executed before the frozen-cohort automation repair.
It proves that historical guardrail runtime only. Its observation Gate is
invalidated as `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`; the result does
not initialize or pass a replacement 20-job Gate and does not establish
Production acceptance. The repaired deployment generated a separate fresh
timestamped 7/7 result before arming its replacement Gate; both runs remain
independently auditable.

## Isolated fault matrix

| Breaker | Injected condition | Local isolated result | Server image/runtime result |
| --- | --- | --- | --- |
| `source_mutation` | Failed source-integrity observation with changed checksum evidence | PASS | PASS |
| `duplicate_publish` | Failed publish observation with duplicate-publication evidence | PASS | PASS |
| `output_parse_failure` | Failed delivery-verification parse observation | PASS | PASS |
| `incorrect_completion` | Reported success without required verified delivery evidence | PASS | PASS |
| `repeated_oom` | Three consecutive OOM-classified failures | PASS | PASS |
| `repeated_identical_stage_failure` | Three consecutive identical stage/failure signatures | PASS | PASS |
| `insufficient_disk_space` | Isolated disk-capacity hook returns less than the configured floor | PASS | PASS |

Both repeated-failure thresholds were `3`. The first two observations remained
closed and the third opened the expected breaker.

The recovery candidate adds explicit coverage that five retry attempts for one
stable job count as one streak member, while three distinct jobs still trip.
It also verifies that `source_selection_needs_review` remains
`QUALITY_BLOCKED`/`NEEDS_REVIEW`, not a retryable system failure.

## Historical recovery matrix

The focused recovery tests cover known fixed-version failures, canonical and
schema-compatible checkpoint resume, completed-ASR/translation-stage reuse,
partial-translation continuation, QC-only resume, incompatible-checkpoint
minimum-stage replay, stale running recovery, unsupported/quarantined input,
single-canary dispatch, retry budget/no-progress blocking, durable restart,
source/output immutability, required metrics, controlled latch recovery, and
old-Gate runtime-change invalidation. Existing frozen-cohort and observation
recovery suites continue to cover no backfill, immutable first-20 membership,
terminal replay idempotency, and exactly-once summary publication.

## Assertions applied to every case

Each of the seven local cases and all seven server-isolated cases passed all
ten assertions:

| Assertion | Local result | Server-isolated result |
| --- | --- | --- |
| Production claim admission wrapper called | 7/7 PASS | 7/7 PASS |
| New job claim stopped | 7/7 PASS | 7/7 PASS |
| Queue state preserved | 7/7 PASS | 7/7 PASS |
| Valid checkpoint preserved | 7/7 PASS | 7/7 PASS |
| Running job not interrupted | 7/7 PASS | 7/7 PASS |
| No false `COMPLETED` state | 7/7 PASS | 7/7 PASS |
| Expected reason and non-empty evidence persisted | 7/7 PASS | 7/7 PASS |
| Sandbox-only safe recovery verified | 7/7 PASS | 7/7 PASS |
| Synthetic source hash unchanged | 7/7 PASS | 7/7 PASS |
| Output fixture untouched | 7/7 PASS | 7/7 PASS |

The claim assertion calls `main._m2_server_canary_admit_new_job`, the same
fail-closed wrapper used immediately before production claims. The queue behind
that boundary is synthetic and isolated; no production queue backend is opened.

## Historical server validation evidence

- Run ID: `m2-guardrail-fi-20260904T105817974649Z-b7176039`
- Worker source revision: `b8986e794d3cb84bdcc831fbb53d19dfe8275358c37529fe1d9375ccd6e1fd3d`
- Started: `2026-09-04T10:58:17.974636Z`
- Finished: `2026-09-04T10:58:18.758425Z`
- Result digest: `sha256:0b5f747c66eb514a1927daf33cec985c8235f7f27f73572a838ad838b8bb4c90`
- Full event-log digest: `sha256:371c300b231fdc42efa0203533d7473d07344eebd236d973839770abe10e674a`
- Historical runtime gate initialization: `ARMED`, baseline `m2-guardrail-v1:276fdef781528ba2059c114e`, initial progress `0/20`; subsequently invalidated as `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`

## Final repaired server validation and recovery evidence

- Worker runtime SHA: `d9dfcd01aa9ebeffe65c8367f4e1bbace56d5bcc`
- WebUI runtime SHA: `7bd36c30fb07e393eba71760a164246d267c5b16`
- Fault run ID: `m2-guardrail-fi-20260905T020807095602Z-7f551399`
- Fault result: `/logs/m2-guardrail-fi-20260905T020807095602Z-7f551399/result.json`
- Full fault event log: `/logs/m2-guardrail-fi-20260905T020807095602Z-7f551399/events.jsonl`
- Breaker tests: `7/7 PASS`
- Controlled recovery log: `/logs/m2-production-recovery-resume-20260905T020843873483Z-b68b9cda.json`
- Breaker transition: `TRIPPED` -> `ARMED`
- Invalidated Gate: `m2-gate-20260904T163053158998Z-f3076238c7` (`INVALIDATED_BY_RUNTIME_CHANGE`)
- Replacement Gate: `m2-gate-20260905T020845085531Z-7d0c5c7333`
- Replacement baseline: `m2-guardrail-v1:0180b8779ee97524bf0150d2`
- Gate start: `2026-09-05T02:08:45.085531Z`
- Initial progress: `0/20`
- Production source/output affected: `false`

The persisted recovery snapshot recorded 2 historical `FAILED`, 470
`RETRYING`, 4 stale `RUNNING`, 0 `QUARANTINED`, and 693 historical
`NEEDS_REVIEW` rows. It classified 222 entries as recoverable and 947 as
permanently excluded. At the bounded closeout snapshot, one recovery canary had
been dispatched, 221 entries remained ready, and the checkpoint-resume count
was 0. The canary result and later recovery work were deliberately not awaited.

## Commands and focused results

```text
python -m unittest -v test_m2_guardrail_fault_injection.py
Ran 6 tests ... OK

python m2_guardrail_fault_injection.py --log-dir <server-log-directory>
breaker_tests_passed=7, breaker_tests_total=7, status=PASS,
production_resources_affected=false

python m2_guardrail_runtime.py arm <bounded-runtime-evidence>
status=ARMED, breaker_tests_passed=7, initial_gate_progress=0/20

python m2_production_observation.py --config <runtime-config>
milestone_status=M2_GUARDRAILS_ARMED, status=ARMED, gate_progress=0

python -m py_compile m2_guardrail_fault_injection.py test_m2_guardrail_fault_injection.py
PASS

git diff --check -- m2_guardrail_fault_injection.py test_m2_guardrail_fault_injection.py
PASS
```

Successful CLI output contains only the bounded status, pass count, run ID,
log location, and production-impact flag. Per-case checks, reason evidence, and
tracebacks remain in `events.jsonl`; the machine-readable complete result is
stored in `result.json` under the same timestamped run directory.

## Remaining unverified

The following evidence remains explicitly pending:

- The replacement frozen first-20 cohort outcome. This closeout initializes it
  but does not wait for its 20 jobs or claim Production acceptance.
- The terminal result of the single dispatched recovery canary and later
  recovery-lane items.
- Full 100-input M2 release gate and later rolling-production SLO evidence.
- Any measured production autonomy-rate claim.

No production media or output was used for the local fault suite. No production
observation milestone or autonomy-rate conclusion is asserted by this report.
