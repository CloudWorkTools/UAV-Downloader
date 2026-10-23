# 批次提交邊界與共用契約

本文件記錄現行 Web 批次提交的可觀察邊界。來源為 [download_batch.py](../../src/uav_downloader/daemon/download_batch.py)、[server.py](../../src/uav_downloader/daemon/server.py)、[task_manager.py](../../src/uav_downloader/daemon/task_manager.py) 與 [download-batch.js](../../uav-web/frontend/download-batch.js)。模組與安全要求以 [CONTRIBUTING.md](../../CONTRIBUTING.md) 為準；本文件不替代它。

## HTTP 邊界

| 方法與路徑 | 輸入與作用 | 成功輸出 | 失敗 |
| --- | --- | --- | --- |
| `POST /api/download/batch` | JSON 物件；依 action 預覽或提交 | 200；預覽資料或帶有 `tasks` 的提交資料 | daemon 授權失敗 401；JSON 讀取／ValueError 400；其他例外 500；Rust 連線或回應解析失敗 502 |

Rust 的 `download_batch_handler` 與 `DaemonClient::download_batch` 原樣轉送 JSON，保留 daemon HTTP 狀態。daemon 可配置 Bearer 或 `X-Auth-Token` 驗證，未設 token 時允許請求。daemon 的 `_read_json_body` 只解析 JSON，未保證結果為物件；非物件輸入可能在使用 `.get` 時造成 500，不能宣稱有完整 schema 驗證。

### 批次請求

| 欄位 | JSON 型別 | 省略時 | 使用與限制 |
| --- | --- | --- | --- |
| `action` | string | 無預設 | `preview` 或 `submit`；其他值拒絕 |
| `storage` | string | `query` | `query` 使用資料夾名稱；`settings` 使用設定的根目錄 |
| `name` | string | 空字串 | query 模式限 1000 字元，清理禁用字元、尾端空白及句點後截至 100 字元；空名稱轉為當日預設名稱；Windows 保留名稱加前綴；settings 模式忽略 |
| `policy` | string | `new` | `new` 建立新目錄；`merge` 合併；`overwrite` 記錄後續允許覆寫同名輸出，不會在提交時刪除舊檔 |
| `naming` | string | `date` | `date`、`number`、`custom`；new 衝突時前兩者找可用名稱，custom 不允許同名提交 |
| `expected_destination` | string | 無預設 | submit 時必須與重新計算的顯示目的地相等 |
| `urls` | array of string | 無預設 | submit 需要 1..500 個網址，每個以 `http://` 或 `https://` 開頭且不超過 4096 字元；這不是完整網址或支援網站驗證；同字串去重後依原順序建立任務 |
| `request_id` | string | 無預設 | submit 需要非空且不超過 100 字元；對應程序內有限快取 |

上述有預設值的欄位只在省略時採預設；明示 `null` 不等於省略。`storage`、`policy`、`naming` 的 null 會拒絕；query 的 `name` null 會拒絕；submit 的 `urls`、`request_id`、`expected_destination` null 也不符合驗證。未知欄位通常不參與操作，但相同 request_id 的整個 body 比較會包含它們。

### 預覽與提交回應

| 欄位 | JSON 型別 | 意義 |
| --- | --- | --- |
| `success` | boolean | 成功為 true |
| `name` | string | 解析後的資料夾名稱；settings 模式為空 |
| `destination` | string | 最終顯示目的地；遠端模式為編碼的 SMB URL |
| `base_destination` | string | 設定的顯示根目錄 |
| `exists` | boolean | 改名避開衝突前的目標是否存在 |
| `file_count` | number (integer) | 最終目標中的一般檔案數，不含 `.uav-` 前綴檔案 |
| `busy` | boolean | 同一顯示目的地有未終結任務；paused 也視為忙碌 |
| `can_submit` | boolean | 目的地不忙碌，且不存在 custom/new 同名衝突 |
| `tasks` | array of object | 僅 submit 回應包含；每項由 `DownloadTask.to_dict` 序列化，完整欄位以該來源為準；可包含 skipped 任務 |

daemon 錯誤物件為 `{"success": false, "error": "訊息"}`；Rust 自己產生的 502 只有 `error`。預覽失敗只顯示錯誤；提交收到伺服器拒絕會清除待重試內容並重新預覽。未收到可解析回應時，前端保留相同 body、request_id 與勾選，鎖住選項供重試。

## 共用輸入與副作用

| 邊界 | 執行輸入與責任 | 錯誤與限制 |
| --- | --- | --- |
| 一般偏好與路徑 | `config.py` 讀 `ui_prefs.json`；批次根目錄依 download_dir、`DOWNLOAD_DIR`、工作目錄下 download 決定；`paths.py` 與 metadata 定義資料位置 | 讀取一般偏好失敗回空設定；NAS 設定失敗會拋出 `NasError` |
| NAS 與儲存 | `nas.py` 讀 `CONFIG_DIR` 或產品目錄的 nas.json/nas.key；direct 驗證網路掛載；smb/upload 用 `SmbStorage` 獨立 connection cache 存取遠端 | 預覽可連線及讀取目錄，但不建立批次目錄；退出後解除註冊並關閉連線；虛擬 SMB 根目錄沒有連線時失敗，不退回本機 |
| 批次資料夾 | commit 再核對顯示位置、忙碌狀態與衝突，之後建立資料夾 | 任務新增或持久化失敗只回復新增的任務及佇列，已建立資料夾可能保留；目的地建立失敗也不承諾目錄回復 |
| 佇列與快照 | `add_task(..., defer_start=True)` 建立任務並保存目的地快照；每個 SMB 任務取得獨立虛擬根；批次策略只略過相同影片的未完成任務 | skipped 任務仍會出現在回應與快照。批次提交鎖只涵蓋此管理器，並非跨程序交易 |
| 持久化 | `_save_tasks` 排除 NAS password/password_encrypted/password_saved，寫入 .tmp 並 fsync 後以 os.replace 取代；相同快照不重寫 | 無 state_path 或持久化已停止時直接返回；不代表已儲存磁碟。寫入成功後的啟動派送或回應序列化失敗不在新增任務回復範圍 |
| 識別碼快取 | 相同 request_id 與相同 body 回傳快取結果，不同 body 拒絕；最多保留 100 筆，超過移除最舊一筆 | 僅程序內記憶體，重啟／淘汰後無跨次冪等保證；快取在啟動派送與序列化之後才寫入 |
| 瀏覽器狀態 | localStorage 的 `uav-download-storage` 記住 query/settings；預覽版本號忽略過時回應；成功才移除本次勾選 | 請求仍在等待期間阻止取消；結果不明的請求結束後仍可取消視窗，此時不再送出請求，但不會撤回伺服器可能已建立的任務。重新開啟會清除 pendingRequest 並產生新 request_id，不能再用舊識別碼確認結果；完成提交後的任務取消屬未採用流程 |

NAS 帳密、真實位址、影片與任務狀態檔不是文件輸出。依賴版本宣告納入 manifest 以捕捉檔案變更；浮動版本實際安裝內容及遠端服務狀態需另行驗證。
