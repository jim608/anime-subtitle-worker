# Incremental Delivery Plan

## Active Goal — M2 download/extraction acceptance and frozen Gate disposition

Sep12 20:05UTC: evidence-bound single-job repair candidate implemented through
mikan.requeue_extract; atomic original-row archive+pending+existingQueue transition,
preserved budgets/exclusions, claim/publication source+runtime guards and manifest
binding. Server489 testsPASS; real4-container loss/restart/resume and freshreal
ffmpeg351cue fixturePASS. NOTdeployed, formalAI0/download0/extraction0, M2 NOTCOMPLETE.
Live19:40Worker5e73636c.../ARMED/70holds/UNPROVEN, Gate183251 ACTIVE10/10 retained.
Nextone safehandoff+actualimage/fresh7+controlledrecovery+realc1102formalcanary.
See docs/M2_REVIEWED_EXTRACTION_RECOVERY_20260912.md. No M3/M4/Gate20wait.

Sep12 19:32UTC: narrow parallel-CN/JP ASS candidate passes74 local/server tests
and real isolated ffmpeg/import/publisher, cross-container idempotency and
source-hold refusal. Keeps380 text lines;351cue output passes original QC.
Candidate NOT deployed; real formalAI0/download0/extraction0; M2 NOT COMPLETE.
Actual5e73636c.../WebUI175a02a7.../ARMED/70holds/UNPROVEN unchanged; Gate183251
ACTIVE7enrolled/7settled. Original20 FAIL report hash reverified; no Gate mutation.
Next: evidence-bound one-shot re-extraction for the exact replaced c1102 job
through existing single-job recovery, then one safe handoff/real formalcanary.
No manual DB rewrite, failed-hash removal or old receipt reuse. Details and
test/lineage evidence: docs/M2_PARALLEL_SUBTITLE_REPAIR_20260912.md.

Sep12 18:42UTC: progress fix deployed at Worker5e73636c317cc32919ce1a8104dafc0d05ce0b5c.
Safe deployment20260912T182521Z-306768/controlled recovery exit0, actual-image421
tests +fresh7/7 breakers PASS; runtimeARMED,70holds/UNPROVEN retained,0new deltas.
OldGate173915 INVALIDATED with4members retained; newGate183251 starts18:32:51.418961Z,
baselinea4504d3092426d19e4ab433d,0/20. Original settled20 FAIL unchanged.
Actual historical AI automatic claim1789238322.5520434 reachedASR/heartbeat and
validd7510126...checkpoint, no manualretry/wake or forced jobtermination.
Third sourcec1102 download+extract proved butparsePASS/hardQCFAIL372overlaps;
all3failedsources excluded, target+18sidecars unchanged,0newmanifest/verifiedTC.
M2 NOT COMPLETE: formalAI0/download0/extraction0. New-runtime download/extraction
claim, suitable QC-valid source and trueformalmanifest/lineage/finalQC/dedup remain.
No repeat handoff, QCchange, Gate20wait or M3/M4; docs-only sync doesnotredeploy.
Evidence /logs/m2-progress-identity-deploy-20260912T1815/ and latestacceptancedocs.

Closeout18:08: progress fixd09754248581a226e3a17444a9a9656622b5a864 pushed/257serverPASS,
notdeployed: freshplanned_change_work_not_idle, existingtranscription preserved,
no newpause. Actual72f6e5e/ARMED/currentGateunchanged. Finalformalcheck0newmanifest;
thethirdsource'sJune successrowisnotnewdelivery. Nextsafeidlehandoff+realTCproof.

Sep12 18:03UTC: M2 NOT COMPLETE. Actual72f6e5e/ARMED/70holds, sameGate173915
ACTIVE2/1 at bounded snapshot. Real historical3583:2 retained-download claimattempt2
and automatic nextsource35a7 download393129896bytes/100%+extract both failed original
hardQC;0formalmanifest/0newTC, source+18sidecars unchanged. Thirdsourcec1102 genuinely
41%downloading, not its oldJune success row. Two separate AI automaticclaims reached
transcription; first precedes optionalwake and hasvalidsavedcheckpoint. EP6 refused
revision/exclusion-history difference retained;10othermissingTC obligations remain.
New reproduced progressidentitybug mixedold100% withnew41%; minimalcandidate
_torrents_for_pending fix/test_mikan_progress_identity.py, local257PASS, actualoldimage
4/5regressionsfailedasexpected, servercandidate257PASS/exit0 at18:02:59. No newdeployment
for thisfixyet; don'tkillAI/resetGate/restarttests. Finishserverproof thenexisting
safeidlehandoff; continueactualthirdsourcebounded, formalartifactQC/lineage/dedup
stillrequired. Logs/docs latestsection holds exactIDs; noM3/M4/Gate20wait.

Sep12 17:40UTC: Worker72f6e5e4c6a06aa418045ebef645480741da7112 pushed and
safe-deployed20260912T173219Z-4047011; actualimage416 tests +fresh7/7 breakers PASS.
Existing authorized reconciliation recovered ARMED, retaining truthfulTRIPPED
origin/incorrectcompletion/oldreceipts/backups/UNPROVEN. One new unverifiedQueue
identity retainedPENDING_REVIEW;69prior+1new=70holds.6841recoverable/7234retained
are not deliveries. OldGate152911 INVALIDATED with8members; newGate173915 starts
2026-09-12T17:39:15.744898Z at0/20, baselineb783ee77eb48c7ecf6e51e46; no backfill.
Retained-download revalidation uses existing public API/Queue, exact current
checksum/member/completeqB/matching checks, archives oldreceipt, retainsretrybudget,
neverre-adds torrent. Exact12preflight:1validTCkeep/11missingTC reusable sources.
M2 NOT COMPLETE: formalAI0/download0/extraction0. Next exactcanary automaticclaim,
formalTCmanifest/finalQC/source+sidecarhash/lineage/dedup;10othermissing obligations
remainexplicitlyoutstanding. NoM3/M4/Gate20wait. Fullserverlogs andreceipt IDs in
latest docs/M2_REMAINING_ACCEPTANCE_20260908.md section.

