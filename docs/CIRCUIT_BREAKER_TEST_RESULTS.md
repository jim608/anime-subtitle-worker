# M2 Circuit Breaker Test Results

## Result boundary

Recorded on 2026-09-04.

- Local focused harness: **PASS (7/7 breaker cases)**
- Focused unit and CLI contract tests: **PASS (6/6 tests)**
- Frozen cohort/schema/event-journal tests: **PASS (24/24 tests)**
- Recovery/replay tests: **PASS (11/11 tests)**
- Complete Worker regression: **OK (1,649 run; 1 conditional skip)**
- Historical pre-repair server image/runtime execution: **PASS (7/7 breaker cases)**
- Historical server timestamped full log validation: **PASS**
- Historical runtime status after initialization: **ARMED**
- Production source media or formal outputs affected by fault injection: **No**

The server run below was executed before the frozen-cohort automation repair.
It proves that historical guardrail runtime only. Its observation Gate is
invalidated as `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`; the result does
not initialize or pass a replacement 20-job Gate and does not establish
Production acceptance. A repaired deployment must generate a fresh timestamped
7/7 result before arming its replacement Gate.

## Isolated fault matrix

| Breaker | Injected condition | Local isolated result | Server image/runtime result |
| --- | --- | --- | --- |
| `source_mutation` | Failed source-integrity observation with changed checksum evidence | PASS | PASS |
| `duplicate_publish` | Failed publish observation with duplicate-publication evidence | PASS | PASS |
| `output_parse_failure` | Failed delivery-verification parse observation | PASS | PASS |
| `incorrect_completion` | Reported success without required verified delivery evidence | PASS | PASS |
| `repeated_oom` | Three consecutive OOM-classified failures | PASS | PASS |
| `repeated_identical_stage_failure` | Three consecutive identical stage/failure signatures | PASS | PASS |
| `insufficient_disk_space` | Isolated disk-capacity hook returns less than the configured floor | PASS | PASS |

Both repeated-failure thresholds were `3`. The first two observations remained
closed and the third opened the expected breaker.

## Assertions applied to every case

Each of the seven local cases and all seven server-isolated cases passed all
ten assertions:

| Assertion | Local result | Server-isolated result |
| --- | --- | --- |
| Production claim admission wrapper called | 7/7 PASS | 7/7 PASS |
| New job claim stopped | 7/7 PASS | 7/7 PASS |
| Queue state preserved | 7/7 PASS | 7/7 PASS |
| Valid checkpoint preserved | 7/7 PASS | 7/7 PASS |
| Running job not interrupted | 7/7 PASS | 7/7 PASS |
| No false `COMPLETED` state | 7/7 PASS | 7/7 PASS |
| Expected reason and non-empty evidence persisted | 7/7 PASS | 7/7 PASS |
| Sandbox-only safe recovery verified | 7/7 PASS | 7/7 PASS |
| Synthetic source hash unchanged | 7/7 PASS | 7/7 PASS |
| Output fixture untouched | 7/7 PASS | 7/7 PASS |

The claim assertion calls `main._m2_server_canary_admit_new_job`, the same
fail-closed wrapper used immediately before production claims. The queue behind
that boundary is synthetic and isolated; no production queue backend is opened.

## Historical server validation evidence

- Run ID: `m2-guardrail-fi-20260904T105817974649Z-b7176039`
- Worker source revision: `b8986e794d3cb84bdcc831fbb53d19dfe8275358c37529fe1d9375ccd6e1fd3d`
- Started: `2026-09-04T10:58:17.974636Z`
- Finished: `2026-09-04T10:58:18.758425Z`
- Result digest: `sha256:0b5f747c66eb514a1927daf33cec985c8235f7f27f73572a838ad838b8bb4c90`
- Full event-log digest: `sha256:371c300b231fdc42efa0203533d7473d07344eebd236d973839770abe10e674a`
- Historical runtime gate initialization: `ARMED`, baseline `m2-guardrail-v1:276fdef781528ba2059c114e`, initial progress `0/20`; subsequently invalidated as `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`

## Commands and focused results

```text
python -m unittest -v test_m2_guardrail_fault_injection.py
Ran 6 tests ... OK

python m2_guardrail_fault_injection.py --log-dir <server-log-directory>
breaker_tests_passed=7, breaker_tests_total=7, status=PASS,
production_resources_affected=false

python m2_guardrail_runtime.py arm <bounded-runtime-evidence>
status=ARMED, breaker_tests_passed=7, initial_gate_progress=0/20

python m2_production_observation.py --config <runtime-config>
milestone_status=M2_GUARDRAILS_ARMED, status=ARMED, gate_progress=0

python -m py_compile m2_guardrail_fault_injection.py test_m2_guardrail_fault_injection.py
PASS

git diff --check -- m2_guardrail_fault_injection.py test_m2_guardrail_fault_injection.py
PASS
```

Successful CLI output contains only the bounded status, pass count, run ID,
log location, and production-impact flag. Per-case checks, reason evidence, and
tracebacks remain in `events.jsonl`; the machine-readable complete result is
stored in `result.json` under the same timestamped run directory.

## Remaining unverified

The following evidence remains explicitly pending:

- Fresh repaired-image server validation, runtime arming, and the replacement
  Gate's bounded initial `0/20` evidence. Local tests cover no-backfill
  selection, complete per-job evidence, exactly-once reporting, recovery, and
  `INVALIDATED_BY_RUNTIME_CHANGE`; they do not substitute for live deployment.
- The replacement frozen first-20 cohort outcome. This closeout initializes it
  but does not wait for its 20 jobs or claim Production acceptance.
- Live production trip recovery, including a controlled runtime reload after a real cause is remediated.
- Full 100-input M2 release gate and later rolling-production SLO evidence.
- Any measured production autonomy-rate claim.

No production media or output was used for the local fault suite. No production
observation milestone or autonomy-rate conclusion is asserted by this report.
