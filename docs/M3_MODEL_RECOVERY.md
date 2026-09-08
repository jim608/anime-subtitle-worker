# M3 — configured model recovery

M3 is authorized and active. M2 production acceptance remains incomplete;
its frozen cohort, held sources and UNPROVEN preservation claims are unchanged.

## Scope

Build on the existing ASR/translation entry points, model configuration,
resource admission and persistent stage/checkpoint stores. Do not create a
parallel queue or lower output QC to make a fallback appear successful.
Model discovery is evidence of availability, not authorization to use unrelated
models. Model route attempts and their failure/recovery reasons must be bounded
and attributable to the actual processing runtime.

## Initial verified change

The existing translator resolver substituted any sole advertised model for a
missing configured model. It could also collapse primary/fallback into just the
advertised fallback. Two new regressions reproduced both failures on the server
in a read-only candidate mount, without production data or network access.

The implicit sole-model substitution is removed. Configured routes remain in
order when discovery lacks the primary; the existing request failure path then
advances only through that configured chain. Existing unique alias matching is
retained for compatibility and still needs an explicit M3 authorization contract.
This first change does not claim the entire model adapter design is finished.

- Baseline: 118 tests PASS.
- New failing regressions: 2 expected failures before the fix.
- After fix: 121 resource admission/runtime/Worker integration, translator and
  translation checkpoint tests PASS, including composed discovery-to-request
  rejection of the unapproved model and bounded configured-chain exhaustion.
- Server logs: `/logs/m3-baseline-20260908T070237Z/` (`tests.log`,
  `model-route-before.log`, `model-route-after.log`).
- Production runtime/configuration not changed by these isolated tests.

## Request ownership increment (candidate, not deployed)

Two real-thread/event regressions reproduced overlapping calls after a hard
timeout: immediate fallback in the same Translator, and a new Translator instance
calling the same endpoint while the first request was still executing.

The candidate retains endpoint ownership until the actual Future settles, not
just until the caller's timeout expires. Ownership is shared across Translator
instances in the **same process**, acquired atomically, and released by Future
identity so a delayed callback cannot release a newer request. An explicit
`TranslationRequestInFlightError` prevents immediate model fallback and batch
split/context retry. Model unload also participates in endpoint ownership and
does not unload a model under an outstanding request.

255 related tests PASS, including actual blocked executor threads (no network),
cross-instance refusal, completion followed by successful next request, no
split/context bypass, and request/unload mutual exclusion. Fixture requests are
bounded and released. Logs: `request-ownership-before.log` (two reproduced failures)
and `request-ownership-release.log` under `/logs/m3-baseline-20260908T070237Z/`.

This does **not** establish cross-process exclusion, crash/restart ownership,
or remote server cancellation after a socket closes. Those remain required before
M3 deployment/acceptance; the process-local registry is not a durable replacement
for the existing job/checkpoint/recovery stores.

## Remaining engineering and acceptance

- Explicit compatible alias/route identity and durable route-attempt evidence.
- Verify failure-budget persistence when failure occurs before a completed batch:
  current translation checkpoint writes follow successful batches and restore
  `last_model` only alongside completed batches.
- Extend verified process-local request ownership through existing durable
  checkpoint/resource/lease mechanisms. After a crash, lack of local Future is
  not proof that a remote request stopped; preserve unresolved ownership evidence
  and fail closed until the applicable recovery condition is proven.
- Exercise crash/OOM, timeout, exhausted fallback, missing resources and restart,
  keeping valid checkpoints and source/valid-output safety intact.
- Actual-image integration, safe deployment/controlled Gate handoff and bounded
  live evidence remain required. No production completion or M3 acceptance yet.
