# M1 Job State Machine

## States

`DISCOVERED`, `STABILIZING`, `ANALYZING`, `QUEUED`, `SUBTITLE_DETECTION`, `ASR`, `TRANSLATING`, `POST_PROCESSING`, `QC`, `MUXING`, `RETRYING`, `NEEDS_REVIEW`, `FAILED`, `COMPLETED`.

## Normal progression

```text
DISCOVERED -> STABILIZING -> ANALYZING -> QUEUED
QUEUED -> SUBTITLE_DETECTION -> ASR -> TRANSLATING
       -> POST_PROCESSING -> QC -> MUXING -> COMPLETED
```

Optional stages may be skipped with an audited forward transition when existing subtitles or cached validated artifacts make them unnecessary. A state transition uses compare-and-set against the current state, records `job_id`, media revision, reason code, evidence, confidence and time, and may not leave a terminal state except through an explicit operator recovery API.

## Failure progression

```text
active state -> RETRYING -> QUEUED
active state -> NEEDS_REVIEW
active state -> FAILED
```

`RETRYING` is allowed only for classified recoverable faults within retry budget. Quality ambiguity becomes `NEEDS_REVIEW`. Unsupported/permanent faults or exhausted retry budget become `FAILED`. Neither state can be mislabeled `COMPLETED`.

## Legacy stage mapping

- preflight/worker/source selection -> `ANALYZING` or `SUBTITLE_DETECTION`
- audio/transcription -> `ASR`
- translation -> `TRANSLATING`
- cleanup/OpenCC/ASS export -> `POST_PROCESSING`
- quality gates -> `QC`
- completed media delivery -> `MUXING`

Legacy stage strings remain available for compatibility and are recorded as evidence on the formal state attempt.

