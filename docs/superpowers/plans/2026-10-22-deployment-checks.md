# 部署檢查 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 將本次 retro 已接受的四項改善落實為部署工具及自動檢查。

**Architecture:** 延伸既有 `storage_smoke` 與 `STORAGE_PROBE`；正式映像、CI 及生產檢查共用中文路徑封裝驗證。部署前使用既有 `Dockerfile.web-tests` 執行 `check_web.sh`，再執行正式映像與 Samba 檢查；全部成功才傳送映像。缺少 worktree 私有設定時，沿用主工作目錄設定。

**Tech Stack:** Python、pytest、FFmpeg、PyAV、Docker、既有 Bash 檢查入口。

**Spec:** 本次對話中已接受的 retro 四項建議；部署及資料保護規則見 `CONTRIBUTING.md` 與 `docs/install.md`。

## Global Constraints

- 沿用既有依賴與測試入口，不新增略過部署檢查的選項。
- 測試影片由工具產生，僅寫入專屬暫存目錄；成功或失敗都清理，保留正式影片與設定。
- 真實主機、帳密及私有路徑不輸出至測試結果或版本控制文件。

## Review Focus

- 中文及表情符號須出現在實際 SMB 路徑，驗證經過遠端封裝分支。
- 任何映像檢查失敗都不得進入遠端載入與安裝。
- 生產封裝失敗須沿用既有回復機制。
- worktree 設定優先；明確指定但不存在或無效的設定不得偷偷替換。
- 測試資料與測試映像的清理不得影響既有服務資源。

### Task 1: 共用中文路徑封裝檢查

**Files:** `src/uav_downloader/daemon/storage_smoke.py`、`tests/test_smb_storage.py`、`scripts/deploy_remote.py`、`tests/test_deploy_remote.py`。

**Interfaces:** 新增 `check_mp4_smb(settings) -> bool`，非原生 SMB 回傳 False；成功回傳 True，失敗拋出例外。CLI 與生產 `STORAGE_PROBE` 都執行它。

- [x] 先加入失敗測試：實際 TS／fMP4 封裝、中文路徑、片長、影音及跳轉解碼；錯誤與清理失敗不可視為通過。
- [x] 執行測試確認缺少功能而失敗，再以既有 crawler 與 SMB seam 實作。
- [x] 接入生產 probe，驗證錯誤遮蔽、清理與回復舊服務。

### Task 2: 部署檢查與設定解析

**Files:** `scripts/deploy_remote.py`、`tests/test_deploy_remote.py`、`scripts/check_web.sh`、`tests/test_web_checks.py`。

**Interfaces:** `load_config(path=None)` 優先讀本地設定，否則以 Git 主工作目錄尋找；明確 path 維持原行為。`check_image(image)` 在正式映像上建立隔離測試映像，執行既有三項檢查並清理測試映像。

- [x] 以真實臨時 Git repo／worktree 驗證設定優先、缺少設定、無效設定與含空白路徑。
- [x] 以假的 Docker／SSH 驗證每個檢查失敗時未傳送、未安裝，成功時測試與部署使用同一個提交映像。
- [x] 補入共用入口的 crawler 測試，驗證 Python 失敗會停止後續前端檢查。
- [x] 實作後執行上述測試並確認通過。

### Task 3: 整合與交付

**Files:** `docs/install.md`、`CONTRIBUTING.md`，以及前兩項既有檢查入口。

- [x] 同步文件：單一測試入口、設定尋找順序、強制部署檢查與生產封裝回復。
- [x] 建置正式映像及測試映像，透過既有共用入口及 Samba 檢查驗證；另執行完整 pytest 並比對已知失敗。
- [x] 請獨立 reviewer 檢查失敗路徑與正式資料保護，處理有效發現。
- [x] 完成可提交變更與部署前檢查；生產部署及實際 NAS 結果由本次交付回覆記錄。
