# Laya diagnostic-only acceptance — 2026-09-23

PARTIAL: the independent CPU diagnostic service is deployed; classification quality
is poor and grants no operational authority. Production recovery is NOT complete.
All evidence below is under `/mnt/user/appdata/anime-subtitle-worker/logs/laya-20260923`.

## Actual frozen software and isolation

- Official SDK Laya 0.3.6, Git `c7527708f9f5220c669d8aa385077cd28d04708a`.
- `convaiinnovations/laya`, only `multilingual/`, revision
  `1c5edc17a7acd8701df6fc341c0d179f1c62c982`.
- torch 2.8.0+cpu; transformers 4.57.6; huggingface-hub 0.36.0. Full dependency
  lock and SDK archive hash: `diagnostics/laya/requirements.lock`.
- Five downloaded files verified against pinned Hub git/LFS identities; report
  `work/laya-diagnostics/model-integrity.json`. SDK tokenizer adaptation uses private
  tmpfs metadata, not writes to the original read-only cache.
- Service `subtitle-laya-diagnostics`, container
  `d2510ce178324d1313da5b35855d9444a0d5c083758f6d480a1c9b085b19e0c3`, image
  `sha256:4de2626f28c9d540a906615927dd15523760eb434a105d4a4528698709fd0d81`.
- Adapter bundle SHA256 `e0ac28fe9face4f9522f9f2b1a83d29f9bd60b8c5a210263090d17cfc87f3a94`.
- UID/GID 99/100, runc, concurrency one, 2 CPUs/4 GiB, no GPU, TCP port, network,
  Docker socket, shell tools, writable Production DB or media mount. Read-only root,
  dropped capabilities, no-new-privileges; writable state is its own diagnostics directory.
- State/cache: `work/laya-diagnostics` / `work/laya-model-cache`. Existing post-commit
  `pipeline-events.jsonl` consumed via filesystem notifications and durable cursor;
  initial install starts at EOF. Model calls never occur inside claim/publish transactions.
- All outputs are advice only. Invalid/missing/expired evidence is not authorization
  to clear a breaker. Scores are not calibrated correctness probabilities.

## Measured results (final image)

Seven independent historical incidents, not repeated logs from the same incident:

| Incident | Confirmed label | Deterministic rule | Laya |
|---|---|---|---|
| malformed SRT, Sep 19 | BAD_INPUT | BAD_INPUT | RESOURCE |
| subtitle encoding, Sep 20 | BAD_INPUT | BAD_INPUT | RESOURCE |
| SQLite lock, Sep 19 | TRANSIENT | TRANSIENT | RESOURCE |
| unaccepted ASR cache, Sep 13 | INTEGRITY_RISK | INTEGRITY_RISK | RESOURCE |
| checkpoint bridge, Sep 19 | SYSTEM_BUG | UNKNOWN | RESOURCE |
| report export, Sep 20 | SYSTEM_BUG | UNKNOWN | RESOURCE |
| output normalizer rename, Sep 20 | SYSTEM_BUG | UNKNOWN | RESOURCE |

Laya: **0/7 matching labels, 7 wrong, 0 UNKNOWN; one genuine safety incident
misclassified**. Rules: four matching labels, three UNKNOWN in this selected set.
No claim of general accuracy; no tuning, alternate model, or automatic action added.
Complete pinned inputs, raw SDK scores and timing: `work/laya-diagnostics/historical-evaluation.json`
and immutable input/result records; log `final-historical-evaluation.log`.
All full requests fit (385–408 actual tokens); silent SDK truncation is rejected.

One actual current incident: `m2ai_832fad6f8dc4f831c062`, event
`1789963021.6800535`. Advice record
`0a767220e7f48c787a91319cfc04c1cda06276533bc8c3528917914a5a4a6560`:
rule INTEGRITY_RISK, Laya RESOURCE (not the confirmed root cause). Evidence:
`current-incident.json`, `incident-normalizer-verified-id.log`, `final-live-diagnosis.log`.

Final-image latency: historical mean 0.5104 s (0.4898–0.5394 s); current-event
0.9357 s wall / 1.8343 s CPU. Model load 5.2702 s. Peak process RSS 2,374,816 KiB;
21:51 UTC container sample 1.717 GiB/4 GiB, idle CPU 0.04%, network 0 B, 8 PIDs.
Host has no swap; RAM limit verified but swap-limit capability is unavailable.

## Failure isolation and cleanup

- Final image: 16/16 adapter/event tests PASS (`final-sidecar-tests.log`).
- Actual model process stopped to force timeout, terminated/offline, malformed SDK
  output, and 12,246-token oversize request: 4/4 PASS (`final-model-faults.log`,
  `fault-final/actual-model-faults.json`). Only diagnosis becomes UNAVAILABLE.
- Timeout includes bounded child termination grace (~5.16 s in the fault test).
- Original Worker image: 9/9 isolated admission/report/safety regressions PASS
  (`worker-admission-regression.log`). This is NOT proof Production resumed.
- WebUI optional-read adapter: 4/4 tests and frontend test/build PASS; NOT deployed yet.
- No actual whole-container OOM was induced. Service restart/event replay has unit
  evidence; long-running event rotation/oversize-line recovery remains a limitation.
- One-shot download/test containers use `--rm`, persistent logs/cidfiles and labels.
  At 21:51 UTC, the project label inventory found no stopped temporary containers.
  Two stopped permanent diagnostic revisions are retained deliberately: failed-b
  startup evidence and rollback-c. They are not disposable tests. No prune, force,
  volume/image removal, backup cleanup, Worker restart or Gate change for Laya.

## Production root cause remains separate

Actual Worker `f52118c564e9aebca89d52b336e2156d2ba095cd`, WebUI
`69f2352d0d5d371ca58eaf5a4c62d97f79529e69`; effective breaker TRIPPED.
The Sep 21 event is a normalizer/manifest integration defect, not Gate FAIL:
claim 03:56:19.690543Z; external `sublangid` rename 03:56:37.179342336Z;
first incorrect_completion trip 03:57:01.680053Z. The renamed output retains its
recorded hash/size/mtime, but the old exact-path receipt points to the absent name.
Original source checksum still matches its recorded provenance. Do not rewrite
that receipt as PASS or treat the model's RESOURCE suggestion as a verified cause.

Minimal canonical publication-path and typed incident-recovery candidate: 113 related
isolated tests PASS (`related-recovery-regression.log`), not yet deployed at this
checkpoint. Requires actual-image/restart proof, exact current incident controlled
recovery, and real terminal-to-next-claim evidence. 105 existing holds retained.
Gate `m2-gate-20260920T014830021232Z-8147e39531` settled 20/20, 1 strict /19 review/FAIL;
baseline `m2-guardrail-v1:c83fd65db8ed1c8307468303`. No query-driven Gate reset.
New formal subtitles this work: **0**. M2 Production accepted: **NO**.

Official references:
- https://github.com/NandhaKishorM/laya/tree/c7527708f9f5220c669d8aa385077cd28d04708a
- https://huggingface.co/convaiinnovations/laya/tree/1c5edc17a7acd8701df6fc341c0d179f1c62c982/multilingual