Sep12 16:52UTC: M2 NOT COMPLETE. Real collection finished/extracted but aggregate
count1 falsely completed12 members; target3583:6 has no verified TC/new manifest,
source checksum+17 sidecars unchanged. Incident persisted via existing breaker API:
TRIPPED. Existing owned reconciliation hold m2-recon-batch-completion-20260912T1645
contains admission; active AI Queue jobs0 at bounded snapshot. Runtime stillad5bdad,
WebUI175a02a,69holds/UNPROVEN; same Gate152911 ACTIVE8/8, no reset/acceptance/wait.
Candidate minimal per-member results, valid-subtitle preservation and Mikan breaker
admission:369 server targeted tests PASS plus real ffmpeg/ffprobe preservation and
SQLite latch/restart replay. Not deployed; no formal AI/download/extraction additions.
Next engineering work: controlled tripped-runtime handoff (not old ARMED-only script),
exact12 completion revalidation reusing full download, safe deployment/recovery,
formal artifact proof. Details in latest remaining-acceptance and observation docs.

Sep12 16:02UTC: read-only exact-source mapping verified on ad5bdad/ARMED/69holds.
Download801,386,877bytes/17.183%; EP06 correctly selected/readable but only16.3033%
complete despite full allocated file size. No partial-media decoding/publication.
Same Gate152911 ACTIVE2/20, no final summary/wait. Read-only formal checker with
11 local/server receipt checks PASS; live result0 manifests/0 extraction jobs,
WAITING_FOR_FORMAL_PUBLICATION. Actual source-lineage/ledger/dedup remain mandatory
after future file checks. No runtime/config/deploy/new request; M2 NOT COMPLETE.
Details and full server evidence in latest remaining-acceptance doc.
Existing resolver also matched exact S01E06 via indexed target at16:06UTC
(target-resolution-1789229183446329828.json), without partial-media decoding or
global fallback. Metadata/path match is not an executed extraction/publication.

Sep12 15:42UTC: ad5bdade07d000ca28160cc38b919120700f10af is actually deployed
and controlled recovery completed ARMED. Deployment20260912T152306Z-2976210 exit0;
actual-image340 tests, real collection metadata replay/Docker restart and fresh
7/7 breakers PASS.69 holds/UNPROVEN retained,0 new handoff differences;6850
recoverable/7225 retained identities are not deliveries. Old Gate145452 is
INVALIDATED_BY_RUNTIME_CHANGE with all4 members unchanged. Earlier failed
Gate125703 and immutable20-member summary remain unchanged. New Gate152911 starts
15:29:11.952471Z/baselinef8f95cbe280c55ab570cbe05/initial0/20; no waiting/backfill.
One reviewed profile3583 was reconciled through existing metadata APIs, preserving
provider/season and adding independently verified aliases; reopen/idempotency
verified. One ordinary3583:6 recovery request submitted, not manually consumed.
Preflight confirms no validTC/source+17sidecars unchanged/no running target/no
existing newhash torrent. Need actual server download/extraction, original QC,
formal manifest/final reread/task convergence/dedup. Formal additions remain
AI0/download0/extraction0, M2 NOT COMPLETE; no further deployment for these docs.
Evidence /logs/m2-collection-identity-deploy-20260912T1525/ and
/logs/m2-collection-target-20260912T1545/.
Follow-up evidence: public3583 request automatically consumed/newhash admitted,
but15:43:59 snapshot stalledDL/0bytes/0seeds and no extraction. Existing bounded
timeout/alternatives retained, not a formal success. AI separately auto-claimed,
ASR heartbeat+new valid detection checkpoint, safely NEEDS_REVIEW; another scan
claim followed. claim-evidence-1789227977574137277.json; no new recovery-row dispatch
or independently verified formal subtitle counted.
Final bounded15:49:46 check confirms actual download21,595,226bytes/0.4626%,
2seeds/downloading; earlier zero-peer wait resolved automatically. No extraction
job yet; source not complete/formal0. Continue at exact-source completion or
durable failure, do not resubmit or wait for the whole download/Gate.

Sep12 15:20UTC: reproducible collection-prefix/range alias defect, isolated fix
only in mikan_worker.py. Independent catalog aliases recorded, Production mapping
not changed. Local27/server300 and real metadata Docker restart PASS at collection-
candidate-fFwAkj; no download/publication. Current c65d856/Gate145452/ARMED/69holds
unchanged. Next safe deployment and existing scoped metadata/recovery APIs;
formal downloaded/extracted artifact proof remains required. M2 NOT COMPLETE.

Sep12 15:00UTC: c65d8565864517fa8b5b95cb636aa060f4721405 actually deployed by
safe-update-stack20260912T144844Z-2672655; controlled recovery ARMED,69holds retained,
0new handoff deltas,6854 recoverable/7221 retained identities (not deliveries).
Actual-image329/real metadata Docker restart/fresh7 breakers PASS. Auto AI claim
b62db841... enteredASR with hash-valid detection checkpoint; no new historical
recovery-lane dispatch or formal delivery claimed. Full logs /logs/m2-alias-identity-20260912T1450/.
Old Gate125703 remains SETTLED FAIL6strict/14review with all20/evidence unchanged.
Actual runtime change alone created Gate145452, baseline9955ddd46a660dfd147e5d42,
start14:54:52.910031Z/initial0/20; do not wait, backfill or rebuild for docs.
Three more known missing-TC targets3583:6/3552:12/3368:12 are source/identity blocked;
no torrents added or formal subtitles published. Next requires a safe complete
source and real formal artifact/QC/manifest/dedup proof. M2 NOT COMPLETE.

