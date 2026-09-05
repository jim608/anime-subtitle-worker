# M2 Production Observation

## Verified recovery deployment and bounded cases (2026-09-05 09:07 UTC)

The second safe deployment, `20260905T083811Z-380719`, actually runs Worker
`60d6b2361a54a76730c5a943dfd3fac8b98cca19` and unchanged WebUI
`7bd36c30fb07e393eba71760a164246d267c5b16`. Worker image is
`sha256:a28118c6454d5bd8f59eb8a445aa35a35dea816385055f93660a7e566e50565e`.
Safe-update passed 1,821 Worker / 229 WebUI tests; the running image passed
73 focused tests and fresh 7/7 isolated breaker tests. The guarded recovery
completed at `2026-09-05T08:46:06Z`, then runtime explicitly reported
`ARMED/runtime_baseline_match` and resumed claims. Immutable receipts are in
`runtime-handoff-v2/` under the evidence root below.

The old `m2-gate-20260905T045640981079Z-08147de925` remains invalidated with
its members preserved. The replacement started at
`2026-09-05T08:46:08.410661Z`, ID
`m2-gate-20260905T084608410661Z-d618a17882`, baseline
`m2-guardrail-v1:d9b0ba4c8c41da78cf61f964`, initial **0/20**. Configuration
fingerprint remains `sha256:355300b197164801be4616a688d852c1b8b5274fe91e91e40a2a21f12a3c4dbc`;
Decision Schema 1 / `m2-source-decision-v1` is unchanged. Historical recovery
counts do not backfill this frozen cohort; this is not a Gate-pass claim.

Bounded production evidence (no manual claim, priority change or marker reset):

- Recovery `m2rec_1629a2548a08033c20c32206c572302ea0db81c6dafaacbc9f5a4e50100dea34`
  claimed at `08:46:35.962861Z`, performed SUBTITLE_DETECTION, persisted decision
  and checkpoint, then safely settled NEEDS_REVIEW / QUALITY_BLOCKED at
  `08:46:46.072946Z`. It did not falsely complete or permanently pause the lane.
- The next recovery `m2rec_1686d70effd6e814eb735661f119d621c970f8d0b3d5636830ea4714df7f69b3`
  was automatically dispatched at `08:51:19.752685Z` and actually claimed at
  `09:00:41.743083Z`. SUBTITLE_DETECTION finished NEEDS_REVIEW at
  `09:00:54.722207Z` with heartbeat and checkpoint
  `2d2257be5dfa140016de1c1cdebc37a139095b538995ad57aa3df0d78429ad1e`;
  recovery settled safely at `09:01:01.370461Z`. This proves automatic next
  claim, not AI fallback completion. See `actual-next-claim-0908.json`.
- The existing Amaburi download reached completion by normal qB continuation,
  retaining partial data. Its existing extract job started at
  `08:45:57.550386Z` and finished at `08:46:00.051702Z`, attempts=1. Both
  traditional/simplified E13 sidecars passed parse/identity but failed unchanged
  hard QC (empty/short cues, overlaps and hallucination-text findings). The job
  recorded `hard_qc_failed` and entered the existing bounded replacement path;
  it did not block unrelated work. E12 likewise failed existing hard QC.
  Obligation `m2dl_4cd4c0387bcc606d1e7b` has **0 new formal subtitles**;
  source video and all pre-existing target sidecar checksums are unchanged.
  `amaburi-final-evidence-v3.json` preserves the evidence, including a separate
  pre-existing malformed SRT revalidation exception. An extraction attempt or
  completed torrent is not a usable subtitle or successful publication.

Current source recovery accounting: 83 deterministic local target bindings
(59 formerly missing mappings + 24 formerly ambiguous targets), not 83 sources
or deliveries. Of the original 1,227, 356 remain source-backoff cases and 788
still need trustworthy evidence: mapping unavailable 328, target ambiguous 301,
release identity 148, match review 9, target not indexed 2. Exact trusted
metadata/source mapping or existing review resolution can trigger matching;
terminal cached misses do not have a generic timed metadata-refresh guarantee.
Source-backoff jobs retain existing deadlines, source budgets and alternatives.

All four No-Rin diagnostics selected trusted JA-audio fallback, but the fixed
E2 normal scan entry remains queued with attempts=0: actual fallback starts=0,
fallback completions=0 in this bounded validation. Format-compatible candidates
were 2/1/1/1; all five retain historical failed/seen URL/hash evidence with
`did not start`, not a proven corrupt-content finding. No blanket failed/seen
reset, source redownload, manual priority override or mass Whisper routing.

A narrowly reproduced candidate-parser exception fix passed 341 server-isolated
tests (`server-parser-boundary-tests.log`) but is not yet in this runtime SHA.
It classifies malformed SRT per candidate without masking valid siblings and
refuses invalid staged publication without touching prior outputs. Unsupported
raw SSA/VTT validation remains explicit, never a fabricated QC PASS. A further
runtime deployment requires a safely attested planned Gate handoff first;
queries and documentation alone do not retire or recreate this Gate.

The same candidate also revalidated the exact real E13 final files in a
network-isolated read-only mount: `amaburi-candidate-file-validation.json`.
Revalidation completed without exception, verified languages remained empty,
new sidecars=0, formal publications=0, and source/all prior sidecar checksums
matched. This is real-file candidate safety evidence, not deployed attestation
or a successful subtitle delivery. The earlier RO database probe failure is
retained separately; no database access mode was weakened to bypass it.

## Recovery unblock validation in progress (2026-09-05)

