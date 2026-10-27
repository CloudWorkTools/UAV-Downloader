# Runtime evidence: download.batch / offline-regression

- Base: `1d4d6993f8b3881f9e9e27a97010d4147352e81e`。
- Candidate: worktree；功能指紋與來源／流程／政策 SHA-256 見 [candidate-inputs.json](../../candidate-inputs.json)。
- Environment: Linux；系統 Python 3.14.4、專案 venv Python 3.12.14、Node v22.22.1、Git 2.53.0；執行日期 2026-10-23。
- External dependency: Python 測試使用暫存設定、假的 NAS 設定與停用派送器的 loopback daemon；Chrome 測試使用真實 DOM 與 stub fetch。未呼叫真實影片網站或 NAS。
- Preflight: 現有 `.venv/bin/python`、Node 與 Chrome 測試入口可執行；未新增產品依賴。
- Operation and inputs: `.venv/bin/python -m pytest -q tests/test_daemon_downloads.py tests/test_web_download_settings.py -k batch`；`node tests/download_batch.cjs`。使用測試內固定的 example 網址與隔離目錄。
- Expected behavior: [共用契約](../../../flows/contracts.md)；預覽不建資料夾、同請求重試、儲存失敗時回復佇列、衝突／忙碌／無效輸入處理、覆寫策略與前端取消／過時回應／重複送出。
- Actual behavior: Python 27 passed、115 deselected；Chrome 批次視窗測試 PASS。檢查器隔離測試另有 10 tests OK。
- Side effects and cleanup: Python 使用 pytest 暫存目錄、monkeypatch 與 fixture teardown；Chrome 測試在 finally 移除獨立暫存頁面及 profile；未操作正式影片或設定。
- Evidence: [Python log](offline-regression/python-tests.log)、[browser log](offline-regression/browser-tests.log)、[checker log](offline-regression/checker-tests.log)。各 log 保留命令、輸出與 exit_code。
- Evidence retention: 上述文件及 log 保留於倉庫的 docs/flow-evidence，獨立於測試暫存目錄。
- Failure classification: 已執行範圍無失敗；外部整合為 verification gap。
- Limitations: 未經 Rust HTTP 服務端到端轉送；未驗證真實 NAS、浮動依賴安裝版本、外站下載、字幕、正式映像或生產環境；Chrome 測試不是流程圖視覺驗證。
