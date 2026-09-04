# M2 Project Goal

完成 M2：建立可持久化、可解釋、可測試的字幕與音訊來源分析及自動策略決策系統。

M0/M1 已完成。本次工作必須建立在現有 Event-first Watcher、SQLite WAL Job Store、Stage checkpoint、retry、timeout、restart/resume 與來源唯讀保護之上。

## 開始前必須

1. 閱讀 `AGENTS.md`、`PLAN.md`、`docs/PROJECT_GOAL.md`、`docs/TEST_RESULTS_M1.md`、`docs/STATE_MACHINE.md`、`docs/RECOVERY_POLICY.md`。
2. 確認目前 Git 工作目錄乾淨，並確認基準包含 M1 驗收版本。
3. 執行現有回歸測試，建立 M2 修改前基準。
4. 盤點目前字幕軌、音訊軌、語言判斷、字幕抽取、OpenCC、Whisper 與翻譯入口。
5. 優先擴充現有實作，不建立功能重複的平行 Pipeline。

## 本次目標只包含

- 媒體字幕軌與音訊軌 inventory。
- 字幕與音訊語言辨識。
- 字幕完整度與可用性評分。
- 多字幕及多音軌候選排序。
- Forced subtitle / signs-only / songs-only 風險辨識。
- 最佳來源選擇。
- 處理策略決策。
- Confidence、reason、evidence 與候選結果持久化。
- 與 M1 Job Store、checkpoint、resume、structured log 整合。

## 本次不得開始

- 改善實際翻譯文筆。
- Translation Memory。
- 完整 Glossary 系統。
- 完整字幕 QC。
- 自動翻譯修復。
- WebUI 大幅改造。
- 更換整套 ASR 或翻譯模型。
- 全面重寫現有 Pipeline。

## 必須支援的正式策略

1. `USE_EXISTING_ZH_TW`：已有完整且可用的台灣繁體中文字幕，直接使用。
2. `NORMALIZE_ZH_HANT`：已有繁體中文，但需要台灣用語或格式正規化。
3. `CONVERT_ZH_CN`：已有完整簡體中文字幕，交由後續 OpenCC 處理。
4. `TRANSLATE_JA_SUBTITLE`：已有完整可用的日文對話字幕，後續直接翻譯，不執行 Whisper。
5. `ASR_JA_AUDIO`：沒有可用字幕，但有可信的日文音訊，後續才允許執行 Whisper。
6. `NEEDS_REVIEW`：有可處理候選，但無法可靠自動選擇。
7. `UNSUPPORTED`：不存在任何受支援且可處理的字幕或音訊來源。

## 字幕候選分析

至少必須包含：

- track index。
- codec。
- container language tag。
- normalized language tag。
- title。
- default flag。
- forced flag。
- hearing-impaired flag，如可取得。
- subtitle event count。
- 首尾字幕時間。
- 與影片時長的 coverage。
- 有效時間碼比例。
- 空字幕比例。
- 語言偵測結果。
- 語言 confidence。
- 中文繁簡判斷。
- 日文字符比例。
- signs-only / forced-track probability。
- dialogue completeness score。
- extraction error。

## 音訊候選分析

至少必須包含：

- track index。
- codec。
- container language tag。
- normalized language tag。
- title。
- default flag。
- channels。
- sample rate。
- duration。
- 語言辨識結果，如目前架構可安全支援。
- language confidence。
- extraction or probing error。

語言判斷不得只相信 container metadata，必須結合 container language tag、track title、抽樣字幕內容、Unicode script 分布、中文繁簡判定，以及必要時的額外語言分類器。

必須正規化常見 language tag：

- `zh-TW`、`zh-Hant`、`cht`、`chi`、`zho`。
- `zh-CN`、`zh-Hans`、`chs`。
- `ja`、`jpn`。
- `und`、`unknown` 或缺失值。

## 決策規則

優先順序：

完整可用的 zh-TW / zh-Hant → 完整可用的 zh-CN / zh-Hans → 完整可用的日文對話字幕 → 日文音訊 → `NEEDS_REVIEW` 或 `UNSUPPORTED`。

Metadata default flag 不得無條件高於實際字幕品質。

不得把以下字幕誤判為完整中文字幕：

- 只有招牌。
- 只有歌詞。
- 只有 OP/ED。
- forced subtitle。
- 極少數字幕事件。
- 字幕覆蓋率明顯不足。
- 時間軸損壞。
- 空字幕。
- 內容語言與 metadata 不一致。

## Decision Record

