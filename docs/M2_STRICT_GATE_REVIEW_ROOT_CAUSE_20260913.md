# M2 strict Gate review root causes — 2026-09-13

## Scope and status

M2 Production accepted: **NO**. No M3 work. The previous formal download /
extraction acceptance (one TC subtitle, 351 cues) is retained, not repeated or
counted again. No old Gate Job was rerun or media-library audit performed.
The standard first safe deployment ran its existing legacy quality-sidecar
metadata migration check (2457 directories, zero matches/changes); it is not a
second media/Queue investigation and did not publish or remove any artifact.

Old Gate `m2-gate-20260912T183251418961Z-5f0cbe7f8a` remains
**20 / 20, 5 strict verified / 15 NEEDS_REVIEW / FAIL**. Its sealed 20 SQL rows
and all 20 append-only result-event digests were checked against the original
deployment evidence. The report SHA-256 remains
`efea89fa89681dfc8a105b61d752209e9bb59877ee04e7cae9a98a407c4407bf`.

Initial read-only runtime check: Worker
`88598848ebf2efaf945a9128c320ba188074f3b6`, Breaker ARMED, 71 source holds.
The current Gate `m2-gate-20260912T202742079916Z-9d08eceb76` was ACTIVE,
20 enrolled / 18 settled at that single snapshot. Analysis did not mutate it.

## Complete bounded classification

Classification concerns the **first review-producing cause in the original
attempt**, not a claim that every FALSE_REVIEW member is safe to publish.
All seven false primary blockers also have independent downstream hard-QC
failures. Removing the incorrect blocker must expose those failures, not waive
them. No seven-job success forecast or retrospective Gate correction is made.

| Old ordinal | Anonymous obligation suffix | Primary class | Root reason | Additional retained boundary |
| --- | --- | --- | --- | --- |
| 1 | `7885dcc8ce23` | AUTOMATABLE_GAP | `timing_overlap` | 3 overlaps; reject caption, reassess existing trusted JA audio |
| 2 | `d37ea763e0d3` | AUTOMATABLE_GAP | `timing_overlap` | 1 overlap; reject caption, reassess existing trusted JA audio |
| 3 | `ec6461ff9b6f` | TRUE_EXCEPTION | `short_fragment` | ASR repair attempted; 18 unresolved ranges, immutable rejected checkpoint |
| 7 | `ff48b16bccf0` | TRUE_EXCEPTION | `short_fragment` | ASR repair attempted; 22 unresolved ranges, immutable rejected checkpoint |
| 8 | `af7739cdc4ed` | FALSE_REVIEW | `subtitle_selection_ambiguous` | Identical normalized timed multiset; still 3 hard-QC overlaps |
| 9 | `e716918a0317` | AUTOMATABLE_GAP | `timing_overlap` | 45 overlaps; reject caption, reassess existing trusted JA audio |
| 10 | `df8d63fe4502` | FALSE_REVIEW | `subtitle_selection_ambiguous` | Identical normalized timed multiset; still 5 overlaps |
| 11 | `5affc304aa41` | FALSE_REVIEW | `subtitle_selection_ambiguous` | Identical normalized timed multiset; still 2 overlaps |
| 12 | `1c3b9e3185af` | AUTOMATABLE_GAP | `timing_overlap` | 3 overlaps; reject caption, reassess existing trusted JA audio |
| 13 | `b38c3622aec4` | FALSE_REVIEW | `legacy_language_conflict` | Japanese kana ratio 0.745633; downstream too-short / CPS failures remain |
| 14 | `30c2954085b5` | FALSE_REVIEW | `subtitle_selection_ambiguous` | Identical normalized timed multiset; still 2 overlaps |
| 15 | `4fcc0e31d06c` | FALSE_REVIEW | `legacy_language_conflict` | Japanese kana ratio 0.753335; downstream too-short / CPS failures remain |
| 17 | `26068361a0bb` | FALSE_REVIEW | `legacy_language_conflict` | Japanese kana ratio 0.712519; downstream too-short / CPS failures remain |
| 19 | `5af06d2cc7ae` | TRUE_EXCEPTION | `short_fragment` | ASR repair attempted; 12 unresolved ranges, immutable rejected checkpoint |
| 20 | `578421aec1ed` | AUTOMATABLE_GAP | `timing_overlap` | 6 overlaps; reject caption, reassess existing trusted JA audio |

