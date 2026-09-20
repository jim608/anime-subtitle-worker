# Full-flow reliability acceptance — 2026-09-20

Active Goal replaces the previous one-claim recovery closeout. Overall NOT COMPLETE.
No M3, full-library rescan, broad Queue retry, QC relaxation, source/valid-output
overwrite, backup deletion, or ASR-budget reset. Server must own the eventual
24-hour observation using existing observer/ledger; not a second framework.

## Current baselinef52118c — deployed; WAITING_FOR_EVIDENCE

Actual Worker `f52118c564e9aebca89d52b336e2156d2ba095cd`, image
`sha256:e624e0e890a68025660fe320d1dd1b109450d179120d13f617ac13b15e139f65`,
source revision `f65fbb031e62f1e06d58572acadc8d09b9b8661f686032c877f7685fbc0d9daf`.
WebUI69f2352/image63e035bc unchanged. Safe deployment `20260920T014525Z-3338102`
completed only after the active ASR task naturally reached review01:44:36.811125Z.
No force-kill or foreign pause release. Actual image386 related tests PASS as non-root,
including real report-directory permission failure; real Docker exit/restart preserves
the report retry budget and immutable20-result journal, access repair publishes once,
and the next isolated queue task claims/starts Stage. Fresh7/7 breakers PASS.

Reconciliation `m2-recon-report-export-20260920`, receipt SHA256
`bd36fc24b0aa8284a27d4e9959fc96f0fc5db8211c70e547483aa661d5a71885`:0 new holds,
105 prior holds retained;6735 queued identities/7377 other states preserved, not
eligible/delivered counts. All55 old backups plus new56 retained. Recovery
`m2breakerrec_9146d3b1d14f460280dc0b52312b0f2e` cleared only the exact planned
runtime-change trip and released owned hold01:48:30.652033Z. Report permissions
were never changed in Production; its independently reproduced defect was isolated.

New Gate `m2-gate-20260920T014830021232Z-8147e39531`, baseline
`m2-guardrail-v1:c83fd65db8ed1c8307468303`, starts2026-09-20T01:48:30.021232Z.
Configuration fingerprint f71f27e11f68a07b316dd5797cc670ec55359d13de13f7d11665872fa7781aac,
decision schema1/frozen-first20 unchanged. Old005942 Gate's2 review members retained,
INVALIDATED_BY_RUNTIME_CHANGE, no backfill. Earliest24-hour end
2026-09-21T01:48:30.021232Z; all other acceptance criteria also remain required.
At01:51:04Z: ARMED, provider freshly VERIFIED, Gate enrolled1/20, settled0/strict0.
`m2ai_7a642b2574cf708e9e19` auto-claimed01:49:22.115900Z -> trusted-JA ASR/heartbeat
01:49:48.898084Z; verified subtitle-detection checkpoint90e759ca9811801aaaebf8e739eed0d6750d10e967f7fad5a811d865c1136b17.
At01:56:24.249511Z this first member reached deterministic_asr_quality review,
not false completion.01:58:19Z: ARMED, enrolled1/20, settled1, strict0. Existing
ASR review autopilot queued cmd_2ecb79858b0cb8ba3c664d1c at01:57:04.759Z and normal
queue yielded to that bounded revision-scoped remediation; no manual Retry.
Narrow durable log `post-terminal-scheduler.log` (01:56:20..01:58:40) records the
reason. Existing watch interval is300 seconds; do not force a legitimate wait.
Latest-baseline distinct terminal/next-claim and sustained24-hour proof remain pending.

Evidence `/logs/reliability-20260920/report-export-handoff-20260920/`:
`actual-runtime.json`, `actual-image-proof.json`, `actual-image-tests.log`,
`fault-suite.log`, `recovery-closeout.json`, `backup-preservation.json`,
`observation-check-1789869064933116458.json`,
`observation-check-1789869499543303553.json`. Actual restart
`../report-export-final-restart-actual/restart-xk15hswm/state/result.json`, fixture
6ac102bc... removed after export, volumes_removed0. Latest related stopped containers0
in `../container-inventory-1789869063852915091.json`; initial5 obsolete removals unchanged.

The one-shot read-only closeout now bounds Stage/heartbeat evidence to each claim's
own terminal time and separates same-obligation repair from distinct next-job claims.
Old JSON is not rewritten.6069fcb had2 distinct continuations plus1 bounded ASR repair;
6f400b4's5 continuations were independently checked to be distinct. Same-task ASR
repair uses an existing revision-bound command and max3 total attempts, not a budget
reset/new delivery. Goal-to-date2 new335/318-cue TC outputs remain6f400b4 evidence;
prior download/extract351-cue proof is reused and not recounted. Mux remains disabled.

