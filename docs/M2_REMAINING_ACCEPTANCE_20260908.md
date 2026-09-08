# M2 remaining acceptance — 2026-09-08

## Verified autonomous formal delivery

- Anonymous obligation: `aiobl_17110f05e900b186c5dcf3b24103d05bbd1b08636ef526f0773696d818a75be7`.
- Existing deployed Worker `65a9670fb1cbeb459939df60755366bd32589e63`
  automatically produced one new traditional-Chinese delivery (three language ASS artifacts).
- The read-only verifier reopened the final files: manifest/delivery/publication
  semantics, parsing and existing hard QC PASS. Source SHA256 before/after the
  verification matches the checksum recorded during processing. Latest pipeline
  state is COMPLETED; the pre-deployment database had no verified manifest for
  this target. The verifier performed zero downloads and zero publications.
- Evidence: `/logs/m2-remaining-acceptance-20260908/delivery-final-verify-1788843960408162885.json`.
- This is **AI delivery**, not download/extraction acceptance or a Gate PASS.
  A duplicate-trigger production replay was not performed by this verifier.

## Reproduced final ASR evidence defect

- Existing failure: `aiobl_e7c26b6e56ea6b0076a572536f2993cd844a0160a8fd99470f99f231507061d2`.
  The shared final Japanese fragment gate correctly rejected the transcript,
  but did not persist that rejection before fail-closed cache cleanup. The inline
  fragment repair also bypassed the existing durable one-shot repair claim.
  Review consequently showed checkpoint unavailable and repair_attempted=false.
- Fix commit: `de6759499eaa0576dc9b9affd2f842109781d4c5`.
  Persist final rejection bound to the actual SRT checksum. Retain existing
  confidence/repair history only for matching bytes. Active-source fragment
  repair requires the existing durable budget; unavailable/consumed evidence
  fails closed. No QC, model, routing or Decision Schema change.
- Baseline reproduction: `fragment-rejection-before.log` FAILED at the expected
  repair_attempted assertion. The first broader invocation named a nonexistent
  test module; that invocation is retained as failed, not called a PASS.
- Correct focused suite: **209 PASS** (`fragment-rejection-regression.log`).
- Related guardrail/recovery/checkpoint suite: **312 PASS** and actual isolated
  container crash/restart PASS, source/audio unchanged, no ASR re-decode.
  `/logs/m2-asr-postprocess-isolated-20260908T050742138969Z/`.
  This tested candidate source mounted read-only in the prior image, not a new
  deployed image. Source revision: `1b9c2038283362c7102f25ce24f17eb08a02e3dfe76bbc58dd95a7e699f2e37f`.

## Deployment boundary

The candidate is committed and pushed; actual deployment and controlled recovery
are not claimed. Two bounded safe-idle checks returned
`planned_change_work_not_idle`: the preceding real Worker job remains active
with heartbeat. No force-stop, lock deletion or stale relabelling was performed.
At 2026-09-08T05:10:55Z the existing controlled claim helper released only this
attempt's owned admission pause, after verifying ARMED, unchanged baseline/Gate,
no prepared receipt and no deployment start. Server automation remains enabled.
Evidence: `/logs/m2-fragment-evidence-repair-20260908/deferred-closeout.json`.

- Actual Worker remains `65a9670fb1cbeb459939df60755366bd32589e63`;
  WebUI remains `175a02a7bad46e0b6fa2372c59f39e8dd272911e`.
- Breaker ARMED; existing Gate `m2-gate-20260908T042203260424Z-ea2562f0dc`
  and baseline `m2-guardrail-v1:4bd8a3c2eb64004cfd7c6717` retained.
- No new reconciliation record, source holds, Gate or runtime deployment was made.
  The prior 67 holds and old evidence remain in force.
- Next deployment must use a fresh timestamped handoff attempt; do not rerun or
  overwrite the closed attempt's immutable pause/target files. Recheck actual
  runtime and safe idle, preserve all current differences/holds, then use
  safe-update-stack, actual-image verification and formal controlled recovery.
  The prepared helper scripts are not evidence of a completed deployment.

## Still open

- A genuinely missing-subtitle target verified through the download → extraction
  → matching → formal-publication chain. The AI delivery above does not substitute.
- Existing external-source/backoff and uncertain identity obligations, including
  the retained 67 logical holds. Nothing is silently removed from failure totals.
- Production verification of the new final-ASR evidence behavior after safe deployment.
- Strict frozen 20-job acceptance remains server-side; no waiting, cohort replacement,
  acceptance/99-percent claim, or M3 work was performed in this closeout.
