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

Durable receipt component work-in-progress: `model_request_state.py` uses the
existing Pipeline Job Store stage model metadata and stage events, not another
Queue. Reservations commit before dispatch, retain UNKNOWN ownership across
reopen, reject replay dispatch, and count model/request attempts across stage
retries. Receipts do not imply output QC success. Twelve isolated component
tests plus 21 existing Pipeline Job Store tests PASS on local Windows Python
(33 total); the concurrency test uses two real spawned processes. Source bytes,
mtime, model metadata and checkpoint fields remain unchanged. The log is stored
on the server share at `/logs/m3-baseline-20260908T070237Z/durable-request-local.log`;
execution was local, not in the production image.

This component is **not wired into Worker or deployed**. Remaining before wiring:
review metadata/event retention and mutation boundaries; bind active stage,
request identity and runtime identity at every translation/repair entry; classify
remote completion versus ambiguous transport failure; integrate resource and
controlled recovery evidence. Then run server isolated integration/restart tests.
No production DB was opened or modified for these component tests.

Follow-up boundary verification: three additional regressions first reproduced
metadata erasure of an unresolved owner, receipt deletion/rewrite, and expansion
of an already established request budget. Additive SQLite triggers now preserve
owned metadata unless its matching result event exists, and prohibit mutation or
deletion of request receipts (including retention cascades). Existing budgets can
be tightened but not replenished by adapter re-creation. Other metadata remains
preserved. Receipt retention needs an explicit future archival policy; it must
not silently discard pending ownership or spent attempts.

Final component suite: 15 request-state tests plus 21 existing Pipeline tests,
**36 PASS locally and in server isolation using the current Worker image**.
Server runs used a read-only candidate mount, no production work/media mount,
and no network. Logs: `durable-request-server.log` (35) and
`durable-request-boundaries-server.log` (36), under the same M3 log root.
This proves the component on the actual image, not live Worker integration.

Integration inspection located all four `_get_translator` call sites: ordinary
Japanese/non-Japanese translation and two targeted repairs. Binding must use the
committed active Pipeline attempt, not `_translator_progress_video` alone (one
path sets it after creating the Translator, and repairs cannot rely on it).
The existing stage-finish/checkpoint methods do not overwrite `model_json`.

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
