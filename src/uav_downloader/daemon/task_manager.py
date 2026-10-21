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
from uav_downloader.core.storage import SMB_PATH_ROOT, StorageWriteError, storage_fs, probe_destination
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
    subtitles_only: bool = False
    nas_settings: dict = field(default_factory=dict, repr=False)
    files: Any = field(default=None, repr=False)
    remote_files: list = field(default_factory=list, repr=False)
    cleanup_unverified: bool = field(default=False, repr=False)
    name: str = ""
    status: str = "pending"
    progress: float = 0.0
    downloaded_segments: int = 0
    total_segments: int = 0
    speed_bps: float = 0.0
    error: Optional[str] = None
    phase: str = ''
    phase_done: int = 0
    phase_total: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    job: Any = None
    thread: Optional[threading.Thread] = None
    cancelled_event: threading.Event = field(default_factory=threading.Event)
    paused_event: threading.Event = field(default_factory=threading.Event)
    staging_dir: str = ''
    folder_policy: str = ''
    subtitle_request: Optional[dict] = field(default=None, repr=False)

    def set_phase(self, message, done=0, total=0):
        self.phase, self.phase_done, self.phase_total = message, done, total
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        destination = self.dest
        if self.nas_settings.get("enabled") and self.nas_settings.get("mode") == "smb":
            destination = nas.public_destination(self.nas_settings)
        return {
            "task_id": self.task_id,
            "url": self.url,
            "dest": destination,
            "staging_dir": self.staging_dir,
            "folder_policy": self.folder_policy,
            "final_dest": nas.public_destination(self.nas_settings) if self.nas_settings.get('enabled') and self.nas_settings.get('mode') == 'upload' else destination,
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
            "subtitles_only": self.subtitles_only,
            "download_cover": self.download_cover,
            "resolution": self.resolution,
            "max_workers": self.max_workers,
            "phase": self.phase,
            "phase_done": self.phase_done,
            "phase_total": self.phase_total,
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

    def __init__(self, max_concurrent: int = 2, state_path=None, subtitle_task_manager=None):
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
        self._subtitle_task_manager = subtitle_task_manager
        if self._state_path is not None and self._state_path.exists():
            with self._state_path.open(encoding="utf-8") as source:
                records = json.load(source)
            smb_destinations = set()
            for record in records:
                files = record.pop('files', None)
                task = DownloadTask(**record)
                if task.dest.startswith('smb://') and not task.nas_settings:
                    current = nas.load_settings()
                    if current.get('enabled') and current.get('mode') == 'smb' and nas.public_destination(current) == task.dest:
                        task.dest = nas.smb_destination()
                        task.nas_settings = dict(current, _smb_path=task.dest)
                if (task.nas_settings.get('enabled') and task.nas_settings.get('mode') == 'smb'
                        and task.dest.startswith(SMB_PATH_ROOT + os.sep)):
                    if task.dest in smb_destinations:
                        old_dest, task.dest = task.dest, nas.smb_destination()
                        if files:
                            def relocate(path):
                                return task.dest + path[len(old_dest):] if path == old_dest or path.startswith(old_dest + os.sep) else path
                            files['workdir'] = relocate(files['workdir'])
                            files['published'] = [relocate(path) for path in files.get('published', [])]
                        task.nas_settings['_smb_path'] = task.dest
                    smb_destinations.add(task.dest)
                if files:
                    task.files = DownloadFiles.restore(task.dest, task.task_id, files, task.staging_dir)
                # Old snapshots have no file ownership manifest; never assume their files were removed.
                task.cleanup_unverified = not task.subtitles_only and not files and task.status not in ("pending", "completed", "skipped", "cancelled", "paused")
                if task.status == 'pausing':
                    task.status = 'paused'
                if task.status not in ("completed", "skipped", "cancelled", "failed", "paused"):
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
            records = []
            for task in self._tasks.values():
                record = {key: value for key, value in task.to_dict().items() if key not in ('speed_formatted', 'final_dest')}
                record['dest'] = task.dest
                record['nas_settings'] = {key: value for key, value in task.nas_settings.items()
                                          if key not in ('password', 'password_encrypted', 'password_saved')}
                record['subtitle_request'] = task.subtitle_request
                if isinstance(task.files, DownloadFiles):
                    record['files'] = task.files.snapshot()
                records.append(record)
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

    def set_subtitle_task_manager(self, manager) -> None:
        """Attach the daemon's shared subtitle queue manager."""
        self._subtitle_task_manager = manager

    def stop_dispatcher(self) -> None:
        """Preserve partial downloads when the service stops."""
        with self._lock:
            self._running = False
            self._save_tasks()
            self._persistence_stopped = True
            for task in self._active.values():
                task.paused_event.set()
                if task.job is not None:
                    task.job.cancel_download(cleanup=False)

    def add_task(
        self,
        url: str,
        dest: Optional[str] = None,
        resolution: Optional[str] = None,
        max_workers: Optional[int] = None,
        enable_subtitles: Optional[bool] = None,
        subtitle_mode: Optional[str] = None,
        download_cover: Optional[bool] = None,
        staging_dir: Optional[str] = None,
        batch_destination=None,
        folder_policy="",
        defer_start=False,
        subtitle_request=None,
    ) -> DownloadTask:
        subtitle_mode = config.get_subtitle_pref() if subtitle_mode is None else subtitle_mode
        if subtitle_mode not in config.VALID_SUBTITLE_PREFS:
            raise ValueError("不支援的字幕模式")
        if enable_subtitles is not None and not isinstance(enable_subtitles, bool):
            raise ValueError("字幕開關必須為開啟或關閉")
        if download_cover is not None and not isinstance(download_cover, bool):
            raise ValueError("下載封面必須為開啟或關閉")
        if subtitle_request is not None:
            if not isinstance(subtitle_request, dict):
                raise ValueError('字幕工作要求格式無效')
            from uav_downloader.subtitles.profiles import ProfileSnapshot
            snapshots = subtitle_request.get('profile_snapshots')
            stages = subtitle_request.get('selected_stages')
            if (('video_id' in subtitle_request and subtitle_request['video_id'] is not None
                 and (not isinstance(subtitle_request['video_id'], str) or not subtitle_request['video_id']))
                    or not isinstance(stages, (list, tuple)) or not stages
                    or any(stage not in ('vad', 'asr', 'correction', 'translation') for stage in stages)
                    or not isinstance(snapshots, dict)):
                raise ValueError('字幕工作要求缺少設定快照')
            subtitle_request = dict(subtitle_request)
            if not isinstance(subtitle_request.get('allow_reasoning', False), bool):
                raise ValueError('推理模式選項無效')
            subtitle_request['profile_snapshots'] = {
                key: ({'profile_json': value.profile_json,
                       'credential_ref': value.credential_ref,
                       'snapshot_id': value.snapshot_id}
                      if isinstance(value, ProfileSnapshot) else value)
                for key, value in snapshots.items()
            }
            subtitle_request = {key: subtitle_request[key] for key in (
                'video_id', 'selected_stages', 'target_languages', 'output_languages',
                'profile_snapshots', 'input_result_id', 'allow_reasoning') if key in subtitle_request}
            try:
                subtitle_request = json.loads(json.dumps(
                    subtitle_request, ensure_ascii=False, allow_nan=False))
            except (TypeError, ValueError):
                raise ValueError('字幕工作要求格式無效') from None
        enable_subtitles = subtitle_mode != "none" if enable_subtitles is None else enable_subtitles
        download_cover = config.get_download_cover() if download_cover is None else download_cover
        staging_dir = self._staging_directory(config._load_prefs().get('staging_dir', '') if staging_dir is None else staging_dir)
        target_dest = dest or config._load_prefs().get("download_dir") or os.environ.get("DOWNLOAD_DIR")
        if not target_dest:
            target_dest = os.path.join(os.getcwd(), "download")

        nas_settings = nas.load_settings()
        if batch_destination is not None:
            target_dest, nas_settings = batch_destination
            nas_settings = dict(nas_settings)
            if nas_settings.get('enabled') and nas_settings.get('mode') == 'smb':
                target_dest = nas.smb_destination()
                nas_settings['_smb_path'] = target_dest
        elif nas_settings.get("enabled"):
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

        if folder_policy == "overwrite":
            nas_settings["_overwrite"] = True

        task_id = f"task_{uuid.uuid4().hex[:10]}"
        task = DownloadTask(
            nas_settings=nas_settings,
            folder_policy=folder_policy,
            task_id=task_id,
            url=url.strip(),
            dest=os.path.abspath(target_dest),
            staging_dir=staging_dir,
            resolution=config.get_resolution_pref() if resolution is None else resolution,
            max_workers=config.get_max_workers_per_video() if max_workers is None else max_workers,
            enable_subtitles=enable_subtitles,
            download_cover=download_cover,
            subtitle_mode=subtitle_mode if enable_subtitles else "none",
            subtitle_request=subtitle_request,
        )

        with self._lock:
            self._tasks[task_id] = task
            reason = self._duplicate_reason(task)
            if reason:
                task.status, task.error = "skipped", reason
            else:
                self._queue.append(task_id)

        if not defer_start:
            self._save_tasks()
            self.start_dispatcher()
        return task

    def get_task(self, task_id: str) -> Optional[DownloadTask]:
        with self._lock:
            return self._tasks.get(task_id)

    @staticmethod
    def _staging_directory(value):
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value) or value.strip().lower().startswith('smb:'):
            raise ValueError('暫存區必須是有效的本機目錄')
        path = os.path.abspath(value.strip()) if value.strip() else ''
        if path == SMB_PATH_ROOT or path.startswith(SMB_PATH_ROOT + os.path.sep):
            raise ValueError('暫存區必須是有效的本機目錄')
        return path

    def update_tasks(self, task_ids, *, storage=None, dest=None, subtitle_mode=None, nas_settings=None, staging_dir=None):
        if not isinstance(task_ids, list) or not task_ids or any(not isinstance(tid, str) or not tid for tid in task_ids):
            raise ValueError('請提供要修改的任務')
        if storage not in (None, 'local', 'settings', 'custom'):
            raise ValueError('不支援的儲存方式')
        if subtitle_mode is not None and (not isinstance(subtitle_mode, str) or subtitle_mode not in config.VALID_SUBTITLE_PREFS):
            raise ValueError('不支援的字幕模式')
        if storage is None and subtitle_mode is None and staging_dir is None:
            raise ValueError('請至少選擇一項修改')
        if staging_dir is not None:
            staging_dir = self._staging_directory(staging_dir)
            if staging_dir:
                probe_destination(staging_dir)
        target = None
        settings = {}
        if storage is not None:
            if storage == 'custom':
                reuse_password = isinstance(nas_settings, dict) and nas_settings.get('use_saved_password') is True
                settings = nas.prepare_settings(nas_settings, use_saved_password=reuse_password)
                if not settings['enabled']:
                    raise ValueError('請選擇 NAS 儲存模式')
                if settings['mode'] in ('smb', 'direct'):
                    dest = os.path.abspath('download')
            if storage == 'settings':
                settings = nas.load_settings(secret=True)
                dest = config._load_prefs().get('download_dir') or os.environ.get('DOWNLOAD_DIR') or os.path.abspath('download')
            if not isinstance(dest, str) or not dest.strip() or len(dest) > 4096 or any(ord(c) < 32 for c in dest) or dest.strip().lower().startswith('smb:'):
                raise ValueError('請提供有效的本機或已掛載下載路徑；SMB 請使用系統儲存設定')
            target = os.path.abspath(dest.strip())
            if settings.get('enabled'):
                if settings.get('mode') == 'direct':
                    target = nas.direct_destination(settings, probe=True)
                else:
                    if storage == 'custom':
                        nas.test_connection(settings, use_saved_password=False)
                    else:
                        nas.test_connection(settings)
                    if settings.get('mode') == 'smb':
                        target = nas.smb_destination()
                        settings['_smb_path'] = target
            if not (settings.get('enabled') and settings.get('mode') == 'smb'):
                probe_destination(target)
            if storage == 'custom' and settings.get('password'):
                settings['_task_password_encrypted'] = nas.encrypt_password(settings['password'])
        updated, skipped = [], []
        with self._lock:
            for tid in dict.fromkeys(task_ids):
                task = self._tasks.get(tid)
                if not task or tid in self._active or task.status not in ('pending', 'paused', 'failed', 'cancelled') or task.subtitles_only:
                    skipped.append(tid)
                    continue
                if target is not None:
                    changed = target != task.dest or any(settings.get(k) != task.nas_settings.get(k) for k in ('enabled', 'mode', 'host', 'share', 'directory', 'username', 'direct_path'))
                    if changed:
                        # Keep old files untouched; a changed destination starts a fresh download.
                        task.files, task.remote_files, task.job = None, [], None
                        task.cleanup_unverified = False
                        task.name, task.progress = '', 0.0
                        task.downloaded_segments = task.total_segments = 0
                    task.dest, task.nas_settings = target, dict(settings)
                    if settings.get('enabled') and settings.get('mode') == 'smb':
                        task.dest = nas.smb_destination()
                        task.nas_settings['_smb_path'] = task.dest
                    task.folder_policy = ''
                if staging_dir is not None and staging_dir != task.staging_dir:
                    task.staging_dir = staging_dir
                    task.files, task.remote_files, task.job = None, [], None
                    task.cleanup_unverified = False
                    task.name, task.progress = '', 0.0
                    task.downloaded_segments = task.total_segments = 0
                if subtitle_mode is not None:
                    task.subtitle_mode = subtitle_mode
                    task.enable_subtitles = subtitle_mode != 'none'
                task.error = None
                task.set_phase('設定已修改；等待下載' if task.status == 'pending' else '設定已修改；請按繼續或重試')
                updated.append(tid)
            self._save_tasks()
        return {'updated': updated, 'skipped': skipped}

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            # Return ordered list, newest first
            return [task.to_dict() for task in reversed(list(self._tasks.values()))]

    def pause_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status not in ('pending', 'preparing', 'downloading'):
                return False
            task.paused_event.set()
            task.status = 'pausing' if task_id in self._active else 'paused'
            task.speed_bps = 0
            task.updated_at = datetime.now(timezone.utc).isoformat()
            if task_id in self._queue:
                self._queue.remove(task_id)
            if task.job is not None:
                task.job.cancel_download(cleanup=False)
            self._save_tasks()
            return True

    def restart_task(self, task_id: str, *, subtitles_only=False) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task_id in self._active or task.status not in (('completed', 'paused', 'failed', 'cancelled') if subtitles_only else ('paused', 'failed', 'cancelled')):
                return False
            if subtitles_only and (not task.name or not task.enable_subtitles or task.subtitle_mode == 'none'):
                return False
            if task.nas_settings.get('_task_password_encrypted') and not task.nas_settings.get('password'):
                task.nas_settings['password'] = nas.decrypt_password(task.nas_settings['_task_password_encrypted'])
            if task.nas_settings.get('enabled') and task.nas_settings.get('mode') in ('smb', 'upload') and not task.nas_settings.get('password'):
                current = nas.load_settings(secret=True)
                if not current.get('enabled') or any(current.get(k) != task.nas_settings.get(k) for k in ('host', 'share', 'username', 'mode')):
                    raise ValueError('NAS 設定已變更，請恢復原任務的 NAS 連線設定後重試')
                task.nas_settings['password'] = current['password']
            if task.dest.startswith('smb:') and not task.nas_settings.get('enabled'):
                raise ValueError('舊任務缺少 NAS 設定，請使用目前儲存設定重新加入網址')
            if not (subtitles_only or task.subtitles_only):
                reason = self._duplicate_reason(task)
                if reason:
                    raise ValueError(reason)
            if subtitles_only:
                task.subtitles_only = True
                # Retrying subtitles must never own or delete the completed MP4.
                task.files = None
                task.remote_files = []
                task.cleanup_unverified = False
            task.cancelled_event.clear()
            task.paused_event.clear()
            task.job = None
            task.error = None
            task.status = 'pending'
            task.set_phase('等待字幕處理' if task.subtitles_only else '等待下載')
            task.speed_bps = 0
            task.updated_at = datetime.now(timezone.utc).isoformat()
            self._queue.append(task_id)
            self._save_tasks()
        self.start_dispatcher()
        return True

    def resume_unfinished(self):
        with self._lock:
            for task in self._tasks.values():
                if task.status == 'pending' and task.task_id not in self._active and task.task_id not in self._queue:
                    self._queue.append(task.task_id)
            ids = [task.task_id for task in self._tasks.values() if task.status in ('paused', 'failed')]
        resumed, errors = 0, []
        for task_id in ids:
            try:
                resumed += bool(self.restart_task(task_id))
            except ValueError as exc:
                errors.append({'task_id': task_id, 'error': str(exc)})
        self.start_dispatcher()
        with self._lock:
            waiting = sum(task.status == 'pending' and task.task_id not in self._active for task in self._tasks.values())
            return {'resumed': resumed, 'errors': errors, 'waiting': waiting, 'running': len(self._active)}

    def remove_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task_id in self._active or task.status not in ('completed', 'skipped', 'cancelled', 'failed'):
                return False
            if task.files and task.status == 'failed':
                raise ValueError('此任務仍有暫存檔，請先取消並清除檔案後再移除紀錄')
            del self._tasks[task_id]
            self._save_tasks()
            return True

    def clear_completed(self) -> int:
        with self._lock:
            ids = [task_id for task_id, task in self._tasks.items()
                   if task.status == 'completed' and task_id not in self._active]
            for task_id in ids:
                del self._tasks[task_id]
            self._save_tasks()
            return len(ids)

    def remove_duplicates(self):
        with self._lock:
            groups = {}
            for task in self._tasks.values():
                groups.setdefault(video_key(task.url, task.name), []).append(task)
            removed, protected_count = [], 0
            for tasks in groups.values():
                protected = [task for task in tasks if task.task_id in self._active or task.task_id in self._queue
                             or task.status not in ('completed', 'skipped', 'cancelled', 'failed')
                             or (task.status == 'failed' and (task.files or task.cleanup_unverified or task.remote_files))]
                keeper = protected[0] if protected else next((task for task in tasks if task.status == 'completed'),
                                                              next((task for task in reversed(tasks) if task.status != 'skipped'), None))
                protected_ids = {task.task_id for task in protected}
                removed.extend(task.task_id for task in tasks if task is not keeper and task.task_id not in protected_ids)
                protected_count += max(0, len(protected) - 1)
            for task_id in removed:
                del self._tasks[task_id]
            self._save_tasks()
            return {'removed': len(removed), 'protected': protected_count}

    def check_destination(self, task_id=None):
        task = self.get_task(task_id) if task_id else None
        if task_id and task is None:
            raise ValueError('任務不存在')
        settings = task.nas_settings.copy() if task else nas.load_settings(secret=True)
        destination = task.dest if task else (config._load_prefs().get('download_dir') or os.environ.get('DOWNLOAD_DIR') or os.path.abspath('download'))
        if not task and settings.get('enabled'):
            if settings.get('mode') == 'smb':
                destination = nas.smb_destination()
            elif settings.get('mode') == 'direct':
                destination = nas.direct_destination(settings)
        if settings.get('_task_password_encrypted') and not settings.get('password'):
            settings['password'] = nas.decrypt_password(settings['_task_password_encrypted'])
        if settings.get('enabled') and settings.get('mode') in ('smb', 'upload') and not settings.get('password'):
            current = nas.load_settings(secret=True)
            if any(current.get(k) != settings.get(k) for k in ('host', 'share', 'username', 'mode')):
                raise ValueError('NAS 設定已變更，請恢復原任務的設定後測試')
            settings['password'] = current['password']
        with ExitStack() as storage:
            if task and task.staging_dir:
                probe_destination(task.staging_dir)
            if settings.get('enabled') and settings.get('mode') == 'direct':
                nas.direct_destination(settings)
            if settings.get('enabled') and settings.get('mode') == 'smb':
                storage.enter_context(nas.open_smb_storage(settings, destination))
            probe_destination(destination)
            if settings.get('enabled') and settings.get('mode') == 'upload':
                remote = nas.smb_destination()
                storage.enter_context(nas.open_smb_storage(settings, remote))
                probe_destination(remote)
        return {'success': True, 'message': '實際目的地建立、寫入、重新命名、讀回及刪除測試成功'}

    def _duplicate_reason(self, task):
        key = video_key(task.url, task.name)
        if task.folder_policy:
            for other in self._tasks.values():
                if other is task or other.status in ('completed', 'failed', 'cancelled', 'skipped'):
                    continue
                if video_key(other.url, other.name) == key:
                    return '相同影片仍有未完成任務，請先完成或取消該任務'
            return ''
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
                        task.files.filesystem = storage_fs(task.staging_dir or task.dest)
                    self._cleanup_cancelled(task)
            except (OSError, nas.NasError) as exc:
                task.error = f"取消後檔案清除失敗，請恢復儲存連線後重試取消：{exc}"
                task.status = "failed"
        self._save_tasks()
        return True

    def _cleanup_cancelled(self, task):
        try:
            if task.cleanup_unverified:
                raise OSError("服務重啟後缺少檔案歸屬紀錄，無法確認清除範圍；請手動檢查下載目錄")
            if task.remote_files:
                nas.cleanup_upload(task.nas_settings, task.remote_files)
            if task.files:
                task.files.filesystem = storage_fs(task.staging_dir or task.dest)
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
        task.status = 'pausing' if task.paused_event.is_set() else "preparing"
        task.updated_at = datetime.now(timezone.utc).isoformat()

        if task.cancelled_event.is_set():
            self._cleanup_cancelled(task)
            with self._lock:
                self._active.pop(task.task_id, None)
            return
        if task.paused_event.is_set():
            with self._lock:
                self._active.pop(task.task_id, None)
                task.status = 'paused'
            self._save_tasks()
            return

        task.set_phase('連線下載目的地')
        storage = ExitStack()
        direct_smb = task.nas_settings.get("enabled") and task.nas_settings.get("mode") == "smb"
        try:
            if direct_smb:
                storage.enter_context(nas.open_smb_storage(task.nas_settings, task.dest))
            if task.nas_settings.get("enabled") and task.nas_settings.get("mode") == "direct":
                nas.direct_destination(task.nas_settings)

            if task.subtitles_only:
                task.status = 'subtitles'
                self._run_subtitles(task, None)
                if not task.error and task.nas_settings.get('enabled') and task.nas_settings.get('mode') == 'upload':
                    task.status = 'uploading'
                    nas.upload_video(task.nas_settings, os.path.join(task.dest, task.name + '.mp4'),
                                     task.cancelled_event, task.remote_files, subtitles_only=True)
                task.progress = 100.0
                self._finish_task(task, 'completed')
                return

            task.set_phase('測試實際下載目的地')
            probe_destination(task.dest)
            if task.staging_dir:
                probe_destination(task.staging_dir)

            if task.folder_policy != "overwrite" and not (task.files and task.files.published) and existing_video(task.dest, task.url):
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

            workspace = task.staging_dir or task.dest
            filesystem = storage_fs(workspace)
            if not task.files and task.cleanup_unverified:
                candidates = [name for name in filesystem.listdir(workspace)
                              if name.startswith(f'.uav-{task.task_id}-')
                              and filesystem.isdir(os.path.join(workspace, name))
                              and not filesystem.islink(os.path.join(workspace, name))]
                if len(candidates) > 1:
                    raise ValueError('此任務有多份暫存目錄，請先檢查下載目的地')
                if candidates:
                    task.files = DownloadFiles.restore(task.dest, task.task_id, {'workdir': os.path.join(workspace, candidates[0])}, task.staging_dir)
                    task.cleanup_unverified = False
            if task.files:
                task.files.filesystem = filesystem
                if filesystem.islink(task.files.workdir):
                    raise ValueError('任務暫存目錄不可使用符號連結')
            if not task.files or not task.files.filesystem.isdir(task.files.workdir):
                task.files = DownloadFiles(task.dest, task.task_id, task.staging_dir)
                task.cleanup_unverified = False
                task.progress = 0
                task.downloaded_segments = 0
                task.total_segments = 0
            self._save_tasks()
            task.set_phase('讀取影片資訊與下載來源')
            job = M3U8Sites.CreateSite(
                task.url,
                savepath=task.files.workdir,
                max_workers=task.max_workers,
            )
            task.job = job
            if job is not None:
                job.download_cover = task.download_cover

            if task.cancelled_event.is_set() or task.paused_event.is_set():
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
            if reason or (task.folder_policy != "overwrite" and not task.files.published and existing_video(task.dest, task.url, task.name)):
                task.error = reason or "儲存目錄已有相同番號的影片，已略過"
                self._finish_task(task, "skipped")
                return
            if task.nas_settings.get("enabled") and task.nas_settings.get("mode", "upload") == "upload":
                remote = nas.smb_destination()
                with nas.open_smb_storage(task.nas_settings, remote):
                    if task.folder_policy != "overwrite" and existing_video(remote, task.url, task.name):
                        task.error = "NAS 已有相同番號的影片，已略過"
                        self._finish_task(task, "skipped")
                        return
            if task.nas_settings.get("enabled") and task.nas_settings.get("mode") in ("direct", "smb"):
                if os.path.normcase(os.path.abspath(job.dest_folder())) != os.path.normcase(task.files.workdir):
                    raise nas.NasError("下載器未使用指定的 NAS 目錄，已停止下載")
                storage.enter_context(nas.reserve_direct_target(task.nas_settings, f"{task.name}.mp4", owned_paths=task.files.published if task.staging_dir else (), overwrite=task.folder_policy == "overwrite"))
            with self._lock:
                if task.paused_event.is_set() or task.cancelled_event.is_set():
                    self._finish_task(task, 'cancelled')
                    return
                task.status = "downloading"
            task.updated_at = datetime.now(timezone.utc).isoformat()

            def progress_cb(downloaded: int, total: int, speed: float) -> None:
                if task.cancelled_event.is_set() or task.paused_event.is_set():
                    return
                task.downloaded_segments = downloaded
                task.set_phase('下載影片片段', downloaded, total)
                task.total_segments = total
                task.speed_bps = speed
                if total > 0:
                    task.progress = min(100.0, (downloaded / total) * 100.0)
                task.updated_at = datetime.now(timezone.utc).isoformat()

            job._progress_callback = progress_cb
            job._stage_callback = task.set_phase

            ok = job.start_download()

            if task.cancelled_event.is_set() or task.paused_event.is_set() or getattr(job, "_cancel_job", False):
                self._finish_task(task, "cancelled")
                return

            if ok is False:
                task.error = "下載過程中發生錯誤"
                self._finish_task(task, "failed")
                return

            with self._lock:
                if task.paused_event.is_set() or task.cancelled_event.is_set():
                    self._finish_task(task, 'cancelled')
                    return
                task.status = 'publishing'

            # Check for AI subtitles if enabled (default false)
            if task.enable_subtitles and task.subtitle_mode != "none" and not task.subtitle_request:
                task.status = "subtitles"
                task.set_phase('處理影片字幕')
                task.updated_at = datetime.now(timezone.utc).isoformat()
                self._run_subtitles(task, job)

            if task.cancelled_event.is_set():
                self._finish_task(task, "cancelled")
                return
            video_file = os.path.join(task.files.workdir, f"{task.name}.mp4")
            if not storage_fs(video_file).isfile(video_file):
                raise OSError("下載器未產生完成的影片，已保留暫存檔")
            task.set_phase('移動完成檔案至存放位置' if task.staging_dir else '發布完成檔案至下載目的地')
            task.files.publish(task.cancelled_event, overwrite=task.folder_policy == "overwrite")
            if task.cancelled_event.is_set():
                self._finish_task(task, "cancelled")
                return
            if task.nas_settings.get("enabled") and task.nas_settings.get("mode", "upload") == "upload":
                task.status = "uploading"
                task.set_phase('傳送影片至 NAS')
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

            self._submit_subtitle_request(task)

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
            except OSError as exc:
                warning = f"NAS 連線或下載鎖釋放失敗：{exc}；請確認連線後重試"
                task.error = f"{task.error}；{warning}" if task.error else warning
                if task.cancelled_event.is_set():
                    task.status = "failed"
                if task.paused_event.is_set():
                    task.paused_event.clear()
                    task.status = 'failed'
            with self._lock:
                self._active.pop(task.task_id, None)
                task.speed_bps = 0
                if task.paused_event.is_set() and not task.cancelled_event.is_set():
                    task.status = 'paused'
                    task.error = None
                    task.updated_at = datetime.now(timezone.utc).isoformat()
            self._save_tasks()

    def _run_subtitles(self, task: DownloadTask, job: Any) -> None:
        try:
            from uav_downloader.subtitles.engine import generate_subtitles
            from uav_downloader.subtitles.asr import load_settings
            target_file = os.path.join((job.dest_folder() if job else None) or task.dest, f"{task.name}.mp4")
            if not storage_fs(target_file).isfile(target_file):
                raise FileNotFoundError('找不到已完成的影片，無法重試字幕')
            generate_subtitles(
                target_file,
                mode=task.subtitle_mode,
                cancel_check=task.cancelled_event.is_set,
                asr_settings=load_settings(secret=True),
                progress_callback=lambda stage, percent: task.set_phase({
                    'audio': '擷取字幕音訊', 'transcribe_ja': '辨識日文字幕',
                    'asr_tail_retry': '重新辨識異常尾段', 'asr_cached': '讀取已完成辨識片段',
                    'translate_en': '翻譯英文字幕', 'translate_zh': '翻譯繁體中文字幕',
                    'translation_model': '準備本機翻譯模型',
                }.get(stage, '處理影片字幕'), percent or 0, 100 if percent is not None else 0),
            )
        except Exception as exc:
            # Subtitle error does not fail the whole download if MP4 succeeded
            task.error = f"影片已完成，但字幕生成失敗: {exc}"

    def _submit_subtitle_request(self, task: DownloadTask) -> None:
        request = task.subtitle_request
        if not request:
            return
        try:
            from uav_downloader.subtitles.profiles import ProfileSnapshot
            if self._subtitle_task_manager is None:
                raise RuntimeError('字幕工作佇列尚未接入')
            snapshots = {
                key: ProfileSnapshot(value['profile_json'], value.get('credential_ref'), value['snapshot_id'],
                                     value.get('reasoning_verification_json'))
                for key, value in request['profile_snapshots'].items()
            }
            video_path = os.path.join(task.dest, f'{task.name}.mp4')
            from uav_downloader.daemon.subtitle_files import SubtitleFileManager
            manager = getattr(self, '_subtitle_file_manager', None)
            if manager is None:
                manager = self._subtitle_file_manager = SubtitleFileManager()
            settings = task.nas_settings or {}
            if settings.get('enabled') and settings.get('mode') == 'smb':
                current = self._subtitle_nas_settings(settings, manager)
                source = manager.register_smb(video_path, storage_root=task.dest,
                                              nas_settings=current, output_mode='smb',
                                              batch_relative_path=settings.get('_subtitle_relative_path', ''))
            elif settings.get('enabled') and settings.get('mode') == 'upload':
                current = self._subtitle_nas_settings(settings, manager)
                upload_settings = dict(current, directory=settings.get('directory', ''))
                root = nas.smb_destination()
                with nas.open_smb_storage(upload_settings, root):
                    remote_video = os.path.join(root, os.path.basename(video_path))
                    source = manager.register_upload(
                        video_path, allowed_roots=[task.dest], nas_settings=current,
                        remote_root=root, remote_video_path=remote_video,
                        batch_relative_path=settings.get('_subtitle_relative_path', ''))
            else:
                roots = [task.dest]
                if task.staging_dir:
                    roots.append(task.staging_dir)
                source = manager.register(video_path, allowed_roots=roots,
                                          output_mode='local')
            self._subtitle_task_manager.submit(
                source.video_id, request.get('selected_stages'), snapshots,
                request.get('target_languages', []), video_path=video_path,
                input_result_id=request.get('input_result_id'),
                allow_reasoning=request.get('allow_reasoning', False),
                output_languages=request.get('output_languages', []))
        except Exception:
            logging.exception('影片已下載完成，但無法提交字幕工作')
            task.error = '影片完成，字幕未能加入佇列'
            try:
                self._save_tasks()
            except Exception:
                logging.exception('影片已完成，但字幕佇列警告無法保存')

    @staticmethod
    def _subtitle_nas_settings(settings, manager):
        """Revalidate base NAS identity while retaining the exact batch folder."""
        current = nas.load_settings(secret=True)
        relative = settings.get('_subtitle_relative_path')
        if relative is None:
            if manager._nas_identity(current) != manager._nas_identity(settings):
                raise ValueError('NAS 設定已變更，無法註冊字幕影片來源')
            return current

        base_directory = settings.get('_subtitle_base_directory')
        if not isinstance(base_directory, str) or not isinstance(relative, str):
            raise ValueError('NAS 批次儲存位置無效，無法註冊字幕影片來源')
        normalized_base = base_directory.replace('\\', '/').strip('/')
        normalized_relative = relative.replace('\\', '/').strip('/')
        components = normalized_base.split('/') if normalized_base else []
        suffix = normalized_relative.split('/') if normalized_relative else []
        if (not suffix or any(part in ('', '.', '..') for part in components + suffix)
                or any(ord(char) < 32 for char in normalized_relative)):
            raise ValueError('NAS 批次儲存位置無效，無法註冊字幕影片來源')
        expected_directory = '/'.join(components + suffix)
        if settings.get('directory', '').replace('\\', '/').strip('/') != expected_directory:
            raise ValueError('NAS 批次儲存位置已變更，無法註冊字幕影片來源')
        current_base = current.get('directory', '')
        if not isinstance(current_base, str) or current_base.replace('\\', '/').strip('/') != normalized_base:
            raise ValueError('NAS 設定已變更，無法註冊字幕影片來源')
        base_settings = dict(settings, directory=current_base)
        if manager._nas_identity(current) != manager._nas_identity(base_settings):
            raise ValueError('NAS 設定已變更，無法註冊字幕影片來源')
        return current

    def _finish_task(self, task: DownloadTask, status: str) -> None:
        with self._lock:
            task.status = ('cancelling' if task.cancelled_event.is_set() else
                           'pausing' if task.paused_event.is_set() else status)
            if task.status == 'completed':
                task.set_phase('影片已儲存完成')
            task.updated_at = datetime.now(timezone.utc).isoformat()
        self._save_tasks()
