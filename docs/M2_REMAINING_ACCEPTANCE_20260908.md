# M2 remaining acceptance — 2026-09-08

## 2026-09-10 — exact language-vote recovery candidate

The evidence-bound recovery mode now has 31 passing server-isolated tests, including
safe-source resumption while the incident remains held. Previous postprocess and
line-repair modes and authorized reconciliation regressions are included; counts
overlap prior suites and are not summed. Evidence:
`/logs/m2-language-vote-recovery-20260909T1720/candidate-recovery-20260910-a.log`
and `.exit` (0).

Read-only preflight `preflight-1788974796368163081.json` in that directory confirms
TRIPPED, 68 holds, the original Gate claim binding and no running Queue rows.
The runtime probe also reports four running attempt records; this is not an idle
attestation. The official idle/heartbeat/child checks must pass before deployment.
Runtime remains bd2689437ea36d3c04f3193538862a931331c7f0; candidate parent
ebb79964aa2c9272ea95d2cc4a773ebdd63a0036 is pushed but not deployed.

Next: seal an owned current-state reconciliation snapshot, use safe-update-stack,
verify actual-image tests/restart and fresh 7/7 breakers, retain the exact incident
as a hold, then controlled recovery and real automatic claim. No direct DB/latch
change. Formal AI/download/extraction additions remain 0/0/0; M2 incomplete.

## 2026-09-09 — mixed-language source vote and review convergence

The intercepted attempt's retained decision selects stream1, metadata Japanese,
strategy ASR_JA_AUDIO. Runtime provenance records three content samples:
en0.84375, ko0.60302734375, ja0.9853515625. Actual minimum probability is0.6,
uncertain policy continue, allowed languages ja. Old aggregation combines English
and Korean into two non-allowed votes and then calls the source confidently English.
The child consequently produced an English ASR artifact and English-to-TC delivery;
the English checkpoint hash still matches, while the expected Japanese transcript
and diagnostics are absent. Strict hallucination evidence correctly rejects that
decision/artifact mismatch. No English artifact is relabelled as Japanese.

An explicitly modelled replay in a disposable consistent SQLite copy reaches
Pipeline COMPLETED before the deliberate rollback. It is NOT a historical replay
or successful delivery. Raw final_state_completed is true; qualify_strict_output
also forces it false when incorrect_completion is reported. Therefore the original
first failed predicate did not by itself prove a failed completion DB write.
The replay's runtime predicate is false because the real runtime remains TRIPPED.

Reproduced candidate fixes:
- language_detector.py requires a majority of one specific blocked language, not
  a pooled non-allowed category. Existing saved samples are reaggregated on cache
  reads, including after restart; no cache clear, media scan or automatic new model
  request. Existing uncertainty/Japanese-evidence policy remains in force.
- main.py settles the exact parent strict rejection after a committed QC/MUXING
  stage to NEEDS_REVIEW, preserving successful attempts/checkpoints and retry budget.
  Generic late telemetry safeguards remain unchanged. All failed predicate names
  are now retained in rejection detail rather than just the first/count.
- scan_state.py retains the previously tested protection against discovery turning
  an unresolved strict review into Queue done.

Evidence in `/logs/m2-mapping-budget-20260908T2117/`:
`completion-snapshot-387rdl83/result.json` (MODELED_REPLAY_NOT_HISTORICAL_PROOF),
`hallucination-diagnosis-1788938831048325452.json`,
`language-policy-proof-20260909T1222.log` (points to actual policy report),
`language-vote-red-20260909T0854.log` (2 expected failures),
`strict-qc-convergence-red-20260909T1215.log` (QC != NEEDS_REVIEW),
`completion-language-regression-20260909T1218.log` (**276 PASS**),
`language-cache-public-restart-20260909T1222.log` (**11 PASS**, public cache entry).
Real isolated container restart: `completion-language-restart-20260909T1229/`,
same container15b22fa47ce8..., running before restart12:19:27Z, restarted12:19:59Z,
exited0 at12:20:00Z. result.json PASS: checkpoint/source/cache preserved, no model
request, review settled, duplicate discovery refused. Only fixture data and three
read-only code files mounted. Earlier1226 fixture failed on an invalid test API
argument before preparing the checkpoint; its exited container/log is retained,
not counted as restart PASS. Log name time labels are identifiers; Docker's recorded
start/finish times are authoritative.

Runtime recheck remains bd2689437ea36d3c04f3193538862a931331c7f0/image6fc360c4...;
no Production deployment, source/output mutation, hold release or Gate reset.
`download-bounded-20260909T1215.json`: TRIPPED,68holds, original Gate212600;
target2565:12 still extract_failed/due with no torrent and no completion. Replacement
request progressed to3other targets; this is discovery progress, not canary claim.
Formal new AI/download/extraction deliveries this increment: **0/0/0**.

Next: retain the exact source/decision mismatch as an unresolved held incident,
complete evidence-bound controlled recovery support for THIS incident (do not label
it as the previous postprocess or line-repair cause), then safe deploy/actual-image
proof and automatic real claim. Formal download/extraction and frozen Gate acceptance
remain open. M2 is not complete; no M3/M4 work or acceptance-rate claim.

## 2026-09-09 — reproduced post-interception queue overwrite (candidate only)

The exact completion rejection regression passes before the new characterization.
However, calling `mark_ai_queue_done(detected_existing=True)` after a strict
rejection returns True and overwrites the paused queue. This is a reproduced
secondary safety defect, NOT proof of the original two failed strict predicates.
Candidate `scan_state.py` prevents completion when the same-media open obligation's
latest attempt remains `review_required/m2_strict_completion/incorrect_completion`.
An actual verified delivery closes its obligation through the existing transaction;
no new recovery bypass, schema, QC or retry-budget change is introduced.

Server isolated tests: characterization RED; corrected `test_main_queue` and
`test_scan_state` **212 PASS**, including SQLite reopen, repeated discovery/direct
completion refusal, original source preservation and existing completion regression.
This is read-only candidate source mounted in the deployed image, NOT deployment
or a new production output. Current image/start time unchanged; bounded running
queue query returned no running items. Exact Gate query still reports TRIPPED and
one retained member. No latch, live database, source or subtitle was modified.

Evidence under `/logs/m2-mapping-budget-20260908T2117/`:
`completion-discovery-red-20260909T0819.log`,
`completion-discovery-regression-20260909T0821.log`,
`runtime-inspect-20260909T0816.log`, `current-idle-20260909T0816.json`,
`gate-member-diagnosis-1788938163088163596.json`.
Initial diagnostic used an unavailable `/app/work` helper path and failed without
reading state; corrected stdin invocation is the cited idle evidence.

