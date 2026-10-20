import json
from contextlib import contextmanager
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from uav_downloader.subtitles.models import StageRequestError
from uav_downloader.subtitles.profiles import ProfileSnapshot


SNAPSHOT = ProfileSnapshot(
    profile_json='{"capabilities":["vad"],"profile_id":"vad"}',
    credential_ref=None,
    snapshot_id='snapshot-vad',
)
ASR_SNAPSHOT = ProfileSnapshot(
    profile_json='{"capabilities":["asr"],"profile_id":"asr"}',
    credential_ref=None,
    snapshot_id='snapshot-asr',
)
TEXT_SNAPSHOT = ProfileSnapshot(
    profile_json='{"capabilities":["translation"],"profile_id":"text"}',
    credential_ref=None,
    snapshot_id='snapshot-text',
)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from uav_downloader.daemon.subtitle_tasks import SubtitleTaskManager

    class Sources:
        @contextmanager
        def resolve(self, _video_id):
            yield SimpleNamespace(video_path='/tmp/resolved-video.mp4')

        def publish_result(self, *_args, **_kwargs):
            raise AssertionError('test did not request subtitle publication')

    monkeypatch.setattr(SubtitleTaskManager, 'start_dispatcher', lambda self: None)
    return SubtitleTaskManager(state_path=tmp_path / 'subtitle_tasks.json', source_manager=Sources())


def submit(manager, **kwargs):
    video_id = kwargs.pop('video_id', 'video-1')
    return manager.submit(
        video_id, ['vad'], {'vad': SNAPSHOT}, [],
        audio_path='/tmp/video.wav', **kwargs)


def test_submit_persists_once_and_deduplicates_an_unfinished_request(manager):
    first = submit(manager)
    duplicate = submit(manager)

    assert duplicate['task_id'] == first['task_id']
    assert manager.list_tasks()[0]['task_id'] == first['task_id']
    assert len(json.loads(manager._state_path.read_text())['tasks']) == 1


def test_output_languages_are_separate_from_translation_targets(manager):
    task = manager.submit('video-1', ['translation'], {'translation': TEXT_SNAPSHOT}, ['en'],
                          output_languages=['ja'])
    assert task['target_languages'] == ['en']
    assert task['output_languages'] == ['ja']


def test_retry_publish_reuses_result_without_calling_model(manager, monkeypatch):
    from uav_downloader.subtitles import engine, results

    task = submit(manager)
    manager._tasks[task['task_id']]['status'] = 'completed'
    manager._tasks[task['task_id']]['publish_status'] = {
        'en': {'status': 'failed', 'result_id': 'saved-result'}}
    manager._save()
    monkeypatch.setattr(results, 'get', lambda _video, result_id: SimpleNamespace(
        result_id=result_id, video_id='video-1', stage='translation', language='en',
        segments=(), content_available=True))
    published = []
    manager._source_manager.publish_result = lambda *_a, **_k: published.append('write') or {'path': 'movie.en.srt'}
    monkeypatch.setattr(engine, 'run_stage', lambda *_a, **_k: pytest.fail('retry_publish called a model stage'))

    retried = manager.retry_publish(task['task_id'], 'en')

    assert published == ['write']
    assert retried['status'] == 'completed'
    assert retried['publish_status']['en']['status'] == 'published'


def test_model_success_and_publish_failure_are_independent_and_retry_write_only(manager, monkeypatch):
    from uav_downloader.subtitles import engine, results

    task = manager.submit('video-1', ['translation'], {'translation': TEXT_SNAPSHOT}, ['en'],
                          output_languages=['en'])
    generated = SimpleNamespace(result_id='generated-result', video_id='video-1', stage='translation',
                                language='en', segments=(), content_available=True)
    monkeypatch.setattr(results, 'adopted', lambda *_a: SimpleNamespace(
        result_id='asr-source', stage='asr', content_available=True, stale=False))
    monkeypatch.setattr(results, 'get', lambda _video, _result_id=None: generated)
    model_calls = []
    def run_stage(*_args, commit_guard, **_kwargs):
        model_calls.append(1)
        return commit_guard(lambda: generated)
    monkeypatch.setattr(engine, 'run_stage', run_stage)
    writes = []
    def fail_once(*_args, **_kwargs):
        writes.append('attempt')
        if len(writes) == 1:
            raise OSError('NAS unavailable')
        return {'path': 'movie.en.srt'}
    manager._source_manager.publish_result = fail_once

    manager._run_task(task['task_id'])
    failed = manager.get_task(task['task_id'])
    assert failed['status'] == 'completed'
    assert failed['publish_status']['en']['status'] == 'failed'
    assert len(model_calls) == 1

    monkeypatch.setattr(engine, 'run_stage', lambda *_a, **_k: pytest.fail('write retry invoked model'))
    retried = manager.retry_publish(task['task_id'], 'en')
    assert retried['publish_status']['en']['status'] == 'published'
    assert len(model_calls) == 1
    assert len(writes) == 2


