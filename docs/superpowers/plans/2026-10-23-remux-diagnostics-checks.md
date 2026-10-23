# Remux 診斷與回歸入口實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保存可追查且已遮蔽敏感資訊的封裝錯誤，讓既有失敗測試回到共用 CI 入口。

**Architecture:** SMB 封裝擷取每個執行緒的 FFmpeg 錯誤，沿既有任務錯誤欄位保存原因；任務管理器記錄任務 ID 與階段。測試隔離使用者偏好，文件測試分別驗證 Web 首頁與上游桌面說明。

**Tech Stack:** Python、PyAV、logging、pytest、既有 Shell／Docker 檢查入口。

**Spec:** 本次對話接受的 retro 項目 1 與 2；模組邊界及驗證規則見 `CONTRIBUTING.md`。

## Global Constraints

- 保留目前 DTS 修正、取消與儲存錯誤處理，不加入依賴。
- 每項封裝錯誤最多保存 8 則訊息；每則最多 512 字元，合併細節最多 2048 字元。
- URL、檔案路徑及帳密欄位遮蔽後才保存或輸出；不同執行緒不能混用訊息。
- 不以略過失敗測試或修改使用者偏好讓檢查通過。
- 本次不部署、不操作使用者下載任務。

## Review Focus

- 並行封裝與連續重試的診斷不能混入另一個任務的錯誤。
- 訊息數量、長度及帶有憑證的輸入均須有測試。
- 取消與磁碟寫入失敗保留原有語意。
- 命名測試在使用者偏好為 code-only 時仍獨立通過。
- 共用入口確實傳入三個模組，並保留原有失敗即停止行為。

## Task 1: 保存封裝診斷

**Files:** `src/uav_downloader/core/media_errors.py`、`src/uav_downloader/core/smb_storage.py`、`src/uav_downloader/daemon/task_manager.py`、`tests/test_media_errors.py`、`tests/test_smb_storage.py`、`tests/test_daemon_downloads.py`。

**Interfaces:** `capture_remux_errors()` 將 PyAV 封裝失敗轉成含安全診斷的 `MediaRemuxError`；既有 `DownloadTask.error` 與容器日誌供查詢。

- [x] 新增失敗測試：有上限、遮蔽、並行隔離，以及正式封裝及任務持久化路徑。
- [x] 執行測試，確認缺少診斷時失敗。
- [x] 實作擷取與任務日誌，保留其他例外型別。
- [x] 執行診斷、SMB、任務及儲存測試，要求全部通過。

## Task 2: 恢復並接入既有檢查

**Files:** `tests/test_pathlen.py`、`tests/test_hanime1.py`、`tests/test_ui_theme.py`、`tests/test_web_checks.py`、`scripts/check_web.sh`、`docs/delivery-status.md`。

**Interfaces:** 保留 `PYTHON=.venv/bin/python bash scripts/check_web.sh` 為本機、CI 與部署共同入口。

- [x] 以 code-only 使用者偏好重現命名測試失敗；確認文件測試與現行頁面的分工。
- [x] 隔離命名測試設定，調整文件斷言，保留語言連結、功能與安全指引的實際驗證。
- [x] 在入口測試要求診斷及三個既有模組，先確認舊入口失敗，再接入入口。
- [x] 執行共用入口及完整 Python 測試；建置正式／測試映像並執行共用容器檢查。
- [x] 更新交付狀態，區分本機、容器、遠端 CI 與生產未部署的範圍。

## 執行記錄

- 需求已由使用者接受；在現有隔離工作目錄直接實作，不另外建立工作目錄。
- 不提交或推送；交付可檢查的工作目錄變更。
