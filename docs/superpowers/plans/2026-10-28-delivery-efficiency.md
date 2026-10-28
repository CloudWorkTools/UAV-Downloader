# 交付管線改善 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 實作使用者選定的 retro 1、2、3、4，減少重建與過度失效，保留部署保護。

**Architecture:** CI 完成正式映像測試後輸出映像 archive 與識別資料；部署下載同一份 archive，核對提交、CI run/attempt、SHA-256 與 image ID，再送至遠端。一般部署沿用附屬服務，另以明確參數更新整套服務。流程檢查器分開共用與各功能測試輸入；手機測試取樣動畫時間軸。

**Tech Stack:** Python standard library、GitHub Actions／gh、Docker、Node.js CDP。

**Spec:** 本次對話使用者「做 1 2 3 4」及前一輪 retro 的四項建議。

## Global Constraints

- 保留同提交成功 CI、祖先版本、部署鎖、佇列恢復、遠端 image ID／前端雜湊與 NAS 驗證。
- PR 仍檢查實際合併候選；可部署 archive 僅由 exact-head push／workflow_dispatch CI 產生。
- Artifact 缺少、過期或驗證失敗時停止，不回退為未驗證本機建置。
- 不加入依賴、不將私人部署設定或位址寫入 Git。

## Review Focus

- PR 合併提交或上游相容性 run 不得冒充 exact-head 部署產物。
- archive、image ID、commit、run 或 attempt 不符時，遠端保持原狀。
- 一般部署不重建、重啟或停用既有附屬服務。
- 全域政策變更仍使所有審查失效；功能測試只影響該功能及合理相依者。
- 動畫中途的縮圖與頭像可點擊且位於畫面內；縮減動畫保持有效。

### Task 1: 功能輸入範圍

**Files:** tools/flow-docs/flow_checker.py、tests/test_flow_docs.py、docs/flow-manifest.json、docs/agents/review-efficiency.md。

- [x] 先測 scoped/global 輸入變更、依賴傳播、無效路徑與 staged 行為。
- [x] 新增 features[].policy_inputs，保留舊 manifest 相容性。
- [x] 將手機與批次 UI 測試從全域輸入移至 download.batch。
- [x] 執行 flow checker 回歸並保存獨立審查。

### Task 2: 動畫時間軸

**Files:** tests/mobile_actions.cjs。

- [x] 取樣 paused Web Animations 時間軸 0%、50%、100%，保留 delay。
- [x] 檢查畫面界線、路徑／頭像點擊與最終按鈕分離。
- [x] 用舊 scale 錯誤證明測試失敗，再恢復並通過。

### Task 3: CI artifact 與部署

**Files:** scripts/deploy_artifact.py、tests/test_deploy_artifact.py、scripts/deploy_remote.py、tests/test_deploy_remote.py、.github/workflows/web-check.yml、scripts/check_web.sh。

- [x] 先測 archive、run、commit、attempt、tag 與 image ID 拒絕分支。
- [x] CI 測試後封裝並驗證 archive；成功才上傳固定版本 action 的 artifact。
- [x] 部署僅載入通過驗證的 archive，遠端在鎖內核對 ID。
- [x] 預設只部署 app；--update-services 明確更新附屬服務，保留舊參數的明確更新意義。
- [x] 更新現有部署安全與貢獻文件，讓證據重用規則符合程式。
- [x] 離線、正式映像、exact-head CI、真實遠端部署逐項驗證；不合併 PR。
