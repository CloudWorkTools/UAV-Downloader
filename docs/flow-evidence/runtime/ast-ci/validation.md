# AST 流程門檻驗證

- 比較基準：`3b78b60b581b09b65b6880f5e597a4d2cae0c238`；實作候選：`69481904c69a627aa0fd8e257c666cfe0a64af51`。CI 環境候選 `a37a0c555d789c1742d9f7dea2d415eac2859b07` 另補第一方固定提交的 Python setup，因實際 runner 缺少 ensurepip；AST 實作未變。後續證據提交不改動檢查行為。
- `python -m unittest discover -s tests -p test_flow_docs.py`：22 項通過，包含 AST 相同略過、邏輯／字串／工具指令位置變更失效、解析錯誤、證據重用、實際 CI 基準與失敗傳遞。
- `flow_checker.py impact --root . --base <BASE>`：應用來源內容零變更；21 個來源改採整模組 AST，政策更新導致四功能需綁定新指紋。集中 reviewer 重用既有應用審查，只增讀本次工具、CI、規範與修正差異；token 計數不可取得，未宣稱實測節省量。
- `flow_checker.py check --root . --base <BASE>`：四個受影響功能皆通過；集中審查原始回報見 `docs/flow-evidence/reviews/ast-ci/6948190.json`。
- 正式映像專屬驗證：Web CI 的「正式映像的原生 AST 驗證」以唯讀掛載執行 `tools/flow-docs/runtime_smoke.py`。該提交的執行結果由 PR 的 GitHub Actions 紀錄提供。
- 生產環境唯讀驗證：以既有服務容器的 Python 3.11.17 在記憶體載入同一份 checker 與共用 runtime smoke；驗證格式／一般註解不失效，字串、工具指令與 `type: ignore` 的作用位置改變會失效，無效語法拒絕。結果通過；checker SHA-256：`0f020a56edc54848cef37cb51cc0ab41e777b698f3ab6afbf6ec313d33547771`。
- 限制：生產服務未安裝 Tree-sitter；跨語言解析由隔離測試與 CI 驗證。沒有部署新版服務、操作使用者任務或對真實 NAS 注入故障。CI 的審查 JSON 一致性檢查不等於 reviewer 身分驗證。