| Recorded interval (UTC) | Cause / boundary | Duration to controlled admission resume |
| --- | --- | --- |
| Sep19 17:29:10.445402 ->23:33:52.718677 | Actual incorrect_completion/source-transcript evidence bridge incident | 6h04m42s |
| Sep19 23:45:34.896185 ->Sep20 00:12:30.342904 | Actual source-ASR diagnostic-loss recurrence | 26m55s |
| Sep20 01:37:44.613069 ->01:48:30.652033 | Owned planned report-boundary deployment pause; active job finished naturally | 10m46s, not unexplained safety stoppage |

These historical interruptions cannot count toward the new continuous24-hour window.
No M2 acceptance or Goal completion; documents alone must not redeploy/recreate Gate.

## Previous baseline6069fcb — preserved deployment and counterexample evidence

Follow-up admission audit reproduced a separate report-only defect in isolation:
after all20 results are durably journaled, report-directory EACCES escaped terminal
settlement; the next admission classified it as observation_state_degraded and
tripped globally. This is NOT a new Production incident/clearance. Candidate limits
deferral to report-export EACCES/EPERM/EROFS, preserving immutable journal/hash,
full disk/EIO, database corruption, report collision and unknown-fault protection.
Existing observation metadata records reason/time and three bounded attempts with
backoff; unchanged failed access cannot retry forever. A real report-directory/file
access change permits recovery; restart retains the budget. Public status exposes
this report warning separately from the safety latch. No new Queue/framework/schema.
Server non-root real-permission test and179 relevant tests PASS before final metadata
refinement; final candidate regression and actual-image verification remain required.
Existing Docker lifecycle proof `report-export-restart-candidate/restart-vsvv_45j/`
preserves20 frozen results and retry budget through exit/restart, then publishes once
after access changes and claims the next real isolated queue Stage. Fixture removed.
Original failing logs remain (`report-export-before.log`, `report-export-targeted.log`).
This was candidate-stage evidence. The current section above records the later
f52118c deployment and actual-image386 tests; no old test is substituted for that SHA.

6069fcb real counterexample is now proven, not just first claim: task
`m2ai_fe725855a8d35dee61f8` exhausted the existing hard-display repair at index140,
allowed25; it correctly reached subtitle_quality_review01:23:07.851195Z instead of
translation_unknown/bounded_retry. Next `m2ai_8a574174b40e28870976` auto-claimed
01:24:01.445965Z and entered JA transcription01:24:28.627032Z. ARMED at01:25:57Z,
two continuations; Gate enrolled2/20, settled1, strict0. Report
`readability-handoff-20260920/observation-check-1789867557338683531.json` preserves
the first member's review, not a replacement or strict success. No new output counted.

Worker `6069fcb3729c12e64a7d892903f62cf391fb5563`, image
`sha256:170661722f188234f403d476eb4d58f7d0846d9490c919ff1dc4113e7bc37e3d`,
source revision `2878754113c9a3c2848972ff0c04126539c8331fc56b1914047fea4cd49d1b58`.
WebUI remains `69f2352d0d5d371ca58eaf5a4c62d97f79529e69`, image
`sha256:63e035bc7d0cac764247a7eb83c7197597a026088e53f335c42e3848fdd46e5b`.
Safe deployment `20260920T005641Z-2895914` and complete handoff exited0.
Actual-image748 related tests, actual Docker review/restart/next-claim,
read-only original exhausted-quality classification and fresh7/7 breakers PASS.
The original failed attempt is preserved, not retroactively made successful.

Reconciliation `m2-recon-readability-20260920`, receipt SHA256
`1fdd427fda40ac75905ea45ae095568743db81db83a8228f0d3ddb8054d678f5`:
0 new unexplained differences,105 existing holds retained;6715 queued identities
and7375 other-state records retained (not eligible/delivered counts).
Recovery `m2breakerrec_bfb2cfc287f94ca3a482e6e47bd1fa33` changed the exact planned
runtime-change trip to ARMED. Only owned hold released00:59:43.448694Z.
All54 previous backups plus this deployment backup retained:55. No source writes,
valid-output overwrite, isolation release, checkpoint-budget reset or latch deletion.

Current Gate `m2-gate-20260920T005942949442Z-ffe5ed7d42`, baseline
`m2-guardrail-v1:32850f5c3550d3b5fbfe78c6`, start
`2026-09-20T00:59:42.949442Z`. Prior004343 Gate retained and
INVALIDATED_BY_RUNTIME_CHANGE. Configuration fingerprint
`sha256:f71f27e11f68a07b316dd5797cc670ec55359d13de13f7d11665872fa7781aac`, schema1,
unchanged frozen-first20 policy. Earliest24-hour end
`2026-09-21T00:59:42.949442Z`; time alone does not satisfy the acceptance criteria.

