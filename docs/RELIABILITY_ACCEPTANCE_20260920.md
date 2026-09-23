# Full-flow reliability acceptance — 2026-09-20

## 2026-09-23 02:56 UTC — post-terminal safety and owned-hold recovery

The deployed Worker is `e30d3c61117508e1c88d47f40c72ce279d7aa2d0`
(`sha256:0ace552cfe597f5028bb90aff2a8c10dacc7ef6bd062d238a8891edee2e7546f`);
WebUI remains `ef5c20e699f1e5a639eb1207f02ce254274c2a73`.
Isolated regressions reproduced two defects: a later attempt after a frozen
member's terminal review was ignored before the breaker, and a planned
runtime-change recovery could arm a Gate but not release this run's own
reconciliation hold. The first repair journals distinct attempts without
changing frozen outcomes or invalidated old Gates. The release repair checks
the exact hold ID, immutable planned receipt, hashed recovery record, runtime
identity and old Gate; wrong owner or receipt remains fail-closed.

The first safe deployment (`20260923T022626Z-141902`) used receipt
`m2-postterminal-20260923`; 324 relevant isolated tests and fresh 7/7 breaker
tests passed. Controlled recovery reached a new ARMED 0/20 Gate but refused
`resume-local` with `reconciliation_release_record_missing`. That failure
remains in `logs/m2-source-id-20260923/postterminal-controlled-recovery.log`;
the hold was not cleared directly. The second minimal patch passed 395 related
tests in UNRAID isolation and 395 again in the actual deployed image. Its
safe deployment (`20260923T024939Z-423517`) retained the hold, all backups
and source holds. Receipt
`/logs/m2-planned-runtime-change-m2-owned-release-20260923.json` has digest
`sha256:05ef3135968d711e34171bc52b13f7291d7f2e6ab64041c1c0e6bef305946a73`.
The final image's fresh 7/7 fault suite affected no Production resource.
Receipt-bound controlled recovery
`m2breakerrec_3b05273fc89c4f8cb4530721580a241d` returned Breaker ARMED,
released only hold `m2-recon-postterminal-20260923`, and resumed claims.
All 107 source holds and 60 deployment backups remain.

The old Gate at 3/20 and intermediate Gate at 0/20 were invalidated by their
actual runtime changes, with no backfill. Current Gate
`m2-gate-20260923T025449876085Z-6d5682c2c1`, baseline
`m2-guardrail-v1:b148a1eb90c1dc7ba65363f1`, began
`2026-09-23T02:54:49.876085Z`. One bounded read-only snapshot records 1/20,
one correctly reviewed claim with preflight/source-selection stages, 0 strict,
and no new formal subtitle. This proves admission resumed, not sustained
terminal-to-next-distinct-claim operation. The deployment/test/fault/recovery
logs are under `logs/m2-source-id-20260923/`; the immutable Gate snapshot is
`post-owned-release-snapshot-1790132182183456481.json` there. A second
bounded snapshot (`post-owned-release-snapshot-1790132421559620201.json`)
shows 2/20, 1 settled review and 0 strict. The first review finished
02:56:11.545679Z; a distinct obligation automatically claimed
02:57:24.259512Z and entered transcription with a heartbeat at
02:57:53.217697Z. This is one actual terminal-to-next-distinct-claim
cycle, not the required repeated sustained evidence. The full-suite
preflight still has two unrelated `config.example.yaml` v1/v2 expectation
failures; this work did not change example config or count that suite as PASS.

A further bounded 03:12 UTC snapshot is ACTIVE 2/20, **both settled review**,
0 strict, ARMED/no pause, 107 holds. The second reviewed obligation's
`deterministic_asr_quality` terminal at 03:05:03Z was followed by its own
bounded transcription retry claim at 03:11:00Z (the normal 300-second
scheduler cadence). It is **not** a third Gate member or another distinct-
obligation continuation. Evidence:
`logs/m2-source-id-20260923/post-owned-release-snapshot-1790133177616909111.json`.
An earlier same-time snapshot's stage/heartbeat fields were not bounded to
each attempt's end; it is retained, marked superseded for those fields in
`post-owned-release-snapshot-correction.txt`, and not used for attempt proof.

At 03:31:07 UTC, a new bounded read-only snapshot recorded the same Gate
ACTIVE at 4/20 enrolled, 3 settled (2 review, 1 strict), ARMED/no pause.
An exact frozen-member read subsequently observed ordinal 4 in review.
Durable claim/terminal/Stage times now prove three different-job
terminal-to-next-claim cycles, not the intervening same-job ASR retry.
Ordinal 3 is **one** newly created formal zh-TW output on this baseline:
`CONVERT_ZH_CN`, 323 parsed dialogues, final hard QC/manifest/output hash
PASS, and a within-claim completed publication rollback journal with
`backups=[]` and the matching published hash. It is not an adopted old file;
it is also not the second required new output or 20-job/24-hour acceptance.
Exact server receipts and timestamp caveats are in
`docs/M2_PRODUCTION_OBSERVATION.md` and
`logs/m2-source-id-20260923/precise-publication-journal-1790134684657260176.json`.
No Worker/WebUI/runtime configuration changed for these checks; no Gate
replacement or deployment occurred.

