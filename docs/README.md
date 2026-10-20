# 文件索引

CloudWorkTools 版本以繁體中文 Web 操作與 NAS 擴充為主：

- [專案首頁與本機啟動](../README.md)
- [Web 與 NAS 使用說明](web-ui.md)
- [上游更新維護](upstream-sync.md)
- [架構與模組邊界](architecture.md)
- [四站精準搜尋與套件評估](search-optimization-research.md)
- [貢獻指南](../CONTRIBUTING.md)
- [代理任務追蹤](agents/issue-tracker.md)
- [代理分流標籤](agents/triage-labels.md)
- [領域文件讀取規則](agents/domain.md)
- [領域詞彙表](../GLOSSARY.md)
- [字幕階段結果與指定重做決策](adr/0001-retain-subtitle-stage-results.md)
- [四個字幕階段使用外部 API 決策](adr/0002-external-api-for-every-subtitle-stage.md)

## 規格與審閱資料

- [影匣 Web 外部 API 字幕正式規格](https://github.com/CloudWorkTools/UAV-Downloader/issues/10)：四階段模型設定、結果保留、指定重做與瀏覽器操作；功能已在整合 PR 實作，最終正式映像及環境驗收仍由 [#18](https://github.com/CloudWorkTools/UAV-Downloader/issues/18) 追蹤。
- [功能規格審閱快照](superpowers/specs/2026-10-20-external-subtitles-design.md)：保留發布前的設計討論，正式需求以 GitHub Issue 為準。
- [實作工作拆分審閱快照](superpowers/specs/2026-10-20-external-subtitles-work-breakdown.md)：保留依賴順序與模組接點，執行計畫依正式規格建立。

## 原有桌面與核心文件

以下文件保留原作者工具的操作說明；上游 Windows 發行檔不含本專案的 Web／NAS 擴充。

- [UAV Browser](./uav-browser.md)：Windows 瀏覽、搜尋、佇列及下載。
- [UAV Watcher](./uav-watcher.md)：排程追新與無人值守下載。
- [UAV Downloader CLI](./docker-cli.md)：Docker、NAS 及命令列下載。

其他參考文件：

- [AI subtitles](./ai-subtitles.md)
- [Docker 字幕與外接 ASR API 研究](./docker-subtitles-research.md)：現有限制、Linux 引擎與外接模型方案。
- [字幕模型與外部 API 相容性研究](./subtitle-model-api-research.md)：LocalAI、Ollama、LM Studio 的階段能力、非思考設定與候選模型。
- [Architecture and source layout](./architecture.md)
- [Migrating to UAV Downloader](./migration-to-uav.md)
- [Windows download verification](../WINDOWS_SECURITY.md)
- [Contributing](../CONTRIBUTING.md)
- [Security policy](../SECURITY.md)
