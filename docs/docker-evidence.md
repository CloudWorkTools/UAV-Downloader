# 私有 Docker 隔離驗證紀錄

`scripts/docker_evidence.py` 用於短命、隔離的 Docker 檢查，不管理正式服務，也不讀取或採納既有容器與 volume。它只使用 Docker CLI 與 Python 標準庫。

先唯讀檢查 daemon/context 與映像設定：

```bash
python3 scripts/docker_evidence.py preflight --image local/uav-web:test
```

加上 `--probe` 會建立本次專用的標記 volume 與短命容器，使用映像預設 entrypoint 測試 client-side readonly sentinel bind、`CONFIG_DIR`/`APPDATA` 寫入及跨容器 hash 讀回。工具只在新建的 scratch volume 上準備 `APPDATA` 擁有者，摘要會記錄 UID/GID 與此前提；它不驗證既有路徑權限。無法證明 bind 路徑可見或 readback 不符時會失敗並保留診斷資料。

```bash
python3 scripts/docker_evidence.py preflight --image local/uav-web:test --probe
```

以映像 ID 執行一個不掛載主機路徑、停用網路的命令：

```bash
python3 scripts/docker_evidence.py run --image local/uav-web:test --timeout 900 -- bash scripts/check_web.sh
```

每次執行都會輸出唯一 run ID 與私有 evidence 目錄。Manifest 會在 start 前保存精確容器 ID、名稱、labels 和 Mounts；events 記錄 Docker 呼叫的開始、結束、exit code、timeout 與 signal。容器 stdout/stderr 留在該目錄的私有檔案，摘要不輸出原始命令、環境變數或 inspect 資料。若映像宣告匿名 volume，cleanup 會保留並列出它們，因此結果會標示 `cleanup_pending`。

對已完成的 run，可用摘要中的 run ID 清除本工具建立且可驗明的停止容器：

```bash
python3 scripts/docker_evidence.py cleanup RUN_ID
```

Cleanup 只在 manifest 與目前 daemon/context fingerprint 相符、容器 ID/name/owner label 完全吻合時移除停止容器；執行中、狀態不明、volume 參照未能完整掃描或匿名 volume 一律保留。它不使用 force、prune、adopt 或既有資源標記功能。Evidence 預設存於 `~/.local/state/uav-docker-evidence`，目錄權限為 `0700`，檔案為 `0600`。