The first safe deployment of `ea0baafac3baa703e3f4186632051765bc1af6bb`
completed (deployment `20260905T081354Z-47629`): 1,813 Worker and 229 WebUI
tests, 65 actual-image regression tests and fresh 7/7 fault injection passed.
Its expected container-identity drift invalidated the old Gate at
`2026-09-05T08:17:54.551592Z` and appended a runtime-change trip. The initial
recovery correctly refused because its exact old-trip timestamp contract did
not yet cover that legitimate deployment transition; claims remained paused.
The follow-up recovery contract binds the first immutable attestation receipt,
exact invalidation and runtime-change evidence, original incident/counters,
unchanged configuration/Decision Schema and absence of all newer claims/events.
It does not replace the original incident time or hide other trips. Server RO
validation with the actual effective Worker environment passed in
`runtime-handoff-v2-parity-validation.json`; the first failed attempt and receipts
are retained. No replacement Gate was armed during this first deployment.

Predeployment incident at `2026-09-05T07:15:03.553Z`: the unchanged b911
runtime tripped after two materialized-subtitle language refusals and one
`database is locked` failure were collapsed into `worker:worker_unknown`.
The initial deployment attempt stopped before claim-control or runtime changes.
Immutable-event and source-checksum verification in
`collision-readonly-validation.json` confirmed three distinct obligations and
two cause clusters (2 + 1), not three database errors. Candidate fixes preserve
typed source review, normalize generic failure causes, retain each new trip
occurrence, and move scanner completion hashing/probing outside its write
transaction with exact dependency revisions checked before commit. No QC or
retry deadline is weakened. Controlled recovery must bind this exact incident
to fresh deployed-image evidence; the full historical reconciliation is skipped.
The final frozen server-isolated candidate suite passed **744 tests** in
`server-final-freeze-tests.log`; this is not deployed-image attestation.

The precise paused recovery canary is separate from the three-member breaker:
its `translation_unknown` detail reports the unchanged hard display limit,
with a durable `review_required` attempt and preserved checkpoint. It was
incorrectly classified as a permanent system error. Exact dispatch/settlement
hashes and the current checkpoint passed `quality-pause-readonly-validation.json`.
Controlled lane recovery must leave that canary failed/review-only, release only
its proven lane-level pause, and preserve every retry budget/deadline. Dispatch
must respect both recovery `not_before` and Queue `next_retry_at`; the first
READY item remains in normal backoff and cannot be forced ahead of its deadline.

Evidence root: `/logs/m2-recovery-unblock-20260905T064508843990Z/`.
The one entry attestation confirmed Worker `b911794ed0ec872cb475f714e1385e20e8ac4388`,
WebUI `7bd36c30fb07e393eba71760a164246d267c5b16`, ARMED and the existing ACTIVE
`m2-gate-20260905T045640981079Z-08147de925`. Candidate edits are not a deployed
baseline. A real code deployment requires the existing guarded handoff; these
queries, isolated tests and documentation do not themselves recreate a Gate.

Reproduced defects and bounded candidate fixes:

- Due replacement work was delayed by unbounded normal source discovery,
  including cold online matching before the old slice. Discovery now uses a
  durable cursor, shared deadlines across HTTP retries and bounded recovery
  batches. Cold lookup uses the existing metadata index, checkpoints incomplete
  evidence in the existing cache, and reserves time for already-matched series.
  Local scheduling preemption is not a failed source or a consumed retry.
- A No-Rin E1 half-episode official sidecar suppressed AI admission. The candidate
  reuses existing full parse/coverage verification and changes only the scanner
  cache signature so exact normal visits revalidate old completion entries.
  All four diagnostic inventories selected trusted Japanese-audio ASR, but a
  diagnostic decision is not a durable Worker claim or an AI success.
- Persisted source IDs with explicit Chinese-numeral seasons were not properly
  scoped to independently verified local season NFOs. The frozen-key-only server
  probe resolved **83 unique local target bindings**: 59/387 missing mappings and
  24/325 ambiguous targets. Twelve verified scopes were found. This is not 83
  downloaded or publishable sources; unseasoned old batches remain rejected.
  The other 629 of these 712 keys, and 159 other identity/index/review keys,
  still require evidence (788 total). Terminal cached misses outside the verified
  season rule are not promised automatic refresh from arbitrary metadata edits.
- A real completed Amaburi E12 ASS passed full parse/coverage but failed existing
  hard QC (empty/short/overlapping cues). The old runtime copied it verbatim over
  a valid **isolated** prior subtitle. Candidate import and staged publication
  now both apply the existing hard QC without changing rules or thresholds.

The four No-Rin cases have 2/1/1/1 format-compatible candidates; all five are
excluded by preserved failed URL/hash and seen URL/hash records. The persisted
reason is `did not start`, not verified bad content. Missing historic qB-state
evidence does not justify resetting these entries. The older shared unsafe
mapping remains review-only. All six fallback providers responded in the bounded
lookup; this was not evidence of a source-wide outage or permanent no-subtitle.

Amaburi's existing torrent is being resumed without deletion or re-addition.
Completed individual sidecars do not make the batch complete or allow the normal
whole-torrent extraction dispatcher to claim it. The representative obligation
`m2dl_6cdbf3974b0bb9979040` has no verified official output and currently fails
hard QC; no Production publication is credited. New download, extraction,
official-publication, AI routing and AI completion counts must remain separate.
Full source-video checksums and existing subtitle hashes were unchanged in the
real-artifact probes. No M3 or Production-accepted/SLO claim is made.

A second exact missing-output target, Amaburi E13
`m2dl_4cd4c0387bcc606d1e7b`, has complete downloaded TC/SC sidecars and a verified
episode match, but both fail unchanged hard QC (348 cues, 2 empty/too-short
cues, 27 overlaps and 2 hallucination-text detections). Parse/coverage eligibility
alone is not publication approval. At `2026-09-05T08:04:25.947483Z` the existing
torrent was 75.7% complete and still downloading its original partials; normal
whole-torrent extraction was not yet eligible. This probe wrote no subtitles,
and the original E13 source and existing subtitle hashes remained unchanged.

## Bounded acceptance follow-up (2026-09-05, no runtime change)

