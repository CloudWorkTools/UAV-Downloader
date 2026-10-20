# 字幕模型與外部 API 相容性研究

LocalAI 可作為語音辨識（ASR）與語音活動偵測（VAD）的候選服務；Ollama、LM Studio 可承接文字校正與翻譯。服務必須逐一通過階段所需的回傳契約，不能僅以「OpenAI 相容」判定可以接入。LocalAI 的音訊與 VAD 介面分別有官方文件；Ollama、LM Studio 本次查閱的相容端點清單未提供這兩種能力的依據。[LocalAI ASR](https://localai.io/docs/features/audio-to-text/)、[LocalAI VAD](https://localai.io/docs/features/voice-activity-detection/)、[Ollama 相容 API](https://docs.ollama.com/api/openai-compatibility)、[LM Studio 相容 API](https://lmstudio.ai/docs/developer/openai-compat)。

本研究的外部文件查閱日期為 2026-10-20。先以 Context7 查找各服務目前文件，再用 Firecrawl 即時讀取官方頁面與程式碼。「官方支援」指文件或來源碼描述的介面；「候選」是設計建議；「未實測」表示尚未對指定部署版本、模型與影匣影片完成端到端驗收。

既有 Docker ASR 整合背景見 [Docker 字幕研究](docker-subtitles-research.md)。字幕結果保留與指定重做的決策由 [相關 ADR](adr/0001-retain-subtitle-stage-results.md) 記錄。本文件負責外部服務契約與模型候選，不取代決策文件。

## 四階段的服務分工

| 服務或執行方式 | VAD | ASR | 原文校正與翻譯 | 接入限制 |
| --- | --- | --- | --- | --- |
| LocalAI | 官方 `POST /v1/vad` | 官方 `POST /v1/audio/transcriptions` | 官方 `POST /v1/chat/completions` | 實際能力由後端、模型與版本決定，三種端點分開驗證 |
| Ollama | 本次官方相容 API 清單未見 | 本次官方相容 API 清單未見 | 官方 `/v1/chat/completions` 或原生 `/api/chat` | 目前列為文字階段候選，不能推定接受音訊上傳 |
| LM Studio | 本次官方相容 API 清單未見 | 本次官方相容 API 清單未見 | 官方 `/v1/chat/completions` 或原生 `/api/v1/chat` | 原生與相容 API 的設定欄位不同 |
| 影匣自行執行 Silero | 官方套件提供音訊語音區間 | 不負責 | 不負責 | 影匣須處理模型載入、音訊分段與時間偏移 |

表格來源：[LocalAI VAD](https://localai.io/docs/features/voice-activity-detection/)、[LocalAI ASR](https://localai.io/docs/features/audio-to-text/)、[LocalAI 文字生成](https://localai.io/docs/features/text-generation/)、[Ollama 相容 API](https://docs.ollama.com/api/openai-compatibility)、[LM Studio 相容 API](https://lmstudio.ai/docs/developer/openai-compat)、[Silero 官方專案](https://github.com/snakers4/silero-vad)。未見端點代表本次文件查核無支援依據，不構成對未來版本的否定。

建議將 VAD、ASR、原文校正、翻譯各自保存服務與模型身分。校正和翻譯可以使用同一個文字模型，但兩階段結果與重做範圍應獨立。此為功能設計建議，尚未代表四階段已全部接入影匣。

VAD 放在影匣可讓不同 ASR 服務共用分段策略，但增加影匣的模型與運算責任。交給 LocalAI 的獨立 VAD 端點可把運算移至模型主機，但影匣仍須送出正確格式、依回傳區間切音訊，再還原字幕時間。此為介面契約導出的取捨，尚無本機硬體效能證據。

## LocalAI 音訊契約與 VAD

影匣現有 ASR 所需的契約與 LocalAI 文件列出的 multipart 介面相符，但仍須以部署版本確認。必須明確送 `response_format=verbose_json`，因為文件對預設格式的不同段落敘述不一致。[ASR 文件](https://localai.io/docs/features/audio-to-text/)。

| 項目 | 影匣需確認的內容 | 用途 |
| --- | --- | --- |
| URL 與方法 | `POST /v1/audio/transcriptions` | 上傳音訊辨識 |
| multipart 欄位 | `file`、`model`、`language=ja` | 指定音訊、模型與日語提示 |
| 輸出格式 | `response_format=verbose_json` | 保留片段時間與文字 |
| 時間粒度 | `timestamp_granularities[]=segment` | 取得字幕片段，支援程度仍依後端 |
| 必要回傳 | `segments[]` 含 `start`、`end`、`text` | 影匣驗證秒單位時間軸後才接受 |

來源：[LocalAI ASR](https://localai.io/docs/features/audio-to-text/)、[transcription endpoint 原始碼](https://github.com/mudler/LocalAI/blob/master/core/http/endpoints/openai/transcription.go)。模型可以轉錄文字，不代表該後端能產生要求的時間粒度；必須另外驗證非空片段與合法時間。

官方頁面的較早 JSON 範例含 `end=9640000000`，與秒單位契約不一致。查閱時 `master` 原始碼的 `verbose_json` 路徑以 `seg.Start.Seconds()`、`seg.End.Seconds()` 組成回傳。原始碼提供目前實作的秒單位依據，卻不能證明已部署映像包含相同實作。影匣應以短音訊測試確認版本，不應按數值大小自動猜測並換算單位。[文件範例](https://localai.io/docs/features/audio-to-text/)、[原始碼](https://github.com/mudler/LocalAI/blob/master/core/http/endpoints/openai/transcription.go)。

LocalAI 的 VAD 契約是 `POST /v1/vad`，JSON 含 `model` 與 `audio`，後者為 16 kHz 單聲道 float32 PCM 樣本陣列。回傳 `segments[].start/end` 的單位是秒。這個介面與 multipart 音訊上傳不同，不能共用 ASR 請求格式。[VAD 文件](https://localai.io/docs/features/voice-activity-detection/)。

Silero 後端以模型 YAML 選擇 `backend: silero-vad`，`options` 可設定 `threshold`、`min_silence_duration_ms`、`speech_pad_ms`；修改後須重載模型或重新啟動服務。這些是模型服務端設定，文件未將它們列為 `/v1/vad` 每次請求可覆寫的欄位。若影匣需要每個任務獨立參數，須選擇服務端模型別名或另設可驗證的適配方式。[VAD 模型設定](https://localai.io/docs/features/voice-activity-detection/)。

## 非思考設定與結構化文字

字幕的「停用思考」應表示不生成額外的推理文字。隱藏、丟棄或分離推理欄位不能證明服務省下推理生成時間，任何文字模型仍須執行推論。各服務與模型的控制欄位不同，不能提供一個未驗證的通用開關。

| 服務與介面 | 官方控制方式 | 可確認的範圍與限制 |
| --- | --- | --- |
| LocalAI `/v1/chat/completions` | `reasoning_effort: "none"`；或 `metadata: {"enable_thinking":"false"}` | 文件說明會傳遞 chat template 控制；需所選模型與後端接受該控制 |
| LocalAI 模型 YAML | `reasoning_effort: none`；`chat_template_kwargs`；`options` 的 `reasoning_budget:0` 等 | 屬服務端預設或載入時選項，不能當作影匣通用 request 欄位 |
| Ollama `/api/chat` | `think:false`；先用 `/api/show` 查看 `thinking.values/default` | 官方限定模型允許時才可關閉；metadata 缺漏時仍可能產生思考 |
| Ollama `/v1/chat/completions` | `reasoning_effort:"none"` 或 `reasoning.effort` | 相容映射依模型而異，不支援值可能回落預設，須核對模型 metadata |
| LM Studio `/api/v1/chat` | `reasoning:"off"` | 原生 API 文件明列，模型不支援該設定會報錯 |
| LM Studio `/v1/chat/completions` | 本次文件未確認可直接使用的通用硬關閉欄位 | 不能把原生 `reasoning:"off"` 直接套入；可先用純 Instruct 候選 |

表格來源：[LocalAI 模型設定](https://localai.io/docs/advanced/model-configuration/)、[Ollama thinking](https://docs.ollama.com/capabilities/thinking)、[Ollama 相容 API](https://docs.ollama.com/api/openai-compatibility)、[LM Studio 原生 chat](https://lmstudio.ai/docs/developer/rest/chat)、[LM Studio chat completions](https://lmstudio.ai/docs/developer/openai-compat/chat-completions)。

LocalAI 同一設定文件對 `reasoning.disable` 同時描述「停用 extraction」與驅動 `enable_thinking` 預設。`reasoning.strip_reasoning_only` 則明確是移除輸出推理文字。驗收不能只確認結果沒有 `<think>`；應固定後端與模板，確認非思考參數確實生效。文件亦指出部分模型忽略 `enable_thinking`，須用模型對應的 `reasoning_effort`。[LocalAI 模型設定](https://localai.io/docs/advanced/model-configuration/)。

Ollama 原生 API 用 `format` 接 JSON Schema，LM Studio 的 Chat Completions 用 `response_format.type=json_schema`。LM Studio 另提醒模型本身可能無法可靠完成結構化任務。LocalAI 可列為文字聊天候選，但本次未完整查證所有後端對 `response_format.json_schema` 的一致支援，不能先標示為通用保證。[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)、[LM Studio structured output](https://lmstudio.ai/docs/developer/openai-compat/structured-output)、[LocalAI 文字生成](https://localai.io/docs/features/text-generation/)。

建議校正與翻譯皆輸入帶穩定 ID 的片段，輸出只接受同組 ID 與非空文字，並由影匣驗證遺漏、重複及額外項目。JSON Schema 能約束格式，字幕語意與時間對齊仍需影匣驗證。非思考要求無法由指定服務與模型落實時，應顯示「未確認」，不應默默忽略。

## 模型候選與驗收界線

| 階段 | 候選 | 官方能力依據 | 尚待確認 |
| --- | --- | --- | --- |
| VAD | Silero VAD | 官方 ONNX/PyTorch 工具提供語音區間；LocalAI 有 Silero 後端 | 呼吸、呻吟、音樂、短句的偵測取捨與閾值 |
| ASR | Whisper large-v3 | 多語辨識、片段或字詞時間戳 | 選定 LocalAI 後端、量化與版本的回傳契約及日文正確率 |
| ASR 對照 | Whisper large-v3-turbo | 官方描述為減少 decoder 層數的版本，速度與品質有取捨 | 影匣影片是否可接受，不能直接視為品質升級 |
| 校正與翻譯基準 | Qwen2.5-14B-Instruct | 官方卡列中文、英文、日文與 JSON 能力 | 模型主機資源、日文保真及繁中用詞；不是音訊模型 |
| 校正與翻譯對照 | Qwen3-8B 非思考模式 | 官方模板有 `enable_thinking=False` 硬切換 | 服務是否傳入正確模板控制，與部署量化格式的相容性 |

來源：[Silero](https://github.com/snakers4/silero-vad)、[Whisper large-v3](https://huggingface.co/openai/whisper-large-v3)、[Whisper large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo/blob/main/README.md)、[Qwen2.5-14B-Instruct](https://huggingface.co/Qwen/Qwen2.5-14B-Instruct/blob/main/README.md)、[Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B/blob/main/README.md)。這些候選未在影匣進行品質或效能排名。

Qwen3 官方把 `/no_think` 描述為軟控制；`enable_thinking=False` 才是模板硬控制。模型文件承諾的行為不能證明所有伺服器都會正確傳遞該欄位。Qwen2.5 Instruct 可作為不依賴 Qwen3 模式開關的基準，但仍須驗證字幕輸出與部署模板。[Qwen3 模式說明](https://huggingface.co/Qwen/Qwen3-8B/blob/main/README.md)。

Whisper large-v3 官方說明辨識可能產生音訊中沒有說出的文字，因此更換模型不能保證消除幻覺。文字校正也可能把不確定辨識改成流暢但錯誤的句子；後者是功能設計風險，驗收須比較原文與音訊，不能只看流暢度。[Whisper 限制](https://huggingface.co/openai/whisper-large-v3#performance-and-limitations)。

本次未安裝三種模型服務，未送出真實影片音訊，也未量測 CPU、GPU、RAM、速度或字幕錯誤率。實作前須取得模型主機規格、服務版本、後端、模型及量化身分，並用同組代表片段檢查時間戳、空音訊、短句、噪音、校正保真與繁中輸出。長片的分段重試、取消、排程及失敗時是否保留已完成片段，仍屬影匣需要定義與驗收的行為。
