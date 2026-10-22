# 既有資料夾追加下載 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 合併模式可向仍有任務的資料夾追加不同影片，不改動原任務或覆寫既有影片。

**Architecture:** 沿用 destination 的預覽與提交共同檢查、TaskManager 的影片去重、每項任務的獨立暫存目錄。允許合併的前提是同目的地沒有未完成覆寫任務；提交仍在 manager 鎖內重新驗證。

**Tech Stack:** Python、pytest、既有 JavaScript、Node.js、Chrome headless、既有 NAS 儲存介面；不新增產品依賴。

**Spec:** 本文件的「行為規格」。產品程式與正式回歸測試已實作；執行證據見文末。

## 行為規格

| 同目的地狀態 | 本次選項 | 建議結果 |
|---|---|---|
| 有一般未完成任務 | merge | 允許追加；原任務保持原狀 |
| 有未完成覆寫任務 | merge | 阻擋，等待覆寫任務結束 |
| 有任何未完成任務 | overwrite | 阻擋 |
| 相同影片已有未完成任務 | merge | 新重複項目標為 skipped，其他影片繼續加入 |
| 目的地已有影片 | merge | 沿用下載工作中的既有影片檢查，略過該影片 |
| 新建資料夾 | new | 保留現有名稱、競爭檢查及建立規則 |

未完成狀態沿用目前定義：不屬於 completed、skipped、cancelled、failed。paused 與 cancelling 仍算未完成。保留目前跨目的地的影片去重範圍，不在這次修改去重政策。

## Global Constraints

- 保留 expected_destination 驗證、request_id 重試、原任務設定及檔案歸屬紀錄。
- 預覽允許不代表提交保證允許，提交必須重查目的地及覆寫狀態。
- busy 表示目的地有未完成任務；can_submit 決定是否允許提交。
- 新增影片任務，不合併任務 ID，不重建舊任務，不修改舊任務字幕設定。
- 覆寫阻擋為保守選擇，代價是覆寫期間無關影片也須等待；避免引入檔案層級覆寫協調。

## Review Focus

- 預覽後出現覆寫任務，提交應拒絕且不新增任務。
- 部分影片重複時，其餘影片仍須入列，原任務狀態不變。
- 並行或重送請求不得重複排入相同影片。
- 保存失敗只回滾新增任務，取消只清理該任務持有的檔案。
- smb、upload、direct 模式使用相同最終目的地，保留獨立暫存及虛擬 SMB 根目錄。

## 檢查證據

程式依據：`src/uav_downloader/daemon/download_batch.py:74-87` 因 busy 阻擋提交；`src/uav_downloader/daemon/task_manager.py:311-318,567-582` 負責入列去重；`src/uav_downloader/daemon/download_files.py` 管理獨立暫存及發布清理；`uav-web/frontend/download-batch.js:57-61` 將 busy 顯示為禁止追加的提示。

2026-10-22 在工作目錄執行以下命令：

```bash
.venv/bin/python -m pytest -q tests/test_web_download_settings.py tests/test_daemon_downloads.py tests/test_nas.py tests/test_smb_storage.py
node tests/download_batch.cjs
# 調查階段的候選程式已移除，由下列正式測試取代
.venv/bin/python -m pytest -q tests/test_daemon_downloads.py -k batch_append
```

調查基線為 250 passed 與瀏覽器測試 PASS；這些數字屬於修改前的結果。Python 基線首輪因缺少 av 有 12 項失敗，補裝專案已列的 PyAV 後重跑上述四個檔案全數通過。前端使用 stub 後端，NAS 測試使用假的儲存介面，不是實體 NAS 驗收。

調查階段的候選驗證只在測試程序記憶體中替換 destination，不修改產品檔案，結果為當時的 15 項候選測試。正式測試已移入 `tests/test_daemon_downloads.py`、`tests/test_web_download_settings.py` 及 `tests/test_nas.py`，移除記憶體替換程式。測試範圍包含四種忙碌狀態、重複影片、相同請求並行重送、不同請求的同影片去重、保存失敗回滾、取消檔案隔離及三種 NAS 模式的追加目的地。

第一版僅加入 merge 豁免時，「既有覆寫任務仍禁止追加」測試失敗；增加該阻擋後通過。這證明候選缺少保守限制，不代表已重現資料損壞。多執行緒測試使用同一 TaskManager，尚未涵蓋跨程序競爭。

### Task 1: 後端允許安全追加

**Files:** 修改 `src/uav_downloader/daemon/download_batch.py`；測試 `tests/test_web_download_settings.py`、`tests/test_daemon_downloads.py`、`tests/test_nas.py`。

**Interfaces:** `destination(manager, body, *, commit=False)` 保留既有欄位，新增布林 `overwrite_busy`；`submit(manager, body)` 保留既有輸入與 tasks 回傳格式。

- [x] 先寫 API 測試：建立 merge 任務後，另一批不同影片預覽為 busy=True、overwrite_busy=False、can_submit=True；提交成功，原任務內容及 ID 不變。
- [x] 補測部分重複、全部重複、paused／cancelling、相同請求重送、並行提交及保存失敗；比對原任務、queue 及持久化檔案。
- [x] 補測先預覽允許、再建立覆寫任務、最後提交合併：拒絕且 queue 不變。原本驗證忙碌時拒絕合併的測試，改為明確針對未完成覆寫任務。
- [x] 執行新增測試，確認目前產品程式因禁止合併或缺少欄位而失敗。
- [x] 在既有 manager 鎖內計算 busy 與 overwrite_busy。只有 policy=merge 且 overwrite_busy=False 時放寬 busy；保留其他名稱及目的地檢查。提交錯誤區分覆寫衝突與名稱衝突。
- [x] 將候選 NAS 追加案例移入正式 fixtures，驗證 smb／upload／direct；補測取消後原批次檔案保留，以及重啟後兩批任務仍存在。
- [x] 執行上列四個 Python 測試檔案，通過後提交此項相關檔案。

