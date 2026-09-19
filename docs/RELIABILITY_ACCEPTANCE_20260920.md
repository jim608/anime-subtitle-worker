# Full-flow reliability acceptance — 2026-09-20

Active Goal replaces the previous one-claim recovery closeout. Overall NOT COMPLETE.
No M3, full-library rescan, broad Queue retry, QC relaxation, source/valid-output
overwrite, backup deletion, or ASR-budget reset. Server must own the eventual
24-hour observation using existing observer/ledger; not a second framework.

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
remain --rm and now carry the same label vocabulary. Lifecycle regression is
still in progress; its first failure was a test-driver checkpoint inspection
through `docker exec` after fixture exit, fixed to read its persisted bind file.
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

## Persistent acceptance requirements

- [ ] Shared root repairs reproduced and tested on UNRAID isolation, tested SHA recorded.
- [ ] Relevant integration tests include success/review/fallback/SQLite contention,
      untrusted cache refusal, publication/replay, Gate settle/expiry/refresh/restart,
      real safety events still blocking.
- [ ] Safe deployment, attestation and new-incident controlled recovery; no blind rearm.
- [ ] At least24 hours of existing server observation on a fixed20 eligible cohort,
      all results retained and no backfill, adequate-resource admission bounded.
- [ ] Multiple real terminal->next-claim cycles and at least2 newly verified formal subtitles.
- [ ] Download/extract and AI publication evidence individually attributable,
      unaffected old evidence reused but not recounted as new.
- [ ] No false completion, source damage, duplicate publish or unbounded retry.
- [ ] All isolation/UNPROVEN/backups preserved; no unsupported continuity claim.
- [x] Confirmed obsolete temporary containers actually removed only after archival.
- [x] Success/failure/timeout/cancel/restart/orphan-next-entry cleanup verified;
      services, live tests, volumes, artifacts and backups unaffected.

Observation not started on a repaired baseline yet. Status INVESTIGATING, not
WAITING_FOR_EVIDENCE until safe repaired runtime and autonomous collection exist.
