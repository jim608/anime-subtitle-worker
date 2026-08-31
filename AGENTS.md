# AI Anime Subtitle Platform — Codex Project Instructions

## Mission

漸進式地將現有 AI 動漫字幕系統改造成：

「99% 無人值守、自動恢復、自動決策、可驗證品質」的繁體中文字幕處理平台。

最終使用者正常情況下只需要：

1. 將影片放入 Watch Folder。
2. 等待系統自動完成。
3. 取得繁體中文字幕、ASS/SRT，或已 mux 字幕的 MKV。

使用者應管理例外，而不是管理 Pipeline。

不要嘗試一次重寫整個專案。所有修改必須分階段實作、測試與驗收。

## Runtime Constraints

- 主要部署環境：單機 UNRAID + Docker。
- 基準 GPU：NVIDIA RTX 3060 12GB。
- CPU、RAM、GPU 和磁碟可能同時被其他服務使用。
- 現有技術包含 ffmpeg、ffprobe、faster-whisper/Whisper、LLM 翻譯、OpenCC、ASS/SRT。
- 翻譯模型與 ASR 模型必須透過 Adapter 和設定檔管理，不可硬編碼。
- 必須盡量保持現有設定檔、輸入目錄、輸出目錄及命名方式的相容性。
- 來源影片必須視為唯讀，不得修改或刪除。

## Priority Order

發生需求衝突時，依照以下優先順序決策：

1. P0：來源檔案安全、避免資料遺失及錯誤覆蓋。
2. P1：任務持久化、Checkpoint、Resume、Idempotency。
3. P2：字幕與音訊來源選擇正確性。
4. P3：字幕品質、翻譯一致性與 QC。
5. P4：GPU、RAM、CPU、磁碟 I/O 與效能。
6. P5：WebUI、顯示效果及非必要功能。

## Mandatory Engineering Rules

1. 修改前先閱讀現有程式、設定、Docker 配置、測試與日誌。
2. 修改前先建立目前行為基準，不得憑假設重構。
3. 優先小範圍漸進式修改，不做無關重構。
4. 除非有明確技術證據，不得全面更換框架或加入大型基礎設施。
5. 每個 Pipeline Stage 必須是可重入且具 Idempotency。
6. 每個 Stage 必須保存開始時間、完成時間、輸入、輸出、模型、Retry 次數及錯誤。
7. 每個 Stage 完成後才可原子性提交狀態。
8. 所有正式輸出先寫入暫存路徑，驗證成功後再使用 atomic rename 發布。
9. 不得僅依檔案路徑判斷重複任務；必須使用穩定的檔案識別或 fingerprint。
10. Retry 必須有限制，並區分 transient、resource、quality、permanent failure。
11. 不得無限重試相同操作；重試必須包含 backoff、參數調整或 fallback。
12. 可恢復錯誤不得直接將整個任務標記為永久失敗。
13. 模型 fallback、batch size、context、concurrency 必須由設定及資源狀態控制。
14. Watcher 不得持續遞迴掃描完整媒體資料庫。
15. 優先使用 filesystem events；只允許啟動時 reconciliation 及低頻有限範圍補掃。
16. WebUI 是監控介面，不應成為正常 Pipeline 必須依賴的控制介面。
17. 不得以 mock、placeholder、TODO 或僅單元測試通過宣稱正式功能完成。
18. 不得在沒有實際 eval 結果時宣稱已達成 99% 自動化。
19. 所有重要決策必須留下 reason code、evidence 與 confidence。
20. 發現既有行為可能被破壞時，必須先加入 regression test。

## Testing Requirements

每次功能修改至少需要：

- Unit tests。
- Integration tests。
- Happy-path 測試。
- Failure-path 測試。
- Restart/resume 測試。
- Idempotency 測試。
- 原始檔案不被修改的檢查。
- 舊設定或既有流程的 regression test。

涉及 Pipeline 的修改，必須測試：

- 程式在 Stage 中途終止後重新啟動。
- Docker 重啟後恢復。
- 同一檔案收到多次 filesystem event。
- 模型 timeout。
- 模型 crash。
- GPU OOM 或模擬的資源不足。
- 輸出暫存檔已存在。
- 部分 Stage 已完成。
- 使用者將尚未寫完的檔案放入 Watch Folder。

## Working Method

- 先閱讀 `docs/PROJECT_VISION.md`。
- 依照 `PLAN.md` 一次完成一個里程碑。
- 每個里程碑維護可驗證的 checklist。
- 使用小型且可回滾的 checkpoint。
- 每次修改後執行相關測試並檢查 diff。
- 除了憑證、不可逆操作或資訊確實不足，不要要求使用者決策。
- 可自行做出的工程判斷，記錄假設後直接執行。
- 遇到阻塞時，先尋找安全替代方案，不要直接停止整個工作。

## Definition of Done

一個里程碑只有在以下條件全部成立時才算完成：

1. 功能已實際實作，不是設計文件或 placeholder。
2. 相關測試全部通過。
3. 已執行至少一個真實或代表性整合測試。
4. 沒有破壞現有可用流程。
5. Restart、Resume 和 Retry 行為符合該里程碑要求。
6. 文件、設定範例及 migration 說明已更新。
7. 最終報告列出修改檔案、測試命令、測試結果與剩餘風險。
