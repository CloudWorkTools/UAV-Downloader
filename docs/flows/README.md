# 系統流程與程式導航

本索引涵蓋已運行服務上的 Web 批次下載提交：從勾選影片、預覽目的地，到任務加入佇列、整批儲存及啟動派送。本範圍包含提交時的選用字幕設定快照；下載執行的任務重試與發布邊界另見 `download.execute`；網站擷取細節、後續字幕處理及服務啟停尚未建立流程基線。

閱讀程式前，依[同步政策](../agents/flow-docs-sync.md)讀取本索引、[manifest](../flow-manifest.json)、受影響流程與[共用契約](contracts.md)。流程圖描述現行實作；預期責任仍由既有規格及貢獻規範定義。早期基線的歷史驗證見[導入證據](../flow-evidence/adoption.md)；目前候選版本以 manifest 中的指紋與審查紀錄為準。

## 流程索引

| Feature ID | 已採用範圍與流程圖 | 入口與主要符號 | 相依功能 |
| --- | --- | --- | --- |
| `download.batch` | [勾選、預覽、提交、重試與取消](download.batch.mmd) | [mobile-actions.js](../../uav-web/frontend/mobile-actions.js) 與 [mobile-actions.css](../../uav-web/frontend/mobile-actions.css) 管理手機選單；[video-selection.js](../../uav-web/frontend/video-selection.js) `createVideoSelection.download`；[download-batch.js](../../uav-web/frontend/download-batch.js) `createDownloadBatch`；[index.html](../../uav-web/frontend/index.html) `downloadBatch.open`；[routes.rs](../../uav-web/src/routes.rs) `create_router`、`download_batch_handler`；[client.rs](../../uav-web/src/client.rs) `DaemonClient::download_batch`；[server.py](../../src/uav_downloader/daemon/server.py) `DaemonRequestHandler.do_POST`；[download_batch.py](../../src/uav_downloader/daemon/download_batch.py) `submit` | `storage.destination`、`queue.enqueue` |
| `storage.destination` | [儲存位置解析、衝突檢查與提交確認](storage.destination.mmd) | [download_batch.py](../../src/uav_downloader/daemon/download_batch.py) `folder_name`、`destination`；[nas.py](../../src/uav_downloader/daemon/nas.py) `load_settings`、`direct_destination`、`smb_destination`、`public_destination`；[storage.py](../../src/uav_downloader/core/storage.py) `storage_fs`、`registered_storage`；[smb_storage.py](../../src/uav_downloader/core/smb_storage.py) `SmbStorage` | 無 |
| `queue.enqueue` | [批次任務建立、去重與持久化](queue.enqueue.mmd) | [task_manager.py](../../src/uav_downloader/daemon/task_manager.py) `TaskManager.add_task`、`_duplicate_reason`、`_save_tasks`、`start_dispatcher`、`DownloadTask.to_dict`；[download_files.py](../../src/uav_downloader/daemon/download_files.py) `video_key`、`DownloadFiles.snapshot`；[video_identity.py](../../src/uav_downloader/core/video_identity.py) `video_code` | `storage.destination` |
| `download.execute` | [下載、重試、發布、取消與暫停](download.execute.mmd) | [task_manager.py](../../src/uav_downloader/daemon/task_manager.py) `TaskManager._run_task_with_retries`、`_run_task`、`_retry_publication`；[download_files.py](../../src/uav_downloader/daemon/download_files.py) `DownloadFiles.publish`；[nas.py](../../src/uav_downloader/daemon/nas.py) `upload_video` | `queue.enqueue`、`storage.destination` |

每個圖節點的檔案與符號映射以 manifest 為準。`download_batch.py` 使用原生 Python AST 模式，所有頂層函式均有映射，內部 `same_destination` 屬於 `destination`。混合職責模組、Rust、JavaScript、HTML 及行為設定採明示 `file` 模式：整份檔案異動會保守觸發審查，不表示該檔案所有功能都已有流程圖。

## 來源與文件責任

