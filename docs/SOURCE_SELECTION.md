# M2 字幕與音訊來源選擇

## 決策原則

M2 優先使用可驗證、完整的既有字幕，只有在沒有 eligible 字幕時才考慮日文音訊。這項優先序不受「強制 AI」操作影響：強制重新處理不能跳過更可靠的字幕來源，也不能讓 ASR 優先於合格字幕。

Analyzer 只使用 inventory 已觀察到的 facts，不自行讀取媒體，也不執行 Whisper。`ASR_JA_AUDIO` 是後續路由結果，不代表決策階段已轉錄音訊。

## Candidate inventory

### 字幕 candidate

字幕輸入包含：

- 穩定 `track_index`；sidecar 使用負數索引，內嵌字幕使用 ffprobe stream index。
- `source_kind` 與 `source_reference`；目前可執行來源為 `sidecar` 相對參照或 `embedded` 的 `stream:<index>`。
- sidecar 的 `source_size`、`source_mtime_ns`、raw `source_sha256`。
- 跨 SRT/ASS 格式正規化後事件內容的 semantic `content_sha256`。
- codec、container language tag、title、default/forced/hearing-impaired flags。
- event count、首末時間、有效 timing 數、空事件數及 bounded sample text。
- 抽取錯誤。

`source_*` identity 欄位供 restart materialization 重驗，不參與內容品質分數。`content_sha256` 用於識別語意相同的字幕：若 sidecar 和 embedded 內容相同，只保留一個代表 candidate；在可用性相同時偏好 sidecar，其餘標記 `duplicate_subtitle_content`，不會形成假的 close tie。

### 音訊 candidate

音訊輸入包含 stream index、codec、語言 tag、title、default/commentary flags、channels、sample rate、duration，以及可選的預先偵測語言、信心與偵測來源。目前 inventory 不呼叫音訊語言模型，通常提供的是容器 metadata evidence。

## 語言 evidence

語言 tag 會先正規化，例如 `jpn -> ja`、`cht -> zh-hant`、`chs -> zh-hans`、`chi/zho -> zh`。字幕內容會移除常見 ASS override、HTML tag、SRT timing/序號後再分析：

- Kana 數量與 kana/(han+kana) 比率用於日文 evidence。
- 固定的繁體與簡體 marker 分布用於 `zh-hant`／`zh-hans` evidence。
- `zh-TW`、`zh-Hant`、`zh-CN`、`zh-Hans` metadata/title 可進一步決定 variant。
- Metadata 與內容矛盾時會留下 `metadata_content_conflict` 並扣除信心；title 與內容不一致也會留下衝突 evidence，而不是盲信名稱或 tag。
- 文字量不足時，metadata 只能提供較弱 evidence，不能單獨保證 auto-accept。

音訊優先採用已提供的 deterministic language result；否則使用容器 tag/title。只有 title 顯示 Japanese 的弱 evidence 不足以自動接受；commentary 會被拒絕。

## 字幕完整性與風險

每個字幕 candidate 會計算：

- `coverage_ratio`：有效首末字幕時間跨度／媒體長度。
- `valid_timing_ratio`：有效 timing events／全部 events。
- `empty_event_ratio`：空 events／全部 events。
- `forced_probability`：forced flag 或 title marker。
- `signs_only_probability`：signs title、forced、事件過少或覆蓋過短等 evidence。
- `songs_only_probability`：songs/lyrics/karaoke/OP/ED title 或音符密度。
- `dialogue_completeness_score`：coverage、timing、非空比例、事件量扣除特殊軌風險後的固定加權分數。

以下任一狀況會使字幕 candidate 不具 auto-select 資格：

- 抽取失敗或 codec 不支援。
- 零事件／事件過少。
- timing 或 empty metrics 缺失。
- coverage、有效 timing、空事件比例未過門檻。
- forced、signs-only、songs-only 風險達門檻。
- 對話完整度不足。
- 語言不支援、未知或語言信心低於 review 門檻。
- 與另一 candidate 有相同非空 semantic content hash，且不是代表 candidate。

「不 eligible」不必然等同 unsupported。仍可能修正、重抽或需要人工判斷的 candidate 會保留 `processable=true`，使整體決策 fail closed 到 `NEEDS_REVIEW`。

## 排序規則

### 字幕

只有 eligible 字幕能被選中，排序依序為：

1. 語言優先序：`zh-tw`、`zh-hant`、`zh-cn/zh-hans`、`ja`。
2. Candidate score 由高到低。
3. Event count 由高到低。
4. Track index、codec、title 的穩定 tie-break。

