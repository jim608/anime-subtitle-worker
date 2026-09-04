# M2 Confidence Policy

## 信心不是成功率

M2 的 `score` 與 `confidence` 是 deterministic policy values，用於排序、門檻與 fail-closed 路由。它們不是經統計校準的機率，也不能直接解讀成「字幕有 90% 正確」或「平台已達 99.9% 成功率」。

來源選擇通過後，後續 ASR、翻譯、OpenCC、字幕結構及發佈仍各自需要驗證。Production SLO 必須由獨立驗收 corpus 與實際工作資料證明。

## 預設門檻

`AnalyzerThresholds` 的完整預設值如下：

| Policy field | 預設 | 用途 |
| --- | ---: | --- |
| `auto_accept_confidence` | `0.90` | 自動執行策略的最低最終信心。 |
| `review_confidence` | `0.60` | 中信心區下界及候選語言最低門檻。 |
| `min_subtitle_events` | `20` | 最低字幕事件數。 |
| `min_subtitle_coverage_ratio` | `0.60` | 最低字幕時間覆蓋。 |
| `min_valid_timing_ratio` | `0.90` | 最低有效 timing 比例。 |
| `max_empty_event_ratio` | `0.20` | 最高空事件比例。 |
| `max_forced_probability` | `0.60` | Forced-track 風險上限。 |
| `max_signs_only_probability` | `0.65` | Signs-only 風險上限。 |
| `max_songs_only_probability` | `0.70` | Songs-only 風險上限。 |
| `min_dialogue_completeness_score` | `0.68` | 最低對話完整度。 |
| `min_content_characters` | `12` | 進行內容語言判斷的最低有效文字量。 |
| `min_cjk_characters` | `8` | 泛中文內容 evidence 最低漢字量。 |
| `min_kana_characters` | `2` | 日文內容 evidence 最低 kana 數。 |
| `min_japanese_character_ratio` | `0.08` | Kana 在 han+kana 中的最低比例。 |
| `min_audio_duration_ratio` | `0.60` | 音訊軌相對媒體長度下限。 |
| `close_candidate_score_margin` | `0.025` | 觸發 close-candidate checks 的分數差。 |
| `exact_tie_score_epsilon` | `0.002` | 認定具有實質分數差的下限。 |
| `metadata_conflict_penalty` | `0.06` | Metadata 與內容矛盾的信心扣分。 |
| `japanese_audio_tag_confidence` | `0.91` | 純日文 container tag 的基準信心。 |

所有 ratio/probability 必須是 finite `[0,1]`；`review_confidence < auto_accept_confidence`；各 count 門檻必須為正；`exact_tie_score_epsilon` 不得大於 close margin。

## AppConfig 對應

目前 YAML/AppConfig 只公開以下政策旋鈕：

| Config key | AnalyzerThresholds field |
| --- | --- |
| `source_analyzer_high_confidence` | `auto_accept_confidence` |
| `source_analyzer_low_confidence` | `review_confidence` |
| `source_analyzer_min_dialogue_completeness_score` | `min_dialogue_completeness_score` |
| `source_analyzer_min_subtitle_coverage_ratio` | `min_subtitle_coverage_ratio` |
| `source_analyzer_tie_margin` | `close_candidate_score_margin` |

`exact_tie_score_epsilon` 會取 analyzer 預設 `0.002` 與設定 tie margin 的較小值。其他欄位目前使用 `AnalyzerThresholds` 固定預設；若日後公開新設定，必須納入 config fingerprint、驗證與 regression tests。

版本設定 `source_analyzer_version`、`source_decision_schema_version`、`source_decision_version` 不直接改變公式，但會納入 config/reuse contract。`source_analyzer_enabled` 只切換 M2 或舊流程；它不是信心門檻。

## 字幕 metric

### Timeline ratios

```text
coverage_ratio = clamp((last_timestamp - first_timestamp) / media_duration)
valid_timing_ratio = clamp(valid_timing_count / event_count)
empty_event_ratio = clamp(empty_event_count / event_count)
event_score = clamp(event_count / min_subtitle_events)
nonempty_score = 1 - empty_event_ratio
```

無有效 media duration、首末時間缺失或末時間不大於首時間時，coverage 為 `0`。Timing/empty 原始 metrics 缺失會另外產生 rejection，不能用計算出的 `0` 掩蓋缺證據。

### Special-track risk

- `forced_probability`：forced flag 為 `1.0`；title 命中 forced marker 至少 `0.95`。
- `signs_only_probability`：signs title 至少 `0.95`、forced 至少 `0.85`、事件過少至少 `0.78`、極短 coverage 至少 `0.72`。
- `songs_only_probability`：songs/lyrics/karaoke/OP/ED title 至少 `0.95`；音符密度 evidence 至少 `0.82`。

### Dialogue completeness

```text
base = 0.30*coverage
     + 0.25*valid_timing
     + 0.20*nonempty
     + 0.25*event_score

risk_penalty = max(
    0.65*forced_probability,
    0.48*signs_only_probability,
    0.48*songs_only_probability,
)

dialogue_completeness = clamp(base * (1 - risk_penalty))
```

### Subtitle candidate score

