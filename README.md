# Anime Subtitle Worker

Docker-first anime subtitle automation for Unraid/Linux servers.

流程順序：

1. 掃描 `/anime` 內現有影片。
2. 自動比對 Mikanani 是否有對應官方字幕資源。
3. 若有官方字幕，透過 qBittorrent API 加入下載，分類 `llm-sub`、標籤 `mikansub`。
4. 下載完成後自動抽出字幕，放回影片旁邊。
5. 成功抽出官方字幕後，移除同集 AI 產生的字幕。
6. 若沒有可用官方字幕，才使用 Demucs + faster-whisper large-v3 + SakuraLLM 翻譯產生 AI 字幕。

AI 輸出命名：

- `.AI日本語.ja.ass`
- `.AI简日双语.zh-CN.ass`
- `.AI繁日雙語.zh-TW.ass`

官方字幕保留原本命名邏輯，例如：

- `.日本語.ja.ass`
- `.简体中文.zh.ass`
- `.繁體中文.zh-TW.ass`

## Server Paths

容器內固定路徑：

- `/anime`: 動漫影片資料夾
- `/completed`: 可選的最終繁中 MKV 成品目錄（啟用 completed delivery 時必須獨立掛載）
- `/work`: 暫存、模型 cache、Mikan 狀態檔
- `/logs`: `app.log` / `failed.log`

Unraid 範例掛載：

- `${ANIME_INPUT_HOST_PATH:-./media}:/anime`
- `${QBIT_SUBTITLE_HOST_PATH:-./qbit_subtitles}:/qbit_subtitle_extractor`
- `${ANIME_WORK_HOST_PATH:-./work}:/work`
- `${ANIME_LOG_HOST_PATH:-./logs}:/logs`

### Final MKV delivery

預設只發布可重驗的 ASS/SRT，`completed_delivery_enabled` 保持關閉。要讓
Worker 把已通過 strict manifest/QC 的日文、簡中、繁中三軌 mux 成最終 MKV，
先把一個**不在 `/anime` 與 `/work` 之下**的真實目錄掛成 `/completed`，再設定：

```yaml
completed_delivery_enabled: true
completed_delivery_path: /completed
completed_delivery_source_policy: retain
```

初始版本刻意保留來源影片，避免破壞 qBittorrent 做種、Mikan 提取與既有
delivery ledger。成品以同檔案系統的 staging 檔驗證後原子發布；既有不同成品
不會被覆寫。每個成品另有 hash-bound receipt，重啟後可從 mux 完成、final rename
完成或 receipt 尚未寫入等中斷點自動續接。正式開 backlog 前先用一部影片確認：

- completed volume 支援 hard link 與 atomic rename；
- 最終 MKV 保留來源 A/V，且唯一預設字幕為 `zh-TW`；
- receipt、最終檔 SHA-256 與 strict output manifest 均可重新驗證。

## Required Server Setup

1. 安裝 Unraid NVIDIA Driver plugin。
2. 在 Unraid terminal 確認：

```bash
nvidia-smi
```

3. 建立資料夾：

```bash
mkdir -p ./work
mkdir -p ./logs
```

4. 確認 qBittorrent WebUI API 可從容器網路連線。
5. 確認 SakuraLLM / Ollama / vLLM 的 OpenAI-compatible API 可從容器網路連線。

`translator_base_url` 不可以用 `localhost`，因為容器內的 `localhost` 是容器自己。請改成由部署者私下設定的翻譯服務網址，例如 `https://translator.example.test/v1`。

## Configure

建議用 `.env` 放伺服器差異與密碼，不要把密碼寫死在 `config.yaml`。

建立 `.env`：

```bash
ANIME_INPUT_PATH=/anime
ANIME_WORK_PATH=/work
ANIME_LOG_PATH=/logs
ANIME_INPUT_HOST_PATH=./media
ANIME_COMPLETED_HOST_PATH=./completed
QBIT_SUBTITLE_HOST_PATH=./qbit_subtitles
ANIME_WORK_HOST_PATH=./work
ANIME_LOG_HOST_PATH=./logs
COMPLETED_DELIVERY_ENABLED=false
QBIT_BASE_URL=http://qbittorrent.example.test
QBIT_USERNAME=admin
QBIT_PASSWORD=your_qbit_password
QBIT_SAVE_PATH=/anime
QBIT_REMOTE_ANIME_PATH=/anime
TRANSLATOR_BASE_URL=https://translator.example.test/v1
TRANSLATOR_API_KEY=EMPTY
TRANSLATOR_MODEL=SakuraLLM:latest
```

