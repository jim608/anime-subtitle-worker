# M2：可持久化的來源分析與決策

## 狀態與範圍

M2 在既有 Worker 前加入一層可重現的字幕／音訊來源決策。它負責回答「本次任務應使用哪一個來源，以及下一步採取哪一種策略」，並在任何昂貴模型工作開始前，把結果寫入 SQLite checkpoint。

本里程碑已實作 deterministic analyzer、來源 inventory、SQLite 決策紀錄、精確 reuse/invalidation、選定來源重驗與 Worker 路由。功能旗標預設關閉，尚未以正式驗收 corpus 證明 99% 或 99.9% 自動化率。

M2 不負責：

- 在決策階段執行 Whisper、LLM 或其他機率模型。
- 宣稱字幕翻譯或最終發佈已通過 QC。
- 用來源選擇信心取代後續 ASR、翻譯與輸出 QC。
- 修改或刪除來源影片。

## 核心元件

| 元件 | 責任 |
| --- | --- |
| `source_inventory.py` | 建立廉價 input identity；必要時以 ffprobe/ffmpeg 盤點 sidecar、內嵌字幕與音訊軌。 |
| `source_analyzer.py` | 對已提供的 candidate 資料進行純 deterministic 分析、排序與七策略決策。 |
| `source_analysis_service.py` | 先查可重用 checkpoint，miss 才延遲載入完整 inventory；以穩定 idempotency key 持久化。 |
| `pipeline_state.py` | 保存 immutable source decision、SHA-256、版本與 `SUBTITLE_DETECTION` stage checkpoint。 |
| `source_decision_adapter.py` | 執行前重新驗證 persisted context，materialize 指定來源並接到既有 Worker。 |
| `worker.py` | 在功能開啟時呼叫 M2，依策略進入沿用、正規化、轉換、翻譯、ASR、review 或 unsupported 路徑。 |

詳細欄位與排序規則分別見 [DECISION_SCHEMA.md](DECISION_SCHEMA.md) 與 [SOURCE_SELECTION.md](SOURCE_SELECTION.md)。

## 執行流程

1. Worker 必須已有 durable Job，且目前有一個狀態為 `RUNNING` 的 `SUBTITLE_DETECTION` attempt。
2. `build_source_input_identity` 以 Job 的媒體身分及 source sidecar 的相對路徑、大小、mtime、SHA-256 建立廉價 candidate fingerprint；這一步不 probe 或抽取內嵌串流。
3. Worker 建立 `SourceAnalysisContext`，包含 Job、input identity、media revision、source fingerprint、config fingerprint、廉價 candidate fingerprint，以及 analyzer/schema/decision 版本。
4. Service 先呼叫 `reusable_source_decision`：
   - Exact hit：完全不呼叫詳細 `candidate_loader`，把同一筆 immutable decision 綁到目前 attempt。
   - Miss：才執行完整 inventory，呼叫 `analyze_sources`，再以不含 attempt ID 的穩定 idempotency key 寫入決策。
5. 決策及 checkpoint 成功提交後，adapter 才 materialize 選定字幕或重新 probe 選定音訊軌。
6. Adapter 再驗 candidate fingerprint、來源 identity、語言與結構品質；任何矛盾都停止路由，不沿用不可信 checkpoint。
7. Worker 依七種策略繼續。`ASR_JA_AUDIO` 只代表後續允許對已驗證的日文音訊執行 ASR；analyzer 本身沒有執行 Whisper。

## Deterministic 邊界

對相同 candidate input、完整性旗標、影片長度及 `AnalyzerThresholds`，`analyze_sources` 會產生相同 canonical decision。Analyzer：

- 不讀檔、不執行 subprocess、不存取網路。
- 不使用目前時間或隨機值。
- 不匯入或呼叫 Whisper／transcriber。
- 以固定語言優先序、固定公式和穩定 tie-break 排序。
- 將 JSON key 排序並將計算後浮點數正規化，再用 SHA-256 保護結果。

Inventory 本身可以呼叫 ffprobe/ffmpeg 取得事實；這些 I/O 位於 analyzer 之外。這個分界讓決策邏輯可單獨重播及測試。

## Durable checkpoint 與 idempotency

SQLite 的 `pipeline_source_decisions` 是 append-only 的 immutable 決策紀錄。每筆紀錄具有：

- Job、stage attempt 與 input identity。
- media revision、source fingerprint、config fingerprint、candidate fingerprint。
- analyzer、decision schema、decision 版本。
- canonical decision JSON、decision SHA-256、candidate results SHA-256。
- 穩定 idempotency key 與建立時間。

