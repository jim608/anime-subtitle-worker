# Laya diagnostic-only acceptance — 2026-09-23

PARTIAL / WAITING_FOR_EVIDENCE: independent diagnostics and the current controlled
Production recovery are deployed. Classification quality is poor and grants no
operational authority. Full-flow sustained Production acceptance is NOT complete.
All evidence below is under `/mnt/user/appdata/anime-subtitle-worker/logs/laya-20260923`.

## Latest deployed closeout (supersedes initial D checkpoint below)

Final diagnostic service revision E: container
`7c662e64727194edcbd2b43bc43f6aeec9bfeb02a9c7c19bb02317b9de9416ee`, image
`sha256:bbf475827326efcd8348b357d6e3e94de61e53da309415821d98e8fad7f79e20`;
adapter `510e10f2e7df92e6181479c72540771d5c128eea89363eb393be294ce0e71f49`.
SDK/model/dependency revisions below remain unchanged. E changes only diagnostic
evidence validation, not Worker source, QC, admission or the current Gate.

A real parent transition supplied only `quality_blocked_requires_review`, without
its underlying `model_output_cache_lineage_unproven` cause. D incorrectly suggested
TRANSIENT. This input is insufficient, not proof that the model found a retryable
failure. E journals UNAVAILABLE/original_reason_missing without model inference;
missing Stage/attempt is likewise explicit. Structural byte bounds remain enforced.
Old incorrect record `cdea3fa4223ddc2c4ee0aa3a63cdc1c21551d023e7125e976acd37b3fcbab65a`
is unchanged; corrected unavailable record
`36ce2beaef93a971c27e8f05c0679190dbb894a107ce9e44e201d69a51d8ff4c` replays idempotently.
Evidence: `e-parent-evidence.log`, `work/laya-diagnostics/parent-evidence-validation-e.json`.

- E adapter/event tests **18/18 PASS**, `e-pretest.log` and `laya-deploy-e/unit-tests.log`.
- Actual stopped model, timeout, malformed SDK output and tokenizer limit: **4/4 PASS**,
  `e-model-faults.log`, `fault-e/actual-model-faults.json`.
- Same seven independent historical incidents: still **0/7 correct**, seven wrong,
  zero UNKNOWN, including one genuine safety incident misclassified. This is not a
  calibrated accuracy estimate. `e-historical-evaluation.log`; retained immutable records.
- Exact current normalizer incident: E record
  `f3c8cc95e0a47d92ac82bb30eb58a12298b036d486cba250efd54654f55b22e4`,
  rule INTEGRITY_RISK / model RESOURCE (wrong), `e-live-diagnosis.log`.
- E historical latency mean **0.5690 s**, range **0.5003–0.7478 s**; real incident
  **1.1144 s wall /2.1877 s CPU**. Load **6.5104 s**, peak RSS **2,374,356 KiB**.
  Container sample **1.941 GiB/4 GiB**, idle **0.01% CPU**, 8 PIDs, zero network I/O.
- 18 exact one-shot cidfiles are absent after recorded `--rm` runs, plus one actual
  restart fixture explicitly removed after exporting its evidence: **19 verified
  temporary container IDs, zero retained**. `temporary-container-closeout-audit.log`.
  Three stopped **permanent** diagnostic revisions remain intentionally: failed-b
  and rollback-c/d. They are not classified as temporary trash. Project-name/label
  inventory found no other stopped Worker test containers. No force/prune/volume or
  backup deletion. Existing deployment-helper containers also use labeled `--rm`;
  they are not added to the 19-ID count without individual CID evidence.

## Current Production recovery (separate from Laya)

One safe Worker/WebUI deployment completed, ID `20260922T220814Z-1148669`:

- Worker **`cf82fc27d28b0ed69c33ce95ef0f72e0492a53bf`**, actual image
  **`sha256:2733529ad6c8d56b9edab7e8facccc1c4df43843aa70a8a9098a4130dcf3537f`**.