def test_retry_publish_waits_for_same_video_task_lock(manager, monkeypatch):
    from uav_downloader.subtitles import results

    task = submit(manager)
    manager._tasks[task['task_id']]['status'] = 'completed'
    manager._tasks[task['task_id']]['publish_status'] = {
        'en': {'status': 'failed', 'result_id': 'saved-result'}}
    manager._save()
    lock = manager._video_locks.setdefault('video-1', threading.Lock())
    lock.acquire()
    looked_up = threading.Event()
    monkeypatch.setattr(results, 'get', lambda *_a: looked_up.set() or SimpleNamespace(
        result_id='saved-result', video_id='video-1', stage='translation',
        language='en', segments=(), content_available=True))
    manager._source_manager.publish_result = lambda *_a, **_k: {'path': 'movie.en.srt'}
    worker = threading.Thread(target=manager.retry_publish, args=(task['task_id'], 'en'))
    worker.start()

    assert not looked_up.wait(.05)
    lock.release()
    worker.join(2)
    assert not worker.is_alive()
    assert looked_up.is_set()


def test_restore_publish_failure_is_persisted_and_retried_after_restart(manager, monkeypatch):
    from uav_downloader.daemon.subtitle_tasks import SubtitleTaskManager
    from uav_downloader.subtitles import engine, results

    restored = SimpleNamespace(result_id='prior-result', video_id='video-1', stage='asr',
                               language=None, segments=(), content_available=True)
    monkeypatch.setattr(results, 'restore_previous', lambda *_a: restored)
    monkeypatch.setattr(results, 'get', lambda *_a: restored)
    writes = []
    def fail_then_publish(*_args, **_kwargs):
        writes.append('write')
        if len(writes) == 1:
            raise OSError('storage offline')
        return {'path': 'movie.ja.srt'}
    manager._source_manager.publish_result = fail_then_publish
    monkeypatch.setattr(engine, 'run_stage', lambda *_a, **_k: pytest.fail('restore retry called a model'))

    failed = manager.restore_previous('video-1', 'asr')
    assert failed['restore_status'] == 'restored'
    assert failed['publish_status'] == 'failed'
    assert failed['publication_id'].startswith('subtitle_pub_')

    saved = json.loads(manager._state_path.read_text())
    saved['publications'][failed['publication_id']]['publish_status'] = 'publishing'
    manager._state_path.write_text(json.dumps(saved))

    restarted = SubtitleTaskManager(state_path=manager._state_path,
                                    source_manager=manager._source_manager)
    assert restarted.get_publication(failed['publication_id'])['publish_status'] == 'failed'
    retried = restarted.retry_restored_publish(failed['publication_id'])

    assert retried['restore_status'] == 'restored'
    assert retried['publish_status'] == 'published'
    assert writes == ['write', 'write']
    persisted = json.loads(manager._state_path.read_text())
    assert persisted['publications'][failed['publication_id']]['publish_status'] == 'published'


def test_reorder_moves_only_waiting_work_and_redo_gets_a_new_execution_id(manager):
    first = submit(manager)
    second = submit(manager, video_id='video-2')

    manager.reorder(second['task_id'], 0)
    redo = manager.redo('video-1', 'vad', SNAPSHOT, audio_path='/tmp/video.wav')

    assert [task['task_id'] for task in manager.list_tasks()[:2]] == [second['task_id'], first['task_id']]
    assert redo['task_id'] not in (first['task_id'], second['task_id'])
    assert redo['execution_id'] == redo['task_id']


def test_restart_marks_queued_and_running_tasks_for_manual_resume(tmp_path, monkeypatch):
    from uav_downloader.daemon.subtitle_tasks import SubtitleTaskManager

    path = tmp_path / 'subtitle_tasks.json'
    manager = SubtitleTaskManager(state_path=path)
    monkeypatch.setattr(manager, 'start_dispatcher', lambda: None)
    queued = submit(manager)
    manager._tasks[queued['task_id']]['status'] = 'running'
    manager._save()

    restored = SubtitleTaskManager(state_path=path)

    assert restored.list_tasks()[0]['status'] == 'waiting_for_resume'
    assert restored._queue == [queued['task_id']]


