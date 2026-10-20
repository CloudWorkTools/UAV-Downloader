# 影匣外部 API 字幕實作工作拆分

> 發布前工作拆分快照。後續執行計畫與任務依 [GitHub 規格 #10](https://github.com/CloudWorkTools/UAV-Downloader/issues/10) 建立，測試以正式規格的瀏覽器操作驗證為主；下方保留原審閱接點，尚未開始實作。

**本文件供規格確認與排程審閱，所有工作尚未開始。**

| 項目 | 內容 |
| --- | --- |
| 狀態 | 待使用者確認，不是實作授權 |
| 規格來源 | [完整功能規格](2026-10-20-external-subtitles-design.md) |
| 程式查核基準 | Git commit `1d4d6993f8b3881f9e9e27a97010d4147352e81e` |
| 正式追蹤 | 確認後依[任務追蹤規則](../../agents/issue-tracker.md)建立或更新 GitHub Issues，本輪不發布 |
| 順序 | W1 → W2 → W3 → W4 → W5 → W6 → W7 → W8 |

本拆分引用規格的產品規則，不另維護第二份需求。新增檔名與責任是實作建議，具體函式介面、DTO 與測試步驟在規格確認後的執行計畫固定。實作者先建立能重現需求的失敗測試，再完成最小變更及相應驗證；不先建通用插件框架或改寫下載核心。

## 1. 模組責任

| 路徑 | 修改性質 | 責任 |
| --- | --- | --- |
| `src/uav_downloader/subtitles/profiles.py` | 建議新增 | 多組具名設定、參數驗證、非秘密快照與既有設定遷移；重用既有秘密保存方式 |
| `src/uav_downloader/subtitles/vad.py` | 建議新增 | 獨立 VAD 的外部請求、區間驗證及明確選用本機 VAD 的接點 |
| `src/uav_downloader/subtitles/asr.py` | 修改 | 沿用音訊轉錄與取消能力，加入設定檔、能力驗證與強制重做語意 |
| `src/uav_downloader/subtitles/settings.py`、`llm.py` | 修改 | 重用文字服務串接，支援校正、翻譯與各服務思考控制 |
| `src/uav_downloader/subtitles/results.py` | 建議新增 | 階段結果版本、輸入相依、保留、過期判定及檔案發布保護接點 |
| `src/uav_downloader/subtitles/engine.py` | 修改 | 串接四階段、原始時間還原及選定輸出的發布，沿用既有底層工具 |
| `src/uav_downloader/daemon/subtitle_tasks.py` | 建議新增 | 字幕專用佇列、設定快照、片段續跑、排序與同影片寫入互斥 |
| `src/uav_downloader/daemon/task_manager.py` | 修改 | 在下載完成點建立字幕工作，保留下載任務生命週期與舊紀錄相容 |
| `src/uav_downloader/core/storage.py`、`daemon/nas.py` | 必要時修改 | 沿用既有檔案與 SMB 抽象；補足影片列檔、SRT 發布與只重試儲存所需能力 |
| `src/uav_downloader/daemon/server.py`、`uav-web/src/routes.rs` | 修改 | HTTP 驗證、設定與字幕操作轉接，不放模型或 SMB 實作 |
| `uav-web/frontend/index.html` | 修改 | 將字幕入口接到獨立表單與任務操作；沿用既有頁面結構 |
| `uav-web/frontend/subtitle-settings.js`、`subtitle-tasks.js` | 建議新增 | 字幕模型設定、任務與結果互動，避免繼續擴大單一 HTML 的業務邏輯 |

設定遷移、結果持久化與佇列都沿用既有檔案式保存，首版不引入資料庫。只有新增契約確有需要才修改 Rust 代理路由，不重做整套 HTTP 代理。

## 2. 可獨立驗收的工作

### W1：模型設定檔、憑證與相容遷移

**成果：** 可保存多組 VAD、ASR 與文字模型設定，並能為各階段選用；既有設定可安全遷移。

**規格：** ch. 5.1、5.4、11。**前置：** 無。

**檔案：** 新增 `subtitles/profiles.py`；調整 `subtitles/asr.py`、`subtitles/settings.py` 的共用保存接點；新增 `tests/test_subtitle_profiles.py`，延伸 `tests/test_translation_settings.py`、`tests/test_asr_api.py`。

- [ ] 驗證名稱、能力、服務類型、HTTP(S) URL、模型及該服務支援的參數；無效設定不覆蓋已保存設定。
- [ ] 沿用既有秘密保存能力，不在快照或公開設定輸出金鑰；服務位址變更不攜帶舊服務金鑰。
- [ ] 遷移既有單組 ASR／翻譯設定，保留原檔與既有值；無法解密時提示補填，不清除原設定。
- [ ] 建立具版本的設定快照與憑證參照；測試修改設定不改變已建立快照。
- [ ] 新安裝預設 LocalAI 服務類型，位址、模型與硬體保持可設定，不改動既有安裝的服務選擇。

**完成證據：** 設定驗證、秘密生命週期、舊資料遷移與快照不變性測試通過。

### W2：四階段服務適配與能力測試

**成果：** LocalAI 可提供獨立 VAD／ASR；三種主要服務可提供文字階段，並清楚區分已確認與未確認能力。

**規格：** ch. 4、5.2、5.3。**前置：** W1。

**檔案：** 新增 `subtitles/vad.py`；修改 `subtitles/asr.py`、`subtitles/llm.py`、`subtitles/settings.py`；新增 `tests/test_vad_api.py`、`tests/test_subtitle_capabilities.py`，延伸 `tests/test_asr_api.py`、`tests/test_llm_translation.py`。

- [ ] 用可檢查實際 HTTP 請求的隔離服務測試 LocalAI VAD、ASR、OpenAI 相容文字，以及必要的 Ollama／LM Studio 專用 HTTP 介面。
- [ ] 測試 base URL 拼接、可選金鑰、來源語言、VAD 樣本格式、segment 時間戳及超界／非數值回應。
- [ ] 將校正與翻譯作為不同操作，保留穩定 ID 與時間；缺漏、重複或額外 ID 不產生有效結果。
- [ ] 依服務與模型送出思考控制；只隱藏思考輸出的模式不算通過，能力未知時阻止要求停用思考的工作。
- [ ] 提供小型音訊／文字的明確相容性測試，不以 `/models` 有列出名稱取代實際契約驗證。
- [ ] 保留使用者明確選用本機 VAD 的接點，缺少平台 runtime 時明示不可用；外部路徑的測試禁止呼叫本機模型準備或下載函式。

**完成證據：** 每個協定有請求與回應斷言，錯誤及取消測試通過；真實服務驗證留在 W8，不以模擬結果聲稱部署相容。

### W3：階段結果與指定階段引擎

**成果：** 可保存與選用各階段結果，強制重做單一階段，正確判斷下游過期。

**規格：** ch. 4、7、8.1、8.2。**前置：** W1、W2。

**檔案：** 新增 `subtitles/results.py`；修改 `subtitles/engine.py`、`subtitles/asr.py` 的快取接點；新增 `tests/test_subtitle_results.py`，延伸 `tests/test_subtitle_engine.py`、`tests/test_subtitle_provenance.py`。

- [ ] 保存版本化結果、上游身分、非秘密設定及分塊完成資訊，原子寫入後才將版本標為成功。
- [ ] 區分首次完整產生、指定階段強制重做與同工作中斷續跑，測試各階段實際呼叫次數。
- [ ] 重做只失效真正依賴它的下游；使用者可明確選現存舊上游，不能宣稱整條流程已最新。
- [ ] 關閉矯正時翻譯使用 ASR，關閉翻譯時不發出文字翻譯呼叫；翻譯語言獨立保存。
- [ ] 實作最新／上一份成功結果、回復及清理；回復不呼叫模型，依選定輸出重新發布，已清理內容的來源仍可辨識，失敗不取代成功結果。
- [ ] 測試分塊接縫、短句、重疊、原始時間偏移、無語音與原影片變更，不累積壓縮時間軸。

**完成證據：** 固定結果相依與時間軸測試通過，可從記錄證明未重跑未指定階段。

### W4：字幕專用佇列、快照與續跑

**成果：** 自動字幕及手動字幕共用可排序、取消、重啟後手動恢復的獨立佇列。

**規格：** ch. 5.4、6.2、6.3。**前置：** W3。

**檔案：** 新增 `daemon/subtitle_tasks.py`；調整 `daemon/task_manager.py`；新增 `tests/test_subtitle_tasks.py`，延伸 `tests/test_daemon_downloads.py`。

- [ ] 下載完成只提交字幕工作，不讓字幕失敗觸發影片重新下載；既有下載紀錄仍可讀取。
- [ ] 預設一支影片執行字幕，同影片寫入互斥，重複提交不產生同一份重複工作。
- [ ] 支援等待順序、取消及部分失敗；中斷工作重啟後等待使用者續跑，已完成片段不重新送 API。
- [ ] 依規格有限次重試暫時性錯誤；驗證、授權或不相容錯誤停止，不自動換服務或模型。
- [ ] 排隊工作可明確套用新設定；已開始的工作保留快照，變更設定需建立新重做工作。
- [ ] 取消後忽略晚到回應，持久化失敗不回報已成功，任務移除不刪影片或字幕。

**完成證據：** 取消、佇列互斥、失敗續跑、服務重啟與舊紀錄相容的回歸測試通過。

### W5：既有影片入口、SRT 保護與 NAS 發布

**成果：** 下載紀錄及已設定儲存根目錄中的影片可單支／批次提交；字幕安全發布至本機或 NAS。

**規格：** ch. 6.1、8.3、8.4。**前置：** W3、W4。

**檔案：** 修改 `daemon/subtitle_tasks.py`、`daemon/task_manager.py`、`subtitles/engine.py`；必要時延伸 `core/storage.py`、`daemon/nas.py`；新增 `tests/test_subtitle_file_selection.py`，延伸 `tests/test_smb_storage.py`、`tests/test_subtitle_provenance.py`、`tests/test_web_download_settings.py`。

- [ ] 提供根目錄內的影片列檔與選取，驗證不存在、無權限、路徑跳脫及符號連結越界；舊任務未開啟字幕也可提交。
- [ ] 任意影片只建立字幕工作，不加入下載工作；批次項目能各自顯示提交或失敗結果。
- [ ] 按所選輸出語言發布對應 SRT；人工修改或來源不明字幕另存，只有明確替換才覆寫。
- [ ] 原子發布、另存名稱衝突、空字幕、NAS 離線與傳送失敗都不破壞既有檔案；回復舊結果也遵守人工保護，發布失敗可單獨重試。
- [ ] 本機、掛載 NAS、原生 SMB 及下載後傳送模式都走既有儲存介面；中間結果不誤當 SRT 傳送。
- [ ] 發布失敗保留成功模型結果，可只重試儲存；測試模型呼叫次數不增加。

**完成證據：** 隔離檔案與 SMB 測試通過，所有失敗案例保留原影片與人工字幕。

### W6：Web API 與 Rust 轉接

**成果：** Web 可操作模型設定、相容測試、影片選取、字幕任務、版本選取及只重試發布。

**規格：** ch. 5-8、10、11。**前置：** W1-W5。

**檔案：** 修改 `daemon/server.py`、`uav-web/src/routes.rs`；延伸 `tests/test_web_download_settings.py`、`tests/test_daemon.py`，新增 `tests/test_subtitle_http.py`。

- [ ] 沿用設定通道暴露去秘密的設定與狀態；為獨立字幕操作增加必要的小型路由轉接，不把新工作假裝成下載。
- [ ] HTTP 邊界驗證影片來源、階段、語言、結果版本、排序與替換意圖，失效的輸入不排入執行。
- [ ] 明確傳遞強制重做、續跑、選舊版本、恢復結果、套用新設定與只重試儲存等不同意圖；恢復結果回應分別呈現採用版本與發布狀態。
- [ ] 測試錯誤狀態碼、部分批次結果、重複送出與憑證遮蔽；代理層不繞過 daemon 的限制。

**完成證據：** 以 HTTP 端到端測試證明各操作可達正確工作與結果；API 回應不含秘密。

### W7：Web 設定、任務與結果介面

**成果：** 使用者可理解所需模型契約、選影片及階段，看到重做影響與每個結果的來源。

**規格：** ch. 5-8、10。**前置：** W6。

**檔案：** 修改 `uav-web/frontend/index.html`；建議新增 `subtitle-settings.js`、`subtitle-tasks.js`；延伸 `tests/web_settings.cjs`、`tests/task_menu.cjs`，新增 `tests/subtitle_tasks.cjs`。

- [ ] 提供四階段設定檔選擇、後矯正／翻譯開關，以及服務實際支援的參數與契約說明。
- [ ] 明示「未確認思考控制」、需至服務端修改的參數及相容性測試會呼叫外部服務。
- [ ] 支援下載紀錄／根目錄選檔、單支／批次、工作排序、取消與等待續跑。
- [ ] 重做前顯示輸入版本、模型設定與下游影響；過期、缺少輸入及明確選舊版本均可操作。
- [ ] 可查看矯正前後、最新／上一份結果，並區分模型失敗與發布失敗；人工字幕替換有明確操作。
- [ ] 用真實 DOM 驗證窄畫面、空資料、焦點、鍵盤操作、按鈕尺寸、重複送出及失敗後重試。

**完成證據：** 真實 DOM 測試通過，實際版本畫面可完成規格中的主要流程。

### W8：整合、模型品質、文件與部署驗證

**成果：** 離線回歸、正式映像與專用環境驗證各有證據，使用者可以依操作說明完成字幕工作。

**規格：** ch. 9-13。**前置：** W7；真實服務驗收另需模型服務與代表片段。

**檔案：** 修改 `scripts/check_web.sh`、`docs/web-ui.md`、`docs/README.md`、`README.md`；新增去識別的 `docs/assets/` 截圖。正式映像／部署測試僅延伸必要入口，不為此改造 CI 架構。

- [ ] 將新增 Web Python 測試納入集中入口，並納入此次 Web 新使用的文字設定／翻譯回歸測試；前端 `.cjs` 依既有入口自動執行。
- [ ] 執行離線測試與正式映像目的專屬整合測試，覆蓋四階段、單階段重做、取消／重啟及 NAS 發布保護。
- [ ] 在指定服務版本逐一測 LocalAI VAD／ASR、三種服務文字路徑與思考控制，記錄模型、後端、量化與模板資訊。
- [ ] 依規格 ch. 9 比較代表片段並選定實際模型設定；任何尚缺硬體或樣本的項目標示未完成。
- [ ] 部署只用既有遠端流程，在獨立暫存資料驗證完整與單階段工作；失敗依既有部署機制回復服務。
- [ ] 更新說明與實際版本截圖，隱去影片內容、真實位址、私人路徑與憑證；正式資料不進測試輸出或 Git。

**完成證據：** 記錄版本、執行命令、結果與未驗證範圍。修改集中 CI 入口時，依貢獻指南提供當次目的專屬正式映像與生產驗證證據，不能以模擬模型代替。

## 3. 驗證入口與測試範圍

下列為既有入口或計畫中的驗證命令，本次文件工作未執行產品測試。安裝需求依 [README](../../../README.md#開發與檢查)；新增測試完成且環境準備好後才執行。

```bash
PYTHON=.venv/bin/python bash scripts/check_web.sh
```

開發中至少覆蓋既有相鄰行為，限定測試的例子如下：

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_asr_api.py tests/test_subtitle_engine.py tests/test_subtitle_provenance.py tests/test_llm_translation.py tests/test_translation_settings.py tests/test_daemon_downloads.py tests/test_web_download_settings.py tests/test_smb_storage.py
```

W1-W7 各自執行列出的新增測試，再執行集中入口。若修改 Rust 路由，另執行 `cargo test --manifest-path uav-web/Cargo.toml`。前端驗證需可用 Chrome／Chromium，不以缺少瀏覽器為通過。

正式映像沿用 `bash scripts/check_web_container.sh IMAGE` 與 `bash scripts/check_smb_batch_container.sh IMAGE`，並補上本功能專屬的整合驗證。這些既有入口本身不能證明新模型的辨識品質。

遠端部署使用 `python3 scripts/deploy_remote.py`；本機 Docker 僅建置與短期隔離驗證。CI runner、外部 fork 分流、正式資料保護與生產證據依 [CONTRIBUTING.md](../../../CONTRIBUTING.md#ci-與生產環境驗證)，本稿不另設相衝突規則。

## 4. 工作與規格對照

| 規格範圍 | 主要工作 | 必須留下的可驗證成果 |
| --- | --- | --- |
| 模型設定、憑證、快照、遷移 | W1、W4、W6、W7 | 設定遷移與快照測試、去秘密 API、設定操作 |
| 外部四階段、相容契約、思考控制 | W2、W3、W7、W8 | 請求斷言、能力錯誤提示、真實服務驗收 |
| 結果版本、過期、指定重做與語言 | W3、W6、W7 | 上游關係與 API 呼叫次數測試、版本選取 |
| 佇列、取消、有限重試、重啟續跑 | W4、W6、W7 | 佇列持久化與晚到回應測試、可操作的續跑入口 |
| 舊影片、批次、NAS 與人工字幕保護 | W5、W6、W7 | 根目錄驗證、SRT 不覆寫、只重試發布與 SMB 測試 |
| 聽錯、幻覺、時間與文字保真 | W2、W3、W8 | 固定技術案例、代表片段比較及未驗證範圍 |
| 文件、正式映像與實際環境 | W8 | 操作說明、去識別截圖、分開記錄的驗證證據 |

## 5. 確認後的處理

使用者確認完整規格與本拆分後，先搜尋既有 GitHub Issues，避免重複建立。正式需求放入一個 epic，W1-W8 視既有議題合併或拆成可獨立驗收的工作；每項連回正式規格並記錄依賴，不複製另一份產品規則。

接著建立具體執行計畫，固定資料結構、HTTP 契約、函式介面與逐項驗證。未取得使用者對規格的確認前，不建立產品程式、安裝模型、執行部署或聲稱驗收通過。
