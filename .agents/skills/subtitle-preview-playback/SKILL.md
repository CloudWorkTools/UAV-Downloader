---
name: subtitle-preview-playback
description: 驗證影片庫的播放字幕結果、字幕語言切換、同語言去重、字幕時間及延遲回應。修改播放器或字幕預覽 API 時使用。
---

# 影片庫字幕試播

使用者選擇哪筆字幕結果與哪種語言，播放器就應在對應時間顯示該結果的單一字幕軌。前一筆結果的回應若蓋過目前這筆，畫面會顯示錯的台詞或重疊字幕，讓人誤判翻譯品質。

## 執行驗證與查看畫面

於專案根目錄執行。已核對 Node.js 22.22.1、Chrome 158.0.8127.36 與 FFmpeg 8.0.1。Chrome 預設從 `/usr/bin/google-chrome` 啟動，可用 `CHROME_BIN` 指定路徑。樣式由專案原有的 Tailwind CDN 提供。

```bash
SUBTITLE_PREVIEW_SCREENSHOTS="$(mktemp -d)" node .agents/skills/subtitle-preview-playback/scripts/check_preview.cjs
```

在輸出的證據目錄檢視 `desktop-zh-TW.png` 與 `mobile-zh-TW.png`。確認影片畫面上只有一行較早結果的繁中台詞，並可看到「顯示字幕」選單。程式也會直接檢查 Chrome 的字幕軌、有效字幕及選單狀態。

## 驗證界線

- 一部三秒合成影片有日文、繁中兩份既有字幕，佇列內有同一影片的兩筆完成結果。開啟較新結果後，在兩筆請求都未釋放時切到較早結果。
- 每筆結果各送回日文與繁中。窮舉四個成功回應的 24 種送達順序，在 1440×1000 與 390×844 各以選擇繁中、選擇日文重跑，共 96 條。每次送達都查目前字幕。完成後跳到字幕區間外，回到區間內切換語言，最後關閉字幕。
- 執行專案的完整頁面與實際播放器程式。影片及 API 資料使用隔離的本機素材，字幕回應順序由測試控制。涵蓋的是這組 UI 與字幕負載的互動。工作建立、任務重啟恢復、網路失敗、實際 NAS、語音模型與翻譯品質需各自驗證。

## 核對失敗與更新基準

有字幕但文字不同時，依輸出的送達順序重現，核對所選任務與字幕來源。瀏覽器等不到畫面或字幕時，先確認 Chrome、Tailwind CDN 和 FFmpeg，再檢查播放入口與測試回應是否仍使用相同路徑。失敗若可在實際頁面重現，保留反例。

| 驗證範圍 | 程式位置 |
| --- | --- |
| 字幕頁、影片容器與操作入口 | `uav-web/frontend/index.html` 的字幕頁及 `switchTab` |
| 點選指定任務與隔離晚到回應 | `uav-web/frontend/subtitle-tasks.js` 的 `renderTasks`、`openLibraryVideo`、`updateLibraryStatus`、`setLibraryTracks`、`addCompletedLibraryTracks` |
| 同語言取代、字幕選單與播放語言 | 同檔的 `appendLibraryTrack`、`applyLibraryCaption`、`bindLibraryCaption` |
| 驗證工具採用的回應格式 | `src/uav_downloader/daemon/subtitle_http.py` 的工作字幕路由，及 `src/uav_downloader/daemon/subtitle_files.py` 的 `srt_to_vtt` |

同步基準為 `810ae0e91b139e645270fdc6b7e9ff5c9c9f59eb`。變更上述範圍後，先在暫存副本逐項破壞防護。

1. 移除 `addCompletedLibraryTracks` 對 `libraryTrackRequest!==request` 的檢查。預期會抓到較新結果的延遲字幕蓋過較早結果。
2. 還原後，讓 `appendLibraryTrack` 每次都新增字幕軌。預期會抓到同語言字幕重複。
3. 還原後，讓 `applyLibraryCaption` 把日文選擇改成繁中。預期會抓到選日文時仍顯示繁中。
4. 還原後，在 `addCompletedLibraryTracks` 將收到的 `00:00:02.000` 改成 `00:00:02.900`。預期會抓到跳到 2.5 秒仍有字幕。
5. 還原。確認 96 條順序都通過，並查看兩張畫面，再將基準移到已核對的提交。後續可用 `$pstack:maintain-formal-verification-skill` 維護。
