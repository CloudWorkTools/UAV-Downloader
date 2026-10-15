# UAV Downloader Web

[CloudWorkTools/UAV-Downloader](https://github.com/CloudWorkTools/UAV-Downloader) 是以繁體中文使用者為主的 Web 下載介面與 NAS 擴充版本。專案沿用 [Alos21750/UAV-Downloader](https://github.com/Alos21750/UAV-Downloader) 的下載核心，將瀏覽、複選下載、下載佇列及 SMB 儲存整合到瀏覽器。

## 本機啟動

需要可執行的 Docker。第一次建置會下載 Rust、Python 與 FFmpeg 的依賴。

```bash
docker build -f Dockerfile.web -t uav-downloader:local .
mkdir -p "$HOME/.local/share/uav-downloader-local/downloads" "$HOME/.local/share/uav-downloader-local/config"
docker run -d --name uav-downloader-local \
  -p 127.0.0.1:8080:8080 \
  -e TZ=Asia/Taipei \
  -v "$HOME/.local/share/uav-downloader-local/downloads:/downloads" \
  -v "$HOME/.local/share/uav-downloader-local/config:/config" \
  uav-downloader:local
```

開啟 **<http://127.0.0.1:8080>**。已有同名容器時，使用 `docker start uav-downloader-local` 啟動現有容器。

驗證服務：

```bash
curl --fail http://127.0.0.1:8080/api/status
```

回應中的 `status` 應為 `ok`。停止服務使用 `docker stop uav-downloader-local`。下載與設定存放在上方掛載的本機目錄，停止或重建容器時仍會保留。

Web 介面目前沒有登入驗證，因此範例只開放本機連線。遠端使用前，請先在外層配置 HTTPS 與存取驗證。

## 瀏覽與批次下載

1. 選擇網站與分類，或輸入關鍵字搜尋。
2. 使用搜尋列旁的「全選」選取目前頁面的影片，也可逐片勾選。
3. 按「下載已選」加入下載佇列。
4. 在「下載佇列」查看進度或取消任務。

「取消全選」只取消目前頁面的選取。跨頁已選影片仍會保留；加入佇列失敗的影片會維持選取，方便重試。

## NAS / SMB 設定

在「系統設定」填寫 NAS IP、共享資料夾、帳號、密碼及可選的子目錄。共享目錄與子目錄須已存在，帳號須有寫入權限。

先按「測試連線」，再按「儲存設定」。啟用後，新加入的下載任務會在本機完成影片與字幕，再將檔案傳送到 NAS。傳送失敗會標記任務失敗並保留本機檔案；NAS 已存在的同名檔案不會被覆寫。

密碼會加密保存在掛載的 `/config`，網頁只顯示是否已保存。重新編輯時留空可沿用密碼；變更 IP 或帳號時須重新輸入。備份設定時請一併保留 `nas.json` 與 `nas.key`，並限制備份的讀取權限。能讀取這兩個檔案的系統管理者仍可解密密碼。

詳細操作與限制見 [Web 與 NAS 使用說明](docs/web-ui.md)。

## 模組分工

| 範圍 | 位置 | 責任 |
| --- | --- | --- |
| 原作者下載核心 | `src/uav_downloader/sites/`、`core/`、`subtitles/` | 網站解析、下載與字幕 |
| Web HTTP 與靜態頁面 | `uav-web/` | Rust 網頁服務、JSON API 轉接、瀏覽介面 |
| 背景任務 | `src/uav_downloader/daemon/` | 下載佇列及下載核心的銜接 |
| NAS 擴充 | `src/uav_downloader/daemon/nas.py` | 連線驗證、加密保存與完成檔案傳送 |
| 批次選取介面 | `uav-web/frontend/video-selection.js` | 全選、跨頁選取與批次送出 |
| NAS 設定介面 | `uav-web/frontend/nas-settings.js` | NAS 表單、密碼保存狀態與連線測試 |

NAS 模組不修改網站爬蟲；任務管理器只在影片完成後呼叫傳送介面。模組責任與新增功能規範見 [架構說明](docs/architecture.md)及 [貢獻指南](CONTRIBUTING.md)。

## 上游更新

GitHub Actions 每天於臺灣時間 **09:17、21:17** 檢查原作者的 `master`。有更新時建立同步 PR，再評估與本專案主分支合併後的相容性。

檢查包含 NAS、設定、佇列、儲存錯誤、全選／批次下載，以及 Web Docker 建置與啟動。衝突或測試失敗時，檢查會失敗。維護者須檢視結果與差異後決定是否合併，排程不會直接推送或合併到 `master`。GitHub 排程可能延遲。

流程與判斷依據見 [上游更新維護](docs/upstream-sync.md)。

## 開發與檢查

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-docker.txt pytest PySocks customtkinter Pillow
.venv/bin/python -m pytest -q tests/test_nas.py tests/test_daemon.py tests/test_proxy.py tests/test_storage_failures.py tests/test_resolution_pref.py
node tests/web_ui.cjs
bash scripts/check_web_container.sh uav-downloader:local
```

桌面版原有測試另有 GUI 與 AI 依賴；上方為 Web 擴充使用的檢查集合。

## 原作者與授權

下載核心與原有桌面工具來自 [Alos21750/UAV-Downloader](https://github.com/Alos21750/UAV-Downloader)，沿用 [Apache License 2.0](LICENSE) 並保留原有著作權與第三方聲明。原作者的 Windows 執行檔請至[上游 Releases](https://github.com/Alos21750/UAV-Downloader/releases)取得，並參閱[桌面版文件](docs/README.md)。這些發行檔不包含本專案的 Web／NAS 擴充。