所有決策必須產生結構化 Decision Record，至少包含：

- `job_id`。
- `decision_version`。
- `strategy`。
- selected subtitle track。
- selected audio track。
- `confidence`。
- `reason_code`。
- `evidence`。
- 所有候選及分數。
- 未選擇其他候選的原因。
- 建立時間。
- analyzer version。
- input identity。
- source fingerprint。

Decision Record 必須持久化到 Job Store 或相關 Artifact，且在 restart/resume 後可重用。

只有在以下情況才允許重新分析：

- 尚無有效 checkpoint。
- 輸入 identity 或 fingerprint 改變。
- analyzer version 或 decision schema 明確需要失效。
- 上一次分析資料損壞或不完整。
- 操作者明確要求重新分析。

Confidence 門檻初始採用：

- `confidence >= 0.90`：自動接受策略。
- `confidence 0.60–0.90`：執行額外檢查後再次決策。
- `confidence < 0.60`：`NEEDS_REVIEW`。

門檻必須可設定，不得散落硬編碼。本次不要求證明門檻已統計校準到 99%，但必須保留後續使用 Eval Dataset 校準的能力。

決策必須 deterministic：對相同輸入、相同 analyzer version、相同設定，重複執行必須得到相同策略、候選排序及 `reason_code`，除非明確使用非確定性模型並已記錄其版本與結果。

禁止在 M2 決策階段直接執行 Whisper。只有策略已確定為 `ASR_JA_AUDIO` 後，後續 Stage 才可以執行 Whisper。

## 必須建立並通過的測試

1. 完整 zh-TW 字幕選擇 `USE_EXISTING_ZH_TW`。
2. 完整 zh-CN 字幕選擇 `CONVERT_ZH_CN`。
3. 完整日文字幕選擇 `TRANSLATE_JA_SUBTITLE`。
4. 無字幕且有日文音訊選擇 `ASR_JA_AUDIO`。
5. zh-TW 與日文字幕同時存在時優先選擇 zh-TW。
6. zh-CN 與日文字幕同時存在時優先選擇 zh-CN。
7. forced 中文字幕不會覆蓋完整日文字幕。
8. signs-only 字幕不會被判定為完整中文字幕。
9. default flag 不會讓低品質字幕勝過完整字幕。
10. metadata 標記為中文但實際內容為日文時可以識別衝突。
11. metadata 缺失時可依內容辨識字幕語言。
12. 繁簡中文標記與內容可以正規化。
13. 多個相近候選會執行額外檢查。
14. 低信心且無法可靠決定時進入 `NEEDS_REVIEW`。
15. 完全無支援來源時進入 `UNSUPPORTED`。
16. 所有候選分數、reason 與 evidence 均被保存。
17. restart 後可重用有效 Decision checkpoint。
18. 輸入 fingerprint 改變後舊 Decision 會失效。
19. 相同輸入重複分析產生 deterministic 結果。
20. 決策階段不會啟動 Whisper。
21. M1 的 12 項驗收測試仍全部通過。
22. 完整既有整合回歸測試仍全部通過。

至少建立以下代表性 fixtures：

- 完整繁中字幕。
- 完整簡中字幕。
- 完整日文字幕。
- 中日多字幕。
- forced/signs-only 字幕。
- metadata 錯誤字幕。
- metadata 缺失字幕。
- 多日文音訊。
- 無可用來源。
- 時間軸或字幕內容異常。

## 交付文件

- `docs/M2_DESIGN.md`
- `docs/SOURCE_SELECTION.md`
- `docs/DECISION_SCHEMA.md`
- `docs/CONFIDENCE_POLICY.md`
- `docs/TEST_RESULTS_M2.md`
- 更新 `PLAN.md`
- 必要的設定範例及 schema migration 說明。

## 完成前必須

- 執行 M2 驗收測試。
- 執行全部 M1 驗收測試。
- 執行完整回歸測試。
- 執行 Python compile 與 `git diff --check`。
- 檢查完整 diff。
- 確認沒有無關重構。
- 確認來源影片仍保持唯讀。
- 確認沒有私人 IP、路徑、端口、憑證或測試媒體進入 commit。
- 列出實際測試命令、通過數量及結果。
- 列出尚未處理的 M2 風險。

## 停止條件

只有當字幕及音訊候選分析、策略決策、Confidence、reason/evidence 持久化、restart/resume 與上述測試全部通過後才停止。

不要自行開始 M3 的 ASR fallback、翻譯 fallback、GPU scheduler、Translation Memory、完整 QC 或 WebUI 改造。
