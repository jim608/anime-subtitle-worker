# M1 Recovery Policy

## Error classes

- `transient`: bounded retry with exponential backoff and jitter.
- `resource`: bounded delayed retry, optionally with reduced parameters or configured fallback.
- `quality`: use configured fallback; if evidence remains ambiguous, enter `NEEDS_REVIEW`.
- `permanent`: enter `FAILED` without automatic retry.
- `interrupted`: process/container stopped while a Stage was running; close that attempt, consume the bounded Stage retry budget, and resume from the latest valid checkpoint. Repeated interruptions eventually enter `NEEDS_REVIEW` rather than retrying forever.

## Checkpoint validity

A checkpoint is reusable only when its recorded input identity matches the current media revision, every required output exists, and stored size/hash/manifest evidence validates. A completed database row without valid output evidence is insufficient. Invalid or partial temporary output is quarantined/replaced by the responsible Stage and never advances the Job.

## Restart sequence

1. Open the persistent WAL database and atomically mark unfinished attempts `interrupted`.
2. Verify the last completed checkpoint without walking unrelated media roots.
3. Transition recoverable Jobs `RETRYING -> QUEUED` with an audited reason only while their saved retry budget permits it.
4. Resume at the first incomplete/invalid Stage; do not rerun earlier valid expensive Stages.
5. Enforce the saved retry budget and timeout for the resumed Stage.

## Source and output safety

Source video is opened read-only and never renamed, overwritten or deleted. A captured source snapshot is verified immediately before publication so a changed source fails closed; its identity is checked again before formal completion, and integration tests compare content checksums before/after. All formal outputs use a per-Job temporary/staging path, validate there, and publish by atomic rename/no-clobber logic. Completion rehashes manifest-v2 and every required final artifact immediately before committing `COMPLETED`. A crash before rename leaves no completed Job; a crash after rename is reconciled from the publication receipt/manifest.