Remaining: identify the original strict failure transaction before controlled
recovery; production activation of this candidate; real download/extraction formal
delivery; frozen Gate disposition. Formal additions this increment: **0**. M2 incomplete.

## Deployed mapping fix; new completion breaker requires recovery

Safe handoff `/logs/m2-mapping-budget-20260908T2117/` exited0; original child79874
ended naturally, safe idle/seal succeeded, deploy.exit0. Actual Worker
bd2689437ea36d3c04f3193538862a931331c7f0, image
sha256:6fc360c4d55e5b03a549885206aeb553dc34297c19e3cf4e144539bb4c1027bd,
container6b0d178da597ac365d9ca63c76d4a93f8dfc59e453c9ccf30a279c3f642dae15,
started2026-09-08T21:23:39.736693332Z. WebUI175a02a7bad46e0b6fa2372c59f39e8dd272911e.
Actual-image78tests PASS, fresh7/7breakers PASS, production_resources_affected=false.
Reconciliation m2-recon-mapping-budget-20260908T2117 retained68holds,0newdifferences,
6979recoverable and7050retained identities. Receipt sha256
611063777e5c058e54003766a3a4538f09f79303cb64bf1383504f3d70abbd2e.
Controlled recovery initially ARMED; new Gate m2-gate-20260908T212600258113Z-81b6d3117d,
baseline m2-guardrail-v1:e5bb306a6538c2c49e4bc6ee started21:26:00.258113Z at0/20.
Old failed Gate/receipt preserved; no prior score copied.

Current verification at1788919803 contradicts continued ARMED: breaker TRIPPED
incorrect_completion, observed1788903489.718634, stage m2_strict_completion.
strict_failure_count2 means TWO FAILED PREDICATES, not two tasks. AI automatically
claimed at1788903093.4545546 and stopped further claims after interception.
New Gate currently has1terminal NEEDS_REVIEW member/strict0; do not complete/reset it.
Exact attempt aiatt_7ba2996f530badc5ae48a968ed009c86cbfd832189180a3b30f1795798e8209d
ended review_required; obligation open, pipeline job21ab68ce485f426d8194f6f51ac05ea4
remains QC with succeeded stage records. Later queue snapshot says done, so legacy
queue convergence also needs explanation; no verified formal delivery credit.

Evidence: recovery-closeout.json (historical recovery success), ai-successor.json
(current paused scheduler), download-successor.json (current TRIPPED/68holds),
gate-member-diagnosis-1788919876587068172.json, completion-trip-1788919954418273076.json,
completion-context-1788920145877869164.json. Read-only current completion context
finds the correct job and matching current filesystem fingerprint without error;
this does not prove the earlier in-transaction completion was correct.
Next reproduce that completion transaction from isolated evidence, including both
strict predicates and later queue-done convergence, before any fix/controlled reset.
Do not bypass breaker or relabel the review. Download request shrank584to400 and
first-series advanced, evidence of resumed discovery, NOT proof of this target's
next download claim or formal output. Formal download/extraction0; M2 incomplete.

## Reproduced enqueue preparation stall; candidate repair

Actual PID50 nonblocking stack `worker-single-stack.log` locates thread162
mikan-qbit-enqueue-watch in _queued_library_scan_mappings -> _path_is_relative_to
-> realpath, before the bounded discovery loop. No queue lock or job lease owned;
deployment/reconciliation holds false. Replacement target2565:12 is position295
in the retained584target request, not a newly claimed download. Read-only owner
evidence replacement-owner-1788884653335761924.json. No lock/hold was cleared.

Isolated characterization mapping-budget-before.log reproduces2400path resolutions
for40videos/30mappings (expected at most70), and missing deadline support. Candidate
repair caches resolved roots only within this operation, resolves each video once,
preserves first-mapping/symlink containment, and propagates the existing discovery
deadline into queue-priority mapping. Expired work returns without claiming or
mutating Queue. No QC/threshold/source/manifest/Guardrail change. Cooperative deadline
checks do not claim to interrupt a single blocked kernel filesystem operation.

mapping-budget-focused.log:37PASS. Initial integration run exposed wrong-call-site
deadline wiring and an uncommitted SQLite test fixture; both corrected and original
failure log retained. mapping-budget-integration-fixed.log:40PASS, including real
SQLite reopen/no Queue or source changes, actual symlink containment, interrupted
budget/re-entry, regular replacement-before-discovery and existing source/import
regressions. Tests ran isolated, network disabled, candidate checkout read-only.
Diagnostic py-spy0.4.2 is only in logs/.../diagnostic-tools, not Worker dependencies;
no extra container capability or process pause was used.

Candidate not deployed. Next create a NEW timestamped owned safe deployment attempt
for this revision, preserving current7eeb848 runtime, SETTLED failedGate144756,
68holds/UNPROVEN and all prior receipts. Do not rerun the old1445deployment scripts
with their immutable IDs. Verify safe idle, deploy via existing updater, test actual
image and controlled handoff; then prove automatic source recovery and formal output.
Formal download/extraction0; M2 remains incomplete.

## Exact source and member diagnosis — 2026-09-08 16:17 UTC

Read-only actual-image extraction into evidence-only scratch reproduced the same
QC rejection for both tracks; downloaded source hash/stat unchanged, publications0.
Evidence `/logs/m2-download-revalidation-20260908T1445/bilingual-diagnosis-f09tgu3s/`
contains extracted original ASS, ffmpeg logs and result.json. TC styles: Text-CH373
events/1overlap/1empty; Text-JP371/1overlap; Screen10/0; OP-JP58/27; OP-CH8/0.
SC also has one empty Text-CH event and one overlap in each dialogue-language style.
Global overlap detection does not distinguish ASS presentation channels, but the
candidate still fails independent empty/same-style checks. A style-only exemption
cannot establish acceptance; no QC relaxation, source rewriting or runtime fix was
performed. Existing watchable-bilingual test covers one cue containing both languages,
not independent concurrent style events. Do not expand QC merely to pass this canary.

Exact frozen-member read: `gate-member-diagnosis-1788884136367898483.json`,20members,
18review/2failed, all stage_checkpoint_history_complete=false. The summary code
counts every non-true value as checkpoint_loss_count; this is incomplete strict
history evidence, not20 demonstrated deleted files. Member0 qualification reasons
also lack final output/QC/source/decision evidence, so strict failure is not based
solely on that metric. ARMED remains confirmed. Gate result stays FAIL and immutable.

