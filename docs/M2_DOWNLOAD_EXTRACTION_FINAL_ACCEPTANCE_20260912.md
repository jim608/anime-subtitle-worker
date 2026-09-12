# M2 download/extraction acceptance and frozen Gate disposition — final bounded closeout

## Result at 2026-09-12 20:57 UTC

The requested **download/extraction formal acceptance and frozen Gate result
disposition are complete**. This does **not** accept the overall M2 Production
milestone: the latest completed frozen Gate remains FAIL, and71 logical holds
retain UNKNOWN/UNPROVEN history. No M2_PRODUCTION_ACCEPTED or99%/99.9% claim.

Unique new valid Traditional Chinese target: **1**, anonymous obligation
**m2dl_f0133acfdbb11284e318**. AI0 new deliveries; download-path1 and extraction-path1
describe the same target, not two deliveries. Two actual language artifacts
(TC/SC,351cues each) passed final parse and unchanged hard QC. The independently
completed M3 AI1 remains separate historical acceptance, not a substitute.

## Completion evidence matrix

All paths below are under `/logs/m2-reviewed-extraction-deploy-20260912T2010/`
unless an absolute path or another document is given.

| Requirement | Authoritative evidence and result |
| --- | --- |
| Actual deployed/runtime identity | `recovery-closeout.json`, `actual-runtime-before-recovery.json`, final `runtime-closeout-1789246394901631647.json`: Worker88598848ebf2efaf945a9128c320ba188074f3b6, image3bffd64b..., WebUI175a02a7...; ARMED, baseline matches. Documentation-only commits did not deploy. |
| Real missing-valid-TC historical target | `formal-canary/baseline.json`: exact target had no valid TC/AI; all prior sidecars and their validation were captured before the request. No subtitle was removed to create a test gap. |
| Complete retained download and trusted match | Same baseline and immutable reviewed event bind exact managed torrentc1102...,100%/amount_left0, file size/path mapping, original member3583:2, source/target identities and trusted match checks. No torrent add/delete/redownload. |
| Actual Worker processing, not merely queued | `formal-canary/bounded-observation-1789244950101269267.json` captures command→queued→running→success, exact claim1789244941.4115996, worker51:ee1aa634677d, progress update1789244943.0922942, finish1789244948.1727529 and exact pendingcompleted. Attempts1→2, one authorized reopen; old history retained. |
| Real extraction, language/episode/parse/QC | Exact scoped extraction result, stream3TC/stream2SC diagnostics, real ffmpeg output, original failed ASS snapshots and normalized lineage are in the immutable event/manifest. `formal-canary/verify-1789246297869603050.json` independently re-reads both final files with351cues and hardQC PASS; soft CPS/line/duration warnings were not relaxed. |
| Atomic formal publication, old valid-output safety | Manifest `/work/official_subtitle_versions/86fab60f4dcd3f79/1789244947170323794/manifest.json`, SHA8627b7a2338fac1ea04a48182195ee33a7652e95e8cae2217bfc9c5e344a25e5. Invalid prior canonicalTC has verified previous-0.ass;17 other old sidecars unchanged. Source videos were not modified. |
| Complete final-path lineage | `formal-canary/artifact-location-evidence.json` binds original manifest SHA, exact SC old/new paths and SHA, actual sublangid container/image, timestamped rename log and attestation hashes. Original manifest remains unchanged as the publication-time receipt. Both current files are directly re-read, not accepted solely from a renamed filename or HTTP success. |
| Source checksums unchanged | Final server verification recomputes target1a514508c2dc1682ca98f65b4f7c494a899cc754d7f68b4f6ac1088bdf93c2af and download1fe8edad0311243e046b0568c3414df7c3adac2d09068b65a94e168bd6aec033, including exact identity checks. The earlier Windows-only download-share limitation is resolved by this server check, not reinterpreted as a successful Windows read. |
| Production idempotent replay | Actual mailboxcmd_6866c13d677a9a13d4253283 completed with `already_recorded`, queued0. `formal-canary/replay-baseline.json` plus final verify prove unchanged extraction attempts/start/finish/result, exactly one immutable receipt, identical file hashes/sizes/mtimes and identical manifest history. Original qB hash/addition/completion identity retained; no duplicate claim/download/publication. |
| Automatic continuation independent of Codex | `automatic-ai-checkpoint-1789246481392211119.json` and `final-worker-processes.log`: distinct AI m2ai_a15e28200b22c4e44088 auto-claimed1789246145.8595045, actual process alive, transcription heartbeat1789246365.6843815, verified SUBTITLE_DETECTION checkpointa500a719f8c70393c2e96f9f518645aa54365c7206a3f5f5a01addadcbafd0f8. No manual AI dispatch/wake; new AI delivery0, no wait for translation. Existing Mikan worker/queue/checkpoint/backoff continues. |
| Isolation and preservation | New reconciliationf8786476... retains all70 prior holds and adds1 unexplained Queue removal as PENDING_REVIEW, total71.6817 recoverable/7257 retained-state identities, not deliveries. Original failed receipts, immutable ancestor612943..., backups and UNPROVEN unchanged. No direct SQLite/lock/latch edits or global DB restore. |
| Safe deployment and focused tests | Existing safe-update-stack20260912T201652Z-1272366, natural idle/owned pause, backup/rehearsal/health/mailbox/parity and controlled recovery exit0. Actual-image637 related tests/fresh isolated7breakers PASS. Prior real four-container rollback/restart/lease/idempotency and real-media fixture proofs are retained; counts overlap, not summed. |
| Frozen Gate result disposition | Old183251 automatically SETTLED20/20 before deployment,5strict/15review/FAIL; all20members and reportefea89fa... rechecked unchanged. Earlier1257036/14FAIL/f137826... unchanged. New202742 starts20:27:42.079916Z, baseline4d664e39de091fb35df67e9b,0/20 initialization; final bounded readACTIVE6/6. No backfill/version mixing, no wait for20. Exact historical canary-related AI obligations were checked absent from new Gate membership. |
| Documents, logs and milestone scope | This audit, PLAN, remaining-acceptance, production-observation, deployment closeout and work/M2_RECOVERY_CONTINUE.txt updated. Complete logs are on the server. M3 evidence directly reused, no M3/M4 work or new architecture. |