Counts: **TRUE_EXCEPTION 3 / FALSE_REVIEW 7 / AUTOMATABLE_GAP 5**.
The preliminary 8/7/0 classification is superseded, not erased: the five
unrepairable captions are unsafe, but their original complete inventories also
contain trusted JA audio. Rejecting a caption is not evidence that all safe
source routes are exhausted. These five need the existing fallback transition,
not automatic acceptance of unsafe captions or reset ASR repair budgets.
Top primary reasons: structural QC / timing overlap 5, candidate ambiguity 4,
legacy language conflict 3, ASR short fragment after repair 3. The old observation
projection used generic `review_required` / `source_selection_needs_review`;
the classification above joins the exact frozen claim identity to its original
delivery attempt, source decision and stage evidence. It does not overwrite that
projection or infer incidents from failed strict-qualification predicates.

The 16 subtitle artifacts inspected were copied only after their raw SHA-256,
size and mtime matched the old persisted candidate identities. Source artifacts
were read-only and rechecked unchanged. This is historical content verification,
not using a newly calculated video hash to assert unknown historical continuity.
The 71 unrelated held / UNPROVEN sources remain held and unproven.

## Shared defects and minimal corrections

1. Legacy content classification let two or more Chinese marker characters beat
   thousands of Japanese kana. Japanese naturally contains those shared kanji.
   The adapter now supplies its already-materialized, hash-validated JA strategy
   context. The existing classifier accepts that context only with strong,
   dominant kana and no substantial standalone Chinese dialogue layer. A label
   alone cannot admit Chinese, unknown, kanji-only or bilingual Chinese content.
2. The inventory's semantic digest included serialization order. ASS and SRT
   containing the same complete timed cues could appear to be different sources
   and tie. The digest now sorts the exact normalized events while retaining
   duplicate multiplicity, text and timestamps. No fuzzy timing/text equality,
   omitted cue, candidate-specific rule, or source mutation is introduced.
   Cheap source-input identity and inventory contracts advance to v2, preventing
   old v1 source decisions from being reused under the new digest interpretation.
3. Hard QC ran only after selecting/materializing the preferred caption. A hard
   failure went directly to review even when another safe source was available.
   Inventory now records the existing, unchanged structural/hallucination hard
   QC result per caption. The analyzer excludes failed captions and applies its
   existing TC/SC/JA/audio priority to the remaining eligible sources. Missing
   inventory completeness blocks QC-driven audio fallback; untrusted/unknown
   audio remains rejected. Temporary QC I/O errors propagate to retry.
   Source input/inventory contracts advance to v3 and analyzer version to v2;
   the cheap identity binds only resolved hard-QC settings, not unrelated paths.

No model, QC threshold, hallucination rule, Decision Schema, breaker semantics,
download/extraction architecture or runtime configuration was changed. Source
priority remains TC, SC conversion, JA subtitle translation, trusted JA audio,
then genuine review. Existing ASR fragment repair budgets are not reset.

## Verification and deployment boundary

Before-fix targeted reproducer: 7 tests, 4 failures / 1 error demonstrating the
two shared blockers. Initial after-fix local related suite: 146 PASS. Server
isolated related suite: 336 PASS, plus seven exact historical-artifact checks.
An additional cheap-context version regression and planned-change /
reconciliation suite pass locally (48 tests; overlaps are not additive totals).
Final combined server candidate suite: **377 PASS**, including the v2 context,
owned runtime-change and authorized-reconciliation regressions, followed by all
seven exact historical-artifact checks again. Full log
`candidate-validation-final.log`; result `candidate-validation-1789258410399169820.json`.

