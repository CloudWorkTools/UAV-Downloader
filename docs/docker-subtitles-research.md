# Docker 字幕生成與外接語音辨識 API 研究

Docker 可以產生字幕。建議為 UAV 新增獨立的語音辨識 API 設定，優先接自架 Speaches，再沿用字幕翻譯及 SRT 寫入流程。
模型服務可以部署在同一台 Docker 主機，也可以放在另一台有 GPU 的電腦。
實際使用方式見 [Docker AI 字幕](web-ui.md#docker-ai-字幕)；以下現況與替代方案記錄研究時的查核。

本研究檢查 `src/uav_downloader/subtitles/`、daemon 任務流程及 Docker 建置設定。
外部文件於 2026-10-15 查閱；「現況」來自程式，「來源」來自官方文件，「建議」尚待實作及實測。

## 現況與失敗原因

錯誤來自 [task_manager.py](../src/uav_downloader/daemon/task_manager.py) 的 `TaskManager._run_subtitles`。
該方法在 `os.name != "nt"` 時直接拋出使用者看到的錯誤，影片下載成功仍保留。

只刪除此判斷無法完成支援。還有以下限制：

- [engine.py](../src/uav_downloader/subtitles/engine.py) 的 `_prepare_runtime_locked` 下載並驗證 Windows whisper.cpp 套件。
- 預設 ReazonSpeech 封裝包含 `sherpa-onnx-offline.exe` 及 ONNX Runtime DLL，辨識前也依賴 whisper.cpp 的 Windows VAD 執行檔。
- [requirements-docker.txt](../requirements-docker.txt) 未列入本機字幕翻譯依賴；[Dockerfile.web](../Dockerfile.web) 使用此檔安裝 Python 依賴。
  桌面版的 [requirements.txt](../requirements.txt) 才包含 CTranslate2、SentencePiece 及 OpenCC 等套件。
- 現有 [settings.py](../src/uav_downloader/subtitles/settings.py) 和 [llm.py](../src/uav_downloader/subtitles/llm.py) 的 API URL 用於文字翻譯。
  OpenAI-compatible 路徑呼叫 `/chat/completions`，沒有把音訊送往辨識 API。
- [Web 設定頁](../uav-web/frontend/index.html) 提供字幕開關及語言，尚未提供 ASR API URL、模型及金鑰設定。

辨識與翻譯需要分開：ASR 將音訊轉成附時間戳的原文；翻譯模型只改文字，字幕仍使用原有時間戳。
現有 [generate_subtitles](../src/uav_downloader/subtitles/engine.py) 已分開處理辨識、翻譯、來源紀錄和檔案寫入，可沿用這個分工。

## 可行方案

| 方案 | 官方依據 | 適用情境 | 整合成本與限制 |
| --- | --- | --- | --- |
| 外接 Speaches API | [Docker 安裝](https://speaches.ai/installation/)、[語音辨識 API](https://speaches.ai/usage/speech-to-text/) | NAS 負責下載，另一台主機辨識；也可用同機獨立容器 | 新增 ASR 設定及 HTTP 呼叫；仍須翻譯服務及長音訊處理 |
| UAV 容器直接用 faster-whisper | [官方 README](https://github.com/SYSTRAN/faster-whisper) | 希望單一容器處理，主機有足夠 CPU 或 GPU 資源 | 新增原生依賴、模型快取及辨識分支；模型記憶體與下載服務共用 |
| 移植現有 sherpa-onnx／whisper.cpp | [sherpa-onnx 平台支援](https://k2-fsa.github.io/sherpa/onnx/index.html)、[whisper.cpp](https://github.com/ggml-org/whisper.cpp) | 希望繼續使用現有模型及辨識策略 | 需準備 Linux runtime、驗證封裝、VAD 及 fallback；不能直接沿用 Windows 執行檔 |

faster-whisper 提供 CPU INT8、GPU 推論、片段起訖時間及 Silero VAD。
CPU 可運作，不代表 NAS 能即時完成長片；尚無本專案硬體的效能量測。
其目前 GPU 安裝文件要求 CUDA 12 的 cuBLAS 與 cuDNN 9，部署前需核對版本相容性。
來源：[faster-whisper requirements 與 usage](https://github.com/SYSTRAN/faster-whisper)。

sherpa-onnx 官方支援 Linux，文件列出 x64、arm64 CPU 安裝。
因此現有演算法有移植可能，但官方平台支援不能證明本專案封裝已能在 Linux 使用。
來源：[Linux 安裝](https://k2-fsa.github.io/sherpa/onnx/install/linux.html)。

## 建議的外接 API 分工

建議先使用 Speaches 的 OpenAI-compatible ASR 介面，並保持翻譯供應商獨立。
同一個 URL 不能假定同時提供音訊辨識和文字翻譯；只有 `/chat/completions` 的服務不足以完成此流程。

1. UAV 用現有 FFmpeg 流程擷取音訊。
2. UAV 將音訊分段傳至 ASR API，取得日文文字及各片段時間戳。
3. 翻譯階段沿用現有文字 API，或另外補齊 Linux 本機翻譯依賴。
4. UAV 保留絕對時間，寫入所選語言的 sidecar SRT。

Speaches 官方提供 `latest-cpu` 和 `latest-cuda` 映像，模型快取可掛載 volume。
正式整合建議固定已測試的映像版本或 digest，避免隨浮動標籤改變行為。
來源：[installation](https://speaches.ai/installation/)。

以下是待整合的 API 介面示例，並非目前 UAV 已支援的設定：

| 項目 | 示意值 |
| --- | --- |
| API base URL | 同一 Docker 網路：`http://speaches:8000/v1`；跨主機：由管理者指定 HTTPS 網址 |
| 路徑 | `POST /audio/transcriptions`，組合後為 `/v1/audio/transcriptions` |
| 上傳方式 | multipart 的 `file` 音訊檔 |
| 模型 | 服務端已安裝的多語 Whisper 模型，例如 `Systran/faster-whisper-small` |
| 來源語言 | `language=ja`，表示日文音訊 |
| 回傳格式 | `response_format=verbose_json`、`timestamp_granularities[]=segment` |
| 必要回傳內容 | `segments` 內的 `start`、`end`、`text`，時間單位為秒 |
| 驗證 | 服務設定 `API_KEY` 時使用 Bearer token |

來源：[使用方式](https://speaches.ai/usage/speech-to-text/)、[辨識 router 原始碼](https://github.com/speaches-ai/speaches/blob/master/src/speaches/routers/stt.py)、[設定](https://speaches.ai/configuration/)。
Speaches 也支援直接回傳 SRT／VTT；建議使用片段 JSON，方便沿用 UAV 的驗證、翻譯及時間校正。

日文音訊應選多語模型，避免 `.en` 英文專用模型。
Whisper 的音訊翻譯任務以英文為目標，不能把 `language=zh` 當成日文翻繁中的指令。
繁中仍交由文字翻譯階段處理。
來源：[Whisper command-line usage](https://github.com/openai/whisper#command-line-usage)。

### 網路、金鑰與長片限制

Docker 容器內的 `localhost` 指該容器本身；獨立模型容器應使用同網路服務名稱，跨主機則使用可達的主機網址。
現有翻譯 URL 驗證僅允許 loopback 使用 HTTP，會拒絕 `http://ollama:11434/v1` 等 Docker 服務網址。
若要接區網 HTTP，建議新增明確的受信任端點設定，或使用 HTTPS，保留現有 URL 驗證。
此限制來自 [settings.py 的 `_normalize_base_url`](../src/uav_downloader/subtitles/settings.py)。

Linux 不能沿用 Windows DPAPI 金鑰保護。
現有翻譯模組可讀取 `UAV_TRANSLATION_API_KEY` 等環境變數；新增 ASR 金鑰應分開處理，且不回傳前端或寫入日誌。
來源：[settings.py](../src/uav_downloader/subtitles/settings.py)。

Speaches 的 [音訊解碼實作](https://github.com/speaches-ai/speaches/blob/master/src/speaches/dependencies.py)會將音訊解碼至記憶體，不能假設整部長片上傳沒有成本或上限。
建議採有界分段、保留來源偏移、合併邊界重複內容，並處理逾時、重試及取消。
第三方供應商的上傳限制必須逐一確認，不能由「OpenAI-compatible」推定一致。

外接另一台主機時，音訊會傳到該主機；雲端服務則會接收音訊，與目前只外傳字幕文字的行為不同。
同機自架服務可讓音訊留在自有主機，仍須限制 API 存取。

## 實作與驗收範圍

建議最小版本先完成「外接 ASR API + 既有文字翻譯 API」，保留 Windows 本機辨識。
這可避免第一次整合就同時處理 Linux 原生辨識及本機翻譯封裝，但需要可用的模型服務。
若要求完全離線，再評估同機 Speaches 加本機翻譯，或移植既有 runtime。

實作需涵蓋 daemon 字幕入口、辨識分支、ASR 模型身分與字幕來源紀錄、設定保存、Web 表單及 Docker 環境設定。
模型或端點改變時應避免錯誤重用舊辨識結果，並繼續保護使用者編輯過的字幕。

驗收至少包含短日文音訊、長片分段時間對齊、無人聲、無效回應、認證失敗、逾時及取消。
另外需驗證繁中輸出、既有手動字幕保護，以及 API 失敗時 MP4 仍保留。

本次只完成程式與官方資料查核，未安裝模型、呼叫真實音訊辨識或部署容器。
遠端主機的 CPU、RAM、GPU、處理速度與實際影片辨識品質尚未量測，不能據此承諾即時生成或特定準確率。