At01:08:57Z: ARMED, ACTIVE0/20, no active pause or reconciliation hold.
Real `m2ai_84744809b306acf8b95c` automatically claimed01:05:25.348897Z,
trusted-JA transcription started01:05:33.968073Z, heartbeat01:08:34.449437Z;
verified subtitle-detection checkpoint SHA256
`5f706597d505a29b40d7d291af5d78c75b6a9b37d0798077299928bc573f78c5`.
No manual Retry. This historical retry is not a new frozen-cohort member.
Later bounded evidence at01:14:00.857531Z proves one latest-baseline continuation:
`m2ai_84744809b306acf8b95c` reached review_required01:12:40.895177Z for
deterministic_asr_quality (prompt-free ASR still contains one-character/punctuation
fragments); quality safeguards/budget remain intact. Distinct
`m2ai_fe725855a8d35dee61f8` automatically claimed01:13:34.259037Z, preflight
01:13:36.217625Z, language-detection heartbeat01:13:38.877426Z and verified
subtitle-detection checkpoint `d410acddb6a605fd042dc8635e9b9d313861b5e04b7da430efc35618869807a7`.
No manual operation between terminal and next claim. ARMED; current Gate enrolled1/20,
settled0, strict0; ordinal1 is the latter task's first eligible attempt. The preceding
historical retry remains excluded, not replaced. This is one real cycle, not24 hours
or proof of the separate exhausted-readability case's Production terminal.

Evidence root `/logs/reliability-20260920/readability-handoff-20260920/`:
`safe-deploy.log`, `actual-runtime.json`, `actual-image-proof.json`,
`actual-image-tests.log`, `fault-suite.log`, `recovery-closeout.json`,
`backup-preservation.json`, `observation-check-1789866538968444059.json`,
`observation-check-1789866841011099343.json` (real terminal/next claim).
Actual restart result: sibling `readability-restart-actual/restart-l63b34if/state/result.json`.
Final bounded container inventory `../container-inventory-1789866149610699545.json`
found0 related stopped containers; only running Worker/WebUI and --rm inventory helper.
Initial5 proven obsolete fixtures removed after archival; an additional candidate
fixture retained by strict mount checking was subsequently archived and removed
by exact non-force ID. All later fixtures exported and removed by existing teardown.

Goal-to-date2 new strict formal TC targets (335/318 cues) remain attributed to
6f400b4 below, not this new Gate/window. Prior download/extraction351-cue proof
is reused function evidence, contributes0 new deliveries here. No M2 acceptance.
Read-only loaded runtime config (`effective-delivery-scope.log`) confirms
completed_delivery_enabled=false and watch_interval_seconds=300. Mux is not enabled
on this baseline; no fresh mux output is required or falsely claimed, and this check
did not enable it or alter result-affecting settings.
Documentation-only synchronization must not redeploy or recreate the current Gate.

## Previous baseline70abdc2 and then-candidate quality terminal repair

Encoding repair safely deployed as `70abdc2684dfdd6171c71c71763376c647086ca8`,
image `sha256:b3b759cf50248c081722d8a17449b53636e85bdb9e2128a4dbfeaf3ad1573efe`.
Deployment `20260920T004010Z-2739505` completed. Actual-image642 tests PASS;
actual Docker quality-candidate review/restart/checkpoint/next-claim and real
read-only encoding-source replay PASS. Recovery
`m2breakerrec_20bd9a1c79604026bb6738b17bfc2396` restored ARMED and released only
owned pause at00:43:43.953804Z.105 holds remain; all53 prior backups plus new54
retained. Reconciliation `m2-recon-source-encoding-20260920` (SHA256
`bb881746519a6586bfeeb066038a702c0b0b02e6d9828c93a96344b9bedfc13d`),0 new differences.
Old001228 Gate retained/INVALIDATED_BY_RUNTIME_CHANGE. New
`m2-gate-20260920T004343198914Z-5c074626fa`, baseline
`m2-guardrail-v1:782ac873948e53bca3c1614a`, starts00:43:43.198914Z at0/20.
Evidence root `/logs/reliability-20260920/encoding-handoff-20260920/`.

The last predeployment job naturally ended at00:39:13.178Z, not killed:
`m2ai_4042788fbbd12585edde` exhausted2/2 targeted readability repairs (24 chars,
hard allowance23). Completed translation checkpoint retained. Normal Queue wrongly
classified this proven deterministic quality condition as translation_unknown /
bounded_retry; historical recovery already classifies the exact condition as
QUALITY_BLOCKED. Candidate changes ONLY main._ai_failure_policy: anchored exact
bounded-repair failure in the translation stage -> existing subtitle_quality_review /
manual_review. No blanket TranslationError catch, QC change, budget reset or fake
completion. Timeout/OOM/unknown faults and source mutation continue original paths.