The three historical Japanese artifacts are correctly identified as JA after
the correction and **still fail their original hard QC**. All four equivalent
candidate pairs have equal corrected semantic digests. Genuine changed text,
timing, cue multiplicity, unknown content and QC failure remain rejected.

First two fixes deployed as `70853f3c8f169d411b7ec9ca73e371d01be3389d` via
safe deployment `20260913T001729Z-3479514`, actual-image 388 tests and fresh 7/7
breakers PASS. Runtime ARMED; controlled reconciliation retained 71 old holds
and conservatively held 30 unexplained deltas (28 removed, one added, one
revision): 101 holds, preservation UNPROVEN. Receipt
`m2-recon-review-roots-20260913T0015`; SHA-256
`3137de5100d7c1f8623198a3b815becaf2a1c16fa94ccd9d80a4cb6122ccb336`.
Prior Gate202742 invalidated only for the actual runtime change, all 20 members
retained. Gate `m2-gate-20260913T002210522287Z-1bbcb0a5f4` started at
`2026-09-13T00:22:10.522287Z`, initially 0/20. Five normal automatic claims and
source checkpoints were observed; this alone does not prove a corrected blocker
was crossed. No new formal output is counted by this Goal.

The additional QC fallback bridge passes server **384 tests**, seven exact
historical artifact checks and five original selected-caption/trusted-audio
transition checks. Safe deployment and runtime validation completed as below;
one real automatic task-crossing is now verified below.
The five transition checks use old hash-pinned captions and recorded metadata,
not old Job reprocessing or Production ASR invocation.

## Final shared fix deployment and real crossing — bounded Goal complete

Worker `b6473598830b909cc37f139d8422c1f13697b43e` was safely deployed from
`70853f3c8f169d411b7ec9ca73e371d01be3389d` by existing safe deployment
`20260913T005315Z-3797885`. Owned admission drain reached idle naturally on its
second check; no process termination or another owner's pause release occurred.
The standard metadata migration check again found zero reports/changes/errors.
Online backups and both earlier Gate histories remain retained.

Actual image `sha256:b67cc921a6464104993307959b43187bae2424353f72e615b2e9ca0cb3b28aca`
passed **395 related tests**, fresh breakers **7/7 PASS**, and runtime source
attestation. WebUI remains `175a02a7bad46e0b6fa2372c59f39e8dd272911e`;
Decision Schema 1 and configuration are unchanged. Controlled reconciliation
`m2-recon-review-qc-20260913T0055` retained all 101 holds, zero new unexplained
deltas, 6722 recoverable and 7323 retained-state identities (not deliveries).
Receipt SHA-256 `8c1d0feaba91dc790bb976fecdc1593dedb10a5e18e7e696aadbeb8dd54b4278`
links the previous `3137de51...` receipt. Preservation stays UNPROVEN.

Gate002210 was invalidated only by this actual runtime change, with membership
preserved. Gate `m2-gate-20260913T005933146136Z-085b1b3991` initialized **0/20** at
`2026-09-13T00:59:33.146136Z`, baseline
`m2-guardrail-v1:458b1a8ba2debd873852eb19`. Breaker ARMED; controlled recovery
resumed claims. First bounded sample01:00:10UTC:0enrolled. Second and final
sample01:02:28UTC:**1enrolled/0settled, ACTIVE**. No further Gate/Queue observation
or fullASR/translation wait was performed.

Real anonymous obligation **`m2ai_9a10797765ee1b50b2a8`**, delivery attempt
`aiatt_5a492ab6d887e9d426448731500b552a7649f03d4c0da171835e8eb1cc8e6962`, was
automatically claimed at `2026-09-13T01:00:31.153550Z`. No manual dispatch/Retry,
old member rerun or easy-job selection occurred. Its otherwise-eligible TC
caption (292cues) remained rejected for `timing_overlap` / `very_long_line`.
Existing source priority selected eligible JA audio stream2, confidence0.91;
Worker entered actual `transcription` with a running heartbeat.