This follow-up accepts **partial repair and recovery started**, not a completely
repaired download chain or Production acceptance. Evidence is retained under
`/logs/m2-recovery-acceptance-20260905T051642581908Z/`. Only the preceding audit's
fixed four unsuccessful obligations and five old completions were rechecked
(11 exact indexed video paths, including two ambiguous pairs). No library or
Queue re-audit was run. No Worker/WebUI code, QC, models, configuration, image,
container or Gate was changed. The runtime remains Worker `b911794...`, WebUI
`7bd36c3...`, `ARMED / runtime_baseline_match`, with the same ACTIVE Gate and
baseline recorded below. Documentation synchronization does not redeploy it.

### Real missing-output acceptance: BLOCKED, new Production subtitles = 0

Four real targets, anonymous obligations `m2dl_9e2ac34428c091432381`,
`m2dl_0e107117422ec1a044b0`, `m2dl_62b18022da8c630ffb2d` and
`m2dl_606f48fc7c3811cb0e8e`, lack subtitles passing the existing full Traditional
Chinese validation. Their existing subtitles were preserved. The first three
parse but fail `insufficient_coverage`; the fourth also fails complete timestamp
parsing. No threshold was relaxed and no invalid subtitle was replaced for a test.

The one-series bounded lookup succeeded against Mikan (206 release records)
and all six configured alternative providers (35 matched fallback records).
Only 2/1/1/1 candidates respectively survived existing format/episode policy;
the current selection returned no permitted source for any of the four after
the existing failed/seen-source exclusions. This is **not a network outage**,
nor proof that no usable subtitles can ever exist. Provider circuits were
closed. Existing per-obligation backoff, failed hashes and source budgets stay
intact; retry requires the next normal cycle and an eligible source, without
re-adding the same failed resource. No bulk AI fallback was authorized.

The only completed project torrent in the bounded qB snapshot was the earlier
Dark Gathering sample, whose formal target already had valid subtitles. It
was not reused to fabricate a new-publication pass. Consequently extraction
through new formal publication, final-location re-read and duplicate-publication
replay for a genuinely missing target remain **unverified**, not PASS. The
current download set has not supplied an eligible new-publication case.

### Exact four failures and five old completions

| Frozen group | Reverification/disposition | New imports |
| --- | --- | --- |
| Four No-Rin obligations (episodes 1, 2, 3, 8) | Still `failed_candidate / find_replacement`; full validation rejects current subtitles as above; retain failed-source exclusions and bounded future discovery | 0 |
| `m2dl_d33bd08cfffb12d1fce9` | BOFURI completed release explicitly identifies season 2; exact S02E10 formal subtitles pass parse and existing source QC (S01E10 was checked separately, not substituted) | 0 |
| `m2dl_09eb5d97eb7c68f441c3` | Exact Selection Project S01E12 formal Traditional Chinese output reverified | 0 |
| `m2dl_74a5c3e62a7de64c7ac9` | Exact Undead Murder Farce S01E13 formal Traditional Chinese output reverified | 0 |
| `m2dl_0ad1e9abcdb7f6be54f4` | Exact I Want to End This Love Game S01E03 formal Traditional Chinese output reverified | 0 |
| `m2dl_5c44d82d26a12657804a` | Still identity review: ledger episode 6 versus completed release 6.5. Regular E06 has valid output; special 6.5 does not. E06 cannot prove completion of 6.5 | 0 |

Thus **4/5 old completions have current final-file availability/QC evidence;
1/5 remains unsafe to reconcile**. This is not verification of the historical
download's provenance or a new import. `target-reverification.json` retains
every classifier, full parse/source-QC result, subtitle SHA-256 and video file
identity before/after. All 11 target file identities and existing subtitle
hash sets were unchanged. Full source-video hashing was not repeated in this
read-only acceptance; no source or formal output was written.

### Automatic claim evidence, split by lane

The previous unclaimed Full Metal Panic canary was autonomously recorded as
`RECOVERY_PRECLAIM_EXCLUDED` at `2026-09-05T05:17:06.280160Z`, with unchanged
media identity, zero claims and no false completion. There were no remaining
running deliveries for that exact path in the later bounded check.

AI recovery now has **two real subsequent claims**, not merely dispatch:

| Anonymous recovery | Dispatch / claim (UTC) | Actual stage, heartbeat and disposition |
| --- | --- | --- |
| `m2rec_0104be54ca1f75f715c7d8e622c2b49bb81add01a2a34311901b6499a1b5bf35` | 05:18:08.202955 / 05:23:42.550219 | SUBTITLE_DETECTION started 05:23:42.564939; heartbeat 05:23:56.672366; transient `database is locked`, existing bounded retry remains; no successful checkpoint or completion credited |
| `m2rec_04ea5b7eba0f570717fc61b0031bb9ad1674df353ffdefa0560f856c0fde8f24` | 05:24:28.395641 / 05:24:28.959080 | SUBTITLE_DETECTION started 05:24:28.972935; heartbeat 05:24:31.803102; complete decision/checkpoint persisted; NEEDS_REVIEW / quality isolation, no false COMPLETED |

The second automatic claim after the first disposition proves the recovery
lane continues. `stage-evidence.json` preserves immutable dispatch/claim/settle
events, attempt IDs, actual stage attempts, heartbeats, transitions and the
checkpoint digest. Both used the unchanged b911 runtime. A single transient
SQLite contention event is recorded, not treated as a reproduced scheduler
defect and not bypassed by clearing a lock or rewriting state.

Download continuation is **not equated with those AI claims**. The normal
server Mikan source-search cycle remained actively processing indexed series
through 05:33 UTC (timestamps in the single saved 2 MiB `app-window.log`);
the historical replacement request still awaits that earlier enqueue cycle.
Its retry time had elapsed, but the single enqueue worker had not returned to
the request consumer. The explicit waiting reason is **previous source-enqueue
cycle still executing**, with the existing six-lookup-per-cycle budget, not
operator pause, deployment hold or a tripped breaker. No lock was cleared and
no normal wait was skipped. The latest extraction lease/start/finish remains
the earlier Dark Gathering job (04:00:05.945955–04:00:09.028567); a different
subsequent download/extraction claim has **not** been demonstrated. Source
search progress alone does not close that acceptance item.

