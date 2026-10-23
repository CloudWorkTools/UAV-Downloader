# Runtime evidence: queue.enqueue / offline-regression

- Base: `1d4d6993f8b3881f9e9e27a97010d4147352e81e`。
- Candidate: worktree；[candidate-inputs.json](../../candidate-inputs.json) 保存綁定輸入。
- Environment: Linux，專案 venv Python 3.12.14；隔離暫存目錄與假的 NAS 設定。
- External dependency: 不使用真實 NAS 或影片網站。
- Preflight: 現有測試環境可載入 daemon 與任務管理器。
- Operation and inputs: 與 [批次離線證據](../download.batch/offline-regression.md) 相同的 pytest 命令；包含 `test_batch_rolls_back_queue_when_persistence_fails`、儲存位置快照與批次策略案例。
- Expected behavior: [共用契約](../../../flows/contracts.md) 的任務建立、儲存及失敗回復規則。
- Actual behavior: pytest 選取的 27 個案例通過；115 個不符合 batch 篩選條件的案例未執行。
- Side effects and cleanup: fixture 關閉 daemon、復原 monkeypatch；資料位於隔離暫存目錄。
- Evidence: [python-tests.log](../download.batch/offline-regression/python-tests.log)。
- Evidence retention: log 保留於文件樹，不隨暫存環境清除。
- Failure classification: 已測情境無失敗。
- Limitations: 未驗證真實儲存裝置故障、斷電、跨程序交易或完整派送／下載生命週期。
