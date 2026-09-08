# M3 acceptance evidence audit — 2026-09-08 13:19 UTC

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
| Idempotency | Actual completed-subtitle predicate true, terminal reprocess predicate refuses, final hashes unchanged; restart fixture proves no duplicate send | Predicate + isolated lifecycle proof; a duplicate filesystem-event end-to-end run against this formal target was NOT performed |
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

M3's engineering/deployment/real-publication evidence above is verified within
the stated bounds. Do not equate the predicate check with a duplicate-event E2E
test or claim complete milestone acceptance without resolving its required scope.
Next safe step: verify the existing duplicate-ingress contract in isolation against
representative completed artifacts; never overwrite or retranslate the valid real output.

M2 remains incomplete: download/extraction formal publication lacks a complete,
matching usable source; prior no-start torrents retain failed-hash/backoff policy.
Strict frozen Gate is not accepted; do not backfill or wait for20 in this session.
No code/config/deployment/Gate change during this audit; formal additions this audit0.