### Frozen 1,227 blocked obligations: disjoint routing groups

These are the preceding audit's exact deduplicated keys, not a new scan.
`frozen-keysets.json` retains all IDs and reasons; multiple historical stage
reasons for one obligation must not be summed as additional jobs.

| Route | Count | Trigger or explicit blocker |
| --- | ---: | --- |
| Existing source-backoff, potentially retryable | 356 | Eligible after persisted `no_candidate_until`, when normal enqueue cycle reaches the series; existing candidate and lookup budgets still apply. This historical classification is not proof all 356 sources are currently offline |
| Proven unsuitable source, immediately eligible for a safe alternative | 0 | None of this frozen blocked subset has enough additional current evidence to promote it safely; the separate original 887 replacement targets must not be counted again |
| Proven no usable ready-made subtitle, eligible for AI fallback | 0 | No conclusive source decision establishing this was obtained; missing historical download paths do not qualify |
| Insufficient matching/identity evidence | 871 | 148 release-identity reviews + 325 ambiguous targets + 9 match reviews + 2 unindexed targets + 387 missing mappings; preserve review until trusted identity/mapping/index evidence is resolved, then existing policy may reassess |
| **Total** | **1,227** | **No blanket Whisper, permanent media exclusion, or false completion** |

The 871 review records have no claimed unconditional automatic-resume date:
their blocker is missing trustworthy matching evidence. A suitable alternate
source may help after identity is established; unavailable paths alone are
not a permanent no-subtitle decision. No historical recovery total replaces a
frozen cohort member. No Gate completion, M3, acceptance rate or SLO is claimed.

## Current M2 download/recovery closeout (2026-09-05)

The deployed Worker is `b911794ed0ec872cb475f714e1385e20e8ac4388`; WebUI
remains `7bd36c30fb07e393eba71760a164246d267c5b16`. Safe deployment
`20260905T045130Z-2559308` completed with 1,737 Worker and 229 WebUI tests
passing. Controlled recovery explicitly returned `ARMED`,
`runtime_baseline_match`, and `claims_resumed=true`.

| Frozen runtime field | Value |
| --- | --- |
| Worker image | `sha256:300a394ebd181f57fa7f7d6e017957cbef86d3ce44eae747b37a092503361afa` |
| WebUI image | `sha256:4b0e2458b770de20c9fda8af01926a97e8cc5104aff1458c18f79c403abc26a2` |
| Configuration fingerprint | `sha256:355300b197164801be4616a688d852c1b8b5274fe91e91e40a2a21f12a3c4dbc` |
| Decision schema / version | `1` / `m2-source-decision-v1` |
| Gate ID | `m2-gate-20260905T045640981079Z-08147de925` |
| Baseline | `m2-guardrail-v1:5b4d2a88f2d5c0c5749f6747` |
| Gate start | `2026-09-05T04:56:40.981079Z` |
| Initial progress | `0/20`; not a completion or acceptance claim |
| Selection | `m2-frozen-first-20-v1`; first 20 eligible post-start claims, no backfill |

All superseded runtime Gates, including the interrupted `288de2c08d` handoff
and the subsequent `9f7201d81d` Gate, retain their invalidation records.
No query, inventory or report created a Gate. Each replacement followed an
actual deployed runtime change. The original eight pre-gate attempts and all
later pre-gate work remain historical evidence, not replacement cohort slots.

### Download history and actual evidence boundary

The audit deduplicated 2,120 `(bangumi_id, episode)` obligations. Overlapping
stage counts are download 1,670, extraction 635 and matching/import 71; they
must not be added. The 417 non-success extraction hashes and nine waiting
hashes are another view, not additional obligations. All 426 inspected
historical non-success/waiting source paths were missing; no corrupt-media
claim was inferred from that fact.

The server persisted all 2,120 decisions and initially submitted 887 safe
replacement targets to the existing durable mechanism, alongside one existing
download. These 888 are automatic-reassessment candidates, not guaranteed
successful downloads. The other 1,232 comprise 356 source-backoff records,
148 release-identity reviews, 325 ambiguous targets, two unindexed targets,
nine match reviews, 387 unavailable mappings and five old completions not
reverified. At the 04:40 UTC bounded snapshot, ten replacement targets had
been consumed: eight verified existing outputs and two no-candidate retries;
877 targets remained in the durable request. No new Production import was
credited by those eight idempotent reconciliations.

Representative historical failure `3078:1` (Dark Gathering S01E01) reached a
real 627,324,826-byte completed download, exact target matching, real zh-cn and
zh-tw extraction and validated atomic import in an isolated proof directory.
The real target already had both valid Chinese outputs. Its seven existing
subtitle files and source checksum were unchanged; Production publication was
a safe no-op and **new Production imports = 0**. This is stronger than a mock
but does not prove a new Production publication. The truly missing-output
sample `304:10` had no eligible untried single-episode candidate; the old failed
hash was not re-added. External availability remains a bounded source-policy
blocker, not proof of bad media. A final provider snapshot had no open provider
circuit; per-job no-candidate/backoff records remain durable.

### Autonomous continuation and recovery closeout

Download request backoff no longer starves normal enqueue, and existing valid
outputs are verified before source lookup or another download. Partial qB
pieces, failed-hash exclusions and provider retry policy survive restart.
The server's own consumed-target records prove download continuation without
an online Codex session.

Final bounded snapshot at `2026-09-05T04:59:25.827875Z`
(`autonomous-status-1788584365.json`) retained all 2,120 decisions, with
**12 targets consumed, eight verified-existing completions, four not yet
successful, and 875 replacement targets remaining**. Provider circuits were
closed; the 356 historical source-backoff decisions are not new successes.