- WebUI **`ef5c20e699f1e5a639eb1207f02ce254274c2a73`**. Optional Laya fields deployed;
  no model request is made by WebUI. Frontend test/build and four backend tests PASS.
- Actual Worker image **201 related tests PASS**, plus real Docker restart with
  interrupted canonical publication commit/resume, exact unchanged output/receipt
  replay, safe review, next claim/Stage/checkpoint and fixture source preservation.
  `actual-image-tests.log`, `actual-image-restart.log`, `actual-image-proof.json`.
- Fresh isolated breaker tests **7/7 PASS**. Controlled recovery
  **`m2-recon-publication-path-20260923`**, receipt
  `/logs/m2-reconciliation-m2-recon-publication-path-20260923.json`, SHA256
  `edf15cd25f71ea70683b3beb68b9cf6f9ff28fc6edb9ac5bea1c3025e55439f0`.
- Original 105 holds retained; this incident adds one, **106 total**. Its terminal
  obligation was already absent from both the pre-deploy and current runnable Queue;
  it remains review-required and held, not recreated or counted as a delivery.
  Reconciliation covers **6,637 recoverable** and **7,477 retained** queue identities;
  no other unexplained identity delta was silently accepted. Historical preservation
  remains UNPROVEN. All **56 old backups plus one new backup =57** retained.
- Initial prepare equality compared a summary projection to a raw Gate row; the
  failure is preserved and exact row/summary mapping was checked before continuing
  only the owned hold. A later execution-record assumption that the incident must
  still be runnable was corrected without redeploying. No safety receipt was altered.
- Breaker **TRIPPED -> ARMED**, exact current event bound, not a reused old incident.
  `recovery-closeout.json`, `host-recovery-finish.log`, `backup-preservation.json`.
- Old Gate014830 remains **SETTLED 20/20,1 strict/19 review/FAIL**. New fixed Gate
  **`m2-gate-20260922T221442778928Z-bfe39573b4`**, start
  **2026-09-22T22:14:42.778928Z**, baseline
  **`m2-guardrail-v1:81505164f40ba0636a2a52c6`**, initialized **0/20**, no imported members.

Last bounded evidence `observation-check-1790116002279178545.json`,
**2026-09-22T22:26:41.205368Z**: ARMED/runtime_baseline_match, two real distinct
terminal-to-next-claim continuations, no same-obligation replays in this three-attempt
window. `e50b523ff8d1980eaab2` safe review at 1790115374.6491446 ->
`08d9e7190948237448ca` claim1790115444.4568477, preflight1790115449.0740478,
SUBTITLE_DETECTION checkpoint heartbeat1790115449.4623435, checkpoint
`5324670ceec04c6de6bd2e6dc63aa785e046ea58fe6112cf425c7072a7a9180c`.
Its safe review1790115453.2332788 -> `73defe9007b102afca05` automatic
claim1790115527.6269252, transcription1790115556.6327612, fresh heartbeat
1790116000.788877, detection checkpoint
`bb43fd4f4a0d20302cd1f9472fe0bceae2ed18d4b91ba235088f70989c1f6199`.
The first two historical jobs are correctly supplemental (`job_started_before_gate`),
not cohort replacements. Current formal Gate **1 enrolled/20,0 settled,0 strict**.

No new qualifying formal subtitle yet (**0**); the third task is running, not delivered.
Only ~12 minutes of this baseline observed. Existing server observer/worker own
continuation; earliest 24-hour point **2026-09-23T22:14:42.778928Z** is not acceptance
by itself. Still need all fixed outcomes, >=2 new qualified outputs and sustained
checks, including actual production publication under the repaired naming boundary.
No M3, quality relaxation, unproven-cache release, active-Gate rebuild or query-based deploy.
**M2 Production accepted: NO.**

## Initial D software checkpoint and unchanged isolation

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

## Initial D measured results (historical; final E results above)

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

## Initial D failure isolation and cleanup

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

## Pre-deployment Production root cause evidence

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