Sep12 14:47UTC: exact-alias refusal reason finalized; local16/server289 and
real metadata isolated Docker restart PASS at candidate-c1G520. Runtime remains
1d5d737/ARMED/69holds; earlier alias handoff stopped before any pause/deployment
because work was active. Actual successor ASR process verified; preserve it.
Need a fresh pinned safe handoff and formal downloaded/extracted subtitle proof.
Existing SETTLED Gate125703 strict6/review14/FAIL retained; M2 NOT COMPLETE.

Sep12 14:19UTC candidate: exact verified slash aliases no longer falsely classify
as sequel; canonical hash required so unresolved mirrors cannot bypass failed-hash
dedup. All season/unknown identity/QC guards retained. Server final289 tests and
actual metadata replay/Docker restart PASS at /logs/m2-alias-identity-candidate-
20260912-YcWcvF/. Not deployed yet; runtime1d5d737/ARMED/69holds and settled failed
Gate125703 remain. Need existing safe deployment/controlled reconciliation and
actual-image proof. Completed-only project qB query0; retained ZIP26/26 QC-invalid;
three more known targets source/identity-blocked. Formal download/extraction0,
M2 incomplete; no M3/M4 or fabricated source/season evidence.

Sep12 bounded closeout: frozen Gate125703 automatically SETTLED20/20 at13:27:41Z,
strict6/review14, safetyFAIL. Its immutable auto-summary hash and20 fixed members
were verified; all14 review events are source_selection_needs_review. Preserve
the failed Gate with no backfill/new Gate. Worker1d5d737,ARMED,69holds unchanged.
Seven known historical download targets:4 already valid TC preserved;3 truly
missing TC have no retained complete download and all primary eligible hashes
already failed. Six fallback providers healthy, no safe new selection. Three
public target-bound recovery requests retained for existing server backoff.
New verified formal AI0/download0/extraction0; M2 NOT COMPLETE. No production code,
config, deployment or M3/M4 change; full evidence and next conditions in latest
docs/M2_REMAINING_ACCEPTANCE_20260908.md. Do not repeat finished deployment/Gate
initialization or immediately re-query unchanged blocked source candidates.

Sep12 13:03UTC:1d5d737 deployed safely; actual-image329/two restarts/history28/7FI
PASS. Controlled recovery ARMED, own pause released,69holds/UNPROVEN preserved.
New reconciliation m2-recon-opencc-history-20260912; failed predecessor retained.
Original Gate225436 INVALIDATED with9members unchanged. New Gate125703 baseline
3a892c9880d22f735900edf9 ACTIVE0/20; two automatic historical claims executed and
completed, correctly supplemental job_started_before_gate, not cohort backfill.
Sources strong checksum/old receipts verified unchanged. Bounded recent12 downloads
found no reusable complete torrent; known3085 still28.539%,E13file98.2289%,ZIPQCfail.
Formal download/extraction0; M2 incomplete. Next pursue a policy-eligible complete
QC-valid source and formal publication; preserve current runtime/Gate and do not
repeat finished deploy/recovery/tests or wait for20. Current details/evidence in
docs/M2_REMAINING_ACCEPTANCE_20260908.md latest closeout.

Sep12 postdeployment:0a2b1ab deployed; actual323/restart/7FI PASS. Formal recovery
refused because28 already-recovered historical reasons were treated as unresolved.
Exact history-chain correction reproduced, local85/server110 and real receipt/DB/
archive/runtime probe PASS; not yet deployed. Original Gate225436 invalidated with
9 members retained;69 holds and own reconciliation pause remain. Preserve receipt
m2-recon-opencc-format-20260912 and transfer only owned pause for next safe handoff.
Do not call current runtime ARMED. Formal download/extraction0; M2 incomplete.

Sep12 current: exact subtitle-format authorized reconciliation implemented and
candidate-tested, including a reproduced DB-COMMIT/file-handoff restart defect.
323 server targeted tests and both real isolated Docker restart boundaries PASS;
3 actual SRT copies pass original conversion/QC/manifest/replay (not formal media).
Evidence `/logs/m2-opencc-recovery-20260911-wVrqzj/`, corrected-input runRJTiCN.
Worker5f116c3 remains deployed/TRIPPED; fresh official idle=true,69 holds retained.
Next execute owned safe handoff and actual-image controlled recovery; no generic
reset, no historical continuity upgrade. Formal download/extraction0, M2 incomplete.
Older entries below are historical; the validator is no longer unimplemented.

Latest Sep11: SRT/ASS dispatch and prepublication staging repair implemented in
worker.py;16 targeted tests and187 server regressions PASS. Actual isolated Docker
restart after durable manifest PASS, source/output/manifest unchanged, duplicate
publications0. Evidence `/logs/m2-opencc-format-restart-20260911-NdGDQI/`.
This candidate is NOT deployed. Retention5f116c3 remains the live runtime;
TRIPPED/Gate225436 ACTIVE9 enrolled/5terminal/3strict and69holds remain unchanged.
Next: implement the smallest exact-incident authorized-reconciliation validator
for repeated_identical_stage_failure/opencc:opencc_unknown (current entry point
rejects it); targeted negative/restart tests, actual-image proof, then owned-drain
safe deployment/controlled recovery. Do not use an old receipt or generic reset.
3085 ZIP26 members/CRC/path checks PASS, but both383-cue episode13 subtitles FAIL
existing QC (cps/timing overlap). No formal publication; new download/extraction0.
Retain bounded alternative-source policy; never re-add failed torrents or lower QC.
M2 incomplete; M3 engineering/AI1 evidence remains separate and accepted.

