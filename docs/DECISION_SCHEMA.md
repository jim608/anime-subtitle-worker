# M2 Source Decision Schema

## 版本

目前 analyzer 契約常數為：

| 常數 | 值 | 變更時機 |
| --- | --- | --- |
| `ANALYZER_VERSION` | `m2-source-analyzer-v1` | 評分、排序、語言或風險演算法語意改變。 |
| `DECISION_SCHEMA_VERSION` | `1` | Canonical decision 欄位或型別不相容變更。 |
| `DECISION_VERSION` | `m2-source-decision-v1` | 策略或決策語意改變。 |

SQLite 將 schema version 正規化成文字保存；呼叫端可以傳入正整數或非空版本字串，但 reuse 比較採用正規化後的精確值。

## 兩層資料契約

M2 有兩個相關但不同的 mapping：

1. **Analyzer decision**：`SourceDecision.to_dict()` 產生的純決策資料。
2. **Persisted decision record**：Job Store 將權威 context、hash、ID 與 stage checkpoint 加在 analyzer decision 外圍及其 stored JSON 中。

呼叫端不得自行提供 `decision_id` 或 `stage_attempt_id` 到 analyzer decision；這兩個欄位由 persistence layer 擁有。

## Analyzer decision 頂層

`SourceDecision.to_dict()` 固定輸出以下八個欄位：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `strategy` | string | 七種支援策略之一。 |
| `confidence` | number `[0,1]` | 本次整體決策信心；不是 production 成功機率。 |
| `reason_code` | non-empty string | 可機器處理的主要決策原因。 |
| `evidence` | non-empty object | Policy、版本、inventory 完整性、選定候選及 additional checks。 |
| `selected_subtitle_track` | object 或 null | 選中字幕 candidate 的完整 canonical mapping。 |
| `selected_audio_track` | object 或 null | 選中音訊 candidate 的完整 canonical mapping。 |
| `candidates` | array | 所有字幕及音訊分析結果，包含未選候選。 |
| `unselected_reasons` | array | 穩定排序的 `{candidate, reasons}` 紀錄。 |

物件內部的 `SourceDecision.selected_*_track` 是方便路由的整數 index；只有 canonical `to_dict()` 會把它展開成完整 candidate mapping。持久化與 restart materialization 必須使用 `to_dict()` 格式，不能只保存 index。

### Strategy 與 selected mapping invariant

| Strategy | `selected_subtitle_track` | `selected_audio_track` |
| --- | --- | --- |
| `USE_EXISTING_ZH_TW` | 必須存在 | null |
| `NORMALIZE_ZH_HANT` | 必須存在 | null |
| `CONVERT_ZH_CN` | 必須存在 | null |
| `TRANSLATE_JA_SUBTITLE` | 必須存在 | null |
| `ASR_JA_AUDIO` | null | 必須存在 |
| `NEEDS_REVIEW` | null | null |
| `UNSUPPORTED` | null | null |

Analyzer-produced decisions 只會把一個 candidate 標成 `selected=true`；review/unsupported 不會標記選中來源。

## 共用 candidate 欄位

每個 `candidates[]` 至少具有：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `kind` | `subtitle` 或 `audio` | Candidate 類型。 |
| `index` | integer | 穩定 track index；sidecar 字幕可以是負數。 |
| `score` | finite number `[0,1]` | 排序用 candidate score。 |
| `selected` | boolean | 是否為決策選定來源。 |
| `codec` | string | 正規化 codec。 |
| `source_kind` | string | `sidecar` 或 `embedded`。 |
| `source_reference` | string | 可重建的相對 sidecar 名稱或 `stream:<index>`。 |
| `container_language_tag` | string | Inventory 原始語言 tag。 |
| `normalized_language_tag` | string | 正規化後 tag。 |
| `title` | string | Track/sidecar title evidence。 |
| `eligible` | boolean | 是否通過 auto-selection 硬門檻。 |
| `processable` | boolean | 是否仍可能經修正、重試或 review 處理。 |
| `rejection_reasons` | array[string] | 未過硬門檻或去重的原因。 |
| `evidence` | object | Candidate 級別的可驗證 evidence。 |

### Subtitle candidate 擴充欄位

字幕另包含：

- `source_size`: non-negative integer 或 null。
- `source_mtime_ns`: non-negative integer 或 null。
- `source_sha256`: 空字串或 lowercase 64-hex；sidecar materialization 時必須有效。
- `content_sha256`: 空字串或 lowercase 64-hex semantic event hash；可執行選定字幕必須有效。
- `default`、`forced`、`hearing_impaired`。
- `event_count`、`first_timestamp_seconds`、`last_timestamp_seconds`。
- `coverage_ratio`、`valid_timing_ratio`、`empty_event_ratio`。
- `detected_language`、`language_confidence`、`chinese_script`。
- `traditional_marker_count`、`simplified_marker_count`、`japanese_character_ratio`。
- `forced_probability`、`signs_only_probability`、`songs_only_probability`。
- `dialogue_completeness_score`。
- `extraction_error`。

字幕 evidence 至少描述 metadata/title/content language、內容衝突、script distribution、timeline ratios 與 special-track risk。若為 semantic duplicate，evidence 另含代表 candidate 及相同 `content_sha256`。

### Audio candidate 擴充欄位

音訊另包含：

- `default`、`commentary`。
- `channels`、`sample_rate`、`duration_seconds`、`duration_ratio`。
- `detected_language`、`language_confidence`、`detection_source`。
- `probing_error`。

