# 外部字幕 API 實作計畫

> **代理執行者必讀：** 使用 subagent-driven development 執行此工作圖。每項工作都連到 GitHub issue，須各自完成審查與驗證，再合併至整合分支。

**目標：** 讓使用者從 Web 操作四個可獨立設定的外部字幕階段、重用具版本的結果、指定階段重做，並安全發布 SRT。

**架構：** 服務設定與憑證由字幕設定模組及服務適配器管理；階段結果與字幕佇列各自放在專用模組，再接入既有 daemon。Web 與 Rust 代理提供已驗證的操作；主要驗收由瀏覽器操作實際 Web、daemon 與隔離的 HTTP 模型服務完成。

**技術：** Python 3.12、既有 `requests` 與檔案式保存、Rust Web 代理、既有 HTML／JavaScript、Node.js、Chrome headless、正式 Docker 映像。

**規格：** [GitHub issue #10](https://github.com/CloudWorkTools/UAV-Downloader/issues/10)；工作拆分快照：`docs/superpowers/specs/2026-10-20-external-subtitles-work-breakdown.md`。

## 全域限制

- 四個階段為 VAD、ASR、原文後矯正及字幕翻譯；原文後矯正與字幕翻譯可以關閉。
- 預設服務類型為 LocalAI；Ollama 與 LM Studio 是否支援某階段，依該階段實際能力判斷，不以「OpenAI 相容」推定。
- 每個階段都須支援外部 API 路徑；此路徑不得下載或載入本機模型權重。
- ASR 必須回傳以秒為單位的片段起始、結束時間與文字；不得猜測或推算時間單位。
- 使用者要求停用推理時，若服務能力未知就必須阻止執行；隱藏推理輸出不算停用推理。
- 階段重做只執行指定階段與指定翻譯語言；下游結果標示過期，不自動執行。
- 每階段及語言保留最新與上一份成功結果；失敗或取消的工作不得取代成功結果。
- 提交或執行中的工作使用不可變、非秘密的設定快照。憑證分開保護，並綁定服務位址。
- 允許通過驗證的 HTTP(S) 模型服務位址，包括私有網路端點。沿用 ASR 的 URL 驗證與憑證綁定位址行為；含憑證的請求停用重新導向，且不得經由下載 proxy 傳送。
- 保留舊設定、任務紀錄、已下載媒體，以及人工編輯或來源不明的字幕檔。
- 沿用檔案式保存與既有儲存介面；不新增資料庫或通用外掛框架。
- Web 驗收使用 Chrome 操作實際 Web／daemon 流程與隔離的 HTTP 模型服務；只有前端 stub 不足以通過。
- 離線測試、正式映像測試、生產環境驗證分開記錄。缺少模型主機、版本或樣本不阻擋本地實作，但在完成實測前不得宣稱模型品質驗收通過。
- 若修改 CI，遵守 `CONTRIBUTING.md` 的 runner 與 fork 規則，並留下該功能專屬的正式映像及部署證據。

## 審查重點

- 私有網路模型端點、變更憑證位址、重新導向與 proxy 環境變數：W1／W2 的 API fixture 檢查及 W7 瀏覽器流程，證明只有設定的 HTTP(S) 請求會收到憑證。
- VAD／ASR／文字服務回傳格式錯誤或內容不完整，包括無效時間戳及遺漏、重複、額外片段 ID：W2 拒絕回應；W7 確認不會發布無效結果。
- 上游結果過期或不存在、選用舊版本，以及相同設定下強制重做：W3 契約檢查與 W7 瀏覽器流程，確認依賴關係及只呼叫指定階段。
- 取消、重啟、晚到回應及重複提交：W4 daemon 檢查與 W7 瀏覽器流程，確認原媒體與結果仍保留，且只續跑未完成工作。
- 無語音、人工修改 SRT、檔名衝突及儲存故障：W5 儲存檢查與 W7 瀏覽器流程，確認空結果可正確處理，既有檔案受到保護。

---

## 檔案結構與共用契約

除非既有模組無法滿足需求，產品程式只新增以下模組：

- 新增 `src/uav_downloader/subtitles/profiles.py`：設定檔驗證、遷移、秘密參照與不可變的非秘密快照。
- 新增 `src/uav_downloader/subtitles/vad.py`：外部 VAD 適配器與區間驗證。
- 新增 `src/uav_downloader/subtitles/results.py`：階段結果版本、依賴新鮮度、保留及原子保存。
- 新增 `src/uav_downloader/daemon/subtitle_tasks.py`：字幕專用佇列、設定快照、取消、續跑及同影片寫入鎖。
- 修改 `src/uav_downloader/subtitles/asr.py`、`settings.py` 與 `engine.py`，重用既有服務及字幕處理能力。
- 僅修改 `src/uav_downloader/daemon/task_manager.py` 與 `server.py`，在既有完成節點提交字幕工作，並驗證或轉送字幕操作。
- 僅在新路由需要經過代理時修改 `uav-web/src/routes.rs`。
- 修改 `uav-web/frontend/index.html`；新增 `subtitle-settings.js` 與 `subtitle-tasks.js`，管理字幕專用的瀏覽器狀態及操作。
- 新增 `tests/subtitle_workflow.cjs`，啟動或連接實際 Web、daemon 及本機隔離 HTTP 模型 fixture；擴充既有 Python API、保存、儲存及取消測試，只涵蓋不適合由瀏覽器穩定斷言的狹窄案例。
- 將新增 Python 測試加入 `scripts/check_web.sh`。既有入口會自動發現 `.cjs` 測試。

由 W1-W3 負責、供 W4-W7 使用的共用 Python 契約：

```python
Stage = Literal["vad", "asr", "correction", "translation"]
ProfileKind = Literal["vad", "asr", "text"]

@dataclass(frozen=True)
class StageProfile:
    profile_id: str
    display_name: str  # user-visible name for a named profile
    kind: ProfileKind  # one text profile may serve correction and translation
    provider: str
    capabilities: tuple[Stage, ...]
    base_url: str
    model: str
    parameters_json: str  # canonical serialized scalars, immutable
    credential_ref: str | None

@dataclass(frozen=True)
class ProfileSnapshot:
    profile_json: str  # canonical deep copy; no secret value
    credential_ref: str | None
    snapshot_id: str

@dataclass(frozen=True)
class SpeechInterval:
    start: float
    end: float

@dataclass(frozen=True)
class SubtitleSegment:
    segment_id: str
    start: float
    end: float
    text: str

@dataclass(frozen=True)
class StageResult:
    result_id: str
    video_id: str
    stage: Stage
    language: str | None
    input_result_ids: tuple[str, ...]
    profile_snapshot: ProfileSnapshot
    intervals: tuple[SpeechInterval, ...] | None  # set only for VAD
    segments: tuple[SubtitleSegment, ...] | None  # set only for ASR/correction/translation
```

`profiles.load_profiles() -> list[StageProfile]`；`profiles.save_profile(profile, api_key=None) -> StageProfile`；`profiles.snapshot(profile_id, stage) -> ProfileSnapshot`。驗證所選階段存在於 `capabilities`。只有在適配器即將送出請求時，才解析秘密值。保存與建立快照時使用 canonical JSON 複製設定，不與佇列工作共用任何可變巢狀資料。

`vad.detect_speech(audio_path, profile_snapshot, cancel_check) -> list[SpeechInterval]`；`asr.transcribe_stage(audio_path, intervals, profile_snapshot, language, cancel_check) -> list[SubtitleSegment]`。保留既有公開簽章 `asr.transcribe(settings, wav_path, output_base, log_path, cancel_check=None, progress_callback=None)`，供桌面版與 Web 呼叫端繼續使用。`engine.transform_segments(operation, segments, profile_snapshot, target_language=None, allow_reasoning=False, cancel_check=None) -> list[SubtitleSegment]`。原文後矯正與字幕翻譯須保留輸入 ID 與時間邊界。

`results.commit_success(result) -> StageResult` 原子建立成功版本；`results.latest(video_id, stage, language=None) -> StageResult | None` 取得最新版本；`results.mark_dependents_stale(result_id) -> list[str]` 標示下游過期。任務模組不得讓執行中的結果成為可選用的成功版本。

`subtitle_tasks.submit(video_id, selected_stages, profile_snapshots, target_languages) -> TaskSnapshot`；`subtitle_tasks.redo(video_id, stage, profile_snapshot, language=None, input_result_id=None) -> TaskSnapshot`；`subtitle_tasks.cancel(task_id) -> None`；`subtitle_tasks.resume(task_id) -> TaskSnapshot`。既有 daemon 任務介面保持不變，供下載呼叫端使用。

HTTP 路由前綴為 `/api/subtitles`：`GET/PUT /profiles`、`POST /profiles/test`、`GET /videos`（清單回傳 `partial` 與安全 `notice`，掃描受項目數及經過時間預算限制）、`GET/POST /tasks`、`GET /tasks/{task_id}` 與 `POST /tasks/{task_id}/action`。任務 action JSON 的 `action` 為 `cancel | resume | reorder | apply_new_settings | redo | retry_publish`；僅在該操作需要時使用 `position`、`profile_ids`、`profile_id`、`stage`、`language`、`input_result_id` 與 `publish_status`。`GET /videos/{video_id}/results` 列出階段結果版本及 `stale`／`adopted` 狀態；`GET /videos/{video_id}/results/{result_id}` 回傳區間或字幕片段、上游版本及建立時的非秘密設定快照；`GET /videos/{video_id}/results/{result_id}/content` 一律回傳 JSON `{content_type, filename, content}`。文字結果的 `content` 是 SRT 字串；VAD 結果的 `content` 是 `{start, end}` 區間陣列。Web 以 Blob 顯示或下載該內容，不把內容端點視為原始 HTTP 媒體回應。結果 action 的 `publish` 使用 `output_language` 並要求明確布林值 `replace_existing`；`restore` 只選上一個版本，發布失敗可用 publication ID 再送 `retry_publish`，只重試儲存。下載提交可帶 `subtitle_request`（階段、profile IDs、語言及思考選項）；daemon 在接受下載前驗證並拍下設定快照，沿用下載完成後的既有字幕提交 hook。

`POST /profiles/test` 必須帶 `confirm_external_call: true`；相容性探測實際呼叫所選 adapter 的微型音訊／文字請求，回報 `format_pass`、`reasoning_control`（`supported | unknown | unsupported`）與 `quality: unverified`。只有另帶 `allow_reasoning: true` 才允許文字測試以思考模式送出。自動模型清單失敗時仍允許手動輸入。字幕 JSON POST/PUT 必須使用 `application/json`，daemon 與 Rust 各自設 1 MiB 請求上限；Rust 保留 daemon 狀態碼與 JSON body，且只代理清單內方法和路徑。即使 Rust 已驗證過，daemon 仍須再次驗證。

LM Studio 模型清單使用官方 REST `GET /api/v0/models` 的 `{object: "list", data: [{id, type, ...}]}` 格式；只列出 `llm`／`vlm`（或沒有 `type` 的舊條目），不猜測 `/api/v1/models` 原生清單格式。清單讀取失敗時仍保留手動輸入模型名稱。格式依 [LM Studio REST endpoints](https://lmstudio.ai/docs/developer/rest/endpoints) 文件核對。

恢復上一版本只改變該階段選用的版本，不會呼叫模型。VAD 可恢復選取但不發布 SRT；ASR／原文矯正亦不預設發布日文 SRT。只有明確提供 `output_language` 才發布：ASR／矯正只接受 `ja`，翻譯只接受該版本的目標語言；未選輸出時回報 `publish_status: not_requested`。

## 工作 1：W1，設定檔、憑證與遷移（#11）

**檔案：** 新增 `subtitles/profiles.py`、`tests/test_subtitle_profiles.py`；修改 `subtitles/asr.py`、`subtitles/settings.py`；擴充 `tests/test_asr_api.py` 與 `tests/test_translation_settings.py`。

- [x] 在既有設定／API 接縫新增失敗測試，涵蓋設定檔驗證、相同位址保留憑證、位址變更後清除憑證、遷移及遷移失敗；此層不宣稱涵蓋瀏覽器流程。
- [x] 實作 `StageProfile`、`ProfileSnapshot`、`load_profiles`、`save_profile` 與 `snapshot`；遷移失敗時保留來源檔。
- [x] 儲存及建立快照時深度複製／canonicalize 設定參數；測試修改輸入或讀回資料不會改變佇列快照。
- [x] 快照及公開 JSON 不含秘密；依 ASR 規則接受通過驗證的 HTTP(S) 位址，並將憑證綁定至該位址。
- [x] 執行設定／API 與遷移測試；提交 W1，並由整合 PR #19 關聯 #11；合併後關閉 issue。

## 工作 2：W2，服務適配器與能力驗證（#12）

**檔案：** 新增 `subtitles/vad.py`、`tests/test_vad_api.py`、`tests/test_subtitle_capabilities.py`；修改 `subtitles/asr.py`、`subtitles/llm.py`、`subtitles/settings.py`；擴充 `tests/test_asr_api.py` 與 `tests/test_llm_translation.py`。

- [x] 使用隔離的 HTTP fixture 新增失敗的 adapter／API 測試，並斷言實際請求與能力結果。
- [x] 依共用契約實作 VAD 區間、ASR 片段及文字操作；驗證 URL 組合、有限秒數、片段 ID、服務專用推理控制、取消及回應大小上限。
- [x] 含憑證的請求停用重新導向及環境 proxy；允許使用已設定的私有網路端點。
- [x] 驗證 adapter／API 與格式錯誤回應；瀏覽器驗收集中於 W7/W8。提交 W2，並由整合 PR #19 關聯 #12；合併後關閉 issue。

## 工作 3：W3，階段結果與指定階段引擎（#13）

**檔案：** 新增 `subtitles/results.py`、`tests/test_subtitle_results.py`；修改 `subtitles/engine.py`；擴充 `tests/test_subtitle_engine.py` 與 `tests/test_subtitle_provenance.py`。

- [x] 在現有 API 接縫新增失敗的引擎／結果測試，涵蓋啟用階段、相同設定強制重做、明確選擇舊上游及回復結果。
- [x] 依共用契約實作原子結果版本、最新／上一份保留、輸入連結、指定輸出發布及下游過期標記。
- [x] 分塊時保留原始時間戳；文字階段保留 ID 與時間邊界；停用的階段不得呼叫外部服務。
- [x] 斷言只呼叫指定重做階段／語言，且失敗／取消後仍可選用先前版本。完整瀏覽器流程在 W7/W8 驗收。提交 W3，並由整合 PR #19 關聯 #13；合併後關閉 issue。

## 工作 4：W4，字幕佇列、快照與續跑（#14）

**檔案：** 新增 `daemon/subtitle_tasks.py`、`tests/test_subtitle_tasks.py`；修改 `daemon/task_manager.py`；擴充 `tests/test_daemon_downloads.py`。

- [x] 新增失敗的 daemon／API 測試，涵蓋佇列排序、重複提交、取消、重啟及明確續跑。
- [x] 使用不可變快照實作 `submit`、`redo`、`cancel` 與 `resume`；同影片寫入序列化、未完成片段進度保存，取消後忽略晚到回應。
- [x] 下載完成時提交字幕工作，不改變下載生命週期；字幕失敗不得重新下載影片。
- [x] 驗證續跑只處理未完成片段，保存失敗不得回報成功。完整瀏覽器流程在 W7/W8 驗收。提交 W4，並由整合 PR #19 關聯 #14；合併後關閉 issue。

## 工作 5：W5，影片選取與安全 SRT／NAS 發布（#15）

**檔案：** 修改 `daemon/subtitle_tasks.py`、`daemon/task_manager.py`、`subtitles/engine.py`；必要時才擴充 `core/storage.py` 或 `daemon/nas.py`；新增 `tests/test_subtitle_file_selection.py`；擴充 `tests/test_smb_storage.py`、`tests/test_subtitle_provenance.py` 與 `tests/test_web_download_settings.py`。

- [x] 新增失敗的 task／storage／API 測試，涵蓋選取舊下載與允許根目錄中的影片、發布 SRT 及重試失敗的發布。
- [x] 驗證根目錄、符號連結、不存在的檔案及權限；選片只建立字幕工作。
- [x] 透過既有儲存介面原子發布選定語言 SRT；保護人工修改或來源不明的檔案，未明確替換時另存。
- [x] 驗證隔離的本機路徑，以及可用的掛載／NAS／SMB 路徑；確認重試儲存不會新增模型請求。完整瀏覽器流程在 W7/W8 驗收。提交 W5，並由整合 PR #19 關聯 #15；合併後關閉 issue。

## 工作 6：W6，字幕 HTTP API 與 Rust 代理（#16）

**檔案：** 修改 `daemon/server.py`、`uav-web/src/routes.rs`；新增 `tests/test_subtitle_http.py`；擴充 `tests/test_web_download_settings.py` 與 `tests/test_daemon.py`。

- [x] 透過現有路由接縫新增失敗的 HTTP 測試，涵蓋設定檔、影片、任務、重做、回復及重試發布操作。
- [x] 實作前述 `/api/subtitles` 路由；在 daemon 驗證階段、語言、ID、影片是否位於允許根目錄、明確替換及批次各項結果。
- [x] 只新增必要的 Rust 轉送；daemon 保持驗證權威，回應與錯誤不得含秘密。
- [x] 驗證 HTTP 狀態碼與回應本文；修改 Rust 時執行 `cargo test --manifest-path uav-web/Cargo.toml`。完整瀏覽器流程在 W7/W8 驗收。提交 W6，並由整合 PR #19 關聯 #16；合併後關閉 issue。

## 工作 7：W7，瀏覽器設定、任務與結果（#17）

**檔案：** 修改 `uav-web/frontend/index.html`；新增 `subtitle-settings.js`、`subtitle-tasks.js`、`tests/subtitle_workflow.cjs`；擴充 `tests/web_settings.cjs` 與 `tests/task_menu.cjs`。

- [x] 使用實際 Web／daemon 程序與隔離 HTTP fixture 建立 Chrome 流程：設定 → 選片 → 提交 → 查看進度 → 重做指定階段 → 取消／續跑 → 檢視／發布結果。
- [x] 顯示設定檔能力與推理狀態、階段／語言選擇、佇列操作、過期依賴、原文與矯正後結果、先前版本，以及模型結果與發布狀態的區別。
- [x] 依固定路由支援檢視／下載、回復、明確替換、佇列排序、套用新設定及重試發布。
- [x] 使用 Chrome 驗證窄畫面、鍵盤、空清單、背景進度、重複提交與失敗後重試。提交 W7，並由整合 PR #19 關聯 #17；合併後關閉 issue。

## 工作 8：W8，整合、品質、文件與部署證據（#18）

**檔案：** 修改 `scripts/check_web.sh`、`README.md`、`docs/README.md`、`docs/web-ui.md`；新增去識別截圖至 `docs/assets/`；僅擴充此功能所需的正式映像驗證入口。

- [x] 確認瀏覽器流程測試及必要的 Python 契約測試會透過 `scripts/check_web.sh` 執行，也會在正式 Web 測試映像執行。
- [x] 執行離線回歸及此功能專屬的正式映像整合流程，涵蓋階段重做、重啟／續跑、取消、安全 SRT 發布及儲存重試。
- [x] 僅在服務與模型版本已確認時記錄真實 LocalAI VAD／ASR 與 LocalAI／Ollama／LM Studio 文字服務相容性；否則列為未驗證。模型品質須使用固定代表音訊及參考文字，fixture 結果不能作為品質驗收。
- [x] 更新使用說明，並從執行中的版本擷取畫面；隱去私人媒體、位址、路徑與憑證。
- [ ] 生產環境部署驗證尚未執行；隔離候選主機計畫待核准，不能取代 live 驗證。若之後執行，只透過 `python3 scripts/deploy_remote.py`，並獨立記錄結果；不得以離線或容器測試取代。
- [ ] Standards／Spec 與整體程式審查的發現已處理；PR #19 仍是 draft，issue #18 尚未關閉。整合 PR 保持可供審查，不合併至預設分支。

## 自我檢查

- **規格涵蓋：** W1-W8 已分配設定、遷移、秘密、四個適配器、推理能力、階段版本、過期狀態、指定重做／回復、佇列／取消／重啟、影片選取、安全 SRT／NAS 發布、HTTP／Rust、瀏覽器介面、文件、正式映像與生產驗證。
- **步驟檢查：** W1-W6 從當時可用的設定、adapter、engine、daemon、storage 或 HTTP 接縫開始驗證。W7/W8 負責首個完整的真實瀏覽器流程及最終瀏覽器驗收。
- **型別一致：** `Stage`、`StageProfile`、`ProfileSnapshot`、`SpeechInterval`、`SubtitleSegment`、`StageResult`、adapter、結果保存、任務及 HTTP 名稱在各工作中維持一致。
- **審查重點：** 上述五類未涵蓋輸入都列出瀏覽器或 fixture 斷言及負責工作。
- **篇幅檢查：** 計畫固定檔案責任與跨工作介面，沒有重複 #10 的產品規格，也沒有規定函式內部實作方式。

### 最終驗收證據（2026-10-20）

- 來源 SHA：`f4ef4b195d6224955937e80820b3dd5b8f562bec`；正式產品映像 `sha256:769bb7b4400318680aa19287af605028b89aa319c4b7e30e38a867214d2ea80e`；正式唯讀 CI 測試映像 `sha256:a4745463c5d21fdcc837d91c3b0887fb93c4726b3d37ce01af222197f0056843`。
- `scripts/check_web.sh` 主機與唯讀來源正式映像各通過 866 個 Python 測試（3 個略過），且所有 CJS／Chrome 流程通過。正式流程包含成功的 legacy startup migration fixture 請求、`retry_wait` 取消、發布重試清除舊錯誤。
- `scripts/check_web_container.sh` 通過；`scripts/check_smb_batch_container.sh` 以真實 Samba LIST 來源、直接發布及 upload alias 發布通過，且保留 staging。未掛載真實 NAS。
- 新截圖：`docs/assets/web-independent-subtitles-desktop.png`、`docs/assets/web-independent-subtitles-mobile.png`。實際 LocalAI／Ollama／LM Studio 服務及模型品質尚未驗證。
- 生產 live rollout 尚未執行；隔離部署主機候選計畫仍待核准。PR #19 維持 draft，issue #18 未關閉。