Two additional live closeout defects were repaired: a paused SQLite/manifest
Gate-publication race, and an unclaimed AI recovery canary whose Queue item
had been removed while its dispatch ledger stayed in flight. Controlled
handoff now refuses claims without invalidating a new Gate; genuine drift
still invalidates. The orphan is durably `EXCLUDED / KEEP_NEEDS_REVIEW`, with
zero claims and no false completion. The next item uses the same single-canary
lane and dispatch interval. Local quality/bad-input exclusions and bounded
retries no longer permanently pause unrelated canaries; confirmed permanent
system errors and real breaker trips still stop the lane.

The server independently dispatched the next AI recovery item at
`2026-09-05T04:57:37.050963Z`, after the orphan was excluded. Its exact indexed
Queue row was `queued / m2_recovery` in the final snapshot; 218 other items
were READY, `preclaim_excluded_count=1`, and claim control was unpaused.
This proves automatic next-item dispatch, **not a claim or terminal success**:
the new recovery canary still had zero claims, and no recovery success or
checkpoint-resume success was credited. No wait for that job was performed.

Evidence root: `/logs/m2-download-recovery-audit-20260905T030809930683Z/`.
It retains inventory, applied decisions, all five safe-deployment logs,
`real-source-proof.json`, exact canary/incident snapshots and
`controlled-recovery-closeout.log`. Fresh final-image fault evidence is
`/logs/m2-guardrail-fi-20260905T045613693910Z-334ff38d/result.json` and
`events.jsonl` (7/7 PASS). Recovery identity preservation is in
`/logs/m2-production-recovery-20260905T045637543053Z-edf65157.json`.
No M3, translation/QC relaxation, full media rescan, Queue polling or wait for
the historical backlog / 20-job Gate was performed. New Production import,
remaining source availability and eventual cohort outcomes remain unverified.

## Initial download recovery candidate evidence (superseded by closeout above)

This remains M2, not M3. Download recovery metrics are separate from the frozen
cohort and never backfill failed members. Read-only audit did not reset a Gate.
Deployment requires actual runtime attestation, old-Gate invalidation and an
empty replacement Gate before claim resume.

Evidence: `/logs/m2-download-recovery-audit-20260905T030809930683Z/` contains
the inventory, exact-path checks, provider lookup, reconciliation decisions and
full isolated test log. There are 2,120 distinct `(bangumi_id, episode)` historical
obligations. Overlapping stage counts: download 1,670, extraction 635, match/import
71 (do not add them). There are 417 historical non-success extraction hashes
and nine waiting-download hashes. The older 1,169 AI-recovery rows are separate.

qB login and v5.2.3 were verified. qB `/anime` and Worker
`/qbit_subtitle_extractor` mount the same `/mnt/user/qbit_subtitle_extractor`.
All 426 inspected historical non-success/waiting content paths were absent;
this is not proof of corrupt media. Two current managed stalled torrents lack
trusted mapping and remain untouched, including existing partial pieces.

Verified defects: explicit four-digit episodes were truncated; partially
downloaded `queuedDL` could expire; timeout replacement could delete pieces;
external subtitle publication lacked full parse/completeness validation;
deferred failures could retry every second. Fixes retain existing queues,
leases, deduplication, provider policy and analyzer thresholds. Archives are not
unpacked and cannot be mistaken for usable subtitle files.

The server candidate passed 283 focused tests, including real ffmpeg extraction
and safe import on generated isolated media. This is not a Production download
E2E result. Actual deployment and the representative historical-case disposition
remain required at closeout.

## Historical pre-download recovery status (not the current runtime)

- Milestone state: `M2_GUARDRAILS_ARMED`
- Circuit-breaker runtime status after controlled recovery: `ARMED` (`runtime_baseline_match`)
- Incident breaker transition: `TRIPPED` -> `ARMED`
- Historical automation-repair Gate final status: `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`
- Pre-recovery Gate final status: `INVALIDATED_BY_RUNTIME_CHANGE`
- Frozen-cohort/recovery runtime: **deployed and bounded validation complete**
- Active replacement Gate: `m2-gate-20260905T020845085531Z-7d0c5c7333`, started `2026-09-05T02:08:45.085531Z`
- Active replacement Gate counter: **`0 / 20`; this closeout did not wait for any member**
- Former Gate counter: **`0 / 20` at invalidation; it is not a valid cohort and cannot be resumed or backfilled**
- Production acceptance: **not accepted**
- Scope: M2 observation-automation repair and deployment closeout only. This work does not start M3.
- Evidence boundary: fixed values below preserve both invalidated historical Gates and the bounded live recovery snapshot. The queue was not continuously polled or observed item by item.

This status must not be represented as `M2_PRODUCTION_ACCEPTED`, and it is not evidence that the platform has reached 99% or 99.9% autonomy.

The normative cohort contract is in
[`OBSERVATION_GATE_POLICY.md`](OBSERVATION_GATE_POLICY.md), focused local
evidence is in
[`OBSERVATION_GATE_TEST_RESULTS.md`](OBSERVATION_GATE_TEST_RESULTS.md), and the
additive database rollout is in
[`M2_OBSERVATION_SCHEMA_MIGRATION.md`](M2_OBSERVATION_SCHEMA_MIGRATION.md).

## Invalidated historical Gate baseline