成功 persist 後，目前 `SUBTITLE_DETECTION` attempt 會取得 `kind=source_decision` checkpoint、decision ID/SHA、input identity SHA 與版本資訊，並標記 `outputs_verified=true`。Restart 後，新的 running attempt 可以引用同一筆決策，不需要重做詳細 inventory。

Service 的 idempotency key 刻意不包含 stage attempt ID，因此「同一份權威 context、不同 restart attempt」仍指向同一筆決策；context 變更時則會得到不同 key。

## Reuse 與 invalidation

只有下列值全部精確相等且 stored JSON/SHA/invariants 都通過驗證時才 reuse：

- `job_id` 與完整 `input_identity`
- `media_revision`
- `source_fingerprint`
- `analyzer_version`
- `decision_schema_version`
- `decision_version`
- `config_fingerprint`
- `candidate_fingerprint`

任一項不同、資料損壞、canonical JSON 不一致或 SHA 不符即為 miss。Service 會回傳結構化 `reused` 與 `reuse_reason`，例如 analyzer/schema/decision/config/candidate/input identity 已變更；只有 miss 才允許載入 candidates 並建立新決策。

## Fail-closed 行為

- 沒有 durable Job 或 running `SUBTITLE_DETECTION` attempt：拒絕分析。
- ffprobe、抽取或 inventory 不完整：不把未知狀態當成「沒有來源」，通常回 `NEEDS_REVIEW`。
- 候選資料不完整、時間軸異常、內容空白、疑似 forced/signs/songs 或語言不足：候選不具 auto-select 資格。
- 中信心或無法消除的候選平手：`NEEDS_REVIEW`。
- 完整 inventory 中確定沒有可支援、可處理的來源：`UNSUPPORTED`，不標記完成。
- Persisted decision 的 context、hash 或選定來源重驗失敗：停止執行，不 fallback 到未記錄的來源。
- 來源檔在 inventory 或 materialization 期間改變：拒絕使用舊決策。

`NEEDS_REVIEW` 會保存 reason、evidence、所有 candidates、未選原因及 checkpoint，供修正 metadata/來源後從原 stage 重試。

## 設定

目前 `AppConfig` 公開的 M2 設定如下：

| Key | 預設 | 用途 |
| --- | ---: | --- |
| `source_analyzer_enabled` | `false` | 啟用 M2 路徑；預設保留舊流程相容性。 |
| `source_analyzer_high_confidence` | `0.90` | Auto-accept 最低決策信心。 |
| `source_analyzer_low_confidence` | `0.60` | Review 信心下限及候選語言最低門檻。 |
| `source_analyzer_min_dialogue_completeness_score` | `0.68` | 字幕對話完整度最低門檻。 |
| `source_analyzer_min_subtitle_coverage_ratio` | `0.60` | 字幕時間覆蓋最低門檻。 |
| `source_analyzer_tie_margin` | `0.025` | 觸發 close-candidate additional checks 的分數差。 |
| `source_analyzer_version` | `m2-source-analyzer-v1` | 設定宣告的 analyzer revision，納入 config fingerprint。 |
| `source_decision_schema_version` | `1` | 設定宣告的 decision schema revision。 |
| `source_decision_version` | `m2-source-decision-v1` | 設定宣告的決策語意版本。 |
| `pipeline_job_store_required` | `true` | Durable Job Store 要求；啟用 analyzer 時不可為 `false`。 |

驗證要求為 `0 <= low < high <= 1`，coverage、completeness 與 tie margin 也必須在 `[0,1]`，schema version 必須為正整數。完整信心政策見 [CONFIDENCE_POLICY.md](CONFIDENCE_POLICY.md)。

## 已知限制與後續驗收

- 預設仍為 disabled；正式開啟前需確認部署中的 SQLite schema、Worker 與設定同版。
- 目前語言辨識是 metadata/title/抽樣文字的 deterministic heuristic，不是完整語言模型；短字幕、混合語言、古字或低文字量可能進 review。
- 音訊 inventory 目前主要依容器 metadata，沒有在決策階段執行音訊語言模型；錯誤或缺失 tag 可能進 review。
- 圖形字幕及未支援 codec 不會被當成可直接處理的文字字幕。
- Forced、signs、songs 判斷是保守 heuristic，仍需要代表性 corpus 校準。
- Confidence 是政策分數，不是經實測校準的成功機率。
- M2 測試證明的是契約、restart/idempotency 與代表性 fixture 行為，不等於正式 production SLO。
- 仍需依專案驗收標準，以未參與調整的至少 100 個 Eligible Inputs 與獨立 fault injection 執行 release gate；在取得該證據前不得宣稱 99% 或 99.9% 已達成。