Latest2026-09-11 03:52UTC: retention repair5f116c357ae5e909108b862b155830fa2afe27e8
deployed safely/actual-image270/restart/fresh7FI PASS; controlled recovery initially
ARMED,69holds/UNPROVEN retained. New Gate225436 initialized0/20; old Gate110900
invalidated with20 member evidence unchanged. Later new TRIPPED opencc_unknown:
three distinct attempts have ASS source contains no Dialogue events; current Gate
9enrolled/5terminal/3strict, no pass. Preserve trip; diagnose actual inputs before fix.
AI automatic claim/ASR/checkpoint proven. Download3085:13 automatically started,
Stop/120min verified; now partial batch28.539%, source/identity review state retained.
Its subtitle ZIP is100% downloaded but not integrity/QC/import verified; preserve
it and target13 partial98.23%, no failedhash re-add. Formalnew0/0/0, M2 incomplete.
Details and evidence in top M2_REMAINING_ACCEPTANCE section; no M3/M4 re-opening.

2026-09-11 follow-up: real isolated qB threshold expiry and actual same-container
restart now PASS (threshold-restart-b.exit0); natural elapsed-time expiry fixture
failed and is retained, not relabelled. Candidate scoped Stop protection ready for
commit/safe deployment. Currentdbb6b8b ARMED69holds, Gate110900 frozen20/terminal19/
strict2, not accepted; active work prevents immediate deployment. Use owned drain,
preserve original evidence, then formal3085:13 download/extraction verification.
No Production file/preferences change, no new formal subtitles, M2 incomplete.

2026-09-11: scoped content-retention candidate implemented (qB Stop at existing
limits, exact ownership/readback before extraction); uncommitted and undeployed.
Server final targeted29 PASS; prior266 had2 old mock-signature expectations, now
updated. Need genuine isolated qB expiry/restart proof, safe deployment and formal
3085:13 output. Runtime/Gate unchanged, no new formal delivery; M2 incomplete.

Latest11:15UTC: root cause of vanished3932:3 now PROVEN by qB logs: global
120min seeding limit automatically removed torrent AND content. Before a new
canary, minimally protect project unconsumed downloads without altering other
torrents/global policy; verify durable consumption before releasing protection.
3085:13 known alternative has8 episode candidates; public revalidation helper
prepared but NOT executed. Runtime/Gate unchanged, formal0, M2 incomplete.

Latest2026-09-10 11:11UTC: safe deployment20260910T110246Z-62313 and controlled
recovery EXIT0. ActualWorkerdbb6b8b3cc188c6521ae0e31d22adf7c37349106, ARMED69holds;
actual-image243/restart/fresh7FI PASS. Old SETTLED GateFAIL retained; new Gate110900
initialized0/20. Download3932:3 old completed torrent/file vanished before acceptance;
exact check finds0torrent/no mappedfile/recoveryevent0. Do not re-addfailedb144;
trace retained evidence and known alternatives. Formal0/0/0; M2 incomplete.

2026-09-10 06:18UTC: repairadb615962beb25b1d3be12896d3ab5ea128fda9e pushed,
243server tests and genuine isolated-container restart PASS; not deployed.
Official idle check still refuses planned_change_work_not_idle. Gate173949 is now
SETTLED20/20,20review/0strict/safetyFAIL; original journal+emitted hash verified.
Next owned safe drain/SETTLED-preserving handoff and real3932:3 formal publication.
No forced task termination, no hold removal or Gate replacement; M2 incomplete.

Latest2026-09-10: mapped-source recovery candidate is uncommitted/undeployed.
Server isolated241 PASS; additional receipt retention/compaction regressions
reproduced defects, now fixed with17 local PASS and server regression-b243 PASS,
exit0. Fresh preflight1789020684069238046: ARMED,69holds, same runtime,
one running Queue job. Preserve execution; safe idle deployment and genuine formal
download/extraction output proof remain required. M2 incomplete, additions0/0/0.

2026-09-10 deployed db9265201bd6620ece8a72b2f4ea9a75bcfd82ff via safe updater;
2075Worker/231WebUI/288actual-image tests, true isolated restart and7breakers PASS.
Controlled recovery ARMED,69holds, original evidence retained; two real automatic
claims/stages/checkpoints followed by review verified. New runtime Gate173949
initialized0/20; no acceptance or score transfer. Download3932:3 public historical
revalidation recorded with source/sidecar snapshot and idempotent replay, normal
server discovery requested; actual download/extract/formal output still required.
Formal new AI/download/extraction0/0/0. No runtime redeploy for this documentation.

2026-09-10: exact source-language-vote recovery candidate passes31 server-isolated
contract/regression tests. Safe deployment, actual-image proof, incident hold and
real resumed claim remain required. Preflight retains TRIPPED/68holds/originalGate;
zero running Queue rows does not override four running attempt records or the
official idle check. See the latest M2 acceptance section. Formal additions0/0/0.

Latest candidate2026-09-09: reproduced mixed-language vote pooling (en+ko wrongly
became confident en under a Japanese source decision), and post-QC rejection failing
to settle Pipeline. Minimal language/cache/review fixes verified276tests and an
actual isolated container restart. No Production deployment or Gate reset yet.
Next exact-incident reconciliation proof/hold, safe deployment and automatic claim;
do not reuse prior postprocess root-cause authorization as this incident's evidence.
Download target2565:12 still lacks a new usable source/formal output. M2 incomplete.

2026-09-09: secondary queue overwrite defect reproduced and minimally patched in
candidate source only;212 server-isolated queue tests PASS including reopen/replay.
No deployment or Gate change. Next identify original strict rejection predicates,
then evidence-bound safe deployment/recovery. M2 remains incomplete; formal0.