| Historical field | Preserved value |
| --- | --- |
| Final status | `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY` |
| Worker commit SHA | `99d383f7ee72628875a223b3ff2c4f4f05845ec3` |
| WebUI commit SHA | `7bd36c30fb07e393eba71760a164246d267c5b16` |
| Worker container image ID | `sha256:995b6586dd5bd064a7eeba9d3287d2671cd26b042d1823380fb527acf00c99b7` |
| WebUI container image ID | `sha256:4b0e2458b770de20c9fda8af01926a97e8cc5104aff1458c18f79c403abc26a2` |
| Worker source revision | `b8986e794d3cb84bdcc831fbb53d19dfe8275358c37529fe1d9375ccd6e1fd3d` |
| WebUI source revision | `ea2f9e9f4341d5e76d1067fbc113cdcec4d06bb6a07228258b18ad2db300403c` |
| Configuration fingerprint | `sha256:ca8be249cc69fe265e7bc9668959f2bce916f20f6113b6f63b79b9c7dd99163f` |
| Decision schema/version | schema `1`; `m2-source-decision-v1`; `subtitle-source-priority-v1` |
| Gate baseline version | `m2-guardrail-v1:276fdef781528ba2059c114e` |
| Gate start | `2026-09-04T11:00:12.736033Z` |
| Pre-gate attempts | `8`; supplemental history only |

The historical arm operation verified both clean Git identities, running image
IDs and source revisions, the read-only runtime configuration mount, effective
configuration fingerprint, and loaded decision contract. The observation
implementation attached to that baseline did not satisfy the frozen-cohort
contract, so the complete identity and observation record are preserved under
the exact invalidation status above. None of its attempts may be selected into
the replacement Gate.

The pre-closeout Worker SHA `0d989e6fab861d6beef6a06afe0de3657dc8aaaa`
did not contain the runtime guardrail modules and could not report `ARMED`.
It was therefore superseded by the tested code-only runtime baseline above;
it must not be represented as the armed Worker revision.

## Pre-gate server snapshot

Historical bounded snapshot time: 2026-09-04 06:13 (Asia/Taipei). The recorded
queue count remains the supplied pre-gate snapshot; it was not refreshed while
arming the guardrails.

| Observation | Last confirmed status | Evidence | Confidence |
| --- | --- | --- | --- |
| Processing | `1` job | Anonymous job `checkpoint-resume-001` was processing. | High |
| Queued | `6346` jobs | Queue counter snapshot. | High |
| Failed, awaiting retry | `479` jobs | Retry counter snapshot; this is not counted as completed. | High |
| Completed canary | `canary-latest-added-001` completed | Canonical Traditional Chinese ASS was produced and the observed output passed the available parse and final QC checks. This is one canary, not production acceptance. | High |
| Resumed checkpoint | `checkpoint-resume-001` active | The persisted source decision was reused and the selected Japanese audio source was restored without repeating the completed decision stage. | High |
| Hallucination interception | `hallucination-intercept-001` intercepted | A suspected tail hallucination with unresolved prompt-free repair was held in `transcription_review` and was not published as completed. | High |

Media names, source paths, host addresses, ports, and full logs are intentionally excluded from this document.

At historical arm time the read-only exclusion snapshot found `8` running
delivery attempts and `0` running queue rows. Their identities are persisted
only as hashes. Their eventual results may be recorded in supplemental history,
but none can count toward either the invalidated Gate or its replacement.

## Safe concurrency

Worker concurrency remains at the current safe minimum:

```text
max_concurrent_videos = 1
```

The M2 observer must fail validation if canary mode is enabled with a higher
Worker concurrency. No closeout task may increase concurrency automatically.

## Required frozen twenty-job observation gate

The replacement formal Gate cohort must be the first 20 distinct, eligible jobs
claimed by the Worker after the new `gate_start_at`, in durable claim order.
The cohort closes as soon as its twentieth slot is assigned. A failed, review,
quarantined, hallucination-blocked, or otherwise non-strict member keeps its
slot; later successful jobs must never replace it.

- Gate size: `20`
- Gate ID: deployment-time evidence
- Gate start: deployment-time evidence
- Gate baseline: deployment-time evidence
- Worker SHA, image ID, full container ID, in-container identity and runtime-instance fingerprint: deployment-time evidence
- Initial persisted progress: must be verified as `0 / 20` after re-arm and before claim resume
- Claimed after gate start at initialization: must be `0`
- Completed strict-verified at initialization: must be `0`
- Required selection: first 20 eligible post-start claims, with no backfill
- Required completion trigger: all 20 frozen members have reached a terminal state
- Required output: exactly one atomic, machine-readable, sanitized summary for the frozen cohort

Enrollment is part of the Worker claim transaction and executes under SQLite
WAL with `BEGIN IMMEDIATE`. The stable delivery-obligation identity, not an
individual retry attempt, occupies the Gate slot. Database constraints enforce
one ordinal and one stable job per Gate, and the next ordinal can never exceed
20. Duplicate claims reuse the original slot. Jobs started before the new Gate,
claims after slot 20, and claims after settlement are supplemental only.

Observation schema version 4 is installed or migrated atomically. It binds a
new Gate to the host-attested full Worker container ID plus an in-container
identity/runtime-instance fingerprint. A restart of the same container keeps
the binding; recreation is runtime drift and requires revalidation. Historical
version-1/2 Gate rows are preserved without fabricating missing evidence. The
former Gate's canonical runtime manifest and digest preserve its exact hashed
pre-gate snapshot after the live manifest is replaced during re-arm.

Only a job newly claimed after the replacement `gate_start_at` and processed
completely with its immutable baseline may enter the formal cohort. Each member
retains claimed time, final state, strict-verification result, individual
parse/QC/hallucination/source-checksum results, duplicate job and publish
results, and checkpoint/retry/fallback result. A strict-verified output must
satisfy all of the following at the same terminal decision:

1. Final state is `COMPLETED`.
2. Output parse is `PASS`.
3. Hard QC is `PASS`.
4. Hallucination validation is `PASS`.
5. Source checksum is unchanged.
6. There is no duplicate job.
7. There is no duplicate publish.
8. The Decision Record is complete.
9. Stage and checkpoint history is complete.
10. Runtime commit SHA matches the gate baseline.
11. No unresolved retry, quarantine, or fallback state remains.

