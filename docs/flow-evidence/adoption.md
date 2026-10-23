# 流程文件導入證據

| 實際路徑 | 用途 | 產物狀態 | 驗證證據 |
| --- | --- | --- | --- |
| `AGENTS.md` | 本機入口與共用政策載入指示 | reused/updated，保持未追蹤 | 保留原有本機部署規則；未加入 Git |
| `docs/agents/flow-docs-sync.md` | 可追蹤的共用同步政策 | created | 含完整 bootstrap、交付邊界及檢查命令 |
| `docs/flows/README.md` | 系統總覽、功能索引、來源與文件責任 | created | 三個 Feature ID、依賴、缺口與實際政策位置 |
| `docs/flows/download.batch.mmd` | 批次視窗到提交流程 | created | 來源核對；語意結果見下方審查列 |
| `docs/flows/storage.destination.mmd` | 共用目的地解析與檢查 | created | 同上 |
| `docs/flows/queue.enqueue.mmd` | 任務建立與持久化 | created | 同上 |
| `docs/flow-manifest.json` | 明示輸入、摘要、映射、相依與審查 | created | `snapshot` 已執行；三個目前指紋皆有有效審查紀錄，worktree check passed |
| `docs/flows/contracts.md` | 無其他 schema 擁有的批次共用契約 | created | 程式與測試交叉核對 |
| `tools/flow-docs/flow_checker.py` | 專案檢查器 | created | 與安裝技能的完整原始檔逐位元組相同 |
| `tools/flow-docs/flow_parsers.py` | 選用跨語言 AST adapter | created | 同上；本次未啟用 Tree-sitter |
| `tools/flow-docs/requirements-ast.txt` | 選用 parser 版本 | created | 同上；本次未安裝額外依賴 |
| `tests/test_flow_docs.py` | 真實 CLI 與隔離 Git 倉庫測試 | created | 十項測試；詳見 checker-tests.log |
| `docs/flow-evidence/adoption.md` | 導入產物及各 gate 狀態 | created | 本文件 |
| `docs/flow-evidence/candidate-inputs.json` | 比較基準、候選輸入 SHA-256 與 feature 指紋 | created | 專案檢查器產生指紋；證據不參與自身指紋 |
| `docs/flow-evidence/candidate-inputs.initial.json` | 初次審查的候選輸入 | created | 保留初次原始審查對應的輸入身分 |
| `docs/flow-evidence/.gitignore` | 保留此目錄內的驗證 log | created | 窄範圍解除全域 *.log 忽略，其他目錄維持原規則 |
| `docs/flow-evidence/read-set.json` | 已讀文件身分 | created | 僅為紀錄，不單獨證明閱讀順序或理解 |
| `docs/flow-evidence/staged-check.json` | 隔離 index 的候選結構檢查 | created | check passed (index)，實際使用者 index SHA-256 未變 |
| `docs/flow-evidence/structural-check.json` | 最終 worktree 的結構檢查 | created | Python 3.14 與 3.12 均通過 |
| `docs/flow-evidence/merge-controls.json` | 遠端合併設定的只讀觀察 | created | gh API 原始輸出與退出碼，未修改設定 |
| `docs/flow-evidence/runtime/download.batch/offline-regression.md` | 批次離線執行證據 | created | Python、Chrome 及 checker log 同目錄保存 |
| `docs/flow-evidence/runtime/download.batch/offline-regression/python-tests.log` | 批次 Python 回歸輸出 | created | 27 passed、115 deselected |
| `docs/flow-evidence/runtime/download.batch/offline-regression/browser-tests.log` | Chrome 實際 DOM 回歸輸出 | created | PASS |
| `docs/flow-evidence/runtime/download.batch/offline-regression/checker-tests.log` | 檢查器隔離回歸輸出 | created | 10 tests OK |
| `docs/flow-evidence/runtime/storage.destination/nas-live.md` | 真實 NAS 未驗證狀態 | created | not run，不以離線結果取代 |
| `docs/flow-evidence/runtime/queue.enqueue/offline-regression.md` | 佇列／持久化測試的範圍 | created | 引用同次 pytest log |
| `docs/flow-evidence/reviews/download.batch/8e9e837e8f51e085118ca85f697772e4ac56ec9cc985f71e7b9fd23b87c34f63.json` | 本候選的獨立語意審查 | created | 獨立代理 verdict=pass，無未解決 findings |
| `docs/flow-evidence/reviews/storage.destination/8d7a030c2847979e0442d4dbc4aa37814cdd0da3669d90c0cbb4a266b524492c.json` | 本候選的獨立語意審查 | created | 獨立代理 verdict=pass，無未解決 findings |
| `docs/flow-evidence/reviews/queue.enqueue/b4386133c84f74e91ccd3d7a04128853517d0012c0c527dfedffc8db2a78ca8b.json` | 本候選的獨立語意審查 | created | 獨立代理 verdict=pass，無未解決 findings |
| `.git/hooks/pre-commit` | strict 每次提交 hook | not-applicable | 選用 PR 交付邊界，未安裝或覆寫 hook |
| `.github/workflows/flow-docs.yml` | 自動候選檢查 | missing | 本次未配置，合併強制檢查 incomplete |
| `CLAUDE.md`、`.claude/CLAUDE.md` | Claude 相容入口 | not-applicable | 本次沒有實際 Claude session；上述檔案不存在，未推測需求建立入口 |
| `CLAUDE.local.md`、`.claude/CLAUDE.local.md` | 個人入口偏好 | not-applicable | 本次未發現上述檔案，未建立或覆寫 |
| `README.md` | 選用根目錄導航 | reused/unchanged | 文件入口新增於既有 `docs/README.md` |