Current: bd268943... deployed,78actual-image tests+7breakersPASS, controlled recovery
initiallyARMED; later incorrect_completion TRIPPED and AI admission stopped. NewGate
212600 has1NEEDS_REVIEW/strict0, oldGate preserved. Next isolate exact intercepted
completion transaction (attempt7ba2996...) and explain queue done/pipelineQC/open
obligation mismatch before smallest fix/formal recovery. No direct latch/DB changes.
Download formal0 remains pending; do not substitute AI output or reset cohort.

New candidate: real Worker thread162 stalls before discovery in Queue x mapping
realpath checks. Fix per-operation resolved-path reuse and existing deadline
propagation;40isolated tests PASS including real SQLite/symlink/reopen invariants.
Not deployed. Next fresh safe deployment/controlled handoff (never reuse old1445
receipt/root), then actual automatic claim and formal output proof. Preserve failed
Gate144756/68holds/UNPROVEN; do not claim M2 acceptance from these tests.

16:17UTC exact-source diagnosis complete: independent empty and same-style overlap
failures remain, so no QC exemption/runtime edit justified for this canary. Retain
failed source and let existing alternative-source request proceed with backoff;
next acceptance evidence must be a real alternative claim and valid formal product.
Gate checkpoint_loss20 means20 incomplete strict histories, not proven physical
deletions; exact member snapshot retained. Formal0, GateFAIL, M2 still incomplete.

Latest16:12UTC: exact download completed and Worker automatically extracted E12;
both bilingual ASS tracks parse PASS/hardQC FAIL, formal deliveries0. Preserve
failed hash; do not re-add or relax QC. Next diagnose bilingual import behavior
against retained evidence in isolation, then choose existing safe source policy.
Gate144756 automatically SETTLED20/20,18review/2failed/strict0/safetyFAIL; retain it.
Investigate checkpoint_loss_count20 against actual member evidence, not a library
scan or automatic reset. Current7eeb848/ARMED/68holds unchanged; separate AI successor
755338a... actually running ASR. See latest M2_REMAINING_ACCEPTANCE for evidence.
Historical deployment notes below are chronological, not current action requests.

14:55UTC actual7eeb848 deployed by safe-update-stack20260908T144053Z-3222495;
actual image329targeted tests+7breakers PASS, controlled recovery ARMED. Old SETTLED
Gate/receipt and68holds/UNPROVEN preserved, newGate144756 starts0/20. One reviewed
download source3aeb9cf... truly downloading2.55%; separate AI successor e707c...ASR
attempt4 heartbeat confirmed. No formal download/extraction output yet: next verify
this existing download through extraction/QC/manifest/safe final publish; never re-add.
Full evidence and exact IDs: M2_REMAINING_ACCEPTANCE latest section. M2 incomplete.

Candidate code now fixes both controlled historical completion revalidation and
receipt-bound SETTLED Gate handoff. Combined targeted122tests PASS; not deployed.
Next owned safe deployment/reconciliation followed by real download/extraction
formal publication proof. Old failed Gate/summary stay immutable, no backfill.

Next deployment prerequisite: reproduced ACTIVE-only planned-change failure after
legitimate SETTLED20/20. Preserve settled FAIL/summary; extend receipt-bound
controlled handoff, not the Gate membership/status. Characterization evidence:
settled-handoff-repro-qH3WZy. Mikan post-reopen source/hold recheck14tests PASS,
dispatch-revalidation-4ZhbRN. Not deployed; no formal download/extraction delivery.

2026-09-08 14:11UTC: guarded single-target historical revalidation implemented,
not deployed. Archive+retry intent persist in existing pending store, normal
known-episode discovery resumes;17 targeted/isolation/regression tests PASS.
Real qB/source/output acceptance and safe runtime deployment still outstanding.
Live Worker738e96e/ARMED; successor7769498c translating with heartbeat; no forced
restart. Gate121706 remains settled FAIL, no new formal download/extraction output.
Evidence and remaining safety review: M2_REMAINING_ACCEPTANCE latest section.

13:52UTC three exact old targets have2/8/1 episode candidates but are blocked
before comparison by historical terminal-success markers, not active/backoff.
Existing archival reopen helper has no caller. Preserve history; next verify
narrow controlled revalidation/reopen behavior before changes, no direct DB edits.
See M2 remaining acceptance latest section; no new subtitle/deployment/Gate change.

2026-09-08 13:49UTC: resumed M2; M3 engineering evidence remains accepted separately.
Current frozen Gate121706 has automatically SETTLED20/20:18NEEDS_REVIEW,2FAILED,
strict_verified0; server summary safety_gateFAIL. Preserve failed cohort/summary,
do not backfill/reset or claim M2 acceptance. Runtime738e96e/ARMED unchanged.
Five known old-completed cases:2identity-review/3unverified completion, no retained
project torrents. One existing-policy alternative source cycle returned35releases
from6successful sources, selected0. No re-add/reset/Whisper bypass performed.
Next safe work: bounded source suitability investigation for the3uniquely indexed
old targets; no full library scan. New download/extraction formal delivery0.

## M3 engineering delivered — M2 acceptance remains open (2026-09-08)

13:27UTC final audit closes configured-model/recovery engineering scope:
actual-image real OS duplicate ingress/promotion/scanner/admission2cycles PASS,
no runnable duplicates/unchanged files. Runtime738e96e/ARMED/68holds/Gate stable,
host provider VERIFIED. RealAI TC1; download/extraction0. M2 is NOT accepted.
Authoritative final scope/evidence: docs/M3_ACCEPTANCE_AUDIT_20260908.md.
Earlier timestamped pending notes below are history, not current deployment state.