Each retry or terminal attempt also has an immutable result-event row keyed by
its claim identity. The row stores a canonical bounded payload and its SHA-256.
This preserves earlier OOM, parse-failure or breaker incidents when a later
attempt for the same frozen member succeeds. Before Gate settlement, every
included event payload/digest is revalidated; tampering blocks the twentieth
terminal transaction and summary creation. Only events joined to one of the 20
frozen stable-job identities are included. Pre-gate and slot-21-or-later
supplemental incidents remain recorded but cannot affect frozen counters. Once
`terminal_at` is set, the terminal trigger prevents later updates to every
terminal state, strict/projected result, breaker/reason/strategy/incident field
and evidence digest.

The required automatic summary contract contains `gate_progress`,
`claimed_after_gate_start`, `completed_strict_verified`, `needs_review`,
`failed`, `quarantined`, `hallucination_blocked`, `output_parse_failures`,
`source_mutation_incidents`, `duplicate_jobs`, `duplicate_publishes`,
`breaker_trips`, `checkpoint_resumes`, `oom_events`, and
`processing_strategy_counts`. Every counter and strategy count was initialized
to zero at Gate creation. Frozen-member outcome counters come from member rows;
retry/incident counters also include the immutable result-event journal.
Terminal evidence and the summary payload are written to the same durable
database before publication. The summary outbox publishes an atomic file and
records emission; concurrent or restarted publishers cannot logically emit a
second report. The frozen-cohort report may be emitted only after all 20 fixed
members are terminal. A breaker trip independently stops admission and persists
bounded reason/evidence.

### Repair status and fail-closed behavior

The historical observer failed the no-backfill and complete per-member record
requirements. That Gate therefore has the final status
`INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`; its preserved `0/20` is not a
candidate for repair, continuation, or retroactive enrollment.

The repository candidate implements the replacement frozen cohort in the
existing WAL scanner-state database. Focused local coverage contains 24 core
frozen-cohort/schema/event-journal tests and 11 recovery/replay tests. The seven
isolated breaker classes also pass locally. The repaired Worker image still requires bounded
server deployment, fresh 7/7 breaker execution, runtime identity validation,
new Gate creation at `0/20`, and claim resume. Until those deployment-time
steps pass, this document does not claim that Production automation is ready.

If Worker/WebUI/runtime image or instance identity, configuration fingerprint,
Decision schema, eligibility policy, Gate identity, or Gate start identity
changes during the replacement Gate, claim admission atomically records
expected and actual evidence and marks the Gate
`INVALIDATED_BY_RUNTIME_CHANGE`. New claims then stop; existing queue rows,
running work, checkpoints, and recorded members remain intact. A restart with
the same exact baseline reopens the same Gate and next ordinal without
invalidating it.

The summary may contain counters, stage/error/reason codes, breaker state, and the observation window. It must not contain media titles, source paths, server addresses, ports, credentials, prompts, transcripts, subtitles, or raw exception/log text.

Full operational logs remain on the server under the configured log retention policy. Successful work must emit only the bounded gate summary to the observation channel; full logs must not be copied into a conversation.

## 2026-09-05 production breaker incident and recovery contract

The active pre-recovery runtime reported Worker commit
`45b61eb7da3c7e88fce70e1bea9573ad930e2a21`, Gate
`m2-gate-20260904T163053158998Z-f3076238c7`, baseline
`m2-guardrail-v1:82e3cddd091fdb8bdd6a1714`, and a durably latched
`repeated_identical_stage_failure`. The three observations ran from
`2026-09-04T16:34:31.252117Z` through `2026-09-04T16:37:15.476726Z`. They had
three distinct delivery obligations, the same `source_selection_review` stage,
the same `source_selection_needs_review` error code, and attempt number 2.

The durable pipeline records had already classified all three as
`NEEDS_REVIEW`; the compatibility queue/result adapter instead wrote
`failed_retry` / `retryable_failure`. The observation layer then treated those
expected review outcomes as system failures. This was not a single-job replay,
a permanently corrupt input, or evidence that the source media changed. It was
a state-classification defect at the legacy queue boundary, combined with a
breaker streak that did not retain distinct stable-job identities.

The recovery implementation makes `manual_review` a terminal
`NEEDS_REVIEW`/`review_required` result and excludes `QUALITY_BLOCKED` and
`BAD_INPUT` outcomes from the repeated-system-failure streak. Repeated OOM and
identical-stage counters now retain a bounded set of stable delivery-obligation
identities. Replays and later attempts for the same obligation cannot increase
the global streak; three distinct jobs with the same eligible runtime failure
still trip the breaker.

Historical reconciliation is stored in the scanner WAL database. It reads only
indexed queue, delivery, pipeline, attempt, and checkpoint records and never
walks the media library. Each candidate retains its prior state, normalized
failure category/signature, original/current versions, checkpoint evidence and
compatibility, recovery decision/reason, minimum resume stage, budget, attempt
count, no-progress history, and lane status. The first recoverable item is a
single canary; later dispatch remains one-at-a-time and uses the existing
resource-admission and single-Worker boundary.

The controlled `m2_guardrail_runtime.py recover` path is fail-closed. It
requires a new attested runtime and fresh 7/7 isolated breaker result, validates
the existing trip and distinct-job incident evidence, rejects fresh running
work, preserves stale-work checkpoints, compares queue identity/checkpoint and
formal-output ledger digests before and after reconciliation, verifies the
exact affected source identities, invalidates the old Gate as
`INVALIDATED_BY_RUNTIME_CHANGE`, journals an immutable recovery record, and
only then clears the latch into `DISARMED` pending a new Gate. Re-arming creates
the new baseline at `0/20`; recovery never mixes versions into the old Gate.

## 2026-09-05 live recovery result

The first live recovery attempt correctly stopped after invalidating the old
Gate and clearing the breaker latch to `DISARMED`: the Gate initializer rejected
the operation with `ai_claims_not_paused`. The deployment hold had restored the
WebUI scheduler state, but it had not established the durable `ai_control.json`
operator pause required by Gate initialization. Commit
`d9dfcd01aa9ebeffe65c8367f4e1bbace56d5bcc` made recovery restart-safe: it writes
and validates the durable pause before reconciliation/Gate creation, resumes a
validated pending recovery after deployment, and clears the pause only after
the runtime is `ARMED` with the matching active Gate.

