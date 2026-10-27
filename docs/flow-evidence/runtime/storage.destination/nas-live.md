# Runtime evidence: storage.destination / nas-live

- Base: `1d4d6993f8b3881f9e9e27a97010d4147352e81e`。
- Candidate: worktree；[candidate-inputs.json](../../candidate-inputs.json) 保存綁定輸入。
- Environment: 本次只執行隔離的本機回歸；真實 NAS 環境未使用。
- External dependency: NAS 服務版本、掛載、帳密及可用性 unverified；不保存真實連線資料。
- Preflight: not run。
- Operation and inputs: 真實 NAS 預覽／建立／連線失敗情境 not run；離線涵蓋見 [offline-regression](../download.batch/offline-regression.md)。
- Expected behavior: [共用契約](../../../flows/contracts.md) 的目的地解析、預覽與連線關閉規則。
- Actual behavior: 真實服務 not run；不得以假的 NAS 設定或本機目錄宣稱遠端已通過。
- Side effects and cleanup: 未對真實 NAS 或生產目錄寫入。
- Evidence: 離線 log 見上述連結；真實服務無執行證據。
- Evidence retention: 本文件持續記錄未驗證狀態。
- Failure classification: verification gap。
- Limitations: SMB、upload、direct 的真實外部行為仍未驗證。