13:19UTC requirement/evidence matrix saved in
docs/M3_ACCEPTANCE_AUDIT_20260908.md. Existing server test results and real
restart receipts inspected; no full rerun. Actual runtime ARMED/68holds/Gate
unchanged. Duplicate-ingress E2E scope remains distinct from verified predicates.

13:16UTC real published target retained manifest digest resolves to exact prepared
and provider-confirmed lineage; request watermark/Gate/time ordering verified.
Finished/terminal-reprocess predicates PASS; no duplicate trigger attempted.
AI delivery remains1; M3 closeout audit/M2 download-extraction/Gate remain open.

13:12UTC real9f5394... COMPLETED/ledger succeeded;91 real model RESPONSEs,
runtime/provider identities match,3preparations/1provider confirmation.
Formal v2/hash/parse/Production-role hardQC PASS,source hashes unchanged:
newAI formal TC1 (download0/extraction0). Not strictGate/M3 completion.
Remaining exact lineage/idempotency and acceptance audit; no runtime change.

13:01UTC known alternative9f5394... safely retranslated-cache requeue via existing
revision-bound API;2work artifacts archived/source hashes unchanged/attempts1.
Still queued while server runs other selective recovery; no claim/delivery claim.
Evidence and immutable intent in known-alternative-9f; do not repeat operation.

12:55UTC e15760... canary naturally exited: NEEDS_REVIEW/paused/attempts3,
ASR quality failure after configured fallbacks; no further forced retry.
Source/archive preservation verified; MODEL0/formal additions0.
Runtime738e96e/ARMED/admission open/68holds/Gate unchanged. M3 not accepted.

12:48UTC second canary attempt e15760... actually auto-claimed after7d0c98...
paused for ASR review. Live ASR child1852332 and source checkpoint hash verified.
Fresh runtime738e96e/ARMED/68holds/currentGate unchanged; noMODEL events/output.

12:45UTC canary ASR failed quality; server requeued attempts2, not delivered.
Different successor7d0c98... actually auto-claimed/ASR/smaller fallback heartbeat.
Source/archive safety PASS; formal additions0. No runtime/Gate change.
M3 real model publication and M2 formal download/extraction/Gate still incomplete.

12:30UTC one existing revision-bound retranslate recovery actually auto-claimed
(jobe15760a3eac4466eb2ea02c430d64418),2work artifacts reversibly archived,
retry budget preserved. Cached JA opening rejected -> bounded existing ASR fallback
running; noMODEL events/formal delivery. Runtime/Gate unchanged; details M3 doc.

12:19UTC runtime738e96e safely deployed/439actualtests/FI7/7/ARMED/resumed.
Real post-source cache review now converges Queue and Pipeline NEEDS_REVIEW,
successful checkpoint preserved,attempts1; automatic successor also verified.
68holds/no newdelta; newGate121706 separateoldresults. NoMODEL events/output;
M3 real model acceptance and M2 delivery/Gate remain incomplete.

Pending narrow parent post-source review convergence fix: exact real jobs showed
paused/manual_review Queue but SUBTITLE_DETECTION formal state after source success.
Deployed-image red reproduction;399 server regressions plus reopen/replay PASS.
Not deployed; cache protections/68holds/Gate unchanged; model output still unproven.

12:02UTC writer-reservation runtime57773b2 safely deployed;293 actualimage tests,
freshFI7/7, recoveryARMED/admissionresumed, trueclaim+sourcecheckpoint proved.
Original67holds retained;1 newmtime difference quarantined ->68holds total.
New frozenGate120036 preservesoldresults separately. Model output/download
formal acceptance remains open, additions0. Details in M3_MODEL_RECOVERY.md.

Pending writer-upgrade fix: isolated two-connection WAL reproduced database lock
in PipelineJobStore._savepoint. Self-owned BEGIN IMMEDIATE candidate preserves
caller transactions;24 local/261 server related testsPASS. Not yet deployed;
actual prior Production root attribution remains unproven. See M3 recovery doc.

11:47UTC: diagnostic runtime3f800c8 safely deployed;216 actual-image tests,
freshFI7/7, official recoveryARMED/admission resumed/67holds. New frozen Gate
m2-gate-20260908T114539007821Z-7a4995553b; old evidence retained. Two actual
source-stage continuations/checkpoints verified, no formal output added.
SQLite root cause and real M3 model-output acceptance remain open.

2026-09-08 11:35 UTC: runtime continuation proved after ASR manual review,
but two SQLite lock failures need root-cause evidence. Targeted traceback logging
candidate passed 5 local/5 isolated server tests; not yet deployed, not a lock fix.
See docs/M3_MODEL_RECOVERY.md; M2/M3 acceptance remains open.

The user explicitly authorized starting M3. This supersedes earlier **no-M3 scope
restrictions**, not M2 evidence: M2 production acceptance is still incomplete.
M2 download/extraction and frozen-Gate evidence remain separate and must not be
relabeled PASS or mixed with future M3 runtime results.

### M3 — Configured model fallback and resource-aware recovery

Current 2026-09-08 11:10 UTC: candidate 5f379e877ce59cb3207927e38aad2f6735b76287
deployed by safe-update-stack; actual image 211 tests PASS, fresh breakers 7/7 PASS,
runtime ARMED with pinned host observer automatically refreshing. Controlled
reconciliation preserved 67 holds and UNPROVEN history, classifying 24 late ingest
transitions before official recovery. New frozen Gate e365b1132703b8cd1ae7e063
initialized 0/20; first automatic claim reached NEEDS_REVIEW and remains selected;
next job started ASR. Formal added subtitles 0. Model-response/publication proof
and M2 download/extraction/strict Gate acceptance remain open. No repeat deployment
for this documentation-only closeout; earlier pending-deployment notes are history.