Recorded terminal source disposition: failed_info_hashes includes3aeb9cf..., pending
target_due=true; existing replacement request includes this target among584targets,
next_retry_at1788883990.920198. This proves durable alternative-source intent, NOT
a subsequent download claim. No duplicate request, direct DB edit or failed-hash
re-add was made. Next bounded step: inspect this same target's existing automatic
alternative-source decision/claim; if unavailable retain policy backoff. No need
to re-extract this unchanged rejected source. Formal download/extraction0; M2 open.

One final exact-target read at1788884420 (`alternative-after-source-diagnosis.json`)
still finds no new info_hash/queued_at, despite retained request next_retry_at now
past. This is NOT verified live waiting and NOT proof that another source is
unavailable. Next inspect the existing replacement scheduler owner/job/lease and
its actual failure/backoff reason; do not simply repeat status or claim automation
has selected a successor. No admission/lease/protection was altered.

## Terminal canary and frozen Gate disposition — 2026-09-08 16:12 UTC

The existing bounded observer expired, not the download. A subsequent exact-hash
read proves download complete (progress1, amount_left0, stalledUP), followed by
automatic extraction claim at1788881346.9366496 and terminal1788881399.1704633.
Hash3aeb9cf71d052508dccb77817a4f264f8216dced was NOT re-added. Episode12 mapped
to the actual completed file, target confidence1.0, match version pending-release-v1.
Both embedded bilingual ASS tracks parsed PASS but unchanged hard QC rejected:
TC820 events: empty_dialogue_text1, timing_overlap413, hallucination_text1;
SC815 events: empty_dialogue_text1, timing_overlap408, hallucination_text1.
The hallucination issue sample is empty text, not evidence of invented spoken content.
Result subtitle_validation_failed/no_usable_chinese, extracted_count0; queue job
status replaced is a source-disposition terminal, NOT successful publication.
Pending retains extract_failed and failed hash; no completed_at or formal delivery.
Do not relax QC, re-add this source, or classify the source video as damaged.
Next inspect the retained bilingual-source evidence and existing import normalization
contract in isolation; determine a reproducible defect versus unsupported source
before any runtime fix or alternative-source action. Formal acceptance remains open.

Evidence root `/logs/m2-download-revalidation-20260908T1445/`:
`canary-download-after-observer.json` contains full extraction/matching/QC evidence.
`terminal-closeout-1788883929717665338.json` preserves exact Gate, automatic summary,
runtime, pending and checksums. Source and all3 prior sidecars remain unchanged.
Earlier terminal-closeout1788883865919080852 did not resolve the relative summary
path; summary_present=false there is a verifier path issue, not missing automation.
The corrected read-only helper resolves the persisted basename under the existing
m2_server_canary_observations directory; no Production code/state was changed.

Gate m2-gate-20260908T144756513012Z-ad0fe3a765 is SETTLED20/20 with18NEEDS_REVIEW,
2FAILED, strict0, safety_gateFAIL. Summary emitted1788880578.4625626 automatically.
Its checkpoint_loss_count20 requires member-evidence investigation; do not infer
20 physical checkpoint deletions solely from this aggregate. Breaker trips, false
completion, source mutation and duplicate job/publish counters are0. Preserve this
failed cohort and original receipt, no backfill/reset or mixed-version PASS.
Runtime still7eeb848b70ab5027d17f3a45c83e3d7400d80004/ARMED,68holds preserved.
Independent next AI job755338a47cdc4990a877b0402eb9ea06 automatically claimed
at1788883638.2799692, ASR/running attempt3, heartbeat1788883841.4687028:
`ai-successor-terminal-download.json`. This is processing, not a delivered subtitle.
New formal AI/download/extraction deliveries in this recovery remain0/0/0;
prior M3 AI delivery remains separate. No runtime edit/deploy/restart this follow-up.
M2 is incomplete. Goal remains active; next work is specific source/QC and Gate
member-evidence diagnosis, not another download wait or full-project audit.

## Bounded live download follow-up and prepublication evidence

Same hash3aeb9cf71d052508dccb77817a4f264f8216dced remains downloading; latest
canary-download-fourth.json records0.80031618195progress and7508952035downloaded
bytes/9383197103total. No extraction row yet. This is a verified live wait, not
a stalled/failed download or completed subtitle. No re-add, priority override,
runtime edit, deployment or Gate change occurred in this follow-up.

Before actual publication, before-publish.json/full.log verifies current source
SHA b999b8081a3ea63b756b5c96a32db9db6c484fe00d72477b9dfa800aa09829f8,
size694204096/mtime1639746000573000000 unchanged from canary intent. Three existing
sidecars captured by path/hash/size/mtime; no file was removed to manufacture a case.
valid_finished=false, valid_ai=false, official_languages=[].
existing-subtitles-validation.json applies existing full import policy read-only:
old zh-TW.ass and zh.ass fail hard QC; therefore missing-valid-TC is proven even
though subtitle files exist. English sidecar is retained too; its Chinese-import
policy rejection is not a claim that English captions are universally invalid.

Prepared operational read-only verifier work/m2_download_formal_verify.py for this
exact target's official_subtitle_versions manifest, final bytes/language/QC, source
hash and prior-artifact backups/unchanged files. It has not yet verified a formal
publication. Still require actual extraction/matching/ledger and duplicate-trigger
evidence after normal Worker processing. Formal delivered count remains0.
All evidence is under /logs/m2-download-revalidation-20260908T1445/.

## Deployed and recovered; real canary downloading — 2026-09-08 14:55 UTC

Actual Worker7eeb848b70ab5027d17f3a45c83e3d7400d80004, WebUI175a02a7bad46e0b6fa2372c59f39e8dd272911e.
Image sha256:078f77c90f0dfd19192108bed4b7086a9ac1aa4c5baeb6eac3506f9b413fcb9d;
container c0d0c3b624ac83f8d2da2e644b21916b13397dd649e1ea323f7fd7210a15337a,
started2026-09-08T14:42:30.991964676Z; source revision73160fd55a38a536c6c1be0f69622c63d6707671607dfde9d2846805941278af.
Safe deployment20260908T144053Z-3222495 completed with online backups/manifest,
after original child3140201 naturally ended and safe endpoint idle=1, AI/extract/Mikan0.
No task was killed/frozen. Backup `/work/deployment_backups/20260908T144053Z-3222495`.
Existing deploy quality-sidecar housekeeping inspected2456 directories, matched0,
migrated/deleted0; no additional media-library inventory was performed.

