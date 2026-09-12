# M2 parallel Chinese subtitle repair — candidate evidence

Status: **candidate verified, NOT deployed; M2 NOT COMPLETE**.
New verified formal deliveries remain **AI0 / download0 / extraction0**.
This extends the existing missing-TC obligation, not M3/M4 or a new milestone.

## Reproduced cause and narrow repair

Anonymous obligation `m2dl_f0133acfdbb11284e318` (3583:2) has a complete
328160191-byte retained Kitauji EP2 download, hash
`c1102b70ebe9c0676e82498f39ffaa8735b933e1`. The original TC ASS stream has
722 events: parallel Chinese/Japanese layers cause hard timing-overlap QC
failures. This is not a network, missing-mount or damaged-video diagnosis.
The other two failed sources remain excluded; they were not re-added/retested.

The candidate only handles explicitly named JP/CN counterpart styles with
content-language evidence above existing analyzer thresholds and exact paired
timings. All non-JP styles are retained. Every retained text line must occur in
an overlapping normalized time window; omission, ambiguity, incompatible event
format or insufficient language evidence refuses normalization. It reuses the
existing ASS timing/export utility, without changing subtitle wording, QC
thresholds, models, configuration or Decision Schema.

Only private embedded import candidates with an original timing-overlap failure
enter this path. Existing sidecar verification is unchanged. Original rejection
diagnostics remain; the derived candidate must pass the same full target-aware
source analysis, parsing and hard QC. The publisher binds the derived hash to
that validation, checks the original staged bytes, persists an original ASS
snapshot and transformation evidence in the existing versioned manifest, and
preserves different valid existing outputs. This is not a new publisher/Queue.

Files: `subtitle_language_projection.py`, `subtitle_extract.py`,
`test_subtitle_language_projection.py`, `test_parallel_chinese_import.py`.

## Actual server isolation and restart evidence

Root: `/logs/m2-source-followthrough-20260912T1848/`.
Successful integrated fixture: `parallel-integration-Nz6Kge/`, `exit.txt=0`.
Production source/download videos were individual read-only mounts; only the
fixture directory was writable. No Production work DB/output directory, network
or GPU was mounted/enabled. Existing Production configuration/QC was loaded;
only fixture state/output paths and AI-retirement behavior were isolated.

- Phase1: real ffprobe/ffmpeg, real embedded classification and target-duration
  validation, existing publisher, manifest and final-path revalidation passed.
  Removed350 exactly paired JP events; retained372 events/380 text lines;
  normalized351 cues. Language zh-tw confidence0.995, coverage0.999015.
  Existing CPS/line-length/duration warnings remain; hard QC has no failures.
- Phase2: a new container reused persisted fixture DB/output/manifest. The same
  extraction produced0 additional publications, one manifest total, unchanged
  output bytes/mtime and unchanged manifest hash.
- Phase3: the official source-hold API installed a fixture hold. A new container
  refused publication with `source_continuity_unverified`; existing output and
  manifest were unchanged. The hold was not deleted to make the test pass.
- Original subtitle snapshot SHA256:
  `2b18d2cf85f181e407ab92744d6cf05f071f43ba7733a5a08bc43feb63e360af`.
- Final ASS SHA256:
  `1b9ce7b062e20f2d82dfe22504cc23993ab2fb1661f641885b6f9f07943e99a3`.
- Download SHA256 unchanged:
  `1fe8edad0311243e046b0568c3414df7c3adac2d09068b65a94e168bd6aec033`.
- Target video SHA256 unchanged:
  `1a514508c2dc1682ca98f65b4f7c494a899cc754d7f68b4f6ac1088bdf93c2af`.

Full evidence: phase-1/2/3.json, phase-1/2/3.log, original snapshots and manifest,
candidate-files.sha256, final-candidate-files.sha256, final-74-tests.log.
Candidate production-module hashes:
subtitle_extract.py `7966d92663e48b91f75e865842d230c33770c8be2793b0ec736207a0f351e841`;
subtitle_language_projection.py `fdb28858dccd5a45694a5fc1071cdc2696f542a47f86807f33a1b695187e1de3`.