Before test40 (including37 imported fixture tests) reproduced4 failed assertions;
candidate fixture import corrected to avoid duplicate discovery, related374 PASS.
Actual JA/zh/en two-attempt repair failures retain source/existing output bytes.
Three distinct quality terminals remain ARMED and next claim succeeds. Real Docker
restart `readability-restart-candidate/restart-fjgnyj8a/` preserves review and
checkpoint, next claim/Stage and idempotency; fixture removed after evidence export.
Logs `readability-terminal-before.log`, `readability-terminal-related.log`,
`readability-restart-candidate.log`. This was predeployment evidence; the current
section above records the later actual-image deployment. Do not mix baseline results.
An attempted pre-retire cancellation was correctly NOT performed because safe
deployment had already reached backup stage; no process/container was terminated.
Any further deployment is specifically for this newly reproduced shared defect.

Second genuine new TC output on6f400b4: `m2ai_335743db83bb4232b57a`,318 cues,
COMPLETED00:27:05.676Z, all strict predicates and final-file parse/QC/source checksum
PASS. Proof `recurrence-1789861657751744899/final-publication-1789865089081816293.json`.
Together with335-cue output below:2 distinct new formal TC targets, counted once.
They remain on6f400b4 evidence and do not populate any subsequent frozen Gate.
Predeployment00:39:18Z snapshot5 true terminal->next-claim transitions, not24 hours.

## Follow-up encoding classification repair (now deployed above)

On6f400b4, task `m2ai_4709098eb5ad9c7bd956` actually completed00:19:07.054968Z,
then next automatic claim00:20:01.424621Z. Final-location verification proved
**1 new formal TC target,335 cues**, full parse/hard QC/hallucination/source checksum
and all strict predicates PASS; TC destination absent from publication backups,
no duplicate publication. Other language artifacts are not extra delivered targets.
Evidence `recurrence-1789861657751744899/final-publication-1789864028435634327.json`.
00:22:49Z snapshot proves3 terminal -> next-claim cycles, still ARMED. Historical
attempts remain supplemental, not Gate replacements. This proves the preceding
source-ASR normalization defect repaired on a real task, not24-hour acceptance.

A distinct single-source defect was then reproduced, not an unexplained rearm:
`m2ai_c47042257e92ee49e2b3` at00:20:04.789Z, source_inventory hard-QC ->
subtitle_quality -> read_srt UTF-8 decoding at byte26480. A preserved `.error.ja.srt`
candidate raised UnicodeDecodeError as worker_unknown. Independent work continued;
no global trip observed. Current candidate catches ONLY UnicodeDecodeError at
source-candidate QC and records subtitle_encoding_invalid + subtitle_parse_failed.
No replace/ignore decoding, global exception suppression, QC relaxation or retry
budget reset. OSError, unrelated ValueError and UnicodeEncodeError still propagate.

Server `source-encoding-before.log`:13tests/6 expected errors. After repair
`source-encoding-related.log`:172 PASS. Existing leased Docker restart runner
`source-encoding-restart-candidate/restart-cnzim509/` proves candidate review,
actual exit/restart, preserved checkpoint/source/terminal, next claim/Stage and
idempotency; container exported and removed by existing teardown. Real candidate
read-only replay `source-encoding-real-candidate.log` rejects exactly1 undecodable
candidate and selects already valid Japanese subtitles at confidence0.996805.
All1 source video+8 existing sidecars retain SHA256/size/mtime; zero writes/deliveries.
This additional result-affecting change requires another safe deployment and
normal runtime-change Gate policy, not clearance of a new global safety accident.
Do not mix the delivered6f400b4 result into a later frozen cohort.

## Previous deployed baseline —6f400b4 (preserved history)

Second necessary safe deployment `20260920T000919Z-2451937` completed with exit0.
Actual Worker `6f400b4c4818d196785f26b67ae35caefcf472c4`, image
`sha256:ab80ef55949e5548de96916daec6906f53729d12461b637e9170dbaa676679aa`;
source revision `7c273f072f20bac076f7a0ae70a2e525ffc634060f8755f58751ba3f14f180e0`.
WebUI remains `69f2352d0d5d371ca58eaf5a4c62d97f79529e69`, application image
`sha256:63e035bc7d0cac764247a7eb83c7197597a026088e53f335c42e3848fdd46e5b`.
Subsequent document commits are not runtime changes and must not cause deployment.

Existing controlled recovery `m2breakerrec_352653f76f3546c59e480d696fe27f59`
consumed reconciliation `m2-recon-source-normalization-20260920` (receipt SHA256
`0a728f93e720f3ab3e76e87da3e3f8d91ee5fec3481f4dc8112668fd3cc3a2c3`).
It binds the new23:45:34 incident rather than reusing the earlier receipt;
TRIPPED -> ARMED, only the owned hold released at2026-09-20T00:12:30.342903Z.
All104 prior holds plus this unproven source-ASR incident remain (105 total).
Reconciliation retained6717 recoverable queued identities and7373 other-state
identities without resetting budgets/leases; these counts are not delivered jobs.
All52 predeployment backups plus the new one remain (53). Historical preservation
and the missing original ASR acceptance remain UNPROVEN/UNKNOWN, never upgraded.