def test_resume_retries_temporary_error_at_most_twice(manager, monkeypatch):
    from uav_downloader.subtitles import engine
    monkeypatch.setattr('uav_downloader.daemon.subtitle_tasks._retry_delay', lambda _attempt: 0)

    task = submit(manager)
    calls = []

    def run_stage(*args, commit_guard, **kwargs):
        calls.append(kwargs['execution_id'])
        if len(calls) < 3:
            raise StageRequestError('temporary', retryable=True)
        return SimpleNamespace(result_id='result-vad')

    monkeypatch.setattr(engine, 'run_stage', run_stage)
    manager._run_task(task['task_id'])

    assert calls == [task['task_id']] * 3
    assert manager.get_task(task['task_id'])['status'] == 'completed'


def test_manual_resume_keeps_execution_id_and_completed_chunks(manager, monkeypatch):
    from uav_downloader.subtitles import engine

    task = submit(manager)
    manager._tasks[task['task_id']]['status'] = 'waiting_for_resume'
    manager._queue.clear()
    manager._save()
    restored = manager.resume(task['task_id'])
    assert restored['execution_id'] == task['task_id']
    calls = []

    def run_stage(*args, **kwargs):
        calls.append(kwargs['execution_id'])
        return SimpleNamespace(result_id='result-vad')

    monkeypatch.setattr(engine, 'run_stage', run_stage)
    manager._run_task(task['task_id'])

    assert calls == [task['task_id']]
    assert manager.get_task(task['task_id'])['status'] == 'completed'


def test_permanent_stage_error_is_not_retried(manager, monkeypatch):
    from uav_downloader.subtitles import engine

    task = submit(manager)
    calls = []

    def run_stage(*args, **kwargs):
        calls.append(1)
        raise StageRequestError('bad request', retryable=False)

    monkeypatch.setattr(engine, 'run_stage', run_stage)
    manager._run_task(task['task_id'])

    assert calls == [1]
    assert manager.get_task(task['task_id'])['status'] == 'failed'


def test_allow_reasoning_is_persisted_and_passed_to_runner(manager, monkeypatch):
    from uav_downloader.subtitles import engine

    task = manager.submit('video-1', ['vad'], {'vad': SNAPSHOT}, [],
                          audio_path='/tmp/video.wav', allow_reasoning=True)
    observed = []

    def run_stage(*args, **kwargs):
        observed.append(kwargs['allow_reasoning'])
        return SimpleNamespace(result_id='result-vad')

    monkeypatch.setattr(engine, 'run_stage', run_stage)
    manager._run_task(task['task_id'])
    assert observed == [True]
    assert manager.get_task(task['task_id'])['allow_reasoning'] is True


@pytest.mark.parametrize(('stage', 'required_source'), [('asr', 'vad'), ('correction', 'asr')])
def test_redo_honors_explicit_input_result_for_asr_and_correction(
        manager, monkeypatch, stage, required_source):
    from uav_downloader.subtitles import engine, results

    task = manager.redo('video-1', stage, ASR_SNAPSHOT if stage == 'correction' else SNAPSHOT,
                        input_result_id='explicit-input', audio_path='/tmp/video.wav')
    monkeypatch.setattr(results, 'get', lambda _video_id, result_id: SimpleNamespace(
        result_id=result_id, stage=required_source, content_available=True, stale=True))
    observed = []

    def run_stage(*args, **kwargs):
        observed.append(kwargs['input_result_id'])
        return SimpleNamespace(result_id='result-out')

    monkeypatch.setattr(engine, 'run_stage', run_stage)
    manager._run_task(task['task_id'])
    assert observed == ['explicit-input']