Version applicability was checked separately for the historical path proofs.
The Sep12 download/extraction case (one new 351-dialogue formal target on
Worker `88598848…`) and Sep20 trusted-JA AI case (one new 402-dialogue formal
target on Worker `f52118c…`) retain their original parse/QC/manifest/source
checksum receipts. They are **historical PASS**, not fresh e30 outputs.
Relative to current `e30d3c6…`, shared publication, source identity/path and
ASS QC modules changed; unchanged qB/Mikan/extraction or translation/ASR
modules alone do not prove the entire current end-to-end publication boundary.
Current Gate ordinal 3's new 323-dialogue `CONVERT_ZH_CN` result verifies the
current conversion/publication route only, not download/extraction or AI
ASR/translation publication. Those two current-baseline route proofs remain
**UNVERIFIED** unless a relevant current-image isolated regression plus
appropriately scoped real final-publication evidence closes the changed
boundary. Do not count the 351/402 historical outputs toward the two new
current-baseline deliveries.

A bounded current-image regression on 2026-09-23 used the actual Worker image
`sha256:0ace552cfe597f5028bb90aff2a8c10dacc7ef6bd062d238a8891edee2e7546f`
after checking the running container's image ID. The isolated container had no
Production mounts, network or GPU, and used a read-only root with a temporary
`/tmp`; `test_m2_download_runtime`, `test_mikan_import_validation`,
`test_worker_source_format_dispatch`, `test_source_priority` and
`test_asr_review_integration` passed **40/40**. Full names, command, exit code
and actual-image attestation are in
`logs/m2-source-id-20260923/m2-path-regression-20260923T035443Z.{log,exit}`.
This closes the missing *test-coverage attribution* of the previous quiet
395-test log, not either current-baseline Production final-publication proof.
Two further actual-image, networkless/read-only/no-Production-mount test runs
recorded **101/101** admission, report-export, frozen-Gate and guardrail
lifecycle tests, and **76/76** source inventory, subtitle QC/path and
publication-identity tests. Their full per-test names, image attestation and
exit codes are under `logs/m2-source-id-20260923/` as
`m2-path-regression-20260923T040257Z.{log,exit}` and
`m2-path-regression-20260923T040532Z.{log,exit}`. Both temporary test
containers were confirmed absent afterward. These runs strengthen current
code-boundary regression evidence, but neither replays a Production target
nor proves current-baseline download/extraction or AI final publication.
An exact-key read-only receipt for Gate ordinal 4 is
`ordinal4-review-1790136430614121337.json`: its frozen first attempt ended
in `short_fragment` ASR quality review; later associated diagnosis records
`repair_attempted=true`, but that later fact is not evidence that the first
attempt repaired or that a viable alternate source exists. The Gate's review
outcomes mean its strict 20/20 safety report cannot PASS, even if all remaining slots
settle and 24 hours elapse; the frozen cohort is preserved for the required
full outcome and continuity assessment.

Status: **WAITING_FOR_EVIDENCE**. The existing server observer must still
establish at least 24 hours from the current Gate start, all fixed first 20
outcomes, repeated terminal-to-next-claim cycles, at least two newly
published strict zh-TW targets on this version, and no unresolved safety
incident. Earlier download/extraction and AI proofs remain separate,
version-labelled history; they do not fill new Gate slots or the two-new-
output requirement. Laya remains disabled for automatic inference and does
not gate subtitle admission. No M3 or M2 Production acceptance.
The existing outbox automatically emits the fixed-20 report at settlement;
there is no separate automatic 24-hour PASS flag. At the window end, assess
the same-baseline durable claim/stage/terminal journals and server machine
reports once, without Codex polling or replacing Gate members.

## 2026-09-22 23:52 UTC — current-image settled-Gate admission boundary

Worker cf82fc27 image
`sha256:2733529ad6c8d56b9edab7e8facccc1c4df43843aa70a8a9098a4130dcf3537f`
was tested on UNRAID in a networkless, read-only-root, no-capability,
CPU/RAM-limited disposable container. The focused
`test_m2_report_export_admission` suite passed **9/9 as non-root**, including
a real report-directory EACCES→access-restored recovery. Report permission
failure is journaled with bounded backoff and does not block a safe next
claim; report collision/hash corruption, ENOSPC/EIO and unknown code faults
still block. A root-mode repeat had 8 passes and the expected skip of the
non-root-only real-permission test; counts are not added together.