單片 canary 通過後，將 `COMPLETED_DELIVERY_ENABLED` 改為 `true` 即可啟用
最終 MKV 成品交付；不需要再修改 `config.yaml`。

如果 qBittorrent 容器內下載路徑不是 `/anime`，請調整：

```bash
QBIT_SAVE_PATH=/downloads/anime
QBIT_REMOTE_ANIME_PATH=/downloads/anime
```

`QBIT_SAVE_PATH` / `QBIT_REMOTE_ANIME_PATH` 都是 qBittorrent 容器看到的路徑。如果 qB 容器的 `/anime` 實際掛到部署者私下設定的 `${QBIT_SUBTITLE_HOST_PATH}`，這兩個值仍然要填 `/anime`。

worker 容器需要另外掛載同一個主機下載資料夾：

```yaml
- ${QBIT_SUBTITLE_HOST_PATH:-./qbit_subtitles}:/qbit_subtitle_extractor
```

`config.yaml` 內的 `qbit_path_mappings` 會把 qB 回報的 `/anime` 映射到 worker 看到的 `/qbit_subtitle_extractor`。worker 的 `/anime` 是媒體庫，不是 qB 下載暫存資料夾。

## Build And Run

```bash
docker compose build
docker compose up -d
```

更新 worker 但不想手動分成 build / recreate 兩段時：

```bash
sh safe-update-worker.sh
```

這會先 build 新 image而不停止目前 worker，接著等待 Mikan 狀態更新、下載入列、字幕提取與重新下載離開臨界區，再向 Worker 發送正常停止訊號。Worker 收到訊號後不再接新 AI 任務，但會讓目前影片完成；完成後才 recreate。這避免在大量 AI backlog 下永遠等不到整個佇列歸零，也不會直接切斷當前翻譯。

Mikan 等待與當前 AI 收尾的上限都是 `IDLE_WAIT_SECONDS=14400` 秒；Mikan 等待超時會保留現行 worker 並結束，不會偷偷中斷工作。`MIKAN_WAIT_SECONDS` 仍可作為舊版相容的等待時間設定，`IDLE_WAIT_SECONDS` 優先。

AI `running` 記錄若超過 `AI_RUNNING_STALE_SECONDS=900` 秒沒有 heartbeat，會標為 stale 而不阻擋部署。超過 `MIKAN_LOCK_MAX_AGE_SECONDS=300` 秒的 Mikan lock 也不會阻擋更新。等待期間每 `POLL_SECONDS=30` 秒會輸出目前阻塞項目，因此終端不會像卡住。

安全更新預設會在 recreate 前後各建立一次一致性狀態備份；可用 `AUTO_STATE_BACKUP=0` 關閉。備份採 SQLite online backup、`quick_check` 與 SHA-256 manifest，不會直接複製仍有 WAL 的資料庫。

也可以直接用：

```bash
docker compose up -d --build anime-subtitle-worker
```

這條會把 build 和 recreate 綁在一起。worker 的 `stop_grace_period` 已改短，不會再為了目前 AI 工作等待數小時。

如果要立刻更新並接受目前工作被中斷，用：

```bash
WAIT_FOR_IDLE=0 sh safe-update-worker.sh
```

Mikan uses split locks:

- `/work/mikan_worker.lock`: protects Mikan state reset/enqueue and `seen/pending` updates.
- `/work/mikan_extract.lock`: protects completed-download subtitle extraction.
- `/work/mikan_enqueue.lock`: protects destructive qB boundaries and each qB add/delete ordering; long RSS/library scans only take this lock around the actual mutation.
- `/work/mikan_redownload.lock`: protects the redownload command itself without blocking extraction.

This lets reset/enqueue run while a completed torrent is being extracted, while still preventing concurrent writes to the same Mikan state files. qBittorrent `queuedDL` is treated as healthy backpressure (not a failed start), and the default zero-progress replacement window is 600 seconds to avoid source churn during short peer outages.

正式模式預設執行：

```bash
python main.py --config config.yaml --auto-watch
```

先跑一次測試：

```bash
docker compose run --rm anime-subtitle-worker python main.py --config config.yaml --auto-once
```