Full candidate regression now passes: 2023 tests in the server isolated container
(candidate-full-predeploy-final.log, exit 0). Fixed only executable shell fixture
tempdir contamination from another suite's noexec /dev/shm. Fresh owned admission
drain, planned-change receipt, observer installation and actual image deployment
still required; no Production container switch or database changes yet.

2026-09-08 10:36 UTC live check: original runtime ARMED/baseline match, active
transcription heartbeat and no maintenance hold. No safe idle window demonstrated.
ASS restyling is outside the normal Queue path and is not run/countable here.
Existing host update_cron mechanism verified read-only; installation still pending.

Targeted omission/CPS model repair merges now record both original and partial
repair preparations, requiring exact merge bytes and unchanged unselected cues.
Server 211 related tests PASS. Existing ASS restyling authority and scheduler /
safe runtime deployment acceptance remain outstanding; no Production deployment.

Deterministic prepublication QC repairs now create immutable child SRT preparation
lineage from a verified parent and continuous diagnostic hash chain. Server 210
related tests PASS. Model-based targeted repair and existing ASS restyling still
need lineage review; this candidate is not deployed or formally accepted.

Publication observation-wait timeout now uses exact existing transient timeout
recovery, with configured budget/backoff preserved and no broadened treatment of
provider drift or unknown ownership. Server related suite 375 PASS. Remaining
deployment blockers are repair/restyling lineage and host scheduler/runtime proof.

AI ASS publication now consumes post-preparation provider confirmation before
formal replacement, with a bounded 45-second observation-file-only wait and
post-wait byte/runtime recheck. Server 208 PASS. Repair-derived lineage, restyling
authority and observation failure recovery classification still block candidate
deployment; scheduler installation/runtime proof remain outstanding.

Worker complete-cache admission now consumes exact provider/runtime SRT lineage
before reusing cache. Unknown caches remain intact and use the existing source
review category. Related server 207 PASS. Formal publication confirmation and
repair-derived lineage remain required before deployment.

Post-preparation provider confirmation state primitive now persists exact Gate /
provider evidence newer than SRT preparation, rejects changed/pending inference,
and preserves immutable replay. Worker publication consumption remains required;
this is provider continuity evidence, not a successful subtitle delivery.

Complete SRT preparation now has hash-bound immutable Pipeline lineage including
execution/provider identity and request watermark (204 related server tests PASS).
Worker cache admission/publication consumption and post-preparation confirmation
remain open. Preparation is not a delivered or QC-verified subtitle.

Batch checkpoint signatures now include validated provider/runtime identity for
bound M3 requests; same-identity resume is preserved and cross-provider batches
are not restored. Final SRT-cache lineage and the publication confirmation barrier
remain required; no Production deployment has occurred.

UNRAID Bash/Docker scheduler adapter is implemented and isolated-tested (36
related server tests PASS), without host Python or Docker socket mounts. It is
not installed; scheduling/reboot proof and post-response confirmation remain
required before the candidate's safe deployment.

Gate initialization and provider refresh now serialize across processes through
a nonblocking kernel-owned lock; no lock deletion/PID-age reclaim. Real process
exit/reacquisition verified on Windows and Linux; Linux related 66 PASS.
Autonomous publisher lifecycle and post-response confirmation remain open.

Bounded host `provider-refresh` publisher is implemented; it preserves observed
continuity gaps and cannot arm/resume. Autonomous scheduling, single-writer
handoff and post-response confirmation still block Production deployment.

Provider observation consumer/arm seed now fails closed for missing, stale,
future or baseline-mismatched host evidence. Autonomous host publishing and
post-response generation confirmation remain incomplete; freshness alone is not
continuous provider identity proof. This candidate is not deployment-ready yet.

Real isolated provider termination is now verified (09:16 UTC): real HTTP sender,
durable UNKNOWN, Docker-confirmed sender exit, same-provider restart, exact fresh
binding, controlled settlement/replay and unchanged source/checkpoint/budget.
See `provider-termination-O2PvZp/` evidence in the M3 document. No Production
runtime changed. Ongoing provider drift enforcement and safe deployment remain.

Candidate host integration now resolves one explicitly selected model request
inside existing controlled recovery, between persisted pause/recovery and arm.
Hash-bound recovery record, pause ownership and actual runtime identity are
required. 152 related server-isolated tests PASS; host-composed/real-provider
verification and live drift enforcement remain required before safe deployment.

Controlled model settlement state primitive is implemented and isolated-tested:
74 related server tests PASS. It preserves UNKNOWN history/budget and stage status,
requires persisted sender evidence and exact provider restart ordering, and safely
replays after restart. Trusted host command integration, admission fencing and
provider/Gate transition enforcement are still required before deployment.

Provider binding candidate: host arm/recover can attest an explicitly published
Docker model provider and bind its stable generation to the Gate and request
receipts. Server related tests 258 PASS, final targeted tests 77 PASS (overlap).
Live provider drift enforcement, controlled UNKNOWN resolution and safe runtime
deployment remain required. No production Gate change or formal delivery is
claimed by these tests; see `docs/M3_MODEL_RECOVERY.md` for evidence.

Initial isolated baseline: 118 resource-admission/runtime/Worker integration,
translation-checkpoint and translator-parser tests PASS, exit 0.
Evidence: `/logs/m3-baseline-20260908T070237Z/tests.log`.
First review target: `_select_available_translator_model` currently substitutes
the sole advertised model when the configured model is unavailable. Assess an
explicit, compatible route-authorization contract before changing live behavior.
No M3 runtime or model configuration change has been deployed yet.

First M3 code increment reproduced and removed implicit unrelated sole-model
substitution; the configured primary/fallback chain now remains intact.
121 focused tests PASS; see [M3 evidence and remaining contracts](docs/M3_MODEL_RECOVERY.md).
Durable failed-route budgeting and in-flight timeout/resource ownership remain
open; this increment does not complete M3 or authorize a production deployment.