The same actual-image 201-test receipt already includes frozen-20 settlement
and write-once/restart tests 17/18 plus
`test_publish_review_terminal_next_claim_and_failed_gate_restart`: a
mixed success/review Gate settles at 10 strict of 20, remains ARMED after
restart, and an additional claim stays outside the frozen cohort without
rewriting its report. Code inspection confirms `admit_new_job` accepts
`ACTIVE` or `SETTLED`, and report permission deferral is separate from
integrity faults. The two newly used `--rm` test container IDs were checked
absent afterward; no Production service, Queue, Gate or media was changed.
Server logs: `logs/laya-20260923/{actual-image-tests.log,
report-export-regression-20260922T2352Z.log,
report-export-nonroot-20260922T2353Z.log,
report-export-cleanup-check-20260922T2353Z.log}`.
This is current-image isolated regression evidence, **not** the unfinished
Production Gate-20/24-hour or two-new-output acceptance.

## 2026-09-22 23:46 UTC — historical download/extraction proof reused precisely

One exact Sep12 formal obligation (`m2dl_f0133acfdbb11284e318`) was checked
read-only at its two final-language paths. Its unchanged publication manifest
still exists with the original SHA-256; the zh-TW and zh-CN files still match
their original receipt hashes, each parses to 351 dialogue events, and each
passes current hard QC with no failure codes. The historical receipt records
one newly served target and two language files, not two target deliveries.

The targeted 88598848→f521 applicability review below already scoped the
later `subtitle_extract.py` JA-metadata branch outside this TC/SC case. A
further f521→current Worker cf82fc27 comparison shows no change to Mikan,
qB, `subtitle_extract.py`, `subtitle_quality.py`, source inventory/adapter or
the official-extraction manifest route. The only additional publication edits
are the separate AI source-normalization path and its legacy/canonical output
policy in `worker.py`, `subtitle_paths.py` and `output_manifest.py`; the
official extractor writes its own versioned manifest. Thus the Sep12 case
remains applicable historical download/extraction final-publication evidence,
not a new cf82 delivery or a frozen-Gate member. The target video checksum was
verified in Sep12 evidence but **not recomputed in this read**; do not promote
this to fresh source-continuity proof. This audit contributes zero new
current-baseline deliveries; the last separate Gate snapshot at 23:37 UTC
reported zero strict publications. Read-only server report:
`logs/laya-20260923/download-extract-reuse-20260922T2347Z.json`.

## 2026-09-22 23:37 UTC — current fixed Gate 4/20

One bounded existing-observer read shows ARMED, no pause/hold, Gate ACTIVE
4 enrolled/4 settled/0 strict (all review), 106 source holds intact. The
fourth Gate task was claimed at23:26:14Z and reached review at23:35:52Z;
five distinct terminal-to-next-claim transitions are now evidenced. The
next claim is not yet due to be judged stalled from a snapshot only 102
seconds after the last terminal result. Observed window1.381h, no formal
new strict subtitle. Continue server-owned observation; do not fill the
four review slots with later successes or call M2 complete. Raw report:
`logs/laya-20260923/observation-check-1790120309184330837.json`.

## 2026-09-22 23:32 UTC — current ASS source candidates remain blocked

Read-only, hash-verified inspection of three exact Gate zh-TW ASS sidecars
found 8/120/30 overlaps above current tolerance. One has only same-style,
same-layer overlaps; the other two include additional QC failures or mixed
overlay patterns. An unconditional ASS overlap waiver is not a safe repair.
Their existing reviews remain genuine quality holds pending content-preserving
evidence. No new formal output or runtime change. See the timestamped overlap
report in `logs/laya-20260923/` and `docs/M2_PRODUCTION_OBSERVATION.md`.

## 2026-09-22 23:24 UTC — source path of three current Gate reviews

An exact-three, read-only audit found intact persisted source decisions:
6/4/3 subtitle candidates, zero eligible after existing hard QC, complete
source inventories, and trusted Japanese audio chosen for ASR in each case.
`timing_overlap` occurs in all 13 rejected candidates. The current
deterministic-ASR-quality reviews therefore have no evidence of a skipped
*verified* subtitle route. Timing repair remains unproven; no QC exception,
Worker deployment, Gate change or new formal subtitle resulted. Raw bounded
evidence: `logs/laya-20260923/source-choice-gate3-20260922T2325Z.json`.
The existing 24-hour window, all fixed-20 outcomes and two new strict
publications remain outstanding; M2 is NOT COMPLETE.

## 2026-09-23 superseding checkpoint

The earlier healthy snapshots below are historical, not current acceptance.
Sep21 normalizer rename -> absent manifested output -> incorrect_completion repaired
by actual Worker cf82fc27 / WebUI ef5c20e6 safe deployment and exact controlled recovery.
201 actual-image tests + real restart +7 fresh breakers PASS,106holds/all57backups
retained. At22:26Z ARMED,2real terminal->nextclaim continuations, thirdtaskASR.
OldGate014830 remains20/20,1strict/19review/FAIL; new221442 Gate1enrolled/0settled/0strict.
Old invalid receipt not rewritten. Laya E remains advice only (0/7 historical matches),
18tests+4real faults PASS; generic missing cause markedUNAVAILABLE. No Worker/Gate
change for sidecar E. Formal new0 and sustained24h/2new outputs still pending.
See `LAYA_DIAGNOSTICS_20260923.md`; NOT full-flow acceptance.