All evidence below is under `/logs/m2-download-revalidation-20260908T1445/`.
Actual image relevant329tests PASS (`actual-image-validation.log`); fresh breaker7/7
PASS (`fresh-fault-suite.log`, FI run m2-guardrail-fi-20260908T144547928235Z-3a96125a,
production_resources_affected=false). Initial isolated-network coordinator failed
provider_endpoint_not_on_attested_host; original failure preserved. Host-network
coordinator revalidated existing attestations and completed controlled recovery
without retesting/redeployment (`recovery-host-network.log`, exit0, recovery-closeout.json).

Reconciliation m2-recon-download-revalidation-20260908T1445:68previousholds retained,
0newdifferences,7022recoverable identities,7008retained-state identities. UNPROVEN
is unchanged. Old planned receipt sha256:c77bbe21bd6342578770d22f77f7210a93ec85a2dff944488b108a0d5eed3774;
reconciliation receipt sha256:bd6f220c00c954972e855cd09aec6036ac0b8f595824e5c3eb03ef0f627e6b40.
Old settled Gate121706 row is exactly equal to original receipt; receipt hash matches.
New Gate m2-gate-20260908T144756513012Z-ad0fe3a765, baseline
m2-guardrail-v1:0fd9069deb41ef803987a229, initialized0/20; ARMED and both admission
holds false. No old results copied. Do not claim Gate pass or overall M2 acceptance.

Canary2565:12 was revalidated via public controlled API at14:48:36; anonymous
request c5ddc7dc1fb7b26ca0f1800397d48591b9eee403831b5b280a673846273261db.
Immutable canary-intent.json retains current source checksum and prior revision;
old completed snapshot stays inside pending completion_revalidation. One bounded
existing replacement stage added a NEW source hash3aeb9cf71d052508dccb77817a4f264f8216dced
at14:55:01. qB exact-hash proof at1788879336:downloading,0.02545289898progress,
227587806downloadedbytes/9383197103total, project categoryllm-sub/tagsmikan,mikansub.
No failed old hash was re-added. Download batch is not complete or validated yet.
`canary-download-first.json`, `single-enqueue-full.log` and `single-enqueue-result.json`.

AI independently resumed: e707c192ab7b411abb33440ce9b8e583, running attempt4,
ASR/asr-full-retranscribe-v1, transcription heartbeat1788879229.6354964
(`ai-successor-after-download.json`). Earlier scheduler deployment_hold label did
not prove a live hold; direct flags were false and normal scheduling resumed without
manual Retry or clearing protection. Early canary-followup-1 read omitted SQLite
authority registration; corrected canary-followup-sqlite proves archive persisted.

New formal deliveries this recovery: AI0/download0/extraction0. Prior M3 AI1 remains
separate. Next follow ONLY this live torrent's bounded completion/extraction/import,
re-read final formal subtitle/QC/manifest/source checksum, verify dedup and server
successor. Do not rerun enqueue, canary intent or completed deployment scripts.
Do not wait all history/20 Gate; keep M2 incomplete until formal evidence exists.

## Combined candidate ready for safe deployment preparation

Final combined candidate targeted suite:122/122 PASS, exit0, server evidence
`/logs/m2-resumed-acceptance-20260908/combined-revalidation-uJ60ID/full.log`.
Mounted only candidate source/tests on existing image, read-only root, tmpfs,
no network and no Production data mounts. Mikan dispatch now also verifies the
current checksum, not only metadata. Planned and authorized reconciliation tests
cover a genuinely settled20-member failed fixture, receipt/summary preservation,
idempotent recovery and a new empty Gate; shared frozen-observation regressions pass.

The reproducible SETTLED handoff defect below is fixed in candidate code:
preparation accepts a verified finished cohort, drift still persists a breaker,
and only authenticated controlled-change receipts may recover while retaining
the exact old SETTLED row and summary. Generic breaker recovery is not broadened.
Old evidence is never rewritten as ACTIVE/INVALIDATED/PASS. Earlier failed logs
are retained (settled-handoff-fix-DM2P46 and b0erfU);103tests first passed in lPJLjW.

Not yet deployed or used on Production. Next: owned safe-idle preparation using
the verified SETTLED-capable coordinator, safe-update-stack, actual image/fault
attestation, controlled reconciliation and one real download/extraction delivery.
Preserve68holds, UNPROVEN, source/valid outputs and old Gate. M2 remains incomplete.

## Deployment blocker reproduced; dispatch guard completed

Candidate Mikan reconciliation now rechecks the authorized target path, size/mtime
and source hold before later ordinary/replacement/deferred enqueue. A changed or
held source is refused and its blocked reason persists inside the existing archived
revalidation record without clearing retry/backoff or old completion evidence.
14 focused tests PASS on actual-image isolated candidate mounts:
`/logs/m2-resumed-acceptance-20260908/dispatch-revalidation-4ZhbRN/full.log` (exit0).
Earlier nyU8qF failure showed the generic candidate-review writer would classify
these reasons as ambiguity; that writer was not reused. No Production invocation.

Safe deployment has a newly reproduced code blocker, not merely a busy worker:
`prepare_runtime_change` requires active_gate, while the actual old Gate is SETTLED.
An isolated real20-member fixture (2FAILED/18NEEDS_REVIEW), settled via normal
record_terminal_evidence, reproduces planned_change_active_gate_mismatch and proves
the old gate/summary remain unchanged. Characterization test PASS is NOT a fix.
Evidence `/logs/m2-resumed-acceptance-20260908/settled-handoff-repro-qH3WZy/`.

Next: minimally extend controlled planned/reconciliation recovery to retain an
exact receipt-bound SETTLED gate and immutable summary. Review preparation,
receipt validation, reconciliation, recovery and replay together; they currently
expect ACTIVE/INVALIDATED status at several boundaries. Do not mutate SETTLED to
ACTIVE/INVALIDATED just to satisfy those checks or copy results into a new Gate.
No admission pause, deployment, Gate mutation or new formal subtitle this turn.

## Guarded revalidation and runtime checkpoint — 2026-09-08 14:11 UTC

Implemented `MikanWorker.revalidate_recorded_completion`: one reviewed target,
existing queue/video/state locks, revision-bound full prior entry archive and
current source checksum, unique cached target, source holds, active AI ownership,
valid output, exact historical/failed download hashes and extraction records.
Existing torrent or mapped content refuses reopening so reuse is required first.
Archive and retry intent share one pending write; existing known-episode discovery
resumes it after restart, retaining backoff, failure exclusions and terminal guards.
No parallel Queue/request consumer was introduced. Historical continuity stays UNKNOWN.