刪掉全部 Mikan 種子後，要清空 Mikan 記錄並重抓全部缺官方字幕的集數：

```bash
docker compose run --rm anime-subtitle-worker python main.py --config config.yaml --mikan-reset-all
```

如果要讓 qB 重新載入 Mikan 種子，可刪除 qB 裡的 Mikan torrent 任務、保留檔案，然後 reset 並重新加種：

```bash
docker compose run --rm anime-subtitle-worker python main.py --config config.yaml --mikan-redownload-all
```

`--mikan-redownload-all` 預設不刪影片/下載檔，只刪 qB torrent 任務。只有你確定 qB save path 不會指到媒體庫、且真的要重下檔案時，才加：

```bash
docker compose run --rm anime-subtitle-worker python main.py --config config.yaml --mikan-redownload-all --mikan-redownload-delete-files
```

WebUI 的全域 AI 暫停會寫入 `/work/ai_control.json`。暫停不會中斷目前影片，也不影響 Mikan 下載或字幕提取；Worker 會在目前影片完成後停止啟動下一部，恢復後最遲約 2 秒重新喚醒排程。多影片併發模式只維持 `max_concurrent_videos` 個已提交工作，不再一次把整批標記為 running。

Mikan 全量重抓會持續更新 `/work/mikan_redownload_all.active.json` heartbeat。WebUI 可建立 `/work/mikan_redownload_all.cancel.json` 要求安全停止；目前的網路請求或單一檔案掃描會先完成，已加入 qBittorrent 的任務會保留。安全更新腳本也會辨識這個 heartbeat，不會因長掃描暫時沒有 lock 而誤重啟 Worker。

只看設定是否能載入：

```bash
docker compose run --rm anime-subtitle-worker python -c "from config import load_config; print(load_config('config.yaml'))"
```

### Storage-balanced auto-watch

Auto-watch keeps subtitle extraction immediate without allowing concurrent MKV
readers to monopolize an Unraid storage pool. `mikan_extract_workers` is the
idle extraction limit (default 2), while `mikan_extract_workers_during_ai`
reduces it to 1 during audio selection, Whisper, or other disk-heavy AI stages.
Translation can still use the idle limit because it works from the SRT cache.

The filesystem event watcher remains immediate. Full library reconciliation is
delayed after startup and runs at `scanner_background_scan_interval_seconds`
(default 6 hours). The walker filters non-video files before stat calls and
periodically yields according to `scanner_walk_yield_every_entries` and
`scanner_walk_yield_seconds`. Set the yield seconds to `0` only on SSD-backed
libraries where maximum scan speed is preferred over storage latency.

Linux PSI I/O pressure is also sampled from `/proc/pressure/io`. When the
configured `avg10` threshold is exceeded, subtitle extraction is reduced to one
reader and full-library walking adds a short backoff. This reacts to actual
array congestion instead of relying only on the current AI stage.

Completed multi-episode torrents use an adaptive extraction deadline based on
their video count. Progress is persisted per torrent (`processed/total`), and a
deadline requests cooperative cancellation and requeues the same job. A slow
collection is therefore not immediately treated as a bad source and does not
start a duplicate replacement download while its ffmpeg operation is winding
down.

## Logs

```bash
docker logs -f anime-subtitle-worker
tail -f ./logs/app.log
tail -f ./logs/failed.log
```

`app.log` 預設每 50 MiB 輪替並保留 4 份，`failed.log` 每 10 MiB 輪替並保留 3 份。可用 `APP_LOG_MAX_BYTES`、`APP_LOG_BACKUP_COUNT`、`FAILURE_LOG_MAX_BYTES`、`FAILURE_LOG_BACKUP_COUNT` 環境變數調整。

## `/work` 維護

主 Worker 啟動時會自動移除超過 24 小時、且符合 Worker 雜湊命名格式的暫存 WAV，並只保留最新 2 份 `scanner_state.sqlite3.corrupt-*`。不會清除模型 cache、字幕 cache、目前資料庫或媒體庫。可先手動 dry-run：

```bash
docker exec -i anime-subtitle-worker python /app/work_cleanup.py --config /app/config.yaml
```

確認後套用：

```bash
docker exec -i anime-subtitle-worker python /app/work_cleanup.py --config /app/config.yaml --apply
```

