# Issue tracker: GitHub

本專案的任務與規格存放於 [CloudWorkTools/UAV-Downloader 的 GitHub Issues](https://github.com/CloudWorkTools/UAV-Downloader/issues)。使用 `gh` CLI，並明確指定 `--repo CloudWorkTools/UAV-Downloader`，避免操作上游倉庫。

## 任務操作

- 建立：`gh issue create --repo CloudWorkTools/UAV-Downloader --title "標題" --body-file <file>`。
- 讀取：`gh issue view <number> --repo CloudWorkTools/UAV-Downloader --json number,title,body,labels,comments`。
- 列出：`gh issue list --repo CloudWorkTools/UAV-Downloader --state open --json number,title,labels`。
- 留言：`gh issue comment <number> --repo CloudWorkTools/UAV-Downloader --body-file <file>`。
- 標籤：`gh issue edit <number> --repo CloudWorkTools/UAV-Downloader --add-label <label>`；移除時使用 `--remove-label`。
- 關閉：`gh issue close <number> --repo CloudWorkTools/UAV-Downloader`。

多行本文先寫入暫存檔，再透過 `--body-file` 傳入。發布前先搜尋既有任務，避免重複建立。

技能要求「publish to the issue tracker」時，建立 GitHub issue；要求讀取任務時，讀取對應 issue。標籤角色依 [triage-labels.md](triage-labels.md) 對照。

## PR 分流範圍

PRs as a request surface: no.

自動分流清單僅包含 issues。使用者明確指定的 PR 仍可依任務要求處理。