Server isolated candidate-image tests: 17/17 PASS, exit0. Includes real temporary
file locks/pending storage, SQLite-authoritative replay, fresh Python-process
rediscovery twice after simulated post-commit interruption, refusal paths and five
existing replacement/terminal regressions. External source/output/qB inspection
boundaries in new tests are stubbed; this is NOT real download/publication proof
or a Docker restart acceptance. Log:
`/logs/m2-resumed-acceptance-20260908/guarded-revalidation-GxlNBC/full.log` (+exit.txt).
Earlier failed fixture-normalization test remains in guarded-revalidation-Vak4dZ.

Read-only runtime proof at1788876700:
`/logs/m2-resumed-acceptance-20260908/revalidation-runtime-IgaM5m/`.
Worker738e96eafec66a6f62db31b9247cd58bf0aec6df, WebUI175a02a7bad46e0b6fa2372c59f39e8dd272911e,
ARMED/runtime_baseline_match, same container/image and frozen Gate121706 (18review,2failed).
Successor7769498c4a8b45879a3f1734cc2e3b00 is TRANSLATING, queue running,
translation heartbeat1788876700.477003, batch60/78. No forced stop or deployment.
Next: complete entry/locking review and necessary integration verification, then
safe owned deployment/handoff and one real accepted download/extraction publication.
Code is not deployed or used on Production. Formal new download/extraction subtitles0;
M2 incomplete; M3 evidence reused separately, not counted toward this acceptance.

## Archival transition implementation checkpoint — 2026-09-08

Added a non-wired pure transition helper and five focused tests. Server isolation
using deployed image e34372dc6ae9f1e28b293ce6f9dd61414f7c0b292ad32588f43dcd2613f1c617
passed 5/5 (exit 0), with no network or Production data mounts. Evidence:
`/logs/m2-resumed-acceptance-20260908/completion-revalidation-pzBzIi/full.log`
and `exit.txt`. Tests cover complete historical snapshot preservation, unchanged
input/failure evidence, stale revision refusal, active/unreviewed history refusal,
missing fingerprint refusal, and serialized replay preserving subsequent progress.

This is NOT a durable consumer, real restart integration test, authorization
entry, or formal publication acceptance. No Production state was reopened and
no deployment occurred. The guarded existing-request consumer still needs current
source/output/hold/lease/reusable-download checks under existing locks, durable
intent/replay integration and related failure-path tests before any deployment.
M2 remains incomplete; formal download/extraction additions remain 0. Retain the
settled frozen Gate FAIL and all quarantine/preservation evidence.

## Reopen safety preconditions checked — 2026-09-08 13:56 UTC

Exact3 targets2565:12,3085:13,3932:3 exist and are not held. Production
has_ai_finished_subtitle and has_finished_subtitle both return false for all3;
verified official languages empty. Missing official subtitles alone was not used
to assume AI subtitles absent. No valid output was removed to make these cases.
Read only the6 known historical/last-completed extraction records: all present
(3replaced,3success), but all6 stored qB-to-Worker content paths currently absent.
This does not prove permanent source loss or that historical success was false.
No full directory scan, torrent addition, state mutation, or publication occurred.
Evidence /logs/m2-resumed-acceptance-20260908/three-full-output-preflight.json
and three-retained-files.json. Formal additions0. Controlled revalidation/reopen
still requires guarded implementation/integration tests and safe deployment;
do not call the private allow_completed_reopen flag or clear completion fields
directly against Production. Preserve original completed/extraction records.

## Exact historical-success selection blocker — 2026-09-08 13:52 UTC

Bounded existing-source searches for2565:12,3085:13,3932:3 returned respectively
6/42/6 releases and2/8/1 episode candidates. All6 configured source searches
succeeded per case; no release was selected or torrent added.
Exact current selection predicates: active=false,deferred=false,
terminal_success_short_circuit=true for all3. Their historical extracted counts
12/1/1 and August27 completion timestamps remain recorded under
KEEP_RECORDED_COMPLETE_NOT_REVERIFIED. Thus zero selection is NOT evidence of
no suitable release or source outage: candidate filtering is never reached.

Code inspection: _choose_release_for_episode rejects terminal history before
release comparison. _mark_pending has an allow_completed_reopen branch that
archives history, but no current caller supplies that option. The ordinary
replacement request cannot overcome this terminal check. No completed fields
were cleared; original history and failed/seen protections remain intact.
Next: reproduce and verify a narrowly authorized historical-success revalidation
path with exact target/source identity, missing valid output, no active ownership,
immutable retained completion evidence and idempotency before any runtime change.
Do not assume historical success was false merely because current files are absent.

Logs /logs/m2-resumed-acceptance-20260908/:
source-2565-check.log,source-3085-check.log,source-3932-check.log,
three-selection-reasons.json and the timestamped source JSONs linked by each log.
No code/config/deployment/Gate mutation; formal additions0 this turn.

## Resumed M2 and frozen Gate disposition — 2026-09-08 13:49 UTC

Gate m2-gate-20260908T121706917079Z-5900a7525f automatically SETTLED20/20;
18NEEDS_REVIEW,2FAILED,strict_verified0. Automatic summary safety_gateFAIL.
This is a failed acceptance result, not a missing observation or request to reset.
Summary emitted1788874881.027214; fixed cohort and evidence remain untouched.
Summary /work/m2_server_canary_observations/m2-gate-20260908T121706917079Z-5900a7525f.json
SHA2687618aba2cd19f102582ec507cf7f7b8c3ac859619cec3892cdab26d56c837
matches immutable stored digest. Breaker remains ARMED/runtime_baseline_match;
Worker738e96eafec66a6f62db31b9247cd58bf0aec6df,
WebUI175a02a7bad46e0b6fa2372c59f39e8dd272911e. No deployment/Gate mutation.
Current Worker child2527267 was live at initial read; no forced interruption.

Existing5-case refresh (no library scan):3380:6 and2911:10 each have2indexed
targets, retained identity review;2565:12,3085:13,3932:3 each have1target but no
verified TC completion. None of their historical project torrents is present.
One bounded existing-policy alternate-source search for260:E1/E2/E3/E8 returned
35releases,6successful sources,0failed sources and0selected candidates. Source
service availability does not prove suitable matching media or swarm availability.
No failed/seen reset, torrent add, source mutation, or formal publication.
Next: investigate suitable sources for the3uniquely indexed existing old targets.
M3 AI1 is historical separate evidence, never counted as download/extraction or
replacement Gate membership. New formal download/extraction additions0.

