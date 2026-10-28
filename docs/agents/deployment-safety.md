# 部署版本保護

部署前讀取遠端正在執行的完整映像版本；候選版本必須包含該提交，且必須通過同一提交的 Web CI。PR CI 檢查實際合併候選，不能作為 exact-head 部署產物；上游 candidate_sha 相容性測試也不能用於部署。

## 已測映像重用

對待部署分支派送 exact-head CI：

```bash
gh workflow run web-check.yml --repo CloudWorkTools/UAV-Downloader --ref <branch>
python3 scripts/deploy_remote.py --ci-run <successful-run-id>
```

不要提供 candidate_sha；需要指定流程比較基準時，使用 comparison_sha。成功的 exact-head push／workflow_dispatch CI 才會在正式映像、Web 與 SMB 測試後匯出產物；映像內的前端也會與候選原始碼雜湊核對，再測試 archive 載入與 image ID。Actions artifact 名稱含完整提交及 run attempt，保留三天。

部署工具透過已登入的 gh 下載 CloudWorkTools/UAV-Downloader 的產物，驗證成功的工作流程、相同提交、run／attempt、必要步驟、archive SHA-256、標籤與 Docker image ID；下載缺少、過期或任何核對失敗均停止，不回退為本機建置。image ID 是 Docker config 的內容識別值，並非 registry manifest digest。部署只傳送已測 archive，不重建應用映像，也不再次執行完整離線測試。

未指定 --ci-run 時，工具會找該 HEAD 最近成功的 exact-head push／workflow_dispatch；若沒有可用產物，重新派送 CI。不要將不同提交或不同 attempt 的產物混用。

## 主服務與附屬服務

一般執行 `python3 scripts/deploy_remote.py` 只更新主服務，沿用既有 Speaches、Firecrawl、網路與私人設定，不執行 compose。首次安裝或要更新附屬服務時，明確加上 --update-services；--with-firecrawl 是同義參數。--without-firecrawl 明確更新 Speaches，略過 Firecrawl 安裝，且不停止既有 Firecrawl。

更新附屬服務仍使用既有 compose 的健康檢查。該步驟失敗時不切換主服務，但已更新的附屬容器不會自動回滾；重試前檢查附屬服務狀態。

## 鎖內切換與回復

無法確認目前版本或版本不在本機 Git 歷史時，先查明並取得版本，不得繼續切換。部署鎖內重新核對執行中的映像仍為檢查時的版本，並驗證已載入的候選 image ID。若不同，停止並保留既有服務及佇列。

只有明確需要回滾時才使用 --allow-rollback；它不略過 CI、產物、已知版本或鎖內核對。切換後仍驗證實際容器 image ID、前端雜湊、健康狀態、NAS 寫入與清理；失敗時回復上一版主服務。佇列只恢復本次部署暫停的任務；中斷後使用 --recover-queue 處理既有恢復紀錄。

檢查線上版本是否包含於最新 origin/master。若尚未合併，保留其分支並建立或延續 PR，讓後續工作以包含線上版本的提交為基礎；部署工具會顯示警告。PR 轉為可審查前，依[流程同步政策](flow-docs-sync.md)完成文件、映射與獨立審查。