音訊 evidence 包含 metadata language、title language、提供的 detection language、metadata/content conflict 與 duration ratio。

## `evidence` 契約

所有決策都有以下 base evidence：

- `analyzer_version`
- `decision_schema_version`
- `decision_version`
- `policy`：完整 `AnalyzerThresholds` snapshot
- `subtitle_inventory_complete`
- `audio_inventory_complete`
- `candidate_counts`
- `priority_order`
- `asr_invoked`，在 analyzer 中固定為 `false`

成功 auto-route 時另有 `selected_candidate`、`selected_language` 與 `additional_checks`。Review 會保存 `provisional_candidate`（若存在）及 additional checks。Unsupported 仍保存 inventory 與「不需要 additional checks」的證據。

`additional_checks` 格式：

| 欄位 | 說明 |
| --- | --- |
| `required` | 是否因中信心或 close tie 而需要額外檢查。 |
| `performed` | 具名 check、passed 結果及必要的 runner-up evidence。 |
| `result` | `passed`、`insufficient` 或 `not_required`。 |
| `precheck_confidence` | 額外檢查前信心。 |
| `postcheck_confidence` | 額外檢查後、實際用於決策的信心。 |

## `unselected_reasons`

此欄位是 list，不是 object：

```json
[
  {
    "candidate": "subtitle:2",
    "reasons": ["lower_language_priority"]
  },
  {
    "candidate": "audio:1",
    "reasons": ["usable_subtitle_precedes_audio"]
  }
]
```

Candidate key 固定為 `<kind>:<index>`。若 candidate 沒有硬 rejection，analyzer 仍會記錄 `lower_language_priority`、`lower_candidate_score`、`usable_subtitle_precedes_audio` 或 `decision_not_auto_accepted`，避免未選原因空白。

## Canonical JSON 與 hash

Analyzer canonical JSON 規則為：

- UTF-8，保留非 ASCII 字元。
- Object keys 排序。
- 無額外空白 separator。
- 拒絕 NaN/Infinity。
- 計算值輸出前最多正規化至六位小數。
- Set 類輸入先依 canonical JSON 排序。

`decision_sha256()` 是純 analyzer decision 的 canonical JSON SHA-256。`fingerprint_inputs()` 則對排序後的完整 candidate input inventory、媒體長度及 inventory completeness 做 SHA-256；兩者用途不可互換。現行 Worker 持久化使用的是呼叫端建立的廉價 `SourceInputIdentity.candidate_fingerprint`，讓 reuse lookup 可以在詳細 probe/extraction 前完成；`fingerprint_inputs()` 不會自動寫入 decision。

SQLite persistence 會再次 canonicalize stored decision，注入 `candidate_results_sha256`，並計算資料庫權威的 `decision_sha256`。因此 persisted hash 可能涵蓋比純 analyzer `decision_sha256()` 更多的 context 欄位。

## Persisted context 與資料庫紀錄

Job Store 注入並驗證：

- `job_id`
- `input_identity`
- `media_revision`
- `source_fingerprint`
- `analyzer_version`
- `decision_schema_version`
- `decision_version`
- `config_fingerprint`
- `candidate_fingerprint`
- `candidate_results_sha256`
- `created_at`

資料庫 row 另保存 opaque `decision_id`、原始 `stage_attempt_id`、`input_identity_sha256`、`decision_sha256` 及 optional idempotency key。`decision_json`、identity JSON 與其 SHA 必須一致；策略、信心、reason、candidate results hash 也會在讀取時重新驗證。

## Stage checkpoint

每個已綁定的 running `SUBTITLE_DETECTION` attempt 使用下列 checkpoint reference：

| 欄位 | 值 |
| --- | --- |
| `kind` | `source_decision` |
| `decision_id` | Immutable decision ID |
| `decision_sha256` | Persisted decision JSON SHA-256 |
| `input_identity_sha256` | Canonical input identity SHA-256 |
| `analyzer_version` | 實際 analyzer implementation 版本 |
| `decision_schema_version` | 實際 schema 版本 |

Attempt output 只保存 `no_artifact_required=true` 與相同 checkpoint evidence，並標記 `outputs_verified=true`。這個 stage 的 durable output 是決策紀錄，不是字幕檔案。

## Reuse 與 integrity failure

Reuse 必須精確匹配完整 input identity、media/source/config/candidate fingerprints 及三種版本。常見 miss reason 包含：

- `source_decision_missing`
- `source_decision_input_identity_changed`
- `source_decision_media_revision_changed`
- `source_decision_source_fingerprint_changed`
- `source_decision_analyzer_version_changed`
- `source_decision_schema_version_changed`
- `source_decision_version_changed`
- `source_decision_config_changed`
- `source_decision_candidate_fingerprint_changed`

Stored JSON 無法解析、不 canonical、identity/decision/candidate hash 不符或 context 自相矛盾時，不得 reuse。Service 會走 miss 或直接拒絕衝突，不會修補或信任部分紀錄。

## 相容性規則

- 新增會影響讀取方的必填欄位時，提升 `DECISION_SCHEMA_VERSION`。
- 改變七策略語意、排序或 route semantics 時，提升 `DECISION_VERSION` 和／或 `ANALYZER_VERSION`。
- 只調整設定 threshold 也會改變 `config_fingerprint`，因此舊決策自動 invalidated。
- 不得在未同步 reader、migration、tests 與部署設定時手動改版本字串。
- Schema 驗證通過只代表紀錄結構可信，不代表最終字幕品質或 99%／99.9% SLO 已達成。