Active Goal replaces the previous one-claim recovery closeout. Overall NOT COMPLETE.
No M3, full-library rescan, broad Queue retry, QC relaxation, source/valid-output
overwrite, backup deletion, or ASR-budget reset. Server must own the eventual
24-hour observation using existing observer/ledger; not a second framework.

## Low-frequency live window (05:30 UTC; same runtime and Gate)

`observation-check-1789882229325759596.json` at05:30:28.983Z: same f52118c/imagee624e0e8,
ARMED/runtime_baseline_match, provider freshly VERIFIED,105 holds, no pause or
reconciliation hold. No new trip. Gate014830 remains ACTIVE:18 enrolled/17 settled,
1 strict COMPLETED,16 NEEDS_REVIEW and1 processing. All27 attempts fit the bounded
sample:17 distinct automatic continuations,9 same-obligation repairs separate.
Latest different-task sequence:42270fc4c1ce7b62f3aa quality review05:29:08.729144Z
-> 2b92ab830b2b2e76d60a claim05:30:03.799858Z -> verified subtitle-detection
checkpoint05:30:09.140651Z -> language-detection Stage/heartbeat05:30:13.638267Z.
Checkpoint0598331bac9c96eda7adacf18b451680e3c697a853fdb197622b304ecacc504c.
Do not promote a running Stage or review into a delivery. Current-baseline new
formal TC count remains1;3.700 hours is not24-hour acceptance.

Only the known live main process was monitored between snapshots. `window-0530.*`
records04:34:40..05:29:40Z; exit124 is the read-only waiter's deadline, not a Worker
exit/signal. No full Queue/media scan, Docker-log polling or Production mutation.
No runtime deployment/Gate reset. All20 outcomes, second current-baseline new target,
and24-hour sustained safety/admission evidence remain WAITING_FOR_EVIDENCE.

## Low-frequency live window (04:30 UTC; same runtime and Gate)

`observation-check-1789878621438532173.json` at04:30:21.058Z records f52118c/imagee624e0e8,
ARMED/runtime_baseline_match, provider freshly VERIFIED,105 holds, no pause or
reconciliation hold. The latest historical trip is still the exact planned deployment
event, not a new incident. Same014830 Gate:15 enrolled/14 settled,1 strict COMPLETED,
13 NEEDS_REVIEW and1 running. All21 attempts fit the bounded32-attempt sample;
14 distinct terminal-to-next-claim/Stage sequences and6 same-obligation repairs are
reported separately. Elapsed2.698 hours is not24-hour acceptance.

Previously queued task412fb3a870b8085a15de actually re-claimed03:53:58.679976Z,
entered ASR03:54:02.708651Z, had durable stage heartbeat04:01:10.075241Z, then
safely reviewed04:01:12.941462Z. A different task9fef3fec7a916f33cb96 automatically
claimed04:03:08.793656Z. No manual retry, budget reset or failed-member substitution.
Later task976623019d9c89f877ca reviewed04:25:57.977265Z -> different
bfc73c9bacadb733f48c claimed04:26:51.061177Z -> verified checkpoint04:26:57.045961Z
-> actual JA ASR04:27:24.805481Z -> active bounded-repair heartbeat04:30:04.972103Z.
The running repair is not another publication or a new Gate member.

Between snapshots, only the confirmed live main PID3357554 was monitored with a
bounded read-only tail--pid wait. `window-0430.*` records03:54:40..04:28:40Z,
waiter exit124 at its deadline (not Worker exit or a Worker signal). No Queue, media,
WebUI or Docker-log polling during that interval. Existing server observers continued.
No code/configuration/runtime/Gate change or extra cleanup. New current-baseline
formal TC count remains1, verified below; historical2 deliveries remain separate.
WAITING_FOR_EVIDENCE: all20 results, second current-baseline new target, and24 hours.

## First current-baseline formal publication verified (03:36 UTC)

No code, configuration, container or Gate change. Worker remains
`f52118c564e9aebca89d52b336e2156d2ba095cd`. Read-only snapshot03:30:15.878Z:
ARMED/runtime_baseline_match, provider VERIFIED,105 holds; same014830 Gate has
9 enrolled/9 settled:1 strict COMPLETED and8 NEEDS_REVIEW, still ACTIVE. All13
attempts fit the bounded32-attempt sample:8 distinct terminal-to-next-claim/Stage
sequences and4 separately counted same-obligation repairs. These are not24-hour totals.

