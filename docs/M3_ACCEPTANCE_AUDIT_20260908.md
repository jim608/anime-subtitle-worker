# M3 acceptance evidence audit — 2026-09-08 13:19 UTC

## Final M3 engineering closeout — 2026-09-08 13:27 UTC

M3 configured-model/recovery engineering scope is verified and delivered.
This does not accept M2 or overall unattended Production reliability.

The remaining duplicate-ingress check now passed on actual image
sha256:e34372dc6ae9f1e28b293ce6f9dd61414f7c0b292ad32588f43dcd2613f1c617.
Real ffmpeg media, watchdog OS events, ffprobe, durable SQLite ingest, scanner
classification and exact admission lookup ran without business-logic mocks.
Two watcher/scanner restart cycles each observed10 OS events and1 completed
promotion; each ended with1 done Queue row,0 runnable jobs,0 new obligations,
unchanged source/manifest/ASS hashes and mtimes. No model/publication was invoked.
Only fixture artifacts were mounted, network disabled, no Production media/work DB.
This is complete ingress-to-admission refusal evidence, not a second model E2E.

Evidence `/logs/m3-review-convergence-20260908T1210/duplicate-ingress-XioDjW/`:
`full.log`, `result.json`, `exit.txt` (0). Harness
`work/m3_duplicate_ingress_probe.py`. Earlier `duplicate-ingress-UN53qE/`
ended before full debounce/promotion and is retained as narrower evidence only.

Final runtime1788874027.252612 remains ARMED/runtime_baseline_match, admission
open,68 holds and same Gate. Host provider observation1788874021.8963742 VERIFIED.
Evidence `known-alternative-9f/final-m3-runtime.json`.
No runtime/config/deployment/Gate change in this final test/documentation step.
New verified AI TC delivery remains1, download0/extraction0; no recount.

Scope: configured model adapters/fallback, durable bounded request ownership,
resource-aware recovery, safe deployment and actual AI publication. This is not
M2_PRODUCTION_ACCEPTED, a new Gate, or a 99% reliability claim.

## Requirement matrix

| Requirement | Evidence inspected | Result and boundary |
| --- | --- | --- |
| Existing routes and narrow adapter changes | Configured-only route tests; retained candidate full run2023/OK | Fixture baseline/regressions verified; no model-policy relaxation |
| Durable configured fallback and budgets | route-cancellation-boundary345/OK; durable-binding-transport-final324/OK; real job91 RESPONSE events across Sakura and configured qwen fallback, all within effective limits | Implementation and real responses verified; fixtures and real inference counted separately |
| Timeout/crash/restart/no duplicate send | Real HTTP process-loss1/OK; Docker restart fixture exit0 and duplicate_dispatches0; provider-termination fixture source/checkpoint/budget unchanged, replay idempotent | Real disposable container/transport lifecycle verified, not a Production provider restart |
| Resource/OOM recovery | durable-resource-admission342/OK and config-final16/OK; inspected tests assert lower-memory route or defer after OOM, not bypass; conservative unverified scope remains | Simulated resource/OOM behavior verified; no deliberate Production OOM |
| Actual safe deployment | deployment20260908T121255Z-1408116 exit0, recovery exit0, actual-image439/OK; isolated breaker7/7; backup/receipt retained | Deployed image verified; no repeat deployment for documentation |
| Actual processing and publication | job9f5394b655b340b2822cc43337dbcaf0 auto claim, real models, COMPLETED/ledger succeeded; formal285cue ASS files parse/QC and manifest-v2/hash PASS | One new AI TC target verified, not three deliveries; download0/extraction0 |
| Exact retained lineage | manifest target digest matches prepared token and unique confirmation, runtime/Gate/watermark/time ordering match | Verified against retained manifest/events; intermediate CN SRT no longer exists and was not recreated |
| Idempotency | Real completed-target predicates; real OS duplicate ingress through promotion/scanner/admission on actual image across2 fixture restarts; no runnable job, unchanged files; prior restart fixture no duplicate send | Verified without reprocessing the valid Production target; fixture artifacts are not additional delivery |
| Source and historical safety | Pre/post source/JA/original sidecar hashes unchanged; current68 holds; preservation UNPROVEN retained | Source preservation for the real canary verified; no historical continuity upgrade |

## Authoritative evidence locations

Base `/logs/m3-baseline-20260908T070237Z/`:

- `candidate-full-predeploy-final.log` (2023/OK).
- `route-cancellation-boundary-server.log` (345/OK).
- `durable-resource-admission-server.log` (342/OK),
  `durable-resource-config-final-server.log` (16/OK).
- `durable-unload-server.log` (327/OK),
  `durable-binding-transport-final-server.log` (324/OK).
- `http-process-loss-server.log` (1/OK).
- `docker-request-restart-U48jOk/container.log`, `final-state.log`, `exit-code.txt`:
  restart_verified, unchanged fixture source/checkpoint, UNKNOWN retained,
  duplicate_dispatches0, no Production mounts.
- `provider-termination-O2PvZp/fixture/result.json`: actual HTTP received,
  provider terminated after sender exit, settlement replay idempotent,
  checkpoint/source/budget unchanged, job not falsely completed.

These suites overlap; their counts MUST NOT be summed.

Deployment `/logs/m3-review-convergence-20260908T1210/`:
`actual-image-validation.log` (439/OK), `deploy.exit`, `recovery.exit`,
`attestation.json`, `recovery-closeout.json`, retained deployment backup.
The actual-image suite covers main Queue, scan state, source analysis, logger,
model request recovery/state, Pipeline state, durable translation, checkpoints,
and Worker. Older model/resource fixture evidence is not mislabeled as a fresh
439-test execution of every module.

Real job evidence in its `known-alternative-9f/` subdirectory:
`model-evidence-2.json`, `formal-verification-language-roles.json`,
`manifest-lineage-idempotency.json`, `m3-audit-runtime.json`.

## Current state and remaining work

Fresh runtime check1788873546.176765:
Worker738e96eafec66a6f62db31b9247cd58bf0aec6df,
WebUI175a02a7bad46e0b6fa2372c59f39e8dd272911e,
ARMED/runtime_baseline_match, admission unpaused,68 holds.
Gate m2-gate-20260908T121706917079Z-5900a7525f unchanged.
The first two frozen members remain NEEDS_REVIEW/strict_verified0; the successful
historical recovery did not replace them. This is not current whole-cohort progress.

M3's engineering/deployment/real-publication and duplicate-ingress requirements
are verified within the stated bounds. The final closeout above resolves the
previously open ingress check. Never overwrite or retranslate the valid real output.

M2 remains incomplete: download/extraction formal publication lacks a complete,
matching usable source; prior no-start torrents retain failed-hash/backoff policy.
Strict frozen Gate is not accepted; do not backfill or wait for20 in this session.
No code/config/deployment/Gate change during this audit; formal additions this audit0.
