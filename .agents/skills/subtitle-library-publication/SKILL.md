---
name: subtitle-library-publication
description: 驗證影片庫字幕任務的取消與發布順序。修改試翻、整片翻譯、取消、結果寫入、任務鎖或字幕發布流程時使用。
---

# 影片庫字幕取消與發布

取消先取得任務鎖時，工作不得發布字幕、保留預覽或標成完成。完成先取得鎖時，整片應發布一次，且重讀後的發布紀錄須與字幕檔一致。試翻只提供預覽，稍後的取消應被拒絕。若取消與發布之間有空窗，使用者可能在 NAS 看到已取消工作的字幕。

## 執行檢查

在專案根目錄使用既有開發虛擬環境。驗證所用版本為 CPython 3.12.14，不需額外安裝驗證工具。

```bash
PYTHONPATH=src .venv/bin/python .agents/skills/subtitle-library-publication/scripts/check_publication.py
```

程式用 Python 窮舉同一個工作的一次取消與一次完成，涵蓋試翻與整片各兩種先後順序。它直接執行任務管理器，檢查真正的狀態檔與本機 SRT。從最後一次取得任務鎖到工作函式結束，另一個執行緒會在每個後續可追蹤的程式行與實際發布入口探測同一把鎖。程式固定派工順序，用一段合成對白取代模型回應，並將檔案寫入臨時目錄。

驗證範圍限於同一程序、來源固定及寫入成功。遠端 NAS、語音辨識、模型品質、UI、當機和多工作競爭需另外驗證。

## 核對失敗與更新範圍

出現「流程未到達最後一道鎖」時，先核對測試素材與排程注入點。注入點必須在最後一次進度檢查之後、取得發布鎖之前。出現「取消後仍發布」時，依輸出的順序檢查實際檔案與任務狀態，再對照以下程式位置。

| 檢查範圍 | 程式位置 |
| --- | --- |
| 派工時將工作標成執行中且移出佇列 | `src/uav_downloader/daemon/subtitle_tasks.py` 的 `_dispatch_loop` |
| 取消何時生效、最終檢查與取消後清理 | 同檔的 `cancel`、`_cancelled`、`_run_library_translation`、`_run_task` |
| 完成狀態與預覽重讀 | 同檔的 `_load`、`_save_locked`、`library_track` |
| 整片走實際發布路徑 | 同檔的 `_publish_library_translation` 與 `src/uav_downloader/daemon/subtitle_files.py` 的 `SubtitleFileManager.publish_result` |
| 取消例外如何中止工作 | `src/uav_downloader/subtitles/engine.py` 的 `_check_cancel` |

同步基準為 `810ae0e91b139e645270fdc6b7e9ff5c9c9f59eb`。修改上表所列程式時，比較基準和候選版本的鎖範圍、排程點及可觀察結果。更新基準前，在獨立副本逐項破壞防護並重跑。

1. 移除最後一道鎖內的 `engine._check_cancel(cancel)`。預期回報「整片：取消 → 完成」仍發布字幕。
2. 還原後，讓 `_publish_library_translation` 在鎖暫時釋放時執行。預期回報「整片：完成 → 取消」在發布時仍可取得任務鎖。
3. 還原後，將 `current.update(status='completed', ...)` 到 `_save_locked()` 移出最後的鎖。預期回報標記完成前已放開任務鎖。
4. 還原後，把整片成功發布的紀錄改成 `failed`。預期回報發布紀錄和實際檔案不符。
5. 還原後重跑。確認四條都通過，再將基準移到已核對的提交。

後續可用 `$pstack:maintain-formal-verification-skill` 維護這份驗證。