Second candidate increment: reproduced timeout-overlap with real executor threads;
same-process endpoint ownership now spans Translator instances and model unload.
255 focused/shared tests PASS. Cross-process/restart ownership and durable failed
route budgets remain required before M3 deployment; see the M3 evidence document.

Third candidate component: durable request receipts in the existing Pipeline
stage metadata/events, with cross-process reservation, restart-preserved UNKNOWN
ownership, non-replenishable budgets and immutable evidence. 36 component/shared
tests PASS locally and in server isolation. Worker/Adapter/resource binding is
not yet implemented; this is not runtime protection or deployment acceptance.

Fourth candidate increment now binds all four Worker translation/repair entries
to the committed Pipeline attempt and persists request/result evidence at the
actual Translator boundary. Hidden SDK retries are disabled; ambiguous transport
outcomes retain ownership. Stage-idempotency regression fixed. 324 server-isolated
tests PASS. Persistent unload/resource/recovery integration and live deployment
acceptance remain open; M2 evidence is unchanged.

Fifth candidate increment: managed model unload shares durable endpoint ownership
with inference, including cleanup after stage completion. Required unbound cleanup
is refused and ambiguous unload outcomes remain UNKNOWN. 327 server-isolated
tests PASS. Shared-device admission and controlled UNKNOWN reconciliation are
still required before safe deployment.

Sixth candidate increment: read-only topology confirms Tower Ollama and Worker
share the single RTX 3060. Request resource scopes now feed existing admission
and the post-kernel-lease recheck, with bounded indexed authority reads. CPU work
and proven independent model resources remain separate. 342 server-isolated
related tests plus 16 final targeted tests PASS (overlapping suites, not summed).
Production configuration/runtime unchanged. UNKNOWN controlled resolution,
route contracts and deployment/live acceptance remain open.

Seventh increment: provider read-only discovery confirms exact configured IDs;
implicit substring/short-alias substitution is removed. Verified Future
cancellation records NOT_DISPATCHED; submit failure without a Future retains
UNKNOWN. 345 server-isolated tests PASS. Post-dispatch UNKNOWN cancellation
attestation and real restart/deployment acceptance remain unproven.

Eighth evidence increment: real HTTP sender-process loss and a real isolated
Docker restart both PASS. Restart preserves pending ownership, prevents duplicate
dispatch, and preserves fixture source/checkpoint. No production container was
restarted. Post-crash remote cancellation attestation and production deployment
acceptance remain open; isolated restart is not formal output delivery.

Ninth increment adds immutable sender-exit evidence at the existing subprocess
supervisor boundary, including a real timeout kill-and-wait test. Remote ownership
remains held. Ollama unload now requires terminal `done=true`. 214 related and
5 final targeted server tests PASS (overlap, not summed). Provider generation
binding/quiescence proof and controlled resolution are still required; no
Production deployment or model-provider restart has occurred.

Extend the existing configured ASR/translation routes and resource admission,
not a parallel queue or a wholesale backend rewrite. Preserve compatibility,
source safety, strict QC and existing durable checkpoint/recovery contracts.

- [x] Establish focused baseline of existing model routes, resource admission,
  retry bounds and restart behavior; identify evidence-backed gaps.
- [x] Implement the smallest missing Adapter/fallback contract, using configured
  models only, with persistent route/reason/attempt evidence and bounded retries.
- [x] Verify timeout/crash/OOM, exhausted or unavailable fallback, resume without
  repeating valid checkpoints, and unchanged quality/publication protections.
- [x] Run targeted integration and necessary shared-boundary regressions in the
  server isolation environment; do not count fixtures as production delivery.
- [x] Deploy only verified runtime changes through existing safe idle/update and
  controlled recovery. Preserve old receipts, held obligations and Gate cohorts.
- [x] Verify actual runtime parity and bounded real processing/publication evidence;
  report remaining external/M2 Gate blockers independently.

No new model download, QC relaxation, source hold release or production
configuration change is implied merely by starting this milestone.

## Current M2 acceptance status (2026-09-08 06:33 UTC)

**M2 incomplete; M3 candidate work started but not deployed.** Previously pending repair is now actually
deployed as `a4829a5298304f9e2dc20dd02ecde883ea53ec23`; safe deployment and formal
controlled recovery completed, Breaker ARMED. New actual-image 312 related tests
and isolated restart PASS; fresh breakers 7/7 PASS. All 67 holds preserved.
New frozen Gate initialized 0/20; latest 1/20, strict-verified 0, no old scores copied.
AI recovery and the next normal task actually claimed and entered stages.
Download E2 trial: 1 torrent added, 0 completed downloads/extractions/formal outputs;
existing bounded `did not start` recovery retained failed hash and re-requested alternatives.
See [actual deployment and bounded acceptance](docs/M2_REMAINING_ACCEPTANCE_20260908.md).
Earlier deferral sections below are historical, not the current runtime state.

## Latest bounded acceptance update (2026-09-08 05:10 UTC)

See [remaining acceptance evidence](docs/M2_REMAINING_ACCEPTANCE_20260908.md).
One autonomous new formal AI traditional-Chinese delivery is verified. Candidate
`de6759499eaa0576dc9b9affd2f842109781d4c5` fixes durable final-ASR rejection and
fragment repair-budget evidence; 209 focused / 312 related tests and isolated
container restart PASS. Deployment is deferred by `planned_change_work_not_idle`;
the owned pause has been released. Runtime remains `65a9670`, Breaker ARMED,
existing Gate preserved. Download-chain formal acceptance, held obligations and
strict Gate completion remain open. This is not whole-project acceptance or M3.

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
