# Current Architecture — Baseline vs Implemented M0/M1

## Runtime flow

Docker starts `main.py --auto-watch`. A Watchdog observer receives create, modify, move and close events. The current event path debounces and samples size/mtime, then writes an AI candidate row to `scanner_state.sqlite3`. The foreground loop claims queue rows and launches an isolated Worker. The Worker records legacy stage strings, uses existing cache/checkpoint mechanisms, stages subtitle/media artifacts, and the parent accepts completion only after strict manifest/delivery validation.

The existing SQLite database already uses WAL, busy timeout and short transactions. Existing publication code already stages and validates output before an atomic, rollback-capable replace. Those mechanisms are retained.

## Baseline vs implemented M0/M1

| Baseline gap | Implemented M0/M1 behavior |
|---|---|
| Event stability observations disappeared on restart. | Observations, close evidence, stable samples and retry timing are persisted in the WAL store and restored after restart. |
| Two matching size/mtime samples were treated as sufficient. | Admission now requires the quiet/stability gate and read-open/stat consistency. A no-close quiet event runs a full `ffprobe -count_packets` check outside the Watchdog callback thread; failed probes remain pending. |
| Temporary names such as `.part.mkv` could pass the extension check. | Temporary, staging and incomplete names are rejected before probe or Job creation. |
| Queue history was path-oriented and could mix replacement media. | Jobs use a stable media revision identity including canonical path, size/mtime and filesystem identity where available. |
| Stage state, attempts and checkpoint evidence were not uniformly durable. | Formal states, immutable transitions, attempts, model/timeout/retry metadata, inputs, outputs and checkpoint evidence are durable and auditable. |
| Restart broadly requeued running work. | Startup closes interrupted attempts, validates the latest checkpoint, and requeues only recoverable work within the bounded retry budget. Valid expensive stages remain reusable. |
| Source immutability and formal delivery depended on legacy checks. | The source is opened read-only, captured by identity, checked immediately before publication, and checked again at completion. Manifest-v2 and every required final artifact are rehashed before `COMPLETED`. |
| Idle AI and Mikan cycles could repeatedly recurse large media trees. | Watchdog is authoritative; reconciliation is startup plus low-frequency bounded fallback. Mikan uses persistent incremental coverage, cooldown/due metadata and a bounded root budget instead of re-running all mapped-root `rglob` scans each cycle. |

## Confirmed recurring disk-I/O causes

1. AI reconciliation starts at the media root and performs a recursive walk. An unfinished proof epoch is resumed after a short delay, producing near-continuous batches; Watcher failure previously shortened fallback to the normal watch interval.
2. Mikan missing-media discovery recursively scans many mapped series roots each run. Its long scan can overlap AI reconciliation.
3. Startup catalog/backup maintenance also walks the media tree, but unlike the two loops above it is bounded to startup.

M0 covers both AI reconciliation and Mikan discovery. Watcher degradation does not restore the former five-minute full-library scan, and an unfinished batch is delayed instead of entering a five-second hot loop.

## Baseline evidence

Before M1 edits, the focused suite `test_event_watcher`, `test_scanner_state_recovery`, `test_scanner_state_auto_recovery`, `test_scan_state`, and `test_scanner` passed 140 tests. This is a local regression baseline, not production SLO evidence.

After M0/M1, the 12-case acceptance suite and 680-test focused combined suite pass locally, including a real local Docker restart harness. This is implementation and regression evidence only: no UNRAID deployment, 100-input release corpus, rolling-500 window, or 99%/99.9% SLO is established by these results.