def test_retry_wait_is_persisted_and_restored_for_manual_resume(manager, monkeypatch, tmp_path):
    from uav_downloader.subtitles import engine
    from uav_downloader.daemon import subtitle_tasks

    task = submit(manager)
    state = {}
    real_sleep = time.sleep
    def observe_retry_wait(seconds):
        if seconds > 0:
            saved = json.loads(manager._state_path.read_text())
            state.setdefault('status', saved['tasks'][0]['status'])
            state.setdefault('attempts', saved['tasks'][0]['retry_attempts']['vad:'])
            state.setdefault('snapshot', saved)
        real_sleep(seconds)

    monkeypatch.setattr(subtitle_tasks.time, 'sleep', observe_retry_wait)
    def retry_delay(_attempt):
        return .01

    monkeypatch.setattr(subtitle_tasks, '_retry_delay', retry_delay)
    monkeypatch.setattr(engine, 'run_stage', lambda *a, **k: (_ for _ in ()).throw(
        StageRequestError('temporary', retryable=True)))
    manager._run_task(task['task_id'])

    assert state['status'] == 'retry_wait'
    assert state['attempts'] == 1
    assert manager.get_task(task['task_id'])['status'] == 'failed'
    crash_snapshot = tmp_path / 'crash.json'
    crash_snapshot.write_text(json.dumps(state['snapshot']))
    restored = subtitle_tasks.SubtitleTaskManager(state_path=crash_snapshot)
    assert restored.get_task(task['task_id'])['status'] == 'waiting_for_resume'
    assert restored.get_task(task['task_id'])['retry_attempts'] == {'vad:': 1}


def test_progress_callback_is_visible_in_task_snapshot(manager, monkeypatch):
    from uav_downloader.subtitles import engine

    task = submit(manager)
    def run_stage(*args, progress_callback, **kwargs):
        progress_callback('transcribing', 37)
        visible = manager.get_task(task['task_id'])
        assert visible['phase'] == 'transcribing'
        assert visible['stage_progress'] == 37
        assert visible['progress'] == 37
        return SimpleNamespace(result_id='result-vad')

    monkeypatch.setattr(engine, 'run_stage', run_stage)
    manager._run_task(task['task_id'])


def test_http_auth_error_is_specific_and_does_not_echo_url_or_secret(manager, monkeypatch):
    from uav_downloader.subtitles import engine

    task = submit(manager)
    monkeypatch.setattr(engine, 'run_stage', lambda *a, **k: (_ for _ in ()).throw(
        StageRequestError('POST https://private.example/?token=secret HTTP 401', retryable=False)))
    manager._run_task(task['task_id'])
    error = manager.get_task(task['task_id'])['error']
    assert 'HTTP 401' in error
    assert 'private.example' not in error
    assert 'secret' not in error


@pytest.mark.parametrize(('error_kind', 'message', 'expected'), [
    ('value', '字幕上游結果已過期，請先更新', '上游字幕結果已過期'),
    ('llm', '此模型服務停用思考能力未確認，請明確允許思考 at https://private/?token=secret',
     '尚未確認思考控制'),
])
def test_known_domain_errors_are_specific_and_sanitized(manager, monkeypatch, error_kind, message, expected):
    from uav_downloader.subtitles import engine, llm

    task = submit(manager)
    error = ValueError(message) if error_kind == 'value' else llm.LlmTranslationError(message)
    monkeypatch.setattr(engine, 'run_stage', lambda *a, **k: (_ for _ in ()).throw(error))
    manager._run_task(task['task_id'])
    message = manager.get_task(task['task_id'])['error']
    assert expected in message
    assert 'private' not in message
    assert 'secret' not in message


def test_retry_deadline_crossing_never_sleeps_for_negative_time(monkeypatch):
    from uav_downloader.daemon import subtitle_tasks

    values = iter([0.0, 0.2, 0.3])
    waits = []
    monkeypatch.setattr(subtitle_tasks.time, 'monotonic', lambda: next(values))
    monkeypatch.setattr(subtitle_tasks.time, 'sleep', waits.append)

    subtitle_tasks._wait_for_retry(.25, lambda: False)

    assert len(waits) == 1
    assert 0 <= waits[0] <= .05


def test_dispatcher_does_not_retry_after_start_state_persistence_failure(manager, monkeypatch):
    task = submit(manager)
    attempts = []
    failed_once = threading.Event()
    original_save = manager._save_locked

    def fail_running_save_once():
        if manager._tasks[task['task_id']]['status'] == 'running':
            attempts.append(1)
            if len(attempts) == 1:
                failed_once.set()
                raise OSError('disk full')
        original_save()

    monkeypatch.setattr(manager, '_save_locked', fail_running_save_once)
    manager._running = True
    dispatcher = threading.Thread(target=manager._dispatch_loop, daemon=True)
    dispatcher.start()
    assert failed_once.wait(2)
    for _ in range(100):
        if manager.get_task(task['task_id'])['status'] == 'waiting_for_resume':
            break
        time.sleep(.01)
    time.sleep(.25)
    manager._running = False
    dispatcher.join(2)

    assert manager.get_task(task['task_id'])['status'] == 'waiting_for_resume'
    assert task['task_id'] not in manager._queue
    assert attempts == [1]


