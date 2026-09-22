# Laya read-only diagnostic sidecar

Current status: **MODEL_QUALITY_NOT_ACCEPTED; automatic inference disabled**.
The fixed7 incident SDK/adapter audit found no integration mapping/truncation defect;
original and reversed options both scored0/7. Do not tune prompts or re-enable by
claiming these examples represent general accuracy. `deploy.sh` pins
`LAYA_INFERENCE_ENABLED=0`; the service also defaults to disabled. It retains exact
rules, immutable evidence and the existing event cursor without loading the model.
Unknown/mixed events are MODEL_DISABLED, not re-labelled UNKNOWN successes.
Model cache, SDK and model revisions remain retained. Historical inference behavior
below describes the disabled optional path, not an enabled Production classifier.

Oversized events are saved as bounded base64 chunks with source offsets/hash before
durable cursor advancement; discard-until-newline survives restart. A drain is at
most32 bounded chunks/records; malformed events cannot hide subsequent valid events.
The actual Docker restart test and24 targeted tests passed; actual model-container
OOM injection is still unverified and separate from this model quality rejection.

This is an optional consumer of the **existing post-commit pipeline-events.jsonl**.
It never participates in Worker admission, publication, recovery, QC or Gate decisions.
The Worker Dockerfile copies root Python files, not this directory; its dependencies
remain outside the Worker image. No external inference API, TCP port, Docker socket,
GPU, host PID namespace, writable production DB or media mount is provided.

## Frozen software

- Official SDK `NandhaKishorM/laya`, version 0.3.6, source
  `c7527708f9f5220c669d8aa385077cd28d04708a` (archive hash in requirements.lock).
- Model `convaiinnovations/laya`, **only multilingual/**, revision
  `1c5edc17a7acd8701df6fc341c0d179f1c62c982`.
- CPU torch 2.8.0+cpu, transformers 4.57.6, huggingface-hub 0.36.0;
  every resolved dependency is pinned in requirements.lock.
- Python base image digest is pinned in Dockerfile. `Dockerfile.resolve` records
  initial dependency resolution only; production builds use the locked Dockerfile.

SDK `load` has no revision argument. Download the named revision using `hf download`
and load a local directory with `laya.load(local_path, device="cpu")`. The deployed
SDK copies tokenizer metadata into private tmpfs before its official compatibility
adaptation; the downloaded model/config/tokenizer cache remains read-only and unchanged.
Do not use Router or load another checkpoint. No calibration or fine-tuning was added.

## Boundaries and failure behavior

The independent model subprocess is resident, concurrency one. A Unix socket and
filesystem event notifications are local transports, not a new pipeline Queue.
The adapter consumes bounded new JSONL records; initial installation starts at EOF,
not a historical scan. Its own atomic input/result journal and cursor survive restart.
Repeated event timestamps/locations alone do not cause another inference. New evidence,
Worker runtime, SDK/model revision or adapter code revision create a new record.

Known exact reason codes retain deterministic rule classification. Only unknown or
mixed evidence invokes Laya (historical evaluation explicitly runs both). Returned
categories/checks are allowlisted; model scores are uncalibrated and are NOT accuracy.
UNKNOWN is permitted. Model text cannot become a command or change any safety state.
Generic parent review/worker_unknown labels without the original cause, or missing
Stage/attempt, are journaled as UNAVAILABLE with evidence IDs and replay identity;
they never reach inference and never silently keep an older advisory as the new result.

The adapter compares the **entire SDK-generated token sequence** with the non-truncated
request, including instructions, options, evidence IDs and state. Any SDK truncation,
oversize or insufficient evidence is explicit UNAVAILABLE. It never raises max_len
or silently removes incident evidence. The wrapper reason alone is rejected.

Model timeout, stopped process, OOM/offline or invalid SDK output only makes diagnosis
UNAVAILABLE. Own model reload has a 60-second backoff and bounded load failures; it
does not clear or set any Production hold/latch. The host container limit is 2 CPUs,
4 GiB, one model, runc, no network, read-only root and capped private tmpfs. This UNRAID
host has no swap; Docker reports swap-limit capability unavailable (RAM limit applies).

## Deployment and evidence

`deploy.sh IMAGE_ID NEW_RUN_ID` requires an immutable reviewed image, a new persistent
evidence directory, and absence of the named service. It refuses implicit replacement.
Unit-test containers use --rm, labels, a persistent cidfile and host logs. A sidecar
replacement is a deliberate service deployment, not temporary-container cleanup.
Retain inspect/logs and persisted diagnosis data first; never stop Worker for it.

Permanent service: `subtitle-laya-diagnostics`.
State/socket: `/mnt/user/appdata/anime-subtitle-worker/work/laya-diagnostics`.
Read-only cache: sibling `laya-model-cache`.
Evidence: `/mnt/user/appdata/anime-subtitle-worker/logs/laya-20260923`.
Results are immutable by content key; latest.json and health.json are optional UI aids.

See `docs/LAYA_DIAGNOSTICS_20260923.md` for measured limitations. A running diagnostic
container does **not** mean Production recovered or M2 accepted.

Official references:
- https://github.com/NandhaKishorM/laya/tree/c7527708f9f5220c669d8aa385077cd28d04708a
- https://huggingface.co/convaiinnovations/laya/tree/1c5edc17a7acd8701df6fc341c0d179f1c62c982/multilingual
