# 貢獻指南

本專案主要維護繁體中文 Web 介面與 NAS 擴充，並持續追蹤原作者下載核心。使用者文字及新增文件以臺灣慣用的繁體中文撰寫。

## 模組邊界

- 網站解析與下載行為留在 `src/uav_downloader/sites/`，不匯入 Web 或 NAS 模組。
- `daemon/server.py` 負責 HTTP 資料轉接與驗證結果，不實作 SMB 協定。
- `daemon/task_manager.py` 負責任務狀態與下載生命週期，在下載前驗證 NAS 路徑，或在完成點呼叫 NAS 傳送介面。
- `daemon/nas.py` 管理 SMB 設定、帳密保存及檔案傳送，不依賴 HTTP handler 或前端欄位。
- `uav-web/frontend/video-selection.js` 管理選取狀態，透過傳入的下載方法送出任務，不直接依賴網站解析。
- `uav-web/frontend/nas-settings.js` 管理 NAS 表單；下載器不依賴這個檔案。
- 新增擴充先使用獨立模組，再透過明確的方法接入既有流程。避免在爬蟲內加入 UI 狀態、表單欄位或 NAS 帳密。

## 開發檢查

Web 回歸測試與 CI 使用同一入口：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-docker.txt pytest PySocks customtkinter Pillow
PYTHON=.venv/bin/python bash scripts/check_web.sh
```

另需 Node.js 與 Chrome／Chromium。測試預設執行 `google-chrome`；其他位置可用 `CHROME_BIN` 指定。
修改核心時，另執行受影響的原有測試；修改 UI 時，檢查窄畫面、按鈕高度、空資料、重複送出與失敗後重試。

依修改功能選擇既有測試介面：

| 功能 | 測試責任與導航 |
| --- | --- |
| 任務操作與服務重啟 | `tests/test_daemon_downloads.py` 驗證任務狀態、持久化與取消清理；`tests/test_web_download_settings.py` 驗證批次設定與 HTTP 行為。 |
| 搜尋與來源擷取 | `tests/test_search_intersection.py` 驗證交集與增量分頁；`tests/test_firecrawl.py` 驗證擷取服務轉接。 |
| 字幕 API 與重試 | `tests/test_asr_api.py` 驗證辨識回應；`tests/test_subtitle_engine.py` 與 `tests/test_subtitle_provenance.py` 驗證生成流程與已產生字幕的重用。 |
| 背景更新與前端設定 | `tests/task_menu.cjs` 使用真實 DOM 驗證選單、焦點與表單；其他 `.cjs` 測試驗證選取與設定。 |
| 儲存與部署 | `tests/test_nas.py`、`tests/test_smb_storage.py` 驗證隔離儲存；`tests/test_deploy_remote.py` 驗證假的 SSH／Docker 呼叫與部署失敗處理。 |

新增 Web Python 回歸測試時，將模組接入 `scripts/check_web.sh` 的集中清單；新增前端 `.cjs` 測試會由入口自動納入。CI 直接使用此入口，文件只保留功能導航。

容器檢查使用下列命令：

```bash
docker build -f Dockerfile.web -t uav-downloader:local .
bash scripts/check_web_container.sh uav-downloader:local
bash scripts/check_smb_batch_container.sh uav-downloader:local
```

SMB 檢查另以隔離 Samba 驗證同批多任務的並行儲存。實際 NAS、ASR、模型品質與遠端容器掛載的驗證需要專用環境，離線回歸測試無法代替這些檢查。

SMB 測試使用獨立的測試帳號與共享目錄，勿使用正式影片目錄作為測試資料。保留本機檔案、未經使用者明確選擇不覆寫遠端既有檔案、錯誤不包含密碼，均為 NAS 擴充的必要行為。

## CI 與生產環境驗證

Linux 工作指定 `shared-runners` 群組及 `self-hosted`、`Linux`、`X64`、`shared` 標籤。Web 回歸測試由 `Dockerfile.web-tests` 在正式映像上加入測試依賴，使用獨立容器與唯讀原始碼；共用主機須提供 Docker。公開 fork 的 PR 不自動執行此自架工作，維護者審查後可手動派送。Windows 建置保留 Windows runner。

新增或修改 CI 時，必須針對當次變更目的加入可失敗的驗證，重現該變更要解決的實際操作。完成條件包含離線回歸、正式 Docker 映像的整合測試，以及部署後實際生產環境的驗證；三者分別記錄版本、執行命令、結果及未驗證範圍。未執行的生產驗證須標示未完成，不能以單元測試或模擬服務結果代替。

PR CI 使用隔離服務、測試帳密與獨立資料，生產檢查由部署程序沿用遠端既有設定。測試只在專屬暫存資料夾讀寫，完成或失敗後清理；保留原有影片、設定與下載紀錄。生產檢查失敗時，部署程序回復舊版服務。真實位址、帳密與資料內容留在環境內，不寫入倉庫或測試輸出。

SMB 批次下載的正式映像測試入口為 `bash scripts/check_smb_batch_container.sh IMAGE`，使用真實 Samba 驗證同批兩個任務的獨立連線、並行寫入、跨連線讀回與清理。部署程序在實際 NAS 執行相同的 `daemon.storage_smoke.check_batch_smb` 檢查。這個測試不擷取外站影片，不代表整部影片下載或播放器相容性已驗證。

## 推送前的說明與截圖

推送可見功能變更前，同步更新 README 與對應使用說明。介面或操作流程有變更時，從實際執行的版本重新擷取相關畫面，存入 `docs/assets/` 並更新文件引用；截圖須隱去影片內容、私人位址、帳號、路徑與任務細節。確認圖片可開啟、相對連結正確，且說明符合目前行為後再提交推送。

## 上游相容性

請將爬蟲修正、Web 操作與 NAS 功能分別放在各自模組，讓上游更新容易比較。公開 CLI、既有設定遷移及下載器方法須保持相容；確實需要修改時，連同呼叫端與回歸測試一起調整。

上游同步規則見[上游更新維護](docs/upstream-sync.md)。不提交帳密、Cookies、下載影片、模型或個人日誌。原作者與第三方授權聲明須保留。