def test_load_rejects_tampered_task_id_without_touching_external_file(tmp_path):
    from uav_downloader.daemon.subtitle_tasks import SubtitleTaskManager

    outside = tmp_path / 'outside'
    outside.mkdir()
    marker = outside / 'keep.txt'
    marker.write_text('keep')
    state = tmp_path / 'tasks.json'
    contents = {'schema': 1, 'queue': [], 'tasks': [{
        'task_id': '../outside', 'status': 'cancelled', 'generation': 0,
    }]}
    raw = json.dumps(contents)
    state.write_text(raw)

    with pytest.raises(ValueError, match='佇列格式錯誤'):
        SubtitleTaskManager(state_path=state)

    assert state.read_text() == raw
    assert marker.read_text() == 'keep'


def test_failed_task_cleans_generated_audio_and_resume_reextracts(manager, monkeypatch, tmp_path):
    from uav_downloader.daemon import subtitle_tasks
    from uav_downloader.subtitles import engine

    video = tmp_path / 'input.mp4'
    video.write_bytes(b'video')
    task = manager.submit('video-1', ['vad'], {'vad': SNAPSHOT}, [], video_path=str(video))
    extracted = []
    def extract(_video, audio, _log, _cancel):
        extracted.append(audio)
        Path(audio).write_bytes(b'wav')

    runs = []
    def run_stage(*args, **kwargs):
        runs.append(kwargs['execution_id'])
        if len(runs) == 1:
            raise StageRequestError('permanent', retryable=False)
        return SimpleNamespace(result_id='result-vad')

    monkeypatch.setattr(engine, '_extract_audio', extract)
    monkeypatch.setattr(engine, 'run_stage', run_stage)
    manager._run_task(task['task_id'])
    folder = tmp_path / 'subtitle-task-audio' / task['task_id']
    assert manager.get_task(task['task_id'])['status'] == 'failed'
    assert not folder.exists()

    manager.resume(task['task_id'])
    manager._run_task(task['task_id'])
    assert manager.get_task(task['task_id'])['status'] == 'completed'
    assert len(extracted) == 2
    assert runs == [task['task_id'], task['task_id']]
    assert not folder.exists()
    assert video.read_bytes() == b'video'


def test_disabled_correction_pins_translation_to_adopted_asr(manager, monkeypatch):
    from uav_downloader.subtitles import engine, results

    task = manager.submit('video-1', ['translation'], {'translation': TEXT_SNAPSHOT}, ['zh-TW'])
    monkeypatch.setattr(results, 'adopted', lambda _video_id, stage: SimpleNamespace(
        result_id='asr-result', stage=stage, content_available=True, stale=False))
    observed = []

    def run_stage(*args, **kwargs):
        observed.append(kwargs['input_result_id'])
        return SimpleNamespace(result_id='translation-result')

    monkeypatch.setattr(engine, 'run_stage', run_stage)
    manager._run_task(task['task_id'])

    assert observed == ['asr-result']


def test_stale_adopted_upstream_is_not_used_implicitly(manager, monkeypatch):
    from uav_downloader.subtitles import results

    task = manager.submit('video-1', ['asr'], {'asr': ASR_SNAPSHOT}, [])
    monkeypatch.setattr(results, 'adopted', lambda _video_id, _stage: SimpleNamespace(
        result_id='old-vad', stage='vad', content_available=True, stale=True))
    called = []
    monkeypatch.setattr('uav_downloader.subtitles.engine.run_stage', lambda *a, **k: called.append(1))

    manager._run_task(task['task_id'])

    assert not called
    assert manager.get_task(task['task_id'])['status'] == 'failed'


def test_cancelled_late_result_cannot_pass_commit_guard(manager, monkeypatch):
    from uav_downloader.subtitles import engine

    task = submit(manager)
    waiting, release = threading.Event(), threading.Event()
    committed = []

    def run_stage(*args, commit_guard, **kwargs):
        waiting.set()
        assert release.wait(2)
        return commit_guard(lambda: committed.append('result'))

    monkeypatch.setattr(engine, 'run_stage', run_stage)
    worker = threading.Thread(target=manager._run_task, args=(task['task_id'],))
    worker.start()
    assert waiting.wait(2)
    assert manager.cancel(task['task_id'])
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert committed == []
    assert manager.get_task(task['task_id'])['status'] == 'cancelled'


