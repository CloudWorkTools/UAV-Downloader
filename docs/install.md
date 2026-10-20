# 安裝與部署說明

本文件補充[專案首頁](../README.md#人類安裝)中的 Compose 安裝步驟，說明資料保存、服務檢查與遠端部署。Web 介面與 NAS 設定見[使用說明](web-ui.md)。

## Docker Compose 安裝

請在預定執行影匣的 Docker 主機操作。環境需要 Docker Compose v2、Buildx 與 Python 3；Compose 啟動 Web、Speaches 與專用 Firecrawl 服務。

> **草稿**：一般 Compose 首次安裝分支尚未在專用 Docker 主機完整走查；請首位實際使用者回報結果。

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
