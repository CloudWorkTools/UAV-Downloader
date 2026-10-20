"""Persistent queue for independent subtitle work."""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid

from uav_downloader.subtitles import asr, engine, results
from uav_downloader.subtitles.profiles import ProfileSnapshot

_STAGES = ('vad', 'asr', 'correction', 'translation')
_RETRY_LIMIT = 2
_RETRY_DELAYS = (1.0, 3.0)


def _snapshot_dict(snapshot):
    if not isinstance(snapshot, ProfileSnapshot):
        raise ValueError('字幕設定快照格式無效')
    return {
        'profile_json': snapshot.profile_json,
        'credential_ref': snapshot.credential_ref,
        'snapshot_id': snapshot.snapshot_id,
    }


def _snapshot_from_dict(value):
    if not isinstance(value, dict):
        raise ValueError('字幕設定快照格式無效')
    return ProfileSnapshot(value['profile_json'], value.get('credential_ref'), value['snapshot_id'])


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


def _safe_error(stage, error):
    """Return a concise message without exposing endpoint URLs or credentials."""
    from uav_downloader.subtitles.models import StageRequestError

    message = str(error)
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

    def __init__(self, state_path=None, max_concurrent=1):
        self._lock = threading.RLock()
        self._state_path = Path(state_path) if state_path is not None else asr._folder() / 'web_subtitle_tasks.json'
        self._tasks = {}
        self._queue = []
        self._active = {}
        self._video_locks = {}
        self._max_concurrent = max(1, int(max_concurrent))
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
            if not isinstance(task, dict) or not isinstance(task.get('task_id'), str):
                raise ValueError('字幕工作佇列格式錯誤')
            self._tasks[task['task_id']] = task
        self._queue = [task_id for task_id in records.get('queue', []) if task_id in self._tasks]
        changed = False
        for task in self._tasks.values():
            if task['status'] in ('queued', 'running', 'cancelling', 'retry_wait'):
                task['status'] = 'waiting_for_resume'
                task['generation'] += 1
                changed = True
        if changed:
            self._save()

    def _save_locked(self):
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({'schema': 1, 'queue': self._queue,
                           'tasks': list(self._tasks.values())}, ensure_ascii=False,
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

    def submit(self, video_id, selected_stages, profile_snapshots, target_languages,
               *, audio_path=None, video_path=None, input_result_id=None,
               allow_reasoning=False, _force_new=False):
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
            if key not in snapshots:
                raise ValueError('缺少所選字幕階段的設定快照')
        if (not isinstance(target_languages, (list, tuple))
                or any(not isinstance(language, str) for language in target_languages)):
            raise ValueError('字幕翻譯語言無效')
        languages = list(dict.fromkeys(target_languages))
        if languages is None or ('translation' in stages and not languages):
            raise ValueError('請指定字幕翻譯語言')
        if any(not isinstance(language, str) or not language for language in languages):
            raise ValueError('字幕翻譯語言無效')
        fingerprint = _request_key(video_id, stages, snapshots, languages,
                                   {'result_id': input_result_id, 'allow_reasoning': allow_reasoning})
        with self._lock:
            for task in self._tasks.values():
                if not _force_new and task['request_key'] == fingerprint and task['status'] in (
                        'queued', 'running', 'waiting_for_resume', 'retry_wait', 'cancelling'):
                    return self._copy(task)
            task_id = f'subtitle_{uuid.uuid4().hex}'
            task = {
                'task_id': task_id, 'execution_id': task_id, 'video_id': video_id,
                'video_path': os.path.abspath(video_path) if video_path else None,
                'audio_path': os.path.abspath(audio_path) if audio_path else None,
                'selected_stages': stages, 'target_languages': languages,
                'profile_snapshots': snapshots, 'input_result_id': input_result_id,
                'allow_reasoning': allow_reasoning, 'inputs': {}, 'results': {},
                'completed': [], 'retry_attempts': {}, 'retry_limit': _RETRY_LIMIT,
                'retry_delay_seconds': None, 'next_retry_at': None,
                'current_stage': None, 'current_language': None,
                'status': 'queued', 'generation': 0, 'ever_started': False,
                'progress': 0, 'error': None, 'request_key': fingerprint,
                'created_at': time.time(), 'updated_at': time.time(),
            }
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

    def redo(self, video_id, stage, profile_snapshot, language=None,
             input_result_id=None, *, audio_path=None, video_path=None,
             allow_reasoning=False):
        if stage not in _STAGES:
            raise ValueError('字幕階段無效')
        if stage == 'translation' and not language:
            raise ValueError('請指定字幕翻譯語言')
        return self.submit(video_id, [stage], {stage: profile_snapshot},
                           [language] if language else [], audio_path=audio_path,
                           video_path=video_path, input_result_id=input_result_id,
                           allow_reasoning=allow_reasoning,
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
            if (not task or task['status'] not in ('queued', 'waiting_for_resume')
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
                                                    'allow_reasoning': task['allow_reasoning']})
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

    def _audio_for(self, task, cancel_check):
        if task.get('audio_path'):
            return task['audio_path']
        video_path = task.get('video_path')
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
        folder = self._state_path.parent / 'subtitle-task-audio' / task_id
        shutil.rmtree(folder, ignore_errors=True)

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
                task['status'] = 'queued'
                task['generation'] = previous_generation
                task['ever_started'] = previous_ever_started
                if task_id not in self._queue:
                    self._queue.append(task_id)
                self._active.pop(task_id, None)
                logging.exception('字幕工作狀態無法保存，暫停執行')
                return
            task = self._copy(task)
        video_lock = self._video_locks.setdefault(task['video_id'], threading.Lock())
        video_locked = False
        try:
            while not video_lock.acquire(timeout=0.1):
                if self._cancelled(task_id, generation):
                    raise engine.SubtitleCancelled('Subtitle generation cancelled')
            video_locked = True
            self._pin_inputs(task)
            task = self.get_task(task_id)
            for stage in task['selected_stages']:
                languages = task['target_languages'] if stage == 'translation' else [None]
                for language in languages:
                    key = f'{stage}:{language or ""}'
                    if key in task['completed']:
                        continue
                    input_id = self._input_for(task, stage)
                    if stage != 'vad' and not input_id:
                        raise ValueError(f'字幕階段 {stage} 缺少明確的上游結果')
                    with self._lock:
                        current = self._tasks[task_id]
                        if current['generation'] != generation or current['status'] != 'running':
                            raise engine.SubtitleCancelled('Subtitle generation cancelled')
                        current['current_stage'] = stage
                        current['current_language'] = language
                        current['updated_at'] = time.time()
                        self._save_locked()
                    cancel_check = lambda: self._cancelled(task_id, generation)
                    audio_path = self._audio_for(task, cancel_check) if stage in ('vad', 'asr') else None
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
                            result = engine.run_stage(
                                task['video_id'], audio_path, stage, snapshot,
                                target_language=language, input_result_id=input_id,
                                allow_reasoning=task['allow_reasoning'],
                                cancel_check=cancel_check, progress_callback=progress,
                                execution_id=task['execution_id'],
                                commit_guard=lambda commit, tid=task_id, gen=generation, key=key:
                                    self._commit_guard(tid, gen, key, commit),
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
                            deadline = time.monotonic() + wait_seconds
                            while time.monotonic() < deadline:
                                if cancel_check():
                                    raise engine.SubtitleCancelled('Subtitle generation cancelled')
                                time.sleep(min(0.1, deadline - time.monotonic()))
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
                        current['error'] = _safe_error(current.get('current_stage'), exc)
                    current['current_stage'] = None
                    current['current_language'] = None
                    current['updated_at'] = time.time()
                    if task_id in self._queue:
                        self._queue.remove(task_id)
                    self._save_locked()
        finally:
            if video_locked:
                video_lock.release()
            with self._lock:
                current = self._tasks.get(task_id)
                cleanup_audio = current and current['status'] in ('completed', 'cancelled')
                self._active.pop(task_id, None)
            if cleanup_audio:
                self._cleanup_audio(task_id)

    def _cancelled(self, task_id, generation):
        with self._lock:
            task = self._tasks.get(task_id)
            return not task or task['generation'] != generation or task['status'] not in ('running', 'retry_wait')

    def _commit_guard(self, task_id, generation, stage_key, commit):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task['generation'] != generation or task['status'] != 'running':
                raise engine.SubtitleCancelled('Subtitle generation cancelled')
            result = commit()
            task['results'][stage_key] = result.result_id
            task['updated_at'] = time.time()
            self._save_locked()
            return result