Active frozen Gate `m2-gate-20260920T001228986223Z-5eee243835`, baseline
`m2-guardrail-v1:0a6d3597e833a5da1b634964`, starts2026-09-20T00:12:28.986223Z.
Configuration fingerprint `sha256:f71f27e11f68a07b316dd5797cc670ec55359d13de13f7d11665872fa7781aac`,
decision schema1, eligibility `m2-frozen-first-20-v1`. Previous233351 Gate is
INVALIDATED_BY_RUNTIME_CHANGE with all evidence retained, not relabelled PASS.
No result transfer, backfill or historical-recovery substitution. Initial and
00:16:27Z snapshot0/20; two historical attempts are explicitly supplemental.
Earliest24-hour end **2026-09-21T00:12:28.986223Z** (08:12:28 Taipei), conditional
also on all20 results, multiple continuations and at least2 genuinely new outputs.
The failed previous observation window does not count toward uninterrupted24h.

Actual-image572 related tests PASS, fresh7/7 breaker tests PASS. Actual Docker
crash/restart restores the accepted SRT/diagnostic checkpoint pair without another
ASR pass, preserves repair budget/source bytes and proves idempotency. Its other
fixture proves safe review -> next claim/Stage. Exact incident canary is read-only:
missing original evidence is refused, not fabricated or published. Restart fixture
`b5d2c8490ec33fcc971db8290551c255884965caaa32cb78997b54198fd26a9c` was exported and
automatically removed non-force, volumes_removed0, no cleanup warning.

Real Production evidence at00:16:27Z: `m2ai_5fa8e3c27f076d9138e1` safely enters
review_required00:13:29.259905Z; next `m2ai_4709098eb5ad9c7bd956` automatically
claims00:14:21.766109Z, preflight00:14:24.091346Z, source_transcription(zh)
00:14:57.489767Z with heartbeat and verified source-decision checkpoint
`9cdeb7885b20fa64b6986230b51aabb016a28ecea95b06bd164f35d2d0852a09`.
No intervening manual retry. One continuation is not sustained acceptance;
actual new qualified formal TC subtitles verified on this baseline:0 so far.
Provider VERIFIED with fresh checked_at1789863381.8710325 under the new baseline;
existing server observer, scheduler and installed cron continue independently.

Current evidence root `/logs/reliability-20260920/recurrence-1789861657751744899/`:
`safe-deploy.log`, `actual-runtime.json`, `actual-image-tests.log`,
`actual-image-proof.json`, `read-only-canary-actual.log`, `recovery-closeout.json`,
`backup-preservation.json`, `observation-check-1789863388143003584.json`.
Restart proof is in sibling `source-continuity-restart-actual/restart-cbizd3kf/`.
Full logs and earlier failed attempts remain on the server, not overwritten.

Final bounded temporary-container inventory at00:18:20Z:
`/logs/reliability-20260920/container-inventory-1789863500848030618.json` finds
0 related stopped containers. Worker/WebUI running; the third related running
container is the labelled --rm inventory helper. Original leftovers discovered5,
confirmed5, archived5, removed5; unconfirmed stopped retained0. The later candidate
restart fixture was separately archived/explicitly removed after allowlist refusal;
the actual-image fixture was automatically removed only after exported results.
These newly created fixtures are not counted as old residuals. Protected services,
volumes, images and all backups were not cleaned or pruned.

## New recurrence (historical repair evidence, now deployed above)

At2026-09-19T23:45:34.896185Z, task `m2ai_99a4c7f0c7371ecb7248` again reached
incorrect_completion/hallucination_validation_pass failure on deployed2a44df9.
The prior ARMED snapshot and one continuation remain historically true, but do
not prove sustained recovery. Breaker stayed TRIPPED during repair; no repeated
rearm or reuse of the preceding receipt. Repair/deployment/recovery are completed
as recorded above; sustained Production acceptance remains pending.

New cause: `_process_source_transcription` formatted accepted source-language
SRT, deleted its now-stale diagnostics and cleared the hold. The source manifest
validator allowed absent diagnostics; strict terminal validation correctly did
not. The exact real transcript has no flagged hallucination cues and unchanged
source/output checksums, but missing original ASR evidence is not fabricated.
It remains a preserved review incident, not a new accepted delivery.

Candidate connects source-language formatting to existing `commit_asr_postprocess`:
first commit the accepted raw checkpoint, then atomically validate/commit the
formatted SRT plus hash-bound diagnostics. Restart restores that pair and skips
ASR. M2 unproven source caches review before translation, full text is checked
before publication, and M2 source manifests cannot accept missing diagnostics.
Non-ASR subtitle sources and non-M2 compatibility stay distinct; no QC threshold,
model route or ASR repair budget was relaxed or reset.