Successful SUBTITLE_DETECTION checkpoint binds this exact delivery attempt to
decision `fe624343264d4cbaaddc41203da03695`, SHA-256
`72b51b7e0ef3c17e349b5c0d724534eaa24b6fb704ed88420ebd687b198d683c`.
Source input identity digest:
`9dd7925f9d650fd1e22702036d356fed0916e2cb29265bf40b2060fe935fab21`.
The final validator requires a subtitle whose ONLY rejection was hardQC,
actual ASR start after decision persistence, and heartbeat after stage start.
An already-ineligible source cannot count as a repaired review transition.

`postfix-qc-observation-1789261348183238138.json` contains the runtime/attempt/
source/decision/checkpoint/stage/heartbeat evidence. Offline verification
`bounded-closeout-1789261455478301553.json` validates that saved evidence without
another Queue read. All101holds, original20FAILmembers and prior Gate membership
remain intact. Existing server admission/observation owns subsequent dispatches;
Codex is no longer needed to watch or click Retry.

Bounded root-cause Goal complete; **M2 Production accepted remains NO**.
The three genuine oldASR cases retain18/22/12 unresolved fragment ranges after
their original bounded repair. Unsafe captions remain unpublishable;101holds
remain isolated, preservation UNPROVEN. This Goal verifies zero additional
formal subtitles, not ASR completion or final QC of the running canary.
Earlier download/extractionTC1 remains separate, not a frozen Gate replacement.

The two-container restart test independently persisted/reused the same immutable
QC-fallback decision with zero candidate-loader calls after restart, unchanged
fixture source and no Production mounts. Evidence:
`/logs/m2-review-qc-restart-20260913T0056/restart-pass.json`.
Earlier isolated test-driver mappingproxy serialization failures are retained
under the `0050` and `0052` test roots; they did not affect Production.
Focused command: `python -m unittest test_m2_source_hard_qc_fallback test_m2_review_source_regressions -q`.
Candidate command: `python -B /candidate/work/m2_review_qc_validation.py` inside
an isolated network-disabled container. Actual-image command and395-test module
list: `work/m2_review_qc_recover.py`, `actual-image-validation.log`.
Final verified classification: `classification-1789260583759300014.json`.
Second deployment, actual-image tests, attestation, fresh fault suite links,
reconciliation and bounded real observations are all retained under
`/logs/m2-review-qc-deploy-20260913T0055/`.

## Full server evidence

Evidence root (fixed task label; individual files contain actual epoch times):
`/logs/m2-review-root-cause-20260913T0300/`.

- `evidence-1789257396264124598.json`: runtime and sealed Gate/event export.
- `decisions-1789257481902935737.json`: all 15 original attempts, decisions and stages.
- `subtitle-analysis-1789257645566037862.json`, `subtitle-evidence/`: hash-pinned historical subtitle artifacts and unchanged-source proof.
- `asr-reviews-1789257912016510863.json`: later mutable ASR review snapshot, NOT used to prove the original repair state.
- `original-asr-checkpoints-1789259147535787221.json`: all three original manifests validated against their exact old attempt interval and artifact hashes; repair_attempted=true, 18/22/12 unresolved ranges.
- `qc-candidate-validation-1789260419408089472.json`, `qc-candidate-validation.log`: 384 tests, seven artifact checks, five fallback transition checks and tested source hashes.
- `isolated-repro-normalized.log`, `isolated-repair/subtitle_remediation/`: exact multiset equality and existing bounded repair refusal evidence.
- `local-targeted-before.log`: original failures retained, not relabelled PASS.
- `candidate-validation.log`, `candidate-validation-1789258181493898538.json`: server 336 tests, seven artifact checks and tested source hashes.

The first artifact diagnostic failed because optional `pysubs2` is not installed;
that failure is retained in `subtitle-evidence.log`. The successful diagnostic
uses the project's actual built-in parser; no dependency installation or runtime
change was made to obtain the evidence.