Actual task `m2ai_4fea57818662cbb6a3a8`, frozen ordinal8, claimed02:58:08.829388Z
and succeeded03:09:06.792990Z via trusted-JA ASR. Its completed atomic publication
transaction03:08:31.359489Z..03:08:32.638294Z has no prior destination backups;
all3 published destinations were new. Count **one new formal TC target,402 cues**,
not three deliveries for the JA/CN/TC companion files. Final-location re-read and
current runtime verification passed parse, hard QC, hallucination and original
source-checksum comparison. All11 strict evidence fields are true; no duplicate
job/publish or unresolved retry/quarantine/fallback. The read-only verifier neither
reran the task nor enrolled/replaced any member or rewrote existing outputs.

- TC SHA256 `538f3d0e2400d39c958726db191eadbb8bdded1152750a6777318cad5f49e4b2`.
- Formal manifest `/work/ai_output_manifests/4f/4fea57818662cbb6a3a821c057c078b94c5426e650d712e7ec6a519e819a2f72.json`,
  SHA256 `a8c1358994c3f04315f5ac083d60b0724b72562213b0c8035eebcdfca29f87ae`.
- Publication journal `/work/ai_output_versions/447bb1c64eaf8960/1789873711352528096/manifest.json`,
  SHA256 `074367c1619e1f031ccd2d00a695b84fc2dabcfb2805ee04c79ae933dad07157`.

After success, different task `m2ai_47d31a663b7639ec61f8` automatically claimed
03:10:05.682414Z, entered preflight03:10:10.488388Z, saved verified subtitle-detection
checkpoint/heartbeat03:10:11.558768Z, then actual JA ASR03:10:29.726655Z. Checkpoint
`7b1f77a7a91dd890dcb2c893eb05801e435b3ec34ea1a54ade4f59c65651dcd6` is verified.
Its durable ASR stage heartbeat03:17:17.994753Z precedes safe quality review
03:17:21.794628Z. The mutable job heartbeat was later updated by bounded repair;
this proof uses the timestamped stage-attempt row, not a later attempt's heartbeat.
No manual retry, new pause, breaker recovery or scheduling override was used.

Evidence under `/logs/reliability-20260920/report-export-handoff-20260920/`:
`observation-check-1789875016203506347.json`,
`final-publication-1789875385068802312.json`, `final-publication-4fea-verification.log`.

Additional read-only source-decision/preservation proof at05:34 UTC:
`review-decisions-1789882229797071628.json` exports exact pre-publication decision
effe79a9f0d647aa9afa468888d476c0, created02:58:27.262614Z. SHA256
951166bc141dde7ff44185d5c446d313c0746cd0ab1591b7ccf56bb82bfe097a matches the saved
provenance. Its inventory was complete:18 subtitle candidates,0 eligible; the four
TC/CN sidecar aliases failed existing hard QC with invalid_timing (duplicate content
also identified). Two audio tracks were evaluated; selected JA confidence0.96,
reason trusted_japanese_audio_no_usable_subtitle. Existing subtitles were present,
but no valid directly usable TC candidate was recorded; this is not revalidation
of an already valid subtitle or a filename-only inference of a missing target.
`prior-sidecar-preservation-1789882466835961673.json`/`prior-sidecar-preservation-4fea.log`
verify all17 prior sidecar files still have the original decision's SHA256/size/mtime.
Those expected fingerprints predate publication; current hashes are not substituted
for historical evidence.0 source writes,0 new deliveries counted by this extra check.

Current-baseline new formal TC targets:1. Goal-to-date:3, of which2 belong to the
separate6f400b4 baseline and are not transferred into this Gate. Sep12 download/
extraction evidence remains historical/reused, not a new delivery. Remaining:
second new output on this baseline, all fixed20 results, and continuous24-hour
safety/admission evidence (earliest2026-09-21T01:48:30.021232Z). Server observation
continues autonomously. WAITING_FOR_EVIDENCE; Goal and M2 are not complete.

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
At02:02:23.239434Z the same obligation's bounded ASR remediation automatically
claimed, entered transcription02:02:31.801830Z and produced Stage/heartbeat evidence.
02:02:50Z snapshot still ARMED, enrolled1/20, settled1/strict0. This is one same-job
repair, not a distinct next-job continuation, new cohort member or delivered subtitle.
At02:11:58Z the latest-baseline distinct terminal/next-claim is now proven:
the bounded repair of7a642b2574cf708e9e19 ended in deterministic_asr_quality review
02:09:48.573166Z; different taskbc57ccafc7143aec78c8 auto-claimed02:10:40.485752Z,
entered preflight02:10:43.577813Z and updated Stage/heartbeat02:10:45.203803Z.
Its candidate_analysis_inconclusive review settled02:10:47.546120Z without blocking
independent work. Task25f59843d061fba1c2a3 then claimed02:11:45.322203Z;
this snapshot has not yet captured its Stage, so it is not a second verified cycle.
ARMED/runtime_baseline_match, fresh provider VERIFIED, Gate3 enrolled/2 settled/
0 strict. One distinct continuation and one same-obligation repair are separate;
new formal subtitles on this baseline remain0. Sustained24-hour/all20 proof is pending.