Targeted command (local and isolated server):

```text
python -B -m unittest test_subtitle_language_projection test_parallel_chinese_import test_subtitle_extract test_mikan_import_validation test_mikan_batch_completion -q
```

74 tests PASS. Includes happy/failure paths, deterministic replay, retaining
valid outputs, changed-lineage refusal, interrupted original-snapshot recovery,
and Mikan result convergence without losing original rejection diagnostics.
The actual media test is additional to those unit/integration fixtures.

Retained earlier attempts are not relabeled PASS: JhlzY2 stopped before publish
because its fixture state store was absent (`source_hold_store_unavailable`).
The corrected fixture initializes the normal ScanStateStore; no guard was
disabled. The earlier local cp950/newline failures were fixture portability
issues, corrected by explicit UTF-8/exact byte writes. A first live probe used
the wrong `/app/work` script path; the corrected `/work` probe succeeded.

## Current runtime / Gate and exact remaining work

Live snapshot `/logs/m2-source-followthrough-20260912T1848/runtime-1789241104407110358.json`:
Worker `5e73636c317cc32919ce1a8104dafc0d05ce0b5c`, image
`sha256:534f90a2d84ce4f332d7bdb272fb74ad3069f4f4a82151c28025dc3da840f419`;
WebUI `175a02a7bad46e0b6fa2372c59f39e8dd272911e`; runtime ARMED,70 source holds,
operator pause=false, reconciliation_hold=false. At this bounded snapshot there
were0 running AI Queue entries; docker-top showed the live main Worker. This is
a point-in-time observation, not a future safe-deployment permission or new claim.

Current Gate `m2-gate-20260912T183251418961Z-5f0cbe7f8a` remains ACTIVE with
7 enrolled/7 settled, baseline `m2-guardrail-v1:a4504d3092426d19e4ab433d`.
No Gate mutation/claim substitution occurred. Original settled20 Gate125703
remains FAIL6strict/14review; report SHA256
`f13782698addc2936de1c992801f8290e56a2ca21eeff8333b5ea3929b05b4a3` reverified.
Preservation remains UNPROVEN. Existing accepted M3 evidence is unchanged.

Remaining required actions:

1. The real failed extraction row is `replaced`. Existing
   `requeue_mikan_extract_job` accepts only failed/terminal_failed, resets budgets
   and has no reviewed-repair receipt. Do NOT change the row manually, erase
   failed hashes or repurpose the old completion-revalidation request82ace3...
   (it is bound to e22, not c110). Add the minimum evidence-bound recovery mode
   through the existing single-job recovery entry: exact current row/pending
   revisions, current source/target identities, trusted matching, no live owner,
   unchanged runtime/config, retained original receipts and one persisted retry
   budget. Use the existing Queue/lease/heartbeat/completion mechanism, not a
   parallel worker or direct call counted as autonomous processing.
2. Prove that recovery mode's replay/restart/refusal behavior in isolation,
   preserving all failed-source history, other jobs and valid outputs. A pending
   row must be restored through the controlled transaction so the real result
   can converge; simply queueing the extraction row is insufficient.
3. Commit/push the reviewed repair set, then fresh safe-idle preflight and existing
   safe-update-stack/controlled recovery. Do not reuse either completed1815 or
   1730 handoff. A code deployment changes baseline and requires the normal
   explicit Gate transition, retaining all old members/results. Docs alone do not.
4. One real retained-download job must auto-claim on the deployed candidate,
   pass actual matching/QC, publish to the genuinely missing formal TC target,
   then final-path re-read, manifest/source checksums and idempotency verification.
   Fixture output is NOT this acceptance. Keep all70 holds/UNPROVEN.

Do not rerun successful media phases or all-project tests unless affected.
Do not wait Gate20/backlog or repeatedly inspect full Queue/logs. M2 remains open
until real formal download/extraction evidence and truthful Gate disposition exist.
