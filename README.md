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

在「系統設定」的「下載儲存位置」選擇：

- **透過 SMB 直接儲存至 NAS**：填寫 NAS 位址、帳號與密碼，直接以 SMB 寫入分段與影片，不需預先掛載。支援 `smb://nas.local/download` 或 `nas.local/download`。
- **使用系統已掛載的 NAS 路徑（進階）**：填入系統已掛載的 NAS 路徑，例如容器內的 `/nas`。影片分段、合成檔與字幕都寫入 NAS，不先保留完整影片到本機。未掛載或已存在同名影片時會停止下載。
- **先下載到本機，再以 SMB 傳送至 NAS**：填寫 NAS IP／主機名稱或 SMB 網址，並分開填寫帳號、密碼。網址可省略 `smb://`；網址內的共享資料夾與子目錄會優先套用。完成後傳送，保留本機檔案。
- **本機目錄**：使用一般本機下載目錄。

先按「測試連線」，再按「儲存設定」。SMB 直接儲存與下載後傳送都使用表單的帳密；只有進階掛載模式使用作業系統掛載。Docker 掛載方式見 [Web 與 NAS 使用說明](docs/web-ui.md)。

密碼會加密保存在掛載的 `/config`，網頁只顯示是否已保存。重新編輯時留空可沿用密碼；變更 NAS 位址或帳號時須重新輸入。備份設定時請一併保留 `nas.json` 與 `nas.key`，並限制備份的讀取權限。能讀取這兩個檔案的系統管理者仍可解密密碼。

詳細操作與限制見 [Web 與 NAS 使用說明](docs/web-ui.md)。

## 模組分工

| 範圍 | 位置 | 責任 |
| --- | --- | --- |
| 原作者下載核心 | `src/uav_downloader/sites/`、`core/`、`subtitles/` | 網站解析、下載與字幕 |
| Web HTTP 與靜態頁面 | `uav-web/` | Rust 網頁服務、JSON API 轉接、瀏覽介面 |
| 背景任務 | `src/uav_downloader/daemon/` | 下載佇列及下載核心的銜接 |
| NAS 擴充 | `src/uav_downloader/daemon/nas.py` | SMB 連線、掛載驗證、加密保存與完成檔案傳送 |
| 批次選取介面 | `uav-web/frontend/video-selection.js` | 全選、跨頁選取與批次送出 |
| NAS 設定介面 | `uav-web/frontend/nas-settings.js` | NAS 表單、密碼保存狀態與連線測試 |

各網站共用儲存介面，解析程式不接觸 NAS 帳密；任務管理器負責選擇原生 SMB、系統掛載或下載後傳送模式。模組責任與新增功能規範見 [架構說明](docs/architecture.md)及 [貢獻指南](CONTRIBUTING.md)。

## 上游更新

GitHub Actions 每天於臺灣時間 **09:17、21:17** 比較原作者 `master` 的已提交更新與本專案主分支，不以原作者的 PR 作為更新依據。

每次執行會列出雙方版本、獨有提交、受影響模組、檔案差異與合併衝突，並更新固定的差異追蹤 Issue 及 Actions 摘要。只有文件／圖表更新時建議暫緩同步；功能或建置變更則進入整合與相容性檢查。工作流程不會自動建立同步 PR 或合併更新，GitHub 排程可能延遲。

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