Final bounded snapshot02:13:47.791Z proves3 distinct automatic continuations onf521:

| Previous terminal review (UTC) | Next anonymous task | Automatic claim | Preflight / heartbeat |
| --- | --- | --- | --- |
| 02:09:48.573 | m2ai_bc57ccafc7143aec78c8 | 02:10:40.485 | 02:10:43.577 /02:10:45.203 |
| 02:10:47.546 | m2ai_25f59843d061fba1c2a3 | 02:11:45.322 | 02:12:01.823 /02:12:02.237 |
| 02:12:05.368 | m2ai_60682c962f8bd3a320c8 | 02:13:07.078 | 02:13:09.503 /02:13:10.206 |

No manual Retry between these events. The last task also reached source-selection
review; this does not establish successful publication. Gate4 enrolled/4 settled,
all4 NEEDS_REVIEW, strict0, no substitution. Runtime/breaker ARMED, provider freshly
VERIFIED, no pause/reconciliation hold. Scheduler idle between completed cycles is
not evidence of another admission outage. New-baseline formal outputs0;25 minutes
of observation is not24 hours. Existing server observation continues independently.

Evidence `/logs/reliability-20260920/report-export-handoff-20260920/`:
`actual-runtime.json`, `actual-image-proof.json`, `actual-image-tests.log`,
`fault-suite.log`, `recovery-closeout.json`, `backup-preservation.json`,
`observation-check-1789869064933116458.json`,
`observation-check-1789869499543303553.json`,
`observation-check-1789869770166730449.json` (bounded automatic same-job remediation),
`observation-check-1789870318559243726.json` (first distinct continuation),
`observation-check-1789870428080672824.json` (3 distinct continuations, fixed4 reviews).
Actual restart
`../report-export-final-restart-actual/restart-xk15hswm/state/result.json`, fixture
6ac102bc... removed after export, volumes_removed0. Latest related stopped containers0
in `../container-inventory-1789869063852915091.json`; initial5 obsolete removals unchanged.

The one-shot read-only closeout now bounds Stage/heartbeat evidence to each claim's
own terminal time and separates same-obligation repair from distinct next-job claims.
It samples the latest32 attempts/latest40 result events and reports indexed window
counts and truncation explicitly; sample continuation counts are not full24-hour
totals. The fixed Gate's complete member evidence remains separately included.
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

## Follow-up of three shared source-review decisions (02:25 UTC, no runtime change)

Only the three exact durable decisions following the first ASR repair were read;
no jobs were rerun and no library/Queue scan was performed. Source inventories
were complete, but no candidate passed existing eligibility/QC:

| Anonymous task | Verified rejection evidence | Disposition |
| --- | --- | --- |
| m2ai_bc57ccafc7143aec78c8 | TC/CN timing overlaps; JA overlap/CPS failures; only English-tagged audio | Preserve review |
| m2ai_25f59843d061fba1c2a3 | TC ASS timing overlaps; TC SRT parse failure; only English-tagged audio | Preserve review |
| m2ai_60682c962f8bd3a320c8 | JA content fails overlap/CPS/min-duration QC; audio language unknown | Preserve review |

These3 are genuine exceptions under current safety limits, not proven FALSE_REVIEW.
High language/coverage confidence does not override hard QC or establish trusted JA
audio. Re-evaluation requires new validated candidate/identity/audio evidence; these
records are not permanent declarations that no subtitles exist.

Two exact TC candidates were SHA/size/mtime matched to the durable decisions and
copied into UNRAID isolation. Current deployed imagee624e0e8, no network/GPU/live
DB/media mounts: SRT has3 overlaps of3.46/3.78/2.70 seconds; existing bounded
remediation records `no_safe_change`, and idempotent replay leaves its immutable
diagnostic unchanged. ASS has15 overlap findings, including0.47..4.56 seconds;
the SRT-only remediation does not accept ASS, and converting syntax would not
resolve the over-budget timing. Policy remains200ms per overlap/single shift,
500ms total. No repair allowance, QC threshold, source subtitle or job budget changed.
The test correctly retains QC FAIL; it is not successful subtitle repair/publication.

Evidence: `report-export-handoff-20260920/review-decisions-1789870690852646228.json`
and `/logs/reliability-20260920/review-timing-feasibility-20260920T0223/`.
`isolated-test-2.exit=0`, `output/result.json`, immutable remediation diagnostic,
`sources-unchanged.json` prove both original sidecars' SHA/size/mtime unchanged.
Original failed `isolated-test.log` remains: the diagnostic fixture initially compared
whole return objects, incorrectly rejecting the expected `already_attempted` replay
status. Corrected fixture compares fingerprint/hash/artifact bytes/mtime instead.
The shell fixture's no-newline CID read was corrected; remaining source verification
and exact-container absence checks were completed separately, without rerunning jobs.
Both temporary fixtures used labels/--rm; `container-1-after.txt` and
`container-2-after.txt` are empty authoritative exact-ID listings. No container/image/
volume/backup prune, source write or Worker restart was performed.

