# M2 reviewed extraction deployment and partial formal acceptance — 2026-09-12

## Current result, 20:39 UTC

**M2 NOT COMPLETE.** One genuinely missing-valid-TC target now has a new valid
351-cue formal Traditional Chinese ASS. Its final NAS path, full parse, unchanged
hard QC, manifest binding, original ASS snapshot, target checksum, and preserved
valid/unrelated sidecars have been checked. This is not an isolated fixture.

The complete multi-artifact acceptance is still open: the same transaction's
Simplified Chinese file was subsequently found at a different language-labelled
path with exactly the published SHA. The immutable publication manifest still
names the earlier path. Both actual files parse and pass hard QC, but the rename
owner/lineage and production idempotent replay remain unverified. Do not edit the
old manifest, republish the valid TC, or call the whole chain accepted.

Counts refer to one obligation, not additive deliveries: AI0 new in this M2
work; retained-download-to-valid-TC1; extraction-to-valid-TC1; unique new valid
TC targets1. Full-chain acceptance0 pending the items above. The canonical TC
path already contained an invalid subtitle; it was backed up and replaced by the
normal versioned publisher. Thus new effective TC delivery1 is not a claim that
the TC pathname itself was newly created. The independently accepted M3 AI1 is
historical and is not counted again.

## Actual safe deployment and recovery

- Worker runtime **88598848ebf2efaf945a9128c320ba188074f3b6**.
- Worker image **sha256:3bffd64b670ab3ef5b209d2134f81c4719468250afb553c7b8a6d6bea852b22c**.
- Worker source revision **083a8e8bb941e951f927b0ed3213dfd3ac364a244649fd07074b7a334beb1ac6**.
- WebUI remains **175a02a7bad46e0b6fa2372c59f39e8dd272911e**; image
  sha256:b53781c82ac58315c7190175f507047534d9372bf6d978242416d62486d0bd69.
- Configuration remains sha256:11198e9e15070bc5667dcc53e93adc99dbb73366eaa603e27b227bab57453bb3;
  Decision schema1. No model, QC threshold, routing or WebUI change.

Owned admission drain reached natural idle; no job was killed or another owner's
pause released. Existing safe-update-stack deployment **20260912T201652Z-1272366**
completed with exit0, retained online backups/migration rehearsal/health/mailbox
and image/source checks. Backup: `/work/deployment_backups/20260912T201652Z-1272366/`.
Its existing quality-sidecar migration reported2457 directories,0 matched/migrated/
deleted/quarantined reports; this was part of the unchanged deployment procedure,
not an additional manual library sweep. No source video was moved or modified.

Candidate489 related tests reused; **actual-image637 tests PASS**, including the
frozen-observation/recovery suites required at deployment. Fresh isolated breakers
**7/7 PASS**, no Production fault-injection resources affected. Earlier real
four-container transaction-loss/resume and real-media fixture evidence remains
in `M2_REVIEWED_EXTRACTION_RECOVERY_20260912.md`; suites overlap and are not summed.

Reconciliation **m2-recon-reviewed-extraction-20260912T2010**, receipt
`/logs/m2-reconciliation-m2-recon-reviewed-extraction-20260912T2010.json`, SHA
**f8786476abe5a9334c7e9319eac53502012cb5f98c50abac7970c167bb0569fc**.
All70 old holds retained;1 unexplained Queue identity removal preserved as
PENDING_REVIEW, total71 holds.6817 recoverable and7257 retained-state identities
are not delivered subtitles. Preservation stays **UNPROVEN**. Old receipts,
immutable ancestor612943..., Gate members and backups remain unchanged.
Recovery record **m2breakerrec_8efedc66631247ba83ca22859ab6b95c**;
`/logs/m2-production-recovery-20260912T202738098492Z-9ab6b95c.json`.
Controlled recovery and orchestrator exited0, runtime ARMED, admission open.
Existing recovery lane preserved; no historical bulk dispatch/reset was issued.

## Actual formal canary

Anonymous obligation **m2dl_f0133acfdbb11284e318**, exact member3583:2.
Already complete project torrent **c1102b70ebe9c0676e82498f39ffaa8735b933e1** reused;
no add/delete/redownload action. Original request82ace3... and all failed-source
exclusions remain; it was not reused as permission for another source.

Reviewed request **c6e77fd1d8f2ee7736a535bc4dc40074afe94773afb12536a36f7fe83f605412**
went through existing `mikan.requeue_extract` command
**cmd_17f2ce72c23406eaa99e327d**. A bounded four-sample observer captured:

1. Command queued, extraction originally replaced/attempt1.
2. Command completed, exact extraction queued without resetting attempt history.
3. Worker **51:ee1aa634677d** actually claimed at **1789244941.4115996**,
   statusrunning/attempt2, persisted progress update **1789244943.0922942** and
   lease **1789245841.4115996**.
