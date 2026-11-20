"""Persistent queue for independent subtitle work."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

from uav_downloader.subtitles import asr, engine, results
from uav_downloader.subtitles.models import SubtitleSegment
from uav_downloader.subtitles.profiles import ProfileSnapshot
from uav_downloader.core.storage import SMB_PATH_ROOT

_STAGES = ('vad', 'asr', 'correction', 'translation')
_RETRY_LIMIT = 2
_RETRY_DELAYS = (1.0, 3.0)
_TASK_ID_RE = re.compile(r'subtitle_[0-9a-f]{32}\Z')
LIBRARY_TRIAL_SECONDS = 120


class LibraryTranslationError(ValueError):
    """A library translation error safe to display to the operator."""


def _snapshot_dict(snapshot):
    if not isinstance(snapshot, ProfileSnapshot):
        raise ValueError('字幕設定快照格式無效')
    return {
        'profile_json': snapshot.profile_json,
        'credential_ref': snapshot.credential_ref,
        'snapshot_id': snapshot.snapshot_id,
        'reasoning_verification_json': snapshot.reasoning_verification_json,
    }


def _snapshot_from_dict(value):
    if not isinstance(value, dict):
        raise ValueError('字幕設定快照格式無效')
    return ProfileSnapshot(value['profile_json'], value.get('credential_ref'), value['snapshot_id'],
                           value.get('reasoning_verification_json'))


def _request_key(video_id, stages, snapshots, languages, input_result_id):
    return json.dumps({
        'video_id': video_id,
        'stages': stages,
        'snapshots': snapshots,
        'languages': languages,
        'input_result_id': input_result_id,
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _retry_delay(attempt):
    return _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]


def _wait_for_retry(seconds, cancel_check):
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if cancel_check():
            raise engine.SubtitleCancelled('Subtitle generation cancelled')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _safe_error(stage, error):
    """Return a concise message without exposing endpoint URLs or credentials."""
    from uav_downloader.subtitles.models import StageRequestError

    message = str(error)
    if isinstance(error, LibraryTranslationError):
        return message
    if stage == 'publish' and isinstance(error, OSError):
        return '字幕發布失敗：目的地儲存無法寫入，請檢查連線後只重試儲存'
    http = re.search(r'HTTP\s+(\d{3})\b', message, flags=re.IGNORECASE)
    prefix = f'{stage or "字幕"} 階段失敗'
    if http:
        code = int(http.group(1))
        if code in (401, 403):
            return f'{prefix}：API 驗證失敗（HTTP {code}），請檢查憑證與服務權限'
        if code == 404:
            return f'{prefix}：服務或模型不存在（HTTP 404），請檢查 API 路徑與模型名稱'
        if code in (408, 425, 429) or code >= 500:
            return f'{prefix}：服務暫時無法處理（HTTP {code}）'
        return f'{prefix}：服務拒絕請求（HTTP {code}）'
    lowered = message.casefold()
    if '缺少' in message or '缺明確' in message:
        return f'{prefix}：缺少必要的上游字幕結果，請先完成或指定上游階段'
    if isinstance(error, ValueError) and any(term in message for term in ('API 金鑰', 'API 憑證', 'credential')):
        return f'{prefix}：模型服務憑證無法使用，請重新輸入 API 金鑰後續跑'
    if any(term in lowered for term in ('已過期', '過期', 'stale', 'outdated')):
        return f'{prefix}：上游字幕結果已過期，請更新結果或明確選擇舊版本'
    if any(term in lowered for term in (
            '思考能力未確認', '尚未通過停用思考驗證', '未確認此模型支援 thinking', 'reasoning control',
            'thinking control', 'reasoning is not confirmed')):
        return f'{prefix}：尚未確認思考控制，請切換相容模型或明確允許思考'
    if any(term in lowered for term in ('invalid response', 'invalid json', 'response format', 'returned an invalid')):
        return f'{prefix}：服務回應格式無效，請確認模型與階段 API 相容'
    if isinstance(error, (TimeoutError, engine.SubtitleProcessTimeout)):
        return f'{prefix}：執行逾時，請稍後續跑'
    if isinstance(error, StageRequestError):
        safe = re.sub(r'https?://\S+', '[服務位址]', message)
        safe = re.sub(r'(?i)(authorization|api[_ -]?key|token)\s*[:=]\s*\S+', r'\1=[已隱藏]', safe)
        return f'{prefix}：{safe[:240]}'
    return f'{prefix}：{type(error).__name__}，請檢查服務設定與輸入資料'


class SubtitleTaskManager:
    """Owns subtitle task ordering, persistence, cancellation and retries."""

    def __init__(self, state_path=None, max_concurrent=1, source_manager=None):
        self._lock = threading.RLock()
        self._state_path = Path(state_path) if state_path is not None else asr._folder() / 'web_subtitle_tasks.json'
        self._tasks = {}
        self._queue = []
        self._active = {}
        self._video_locks = {}
        self._publications = {}
        self._publication_heads = {}
        self._max_concurrent = max(1, int(max_concurrent))
        if source_manager is None:
            from uav_downloader.daemon.subtitle_files import SubtitleFileManager
            source_manager = SubtitleFileManager()
        self._source_manager = source_manager
        self._running = False
        self._dispatcher = None
        self._load()

    def _load(self):
        try:
            records = json.loads(self._state_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        if not isinstance(records, dict) or records.get('schema') != 1 or not isinstance(records.get('tasks'), list):
            raise ValueError('字幕工作佇列格式錯誤')
        for task in records['tasks']:
            if (not isinstance(task, dict) or not isinstance(task.get('task_id'), str)
                    or not _TASK_ID_RE.fullmatch(task['task_id'])):
                raise ValueError('字幕工作佇列格式錯誤')
            self._tasks[task['task_id']] = task
        self._queue = [task_id for task_id in records.get('queue', []) if task_id in self._tasks]
        publications = records.get('publications', {})
        if isinstance(publications, dict):
            self._publications = publications
        heads = records.get('publication_heads', {})
        if not isinstance(heads, dict):
            raise ValueError('字幕發布版本資料格式錯誤')
        self._publication_heads = heads
        changed = False
        for publication in self._publications.values():
            if publication.get('publish_status') == 'publishing':
                publication['publish_status'] = 'failed'
                publication['error'] = '字幕發布中斷，請只重試儲存'
                publication['updated_at'] = time.time()
                changed = True
        for task in self._tasks.values():
            if task.get('library_translation'):
                task.setdefault('force_asr', False)
            if task['status'] in ('queued', 'running', 'cancelling', 'retry_wait'):
                task['status'] = 'waiting_for_resume'
                task['generation'] += 1
                changed = True
        if changed:
            self._save()

    def _save_locked(self):
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({'schema': 1, 'queue': self._queue,
                           'tasks': list(self._tasks.values()),
                           'publications': self._publications,
                           'publication_heads': self._publication_heads}, ensure_ascii=False,
                          sort_keys=True, separators=(',', ':'), allow_nan=False)
        fd, temporary = tempfile.mkstemp(prefix=self._state_path.name + '.', suffix='.tmp',
                                         dir=self._state_path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self._state_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _publication_destination(self, video_id):
        key_resolver = getattr(self._source_manager, 'publication_lock_key', None)
        return key_resolver(video_id) if key_resolver else video_id

    def _video_lock(self, video_id):
        key = self._publication_destination(video_id)
        with self._lock:
            return self._video_locks.setdefault(key, threading.Lock())

    def _publication_key(self, video_id, language):
        destination = json.dumps([self._publication_destination(video_id), language], ensure_ascii=False)
        return hashlib.sha256(destination.encode('utf-8')).hexdigest()

    def _publish_locked(self, video_id, result, language, operation_id, **options):
        """Persist a publication revision while the destination lock is held."""
        key = self._publication_key(video_id, language)
        with self._lock:
            previous = self._publication_heads.get(key)
            self._publication_heads[key] = {
                'revision': uuid.uuid4().hex, 'operation': operation_id, 'time': time.time(),
            }
            try:
                self._save_locked()
            except Exception:
                if previous is None:
                    self._publication_heads.pop(key)
                else:
                    self._publication_heads[key] = previous
                raise
        # Storage can change the destination even when the writer reports failure.
        return self._source_manager.publish_result(video_id, result, language, **options)

    def _remember_publish_head(self, record, language):
        key = self._publication_key(record['video_id'], language)
        head = self._publication_heads.get(key)
        record.setdefault('_publish_retry_heads', {})[language] = [key, head['revision'] if head else None]

    def _check_publish_retry(self, record, language):
        key = self._publication_key(record['video_id'], language)
        head = self._publication_heads.get(key)
        operation_id = record.get('task_id') or record['publication_id']
        baseline = record.get('_publish_retry_heads', {}).get(language)
        if baseline is not None:
            current = head['revision'] if head else None
            stale = baseline != [key, current] and (not head or head['operation'] != operation_id)
        else:
            # Older records only have status and timestamps to identify a newer publication.
            attempted_at = record['created_at'] if 'publication_id' in record else record['updated_at']

            def newer(other, published):
                return (published and other.get('updated_at', 0) > attempted_at
                        and self._publication_key(other['video_id'], language) == key)

            stale = ((head and head['time'] > attempted_at)
                     or any(newer(other, other.get('publish_status', {}).get(language, {}).get('status') == 'published')
                            for other in self._tasks.values())
                     or any(newer(publication, publication['language'] == language
                                  and publication['publish_status'] == 'published')
                            for publication in self._publications.values()))
        if stale:
            raise ValueError('目的地已有較新的字幕，無法重試舊版發布')

    def _save(self):
        with self._lock:
            self._save_locked()

    @staticmethod
    def _copy(task):
        return copy.deepcopy(task)

    def get_task(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            return self._copy(task) if task else None

    def list_tasks(self):
        with self._lock:
            ordered = [self._tasks[task_id] for task_id in self._queue if task_id in self._tasks]
            ordered.extend(task for task in reversed(list(self._tasks.values())) if task not in ordered)
            return [self._copy(task) for task in ordered]

    def list_publications(self):
        with self._lock:
            return [self._copy({key: value for key, value in publication.items() if key != '_publish_retry_heads'})
                    for publication in self._publications.values()]

    def get_publication(self, publication_id):
        with self._lock:
            publication = self._publications.get(publication_id)
            return self._copy(publication) if publication else None

    def submit(self, video_id, selected_stages, profile_snapshots, target_languages,
               *, audio_path=None, video_path=None, input_result_id=None,
               allow_reasoning=False, output_languages=(), _force_new=False,
               _library_request=None):
        if not isinstance(video_id, str) or not video_id or len(video_id) > 512:
            raise ValueError('字幕影片識別碼無效')
        if (not isinstance(selected_stages, (list, tuple)) or not selected_stages
                or any(not isinstance(stage, str) for stage in selected_stages)
                or len(set(selected_stages)) != len(selected_stages)
                or any(stage not in _STAGES for stage in selected_stages)):
            raise ValueError('請選擇有效的字幕階段')
        stages = [stage for stage in _STAGES if stage in selected_stages]
        if not isinstance(profile_snapshots, dict):
            raise ValueError('字幕設定快照格式無效')
        if not isinstance(allow_reasoning, bool):
            raise ValueError('推理模式選項無效')
        if input_result_id is not None and (not isinstance(input_result_id, str) or not input_result_id):
            raise ValueError('字幕上游結果識別碼無效')
        snapshots = {key: _snapshot_dict(value) for key, value in profile_snapshots.items()}
        for stage in stages:
            key = stage if stage in snapshots else 'text'
            if key not in snapshots and _library_request is None:
                raise ValueError('缺少所選字幕階段的設定快照')
        if (not isinstance(target_languages, (list, tuple))
                or any(not isinstance(language, str) for language in target_languages)):
            raise ValueError('字幕翻譯語言無效')
        languages = list(dict.fromkeys(target_languages))
        if languages is None or ('translation' in stages and not languages):
            raise ValueError('請指定字幕翻譯語言')
        if any(not isinstance(language, str) or not language for language in languages):
            raise ValueError('字幕翻譯語言無效')
        if (not isinstance(output_languages, (list, tuple))
                or any(language not in ('ja', 'en', 'zh-TW') for language in output_languages)):
            raise ValueError('字幕輸出語言無效')
        output_languages = list(dict.fromkeys(output_languages))
        fingerprint = _request_key(video_id, stages, snapshots, languages,
                                   {'result_id': input_result_id, 'allow_reasoning': allow_reasoning,
                                    'output_languages': output_languages,
                                    **({'library': _library_request} if _library_request is not None else {})})
        with self._lock:
            for task in self._tasks.values():
                if not _force_new and task['request_key'] == fingerprint and task['status'] in (
                        'queued', 'running', 'waiting_for_resume', 'retry_wait', 'cancelling'):
                    return self._copy(task)
            task_id = f'subtitle_{uuid.uuid4().hex}'
            task = {
                'task_id': task_id, 'execution_id': task_id, 'video_id': video_id,
                'video_path': (video_path if video_path and video_path.startswith(SMB_PATH_ROOT + os.path.sep)
                               else os.path.abspath(video_path) if video_path else None),
                'audio_path': (audio_path if audio_path and audio_path.startswith(SMB_PATH_ROOT + os.path.sep)
                               else os.path.abspath(audio_path) if audio_path else None),
                'selected_stages': stages, 'target_languages': languages,
                'output_languages': output_languages, 'publish_status': {},
                'profile_snapshots': snapshots, 'input_result_id': input_result_id,
                'allow_reasoning': allow_reasoning, 'inputs': {}, 'results': {},
                'completed': [], 'retry_attempts': {}, 'retry_limit': _RETRY_LIMIT,
                'retry_delay_seconds': None, 'next_retry_at': None,
                'current_stage': None, 'current_language': None,
                'status': 'queued', 'generation': 0, 'ever_started': False,
                'progress': 0, 'error': None, 'request_key': fingerprint,
                'created_at': time.time(), 'updated_at': time.time(),
            }
            if _library_request is not None:
                task.update(library_translation=True, **_library_request)
                task.setdefault('force_asr', False)
            self._tasks[task_id] = task
            self._queue.append(task_id)
            try:
                self._save_locked()
            except Exception:
                self._queue.remove(task_id)
                del self._tasks[task_id]
                raise
        self.start_dispatcher()
        return self._copy(task)

    def submit_library_translation(self, video_id, trial_seconds=None, *, force_asr=False):
        if trial_seconds is not None and (
                type(trial_seconds) is not int or trial_seconds != LIBRARY_TRIAL_SECONDS):
            raise ValueError('試翻範圍必須為前 120 秒')
        if not isinstance(force_asr, bool):
            raise ValueError('強制語音辨識選項格式無效')
        # Keep the fingerprint of requests already queued before force_asr was added.
        return self.submit(video_id, ['translation'], {}, ['zh-TW'],
                           _library_request={'trial_seconds': trial_seconds,
                                             **({'force_asr': True} if force_asr else {})})

    def redo(self, video_id, stage, profile_snapshot, language=None,
             input_result_id=None, *, audio_path=None, video_path=None,
             allow_reasoning=False, output_languages=()):
        if stage not in _STAGES:
            raise ValueError('字幕階段無效')
        if stage == 'translation' and not language:
            raise ValueError('請指定字幕翻譯語言')
        return self.submit(video_id, [stage], {stage: profile_snapshot},
                           [language] if language else [], audio_path=audio_path,
                           video_path=video_path, input_result_id=input_result_id,
                           allow_reasoning=allow_reasoning,
                           output_languages=output_languages,
                           _force_new=True)

    def reorder(self, task_id, position):
        if not isinstance(position, int) or isinstance(position, bool):
            raise ValueError('字幕工作位置無效')
        with self._lock:
            if task_id not in self._queue or self._tasks[task_id]['status'] not in ('queued', 'waiting_for_resume'):
                raise ValueError('字幕工作目前無法排序')
            old_queue = list(self._queue)
            old_updated = self._tasks[task_id]['updated_at']
            self._queue.remove(task_id)
            self._queue.insert(max(0, min(position, len(self._queue))), task_id)
            self._tasks[task_id]['updated_at'] = time.time()
            try:
                self._save_locked()
            except Exception:
                self._queue = old_queue
                self._tasks[task_id]['updated_at'] = old_updated
                raise
        return self.get_task(task_id)

    def apply_new_settings(self, task_id, profile_snapshots):
        if not isinstance(profile_snapshots, dict):
            raise ValueError('字幕設定快照格式無效')
        snapshots = {key: _snapshot_dict(value) for key, value in profile_snapshots.items()}
        with self._lock:
            task = self._tasks.get(task_id)
            if (not task or task.get('library_translation')
                    or task['status'] not in ('queued', 'waiting_for_resume')
                    or task['ever_started'] or task['inputs']):
                raise ValueError('字幕工作已開始，無法套用新設定')
            if any((stage if stage in snapshots else 'text') not in snapshots
                   for stage in task['selected_stages']):
                raise ValueError('缺少所選字幕階段的設定快照')
            previous = task['profile_snapshots']
            previous_key = task['request_key']
            previous_updated = task['updated_at']
            task['profile_snapshots'] = snapshots
            task['request_key'] = _request_key(task['video_id'], task['selected_stages'], snapshots,
                                                task['target_languages'], {
                                                    'result_id': task['input_result_id'],
                                                    'allow_reasoning': task['allow_reasoning'],
                                                    'output_languages': task.get('output_languages', [])})
            task['updated_at'] = time.time()
            try:
                self._save_locked()
            except Exception:
                task['profile_snapshots'] = previous
                task['request_key'] = previous_key
                task['updated_at'] = previous_updated
                raise
        return self.get_task(task_id)

    def cancel(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task['status'] in ('completed', 'failed', 'cancelled'):
                return False
            old_status, old_generation, old_updated = task['status'], task['generation'], task['updated_at']
            old_queue = list(self._queue)
            task['generation'] += 1
            task['status'] = 'cancelling' if task_id in self._active else 'cancelled'
            task['updated_at'] = time.time()
            if task['status'] == 'cancelled' and task_id in self._queue:
                self._queue.remove(task_id)
            try:
                self._save_locked()
            except Exception:
                task['status'], task['generation'], task['updated_at'] = old_status, old_generation, old_updated
                self._queue = old_queue
                raise
            return True

    def resume(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task['status'] not in ('waiting_for_resume', 'failed'):
                raise ValueError('字幕工作目前無法續跑')
            old_status, old_error, old_generation, old_updated = (
                task['status'], task['error'], task['generation'], task['updated_at'])
            old_queue = list(self._queue)
            old_failure_stage = task.pop('failure_stage', None)
            task['generation'] += 1
            task['status'] = 'queued'
            task['error'] = None
            task['updated_at'] = time.time()
            if task_id not in self._queue:
                self._queue.append(task_id)
            try:
                self._save_locked()
            except Exception:
                task['status'], task['error'], task['generation'], task['updated_at'] = (
                    old_status, old_error, old_generation, old_updated)
                if old_failure_stage is not None:
                    task['failure_stage'] = old_failure_stage
                self._queue = old_queue
                raise
        self.start_dispatcher()
        return self.get_task(task_id)

    def start_dispatcher(self):
        with self._lock:
            if self._dispatcher and self._dispatcher.is_alive():
                return
            self._running = True
            self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True,
                                                name='uav-subtitle-dispatcher')
            self._dispatcher.start()

    def stop_dispatcher(self):
        with self._lock:
            self._running = False
            self._save_locked()

    def _dispatch_loop(self):
        while self._running:
            starts = []
            with self._lock:
                while len(self._active) < self._max_concurrent:
                    task_id = next((item for item in self._queue
                                    if item not in self._active and self._tasks[item]['status'] == 'queued'), None)
                    if task_id is None:
                        break
                    self._active[task_id] = True
                    self._queue.remove(task_id)
                    starts.append(task_id)
            for task_id in starts:
                threading.Thread(target=self._run_task, args=(task_id,), daemon=True,
                                 name=f'subtitle-{task_id}').start()
            time.sleep(0.1)

    def _pin_inputs(self, task):
        if task['inputs']:
            missing = [stage for stage in task['selected_stages']
                       if stage != 'vad' and not task['inputs'].get(stage)]
            if missing:
                raise ValueError('字幕工作固定的上游結果不完整，請建立新工作')
            return
        selected = task['selected_stages']
        generated = set()
        pinned = {}
        for stage in selected:
            dependencies = []
            if stage == 'asr':
                dependencies = [('vad', task.get('input_result_id') if len(selected) == 1 else None)]
            elif stage == 'correction':
                dependencies = [('asr', task.get('input_result_id') if len(selected) == 1 else None)]
            elif stage == 'translation':
                source = 'correction' if 'correction' in selected else 'asr'
                explicit_id = task.get('input_result_id')
                if explicit_id:
                    explicit_result = results.get(task['video_id'], explicit_id)
                    if explicit_result is None or explicit_result.stage not in ('asr', 'correction'):
                        raise ValueError('字幕翻譯需要 ASR 或原文後矯正結果')
                    source = explicit_result.stage
                dependencies = [(source, explicit_id)]
            for source, explicit in dependencies:
                if source in generated:
                    pinned[stage] = f"@task:{source}"
                    continue
                result = results.get(task['video_id'], explicit) if explicit else results.adopted(task['video_id'], source)
                if result is None or not result.content_available:
                    raise ValueError(f'缺少可選用的 {source} 階段結果')
                if result.stage != source and not (stage == 'translation' and result.stage == 'correction'):
                    raise ValueError('字幕上游結果類型不符')
                if result.stale and not explicit:
                    raise ValueError('字幕上游結果已過期，請先更新或明確選擇舊版本')
                pinned[stage] = result.result_id
            generated.add(stage)
        with self._lock:
            current = self._tasks[task['task_id']]
            if current['generation'] != task['generation'] or current['status'] != 'running':
                raise engine.SubtitleCancelled('Subtitle generation cancelled')
            if not current['inputs']:
                previous_updated = current['updated_at']
                current['inputs'] = pinned
                current['updated_at'] = time.time()
                try:
                    self._save_locked()
                except Exception:
                    current['inputs'] = {}
                    current['updated_at'] = previous_updated
                    raise

    def _input_for(self, task, stage):
        value = task['inputs'].get(stage)
        if value and value.startswith('@task:'):
            source = value.split(':', 1)[1]
            return task['results'].get(source)
        return value

    def _audio_for(self, task, cancel_check, video_path=None):
        if task.get('audio_path'):
            return task['audio_path']
        video_path = video_path or task.get('video_path')
        if not video_path:
            return None
        destination = self._state_path.parent / 'subtitle-task-audio' / task['task_id'] / 'audio.wav'
        if not destination.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial = destination.with_name('audio.part.wav')
            try:
                partial.unlink(missing_ok=True)
                engine._extract_audio(video_path, str(partial), str(destination.parent / 'audio.log'), cancel_check)
                if not partial.is_file() or partial.stat().st_size == 0:
                    raise ValueError('音訊擷取未產生完整檔案')
                os.replace(partial, destination)
            finally:
                partial.unlink(missing_ok=True)
        return str(destination)

    def _cleanup_audio(self, task_id):
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            raise ValueError('字幕工作識別碼格式錯誤')
        folder = self._state_path.parent / 'subtitle-task-audio' / task_id
        shutil.rmtree(folder, ignore_errors=True)

    def _library_track_path(self, task_id, language):
        return self._state_path.parent / 'subtitle-library-results' / task_id / (language + '.srt')

    def library_track(self, task_id, language):
        task = self.get_task(task_id)
        if (language not in ('ja', 'zh-TW') or not task
                or not task.get('library_translation') or task['status'] != 'completed'):
            raise FileNotFoundError()
        path = self._library_track_path(task_id, language)
        if not path.is_file():
            raise FileNotFoundError()
        return engine._read_srt_text(str(path))

    @staticmethod
    def _library_cues(text, limit):
        cues = engine.parse_srt(text)
        if len(cues) > engine.MAX_WHISPER_CUES:
            raise ValueError('日文字幕片段過多')
        normalized = []
        for cue in cues:
            start_text, end_text = cue.timing.split('-->', 1)
            start = engine._parse_cli_timestamp(start_text.strip().replace('.', ','))
            end = engine._parse_cli_timestamp(end_text.strip().partition(' ')[0].replace('.', ','))
            if start >= end:
                raise ValueError('日文字幕結束時間必須晚於開始時間')
            if limit is not None:
                if start >= limit * 1000:
                    continue
                end = min(end, limit * 1000)
            normalized.append((start, end, cue.text))
        return sorted(normalized, key=lambda cue: cue[:2])

    @staticmethod
    def _library_asr(video_path, temp_dir, limit, progress, cancel, *, force_asr=False):
        wav = os.path.join(temp_dir, 'audio.wav')
        log = os.path.join(temp_dir, 'audio.log')
        output = os.path.join(temp_dir, 'japanese')
        progress('audio', None)
        engine._extract_audio(video_path, wav, log, cancel, duration_seconds=limit)
        _, duration = engine._validate_pcm16_wav(wav)
        if sys.platform == 'win32':
            progress('model', None, asr_service='native')
            if force_asr:
                raise LibraryTranslationError('強制語音辨識需要受管 Speaches 服務')
            profile = engine.recognition_profile()
            with engine._recognition_profile_scope(profile):
                runtime = engine._prepare_runtime(progress, cancel)
                path = engine._run_recognition(profile, *runtime, wav, output, log,
                                               cancel, progress_callback=progress)
        else:
            settings = asr.load_settings()
            host = urlsplit(settings['base_url']).hostname
            if settings['base_url'] == asr.DEFAULT_URL or host in ('127.0.0.1', '::1', 'localhost'):
                settings = asr.load_settings(secret=True)
                if urlsplit(settings['base_url']).hostname not in (
                        'uav-speaches', '127.0.0.1', '::1', 'localhost'):
                    raise LibraryTranslationError('本機語音辨識設定已變更，請重試')
            else:
                settings = {'base_url': asr.DEFAULT_URL, 'model': asr.DEFAULT_MODEL}
            service = 'speaches' if urlsplit(settings['base_url']).hostname == 'uav-speaches' else 'loopback'
            progress('transcribe_ja', None, asr_service=service)
            managed = settings['base_url'] == asr.DEFAULT_URL
            if force_asr and not managed:
                raise LibraryTranslationError('強制語音辨識需要受管 Speaches 服務')
            transcribe = asr.transcribe_library if managed else asr.transcribe
            try:
                path = transcribe(settings, wav, output, log, cancel, progress)
            except (engine.SubtitleCancelled, engine.SubtitleStorageError):
                raise
            except asr.LibraryQualityError as exc:
                raise LibraryTranslationError(str(exc)) from exc
            except (engine.SubtitleError, OSError, ValueError) as exc:
                raise LibraryTranslationError('本機語音辨識失敗，請檢查 Speaches 或本機辨識服務及模型') from exc
        return path, duration

    def _publish_library_translation(self, task, cues):
        result = SimpleNamespace(
            video_id=task['video_id'], result_id=task['task_id'],
            stage='translation', language='zh-TW',
            segments=[SubtitleSegment(str(index), start / 1000, end / 1000, text)
                      for index, (start, end, text) in enumerate(cues, 1)])
        return self._publish_locked(task['video_id'], result, 'zh-TW', task['task_id'])

    def _run_library_translation(self, task, generation):
        from uav_downloader.daemon.subtitle_files import read_video_subtitle, subtitle_cue_text
        from uav_downloader.subtitles.domain import is_sound_only_cue

        task_id, video_id = task['task_id'], task['video_id']
        limit = task['trial_seconds']
        force_asr = task.get('force_asr', False)
        cancel = lambda: self._cancelled(task_id, generation)

        def progress(phase, percent, *, processed=None, duration=None, count=None, total=None,
                     source_kind=None, asr_service=None):
            stage = {'subtitle_source': 'prepare', 'audio': 'audio', 'model': 'asr',
                     'transcribe_ja': 'asr', 'save': 'save', 'publish': 'publish'}.get(
                         phase, 'asr' if phase.startswith('asr_') else 'translation')
            with self._lock:
                engine._check_cancel(cancel)
                current = self._tasks[task_id]
                current.update(phase=phase, current_stage=stage,
                               stage_progress=percent, progress=percent,
                               updated_at=time.time())
                if processed is not None:
                    current['processed_seconds'] = processed
                if duration is not None:
                    current['media_duration_seconds'] = duration
                if count is not None:
                    current.update(processed_cues=count, total_cues=total)
                if phase == 'subtitle_source' and source_kind is None:
                    for key in ('library_source', 'library_asr_service', 'processed_cues', 'total_cues',
                                'processed_seconds', 'media_duration_seconds'):
                        current.pop(key, None)
                if source_kind is not None:
                    current['library_source'] = source_kind
                if asr_service is not None:
                    current['library_asr_service'] = asr_service
                self._save_locked()

        progress('subtitle_source', None)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._source_manager.resolve(video_id) as source, tempfile.TemporaryDirectory(
                prefix='subtitle-library-', dir=self._state_path.parent) as work:
            verify = self._source_manager.verify_active
            verify(video_id, source)
            duration = None
            track_paths = [] if force_asr else [source.video_path]
            if not force_asr and source.output_mode == 'upload':
                track_paths.append(source.publish_path)
            with engine._generation_slot(cancel):
                for track_path in track_paths:
                    try:
                        original_text = read_video_subtitle(track_path, 'ja')
                    except FileNotFoundError:
                        continue
                    progress('subtitle_source', None, source_kind='existing_srt')
                    break
                else:
                    progress('audio', None, source_kind='asr')
                    path, duration = self._library_asr(source.video_path, work, limit, progress, cancel,
                                                       force_asr=force_asr)
                    original_text = engine._read_srt_text(path) if path else ''
                cues = self._library_cues(original_text, limit)
                dialogue = [cue for cue in cues if not is_sound_only_cue(cue[2])]
                if not dialogue:
                    raise LibraryTranslationError('處理範圍中沒有需要翻譯的對白')
                translated = []
                processed = 0
                progress('translate_zh', 0, processed=0, duration=duration,
                         count=0, total=len(dialogue))
                with engine._translation_profile_scope(None):
                    for offset in range(0, len(dialogue), 96):
                        batch = dialogue[offset:offset + 96]
                        def report_translation(phase, percent):
                            if phase == 'translation_model':
                                progress(phase, percent)
                            elif percent is not None:
                                progress('translate_zh',
                                         (offset * 100 + len(batch) * percent) // len(dialogue))

                        texts = engine.translate_cues(
                            [cue[2] for cue in batch], 'ja', 'zh-TW', 'translate_zh',
                            report_translation, cancel)
                        if len(texts) != len(batch):
                            raise ValueError('字幕翻譯結果句數不符')
                        translated.extend((start, end, clean) for (start, end, _), text in zip(batch, texts)
                                          if (clean := subtitle_cue_text(text)))
                        processed = max(processed, max(cue[1] for cue in batch) / 1000)
                        finished = offset + len(batch)
                        progress('translate_zh', finished * 100 // len(dialogue),
                                 processed=processed, count=finished, total=len(dialogue))
                if not translated:
                    raise LibraryTranslationError('翻譯模型未產生可顯示的繁中字幕')
            verify(video_id, source)
        progress('save', None)
        for language, rows in [('ja', cues), ('zh-TW', translated)]:
            path = self._library_track_path(task_id, language)
            path.parent.mkdir(parents=True, exist_ok=True)
            formatted = [engine.SrtCue(
                str(index), f'{engine._format_srt_timestamp(start / 1000)} --> '
                f'{engine._format_srt_timestamp(end / 1000)}', text)
                for index, (start, end, text) in enumerate(rows, 1)]
            engine._atomic_write_text(str(path), engine.render_srt(formatted))
        if limit is None:
            progress('publish', None)
        with self._lock:
            engine._check_cancel(cancel)
            current = self._tasks[task_id]
            if limit is None:
                self._remember_publish_head(current, 'zh-TW')
                try:
                    published = self._publish_library_translation(task, translated)
                    publication = {'status': 'published', 'path': published['path']}
                except Exception as exc:
                    publication = {'status': 'failed', 'error': _safe_error('publish', exc)}
                current['publish_status']['zh-TW'] = {'result_id': task_id, **publication}
            current.update(status='completed', current_stage=None, progress=100,
                           stage_progress=100, phase='done', updated_at=time.time())
            if task_id in self._queue:
                self._queue.remove(task_id)
            self._save_locked()

    def _run_task(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task['status'] != 'queued':
                self._active.pop(task_id, None)
                return
            task['status'] = 'running'
            previous_generation = task['generation']
            previous_ever_started = task['ever_started']
            task['generation'] += 1
            generation = task['generation']
            task['ever_started'] = True
            task['updated_at'] = time.time()
            try:
                self._save_locked()
            except OSError:
                task['status'] = 'waiting_for_resume'
                task['generation'] = previous_generation
                task['ever_started'] = previous_ever_started
                task['error'] = '字幕工作狀態無法保存，請手動續跑'
                if task_id in self._queue:
                    self._queue.remove(task_id)
                self._active.pop(task_id, None)
                logging.warning('字幕工作狀態無法保存；已停止自動重試，請手動續跑')
                return
            task = self._copy(task)
        video_lock = self._video_lock(task['video_id'])
        video_locked = False
        try:
            while not video_lock.acquire(timeout=0.1):
                if self._cancelled(task_id, generation):
                    raise engine.SubtitleCancelled('Subtitle generation cancelled')
            video_locked = True
            if task.get('library_translation'):
                self._run_library_translation(task, generation)
                return
            with self._source_manager.resolve(task['video_id']):
                pass
            self._pin_inputs(task)
            task = self.get_task(task_id)
            for stage in task['selected_stages']:
                languages = task['target_languages'] if stage == 'translation' else [None]
                for language in languages:
                    key = f'{stage}:{language or ""}'
                    if key in task['completed']:
                        continue
                    with self._lock:
                        current = self._tasks[task_id]
                        if current['generation'] != generation or current['status'] != 'running':
                            raise engine.SubtitleCancelled('Subtitle generation cancelled')
                        current.update(current_stage=stage, current_language=language, phase=stage,
                                       stage_progress=None, progress=None, updated_at=time.time())
                        self._save_locked()
                    input_id = self._input_for(task, stage)
                    if stage != 'vad' and not input_id:
                        raise ValueError(f'字幕階段 {stage} 缺少明確的上游結果')
                    cancel_check = lambda: self._cancelled(task_id, generation)
                    snapshot_key = stage if stage in task['profile_snapshots'] else 'text'
                    snapshot = _snapshot_from_dict(task['profile_snapshots'][snapshot_key])
                    def progress(stage_name, percent):
                        with self._lock:
                            current = self._tasks.get(task_id)
                            if (current and current['generation'] == generation
                                    and current['status'] in ('running', 'retry_wait')):
                                current['stage_progress'] = percent
                                current['progress'] = percent
                                current['phase'] = stage_name
                                current['updated_at'] = time.time()
                                self._save_locked()

                    first_attempt = task.get('retry_attempts', {}).get(key, 0)
                    for attempt in range(first_attempt, _RETRY_LIMIT + 1):
                        try:
                            with self._source_manager.resolve(task['video_id']) as source:
                                verify = getattr(self._source_manager, 'verify_active', None)
                                if verify:
                                    verify(task['video_id'], source)
                                audio_path = (self._audio_for(task, cancel_check, source.video_path)
                                              if stage in ('vad', 'asr') else None)
                                result = engine.run_stage(
                                    task['video_id'], audio_path, stage, snapshot,
                                    target_language=language, input_result_id=input_id,
                                    allow_reasoning=task['allow_reasoning'],
                                    cancel_check=cancel_check, progress_callback=progress,
                                    execution_id=task['execution_id'],
                                    commit_guard=lambda commit, tid=task_id, gen=generation, key=key,
                                        source_id=task['video_id'], active_source=source:
                                        self._commit_guard(tid, gen, key, commit, source_id, active_source),
                                )
                            break
                        except Exception as exc:
                            if attempt >= _RETRY_LIMIT or not engine.is_retryable_stage_error(exc) or cancel_check():
                                raise
                            wait_seconds = _retry_delay(attempt)
                            with self._lock:
                                current = self._tasks[task_id]
                                if current['generation'] != generation or current['status'] != 'running':
                                    raise engine.SubtitleCancelled('Subtitle generation cancelled')
                                current['status'] = 'retry_wait'
                                current['retry_attempts'][key] = attempt + 1
                                current['retry_delay_seconds'] = wait_seconds
                                current['next_retry_at'] = time.time() + wait_seconds
                                current['updated_at'] = time.time()
                                self._save_locked()
                            _wait_for_retry(wait_seconds, cancel_check)
                            with self._lock:
                                current = self._tasks[task_id]
                                if current['generation'] != generation or current['status'] != 'retry_wait':
                                    raise engine.SubtitleCancelled('Subtitle generation cancelled')
                                current['status'] = 'running'
                                current['retry_delay_seconds'] = None
                                current['next_retry_at'] = None
                                current['updated_at'] = time.time()
                                self._save_locked()
                    with self._lock:
                        current = self._tasks[task_id]
                        if current['generation'] != generation or current['status'] != 'running':
                            raise engine.SubtitleCancelled('Subtitle generation cancelled')
                        current['results'][key] = result.result_id
                        current['completed'].append(key)
                        current['retry_attempts'].pop(key, None)
                        current['retry_delay_seconds'] = None
                        current['next_retry_at'] = None
                        if stage != 'translation':
                            current['results'][stage] = result.result_id
                        current['current_stage'] = None
                        current['current_language'] = None
                        current['updated_at'] = time.time()
                        self._save_locked()
                    task = self.get_task(task_id)
            self._publish_completed(task_id, generation)
            with self._lock:
                current = self._tasks[task_id]
                if current['generation'] == generation and current['status'] == 'running':
                    current['status'] = 'completed'
                    current['progress'] = 100
                    current['updated_at'] = time.time()
                    if task_id in self._queue:
                        self._queue.remove(task_id)
                    self._save_locked()
        except Exception as exc:
            cancelled = isinstance(exc, engine.SubtitleCancelled) or self._cancelled(task_id, generation)
            with self._lock:
                current = self._tasks.get(task_id)
                if current:
                    if current['status'] == 'cancelling' or cancelled:
                        current['status'] = 'cancelled'
                        current['error'] = None
                    else:
                        current['status'] = 'failed'
                        current['failure_stage'] = current.get('current_stage') or 'prepare'
                        current['error'] = _safe_error(current.get('current_stage'), exc)
                    current['current_stage'] = None
                    current['current_language'] = None
                    current['updated_at'] = time.time()
                    if task_id in self._queue:
                        self._queue.remove(task_id)
                    self._save_locked()
        finally:
            if video_locked:
                with self._lock:
                    current = self._tasks.get(task_id)
                    remove_tracks = (current and current.get('library_translation')
                                     and current['status'] in ('cancelled', 'failed'))
                if remove_tracks:
                    shutil.rmtree(self._library_track_path(task_id, 'ja').parent, ignore_errors=True)
                video_lock.release()
            with self._lock:
                current = self._tasks.get(task_id)
                cleanup_audio = current and current['status'] in ('completed', 'cancelled', 'failed')
                self._active.pop(task_id, None)
            if cleanup_audio:
                self._cleanup_audio(task_id)

    def _cancelled(self, task_id, generation):
        with self._lock:
            task = self._tasks.get(task_id)
            return not task or task['generation'] != generation or task['status'] not in ('running', 'retry_wait')

    def _commit_guard(self, task_id, generation, stage_key, commit, video_id=None, active_source=None):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task['generation'] != generation or task['status'] != 'running':
                raise engine.SubtitleCancelled('Subtitle generation cancelled')
            if video_id:
                verify = getattr(self._source_manager, 'verify_active', None)
                if verify and active_source is not None:
                    verify(video_id, active_source)
                else:
                    with self._source_manager.resolve(video_id):
                        pass
            result = commit()
            task['results'][stage_key] = result.result_id
            task['updated_at'] = time.time()
            self._save_locked()
            return result

    def _publish_completed(self, task_id, generation):
        """Publish only result IDs produced by this task; keep model status independent."""
        from uav_downloader.subtitles import results
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task['generation'] != generation or task['status'] != 'running':
                raise engine.SubtitleCancelled('Subtitle generation cancelled')
            video_id = task['video_id']
            outputs = list(task.get('output_languages', []))
            completed = dict(task['results'])
        for language in outputs:
            stage_keys = ([f'translation:{language}'] if language != 'ja' else [])
            if language == 'ja':
                stage_keys = ['correction:', 'asr:']
            result_id = next((completed[key] for key in stage_keys if completed.get(key)), None)
            if not result_id:
                continue
            with self._lock:
                task = self._tasks.get(task_id)
                if not task or task['generation'] != generation or task['status'] != 'running':
                    raise engine.SubtitleCancelled('Subtitle generation cancelled')
                task['publish_status'][language] = {'status': 'publishing', 'result_id': result_id}
                self._remember_publish_head(task, language)
                task['updated_at'] = time.time()
                self._save_locked()
            try:
                value = results.get(video_id, result_id)
                if value is None:
                    raise ValueError('字幕結果不存在')
                self._publish_locked(video_id, value, language, task_id)
                status = {'status': 'published', 'result_id': result_id}
            except Exception as exc:
                status = {'status': 'failed', 'result_id': result_id,
                          'error': _safe_error('publish', exc)}
            with self._lock:
                task = self._tasks.get(task_id)
                if task and task['generation'] == generation:
                    task['publish_status'][language] = status
                    task['updated_at'] = time.time()
                    self._save_locked()

    def retry_publish(self, task_id, language):
        """Retry storage only for a completed model result; never call a model stage."""
        if language not in ('ja', 'en', 'zh-TW'):
            raise ValueError('字幕輸出語言無效')
        with self._lock:
            task = self._tasks.get(task_id)
            entry = task.get('publish_status', {}).get(language) if task else None
            if not task or task['status'] != 'completed' or not entry or entry.get('status') != 'failed':
                raise ValueError('字幕發布目前無法重試')
            video_id, result_id = task['video_id'], entry['result_id']
        from uav_downloader.subtitles import results
        with self._video_lock(video_id):
            with self._lock:
                task = self._tasks[task_id]
                if task['publish_status'][language]['status'] != 'failed':
                    raise ValueError('字幕發布目前無法重試')
                self._check_publish_retry(task, language)
            if task.get('library_translation'):
                path = self._library_track_path(task_id, language)
                track = engine._read_srt_text(str(path))
                published = self._publish_library_translation(task, self._library_cues(track, None))
            else:
                result = results.get(video_id, result_id)
                if result is None:
                    raise ValueError('字幕結果不存在')
                published = self._publish_locked(video_id, result, language, task_id)
            with self._lock:
                task = self._tasks[task_id]
                task['publish_status'][language] = {'status': 'published', 'result_id': result_id,
                                                    'path': published['path']}
                task['updated_at'] = time.time()
                self._save_locked()
        return self.get_task(task_id)

    def restore_previous(self, video_id, stage, language=None, *, output_language=None,
                         replace_existing=False):
        """Adopt a prior success; publish text only when an output is requested."""
        if stage not in ('vad', 'asr', 'correction', 'translation'):
            raise ValueError('此字幕階段沒有可回復結果')
        if stage == 'translation' and not language:
            raise ValueError('請指定字幕翻譯語言')
        if stage == 'vad' and output_language is not None:
            raise ValueError('VAD 結果不能發布為字幕')
        if output_language is not None and output_language not in ('ja', 'en', 'zh-TW'):
            raise ValueError('字幕輸出語言無效')
        if output_language is not None and (
                (stage in ('asr', 'correction') and output_language != 'ja') or
                (stage == 'translation' and output_language != language)):
            raise ValueError('字幕結果階段與輸出語言不符')
        if not isinstance(replace_existing, bool):
            raise ValueError('字幕替換選項無效')
        with self._video_lock(video_id):
            restored = results.restore_previous(video_id, stage, language)
            if output_language is None:
                return {'publication_id': None, 'video_id': video_id,
                        'result_id': restored.result_id, 'language': None,
                        'restore_status': 'restored', 'publish_status': 'not_requested'}
            publication_id = 'subtitle_pub_' + uuid.uuid4().hex
            publication = {
                'publication_id': publication_id, 'video_id': video_id,
                'result_id': restored.result_id, 'language': output_language,
                'replace_existing': replace_existing, 'purpose': 'restore',
                'restore_status': 'restored', 'publish_status': 'publishing',
                'created_at': time.time(),
            }
            with self._lock:
                self._publications[publication_id] = publication
                self._remember_publish_head(publication, output_language)
                self._save_locked()
            status = self._publish_restored_locked(publication)
        return {'publication_id': publication_id, 'video_id': video_id,
                'result_id': restored.result_id, 'language': output_language,
                'restore_status': 'restored', 'publish_status': status['publish_status'],
                'path': status.get('path'), 'error': status.get('error')}

    def _publish_restored_locked(self, publication):
        """Publish a persisted restore selection without invoking model code."""
        video_id, result_id = publication['video_id'], publication['result_id']
        try:
            value = results.get(video_id, result_id)
            if value is None:
                raise ValueError('字幕結果不存在')
            published = self._publish_locked(
                video_id, value, publication['language'], publication['publication_id'],
                replace_existing=publication.get('replace_existing', False))
            status = {'publish_status': 'published', 'path': published['path']}
        except Exception as exc:
            status = {'publish_status': 'failed', 'error': _safe_error('publish', exc)}
        with self._lock:
            current = self._publications[publication['publication_id']]
            if status['publish_status'] == 'published':
                current.pop('error', None)
            current.update(status)
            current['updated_at'] = time.time()
            self._save_locked()
        return status

    def publish_result(self, video_id, result_id, language, *, replace_existing):
        """Persist and publish one selected stored text result without model work."""
        from uav_downloader.subtitles import results
        if language not in ('ja', 'en', 'zh-TW') or not isinstance(replace_existing, bool):
            raise ValueError('字幕輸出語言或替換選項無效')
        value = results.get(video_id, result_id)
        if value is None or value.stage not in ('asr', 'correction', 'translation'):
            raise ValueError('所選字幕結果不存在或沒有可發布文字')
        if ((value.stage in ('asr', 'correction') and language != 'ja') or
                (value.stage == 'translation' and value.language != language)):
            raise ValueError('字幕結果階段與輸出語言不符')
        publication_id = 'subtitle_pub_' + uuid.uuid4().hex
        publication = {
            'publication_id': publication_id, 'video_id': video_id,
            'result_id': result_id, 'language': language,
            'replace_existing': replace_existing, 'purpose': 'selected',
            'restore_status': 'not_requested', 'publish_status': 'publishing',
            'created_at': time.time(),
        }
        with self._video_lock(video_id):
            with self._lock:
                self._publications[publication_id] = publication
                self._remember_publish_head(publication, language)
                self._save_locked()
            status = self._publish_restored_locked(publication)
        return {'publication_id': publication_id, 'video_id': video_id,
                'result_id': result_id, 'language': language,
                'restore_status': 'not_requested', 'publish_status': status['publish_status'],
                'path': status.get('path'), 'error': status.get('error')}

    def retry_restored_publish(self, publication_id):
        """Retry only storage for a restored result recorded in the task store."""
        if not isinstance(publication_id, str) or not re.fullmatch(r'subtitle_pub_[0-9a-f]{32}', publication_id):
            raise ValueError('字幕發布識別碼格式錯誤')
        with self._lock:
            publication = self._publications.get(publication_id)
            if (not publication or publication.get('purpose') not in ('restore', 'selected')
                    or publication.get('publish_status') != 'failed'):
                raise ValueError('字幕發布目前無法重試')
        with self._video_lock(publication['video_id']):
            with self._lock:
                publication = self._publications[publication_id]
                if publication['publish_status'] != 'failed':
                    raise ValueError('字幕發布目前無法重試')
                self._check_publish_retry(publication, publication['language'])
            status = self._publish_restored_locked(publication)
        return {'publication_id': publication_id, 'video_id': publication['video_id'],
                'result_id': publication['result_id'], 'language': publication['language'],
                'restore_status': publication.get('restore_status', 'not_requested'),
                'publish_status': status['publish_status'],
                'path': status.get('path'), 'error': status.get('error')}
