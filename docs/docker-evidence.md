# 私有 Docker 隔離驗證紀錄

`scripts/docker_evidence.py` 用於短命、隔離的 Docker 檢查，不管理正式服務，也不採納既有容器或 volume。一般檢查只讀取 Docker metadata；清理 probe volume 時會讀取所有容器的 Mounts metadata 以確認參照關係，不讀取 volume 內容。工具只使用 Docker CLI 與 Python 標準庫。

先唯讀檢查 daemon/context 與映像設定：

```bash
python3 scripts/docker_evidence.py preflight --image IMAGE_TAG_OR_ID
```

將 `IMAGE_TAG_OR_ID` 換成 daemon 上已存在的映像 tag 或 ID。

加上 `--probe` 會建立本次專用的標記 volume 與短命容器，使用映像預設 entrypoint 測試 client-side readonly sentinel bind、`CONFIG_DIR`/`APPDATA` 寫入及跨容器 hash 讀回。工具只在新建的 scratch volume 上準備 `APPDATA` 擁有者，摘要會記錄 UID/GID 與此前提；它不驗證既有路徑權限。無法證明 bind 路徑可見或 readback 不符時會失敗並保留診斷資料。

`--probe` 需要目標映像內有 Python，且預設 entrypoint 支援 `PUID`/`PGID`（UAV Downloader Web 正式映像符合）；不相容時會回報診斷失敗。唯讀 preflight 與 `run` 則分別依映像 metadata 與映像內可執行命令運作。

```bash
python3 scripts/docker_evidence.py preflight --image IMAGE_TAG_OR_ID --probe
```

以映像 ID 執行一個不掛載主機路徑、停用網路的命令：

```bash
python3 scripts/docker_evidence.py run --image IMAGE_TAG_OR_ID --timeout 30 -- sh -c 'printf evidence-check'
```

這個例子只在目標容器輸出一行並結束，不依賴把工作目錄或測試檔帶進映像；正式驗證時可替換成映像內可執行的短命檢查命令。

每次執行都會輸出唯一 run ID 與私有 evidence 目錄。Manifest 會在 start 前保存精確容器 ID、名稱、labels 和 Mounts；events 記錄 Docker 呼叫的開始、結束、exit code、timeout 與 signal。容器 stdout/stderr 和 Docker CLI 錯誤診斷留在該目錄的私有檔案，摘要不輸出原始命令、環境變數或 inspect 資料。若映像宣告匿名 volume，cleanup 會保留並列出它們，因此結果會標示 `cleanup_pending`。

`--timeout` 接受大於 0 且不超過 86400 秒（24 小時）的值，避免底層程序等待超時的整數範圍溢位。每次 Docker CLI 呼叫都有獨立 ID，錯誤診斷以不覆寫的私有檔保存。若 payload logs 無法擷取，cleanup 會保留該容器，直到其私有輸出證據可供檢查。

要在本機執行與 CI 相同的 Web test image，可從任何目前目錄呼叫 canonical runner，並傳入乾淨 checkout 的完整 HEAD SHA：

```bash
python3 /path/to/UAV-Downloader/scripts/check_web_canonical.py --source-sha FULL_COMMIT_SHA
```

Runner 會依序建置 `Dockerfile.web` 與以本次唯一 app tag 為基礎的 `Dockerfile.web-tests`。它在 test build 前後確認該 tag 仍指向相同 app image ID，並驗證 test image 的 root filesystem layers 以 app image layers 為前綴，然後以目前非 root UID/GID、bridge network、唯讀 source bind 和本次專用 `/config`、`/downloads` volumes 執行既有 `scripts/check_web.sh`。它在啟動前檢查實際 image ID、network、user、workdir 與全部 mounts；checkout 在建置期間變髒或身份不符時會 fail closed。成功或失敗摘要會指出私有 evidence 目錄。Runner 只清理 manifest 可驗明的停止容器與未參照 scratch volumes，不做 image 清除或 prune。

對已完成的 run，可用摘要中的 run ID 清除本工具建立且可驗明的停止容器：

```bash
python3 scripts/docker_evidence.py cleanup RUN_ID
```

Cleanup 只在 manifest 與目前 daemon/context fingerprint 相符、容器 ID/name/owner label 完全吻合時移除停止容器；執行中、狀態不明、Mounts 未記錄、logs 未成功取得、volume 參照未能完整掃描或匿名 volume 一律保留。它不使用 force、prune、adopt 或既有資源標記功能。Evidence 預設存於 `~/.local/state/uav-docker-evidence`，目錄權限為 `0700`，manifest、events、logs 與 Docker 診斷檔為 `0600`。只有內容為隨機非敏感值的 readonly client sentinel 檔設為 `0444`，供 bind namespace hash 比對。
