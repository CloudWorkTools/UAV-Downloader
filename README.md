# 影匣

<img src="docs/assets/brand-icon.png" alt="女孩從電影收納盒探出頭的影匣圖示" width="96" height="96">

影匣提供繁體中文影片搜尋、下載佇列與字幕處理 Web 介面，支援本機目錄及 NAS 儲存。

下載佇列的「全選」包含已完成任務；按「取消／移除所選」後可依三類筆數勾選處理範圍，已完成影片保留。

## 能做什麼

- 瀏覽與搜尋 JableTV、MissAV、SupJav、Hanime1，跨頁選取影片並加入下載佇列。
- 暫停、續傳、重試及整理下載任務，保留影片與下載歷史；單項「立即下載」可繞過同時下載數上限。 可向仍有任務的資料夾追加影片，重複影片自動略過。
- 將影片儲存到本機或 NAS，支援 SMB 連線及系統掛載目錄。
- 可設定下載後產生日文辨識與英文或繁體中文字幕，也能為已下載影片建立獨立字幕工作。

Web 與 NAS 操作、字幕設定及限制見[使用說明](docs/web-ui.md)。架構、上游更新及桌面工具文件見[文件索引](docs/README.md)。

### 介面預覽

手機版瀏覽與下載佇列提供右下角人像功能選單，可分層執行影片選取、新增任務、批次處理與佇列整理；電腦版維持原有操作位置。

網站頁首與瀏覽器分頁共用新版「影匣」圖示：女孩從電影收納盒探出頭，盒蓋結合場記板，盒身保留播放符號。

搜尋結果提供接續載入與取消查詢。

![影匣搜尋：已找到 12 / 24 筆，接續補滿本頁並提供取消查詢](docs/assets/web-search.png)

<details>
<summary>查看其他介面畫面</summary>

![取消查詢後保留已顯示結果，並確認後端工作已移除](docs/assets/web-search-cancelled.png)

下載佇列提供容量資訊與批次管理。

![影匣下載清單：NAS 容量、批次管理、移除重複番號與清除已完成項目](docs/assets/web-download-queue.png)

![來源分頁異常時保留 14 筆交集結果，顯示警告並繼續比對其他來源](docs/assets/web-search-warning.png)

</details>

## 人類安裝

在要執行影匣的 Docker 主機上準備 Docker Compose v2、Buildx 與 Python 3，然後執行：

```bash
git clone https://github.com/CloudWorkTools/UAV-Downloader.git
cd UAV-Downloader
```

首次啟動時建立 Firecrawl 資料庫密碼。已有 `.env` 時，保留原檔及密碼：

```bash
(umask 077; test -e .env || python3 -c 'import secrets; print("FIRECRAWL_POSTGRES_PASSWORD=" + secrets.token_urlsafe(40))' > .env)
```

建置並啟動服務：

```bash
docker compose up -d --build --wait
```

在瀏覽器開啟 <http://127.0.0.1:8080>，再確認服務狀態：

```bash
curl --fail http://127.0.0.1:8080/api/status
```

Web 介面目前沒有登入驗證，Compose 預設只綁定本機位址。遠端使用前，請先在外層設定 HTTPS 與存取驗證。資料保存、服務管理及遠端部署細節見[安裝與部署說明](docs/install.md)；Web 與 NAS 操作見[使用說明](docs/web-ui.md)。

## 給 AI 的安裝提示

複製以下提示，請 AI 在你的主機上協助安裝：

```text
請先閱讀專案根目錄 README.md、docs/install.md、docs/web-ui.md，以及適用的 AGENTS.md 和部署指引。檢查目前作業系統、Docker Compose v2、Buildx、Python 3、專案目錄，以及 .env、downloads、config、appdata 是否已存在。

若部署指引指定遠端主機且該主機已配置，依指引在遠端部署；否則依 README 的人類安裝步驟在指定 Docker 主機安裝。若目標已有專案或資料，先沿用並保留，不要覆寫、刪除或清空。尚未下載專案時，只在目標目錄不存在時 clone。先回報環境檢查結果，再執行適用的部署步驟。

不要輸出 .env、密碼、金鑰或其他秘密內容。不要執行會刪除資料卷或使用者資料的命令，也不要把服務公開到網路；維持 Compose 預設的本機綁定位址。完成後以 /api/status 檢查服務，回報開啟網址、檢查結果及未完成事項，秘密資訊一律遮蔽。
```

下載核心與桌面工具來自 [Alos21750/UAV-Downloader](https://github.com/Alos21750/UAV-Downloader)，並沿用 [Apache License 2.0](LICENSE)。