Latest read-only snapshot `observation-check-1789871107847594257.json` at02:25:07Z:
samef521 runtime/ARMED, provider VERIFIED,105 holds, no pause/reconciliation hold;
Gate6 enrolled/6 settled/all NEEDS_REVIEW/strict0, no new formal outputs. Five distinct
terminal->next-claim/Stage sequences are present; four have bounded matching
heartbeat rows in this snapshot. The fifth has durable preflight, audio selection,
language detection and ASR02:15:30.890Z->review02:22:34.429Z, but its mutable
heartbeat row is no longer inside the attempt interval; do not invent that evidence.
The last terminal is only150 seconds before this snapshot, inside the existing
300-second watch interval; do not infer another outage or force Retry from idle alone.
The server's24-hour/fixed20 observation remains WAITING_FOR_EVIDENCE. No deployment,
baseline/Gate change or Production repair was justified by this bounded review check.

## Scope of reused Sep12 download/extraction evidence (no replay or new delivery)

The351-cue TC/SC publication remains a historical88598848 result, not an f521 test
or a current-Gate member. A targeted comparison from full SHA
88598848ebf2efaf945a9128c320ba188074f3b6 to f52118c564e9aebca89d52b336e2156d2ba095cd
corrects the earlier blanket statement that every related runtime module is unchanged:

- Mikan worker/matcher/source/cache/fallback/reviewed-repair, qB client, config,
  subtitle quality, ASS/SRT helpers, language projection, subtitle paths, safe files,
  resource scheduler, lock, control state, pipeline state and remediation are identical.
- `subtitle_extract.py` has one21-line change from70853f3: a JA-metadata/dominant-kana
  classification branch. The original durable extraction diagnostics have TC metadata
  zh-tw, Japanese score449/CJK3491 and SC metadatazh-cn,449/CJK3486. Neither satisfies
  the new branch. The import-validation, parallel-Chinese projection and publication
  implementation are unchanged. Original failed722-event candidates remain failed;
  the normalized351-cue outputs and their original parse/QC/checksum/replay evidence
  remain the only accepted artifacts from that case.
- Shared source inventory/analyzer are not byte-identical to88598848: versioned
  identity/digest ordering and per-candidate hard-QC evidence were added. The import
  path calls `_probe_media`/`_subtitle_metrics` and `analyze_subtitle_candidate`, not
  AI inventory selection/adapter fallback. The metrics change canonical digest order,
  not cue text/timings/count; the new optional hard-QC field defaults toNone for this
  import input and its unchanged independent hard-QC check still gates acceptance.
  Do not reuse old decision/checkpoint identities under the new inventory version.
- Source classifier/inventory/analyzer/adapter and relevant regression files are
  byte-identical between6069fcb andf521. The6069 actual-image748-test evidence includes
  `test_m2_review_source_regressions`, including Chinese/unknown/bilingual rejection
  of a misleading JA hint. This is explicitly6069 evidence, not a claim that f521 ran
  748 tests; f521's own actual-image386-test admission/report evidence stays separate.

Server evidence `/logs/reliability-20260920/download-evidence-reuse-diff.log`,
`download-evidence-reuse-shared-source.diff`, `download-evidence-reuse-tested-boundary.log`;
original diagnostics are in the Sep12 `formal-canary/verify-1789246297869603050.json`,
job.result_json.entry_results[3583:2].subtitle_diagnostics. Subtitle-extract Git blob
db8b5a5bf0157a12b4f0970ea1b83ac133a4f677 (885) differs from
44a01a87633f3fa3e426c5d7ed860a335b0db43f (both6069 andf521).
This scoped applicability audit does not re-download/re-extract/re-publish, reread
the entire media library, mutate a receipt, assert fresh source continuity, or add
to new-output/Gate counts. No new runtime defect or deployment was justified.

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
| Search/match/download/extract/import | PASS reused Sep12 formal351-cue target, replay queued0; TC/SC branch applicability checked above | No new delivery counted; unavailable external sources remain individually blocked |
| TC/CN/JA subtitle and trusted-JA ASR | Related routing/QC/cache regressions PASS; f521 trusted-JA ASR produced1 new402-cue strict TC target, verified at final location | Subtitle-route regression and reused download evidence remain separately attributed; one output is not sustained acceptance |
| Existing non-JA ASR translation | PASS: accepted diagnostic continuity fixed,2 real335/318-cue strict deliveries on6f400b4 | Retain version attribution; do not transfer into latest Gate |
| Transient failures vs review | PASS relevant tests; exact exhausted readability repair produced real review/next claim on6069fcb; report access failure now isolated onf52118c | Unknown faults/disk/DB/journal/collision still block; no budget resets |
| Publish/mux/terminal/next claim | f521 formal manifest/journal/final QC PASS; success03:09:06Z -> different claim03:10:05Z/checkpoint heartbeat03:10:11Z/JA ASR03:10:29Z; runtime mux disabled | At05:30Z17 distinct continuations are in the bounded27-attempt sample,9 same-task repairs separate; second current-baseline delivery and24-hour proof remain pending |
| Provider/Gate/observation/restart | PASS expiry/refresh, settlement and real restart; report-access retry budget persists; cron refreshes fresh evidence | At05:30Z same ACTIVE Gate18 enrolled/17 settled,1 strict/16 review/1 running; all20 and24 hours not passed |
| Historical recovery/fair admission | Real normal/recovery claims retained, budgets/checkpoints not reset | Continued fairness/no starvation is a sustained-observation criterion |
| WebUI waiting reason | Existing underlying reason/time wiring retained; bounded03:41 check distinguished an inter-job idle gap from actual admission refusal | No interface redesign; surface future actual admission reasons |
| Temporary Docker lifecycle | PASS7 actual lifecycle/10 deployment-script tests; initial5 removed, latest targeted inventory had0 related stopped containers | Labels/--rm/traps/owned-orphan handling in existing entrypoints, no service cleanup |

