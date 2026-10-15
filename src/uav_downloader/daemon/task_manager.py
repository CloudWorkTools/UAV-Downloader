"""Task manager for background download queues and lifecycle management."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from uav_downloader import sites as M3U8Sites
from uav_downloader.core import config
from uav_downloader.daemon import nas
from uav_downloader.daemon.download_files import DownloadFiles, existing_video, video_key
from uav_downloader.core.history import get_history_manager
from uav_downloader.core.storage import StorageWriteError, storage_fs
from uav_downloader.core.video_identity import site_from_url
from uav_downloader.sites.base import MirrorsBlockedError


@dataclass
class DownloadTask:
    task_id: str
    url: str
    dest: str
    resolution: Optional[str] = None
    max_workers: Optional[int] = None
    download_cover: bool = True
    enable_subtitles: bool = False
    subtitle_mode: str = "none"
    nas_settings: dict = field(default_factory=dict, repr=False)
    files: Any = field(default=None, repr=False)
    remote_files: list = field(default_factory=list, repr=False)
    name: str = ""
    status: str = "pending"  # pending, preparing, downloading, subtitles, uploading, cancelling, cancelled, completed, skipped, failed
    progress: float = 0.0
    downloaded_segments: int = 0
    total_segments: int = 0
    speed_bps: float = 0.0
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    job: Any = None
    thread: Optional[threading.Thread] = None
    cancelled_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> Dict[str, Any]:
        destination = self.dest
        if self.nas_settings.get("enabled") and self.nas_settings.get("mode") == "smb":
            destination = nas.public_destination(self.nas_settings)
        return {
            "task_id": self.task_id,
            "url": self.url,
            "dest": destination,
            "name": self.name,
            "status": self.status,
            "progress": round(self.progress, 1),
            "downloaded_segments": self.downloaded_segments,
            "total_segments": self.total_segments,
            "speed_bps": round(self.speed_bps, 1),
            "speed_formatted": self._format_speed(),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "enable_subtitles": self.enable_subtitles,
            "subtitle_mode": self.subtitle_mode,
            "download_cover": self.download_cover,
        }

    def _format_speed(self) -> str:
        if self.speed_bps <= 0:
            return "0 B/s"
        bps = self.speed_bps
        if bps < 1024:
            return f"{bps:.0f} B/s"
        elif bps < 1024 * 1024:
            return f"{bps / 1024:.1f} KB/s"
        else:
            return f"{bps / (1024 * 1024):.2f} MB/s"


class TaskManager:
    """Manages queue, concurrency, and execution of video downloads."""

    def __init__(self, max_concurrent: int = 2, state_path=None):
        self._lock = threading.RLock()
        self._state_path = Path(state_path) if state_path is not None else None
        self._last_snapshot = None
        self._persistence_stopped = False
        self.max_concurrent = max_concurrent
        self._tasks: Dict[str, DownloadTask] = {}
        self._queue: List[str] = []  # task_ids waiting
        self._active: Dict[str, DownloadTask] = {}  # task_id -> task
        self._worker_thread: Optional[threading.Thread] = None
        self._running = True
        if self._state_path is not None and self._state_path.exists():
            with self._state_path.open(encoding="utf-8") as source:
                records = json.load(source)
            for record in records:
                task = DownloadTask(**record)
                if task.status not in ("completed", "skipped", "cancelled", "failed"):
                    task.status = "failed"
                    task.error = "服務重啟中斷，未完成；請重新下載"
                    task.updated_at = datetime.now(timezone.utc).isoformat()
                task.speed_bps = 0
                self._tasks[task.task_id] = task

    def _save_tasks(self):
        if self._state_path is None:
            return
        with self._lock:
            if self._persistence_stopped:
                return
            # Persist public task fields only; NAS credentials and runtime objects stay in memory.
            records = [{key: value for key, value in task.to_dict().items()
                        if key != "speed_formatted"} for task in self._tasks.values()]
            snapshot = json.dumps(records, ensure_ascii=False)
            if snapshot == self._last_snapshot:
                return
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as target:
                target.write(snapshot)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self._state_path)
            self._last_snapshot = snapshot

    def start_dispatcher(self) -> None:
        """Start the background dispatcher thread."""
        with self._lock:
            if self._worker_thread is None or not self._worker_thread.is_alive():
                self._running = True
                self._worker_thread = threading.Thread(
                    target=self._dispatch_loop, daemon=True, name="uav-task-dispatcher"
                )
                self._worker_thread.start()

    def stop_dispatcher(self) -> None:
        """Stop dispatcher and cancel all tasks."""
        with self._lock:
            self._running = False
            self._save_tasks()
            self._persistence_stopped = True
        self.cancel_all()

    def add_task(
        self,
        url: str,
        dest: Optional[str] = None,
        resolution: Optional[str] = None,
        max_workers: Optional[int] = None,
        enable_subtitles: Optional[bool] = None,
        subtitle_mode: Optional[str] = None,
        download_cover: Optional[bool] = None,
    ) -> DownloadTask:
        subtitle_mode = config.get_subtitle_pref() if subtitle_mode is None else subtitle_mode
        if subtitle_mode not in config.VALID_SUBTITLE_PREFS:
            raise ValueError("不支援的字幕模式")
        if enable_subtitles is not None and not isinstance(enable_subtitles, bool):
            raise ValueError("字幕開關必須為開啟或關閉")
        if download_cover is not None and not isinstance(download_cover, bool):
            raise ValueError("下載封面必須為開啟或關閉")
        enable_subtitles = subtitle_mode != "none" if enable_subtitles is None else enable_subtitles
        download_cover = config.get_download_cover() if download_cover is None else download_cover
        target_dest = dest or config._load_prefs().get("download_dir") or os.environ.get("DOWNLOAD_DIR")
        if not target_dest:
            target_dest = os.path.join(os.getcwd(), "download")

        nas_settings = nas.load_settings()
        if nas_settings.get("enabled"):
            if nas_settings.get("mode") == "direct":
                target_dest = nas.direct_destination(nas_settings, probe=True)
                if dest and os.path.abspath(dest) != target_dest:
                    raise nas.NasError("直接 NAS 模式不接受其他下載目錄，請先修改 NAS 儲存設定")
            else:
                nas_settings = nas.load_settings(secret=True)
                if nas_settings.get("mode") == "smb":
                    if dest:
                        raise nas.NasError("直接 SMB 模式不接受本機下載目錄，請修改 NAS 連線設定")
                    nas.test_connection(nas_settings)
                    target_dest = nas.smb_destination()
                    nas_settings['_smb_path'] = target_dest

        task_id = f"task_{uuid.uuid4().hex[:10]}"
        task = DownloadTask(
            nas_settings=nas_settings,
            task_id=task_id,
            url=url.strip(),
            dest=os.path.abspath(target_dest),
            resolution=resolution,
            max_workers=max_workers,
            enable_subtitles=enable_subtitles,
            download_cover=download_cover,
            subtitle_mode=subtitle_mode if enable_subtitles else "none",
        )

        with self._lock:
            self._tasks[task_id] = task
            reason = self._duplicate_reason(task)
            if reason:
                task.status, task.error = "skipped", reason
            else:
                self._queue.append(task_id)

        self._save_tasks()
        self.start_dispatcher()
        return task

    def get_task(self, task_id: str) -> Optional[DownloadTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            # Return ordered list, newest first
            return [task.to_dict() for task in reversed(list(self._tasks.values()))]

    def _duplicate_reason(self, task):
        key = video_key(task.url, task.name)
        for other in self._tasks.values():
            if (other is not task and other.status not in ("failed", "cancelled", "skipped")
                    and video_key(other.url, other.name) == key):
                return "相同番號或網址已在佇列或已完成，已略過"
        entry = get_history_manager().find_downloaded({'url': task.url, 'title': task.name}, similarity_threshold=1.0)
        if entry and video_key(entry['url'], entry.get('title', '')) == key:
            return "相同番號或網址已有下載紀錄，已略過"
        return ""

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status in ("completed", "skipped", "cancelled"):
                return False
            task.cancelled_event.set()
            task.status = "cancelling"
            task.updated_at = datetime.now(timezone.utc).isoformat()
            if task_id in self._queue:
                self._queue.remove(task_id)
            active = task_id in self._active
            job = task.job
        if job is not None:
            job._cancel_job = True
            job.cancel_download(cleanup=False)
        if not active:
            try:
                with ExitStack() as storage:
                    if task.files and task.nas_settings.get("mode") == "smb":
                        storage.enter_context(nas.open_smb_storage(task.nas_settings, task.dest))
                        task.files.filesystem = storage_fs(task.dest)
                    self._cleanup_cancelled(task)
            except (OSError, nas.NasError) as exc:
                task.error = f"取消後檔案清除失敗，請恢復儲存連線後重試取消：{exc}"
                task.status = "failed"
        self._save_tasks()
        return True

    def _cleanup_cancelled(self, task):
        try:
            if task.remote_files:
                nas.cleanup_upload(task.nas_settings, task.remote_files)
            if task.files:
                task.files.cleanup(cancelled=True)
            task.error = None
            task.status = "cancelled"
            task.updated_at = datetime.now(timezone.utc).isoformat()
        except (OSError, nas.NasError) as exc:
            task.error = f"取消後檔案清除失敗，請恢復儲存連線後重試取消：{exc}"
            task.status = "failed"

    def cancel_all(self) -> None:
        with self._lock:
            ids = list(self._tasks.keys())
        for tid in ids:
            self.cancel_task(tid)

    def _dispatch_loop(self) -> None:
        while self._running:
            try:
                self._save_tasks()
            except OSError:
                logging.exception("無法儲存下載工作紀錄")
            task_to_run = None
            with self._lock:
                while self._queue and len(self._active) < self.max_concurrent:
                    tid = self._queue.pop(0)
                    task = self._tasks.get(tid)
                    if task and not task.cancelled_event.is_set():
                        self._active[tid] = task
                        task_to_run = task
                        break

            if task_to_run:
                t = threading.Thread(
                    target=self._run_task,
                    args=(task_to_run,),
                    daemon=True,
                    name=f"worker-{task_to_run.task_id}",
                )
                task_to_run.thread = t
                t.start()
            else:
                time.sleep(0.5)

    def _run_task(self, task: DownloadTask) -> None:
        with self._lock:
            self._active[task.task_id] = task
        task.status = "preparing"
        task.updated_at = datetime.now(timezone.utc).isoformat()

        if task.cancelled_event.is_set():
            self._cleanup_cancelled(task)
            with self._lock:
                self._active.pop(task.task_id, None)
            return

        storage = ExitStack()
        direct_smb = task.nas_settings.get("enabled") and task.nas_settings.get("mode") == "smb"
        try:
            if direct_smb:
                storage.enter_context(nas.open_smb_storage(task.nas_settings, task.dest))
            if task.nas_settings.get("enabled") and task.nas_settings.get("mode") == "direct":
                nas.direct_destination(task.nas_settings)

            if existing_video(task.dest, task.url):
                task.error = "儲存目錄已有相同番號的影片，已略過"
                self._finish_task(task, "skipped")
                return

            # Temporarily configure resolution if set
            if task.resolution:
                try:
                    from uav_downloader.sites import base as M3U8Crawler
                    M3U8Crawler.set_resolution_pref(task.resolution)
                except Exception:
                    pass

            task.files = DownloadFiles(task.dest, task.task_id)
            job = M3U8Sites.CreateSite(
                task.url,
                savepath=task.files.workdir,
                max_workers=task.max_workers,
            )
            task.job = job
            if job is not None:
                job.download_cover = task.download_cover

            if task.cancelled_event.is_set():
                if job:
                    job._cancel_job = True
                self._finish_task(task, "cancelled")
                return

            if not job:
                task.error = "不支援的網址格式"
                self._finish_task(task, "failed")
                return

            if not job.is_url_vaildate():
                err = getattr(job, "_last_error", None)
                if isinstance(err, MirrorsBlockedError):
                    task.error = "Cloudflare 阻擋或鏡像無法存取"
                else:
                    task.error = str(err) if err else "影片解析失敗（可能已刪除或版面改版）"
                self._finish_task(task, "failed")
                return

            with self._lock:
                task.name = job.target_name() or "video"
                reason = self._duplicate_reason(task)
            if reason or existing_video(task.dest, task.url, task.name):
                task.error = reason or "儲存目錄已有相同番號的影片，已略過"
                self._finish_task(task, "skipped")
                return
            if task.nas_settings.get("enabled") and task.nas_settings.get("mode", "upload") == "upload":
                remote = nas.smb_destination()
                with nas.open_smb_storage(task.nas_settings, remote):
                    if existing_video(remote, task.url, task.name):
                        task.error = "NAS 已有相同番號的影片，已略過"
                        self._finish_task(task, "skipped")
                        return
            if task.nas_settings.get("enabled") and task.nas_settings.get("mode") in ("direct", "smb"):
                if os.path.normcase(os.path.abspath(job.dest_folder())) != os.path.normcase(task.files.workdir):
                    raise nas.NasError("下載器未使用指定的 NAS 目錄，已停止下載")
                storage.enter_context(nas.reserve_direct_target(task.nas_settings, f"{task.name}.mp4"))
            task.status = "downloading"
            task.updated_at = datetime.now(timezone.utc).isoformat()

            def progress_cb(downloaded: int, total: int, speed: float) -> None:
                if task.cancelled_event.is_set():
                    return
                task.downloaded_segments = downloaded
                task.total_segments = total
                task.speed_bps = speed
                if total > 0:
                    task.progress = min(100.0, (downloaded / total) * 100.0)
                task.updated_at = datetime.now(timezone.utc).isoformat()

            job._progress_callback = progress_cb

            ok = job.start_download()

            if task.cancelled_event.is_set() or getattr(job, "_cancel_job", False):
                self._finish_task(task, "cancelled")
                return

            if ok is False:
                task.error = "下載過程中發生錯誤"
                self._finish_task(task, "failed")
                return

            # Check for AI subtitles if enabled (default false)
            if task.enable_subtitles and task.subtitle_mode != "none":
                task.status = "subtitles"
                task.updated_at = datetime.now(timezone.utc).isoformat()
                self._run_subtitles(task, job)

            if task.cancelled_event.is_set():
                self._finish_task(task, "cancelled")
                return
            video_file = os.path.join(task.files.workdir, f"{task.name}.mp4")
            if not storage_fs(video_file).isfile(video_file):
                raise OSError("下載器未產生完成的影片，已保留暫存檔")
            task.files.publish(task.cancelled_event)
            if task.cancelled_event.is_set():
                self._finish_task(task, "cancelled")
                return
            if task.nas_settings.get("enabled") and task.nas_settings.get("mode", "upload") == "upload":
                task.status = "uploading"
                task.updated_at = datetime.now(timezone.utc).isoformat()
                nas.upload_video(task.nas_settings,
                                 os.path.join(task.dest, f"{task.name}.mp4"),
                                 task.cancelled_event, task.remote_files)
                if task.cancelled_event.is_set():
                    self._finish_task(task, "cancelled")
                    return

            task.progress = 100.0
            self._finish_task(task, "completed")
            if task.status != "completed":
                return

            # Record to history manager
            try:
                history = get_history_manager()
                video_file = os.path.join(task.dest, f"{task.name}.mp4")
                destination = task.dest
                file_path = video_file if storage_fs(video_file).exists(video_file) else ""
                if direct_smb:
                    destination = nas.public_destination(task.nas_settings)
                    if file_path:
                        file_path = nas.public_destination(task.nas_settings, task.name + '.mp4')
                history.record_download(
                    url=task.url,
                    title=task.name,
                    dest_dir=destination,
                    file_path=file_path,
                    site=site_from_url(task.url),
                )
            except Exception:
                pass

        except StorageWriteError as exc:
            task.error = f"儲存寫入錯誤: {exc}"
            self._finish_task(task, "failed")
        except MirrorsBlockedError:
            task.error = "Cloudflare 阻擋"
            self._finish_task(task, "failed")
        except Exception as exc:
            if task.cancelled_event.is_set():
                self._finish_task(task, "cancelled")
            else:
                task.error = str(exc)
                self._finish_task(task, "failed")

        finally:
            if task.cancelled_event.is_set():
                self._cleanup_cancelled(task)
            elif task.status in ("completed", "skipped") and task.files:
                try:
                    task.files.cleanup()
                except OSError as exc:
                    task.error = f"影片已完成，但暫存檔清除失敗：{exc}"
            try:
                storage.close()
            except OSError:
                warning = "NAS 暫存鎖無法清除，請確認連線後清除 .uav-*.lock"
                task.error = f"{task.error}；{warning}" if task.error else warning
            with self._lock:
                self._active.pop(task.task_id, None)
            self._save_tasks()

    def _run_subtitles(self, task: DownloadTask, job: Any) -> None:
        try:
            if os.name != "nt":
                raise RuntimeError("目前內建字幕引擎僅支援 Windows，Linux／Docker 尚不支援產生字幕")
            from uav_downloader.subtitles.engine import generate_subtitles
            target_file = os.path.join(job.dest_folder() or task.dest, f"{task.name}.mp4")
            if storage_fs(target_file).isfile(target_file):
                generate_subtitles(
                    target_file,
                    mode=task.subtitle_mode,
                    cancel_check=task.cancelled_event.is_set,
                )
        except Exception as exc:
            # Subtitle error does not fail the whole download if MP4 succeeded
            task.error = f"影片已完成，但字幕生成失敗: {exc}"

    def _finish_task(self, task: DownloadTask, status: str) -> None:
        with self._lock:
            task.status = "cancelling" if task.cancelled_event.is_set() else status
            task.updated_at = datetime.now(timezone.utc).isoformat()
        self._save_tasks()