掃描狀態庫使用 WAL 以支援 Worker 與 WebUI 並行存取；AI 階段歷史會合併連續進度 heartbeat，並限制保留最新 100,000 筆。Mikan 狀態鏡像只更新實際變動的 SQLite rows，避免每次下載心跳重建整張表。Mikan fallback cache 只保留實際符合該次集數的候選，最多 256 組搜尋，避免 JSON 無限增長；Worker 啟動時也會把舊版無上限 log 截到可輪替大小。

AI 佇列預設維持最新工作優先，但每 `scanner_queue_oldest_every_n_cycles`
個取件週期會改取最舊工作一次，避免大型 backlog 永久飢餓。設為 `0`
可停用公平週期。一般 AI 失敗達 `auto_ai_max_attempts` 次後會改為
`paused/failure_review`，不再無限消耗 GPU；在 WebUI 選擇重試、只重翻譯、
重新轉錄或略過即可。將上限設為 `0` 才會恢復無限自動重試。

檢查 SQLite 可回收空間（只讀）：

```bash
docker exec -i anime-subtitle-worker python /app/database_maintenance.py --config /app/config.yaml
```

實際最佳化會等待 AI／Mikan 提取閒置，先建立一致性備份，再執行
`VACUUM` 與 `quick_check`：

```bash
docker exec -i anime-subtitle-worker python /app/database_maintenance.py \
  --config /app/config.yaml --apply --wait-seconds 900
```

Worker 也會依 `database_maintenance_interval_hours` 定期檢查；只有 AI、Mikan
與字幕提取都閒置，而且可回收空間與比例同時達到設定門檻時，才會自動備份、
checkpoint、`VACUUM` 與 `quick_check`。忙碌時只延期，不會掃描整個資料庫或阻塞目前工作。

## 智慧音軌、作品資料與局部修復

- 語言辨識預設取三個時間點；多音軌 metadata 不可靠時，會逐軌取樣內容，避開 commentary 並保存選擇診斷。
- faster-whisper 在同一部影片內共用模型；`large-v3` 低信心時只用 `large-v2` 修補問題時間範圍，切換前會先釋放 V3 顯存。
- 正片內 OP/ED 預設啟用歌詞補轉：完整轉錄後只重掃片頭 6 分鐘與片尾 5 分鐘中的未覆蓋長缺口，使用歌詞專用 Prompt，再把補出的日文逐行送進原本的日中翻譯流程；可用 `op_ed_transcription_enabled` 關閉。
- `/work/series_metadata.sqlite3` 統一保存 AniList、AniDB、Mikan 對應與每部作品的日中術語；每部作品另有持久化且具唯一索引的 `series_id`，避免 WebUI 逐列掃描。
- 作品索引同步每輪可低速補全少量 AniList 資料，利用游標輪轉，避免一直重查同一批作品或瞬間觸發 API 限流。
- `/work/provenance` 保存每部影片的模型、Prompt signature、音軌、作品資訊、ASR 與品質履歷。
- 翻譯器判定日文轉錄需要人工確認時，工作會停在 `paused`，不再每 900 秒重跑整部 Whisper；WebUI 可查看封存的逐句 SRT，再選擇單行重翻或重新轉錄。
- WebUI 可對完成或失敗項目使用「重翻指定行」；命令列亦可執行：

```bash
docker exec -i anime-subtitle-worker python /app/retranslate_ai_lines.py \
  --config /app/config.yaml \
  --video-path '/anime/作品/Season 1/作品 - S01E01.mkv' \
  --lines '12,18,25-31'
```

局部重翻會先封存既有翻譯，品質檢查失敗時自動回復，不會留下半套輸出。

若設定 `notification_webhook_url`，Worker 會以 JSON webhook 通知 AI
失敗、ASR 人工確認與無法自動替換的字幕提取終止事件；相同事件會依
`notification_min_interval_seconds` 去重，避免故障迴圈洗版。

## 狀態備份與離線還原

Worker 每 24 小時自動備份 AI 佇列、Mikan、作品資料庫及必要 JSON 狀態，預設保留 14 代：

```bash
docker exec -i anime-subtitle-worker python /app/backup_state.py --config /app/config.yaml
docker exec -i anime-subtitle-worker python /app/backup_state.py --config /app/config.yaml \
  --verify /work/state_backups/備份目錄
```

先預覽離線還原計畫：

```bash
docker exec -i anime-subtitle-worker python /app/backup_state.py --config /app/config.yaml \
  --restore /work/state_backups/備份目錄
```