The03:51UTC sample is `report-export-handoff-20260920/observation-check-1789876303638821103.json`.
Taskm2ai_412fb3a870b8085a15de was independently confirmed live (host child183983),
then naturally exited03:48:19Z within a single720-second read-only process wait.
Durable records show ASR quality review03:48:18.784072Z, not publication. Existing
revision-bound autopilot queued its permitted repair03:49:00.107752Z; the03:52
exact query still showed queued, not a verified second claim. No forced retry,
Worker signal, budget reset or false-success count. Detailed evidence:
`task-412fb-wait.*`, `review-resumption-1789876350030845287.json`. Whole-window
totals must eventually come from the existing journals, not an assumed extrapolation.

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
`installed-observer-cron.log` records the existing persistent entry. The
first-baseline provider observation was VERIFIED with checked_at1789860989.280377;
this historical timestamp is not current freshness evidence. Current-baseline
freshness is separately captured in the timestamped observation snapshots above.
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
for the scoped TC/SC path as checked above, not a blanket assertion that all modules
are unchanged. It contributes0 to this new-output count.

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
- [x] Historical6f400b4 has5 real terminal->next-claim cycles and2 new strict TC targets;
      these remain separately versioned and do not transfer into the current Gate.
- [x] Latestf52118c has real terminal->next-claim/Stage/checkpoint-heartbeat proof and
      its first new strict402-cue TC target, final location independently verified.
- [ ] Latest-baseline sustained continuation and at least2 new formal TC targets,
      all fixed20 results and24-hour safety/admission evidence, without mixing windows.
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
# 2026-09-23 bounded diagnostic closeout

At23:13UTC the same Gate had3 enrolled/3 settled/0 strict: three distinct
ASR obligations ended NEEDS_REVIEW. Immutable rejected SRT checksums match
their manifests; each has7/14/9 short-fragment ranges in476/471/403 cues.
Only1 per obligation is numerically eligible to join its predecessor under existing max duration,
max characters and fail CPS. The others include isolated/punctuation-only
or over-limit fragments; an automatic join cannot safely resolve them all.
This is a quality limitation, not evidence to relax QC. Source checksum is
unproven in the strict records; the audit made no media changes. Full bounded
evidence and fixed-cohort treatment: docs/M2_PRODUCTION_OBSERVATION.md.

Latest authoritative details: docs/LAYA_DIAGNOSTICS_20260923.md, final bounded SDK
audit section. Laya inference disabled/MODEL_QUALITY_NOT_ACCEPTED; fixed7 cases0/7
in original and reversed order; no SDK/adapter mapping or truncation defect. Separate
collector oversized-line defect fixed and24 tests+actual Docker restart PASS.
Three archived old diagnostic containers removed, all5 current temporary IDs absent,
one E rollback retained; no Worker/WebUI/Gate/backup/source/output changes.
23:01:11UTC existing observer evidence observation-check-1790118071734932065.json:
samecf82/ef5, ARMED,106 holds, sameGate2214422 enrolled/2 settled/0strict,3 distinct
continuations+2 same-task resumptions,0new qualified outputs,0.775h observed.
Still WAITING_FOR_EVIDENCE for >=24h/all20/>=2strict new publications and real canonical
publication boundary. No M2 acceptance. Do not use model success, rules-only results,
review/resumption or existing subtitles as new delivery. The exact two-task ASR
repair-budget/checkpoint follow-up is now recorded in
`docs/LAYA_DIAGNOSTICS_20260923.md` and
`logs/laya-20260923/asr-budget-evidence-1790118636067764775.json`: manifest hashes
match, selective repair was already attempted, each full fallback was queued once,
and unchanged failure revisions block duplicate remediation. Both remain review.
At23:08:39UTC the same Gate was3 enrolled/2 settled/0 strict, fourth distinct
terminal-to-next-claim sequence entered transcription. No new formal subtitle.