## 位置覆寫與現有內容

本機專用 `AGENTS.md` 的明示指示優先於技能預設。共用規則由預設 `AGENTS.md` 覆寫至 `docs/agents/flow-docs-sync.md`，根目錄保留載入指示；此覆寫也已記錄於根目錄與流程索引。其他必需產物使用預設位置。新工作區須自行載入可追蹤政策，不能假設本機入口會隨 clone 傳遞。

`CONTRIBUTING.md`、`docs/architecture.md`、`docs/web-ui.md`、`docs/agents/domain.md`、`docs/agents/issue-tracker.md` 保持原位置。既有未提交的詞彙表、ADR、研究及規格稿保留；只在文件索引增加流程入口，未將其他人的變更提交或重寫。正式需求仍由 GitHub Issues 擁有。既有研究稿的歷史描述不作現行同步政策。

## 候選與 gate 狀態

- Base: `1d4d6993f8b3881f9e9e27a97010d4147352e81e`。
- Candidate: 未提交 worktree；精確來源／政策／流程 SHA-256 與三個 feature 指紋在 [candidate-inputs.json](candidate-inputs.json)。產品來源與依賴宣告未修改；新增文件、檢查工具及隔離測試。
- Mode: worktree；實際 index 不作任何暫存操作。隔離 index 的候選 tree identity 與結果見 `staged-check.json`，該 snapshot 不含之後寫入的檢查結果文件本身。

| Gate | 狀態 | 依據與限制 |
| --- | --- | --- |
| 技能／工具安裝 | passed | 使用 `/home/karl/.agents/skills/flow-docs-sync/`；SKILL.md、references、hook asset 與三個 scripts 均存在；完整 scripts 複製到專案並比對內容 |
| 根規則與 discovery | partial | 本次 Codex 收到根指示並直接讀取本機 AGENTS 與共用政策；其他工作區及 Claude session 的實際載入 unverified |
| 先讀後改 | partial | 初次沒有流程基線，先閱讀領域文件並核對來源，再建立文件；受影響 ID 已於編輯前報告。read-set 僅保存文件身分；跨工具阻擋及新代理編輯情境未實測 |
| 覆蓋 | partial | 三個功能與 21 個明示來源，依 manifest 計數；完整缺口在流程索引。混合職責檔案為保守 file 模式，不能當作全專案覆蓋 |
| 交付觸發／生成 | incomplete | PR 交付前由準備端代理執行 Maintain；無非互動 runner、GitHub ready/synchronize/base-update/merge_group 整合或測試，不宣稱事件已自動觸發 |
| 結構檢查 | passed (worktree) | Python 3.14.4 與專案 3.12.14 均以真實比較基準通過；隔離 index 模式另見 staged-check.json；實際使用者 index 未驗證或更動 |
| 獨立代理語意審查 | passed (宣告範圍) | 三份目前指紋由 /root/review_batch_final、/root/review_storage_final、/root/review_queue_final 分別審查；均 pass 且 findings 空白。原始 JSON 保留，不能代替人類核准或審查者認證 |
| 行為與外部輸入 | partial | 27 個批次案例、Chrome DOM 及 10 個 checker 隔離案例通過；真實 NAS、Rust 完整 HTTP、正式映像、生產環境與浮動依賴實際版本 not run |
| 視覺檢查 | pending | portable Mermaid 結構受 checker 檢查；沒有渲染入口或派生圖片。visual verification: pending |
| CI／人類核准／合併 | incomplete | 沒有 flow workflow 或必要檢查；只讀 gh API 顯示 master protection 404「Branch not protected」，repository rulesets 為空，見 merge-controls.json。未建立 PR、發布、修改遠端設定或代替人類核准 |

本次未新增或修改 CI，因此沒有以本機測試宣稱符合 CONTRIBUTING 的 CI／正式映像／生產驗證完成條件。後續安裝 CI 仍須完成該規範的目的專屬驗證。

## 已執行驗證與保留缺口

[離線執行證據](runtime/download.batch/offline-regression.md)保存命令、版本、測試輸入、結果、清理及限制。檢查器測試以獨立暫存 Git 倉庫操作真實 CLI，涵蓋初始基線、新審查、摘要過期、新檔／函式缺映射、共用依賴／global 變更、Python 註解濾除、file 模式、失敗或過期審查、刪除 feature、staged 與 worktree 差異、無效基準／路徑／圖形及複雜度限制。測試內審查是合成 fixture，不能用作產品功能的審查證據。

第一次獨立審查發現結果不明後仍可取消視窗的路徑；已依前端來源補正流程與共用契約。初次 JSON 保留於其舊指紋路徑，共用契約變更使三個 feature 指紋全部失效，最終審查重新針對新指紋執行。產品行為未在本次改動。

這次建立的是部分功能的本機基線。自動化交付、跨工作區入口驗證、視覺檢查及遠端合併強制尚未完成；單一結構通過不代表導入的所有 gate 完成，也不證明審查者真實身分、流程理解或執行時等價。