| Live recovery field | Verified value |
| --- | --- |
| Old Worker runtime SHA | `45b61eb7da3c7e88fce70e1bea9573ad930e2a21` |
| New Worker runtime SHA | `d9dfcd01aa9ebeffe65c8367f4e1bbace56d5bcc` |
| WebUI runtime SHA | `7bd36c30fb07e393eba71760a164246d267c5b16` |
| Worker image ID | `sha256:8448157ef1b8cd720c35451e0642e6472f2f19a4e758ecbba77b6f991ab4ccea` |
| WebUI image ID | `sha256:4b0e2458b770de20c9fda8af01926a97e8cc5104aff1458c18f79c403abc26a2` |
| Configuration fingerprint | `sha256:355300b197164801be4616a688d852c1b8b5274fe91e91e40a2a21f12a3c4dbc` |
| Decision schema version | `1` |
| Breaker before / after | `TRIPPED` / `ARMED` |
| Invalidated Gate | `m2-gate-20260904T163053158998Z-f3076238c7` (`INVALIDATED_BY_RUNTIME_CHANGE`) |
| New Gate ID | `m2-gate-20260905T020845085531Z-7d0c5c7333` |
| New Gate baseline | `m2-guardrail-v1:0180b8779ee97524bf0150d2` |
| New Gate start | `2026-09-05T02:08:45.085531Z` |
| Eligibility policy | `m2-frozen-first-20-v1` |
| Initial Gate progress | `0 / 20` |
| Normal claims | resumed after `ARMED` and matching Gate validation |
| Recovery lane | one mandatory canary dispatched; `CANARY_IN_FLIGHT` at the bounded final snapshot |
| Recovery log | `/logs/m2-production-recovery-resume-20260905T020843873483Z-b68b9cda.json` |
| Production source media affected | No |
| Formal outputs affected | No |

The durable recovery matrix contained 2 historical `FAILED`, 470 `RETRYING`,
4 stale `RUNNING`, 0 `QUARANTINED`, and 693 historical `NEEDS_REVIEW` rows.
There were 222 recoverable entries; one was dispatched as the mandatory
recovery canary, 221 remained ready, 947 were permanently excluded, and no
checkpoint resume had occurred at closeout. This is a bounded reconciliation
snapshot, not a claim that the recovery backlog or frozen 20-job Gate finished.

## Automatic circuit breaker

The repaired runtime is `ARMED` and its active Gate reports
`runtime_baseline_match`. After the final container start, the running Worker
image executed a fresh isolated fault suite and passed all seven required
breaker classes before replacement Gate creation.

- Current validation run: `m2-guardrail-fi-20260905T020807095602Z-7f551399`
- Current full event log: `/logs/m2-guardrail-fi-20260905T020807095602Z-7f551399/events.jsonl`
- Current result: `/logs/m2-guardrail-fi-20260905T020807095602Z-7f551399/result.json`
- Current breaker tests: `7 / 7 PASS`
- Current production source/output affected by fault injection: **No**

The following older run remains historical evidence for the pre-repair image:

- Validation run: `m2-guardrail-fi-20260904T105817974649Z-b7176039`
- Validation window: `2026-09-04T10:58:17.974636Z` to `2026-09-04T10:58:18.758425Z`
- Result digest: `sha256:0b5f747c66eb514a1927daf33cec985c8235f7f27f73572a838ad838b8bb4c90`
- Full event-log digest: `sha256:371c300b231fdc42efa0203533d7473d07344eebd236d973839770abe10e674a`
- Breaker tests: `7 / 7 PASS`
- Production source/output affected by fault injection: **No**

The complete `events.jsonl` and `result.json` remain in the timestamped server
validation run; only this bounded evidence is recorded here.

| Trigger | Trip rule |
| --- | --- |
| Source mutation | Trip immediately when a source mutation is detected. |
| Duplicate publish | Trip immediately when a duplicate formal publication is detected. |
| Output parse failure | Trip immediately when a formal output cannot be parsed. |
| Incorrect completion | Trip immediately when an invalid result is marked completed. |
| Repeated OOM | Trip after the configured bounded repeat threshold; current closeout setting is `3`. |
| Repeated identical stage failure | Trip after the same stage and failure signature reaches the configured bounded threshold; current closeout setting is `3`. |
| Insufficient disk space | Trip before claiming a new job when configured free-space reserve is not met. |

Circuit-breaker semantics are fail-closed for **new claims only**:

1. Stop claiming new jobs once the breaker is open.
2. Allow the already-running job to finish or reach its normal recoverable checkpoint.
3. Preserve all completed stages, checkpoint state, retry evidence, source inputs, and existing outputs.
4. Do not delete, rewrite, or invalidate checkpoints as a breaker side effect.
5. Keep the breaker latched until an operator verifies the cause and performs an explicit recovery action.

## Items not yet verified

- Terminal results of the eight pre-gate running attempts; this goal did not wait for them and they remain excluded from the formal gate.
- Real Production execution of the replacement first-20 cohort; this work must not wait for it.
- Exactly-once Production summary publication after those same fixed 20 members are terminal.
- A real Production runtime-drift event; isolated drift invalidation and same-baseline restart are locally verified.
- Terminal result of the single dispatched recovery canary and later recovery-lane items; this closeout intentionally did not wait for them.
- Full M2 release-acceptance corpus of at least 100 eligible inputs.
- Separate restart, Docker restart, model crash/timeout, OOM, partial-stage, duplicate-event, and temporary-output fault-injection gates.
- Rolling 500-job production SLO evidence.
- Any measured 99% or 99.9% autonomy claim.
- Final disposition of the full queued backlog; this closeout intentionally does not wait for or monitor it job by job.
