# 100-video unattended acceptance

This directory defines a fail-closed, read-only acceptance oracle. It does not
start Worker, inject a fault, enqueue media, change a database, deploy a stack,
or claim that a corpus/result exists. A separate, controlled test run must
produce the fixed plan. The collector can then assemble hash-bound observations
from durable artifacts without starting Worker or injecting faults.

## Contracts

- `acceptance_manifest.schema.json` pins exactly 100 distinct canonical media
  identities, their expected source route, corpus strata, and planned faults.
  Schema version 2 additionally pins the source SHA-256 plus the deterministic
  completed-receipt and final-MKV paths for every case. Version 1 remains
  readable for the earlier subtitle-sidecar qualification.
- `acceptance_observations.schema.json` binds the test window and every case to
  the exact plan-file SHA-256. Every observation and fault needs a readable
  hash-bound evidence artifact.
- `fault_evidence.schema.json` proves that a predeclared fault was observed and
  then recovered automatically from a named checkpoint. Its identity,
  timestamps, scenario, trigger, and empty manual-intervention list are checked
  against the plan and observation; an arbitrary log file is not enough.
- `run_unattended_acceptance.py` validates real media with `ffprobe`, then
  evaluates existing artifacts and SQLite in read-only mode.
- `acceptance/planner.py` prepares the corpus from the scanner queue in SQLite
  `mode=ro`. It accepts only terminal queue rows whose current media identity,
  strict manifest, and complete current-policy provenance prove one of the four
  expected routes. It does not infer a route from a filename or metadata hint.

For a version 2 plan, each observation must contain hash-bound
`completed_delivery_receipt` and `completed_mkv` references at the exact paths
declared by the plan. A version 1 plan does not silently acquire this new
requirement.

The executable validator also enforces constraints that JSON Schema alone
cannot express: exactly 100 unique paths/fingerprints/obligations (and, for v2,
unique source SHA-256 values), at least ten
cases for each of the four source routes, at least 10 series, at least ten cases
in each of two containers and in every duration bucket, 10 faulted cases, and
every defined fault type. Duration buckets are `<10 min`, `10-35 min`, and
`>35 min`.

## Strict success oracle

A case succeeds only when all of the following are true:

1. The current file still has the plan's canonical delivery identity and policy
   revision, and `ffprobe` proves it has positive duration, video, and audio.
2. the existing Worker v2 manifest passes `validate_output_manifest` with exact
   obligation/policy identity, every output SHA-256, and no publishing marker;
3. strict publication semantics contain exactly one `zh-TW` output;
4. that exact Traditional Chinese output still passes live subtitle QC, is
   non-empty, and the existing content classifier identifies its script as
   `zh-tw` without trusting filename/metadata hints;
5. scanner SQLite has the same succeeded obligation, exact manifest path/SHA,
   and verified publication semantics containing `zh-TW`;
6. processing provenance is complete, policy-bound, reports the predeclared
   route, and proves ASR was used only for `japanese_audio_asr`;
7. the observation completed without review or manual intervention, all
   evidence hashes match, and every planned fault was injected and recovered.
   Fault recovery additionally requires a terminal failed scanner-ledger
   attempt inside the injection window and a later succeeded attempt; a
   self-authored recovery JSON by itself cannot pass.
8. for schema version 2, the `completed-mkv-delivery-v1` receipt is committed,
   bound to the exact source SHA, obligation, policy revision, subtitle
   manifest SHA, and configured completed root; the source remains present,
   the final MKV size/SHA match, and no delivery marker or owned mux partial is
   left behind;
9. both the independent acceptance probe and the production delivery validator
   prove source audio/video preservation and exactly one default subtitle,
   which must be the `zh-TW` track titled `AI 繁體中文`.

Version 2 also adds `mux_process_crash` and
`completed_publish_interrupt`. Their evidence must use schema version 2, bind
the observed failure to injection stages `mux` and `completed_publish`,
respectively, name `completed_delivery_committed` as the automatic recovery
checkpoint, and have a matching `completed_delivery` failed then succeeded
scanner-ledger attempt. The committed receipt timestamp must fall inside the
fault recovery window.

A Simplified Chinese file, a non-Chinese source transcript, an empty/corrupt
subtitle, an identity drift, an unverified database status, or a human action
cannot count as success.

## Commands

Preparation, validation, and evaluation are read-only by default and print
JSON to stdout; collection writes only the new observations path:

```powershell
python run_unattended_acceptance.py `
  --prepare-plan `
--config .\config.yaml

# Only after reviewing a ready preview; this refuses an existing destination.
python run_unattended_acceptance.py `
  --prepare-plan `
--plan-output .\acceptance\plan.json `
--config .\config.yaml

python run_unattended_acceptance.py `
--validate-manifest .\acceptance\plan.json `
--config .\config.yaml

python run_unattended_acceptance.py `
--collect .\acceptance\plan.json `
--observations .\acceptance\observations.json `
--config .\config.yaml

python run_unattended_acceptance.py `
--evaluate .\acceptance\plan.json `
--observations .\acceptance\observations.json `
--config .\config.yaml
```

The planner is deterministic for an unchanged queue/evidence/media snapshot.
It returns no `plan` unless it can pin exactly 100 distinct files with source
SHA-256, mtime, current policy and obligation identities, a proven route, at
least ten cases per route, at least ten series, all three duration buckets,
and two containers with at least ten cases each. Audio/subtitle stream layouts
come from read-only `ffprobe`; `series_id` comes from the Worker series-root
rule; `release_profile` records the exact queue source. Ambiguous or missing
route evidence is listed under `unresolved_expected_routes` and is never
coerced to an ASR route.

Exactly ten cases receive one deterministic `planned-only` fault declaration
covering every v2 scenario. Each fault is assigned only to a source route that
reaches the trigger stage (for example, an ASR crash can only be assigned to an
ASR case). Preparation never starts Worker, enqueues media, injects those
faults, creates observations, or changes SQLite. Explicit plan output uses
exclusive creation and has no overwrite switch.

Collection reads source identity, output manifests, processing provenance, the
scanner ledger through SQLite `mode=ro` plus `query_only`, completed-delivery
receipts/final-file metadata, and `fault-evidence/<fault_id>.json`. It never
starts Worker, injects a fault, enqueues work, or changes a database. Existing
observation files are never overwritten.

Missing or unreadable case evidence produces `outcome: failed`; missing or
invalid fault evidence produces `status: not_recovered`. Because the existing
v1/v2 schema requires a SHA and injection timestamp even when evidence is
absent, the collector uses an all-zero SHA and the case-start timestamp as
fail-closed sentinels. These sentinels can never qualify. For v2 final MKVs, the
collector copies the full-file SHA from the committed receipt; evaluation then
independently hashes the current MKV. Collection assembles observations;
`--evaluate` remains the strict success oracle.

`--collect ... --observations PATH` and `--report PATH` are the only write
operations. Collection never overwrites. Reports refuse replacement unless
`--overwrite-report` is explicit. Exit code `0` means collection found all
required evidence, the plan is valid, or full evaluation passed; exit code `2`
means fail closed.

The acceptance threshold is at least 99/100 unattended successes, at most one
review case, zero manual interventions, and recovery of every planned fault.
When completed delivery is declared, every declared completed artifact must
verify; 99/100 completed MKVs cannot qualify. This fixed 100-case run is a
release/canary gate, not statistical proof of a 99.9% long-run success rate.
