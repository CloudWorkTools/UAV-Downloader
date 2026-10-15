# 四站精準交集搜尋研究

建議共用查詢規劃器，依站台能力縮小候選集合，再逐片核對缺少的標籤。
優先沿用 HTTP 擷取與 Python 標準函式庫，以 SQLite 保存已取得的影片資料。
全文檢索套件可以加速本機資料搜尋；遠端候選是否齊全，仍取決於站台索引、分頁與擷取範圍。

套件評估屬研究建議；下文「現況」描述程式，實際可用性仍須由部署環境驗證。
外部文件與頁面於 2026-10-15 查閱，第三方介面仍可能改動。
本文的「來源」表示官方文件，「實測」表示指定查詢的觀察，「建議」表示尚未實作的設計。

## 查詢語意與完整性

建議把每個使用者關鍵字視為一項必要條件，允許它出現在片名、番號或標籤。
對一支影片的判定為：

```text
每個關鍵字都滿足（片名符合 OR 番號符合 OR 任一標籤符合）
```

標籤採正規化後的完整名稱比對；片名與番號的字詞或子字串規則須另行明定。
繁簡轉換與同義詞會改變命中範圍，建議保留為明確規則，避免暗中擴張精準查詢。
必須在各欄位的聯集完成後，才對不同關鍵字取交集。

本次查核的套件各自提供擷取、快取或本機索引，沒有一項能直接保證四站遠端資料的完整交集。
這是對套件責任範圍的判斷，並非證明所有市面套件都不存在此功能。
只取得前若干頁、尚未讀取標籤或遇到擷取錯誤時，結果都應標示範圍未完整。

## 站台能力與實測邊界

站台語法需分開驗證，轉接器只啟用已確認的能力。
以下觀察不能推論成全站資料已完成交叉比對，也不能保證站台自身索引完整。

