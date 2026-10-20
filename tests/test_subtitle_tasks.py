import json
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

    monkeypatch.setattr(SubtitleTaskManager, 'start_dispatcher', lambda self: None)
    return SubtitleTaskManager(state_path=tmp_path / 'subtitle_tasks.json')


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