Evidence `/logs/reliability-20260920/`:
`source-continuity-before.log` reproduces the missing evidence (9 tests,11 failed
subtests); `source-continuity-related-3.log`245 PASS after the fix, including full
Worker/source-priority/strict/publication/safety and existing recovery tests.
`source-continuity-restart-candidate/restart-boqdvwhu/state/source-result.json`
proves actual os._exit/restart/checkpoint pair restoration/no repeated ASR;
its `result.json` proves correct review -> next real isolated claim/Stage.
The mount allowlist correctly retained this finished candidate fixture for
explicit verification; full inspect/logs/artifacts exported, then exact ID943bab7e
non-force removed. Original cleanup warning and failed mount-order check retained.
Future candidate runner uses only the existing fixture bind allowlist.
`recurrence-1789861657751744899/read-only-canary-candidate.log` proves the exact
real missing-diagnostic transcript is refused before translation, source and
all original artifacts unchanged, zero Production writes/new deliveries.
The older failed diagnostic/test attempts remain separate, not relabelled PASS.

The interrupted observation baseline cannot satisfy uninterrupted24-hour delivery
acceptance. Preserve its Gate/results; only actual new runtime deployment permits
the existing invalidation/new-baseline policy. Do not restart observation for queries.

## Initial incident (prior baseline; preserved evidence)

Read-only snapshot `/logs/reliability-20260920/diagnostic-1789856641308746202.json`:
Worker d4155dbd0d74c67ebea971beb3e413b3633bf129, WebUI f956b762 unchanged.
TRIPPED at2026-09-19T17:29:10.445402Z (=Sep20 01:29:10 Taipei),
incorrect_completion, initial failed evidence final_state_completed and
hallucination_validation_pass. Task m2ai_5609c69173b0905217a5 took the existing
non-Japanese source-ASR/translation branch. Do not reuse the old SRT receipt.
Worker PID50 live; DB quick_check=ok; no operator or reconciliation pause.
Ledger-dispatchable6727 is NOT proof every source/resource is eligible.
Provider observation is freshly VERIFIED, not the current blocking reason.
Current103 holds/UNPROVEN and51 backups are retained.

### Confirmed causal reproduction

The terminal predicate was a derived failure, not a second proven state-machine
defect: `qualify_strict_output` also clears final_state_completed after
incorrect_completion is reported. An isolated copy of the one incident's rows,
with read-only source/artifact mounts and the original non-secret policy
environment, successfully commits COMPLETED. The original verifier reads only
the canonical Japanese transcript although this existing route actually used
Chinese ASR. Candidate now consumes the already validated, ledger-bound manifest
transcript while retaining accepted diagnostics, identity/hash, hold and full
hallucination-text checks. No QC, source decision, repair budget or original
Gate result has been changed.

Evidence `/logs/reliability-20260920/strict-targeted.log`:7 tests PASS.
`completion-repro-fixed.log`:real preserved manifest valid; completion true;
original hallucination predicate false -> candidate true. This is isolated
counterfactual evidence, NOT a new Production delivery or completed recovery.
Earlier failed diagnostic attempts are retained separately; they are not PASS.

### Temporary container cleanup (actual, not planned)

Five related stopped fixtures were proven finished using commands, mounts,
persisted restart results and historical records:four Worker restart fixtures
and one isolated qB retention/restart fixture. All five were exported, rechecked
and removed by exact64-character ID with non-force `docker rm`.
Archive `/logs/reliability-20260920/container-archive-1789857766846857530/`
includes inspect, full logs, writable-layer diff, fixture/config copies, SHA256
manifest, per-removal progress and protected-before/after comparison.
`container-removal.log`:5 removed;other containers unchanged;170 volumes and51
backups retained. No images, volumes, backups, source or subtitles were deleted.
An initial mount-order comparison failure stopped before deletion and remains
recorded. Cleanup is irreversible for the container objects; archived evidence,
original fixture binds and the qB volume are retained.

Existing `verify_m1_docker_restart.py` now uses durable bind evidence, labels,
owner flock, non-force finalization and bounded next-entry cleanup; pending
restart/active leases are retained. Existing provider restart script has labels,
full evidence/traps and bounded orphan handling. Deployment helper one-shots
remain --rm and now carry the same label vocabulary. Lifecycle regression passed;
its first failure was a test-driver checkpoint inspection through `docker exec`
after fixture exit, fixed to read its persisted bind file. The failed attempt remains.
Do not replay timestamped historical launch scripts; they are immutable evidence,
not supported ongoing verification entrypoints.