Logs /logs/m2-resumed-acceptance-20260908/:
initial-gate-runtime.json,initial-worker.json,initial-process.txt,
old-five-check.log,alternate-source-check.log,gate-summary-resolved.json.
Full source reports linked by those logs under m2-recovery-unblock-20260905T064508843990Z.
Initial gate-summary-evidence.json interpreted basename as cwd-relative;
resolved report uses the actual configured output directory and verifies its hash.

13:27UTC M3 model/recovery engineering closeout completed separately; actual-image
duplicate ingress/promote/scanner refusal2cycles passed in isolation. No additional
subtitle delivery, no runtime/Gate reset. M2 remains incomplete: download/extraction
formal acceptance and strict frozen Gate still open; AI1 is not download/extraction
or Gate substitution. Final runtime ARMED/admission open/68 holds preserved.
See docs/M3_ACCEPTANCE_AUDIT_20260908.md for exact evidence boundary.

13:16UTC follow-up: the newly delivered AI target9f5394... retained manifest
digest matches exact prepared/provider-confirmed model lineage; runtime/Gate and
request watermark agree. Existing finished-subtitle/terminal-reprocess predicates
prevent ordinary reprocessing; formal hashes unchanged. No new dispatch/publication
was attempted, no extra delivery counted (AI1/download0/extraction0 unchanged).
Intermediate CN cache absent; evidence uses retained manifest digest, not invented
bytes. See M3 doc and known-alternative-9f/manifest-lineage-idempotency.json.

## Real model response and formal AI publication — 2026-09-08 13:12 UTC

Known job9f5394b655b340b2822cc43337dbcaf0 auto-claimed after7d0c98...
naturally finished NEEDS_REVIEW/paused/attempts5. The new live child2081593
entered TRANSLATING; heartbeat1788872824.0869553 recorded batch21/48.
No manual priority override, model-side private Queue, or forced interruption.

At1788872963.3860781, the real job had91 MODEL_REQUEST_RESERVED and91
MODEL_REQUEST_SETTLED, all outcomes RESPONSE with valid response digests.
Configured Sakura and qwen2.5:7b-instruct-q4_K_M both used; each reservation
remained within its effective durable attempt limit. Runtime identity hash
e9414f2cda46ccfc61f6b48fdf30d054a7ef032adc06d47fcf8e59bd176bee7b
and provider bindingb91c298cd5186f24dbf03a45db19ad64f46f297ea8ce5a6d19d9b64d41c45415
match loaded baseline Worker738e96e. Also3 MODEL_OUTPUT_PREPARED and1
MODEL_OUTPUT_PROVIDER_CONFIRMED events. Event counts alone are not strict Gate
acceptance or proof of the final artifact's exact confirmation lineage.

Formal re-read at1788873170.8224776: Pipeline COMPLETED, Queue done,
delivery obligation aiobl_6369c08d8fb849e3c50dd8be9fa9a21739e8564d03dbe75569a506700f14b411
succeeded/attempt_count2. Manifest v2 validated against exact ledger obligation
and policy, all file hashes, and translated_trilingual publication semantics.
Manifest SHAe6104d894429497ca3c502532d4ecb31839cb6c0337a0015ad74952184d1d0eb.
Three formal ASS files each parse285 cues and pass hard QC using Production's
explicit japanese/translated_zh_cn/translated_zh_tw roles; warnings retained.
Source video, original JA and existing sidecar hashes match pre-recovery intent.
New formal TC subtitle count **1** (one AI target, not three language outputs).
Download-derived additions0; extraction-derived additions0; old AI delivery not recounted.
Initial generic-role recheck misclassified simplified Chinese; retained initial
formal-verification.json is superseded by formal-verification-language-roles.json.
Only the read-only verifier role was corrected; no QC policy/output changes.

Evidence /logs/m3-review-convergence-20260908T1210/known-alternative-9f/:
followup-6.json,followup-6-scheduler.json,followup-6-process.txt,
model-evidence-1.json (provider summary accessor absent; sample binding present),
model-evidence-2.json (correct nested binding and baseline comparison),
output-probe.json,formal-verification-language-roles.json.
No code/config/deployment/Gate change. M2 download/extraction and strict Gate
remain incomplete. Finish exact confirmation-lineage/idempotency and remaining
M3 acceptance checks before calling M3 complete; do not retry this succeeded job.

## Canary terminal closeout — 2026-09-08 12:55 UTC

The bounded existing ASR chain (large-v3 -> large-v2 -> independent medium)
did not yield accepted ASR output. Job e15760a3eac4466eb2ea02c430d64418
naturally exited: child1852332 absent; Pipeline NEEDS_REVIEW, Queue paused,
attempts3, deterministic_asr_quality/manual_review. MODEL events0.
Do not force another retry, reset its budget, or repeat the one-shot retranslate.
This is real bounded fallback/exhaustion evidence, NOT model publication success.
Formal additions0. Source and original sidecar checksums unchanged (2 files);
original retranslate manifest and both archived intermediates preserved.

Runtime check1788872149.3226001: Worker738e96e, ARMED/runtime_baseline_match,
admission unpaused,68 holds, same Gate121706. No code/config/deployment change.
Evidence /logs/m3-review-convergence-20260908T1210/:
bounded-asr-wait-4.json, bounded-asr-terminal-6.json,
bounded-asr-terminal-6-process.txt, final-canary-safety.json,
final-canary-runtime.json. Do not continue waiting on the exited child.

M3 real translation/provider-confirmed publication remains unverified because
this canary did not pass upstream ASR quality. A different verified eligible
source must use existing admission/ownership; do not bypass quality/lineage.
M2 download/extraction requires usable matching download; prior failed torrents
must not be repeatedly re-added. Strict Gate remains unaccepted, no backfill.

## Automatic second claim verified — 2026-09-08 12:48 UTC

This supersedes the earlier statement that the canary second claim was unobserved.
Job7d0c98eb4a9947a9a65ce61f051c6461 finished NEEDS_REVIEW; Queue paused,
attempts4/manual_review/deterministic_asr_quality. No MODEL events.
The server then automatically claimed e15760a3eac4466eb2ea02c430d64418 at
1788871596.785272: Queue running/attempts2, Pipeline ASR, live childPID1852332.
Heartbeat1788871607.6413944 records configured faster-whisper-large-v3.
Source-selection checkpoint0462ced110b84c38a274cfb4c0e52b6e hash matches
fbfc432c5f66782bde14d8b42978d6781a55ea384f6fd9c514b67e6bce5b0c16.
ASR has not produced a validated checkpoint/output; empty ASR checkpoint hashes
are not counted as valid checkpoints. Model events remain0; formal additions0.
No manual scheduling intervention, runtime edit, deployment or Gate reset.

