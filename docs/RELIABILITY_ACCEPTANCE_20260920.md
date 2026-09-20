# Full-flow reliability acceptance — 2026-09-20

Active Goal replaces the previous one-claim recovery closeout. Overall NOT COMPLETE.
No M3, full-library rescan, broad Queue retry, QC relaxation, source/valid-output
overwrite, backup deletion, or ASR-budget reset. Server must own the eventual
24-hour observation using existing observer/ledger; not a second framework.

## New recurrence supersedes the previous WAITING closeout

At2026-09-19T23:45:34.896185Z, task `m2ai_99a4c7f0c7371ecb7248` again reached
incorrect_completion/hallucination_validation_pass failure on deployed2a44df9.
The prior ARMED snapshot and one continuation remain historically true, but do
not prove sustained recovery. Breaker remains TRIPPED during repair; no repeated
rearm or reuse of the preceding receipt. Current phase: REPAIR_VALIDATED,
deployment/controlled recovery pending.

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

## Current incident

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
| Watch/stabilize/deduplicate/claim | Existing durable pipeline and event watcher | Reuse unaffected tests; check claim fairness and stale leases |
| Search/match/download/extract/import | Prior real1 formal TC delivery351 cues, replay queued0 | Reuse only after dependency/diff check; do not count as new |
| TC/CN/JA subtitle and trusted-JA ASR | c204 parser154 and live fallback verified | Preserve QC and unproven-cache rejection; regress common boundaries |
| Existing non-JA ASR translation | Real source-language publication followed by strict rejection | Reproduce actual transcript/diagnostic/final-state bridge; no assumed PASS |
| Transient failures vs review | Recent transient_timeout on existing ASR review | Inspect retry budget and retry/review classification, no reset |
| Publish/mux/terminal/next claim | Prior next-claim proven, later recurrence disproves sustained reliability | Correct shared cause; full terminal/settlement/next-claim cycle |
| Provider/Gate/observation/restart | Fresh provider verified; current Gate ACTIVE0/20 | Fake-clock expiry/refresh and Gate settlement regression; preserve old FAIL |
| Historical recovery/fair admission | Existing ledger/dispatcher | Verify bounded retries/checkpoint reuse and no starvation |
| WebUI waiting reason | Shows generic admission guard in scheduler | Expose authoritative underlying reason/time/recovery condition only |
| Temporary Docker lifecycle | Discovery requires labels/mounts/run evidence, not names alone | Export evidence, exact non-force removal, repair existing launch/teardown paths |

## Deployed repair and controlled recovery

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

Minimum observation end: **2026-09-20T23:33:51.350781Z** (Sep21 07:33:51 Taipei).
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
- [ ] Multiple real terminal->next-claim cycles and at least2 newly verified formal subtitles.
- [ ] Download/extract and AI publication evidence individually attributable,
      unaffected old evidence reused but not recounted as new.
- [ ] No false completion, source damage, duplicate publish or unbounded retry.
- [x] All isolation/UNPROVEN/backups preserved; no unsupported continuity claim.
- [x] Confirmed obsolete temporary containers actually removed only after archival.
- [x] Success/failure/timeout/cancel/restart/orphan-next-entry cleanup verified;
      services, live tests, volumes, artifacts and backups unaffected.

Status **WAITING_FOR_EVIDENCE** on the repaired baseline. Full Goal and M2
Production acceptance remain incomplete. No M3. Recovery/ARMED and one real
continuation are not sustained acceptance; do not declare the24-hour or output
requirements satisfied before their durable evidence exists.