Server isolation results before candidate commit:
`related-integration-3.log`:343 PASS (strict evidence, exact incident recovery,
Gate/frozen cohort, provider evidence, admission continuity, queue settlement,
Worker source routes and publication/safety contracts).
`lifecycle-tests-3.log`:7 PASS with actual Docker success/failure, injected timeout
and signal cancellation after start, real restart/checkpoint, live-owner and
pending-restart retention, orphan cleanup/replay, and cleanup-warning recovery.
`deployment-script-tests-3.log`:10 PASS; shell syntax PASS.
All earlier failed test attempts remain in distinct logs. These results do not
attest a new runtime image or fulfill the24-hour Production acceptance.

The existing controlled reconciliation accepts a narrowly typed
source_transcript_evidence_bridge proof, bound to this attempt's immutable result
event/hash and original cohort (possibly older than the active Gate). It still
requires tested code/image, preserved incident hold and history, source/DB/safety
checks, and rejects another trip or old SRT incident proof. No latch/state deletion.

## Focused path/gap checklist

| Path | Existing evidence / current gap | Required action |
| --- | --- | --- |
| Watch/stabilize/deduplicate/claim | Relevant isolated regression PASS; latest real automatic claim/checkpoint | Sustained admission bound remains under24-hour observation |
| Search/match/download/extract/import | PASS reused unchanged Sep12 formal351-cue target, replay queued0 | No new delivery counted; unavailable external sources remain individually blocked |
| TC/CN/JA subtitle and trusted-JA ASR | Related routing/QC/cache regressions PASS; latest real trusted-JA ASR active | Do not turn a running Stage into publication PASS |
| Existing non-JA ASR translation | PASS: accepted diagnostic continuity fixed,2 real335/318-cue strict deliveries on6f400b4 | Retain version attribution; do not transfer into latest Gate |
| Transient failures vs review | PASS relevant tests; exact exhausted readability repair produced real review/next claim on6069fcb; report access failure now isolated onf52118c | Unknown faults/disk/DB/journal/collision still block; no budget resets |
| Publish/mux/terminal/next claim | PASS isolated publication/replay/restart;5 distinct continuations on6f400b4 and2 on6069fcb; runtime mux disabled | Latestf52118c terminal/next-claim and24-hour sustained evidence pending |
| Provider/Gate/observation/restart | PASS expiry/refresh, settlement and real restart; report-access retry budget persists; cron refreshes fresh evidence | Latest ACTIVE enrolled1/20, settled0; fixed cohort and24 hours not passed |
| Historical recovery/fair admission | Real normal/recovery claims retained, budgets/checkpoints not reset | Continued fairness/no starvation is a sustained-observation criterion |
| WebUI waiting reason | Existing underlying reason/time wiring retained; scheduler now processing | No interface redesign; surface future actual admission reasons |
| Temporary Docker lifecycle | PASS7 actual lifecycle/10 deployment-script tests; initial5 removed, final related stopped0 | Labels/--rm/traps/owned-orphan handling in existing entrypoints, no service cleanup |

## First deployed repair and controlled recovery (superseded baseline)

Safe deployment `20260919T232213Z-2029080` exited0. Actual Worker
`2a44df9cdc064fca62ad43aaa43d3714e955698f`, image
`sha256:18ce80362fd1d0f863c915574a927106a2a09f27bd791aec7b1787f1d62e10bb`;
WebUI attested repository version `69f2352d0d5d371ca58eaf5a4c62d97f79529e69`.
WebUI application source/image are unchanged (`sha256:63e035bc7d0cac764247a7eb83c7197597a026088e53f335c42e3848fdd46e5b`);
only its host deployment script gained temporary-container labels. No UI redesign.

Actual deployed-image425 tests PASS in `actual-image-tests-with-host-adapter.log`.
The first actual-image attempt had6 missing host-adapter failures (Dockerfile
does not package the host Bash adapter). That failed log is retained; binding the
exact existing script read-only fixed the test environment, with no redeployment.
`actual-image-restart.log` and `actual-image-cycle/restart-fyu_uoqu/state/result.json`
prove an actual container restart, checkpoint restore, idempotency, source
preservation, safe review and next claim; fixture evidence exported and container
removed. The preserved Production incident replay is read-only, not new delivery.
Fresh actual-image breaker tests7/7 PASS, result
`/logs/m2-guardrail-fi-20260919T233343189438Z-eee531ad/result.json`.
The handoff's mixed stdout/stderr JSON parse failure was preserved and resumed
from immutable completed evidence; no test/Production output was relabelled.

Reconciliation `m2-recon-source-transcript-20260920` retains103 original holds,
adds the exact incident hold (104 total), retains6717 queued identities eligible
for existing source/resource checks and7374 other-state identities without budget
or lease reset. No additional unexplained Queue identity differences arose.
Historical preservation remains UNPROVEN. Recovery
`m2breakerrec_6529bdd51eb9438c86a2dde4aa1c00c3` changed TRIPPED -> ARMED,
released only its own reconciliation hold at2026-09-19T23:33:52.718677Z.
Original51 backups plus new backup remain:52. Old Gate150907 evidence is preserved,
invalidated only for this result-affecting runtime change; no results transferred.