### Task 2: 對話框與整合驗收

**Files:** 修改 `uav-web/frontend/download-batch.js`、`tests/download_batch.cjs`；以既有 Web 整合入口補測真實 daemon。

**Interfaces:** 消費 Task 1 的 busy、overwrite_busy、can_submit；按鈕啟用仍依 can_submit 決定。

- [x] 先寫瀏覽器測試：busy=True、overwrite_busy=False、can_submit=True 時，可提交並顯示「將追加至此資料夾，相同影片會略過。」切換覆寫選項後禁止提交。
- [x] 補測 overwrite_busy=True 提示，以及提交時後端拒絕：保留視窗與編輯內容，顯示錯誤，不重複提交。
- [x] 確認現有前端測試失敗後，改用 can_submit 優先決定提示。覆寫衝突顯示「此資料夾有未完成的覆寫任務，請待任務結束後再追加。」
- [x] 執行上列瀏覽器及 Python 命令，全部通過。
- [x] 以真實 daemon、瀏覽器及隔離的影片來源 fixture，驗證兩批追加、部分重複、取消及重啟，不只驗證 stub。
- [x] 在 Samba 測試環境的專屬目錄跑上述操作，核對檔案內容與清理範圍；不得使用正式影片測試。
- [x] 正式程式完成後移除記憶體替換候選，保留正式回歸測試；通過專案共用檢查入口後提交，部署依既有流程。

## 回復方式

本方案不變更任務儲存格式。需要回復時還原後端判斷及前端提示即可；已新增任務沿用原有任務管理，不刪除任務紀錄或媒體。

## 實作驗收證據

產品變更限制在 `download_batch.py` 的提交判斷，以及 `download-batch.js` 的提示；沒有改動任務格式、去重範圍或儲存依賴。

- 正式回歸測試覆蓋 query／settings、未完成狀態、部分／全部重複、並行重送、保存失敗、取消清理及重啟。
- `test_batch_dialog_appends_through_real_daemon` 以 Chrome 及實際 HTTP daemon 提交兩批，透過隔離的合成媒體 fixture 執行 worker 發布，取消第三項任務，再啟動新的 daemon 驗證紀錄與兩部影片內容。下載來源解析使用 fixture，不存取外站。
- `docker build -f Dockerfile.web -t uav-downloader:batch-append-check .` 建置成功；`bash scripts/check_web_container.sh uav-downloader:batch-append-check` 驗證正式 Web／daemon 啟動及設定 API。
- `bash scripts/check_smb_batch_container.sh uav-downloader:batch-append-check` 通過既有並行 SMB、MP4 封裝與字幕發布檢查。
- 額外批次追加驗收在同一正式映像與隔離 Samba 執行，通過兩批追加、重複影片略過、並行 worker、發布、取消及紀錄重載後的內容核對。測試使用合成內容，完成後清理專屬資料夾及容器。此驗收已納入 CI 必跑項目，入口與失敗驗證見下節。
- 獨立程式碼審查未發現產品正確性或安全問題；指出的瀏覽器發布／重啟覆蓋缺口已補上並執行通過。

未部署至使用者的 NAS，也未驗證外站整部影片下載。本變更沿用既有跨程序儲存鎖，沒有新增跨程序任務協調。

完整共用檢查 `PYTHON=.venv/bin/python bash scripts/check_web.sh` 成功：955 passed、3 skipped、6 subtests passed，所有前端腳本通過。首次本機檢查因缺少專案既有 customtkinter 依賴失敗，補裝後完整重跑成功。

正式測試映像以 `Dockerfile.web-tests` 建置，執行下列命令同樣成功，Python 結果亦為 955 passed、3 skipped、6 subtests passed，所有前端腳本通過：

```bash
docker run --rm --user 1000:1000 --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace uav-downloader:batch-append-tests
```

## CI 必跑的 Samba 追加驗收

`tools/smb-ci/check_batch_append.py` 由既有 `scripts/check_smb_batch_container.sh IMAGE` 在專用容器執行，使用內部 Docker 網路、測試帳號及唯一的中文測試資料夾。workflow 的正式映像 SMB 步驟與部署前映像檢查共用此入口，沒有略過開關或容許失敗設定。

驗收使用真實 Samba 及實際 TaskManager 生命週期，來源解析以合成內容 fixture 取代。新增的 `tests/test_web_checks.py` 案例驗證專項必須被呼叫、錯誤碼傳遞、失敗後不繼續下一個驗收，以及容器清理。

反向驗證：相同 CI 入口對修正前的 `66ff1b4` 正式映像，在提交第二批時因忙碌目的地限制而失敗；對修正後的正式映像成功，輸出 `batch append SMB: PASS`。這項驗收會捕捉原始問題，並非只檢查測試腳本能啟動。

此 CI 項目不使用生產帳密或正式 NAS。生產環境驗證仍未執行，GitHub 上的 workflow 也尚未推送觸發。

CI 補強驗證：`tests/test_web_checks.py` 與 `tests/test_deploy_remote.py` 合計 39 passed；正式測試映像執行共用 Web 入口為 957 passed、3 skipped、6 subtests passed，所有前端腳本通過。workflow YAML 解析及 Samba 步驟不可容許失敗的檢查通過。獨立審查未發現須修正問題。
