# M2 canonical output/source identity recovery — 2026-09-23

Status: controlled recovery completed; M2 Production acceptance remains **NO**.
All incident, deployment, test, reconciliation and observation artifacts are retained
under `/logs/m2-source-id-20260923/`; the original read-only one-attempt trace is
under `/logs/laya-20260923/strict-trip-diagnostic.json`.

At 2026-09-23T00:36:23Z, runtime `cf82fc27d28b0ed69c33ce95ef0f72e0492a53bf`
tripped `incorrect_completion` during strict precommit validation of a converted
zh-CN subtitle. The attempted publication had a valid hash-bound manifest,
hard QC and source checksum, but the source inventory treated the newly published
canonical `<video>.繁體中文.zh-TW.ass` as a fresh input sidecar. This invalidated
the earlier source decision and its dependent hallucination/history evidence.
The completion transaction rolled back to `NEEDS_REVIEW`; the physical output,
original source and evidence were preserved. This is not a strict completion or
new accepted delivery. The old Gate's 8 fixed members were preserved and later
marked `INVALIDATED_BY_RUNTIME_CHANGE`, not relabelled PASS or backfilled.

The narrow repair recognizes only the canonical or legacy zh-TW publication
path when the existing manifest validates exact output bytes and publication
semantics for `converted_zh_cn` or eligible `adopted_zh_tw` normalization.
An unverified receipt, unrelated sidecar or independent source revision remains
visible to the source decision. Input/inventory policy versions were bumped to
prevent reuse of a decision made under the old identity rule. Strict QC and
Gate predicates were not changed. The same Worker deployment also contains the
previously tested, bounded ASS top/bottom geometry repair; neither repair
lowers hard QC for ambiguous overlap.

Source-mounted UNRAID isolation: 175 related tests PASS, including real
parser/OpenCC/QC/publication, idempotency, source identity before/after and
independent-source drift rejection. The deployed image
`sha256:aa24150b1c37cc2681bc3722477fc6a09c4ea23f511fd00fbbccc35f653d472d`
passed 278 related tests, an isolated container restart cycle and fresh 7/7
breaker fault injection. Worker SHA is
`12599631ba7f79d7e7b26b8b42b18993b8f213db`; WebUI SHA remains
`ef5c20e699f1e5a639eb1207f02ce254274c2a73`.
The existing safe updater used owned reconciliation hold
`m2-recon-canonical-output-20260923`, online backup
`20260923T013342Z-3699432`, `PRESERVE_EXISTING_BACKUPS=1`, and no
whole-DB rollback. All 57 preceding backups and the new backup are retained.
The 106 prior source holds remain, and the exact rejected incident is a new
107th logical hold. No other Queue difference was silently absorbed.

Formal controlled recovery returned Breaker **ARMED** and resumed claims at
2026-09-23T01:38:00Z. A new frozen Gate,
`m2-gate-20260923T013758379145Z-5c980b0249`, began at
2026-09-23T01:37:58.379145Z, baseline recorded in
`/logs/m2-source-id-20260923/recovery-closeout.json`. A single bounded
post-recovery snapshot at approximately 01:39Z showed this Gate `ACTIVE 1/20`,
`0` settled/strict, runtime `ARMED`, and a newly claimed task
`m2ai_01e4abf51d98db247113` in actual preflight/audio/ASR stages with a
verified `SUBTITLE_DETECTION` checkpoint and fresh ASR heartbeat. This proves
admission and Stage progress, **not** a correct terminal result or sustained
terminal-to-next-claim cycle.

Still required for the same Goal: at least 24 hours of server-side autonomous
observation from the new Gate start (earliest window end
2026-09-24T01:37:58.379145Z), all first 20 frozen eligible outcomes without
replacement, multiple terminal-to-next-claim continuations, at least two new
strict formal subtitles on the new baseline, and final download/extraction and
AI publication evidence without double counting. Do not claim M2 accepted or
restart the Gate for document changes. Laya remains diagnostic-only with
automatic inference disabled after its separate model-quality rejection; it
must not hold subtitle admission.