New fixed cohort: `m2-gate-20260919T233351350781Z-92c0c3ec48`, baseline
`m2-guardrail-v1:13f2f82c52ffb23aa46c4bb2`, start
`2026-09-19T23:33:51.350781Z`. Initial0/20. The unchanged observer enrolls only
the first20 eligible current-baseline jobs; failures stay in the cohort. Historical
recovery/pre-gate attempts remain separate; no backfill or easy-success sampling.

First actual autonomous continuation, UTC2026-09-19:
`m2ai_f0d5d61c690f654a08b0` safely review_required23:34:56.106080;
`m2ai_99a4c7f0c7371ecb7248` automatically claimed23:35:45.610042,
preflight23:35:48.663958, source_transcription(language=zh)23:36:12.338649
with matching heartbeat. No manual retry between them. Evidence
`observation-check-1789861016258700893.json`; this is one continuation, not24h proof.

Final cleanup discovery `container-inventory-1789860912375627369.json`:0 related
stopped containers. Two Production services plus the labelled running inventory
helper were retained; the helper itself uses --rm. Confirmed old leftovers5,
archived5, actually removed5, unconfirmed stopped retained0. No volumes, images
or backups removed. `container-removal.log` compares170 volumes and protected
services before/after; backup count subsequently increased solely by safe deploy.

## Existing autonomous observation, not a new framework

Worker's existing M2 observer and immutable SQLite gate/result/stage journals
remain enabled and produce the one-time fixed-cohort terminal report. Existing
UNRAID cron refreshes provider evidence twice per scheduled minute, independent
of Codex. Installed script SHA
`3a6c1808bb12f80e53948b3aaadc1badcfc58080b62f18b0f0d55e15f8fb35c1`;
`installed-observer-cron.log` records the existing persistent entry. Current
provider observation VERIFIED with fresh checked_at1789860989.280377.
The one-shot `observe_existing_evidence.py` only reads indexed existing events
and writes closeout evidence; it is not a new observer, Queue, timer or admission path.

Original minimum end was2026-09-20T23:33:51.350781Z; the recurrence interrupted
this window. Use only the current baseline/time above for new acceptance.
Do not finish at that time automatically: also require all fixed20 results,
multiple terminal/next-claim cycles, at least2 truly new valid formal TC deliveries,
and no unresolved safety incident. Query existing durable evidence on continuation,
not complete Queue/media scans. Publication journals must prove a newly created
TC destination, matching manifest/strict result and source checksum; existing
output revalidation/replacement and multiple language artifacts do not inflate counts.
The prior download/extract351-cue single formal TC delivery is reusable evidence
because those runtime modules are unchanged, but contributes0 to this new-output count.

Two bounded review-boundary checks were preserved in `review-boundary.json`:
the first post-recovery review has no acceptable subtitle (hard QC failed) and
only English audio evidence, so automatic JA ASR is not justified. The older
transient_timeout review retains4 attempts against configured maximum3; it is
budget exhaustion, not a newly discovered unbounded-retry/classification defect.
Do not reset either budget or loosen source/QC checks to increase success counts.

## Persistent acceptance requirements

- [x] Shared root repairs reproduced and tested on UNRAID isolation, tested SHA recorded.
- [x] Relevant integration tests include success/review/fallback/SQLite contention,
      untrusted cache refusal, publication/replay, Gate settle/expiry/refresh/restart,
      real safety events still blocking.
- [x] Safe deployment, attestation and new-incident controlled recovery; no blind rearm.
- [ ] At least24 hours of existing server observation on a fixed20 eligible cohort,
      all results retained and no backfill, adequate-resource admission bounded.
- [x] Goal-to-date5 real terminal->next-claim cycles and2 new strict TC targets on6f400b4.
- [ ] Latest-baseline sustained continuation and new-output evidence, without mixing windows.
- [x] Download/extract and AI publication evidence individually attributable;
      unaffected old351-cue proof reused but not recounted as new.
- [ ] No false completion, source damage, duplicate publish or unbounded retry.
- [x] All isolation/UNPROVEN/backups preserved; no unsupported continuity claim.
- [x] Confirmed obsolete temporary containers actually removed only after archival.
- [x] Success/failure/timeout/cancel/restart/orphan-next-entry cleanup verified;
      services, live tests, volumes, artifacts and backups unaffected.

Status **WAITING_FOR_EVIDENCE** on the repaired baseline. Full Goal and M2
Production acceptance remain incomplete. No M3. Recovery/ARMED and one real
continuation are not sustained acceptance; do not declare the24-hour or output
requirements satisfied before their durable evidence exists.