語言優先序刻意高於小幅 score 差異。例如完整繁體中文字幕優先於完整日文字幕，完整日文字幕又優先於 ASR。

### 音訊

只有在沒有 eligible 字幕時才檢查音訊。Eligible audio 必須有足夠日文 evidence、足夠 duration、不是 commentary，且沒有 probe error。排序優先日文、較高 score，再以 stream index/codec/title 穩定決勝。

## 七種策略

| Strategy | 選擇條件 | Worker 後續行為 |
| --- | --- | --- |
| `USE_EXISTING_ZH_TW` | 高信心且完整的 `zh-tw` 字幕。 | 重驗並沿用為繁中來源；後續仍受輸出與結構 QC。 |
| `NORMALIZE_ZH_HANT` | 高信心且完整的通用繁體 `zh-hant` 字幕。 | Materialize 後正規化成臺灣繁體，再驗證輸出。 |
| `CONVERT_ZH_CN` | 高信心且完整的簡體 `zh-cn/zh-hans` 字幕。 | Materialize 後轉換成臺灣繁體，再驗證輸出。 |
| `TRANSLATE_JA_SUBTITLE` | 高信心且完整的日文對話字幕。 | 使用既有字幕時間軸作為日文來源，再進翻譯；不對音訊執行 ASR。 |
| `ASR_JA_AUDIO` | 無 eligible 字幕，但有高信心、完整、非 commentary 的日文音訊軌。 | 重驗指定 stream 後，才由下游抽音訊及執行 ASR。 |
| `NEEDS_REVIEW` | Inventory 不完整、有可處理但未過門檻的來源、中信心或無法消除的平手。 | 保存 evidence/checkpoint、建立 source-selection review 並停止自動路由。 |
| `UNSUPPORTED` | Inventory 完整，且確定沒有支援或可處理的字幕／音訊來源。 | 停止本任務；不得標記 `COMPLETED`。 |

## Additional checks 與平手

當 provisional winner 的決策信心位於 low/high 門檻之間，或 runner-up 分數落在 tie margin 內時，decision 的 `evidence.additional_checks` 會記錄額外檢查。

字幕會重查內容語言信心、對話完整度、timing integrity、特殊軌風險，並用語言優先序或實質分數差消除 close tie。音訊會重查日文信心、duration、非 commentary 與實質分數差。若結果仍不足或最終信心未達 high threshold，策略必須是 `NEEDS_REVIEW`。

Semantic hash 相同的重複字幕會先去重，因此不會因「同一字幕存在兩種格式」被誤判成無法決勝。

## 執行前重驗

Persisted decision 不是直接信任的路徑字串。Adapter 會：

- 重建目前 source candidate fingerprint，要求與 persisted fingerprint 完全一致。
- Sidecar：依相對參照解析，限制在媒體目錄範圍內，並比對大小、mtime、raw SHA-256 與 semantic content SHA-256。
- Embedded subtitle：重新抽取指定 stream，再驗語言與 legacy structural QC。
- Audio：重新 ffprobe 指定 stream，驗 index、`source_reference`、codec、channels、日文 tag 與非 commentary。

任何缺失、歧義、越界、內容改變或 persisted/current evidence 衝突都會 raise 並停止，不會靜默選另一軌。

## 主要 reason/rejection codes

成功選擇會使用 `complete_zh_tw_subtitle`、`complete_zh_hant_subtitle_requires_normalization`、`complete_zh_cn_subtitle`、`complete_japanese_dialogue_subtitle` 或 `trusted_japanese_audio_no_usable_subtitle`。

整體無法自動決策常見為 `subtitle_selection_ambiguous`、`audio_selection_ambiguous`、`candidate_analysis_inconclusive`、`source_inventory_incomplete` 或 `no_supported_subtitle_or_audio_source`。每個未選 candidate 另保留具體 rejection reasons；完整清單以 persisted `candidates[].rejection_reasons` 為準。

## 限制

- 此規則集是保守 heuristic，尚未完成 production corpus calibration。
- 短字幕、混合語言、特殊字形、錯誤 metadata 或不完整 duration 可能需要 review。
- 圖形字幕與未支援的文字 codec 不會走直接字幕策略。
- `ASR_JA_AUDIO` 的來源決策主要依 metadata；ASR 品質與最終語言正確性仍由下游驗證。
- 通過來源選擇不代表翻譯或最終字幕已通過 QC，也不是 99%／99.9% 成功率證明。