| 站台 | 已觀察行為 | 查詢規劃的限制 |
| --- | --- | --- |
| Jable | [MIMK 吊帶襪](https://fs1.app/search/?q=MIMK%20%E5%90%8A%E5%B8%B6%E8%A5%AA)含目標片；反序與加入不存在字的對照符合 AND 行為 | 可列為原生複合查詢候選；仍須核對標籤與分頁 |
| Hanime1 | [多標籤查詢](https://hanime1.me/search?tags%5B%5D=ASMR&tags%5B%5D=%E4%B8%AD%E6%96%87%E5%AD%97%E5%B9%95)可回結果；表單說明預設全部標籤，`broad` 開啟才為任一標籤 | 多標籤和 `query` 混合時的欄位語意仍需回歸測試 |
| MissAV | [目標原頁](https://missav.ai/dm122/mimk-054)有人妻標籤；番號加人妻搜尋未回該片 | 不能假設一般搜尋包含標籤 |
| SupJav | [目標原頁](https://supjav.com/25977.html)有 Creampie 標籤；番號加 Creampie 搜尋未回該片 | 不能直接套用 Jable 的複合搜尋 |

工作區的 [BrowseService.search_videos](../src/uav_downloader/daemon/browse_service.py)將 Jable 全部關鍵字送給原生查詢，不掃描標籤頁。
其他站透過 `_search_pages` 輪流讀取每個條件的一般搜尋與同名標籤來源。
任一條件讀完後，`_search_term` 以已完成的集合為候選，再核對其他條件，直到候選全部證實命中或該來源讀完。
這個完成集合不一定是全站最小集合，但已足以限制交集的最大候選範圍。

工作區不再以頁碼上限截斷交集；回傳重複分頁或擷取失敗時會報錯。
[test_search_intersection.py](../tests/test_search_intersection.py)包含第 121 頁命中，以及已證實全部候選後不再繼續讀取大型來源的測試。
完整性仍以原站可枚舉的搜尋集合為邊界，無法證明原站索引本身沒有漏片。
持久 SQLite metadata 快取與自適應的網路成本估計尚未實作。

## 共用查詢規劃器

建議採用資料庫查詢最佳化常見的條件下推與候選驗證方式。
條件下推是把已確認有效的篩選交給站台，讓後續需要下載與解析的資料減少。
每個站台的轉接器提供查詢網址、分頁狀態、標籤解析與能力紀錄。

1. 解析必要關鍵字與已確認的標籤名稱。
2. 優先使用站台已驗證的複合條件或標籤篩選。
3. 估計各候選來源的數量與讀取成本，先處理較小的安全候選集合。
4. 以影片識別碼去重，沿用已取得的片名、番號及標籤。
5. 對資料不足的候選影片，才下載原頁取得標籤。
6. 驗證全部必要條件，回傳命中影片及完整性狀態。

「安全候選集合」必須涵蓋所有可能符合查詢的影片。
例如以番號作為第一個條件時，必須確認該搜尋能涵蓋番號條件的全部匹配。
若一般搜尋不含標籤，只使用最少的片名搜尋結果，仍會漏掉僅標籤命中的影片。

在候選來源沒有涵蓋保證時，規劃器需合併該詞的一般搜尋與標籤來源，或繼續讀取未完成來源。
若站台沒有可用的窄化入口，首次完整查詢仍可能需要讀完大量分頁。
持久快取能攤提後續查詢成本，無法憑空補出從未取得的資料。

完整性建議記錄來源網址、已讀分頁、下一頁、擷取時間、錯誤與尚待核對數量。
掃描達預算或使用者取消時，回傳目前命中與未完成原因。
以完整資料作出的零筆結果，才可與「尚未搜尋完」區分。

## 套件分工與成本

下表是建議採用順序，依賴是否加入仍待實作決定。
HTTP 並行、快取與本機檢索各自處理不同成本，不能相互取代。

| 工具 | 可負責的工作 | 成本與限制 | 建議 |
| --- | --- | --- | --- |
| `curl_cffi` | HTTP 擷取、Session、瀏覽器指紋模擬 | 仍須保留站台解析與反爬錯誤處理 | 沿用既有擷取路徑 |
| `concurrent.futures` | 有界平行取得候選原頁 | 並行只減少等待；過量請求可能觸發限制 | 沿用標準函式庫，限制每站併發 |
| `sqlite3` | 保存影片欄位、標籤關聯與完成範圍 | 資料更新與完整性仍由程式維護 | 優先評估 |
| `requests-cache` | HTTP 回應的持久快取與重新驗證 | 以 Requests Session 為基礎；不能假設直接相容 `curl_cffi` | 有 HTTP 快取需求再整合 |
| `tantivy` Python binding | 本機全文索引、欄位與布林查詢 | 額外原生依賴、索引同步與中文切詞設計 | 本機搜尋成為瓶頸後再評估 |

來源：[`curl_cffi` quick start](https://curl-cffi.readthedocs.io/en/latest/quick_start.html)、
[`concurrent.futures`](https://docs.python.org/3/library/concurrent.futures.html)、
[`sqlite3`](https://docs.python.org/3/library/sqlite3.html)。
目前沒有套件效能基準，本文不宣稱具體加速倍數。

### SQLite 的標籤關聯與 FTS5

建議用影片表與標籤關聯表保存精準標籤，不依賴全文切詞判斷標籤是否存在。
SQLite 的 [`EXISTS`](https://www.sqlite.org/lang_expr.html#the_exists_operator)可檢查關聯資料是否存在，配合 `AND`、`OR` 表達跨欄位必要條件。
識別碼及標籤名稱的複合索引屬待量測的實作選擇。

FTS5 支援明確的 [`AND`、`OR` 及欄位篩選](https://www.sqlite.org/fts5.html#fts5_boolean_operators)。
預設 [`unicode61`](https://www.sqlite.org/fts5.html#unicode61_tokenizer)將連續字母與數字組成 token，並未提供中文詞彙斷詞保證。
因此連續中文內容可能與使用者輸入的詞邊界不同，不能直接視為任意中文子字串搜尋。

[`trigram`](https://www.sqlite.org/fts5.html#the_trigram_tokenizer)可做子字串檢索，但 `MATCH` 不會命中少於三個 Unicode 字元的查詢。
兩字標籤應走標籤關聯或明確的字串核對，避免誤判零筆。
啟用前也需檢查實際封裝環境的 SQLite 是否提供 FTS5 與所需 tokenizer。

### requests-cache 的有效期限與身分邊界

官方 [`CachedSession`](https://requests-cache.readthedocs.io/en/stable/modules/requests_cache.session.html)擴充 `requests.Session`。
本案建議先快取解析後的 metadata，保留既有 `curl_cffi` 路徑；整合兩者的相容性尚未驗證。
套件預設 [SQLite backend](https://requests-cache.readthedocs.io/en/stable/user_guide/backends.html)，可持久保存 HTTP 回應。

回應預設[永久保留](https://requests-cache.readthedocs.io/en/stable/user_guide/expiration.html)，因此必須設定有效期限。
可支援站台提供的 [ETag 與條件式重新驗證](https://requests-cache.readthedocs.io/en/stable/user_guide/headers.html#conditional-requests)，但站台是否提供仍待確認。
頁面更新頻率不同時，清單與影片原頁的期限應分開量測。

預設匹配使用 method、URL、parameters 及 body，[headers 須另外設定](https://requests-cache.readthedocs.io/en/stable/user_guide/matching.html#matching-request-headers)。
若 Cookie、語言或帳號影響內容，建議依有效身分隔離快取，避免錯用其他狀態的回應。
官方[安全文件](https://requests-cache.readthedocs.io/en/stable/user_guide/security.html)定位為私人單使用者快取，並提醒多身分伺服器用途的隔離問題。

### Tantivy 的適用範圍

Python binding 的[查詢文件](https://tantivy-py.readthedocs.io/en/latest/reference.html)展示跨欄位與 `AND`、`OR` 查詢。
底層 [QueryParser](https://docs.rs/tantivy/latest/tantivy/query/struct.QueryParser.html)預設為 OR，必要條件必須明確指定 AND 或 conjunction。
建議由程式產生受控查詢，避免使用者輸入直接改變運算式。

[tokenizer](https://docs.rs/tantivy/latest/tantivy/tokenizer/index.html)決定字詞邊界，採用前需驗證 Python binding 暴露的 tokenizer 與中文行為。
[安裝文件](https://github.com/quickwit-oss/tantivy-py#installation)說明可由 pip 安裝，缺少對應 wheel 時需要 Rust 編譯環境。
索引還需與影片刪除、標籤更新及快取到期同步，增加維護成本。

排名只適合安排結果顯示順序。
固定 top-k、模糊搜尋或語意相似度，不能取代精準 AND 的完整候選收集。
即使索引查詢正確，也只能保證已索引資料內的條件，仍需公開資料範圍。

## Firecrawl 的獨立 Docker 擷取模組

工作區的 [Firecrawl 模組](../src/uav_downloader/core/firecrawl.py)可由設定頁開關，Docker 版預設啟用並於啟動時背景測試 JableTV。
它只讀取本應用程式的 `UAV_FIRECRAWL_URL` 與 `UAV_FIRECRAWL_API_KEY`，不讀取本機 MCP 設定。
[完整 Docker 設定](../docker-compose.yml)建立專用 API、瀏覽器、Redis、RabbitMQ 與 PostgreSQL，使用專屬網路及資料卷，不公開服務連接埠。

一般 HTTP 鏡像全部連線失敗後，才嘗試 Firecrawl 的 `POST /v2/scrape`。
程式要求完整 `rawHtml`、成功狀態與原站 URL，再沿用站台解析器；既有空清單不觸發額外擷取。
[官方 scrape API](https://docs.firecrawl.dev/api-reference/endpoint/scrape)列出這些輸入與回應欄位。

「儲存並測試目前站台」會實際擷取該站影片清單，驗證 HTML 結構。
測試成功只代表指定站台當時可擷取，不能推論其他三站或日後也可使用。
服務有 HTTP 回應、啟用開關或 Docker healthcheck 通過，都不會直接讓介面顯示擷取成功。

[遠端部署工具](../scripts/deploy_remote.py)預設在本機建置附加映像後透過 SSH 載入遠端，再啟動獨立服務；`--without-firecrawl` 可明確略過附加服務安裝。
資料庫密碼由遠端產生，保存於權限為 `0600` 的設定檔，不寫入版本控制文件。
部署工具另執行 `example.com` 的實際 scrape；失敗時列出失敗狀態，站台可用性仍需由設定頁逐站測試。

自架版本採用[官方部署文件](https://docs.firecrawl.dev/contributing/self-host)指定的 `v2.11.162`。
官方說明 readiness 只檢查 API 心跳，實際 scrape 才能驗證工作程序及對外連線。
自架環境與目前 MCP 所連接環境的引擎、網路及防護處理可能不同，MCP 測試成功不能當作此獨立服務成功的證據。

目前已驗證模組、開關 API、前端狀態、Compose 設定解析與 Web 建置。
各站可用性仍需由部署後的實際擷取確認，尚無四站端到端效能基準。