```text
candidate_score = clamp(
    0.54*dialogue_completeness
  + 0.35*language_confidence
  + 0.10*valid_timing_ratio
  + (0.01 if default else 0)
)
```

Candidate score 只用於同優先層候選排序；它不能繞過 eligibility hard gates。

### Subtitle decision confidence

```text
decision_confidence = clamp(
    0.52*language_confidence
  + 0.48*dialogue_completeness
)
```

Default flag、candidate score 與 track index 不直接提高整體 decision confidence。

## 字幕語言信心

字幕先分析 sample text 的 kana、han、繁體 marker 與簡體 marker，再和 normalized metadata/title 結合：

- 足夠 kana 且日文比例達標時，以約 `0.90` 起算日文內容信心。
- 至少兩個繁／簡 marker 且明顯多於另一 variant 時，以約 `0.92` 起算對應中文 script 信心。
- 只有足夠漢字但無法分繁簡時，泛中文內容信心為 `0.58`；可信 variant metadata 可提升到 `0.72`。
- 文字不足、只靠支援語言 tag 時基準信心為 `0.45`，不足以 auto-select。
- Metadata/content conflict 扣除 `metadata_conflict_penalty`。

所有結果 clamp 到 `[0,1]`。完整 marker 與 alias 規則屬 analyzer version 的一部分，修改時必須使舊 checkpoint invalidated。

## 音訊分數與信心

若 inventory 已提供 language detection，analyzer 使用其 normalized language/confidence，並依相符 metadata/title 小幅調整；衝突會扣分。若沒有預先偵測：

- 日文 container tag 使用 `japanese_audio_tag_confidence`，title 也相符時最多再加 `0.05`。
- 只有 Japanese title 時是中信心 evidence，不能自動通過。
- 非日文 tag 的弱 metadata confidence 為 `0.55`；未知為 `0`。

```text
channel_score = clamp(channels / 2)

audio_candidate_score = clamp(
    0.65*language_confidence
  + 0.25*duration_ratio
  + 0.07*channel_score
  + (0.03 if default else 0)
  - (0.45 if commentary else 0)
)

weighted = clamp(0.60*language_confidence + 0.40*duration_ratio)
audio_decision_confidence = min(language_confidence, weighted)
```

最後的 `min` 很重要：完整 duration 只能證明軌道可用，不能把弱語言 evidence 推升到 auto-accept。

## 信心區間與決策

假設 `low=0.60`、`high=0.90`：

| 區間／狀態 | 行為 |
| --- | --- |
| 最終信心 `>= high`、候選 eligible、additional checks 非 insufficient | 執行對應字幕或音訊策略。 |
| `low <= confidence < high` | 記錄 additional checks；目前 policy 不會僅因 checks 通過就提高數值，未達 high 因而進 `NEEDS_REVIEW`。 |
| 語言信心 `< low` | Candidate 不 eligible；若仍可處理，整體進 `NEEDS_REVIEW`。 |
| Inventory 不完整 | `NEEDS_REVIEW`，避免把 probe/extraction failure 當成無來源。 |
| Inventory 完整且沒有 eligible/processable candidate | `UNSUPPORTED`，信心 `0.99` 表示對「不支援」分類的政策信心，不是字幕品質。 |

Auto-route 一律同時要求 eligibility 與最終信心，不是只看一個總分。

## Additional checks

當 base confidence 位於 `[low, high)` 或 eligible runner-up 與第一名 score 差 `<= close_candidate_score_margin` 時，`additional_checks.required=true`。

字幕 checks 記錄：

- `content_language_confidence >= high`
- `dialogue_completeness >= high`
- `valid_timing_ratio >= min_valid_timing_ratio`
- 最大 special-track risk 是否低於三個 risk 上限中的最小值
- Close runner-up 是否可由較高語言優先序或 `score_margin > exact_tie_score_epsilon` 消除

音訊 checks 記錄：

- `language_confidence >= high`
- `duration_ratio >= min_audio_duration_ratio`
- 非 commentary
- Close runner-up 是否有 `score_margin > exact_tie_score_epsilon`

在中信心區，任何 performed check 失敗都標記 `insufficient`；即使 checks 全過，未達 high 的 base confidence 也不會被升級，所以仍 review。對高信心 close tie，若沒有可驗證的 priority/實質 margin，postcheck confidence 最多被壓到 `high - 0.01` 並 review。

相同非空 `content_sha256` 的字幕會先去重，非代表 candidate 變成 ineligible，因此不參與 runner-up close-tie 計算。

## Fail-closed 校準規則

- 降低 high threshold 或 eligibility 門檻會直接擴大自動處理面，必須以獨立 corpus 驗證 false completion 不增加。
- 提高 low threshold 可能增加 review/unsupported；需觀察 autonomy rate，但不得以放寬錯誤來源選擇換取自動化率。
- Tie margin、語言 marker 或 risk heuristic 變更應提升 analyzer version，並更新 config fingerprint。
- 測試 fixture 通過只證明規則可重現，不證明 confidence 已校準。
- 正式宣稱達標前，仍需依 release gate 完成未參與調整的 Eligible Inputs、獨立 fault injection，以及 production rolling-window 統計。
