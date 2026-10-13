# UAV Downloader Web UI (NAS & Docker)

UAV Downloader Web 提供專為 **NAS 伺服器、Docker 容器與區域網路無人值守環境** 設計的網頁管理服務。

採用 **「Rust 高效能 Web 伺服器 ＋ Python 常駐背景 Worker（Local IPC 雙向通訊）」** 架構：
- **超低系統佔用**：平時閒置近乎 0% CPU，總記憶體佔用小於 100MB。
- **防止反爬封鎖**：Python 常駐 Worker 保持 Session 與 Cloudflare Clearance，連續翻頁或搜尋不觸發頻率限制。
- **進程看門狗 (Supervisor)**：Rust 主服務持續健康檢查，萬一 Python 因異常退出，1 秒內自動無感重啟。
- **即時進度廣播**：透過 WebSocket 將每秒下載速率、分段進度與狀態推播至 Web 端。
- **適配 NAS 權限**：支援 `PUID` 與 `PGID`，避免下載檔案唯讀或無法刪除。

---

## 介面功能

1. **🔍 瀏覽 (Browse)**
   - 支援切換 **JableTV、MissAV、SupJav、Hanime1** 四大站點。
   - 動態載入分類、標籤與搜尋關鍵字。
   - 響應式卡片瀑布流，顯示封面縮圖、片長與發布時間。
   - 支援勾選多部影片，底欄「一鍵加入佇列下載」。
2. **⬇️ 下載佇列 (Downloads)**
   - 即時進度條、即時下載速率（MB/s）、剩餘分段。
   - 支援任務取消與失敗重試。
   - 支援「貼上網址」批次加入下載清單。
3. **⏱️ 追新監控 (Watcher)**
   - 依網站選擇追新分類目標。
   - 設定檢查頻率（每 6、12、24 小時），自動去重並下載新片。
4. **📜 歷史記錄 (History)**
   - 查詢已下載的番號與影片，提供模糊比對去重保護。
5. **⚙️ 系統設定 (Settings)**
   - 下載目錄（`DOWNLOAD_DIR`）、畫質偏好、命名規則（番號＋標題 / 僅番號）。
   - 並行影片數量（1–32）與單片執行緒數（1–16）。
   - AI 字幕開關（NAS 上預設停用，可選繁中或三語）。
   - 代理伺服器（Proxy）。

---

## NAS Docker 快速部署

### 使用 Docker Compose（推薦）

建立 `docker-compose.yml`：

```yaml
services:
  uav-web:
    image: ghcr.io/alos21750/uav-downloader:web
    container_name: uav-web
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Asia/Taipei
      - UAV_PORT=8080
      - DOWNLOAD_DIR=/downloads
      - CONFIG_DIR=/config
      - RESOLUTION=highest
    volumes:
      - /volume1/video/downloads:/downloads
      - /volume1/docker/uav/config:/config
```

啟動服務：
```bash
docker compose up -d
```

打開瀏覽器存取：`http://<NAS-IP>:8080`

---

## 從原始碼本地開發與執行

### 1. 啟動 Python 背景 Worker
```bash
python -m uav_downloader.daemon --port 52341
```

### 2. 啟動 Rust Web UI 服務
```bash
cd uav-web
cargo run -- --port 8080 --daemon-port 52341
```