Fresh runtime check1788871696.1845965 confirms Worker738e96e, WebUI175a02a,
ARMED/runtime_baseline_match, admission unpaused,68 holds and unchanged Gate
m2-gate-20260908T121706917079Z-5900a7525f.
Evidence in /logs/m3-review-convergence-20260908T1210/:
successor-followup-1.json, successor-followup-1-process.txt,
two-attempt-evidence.json, current-runtime-1249.json.
Next bounded observation must follow existing child1852332; no restart based
on an observation timeout. M3 model/provider-confirmed publication and M2
download/extraction formal publication/strict Gate still require actual proof.

## Bounded follow-up — 2026-09-08 12:45 UTC

Runtime remains Worker738e96eafec66a6f62db31b9247cd58bf0aec6df /
WebUI175a02a7bad46e0b6fa2372c59f39e8dd272911e. No code/config/deployment
or Gate change in this follow-up; latest guardrail check ARMED/unpaused.

Canary e15760a3eac4466eb2ea02c430d64418 ended its ASR attempt in
NEEDS_REVIEW: deterministic_asr_quality (one-character/punctuation-only fragments).
Existing server review remediation requeued it with attempts2 and
asr-full-retranscribe-v1; this second attempt has NOT been observed claimed.
No MODEL events and no formal delivery. Do not repeat the one-shot retranslate.
Source and original media-side subtitles checksum unchanged (2 files);
retranslate manifest hash unchanged and both archived intermediates preserved.

The scheduler is not globally stuck. At1788871190.2082245 it automatically
claimed a different successor, job7d0c98eb4a9947a9a65ce61f051c6461.
At1788871518.5316741: Queue running/attempts3, Pipeline ASR,
heartbeat1788871499.9581223; existing independent smaller ASR fallback
Systran/faster-whisper-medium active. Host childPID1771896 was alive.
Current waiting reason for the queued canary is another task using the worker;
the preceding idle interval was not established as a scheduler defect.
No forced retry/lock clearing, and no waiting for this successor to complete.

Evidence /logs/m3-review-convergence-20260908T1210/:
canary-followup-check4.json, canary-source-safety-after-asr.json,
canary-wait-protection.json, canary-scheduler-evidence.log,
canary-next-claimed.json (despite name, e157 remains queued),
canary-next-claimed-process.txt, actual-successor.json.
This follow-up added0 formal subtitles; M3 real model/provider-confirmed
publication and M2 download/extraction formal publication/strict Gate remain
unaccepted. Original Gate and68 holds preserved; preservation UNPROVEN.

## One controlled cache recovery actually claimed — 2026-09-08 12:30 UTC

No code/config/deployment/Gate change. Runtime738e96e remains baseline.
Read-only eligibility rejected the missing-Japanese-source shortcut for known
job9f5394... (JA/CN both285cues); no cache was removed for that job.
A separate pre-current-Gate reviewed job e15760a3eac4466eb2ea02c430d64418
passed existing reprocess preconditions: open delivery obligation,attempts1,
exact failure_revision0f5a53ecba86e1f0e571fad2, not held, no verified formal
subtitle, and only2 archive candidates, both in /work. A separate private-DB
real-model test was not launched: shared-provider ownership must remain in the
existing Production request/admission mechanism.

Using existing reprocess_video(mode=retranslate, queue_mode=auto_review), a
single user-authorized revision-bound recovery preserved retry budget under
policy m3-authorized-single-cache-rebuild-v1. It reversibly archived2 intermediates:
 /work/manual_ai_reprocess/1788870543269839390-e9f4301866eb8216-retranslate/manifest.json
SHAecb2d4445774f7563e1994d970a93cbf46a65de98f33f7221d747a907abeed9d.
Source video, JA input and existing subtitles matched pre/post checksums at
remediation closeout; attempts remained1. No old model lineage was fabricated.

Actual subsequent Worker claim confirmed: Queue running, Pipeline ASR,
subprocessPID1616572 alive. Cached JA opening verification rejected0.0-3.9s;
existing bounded prompt-free full ASR fallback started, not a bulk Whisper bypass.
Legacy heartbeat1788870586.233811. No MODEL events or formal delivery yet.
Do not repeat the one-shot remediation: immutable intent already exists.
Continue only this known live attempt, respect its terminal outcome/budget;
no forced retry, no waiting for all translation/backlog/20-job Gate.
Evidence /logs/m3-review-convergence-20260908T1210/:
known-cache-eligibility.json,retranslate-preflight.json,
retranslate-canary-intent.json,retranslate-canary-result.json,
retranslate-canary-claim.json,retranslate-worker-process.txt.
Formal additions0; M2/M3 acceptance remains incomplete.


## Review convergence deployed and proven — 2026-09-08 12:19 UTC

Actual Worker738e96eafec66a6f62db31b9247cd58bf0aec6df; WebUI
175a02a7bad46e0b6fa2372c59f39e8dd272911e unchanged. Image
sha256:e34372dc6ae9f1e28b293ce6f9dd61414f7c0b292ad32588f43dcd2613f1c617.
Safe deployment20260908T121255Z-1408116 exit0; backup retained.
Actual-image439 related tests PASS; fresh isolated breakers7/7 PASS.
Official recovery exit0, ARMED/admission resumed,68 holds preserved/no new deltas.
Reconciliation m3-recon-review-convergence-20260908T1210, receipt SHA
ddd2a2d3870721ac8b7ae451bc17364cc672c390a7721542f83ab6585c793b57.
7069 recoverable identities/6960 retained other states; not delivery counts.
Original planned receipt SHA6828376c5f5f6c1988f153716dc7ecfb4b3af141d01d77f6e5601423acd6124c.
New Gate m2-gate-20260908T121706917079Z-5900a7525f,
start2026-09-08T12:17:06.917079Z, baseline
m2-guardrail-v1:6decf6663590b59db7e8351a, initialized0/20. Old evidence retained.

