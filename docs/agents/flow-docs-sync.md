# 流程文件同步政策

本文件是可追蹤的共用政策。根目錄 `AGENTS.md` 依本機規則保持未追蹤，僅負責載入本文件；其他工作區須把下列載入指示加入其實際使用的代理入口：

> 新增、修改、重構、移動或刪除程式與行為輸入前，必須讀取 `docs/agents/flow-docs-sync.md` 並依其流程文件同步規則執行。

這是 `flow-docs-sync` 預設 `AGENTS.md` 政策位置的明確覆寫，其餘產物使用預設路徑。不要把個人部署設定或本機 `AGENTS.md` 加入版本控制。撰寫技術文件時載入 `technical-writing` 技能。

## [MANDATORY] Flow Documentation Sync Gate

1. Pre-Edit Read: before adding, modifying, refactoring, moving or deleting
   code or behavior-bearing inputs, read docs/flows/README.md,
   docs/flow-manifest.json and all affected feature/shared flows and contracts.
   Before the first edit, report: Affected Feature IDs: <resolved IDs>.
   Read baseline flows together with the pending task diff during WIP.
   Missing or untrusted baselines require source verification first;
   unreadable required documents block dependent edits.
2. Delivery Sync: default to PR delivery. During edits, Draft PRs and WIP
   commits, retain pending impact without generating flows or review records.
   Before handing a non-draft PR to review, synchronize affected documents,
   mappings and fingerprints and obtain required independent agent review.
   Recheck every later candidate; regenerate/review only invalidated features.
   Use the recorded automation when configured; otherwise the preparing agent
   performs Maintain without waiting for a separate user command and reports
   missing automation. Required CI validates the actual merge candidate against
   a trusted comparison base. Never compare a committed candidate to itself.
   A project without PRs uses its recorded pre-commit preparation boundary.
   Preserve repository human approvals; hashes alone are not semantic review.
3. Delegation/resume: each executing agent reads its own affected documents.
   Recheck the read set after scope/input changes or a handoff.
   Missing required checks or unresolved coverage prevent completion claims.

## 實際交付設定

- 邊界：PR 交付審查前。Draft、WIP 只保留待同步範圍；本次未安裝每次提交的 hook，既有 hook 保留。
- 概覽：`docs/flows/README.md`；映射：`docs/flow-manifest.json`；共用契約：`docs/flows/contracts.md`；證據：`docs/flow-evidence/`。
- 檢查器：`python3 tools/flow-docs/flow_checker.py check --root . --base <BASE>`。`<BASE>` 必須是此次變更的實際比較基準，不是已提交候選版本本身。
- 準備端代理先以 `snapshot --root .` 更新已登記來源，再用 `fingerprint --root . --base <BASE> --feature <ID>` 取得審查指紋，保存獨立審查 JSON 並更新 manifest，最後執行 `check`。三者皆為同一個 `flow_checker.py` 的子命令。
- 本機 worktree 檢查不代表 index；若檢查待提交內容，使用 `check --staged`。`snapshot` 只讀寫 worktree，應在隔離候選內容後執行。
- 自動 runner 命令及 GitHub workflow：未配置。由準備端代理自動執行 Maintain；無人處理的 GitHub ready 事件不會產生文件。允許輸出為 `docs/flows/`、`docs/flow-manifest.json`、`docs/flow-evidence/`；擴充範圍或政策須另行審查。
- 必要 CI 與遠端保護設定：未驗證。合併前仍須完成候選版本 CI、所需人類核准及遠端必要檢查設定；本機檢查器無法強制合併限制。
- 覆蓋僅限索引列出的功能。發現新入口或相依來源時，先擴充盤點及閱讀範圍，再開始依賴該來源的編輯；不得以重新計算摘要代替映射與審查。