def test_cancelled_waiter_does_not_release_another_tasks_video_lock(manager):
    task = submit(manager)
    lock = threading.Lock()
    lock.acquire()
    manager._video_locks['video-1'] = lock
    worker = threading.Thread(target=manager._run_task, args=(task['task_id'],))
    worker.start()
    for _ in range(100):
        if manager.get_task(task['task_id'])['status'] == 'running':
            break
        time.sleep(.01)

    assert manager.cancel(task['task_id'])
    worker.join(2)

    assert not worker.is_alive()
    assert lock.locked()
    lock.release()


def test_apply_new_settings_only_changes_a_task_that_has_not_started(manager):
    task = submit(manager)
    changed = ProfileSnapshot(SNAPSHOT.profile_json, None, 'new-snapshot')

    manager.apply_new_settings(task['task_id'], {'vad': changed})
    assert manager.get_task(task['task_id'])['profile_snapshots']['vad']['snapshot_id'] == 'new-snapshot'

    manager._tasks[task['task_id']]['status'] = 'running'
    with pytest.raises(ValueError, match='已開始'):
        manager.apply_new_settings(task['task_id'], {'vad': SNAPSHOT})


def test_queue_persistence_failure_does_not_accept_submission(manager, monkeypatch):
    monkeypatch.setattr(manager, '_save_locked', lambda: (_ for _ in ()).throw(OSError('disk full')))

    with pytest.raises(OSError, match='disk full'):
        submit(manager)

    assert not manager.list_tasks()
    assert not manager._queue


@pytest.mark.parametrize('operation', ['reorder', 'resume', 'apply'])
def test_queue_mutations_roll_back_when_persistence_fails(manager, monkeypatch, operation):
    first = submit(manager)
    second = submit(manager, video_id='video-2')
    if operation == 'resume':
        manager._tasks[first['task_id']]['status'] = 'waiting_for_resume'
        manager._queue.remove(first['task_id'])
    before = manager.get_task(first['task_id'])
    before_queue = list(manager._queue)
    monkeypatch.setattr(manager, '_save_locked', lambda: (_ for _ in ()).throw(OSError('disk full')))

    with pytest.raises(OSError):
        if operation == 'reorder':
            manager.reorder(first['task_id'], 1)
        elif operation == 'resume':
            manager.resume(first['task_id'])
        else:
            changed = ProfileSnapshot(SNAPSHOT.profile_json, None, 'new-settings')
            manager.apply_new_settings(first['task_id'], {'vad': changed})

    after = manager.get_task(first['task_id'])
    assert after == before
    assert manager._queue == before_queue
    assert manager.get_task(second['task_id']) is not None


def test_audio_extraction_failure_never_promotes_partial_file(manager, monkeypatch, tmp_path):
    from uav_downloader.daemon import subtitle_tasks

    task = manager.submit('video-1', ['vad'], {'vad': SNAPSHOT}, [],
                          video_path=str(tmp_path / 'video.mp4'))
    def fail_extraction(video, audio, log, cancel):
        Path(audio).write_bytes(b'incomplete')
        raise ValueError('ffmpeg failed')

    monkeypatch.setattr(subtitle_tasks.engine, '_extract_audio', fail_extraction)
    with pytest.raises(ValueError):
        manager._audio_for(task, lambda: False)
    folder = tmp_path / 'subtitle-task-audio' / task['task_id']
    assert not (folder / 'audio.wav').exists()
    assert not (folder / 'audio.part.wav').exists()


def test_completed_task_cleans_only_its_generated_audio(manager, monkeypatch, tmp_path):
    from uav_downloader.daemon import subtitle_tasks
    from uav_downloader.subtitles import engine

    video = tmp_path / 'input.mp4'
    video.write_bytes(b'video')
    provided_audio = tmp_path / 'provided.wav'
    provided_audio.write_bytes(b'audio')
    task = manager.submit('video-1', ['vad'], {'vad': SNAPSHOT}, [],
                          video_path=str(video), audio_path=str(provided_audio))
    generated = tmp_path / 'subtitle-task-audio' / task['task_id'] / 'audio.wav'
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b'generated')
    monkeypatch.setattr(engine, 'run_stage', lambda *a, **k: SimpleNamespace(result_id='result-vad'))

    manager._run_task(task['task_id'])

    assert manager.get_task(task['task_id'])['status'] == 'completed'
    assert not generated.parent.exists()
    assert provided_audio.read_bytes() == b'audio'
    assert video.read_bytes() == b'video'