| 權威文件或輸入 | 維護責任與用途 | 與本流程的關係 |
| --- | --- | --- |
| [CONTRIBUTING.md](../../CONTRIBUTING.md) | 專案維護者；模組、安全及驗證要求 | 保留 NAS、HTTP、任務管理器的責任邊界 |
| [architecture.md](../architecture.md) | 模組維護者；既有架構 | 服務及儲存模組導航 |
| [web-ui.md](../web-ui.md) | Web 維護者；使用者操作 | 操作說明的既有位置，流程文件不取代它 |
| [issue-tracker.md](../agents/issue-tracker.md) | 需求提出者與維護者；GitHub Issues | 新需求與任務的正式位置 |
| [domain.md](../agents/domain.md) | 領域維護者；術語與 ADR 讀取規則 | 根目錄術語表與字幕 ADR 不屬於現行批次實作證據 |
| [同步政策](../agents/flow-docs-sync.md) | 交付準備代理與維護者 | 共用規則的唯一可追蹤位置；本機 `AGENTS.md` 載入它 |
| [contracts.md](contracts.md) | daemon、Web 與儲存維護者 | 批次邊界、錯誤、副作用及設定輸入；目前沒有其他批次 wire schema |
| [flow-manifest.json](../flow-manifest.json) | 修改受影響功能的代理與審查者 | 明示來源清單、MD5、AST 指紋、節點映射、相依與審查證據 |
| `docs/flow-documentation-research.md` | 研究文件作者 | 現有未追蹤研究稿，保留使用者內容，不作基線依賴或執行規則 |

`ui_prefs.json`、`nas.json`、`nas.key`、環境變數與遠端 NAS 是執行環境輸入，實際內容不加入 Git。其讀取邏輯與依賴宣告登記於 manifest；遠端內容或浮動套件的變化仍需獨立執行證據。

## 覆蓋邊界與缺口

manifest 的 include 清單界定本次盤點。它涵蓋現有批次前端、HTTP 轉送、目的地、佇列建立與持久化依賴；`download_batch*.py` 與 `download-batch*.js` 的新檔會被檢查器要求明示登記。其他新檔或新增跨模組依賴仍需來源審查，檢查器不會自動推導呼叫圖。

未覆蓋的第一方邏輯包括網站擷取、搜尋與瀏覽、提交後的字幕處理、服務重啟後恢復、各網站擷取與片段下載實作、服務啟停／監督／WebSocket 更新、CLI、桌面程式、Watcher、建置與部署。`download.execute` 描述執行中的取消及重試邊界，不涵蓋全部網站擷取細節。批次新增任務成功回應也不代表影片已下載完成。

這是部分導入，不能宣稱全專案覆蓋。共用檔案雖以整檔摘要納入變更警示，其非批次功能仍為缺口；新增功能須連同依賴擴充流程及映射，不能只重算摘要。

## 交付與驗證入口

預設邊界為 PR 交付審查前，實際設定及完整操作依[同步政策](../agents/flow-docs-sync.md)。政策預設位置 `AGENTS.md` 因明示的本機專用規則覆寫為 `docs/agents/flow-docs-sync.md`；其餘採技能預設路徑。未配置自動 runner、GitHub workflow 或 strict pre-commit hook；Maintain 由準備端代理在交付時執行，不能將本機通過視為合併已受保護。

專案檢查器為 `tools/flow-docs/flow_checker.py`，獨立隔離回歸入口為：

```bash
python3 -m unittest discover -s tests -p test_flow_docs.py
python3 tools/flow-docs/flow_checker.py check --root . --base <BASE>
```

`<BASE>` 使用此次交付真正的比較 commit。檢查結果只證明該候選內容的結構、摘要及審查紀錄一致；語意審查、執行結果、圖形視覺檢查、人類核准與遠端合併設定分別記錄於導入證據。圖上限為 25 個節點、分支深度 12；目的地圖保留驗證與各種 I/O 失敗分支，採較高深度以免為通過限制而刪減錯誤路徑。`.mmd` 尚無專案渲染入口，visual verification: pending。
