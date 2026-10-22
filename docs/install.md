# 安裝與部署說明

本文件補充[專案首頁](../README.md#人類安裝)中的 Compose 安裝步驟，說明資料保存、服務檢查與遠端部署。Web 介面與 NAS 設定見[使用說明](web-ui.md)。

## Docker Compose 安裝

請在預定執行影匣的 Docker 主機操作。環境需要 Docker Compose v2、Buildx 與 Python 3；Compose 啟動 Web、Speaches 與專用 Firecrawl 服務。

請依[專案首頁](../README.md#人類安裝)產生 `.env` 密碼並啟動服務。已有 `.env` 時沿用原檔，不要替換密碼。

第一次建置會下載 Rust、Python、FFmpeg 與 Firecrawl 依賴。Compose 將 Speaches 模型快取保存在 `speaches-models` Docker volume。

Compose 預設將 Web 綁定 `127.0.0.1:8080`，服務沒有登入驗證。遠端使用前，請在外層設定 HTTPS 與存取驗證。

## 資料與服務管理

下載影片、設定與應用程式資料保存在專案目錄的 `downloads/`、`config/`、`appdata/`。Speaches 模型及 Firecrawl 的 Redis、RabbitMQ、PostgreSQL 資料保存在 Docker named volumes。

停止服務並保留 named volumes：

```bash
docker compose down
```

重新啟動已建置的服務：

```bash
docker compose up -d
```

`docker compose down -v` 會刪除 Compose named volumes，其中包含模型快取及 Firecrawl 資料。一般停止服務時不要加上 `-v`。

## 健康檢查

依[專案首頁](../README.md#人類安裝)的命令查詢 Web API，回應中的 `status` 應為 `ok`。在系統設定按「儲存並測試目前站台」，確認 Firecrawl 能擷取所選站台的影片清單；Compose readiness 本身不代表所有站台都可擷取。

## 遠端部署

維護者應先閱讀該環境的 `AGENTS.md` 或部署指引。若本機指引要求使用已配置的遠端主機，從專案根目錄執行：

```bash
python3 scripts/deploy_remote.py
```

部署目標設定留在 Git 忽略的本機檔案。不要提交或輸出設定內容、主機位址、帳密或金鑰。沒有遠端部署設定時，請依本文件的 Compose 安裝方式在預定服務主機執行。

部署工具先讀取目前工作目錄的 `.deploy.local.json`；linked worktree 沒有此檔案時，直接讀取 Git 主工作目錄的同名設定，不複製檔案。目前工作目錄已有設定但內容無效時會停止，不會改用另一個目標。

部署要求本機已登入 GitHub CLI（`gh auth login`），程式已提交且工作目錄乾淨。工具會查詢目前 HEAD 的最新 `web-check.yml` 執行，只有提交完全相同、執行已結束且結果成功，且 `Web / UI compatibility` 工作的「驗證部署提交」步驟成功才繼續。此步驟會核對實際 checkout 提交，排除另行合併候選版本的測試；等待中、失敗或查不到都會停止。可用 `python3 scripts/deploy_remote.py --ci-run RUN_ID` 指定執行紀錄，仍須通過同樣核對。

工具建置該提交的正式映像後，以 `Dockerfile.web-tests` 建立隔離測試映像，透過既有 `scripts/check_web.sh` 執行 Python 與前端回歸測試，再檢查正式 Web 容器及隔離 Samba。任一檢查失敗即停止，不會傳送映像或替換遠端服務；測試映像會在檢查結束後清理。

新服務啟動後，原生 SMB 模式另外在 NAS 建立專屬中文及表情符號暫存目錄，將自產的 TS／fMP4 測試片段合成一般 MP4，驗證索引前置、影音、片長與跳轉解碼。測試成功或失敗都清理專屬目錄；檢查失敗時回復舊服務。此檢查不讀取既有影片，也不代表所有播放器均已驗證。

替換遠端服務前，工具會將任務 ID、網址、目的地及本次暫停清單寫入遠端部署目錄的 `.deployment-queue.json`。快照以私有權限原子寫入並同步磁碟；同一部署目錄以檔案鎖阻止同時部署。原本已暫停的任務不會自動恢復。部署結束或回復舊服務後，工具核對任務身分並恢復本次暫停的任務，成功才刪除快照。新服務還必須通過映像版本及首頁、JavaScript、CSS、SVG 的 SHA-256 核對；失敗時嘗試回復舊容器。

連線中斷或恢復任務失敗時，快照會保留，新部署會停止。維護者執行以下命令恢復服務與任務：

```bash
python3 scripts/deploy_remote.py --recover-queue
```

恢復命令不建置或刪除容器，可重複執行。有快照時，命令會啟動現有的 `uav-downloader` 容器；只有該容器不存在時，才將 `uav-downloader-previous` 改回原名再啟動。兩者都不存在時，維護者須先恢復服務。命令最多等待任務 API 可連線約半分鐘，再依快照恢復；成功恢復的項目會逐筆移出快照。任務遺失、網址或目的地改變、任務已失敗時，工具保留快照並停止，維護者須先排查服務及任務狀態。部署期間請勿另行變更任務或操作暫停與恢復，以免與部署的暫停請求競爭。快照包含私人網址與目的地，不要貼入 issue 或提交至 Git。