4. Actual extraction ended **1789244948.1727529**, status success, exact member's
   pending status completed, complete scoped result and one immutable receipt.

This is autonomous Worker processing after a controlled single-job request, not
a direct test invocation of claim/publication. It does not by itself establish a
subsequent new AI claim under885988. Prior5e736 AI claim/checkpoint evidence remains
separate. No Gate slot was backfilled with this historical obligation.

Formal manifest:
`/work/official_subtitle_versions/86fab60f4dcd3f79/1789244947170323794/manifest.json`.
TC output SHA **1b9ce7b062e20f2d82dfe22504cc23993ab2fb1661f641885b6f9f07943e99a3**,
351cues, parse/hard QC PASS, original warnings retained, no wording/QC relaxation.
Original parallel ASS snapshots/transformation and reviewed recovery binding
are persisted. Invalid prior TC SHA b5cc7946f2f61196326d68e484c018ca6951868d0de917a1173a51fc74bfe091
survives in verified `previous-0.ass`;17 other pre-existing sidecars are unchanged.
Final target video SHA **1a514508c2dc1682ca98f65b4f7c494a899cc754d7f68b4f6ac1088bdf93c2af**
rechecked through the existing NAS mount; no video mutation.

SC output SHA **05d56833719ed20c79f8369de042014153c4ead7578ae329c513d0b6374b32a3**,
351cues, parse/hard QC PASS. The manifest's `.zh.ass` path is absent; one unique
same-hash file exists as `.2.简体中文.zh.ass`. This content evidence is not proof of
the rename actor. A `sublangid` container exists, but its task-window log was NOT
obtained; do not claim it caused the rename or change its settings.

The server verifier passed source identity/pending/receipt/backup checks before
failing on the missing SC manifest path. Its full failure log is retained; no
success report was fabricated. The later final-file verification ran on Windows
through NAS mounts and is explicitly labelled that way. It independently proves
both final-file hashes/QC and target checksum, but the qB source share is not
exported through that access path, so its later download checksum is UNKNOWN,
not a missing server file. Prior server-bound download identity is
1fe8edad0311243e046b0568c3414df7c3adac2d09068b65a94e168bd6aec033.

## Frozen Gate disposition

The previously partial Gate **m2-gate-20260912T183251418961Z-5f0cbe7f8a** actually
settled automatically at **2026-09-12T20:16:01.257804Z**, before this handoff:
20/20, **5 strict /15 needs review /FAIL**. Its20 complete immutable members were
sealed and rechecked unchanged; its status remains SETTLED, not rewritten as
invalidated or passed. Automatic report SHA
**efea89fa89681dfc8a105b61d752209e9bb59877ee04e7cae9a98a407c4407bf**.
Earlier Gate125703 remains20/20,6strict/14review/FAIL, report SHA f137826... unchanged.

New actual-runtime Gate **m2-gate-20260912T202742079916Z-9d08eceb76**, start
**2026-09-12T20:27:42.079916Z**, baseline **m2-guardrail-v1:4d664e39de091fb35df67e9b**,
initialized **0/20**; one bounded closeout read foundACTIVE2enrolled/2settled.
No wait for20, member replacement, result transfer or mixed-version statistics.
M2_PRODUCTION_ACCEPTED and99%/99.9% remain unclaimed.

## Evidence and next safe action

Root `/logs/m2-reviewed-extraction-deploy-20260912T2010/` contains full deployment,
actual-image tests, fresh breaker receipt, reconciliation, recovery, original
members, automatic-report hash checks and runtime-closeout-1789245006243154742.json.
`formal-canary/` contains baseline/request/immutable-command response,
bounded-observation-1789244950101269267.json, inventory-1789245086724185088.json,
and **final-files-readonly-1789245519655081100.json**. `canary-verify.log` retains the
missing-SC-path failure; `canary-runtime-window.log` is the one bounded Worker log
window. No repeated full Docker logs or full Queue polling.

Chrome terminal control failed with **Debugger unattached**. One reset/rebind
also failed; the attempted `sublangid` log command was not submitted and no file
was created. Runtime was already safely recovered; do not rerun the handoff.

Next: restore the existing authorized server terminal connection, inspect only
the exact rename window, establish a durable final-path/manifest disposition
without rewriting historical evidence or replacing valid outputs; then submit
the prepared exact replay through the existing mailbox and verify queued0/no
duplicate download or publish. Recheck current download checksum server-side.
Modify code only for a reproduced in-scope defect, with targeted tests and a new
safe handoff only if the actual runtime baseline changes. No M3/M4, mass recovery,
other-service setting change, full scan or waiting for the next20 Gate.
