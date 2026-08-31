# 可量化成功標準

## Eligible Job

只有符合以下條件的任務才納入 99% 自動化率計算：

- 輸入影片已完成寫入。
- ffprobe 可以正常解析。
- 影片包含系統支援的字幕來源，或包含可辨識的日文音訊。
- 輸入編碼及容器格式位於支援範圍。
- 系統具有足夠的必要資源、權限與儲存空間。
- 必要模型或設定已正確安裝。

嚴重損壞、加密、無有效影音軌或不支援格式，可進入 `NEEDS_REVIEW` 或 `UNSUPPORTED`，
但系統不得錯誤標記為 `COMPLETED`。

## 核心 KPI

Autonomy Rate：
沒有人工介入且通過最終 QC 的 Eligible Jobs
除以全部 Eligible Jobs。

False Completion Rate：
輸出無效、語言錯誤、字幕嚴重缺失或檔案損壞，
但任務仍被標記為 `COMPLETED` 的比例。

Duplicate Publish Rate：
相同輸入被重複產生正式輸出的比例。

Recoverable Fault Recovery Rate：
注入可恢復錯誤後，系統可以自行恢復並完成的比例。

Source Data Loss：
來源檔案被刪除、覆寫或損壞的事件數。

## Release Acceptance Gate

正式判定專案達標前，必須使用至少 100 個未參與開發調整的 Eligible Inputs。

測試資料必須包含：

- 原生繁體中文字幕。
- 原生簡體中文字幕。
- 日文字幕。
- 無字幕但有日文音訊。
- 多字幕軌。
- 多音訊軌。
- 不同 MKV/MP4 編碼組合。
- 不同字幕格式。
- 不同片長、來源及命名方式。
- 至少 20 部不同作品，避免測試資料集中在同一系列。

100 個測試輸入中：

- 至少 99 個不得需要人工介入。
- 至少 99 個必須通過正式 QC。
- Source Data Loss 必須為 0。
- False Completion 必須為 0。
- Duplicate Publish 必須為 0。
- 所有正式輸出必須可被對應工具正常解析。
- 所有任務必須具有完整狀態、決策與 QC 紀錄。

## Separate Fault-Injection Gate

另外執行獨立故障注入測試：

- Watcher 重複事件。
- 檔案尚未完成寫入。
- Pipeline 程式中途被終止。
- Docker 重啟。
- Server 重啟。
- Whisper crash。
- LLM timeout。
- GPU OOM。
- 暫存輸出存在。
- 資料庫已有部分完成狀態。

所有被定義為 Recoverable 的錯誤，都必須能從最近的有效 Checkpoint 恢復，
不得重新執行已經成功且輸出有效的昂貴 Stage。

## Production SLO

完成 100 部驗收後，系統仍需持續統計最近 500 個 Eligible Jobs：

- Autonomy Rate >= 99%。
- False Completion Rate = 0%。
- Source Data Loss = 0。
- Duplicate Publish Rate 接近 0%。
- Recoverable Fault Recovery Rate >= 99%。

`NEEDS_REVIEW` 必須保存完整原因、已嘗試的 fallback、最後 checkpoint 與恢復方法。
人工修正後，任務必須能從原 Stage 繼續。