## The SC rename was explained, not hidden

`sublangid` container00f6722bfcb3519ca8876fdf16fb59979b8ea29704e684ec90a4fcea5ae4bf1a
renamed the already-published SC at **2026-09-12T20:29:40.224774360Z** from `.zh.ass`
to `.2.简体中文.zh.ass`, avoiding a different-content pre-existing subtitle.
The old and current artifact SHA is
**05d56833719ed20c79f8369de042014153c4ead7578ae329c513d0b6374b32a3**.
TC stayed at its canonical path with SHA
**1b9ce7b062e20f2d82dfe22504cc23993ab2fb1661f641885b6f9f07943e99a3**.

The supplemental location evidence is a factual, hash-pinned verification record,
not a new runtime resolver, changed Decision Schema or modified old receipt.
The existing official manifest is a publication/backup receipt; current runtime
source/valid-output checks discover the actual sidecars rather than consuming it
as a mutable live-path index. No other service setting was changed. Future
unexplained moves or content changes must still fail verification; this exact
evidence does not authorize arbitrary path substitutions.

The earlier `canary-verify.log` FileNotFound failure and partial closeout remain
history. They were not overwritten or silently labelled PASS. The later evidence
resolves that specific missing-path observation through the real actor and file
identity, and the complete server verification/replay then succeeded.

## Boundaries retained after this Goal

- Overall M2 Production is **not accepted**: completed frozen cohorts failed;
  the current cohort is still ACTIVE and will be handled by server automation.
- 71 held/unknown obligations and historical preservation UNPROVEN remain; they
  were neither resolved nor counted as successes by this acceptance.
- Existing source-unavailable/backoff/unsupported obligations remain under their
  prior bounded recovery policies; this is not an all-backlog completion claim.
- No ongoing Codex polling, full Queue/library sweep, Chrome monitoring, or AI/
  Gate20 wait is needed. The temporary terminal will be released after closeout.