實際還原前必須先停止 Worker 容器，再用同一 image 掛載 `/work` 執行 `--restore ... --apply`。工具會再保存一份 `pre-restore-*` 現況；線上 Worker、AI 任務或 Mikan lock 存在時會拒絕還原。

## GPU Memory

RTX 3060 12GB 建議維持：

```yaml
max_concurrent_videos: 1
auto_ai_max_videos_per_cycle: 1
whisper_model: "large-v3"
whisper_compute_type: "float16"
enable_vocal_separation: true
```

顯存不足時：

```yaml
whisper_compute_type: "int8_float16"
```

完全不用 GPU：

```yaml
whisper_device: "cpu"
whisper_compute_type: "int8"
enable_vocal_separation: false
```

## 安全與效能契約（2026-07）

- 影片檔永遠唯讀。Worker 只會管理字幕、`/work` 快取與狀態資料。
- 三種 AI ASS 會先在暫存區完成並逐一驗證，全部合格後才一起發布；最後才寫完成 manifest。
- 正式字幕不再產生 `.v2`。舊版本、品質報告與還原點均保存於 `/work`。
- 跨檔案系統歸檔使用複製、`fsync`、SHA-256 驗證後才移除來源。
- 過長或無法解析的字幕檔會隔離，單一壞檔不會中止全庫修復。
- `scanner_state.sqlite3` 使用 WAL、短交易及有抖動的指數退避；WebUI 操作透過原子命令信箱交由 Worker 單一執行。
- `mikan_pending.json`、auto-match 與 fallback 熱路徑在權威模式下使用 `mikan_state.sqlite3`；既有 JSON 首次匯入後保留為唯讀備份。
- Mikan 配對只在 pending `bangumi_id`、人工映射、作品資料及其 episode index 範圍內進行。無唯一答案時建立 `target_ambiguity`，不匯入、不刪 torrent、也不啟動替代下載。

RTX 3060 12 GB 的預設調度如下：AI 維持單影片；翻譯階段可並行兩個字幕提取；音訊、Demucs 與 Whisper 階段只允許一個。Linux I/O PSI 達門檻時停止領取新提取工作，既有提取在安全邊界合作式暫停。Whisper 使用 `large-v3`、`float16`，依剩餘 VRAM 自動縮小批次，CUDA OOM 時逐級退回而不讓整個 Worker 崩潰。

全庫對帳採可續跑分片，每批最多 1,000 部或 60 秒，完整週期預設 6 小時；檔案事件仍立即入列。下載活動中 qBittorrent 每 5 秒同步，閒置時每 30 秒同步，提取 dispatcher 每秒檢查持久化工作。

### Deployment hold

`/work/deployment_hold.json` 存在且 `active=true` 時，Worker 不會領取新的 AI、Mikan 下載或字幕提取工作。已開始的工作可安全完成；hold 期間只允許唯讀 `system.health_probe` 命令。檔案無法解析時採 fail-closed。

正式更新請從 WebUI 專案執行整組部署腳本：

```bash
cd /path/to/anime-subtitle-worker-webui
sh safe-update-stack.sh
```

腳本會先建置並測試兩個映像，再等待 AI／提取安全終點、建立四個 SQLite（Scanner、Mikan、Control、Series）online backup 與 SHA-256 manifest、演練加法遷移、帶 hold 重建兩個容器，驗證 v2 API、系列頁與命令信箱後才解除 hold。任一檢查失敗會恢復舊映像與部署前資料；若備份雜湊驗證失敗則保持容器停止，絕不以損壞備份覆寫狀態。

## Common Issues

- `ffmpeg not found`: Docker image 內已安裝 ffmpeg；如果是本機執行，請另外安裝 ffmpeg。
- `CUDA unavailable`: 確認 Unraid NVIDIA Driver plugin、`nvidia-smi`、compose 的 `runtime: nvidia` / `gpus: all`。
- `out of memory`: 改 `int8_float16`，並保持 `max_concurrent_videos: 1`。
- `translation API timeout`: 增加 `translator_timeout_seconds`，或確認 SakuraLLM API 從容器內可連線。
- `qB login failed`: 檢查 `.env` 的 qB URL、帳號、密碼與 WebUI API 是否開啟。
- `OpenCC not found`: Docker image 內已安裝 opencc；如果本機執行請安裝 OpenCC 或使用 `opencc-python-reimplemented`。