REAL FIX PROOF: automatic claim at1788869865.229487; pipeline job
9f5394b655b340b2822cc43337dbcaf0 source attempta6ff6421e3dc4d388a2858011fa3965f
SUCCEEDED with checkpoint cca481408e327523b1620fa18f0cdd369990d7bbc81a2ea02077d237abd0db10
(byte hash verified), then rejected old model cache lineage. Queue paused/manual_review,
attempts1 AND formal Pipeline NEEDS_REVIEW now agree. No synthetic second attempt,
no legacy-cache bypass or false completion. Successor73ffcf607606424c9799d69b23e2d262
also automatically claimed at1788869904.9252076, settled NEEDS_REVIEW for materialized
subtitle structural QC; no protection relaxed. First two frozen members remain
reviews/strict0 and cannot be replaced. No MODEL events, no new formal delivery.
This validates the convergence fix, not M3 model-output or M2 production acceptance.

Evidence root /logs/m3-review-convergence-20260908T1210/:
safe-deploy.log/deploy.exit, actual-runtime-before-recovery.json,
actual-image-validation.log, attestation.json, recovery-closeout.json,
post-cycle-probe.json, real-review-convergence.json.
FI /logs/m2-guardrail-fi-20260908T121659158625Z-6a5f8c00/result.json.
Next focus real eligible model processing without bypassing old-cache lineage,
source/QC holds, resource limits or frozen cohort membership. Do not redeploy docs.


## Pending post-source review convergence fix

Bounded actual-baseline evidence in /logs/m3-writer-reservation-20260908T1155/
exact-model-jobs.json: three known jobs have no MODEL events. One is source
candidate_analysis_inconclusive; two are model_output_cache_lineage_unproven.
The latter Queue rows are paused/manual_review but formal jobs remain
SUBTITLE_DETECTION after successful source checkpoints. Parent reconciliation
previously fell through to the legacy bridge, which correctly refuses to invent
a failed attempt after a successful stage but left the authoritative job unsettled.

Candidate main.py adds a narrow parent-result transition: NEEDS_REVIEW only for
source_selection_review/source_selection_needs_review, current SUBTITLE_DETECTION,
no active attempt and an existing successful source attempt. It keeps the generic
late-telemetry protections and immutable successful attempts, consumes no extra
retry and does not permit legacy cache reuse. Existing expected-state transition
provides conflict rejection.

Real-store regression reproduces this exact mismatch against deployed image
(red test fails as expected); candidate399 related tests PASS, plus durable
store reopen/replay PASS with no new attempts and unchanged source fixture.
Logs: review-convergence-red.log/exit1,
review-convergence-regression.log/exit0, review-convergence-reopen.log/exit0.
Earlier before.log contains an initial fixture-signature error, NOT the red proof.
Candidate is not deployed. Runtime57773b2, current Gate,68 holds and formal output
counts unchanged. M3 model-output acceptance and M2 production acceptance remain open.


## Writer-reservation deployed closeout — 2026-09-08 12:02 UTC

Actual Worker57773b23f4f508a85b243b95f103f9f0106e1d86, WebUI
175a02a7bad46e0b6fa2372c59f39e8dd272911e, image
sha256:5d17cc2591b8c9a089a0e5784bd345ef41e3e4944971c86614bd6e3355730695.
Safe deployment20260908T115510Z-1179712 exit0; backup retained.
Actual-image293 related tests PASS; fresh isolated breakers7/7 PASS.
Official recovery exit0, ARMED/admission resumed. New reconciliation
m3-recon-writer-reservation-20260908T1155, receipt SHA
ee7327fafd9ab58a9e3907d34e02632b403030080a3d9d038d0d021b3db4f427.
Original planned receipt SHA0c748a73f88b002e6ccd1e5558b23507d3f3bbd4bc6b00ba7c6c9e3870cf12df.
Original67 source holds preserved. One additional post-pause source mtime change
was classified PENDING_REVIEW/UNKNOWN/checkpoint-incompatible, not absorbed as
safe: total holds now68. Old/current queue membership both true; mtime changed
1788867000012000000 ->1788868601216869500. Historical preservation UNPROVEN.
7079 recoverable identities,6950 retained other states; not delivery counts.

New Gate m2-gate-20260908T120036464868Z-2e44d4c28b,
start2026-09-08T12:00:36.464868Z, baseline
m2-guardrail-v1:590b25cdec507b8a8c21dd1a initialized0/20. Old evidence retained.
A bounded sample proved actual automatic claim1788868885.3830643; first frozen
member remained NEEDS_REVIEW/strict0 and is not replaceable. Pipeline job
a9aa915b39c549968013823ce5799846 completed source-analysis checkpoint
80cb26ac1193340dcd5547b3d4d569b9f197426cfe4599b4574e294a18caf22d (bytes hash matches).
This proves new runtime persistence/claim, not general elimination of contention
or M3 model-response/formal-output acceptance. Formal additions0; M2/M3 incomplete.

Exact E3 download follow-up: hash439ca54daae64082d358e64404bd94ee3b87b496 was
retained in failed_info_hashes with did not start; no torrent/extraction artifact,
no partial files retained. Existing policy owns alternate-source retries (600s
no-candidate backoff recorded). Not re-added and not counted as successful delivery.

Evidence root /logs/m3-writer-reservation-20260908T1155/:
safe-deploy.log/deploy.exit, actual-image-validation.log,
actual-runtime-before-recovery.json, attestation.json, recovery-closeout.json,
classified-request-*.json, post-recovery-probe.json, download-e3-check.log.
Fresh FI /logs/m2-guardrail-fi-20260908T120020916157Z-cb01849f/result.json.
Next: verify real model/provider-confirmed output without replacing frozen
members or bypassing old-cache lineage/source/QC review. Do not redeploy for docs.


## Reproduced writer-upgrade defect — candidate, not deployed

A two-connection isolated WAL test reproduced database is locked at the
PipelineJobStore._savepoint read-then-write boundary: deferred BEGIN admits
a read snapshot while another writer commits; that snapshot cannot upgrade.
Existing busy_timeout does not repair stale snapshots. Candidate changes only
self-owned transaction entry to BEGIN IMMEDIATE before reads. Caller-owned
transactions are neither replaced nor committed. Tests additionally prove
outer rollback remains authoritative and nested failure preserves outer work.

Red reproduction failed before the change; local24 Pipeline tests and server261
related tests passed (Pipeline, ScanState, source analysis, Worker, model request
state/recovery). Server test used deployed image with readonly candidate/no
network/no Production media. Evidence:
/logs/m3-sqlite-diagnostic-20260908T1137/writer-reservation-regression.log and
.exit0. This is a proven isolated defect, not yet proven to explain the earlier
Production failures: bounded lock excerpts still contain only pre-deploy errors.
No runtime deployment/Gate change/output addition in this patch. Next safety
handoff must preserve existing holds/evidence, then validate real execution.


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
