# M2 reviewed extraction recovery — 2026-09-12

Candidate verified; not yet deployed. M2 NOT COMPLETE. Formal additions remain
AI0/download0/extraction0. This is the necessary completion of the existing M2
single-job recovery path, not a new Queue, state machine, milestone or QC policy.

## Existing entry and authorization boundary

`mikan.requeue_extract` accepts an optional `reviewed_repair` request and delegates
through `requeue_mikan_extract_job`. Normal Retry's existing behavior is unchanged.
The reviewed mode accepts only one exact `replaced` / `subtitle_validation_failed`
job with0 extracted outputs and one unambiguous prior member, not arbitrary jobs
or whole collections. The immutable old completion-revalidation receipt is not
edited or reused as authority for a different source.

Request contract `m2-reviewed-extraction-repair-v1` binds:

- Explicit authorization reference and stable request ID.
- Exact torrent hash and expected current extraction-row/pending-entry digests.
- Previously reviewed target identity plus current complete download identity.
- Current ARMED runtime baseline fingerprint and processing-module hashes.
- Hash-pinned server validation report and code attestation below the log root.

All normal ownership/state locks, live AI ownership, source holds, current source
hash/size/mtime, managed qB category/tags, download/file completeness, path mapping,
episode selection, trusted target matching, valid-output preservation and natural
backoff must pass. HTTP/torrent100% or the request alone does not authorize output.

One transaction in the existing Mikan DB archives the full original job/pending
row and request in an immutable `reviewed_extraction_repair` event, restores the
exact pending member and queues the original extraction job. Attempts and failure
receipt are not reset; failed hashes/URLs and prior completion receipt are retained.
The event is a lifetime one-reopen budget for this job/processing revision and is
excluded from transient UI-event compaction/pruning. Replay returns already_recorded
with queued0. Different requests cannot reuse the spent budget.

The existing normal Worker claim/lease/heartbeat/progress/resume/completion flow
does the work. Claim and publication recheck the persisted request, actual runtime,
processing code and source/target identities. A refused claim retains its budget
and evidence with a bounded future eligibility time so other jobs can proceed.
Publication records the durable receipt reference and both identities in the
existing versioned manifest; it rechecks immediately before atomic publication.
No direct publication or torrent-add operation occurs in the recovery request.

Changed modules: `mikan_extraction_repair.py`, `mikan_worker.py`, `main.py`,
`subtitle_extract.py`; tests `test_mikan_extraction_repair.py` and
`test_parallel_chinese_import.py`. No runtime configuration/model/QC/Decision Schema
or WebUI change. The normalization helper from checkpoint5aeddf53 is unchanged.

## Verification and boundaries

Server489 related tests PASS at
`/logs/m2-reviewed-extraction-repair-20260912T1955/targeted-5vBgK6/`:
targeted-tests.log, candidate-files.sha256, candidate-parity.log, exit.txt0.
Local486 related regressions and the final15 repair tests also PASS.
The server suite covers the existing control entry, source/owner/backoff refusals,
foreign/partial downloads, changed matching/code/runtime/evidence, atomic rollback,
durable replay, normal claim/heartbeat, spent budgets, other-job progress, immutable
event retention, pending-result convergence and manifest/receipt binding.

Actual disposable-container loss/restart test at sibling `targeted-08tDvh/`:

1. Exit73 after event/pending writes but before the job update/commit.
2. A new container proves complete rollback and records exactly one normal retry.
3. A new container replays without queue duplication, really claims the job,
   persists heartbeat/progress and refuses a second concurrent claim.
4. A new container respects the unexpired lease; fixture-clock advancement (no
   lease-field edit) then exercises the normal expired-lease resume. Still one
   recovery receipt; source/target bytes unchanged.

These restart tests use fixture runtime/qB responses and real isolated SQLite,
files and container process loss. They are not Production recovery evidence.
No Production media/state mounts/network/GPU; test sources/outputs are fixtures.
All four-container logs and expected-crash-exit.txt73 are retained; suite exit0.
Its production-module hashes are unchanged in the final489-test receipt.

Fresh real-media verification for the updated publication boundary:
`/logs/m2-source-followthrough-20260912T1848/parallel-integration-kYs2g3/`, exit0.
Actual ffmpeg extraction, original QC,351cue TC fixture publication, a new
container's zero-duplicate replay and held-source refusal passed. Raw ASS snapshot
and380 retained text lines remain verifiable. Both mounted videos are read-only;
target SHA1a514508... / download1fe8edad... unchanged. Final ASS1b9ce7b0... remains
identical to the previous candidate. This is still NOT a new formal subtitle.
Current subtitle_extract.py SHAa5b24345dc0b08801d468bf9fe149f197f1bfa4a96b436af597f1fcc234ee6ff;
projection helper SHA fdb28858dccd5a45694a5fc1071cdc2696f542a47f86807f33a1b695187e1de3.

Exact live target recheck `live-preflight-1789243618690897014.json` under the
reviewed-repair log root still finds0 valid official languages, no valid AI,
18 existing sidecars, c1102 complete/progress1/amount_left0 in the managed qB
category/tags, job=replaced and pending=extract_failed. Target/download full
checksums match the real-media proof. This read made0 Production writes.

## Remaining acceptance

The latest bounded live report is
`/logs/m2-source-followthrough-20260912T1848/runtime-1789242020621114780.json`
(2026-09-12 19:40 UTC). Worker5e73636c317cc32919ce1a8104dafc0d05ce0b5c,
WebUI175a02a7bad46e0b6fa2372c59f39e8dd272911e, ARMED,70 source holds, UNPROVEN.
Gate183251 ACTIVE10 enrolled/10 settled; no active AI row at that snapshot.
No Production mutation/restart/deployment/Gate change was made during these tests.

Required next: fresh safe-idle/owned-drain handoff, safe-update-stack, actual-image
validation/fresh7 breakers and controlled recovery. Preserve original receipts,
Gate members/backups/70 holds; classify new differences, never absorb them.
Then submit one fresh exact c1102 request through the existing control command,
observe actual Worker processing and formal missing-TC publication, re-read final
output/QC/manifest/source checksums and verify idempotency/state convergence.
Do not reuse old82ace3 request or completed1815/1730 handoffs, clear failed hashes,
force-kill work, wait Gate20/backlog, mix versions or declare M2 accepted.
