"""Queue identity and cancellation must cover the real worker lifecycle."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from uav_downloader.core.history import DownloadHistoryManager
from uav_downloader.daemon import task_manager as tm


@pytest.fixture
def manager(monkeypatch, tmp_path):
    history = DownloadHistoryManager(tmp_path / 'history.json')
    monkeypatch.setattr(tm, 'get_history_manager', lambda: history)
    monkeypatch.setattr(tm.nas, 'load_settings', lambda **kw: {'enabled': False})
    monkeypatch.setattr(tm.TaskManager, 'start_dispatcher', lambda self: None)
    monkeypatch.setenv('DOWNLOAD_DIR', str(tmp_path / 'videos'))
    return tm.TaskManager()


def job_at(destination, action, name='MIMK-290'):
    job = SimpleNamespace(_dest_folder=destination, _temp_folder=str(Path(destination) / 'segments'),
                          _cancel_job=False)
    job.dest_folder = lambda: job._dest_folder
    job.target_name = lambda: name
    job.is_url_vaildate = lambda: True
    job.cancel_download = lambda cleanup=True: setattr(job, '_cancel_job', True)
    job.start_download = lambda: action(job)
    return job


def test_same_code_is_skipped_across_sites_and_versions(manager):
    first = manager.add_task('https://missav.ai/mimk-290')
    duplicate = manager.add_task('https://jable.tv/videos/MIMK-290-chinese-subtitle/')
    assert duplicate.status == 'skipped'
    assert manager._queue == [first.task_id]
    assert manager.add_task('https://missav.ai/mimk-291').status == 'pending'


def test_history_skips_same_code_without_fuzzy_near_number_match(manager):
    tm.get_history_manager().record_download('https://missav.ai/mimk-290', title='MIMK-290')
    assert manager.add_task('https://jable.tv/videos/mimk-290/').status == 'skipped'
    assert manager.add_task('https://missav.ai/mimk-291').status == 'pending'


def test_existing_code_filename_skips_before_network_parse(manager, monkeypatch, tmp_path):
    folder = tmp_path / 'videos'
    folder.mkdir()
    (folder / 'MIMK-290 中文字幕.mp4').write_bytes(b'original')
    create = MagicMock(side_effect=AssertionError('must skip before parsing'))
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', create)
    task = manager.add_task('https://missav.ai/mimk-290-uncensored-leak')
    manager._run_task(task)
    assert task.status == 'skipped'
    create.assert_not_called()
    assert (folder / 'MIMK-290 中文字幕.mp4').read_bytes() == b'original'


def test_cancel_waits_for_worker_then_removes_only_its_files(manager, monkeypatch, tmp_path):
    task = manager.add_task('https://missav.ai/mimk-290')
    folder = Path(task.dest)
    folder.mkdir()
    (folder / 'other.mp4').write_bytes(b'keep')
    def download(job):
        work = Path(job.dest_folder())
        work.mkdir(parents=True, exist_ok=True)
        Path(job._temp_folder).mkdir(parents=True, exist_ok=True)
        (Path(job._temp_folder) / '000001.ts').write_bytes(b'segment')
        (work / 'MIMK-290.jpg').write_bytes(b'cover')
        (work / 'MIMK-290.mp4.part').write_bytes(b'partial')
        assert manager.cancel_task(task.task_id)
        assert task.status == 'cancelling'
        assert task.task_id in manager._active, 'Do not free a slot while the worker is still writing'
        (work / 'last-write.part').write_bytes(b'late write')
        return False
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    manager._run_task(task)
    assert task.status == 'cancelled'
    assert manager.get_task(task.task_id).status == 'cancelled'
    assert sorted(p.name for p in folder.iterdir()) == ['other.mp4']
    assert task.task_id not in manager._active
    assert tm.get_history_manager().find_downloaded(task.url) is None


def test_subtitle_cancellation_does_not_complete_or_upload(manager, monkeypatch):
    task = manager.add_task('https://missav.ai/mimk-290', enable_subtitles=True, subtitle_mode='ja')
    def download(job):
        Path(job.dest_folder()).mkdir(parents=True, exist_ok=True)
        (Path(job.dest_folder()) / 'MIMK-290.mp4').write_bytes(b'video')
        return True
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    def subtitles(task, job):
        (Path(job.dest_folder()) / 'MIMK-290.ja.srt').write_text('subtitle')
        manager.cancel_task(task.task_id)
    monkeypatch.setattr(manager, '_run_subtitles', subtitles)
    manager._run_task(task)
    assert task.status == 'cancelled'
    assert manager.get_task(task.task_id).status == 'cancelled'
    assert not list(Path(task.dest).iterdir())
    assert tm.get_history_manager().find_downloaded(task.url) is None


def test_completed_tasks_are_not_cancelled_or_deleted(manager, tmp_path):
    task = manager.add_task('https://missav.ai/mimk-290')
    task.status = 'completed'
    assert manager.cancel_task(task.task_id) is False
    manager.cancel_all()
    assert task.status == 'completed'
    assert not task.cancelled_event.is_set()


def test_resolved_title_code_deduplicates_numeric_site_urls(manager, monkeypatch):
    def download(job):
        (Path(job.dest_folder()) / 'MIMK-290.mp4').write_bytes(b'video')
        return True
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    first = manager.add_task('https://supjav.com/111111.html')
    manager._run_task(first)
    assert first.status == 'completed', first.error
    second = manager.add_task('https://supjav.com/222222.html')
    manager._run_task(second)
    assert second.status == 'skipped'
    assert (Path(first.dest) / 'MIMK-290.mp4').read_bytes() == b'video'
    assert not list(Path(first.dest).glob('.uav-*'))


def test_failed_download_can_be_cancelled_and_cleaned(manager, monkeypatch):
    def download(job):
        (Path(job.dest_folder()) / 'partial.part').write_bytes(b'data')
        raise ConnectionError('network lost')
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    task = manager.add_task('https://missav.ai/mimk-290')
    manager._run_task(task)
    assert task.status == 'failed'
    assert Path(task.files.workdir).exists()
    assert manager.cancel_task(task.task_id)
    assert task.status == 'cancelled'
    assert manager.get_task(task.task_id).status == 'cancelled'
    assert not list(Path(task.dest).iterdir())


def test_cleanup_failure_remains_visible_and_retryable(manager, monkeypatch):
    task = manager.add_task('https://missav.ai/mimk-290')
    task.status = 'failed'
    task.files = SimpleNamespace(cleanup=MagicMock(side_effect=OSError('offline')))
    assert manager.cancel_task(task.task_id)
    assert task.status == 'failed'
    assert '清除失敗' in task.error
    assert 'offline' in manager.list_tasks()[0]['error']
    task.files.cleanup.side_effect = None
    assert manager.cancel_task(task.task_id)
    assert task.status == 'cancelled'
    assert manager.get_task(task.task_id).status == 'cancelled'


def test_pending_cancel_never_starts_download(manager):
    task = manager.add_task('https://missav.ai/mimk-290')
    assert manager.cancel_task(task.task_id)
    assert task.status == 'cancelled'
    assert manager.get_task(task.task_id).status == 'cancelled'
    assert task.task_id not in manager._queue
    assert not Path(task.dest).exists()


@pytest.mark.parametrize('status', ['pending', 'preparing', 'downloading', 'subtitles', 'uploading', 'cancelling'])
def test_restart_keeps_unfinished_tasks(manager, tmp_path, status):
    path = tmp_path / 'tasks.json'
    manager._state_path = path
    task = manager.add_task('https://missav.ai/mimk-290', resolution='720', max_workers=3,
                            download_cover=False, enable_subtitles=True, subtitle_mode='zh')
    task.status = status
    task.name = 'MIMK-290'
    task.progress = 42
    task.downloaded_segments = 21
    task.total_segments = 50
    task.nas_settings = {'password': 'must-not-persist'}
    manager._save_tasks()
    restored = tm.TaskManager(state_path=path)
    record = restored.list_tasks()[0]
    assert record['task_id'] == task.task_id
    assert record['status'] == 'failed'
    assert '服務重啟中斷' in record['error']
    assert record['progress'] == 42
    assert record['downloaded_segments'] == 21
    assert record['name'] == 'MIMK-290'
    assert record['url'] == task.url
    assert not restored._queue
    assert 'must-not-persist' not in path.read_text()
    resumed = restored.get_task(task.task_id)
    assert resumed.dest == task.dest
    assert (resumed.resolution, resumed.max_workers, resumed.download_cover) == ('720', 3, False)
    assert resumed.enable_subtitles and resumed.subtitle_mode == 'zh'
    assert restored.restart_task(task.task_id)
    assert restored.list_tasks()[0]['status'] == 'pending'
    assert restored.restart_task(task.task_id) is False


def test_shutdown_preserves_unfinished_snapshot(manager, tmp_path):
    path = tmp_path / 'tasks.json'
    manager._state_path = path
    task = manager.add_task('https://missav.ai/mimk-290')
    manager.stop_dispatcher()
    manager._save_tasks()
    restored = tm.TaskManager(state_path=path)
    assert restored.get_task(task.task_id).status == 'failed'
    assert '服務重啟中斷' in restored.get_task(task.task_id).error


@pytest.mark.parametrize('status', ['completed', 'skipped', 'cancelled', 'failed'])
def test_restart_preserves_terminal_status(manager, tmp_path, status):
    path = tmp_path / 'tasks.json'
    manager._state_path = path
    task = manager.add_task('https://missav.ai/mimk-290')
    manager._finish_task(task, status)
    restored = tm.TaskManager(state_path=path).get_task(task.task_id)
    assert restored.status == status


def test_corrupt_task_file_is_not_silently_overwritten(tmp_path):
    path = tmp_path / 'tasks.json'
    path.write_text('broken')
    with pytest.raises(ValueError):
        tm.TaskManager(state_path=path)
    assert path.read_text() == 'broken'


def test_daemon_uses_persistent_config_directory(manager, monkeypatch, tmp_path):
    from uav_downloader.daemon.server import DaemonServer

    monkeypatch.setenv('CONFIG_DIR', str(tmp_path / 'config'))
    first = DaemonServer()
    task = first.task_manager.add_task('https://missav.ai/mimk-290')
    first.stop()
    second = DaemonServer()
    assert second.task_manager.list_tasks()[0]['task_id'] == task.task_id
    assert second.task_manager.get_task(task.task_id).status == 'failed'
    second.stop()


@pytest.mark.parametrize('cover,mode', [(False, 'ja'), (True, 'all'), (False, 'none')])
def test_worker_applies_cover_and_subtitle_defaults(manager, monkeypatch, cover, mode):
    monkeypatch.setattr(tm.config, 'get_download_cover', lambda: cover)
    monkeypatch.setattr(tm.config, 'get_subtitle_pref', lambda: mode)
    task = manager.add_task('https://missav.ai/mimk-290')
    def download(job):
        assert job.download_cover is cover
        (Path(job.dest_folder()) / 'MIMK-290.mp4').write_bytes(b'video')
        return True
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    subtitles = MagicMock()
    monkeypatch.setattr(manager, '_run_subtitles', subtitles)
    manager._run_task(task)
    assert task.status == 'completed'
    assert subtitles.call_count == int(mode != 'none')
    assert task.subtitle_mode == mode


def test_successful_cancellation_is_retained_for_restart(manager, tmp_path):
    path = tmp_path / 'tasks.json'
    manager._state_path = path
    task = manager.add_task('https://missav.ai/mimk-290')
    assert manager.cancel_task(task.task_id)
    assert manager.list_tasks()[0]['status'] == 'cancelled'
    assert tm.TaskManager(state_path=path).list_tasks()[0]['status'] == 'cancelled'


def test_cleanup_failure_survives_restart_without_claiming_files_removed(manager, tmp_path):
    path = tmp_path / 'tasks.json'
    manager._state_path = path
    task = manager.add_task('https://missav.ai/mimk-290')
    task.files = SimpleNamespace(cleanup=MagicMock(side_effect=PermissionError('permission denied')))
    assert manager.cancel_task(task.task_id)
    assert 'permission denied' in manager.list_tasks()[0]['error']
    restored = tm.TaskManager(state_path=path)
    assert 'permission denied' in restored.list_tasks()[0]['error']
    assert restored.cancel_task(task.task_id)
    assert restored.list_tasks()[0]['status'] == 'failed'
    assert '無法確認清除範圍' in restored.list_tasks()[0]['error']


def test_cancelled_task_with_storage_close_failure_stays_visible(manager, monkeypatch):
    from contextlib import ExitStack
    stack = ExitStack()
    def fail_close():
        raise PermissionError('lock permission denied')
    stack.callback(fail_close)
    monkeypatch.setattr(tm, 'ExitStack', lambda: stack)
    task = manager.add_task('https://missav.ai/mimk-290')
    def download(job):
        (Path(job.dest_folder()) / 'partial.part').write_bytes(b'partial')
        manager.cancel_task(task.task_id)
        return False
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    manager._run_task(task)
    assert task.status == 'failed'
    assert 'lock permission denied' in manager.list_tasks()[0]['error']


def test_pause_resume_keeps_segments_and_settings(manager, monkeypatch, tmp_path):
    task = manager.add_task('https://missav.ai/mimk-290', resolution='720', max_workers=3, download_cover=False)
    workdirs = []
    def download(job):
        work = Path(job.dest_folder())
        workdirs.append(work)
        if len(workdirs) == 1:
            job._stage_callback('合併影片片段', 3, 8)
            assert task.to_dict()['phase'] == '合併影片片段'
            assert (task.phase_done, task.phase_total) == (3, 8)
            (work / 'segment.ts').write_bytes(b'keep')
            assert manager.pause_task(task.task_id)
            assert task.status == 'pausing'
            assert not manager.restart_task(task.task_id), 'Wait for the worker before resuming'
            (work / 'last-write.part').write_bytes(b'late write')
            return False
        assert (work / 'segment.ts').read_bytes() == b'keep'
        (work / 'MIMK-290.mp4').write_bytes(b'video')
        return True
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    manager._state_path = tmp_path / 'tasks.json'
    manager._run_task(task)
    assert task.status == 'paused' and task.task_id not in manager._active
    restored = tm.TaskManager(state_path=manager._state_path)
    resumed = restored.get_task(task.task_id)
    assert resumed.status == 'paused'
    assert (resumed.resolution, resumed.max_workers, resumed.download_cover) == ('720', 3, False)
    assert restored.restart_task(task.task_id)
    assert not restored.restart_task(task.task_id), 'Double-click must not enqueue twice'
    restored._run_task(resumed)
    assert resumed.status == 'completed', resumed.error
    assert workdirs[0] == workdirs[1]
    assert (Path(task.dest) / 'MIMK-290.mp4').read_bytes() == b'video'


def test_failed_retry_reuses_task_and_private_workspace(manager, monkeypatch):
    task = manager.add_task('https://missav.ai/mimk-290')
    def fail(job):
        (Path(job.dest_folder()) / 'segment.ts').write_bytes(b'resume')
        raise ConnectionError('network lost')
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, fail))
    manager._queue.remove(task.task_id)
    manager._run_task(task)
    workdir = task.files.workdir
    assert manager.restart_task(task.task_id)
    assert task.status == 'pending' and task.error is None
    assert task.files.workdir == workdir
    assert Path(workdir, 'segment.ts').exists()
    assert manager._queue == [task.task_id]
    assert not manager.restart_task(task.task_id)


@pytest.mark.parametrize('status,action,accepted,expected_status', [
    ('pending', 'pause_task', True, 'paused'),
    ('downloading', 'pause_task', True, 'pausing'),
    ('paused', 'pause_task', False, 'paused'),
    ('failed', 'pause_task', False, 'failed'),
    ('cancelled', 'pause_task', False, 'cancelled'),
    ('completed', 'pause_task', False, 'completed'),
    ('pending', 'restart_task', False, 'pending'),
    ('downloading', 'restart_task', False, 'downloading'),
    ('paused', 'restart_task', True, 'pending'),
    ('failed', 'restart_task', True, 'pending'),
    ('cancelled', 'restart_task', True, 'pending'),
    ('completed', 'restart_task', False, 'completed'),
    ('pending', 'cancel_task', True, 'cancelled'),
    ('downloading', 'cancel_task', True, 'cancelling'),
    ('paused', 'cancel_task', True, 'cancelled'),
    ('failed', 'cancel_task', True, 'cancelled'),
    ('cancelled', 'cancel_task', False, 'cancelled'),
    ('completed', 'cancel_task', False, 'completed'),
])
def test_task_operation_state_matrix(manager, status, action, accepted, expected_status):
    task = manager.add_task('https://missav.ai/mimk-290')
    task.status = status
    if status == 'downloading':
        manager._active[task.task_id] = task
    assert getattr(manager, action)(task.task_id) is accepted
    assert manager.list_tasks()[0]['status'] == expected_status
    if action == 'restart_task' and accepted:
        assert manager.restart_task(task.task_id) is False
        assert manager.list_tasks()[0]['status'] == 'pending'


def test_pending_pause_cancel_restart_and_remove(manager, tmp_path):
    task = manager.add_task('https://missav.ai/mimk-290')
    assert manager.pause_task(task.task_id)
    assert task.status == 'paused' and not manager._queue
    assert manager.cancel_task(task.task_id)
    assert manager.restart_task(task.task_id)
    assert manager._queue == [task.task_id]
    assert not manager.remove_task(task.task_id)
    task.status = 'completed'
    output = tmp_path / 'completed.mp4'
    output.write_bytes(b'keep')
    assert manager.remove_task(task.task_id)
    assert manager.get_task(task.task_id) is None
    assert output.read_bytes() == b'keep'


def test_retry_rejects_another_live_task_with_same_code(manager):
    old = manager.add_task('https://missav.ai/mimk-290')
    old.status = 'failed'
    manager.add_task('https://jable.tv/videos/mimk-290/')
    with pytest.raises(ValueError, match='相同番號'):
        manager.restart_task(old.task_id)
    assert old.status == 'failed'


def test_restored_workspace_cannot_escape_destination(manager, tmp_path):
    path = tmp_path / 'tasks.json'
    manager._state_path = path
    task = manager.add_task('https://missav.ai/mimk-290')
    task.files = tm.DownloadFiles(task.dest, task.task_id)
    manager._save_tasks()
    import json
    records = json.loads(path.read_text())
    records[0]['files']['workdir'] = str(tmp_path / 'unowned')
    path.write_text(json.dumps(records))
    with pytest.raises(ValueError, match='不屬於此任務'):
        tm.TaskManager(state_path=path)


def test_retry_recovers_legacy_owned_segments(manager, monkeypatch):
    task = manager.add_task('https://missav.ai/mimk-290')
    files = tm.DownloadFiles(task.dest, task.task_id)
    Path(files.workdir, 'segment.ts').write_bytes(b'old-segment')
    task.cleanup_unverified = True
    task.status = 'failed'
    assert manager.restart_task(task.task_id)
    def download(job):
        assert job.dest_folder() == files.workdir
        assert Path(job.dest_folder(), 'segment.ts').read_bytes() == b'old-segment'
        manager.pause_task(task.task_id)
        return False
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    manager._run_task(task)
    assert task.status == 'paused'
    assert not task.cleanup_unverified


def test_destination_probe_preserves_existing_files(manager, tmp_path):
    folder = tmp_path / 'videos'
    folder.mkdir()
    (folder / 'keep.mp4').write_bytes(b'original')
    assert manager.check_destination()['success']
    assert [p.name for p in folder.iterdir()] == ['keep.mp4']


def test_legacy_smb_task_restores_current_matching_account(manager, monkeypatch, tmp_path):
    import json
    settings = dict(enabled=True, mode='smb', host='nas', share='share', directory='videos', username='user')
    task = manager.add_task('https://missav.ai/mimk-290')
    task.nas_settings = dict(settings, password='never-save')
    task.status = 'failed'
    path = tmp_path / 'legacy.json'
    record = task.to_dict()
    record.pop('speed_formatted')
    record.pop('final_dest')
    path.write_text(json.dumps([record]))
    monkeypatch.setattr(tm.nas, 'load_settings', lambda secret=False: dict(settings, **({'password':'from-encrypted-config'} if secret else {})))
    restored = tm.TaskManager(state_path=path)
    resumed = restored.get_task(task.task_id)
    assert resumed.dest.startswith(tm.nas.SMB_PATH_ROOT)
    assert resumed.nas_settings['_smb_path'] == resumed.dest
    assert restored.restart_task(task.task_id)
    assert resumed.nas_settings['password'] == 'from-encrypted-config'
    assert 'from-encrypted-config' not in path.read_text()
    assert 'never-save' not in path.read_text()


def test_restored_cancel_removes_only_owned_workspace(manager, tmp_path):
    path = tmp_path / 'tasks.json'
    manager._state_path = path
    task = manager.add_task('https://missav.ai/mimk-290')
    task.files = tm.DownloadFiles(task.dest, task.task_id)
    Path(task.files.workdir, 'segment.ts').write_bytes(b'owned')
    unrelated = Path(task.dest, 'keep.mp4')
    unrelated.write_bytes(b'original')
    task.status = 'paused'
    manager._save_tasks()
    restored = tm.TaskManager(state_path=path)
    assert restored.cancel_task(task.task_id)
    assert not Path(task.files.workdir).exists()
    assert unrelated.read_bytes() == b'original'


def test_clear_completed_keeps_files_history_and_other_states(manager, tmp_path):
    manager._state_path = tmp_path / 'tasks.json'
    folder = tmp_path / 'videos'
    folder.mkdir()
    statuses = ['completed', 'completed', 'failed', 'downloading', 'paused', 'pending', 'cancelled', 'skipped']
    tasks = []
    for i, status in enumerate(statuses):
        task = manager.add_task(f'https://missav.ai/mimk-{300+i}')
        task.status = status
        tasks.append(task)
    manager._active[tasks[1].task_id] = tasks[1]
    video = folder / 'MIMK-300.mp4'
    cover = folder / 'MIMK-300.jpg'
    subtitles = folder / 'MIMK-300.srt'
    for path in (video, cover, subtitles):
        path.write_bytes(b'keep unchanged')
    tm.get_history_manager().record_download(tasks[0].url, title='MIMK-300', dest_dir=str(folder), file_path=str(video))
    tasks[0].files = SimpleNamespace(cleanup=MagicMock(side_effect=AssertionError('Never clean files')))
    assert manager.clear_completed() == 1
    assert manager.get_task(tasks[0].task_id) is None
    assert all(manager.get_task(task.task_id) is task for task in tasks[1:])
    assert tm.get_history_manager().find_downloaded(tasks[0].url) is not None
    for path in (video, cover, subtitles):
        assert path.read_bytes() == b'keep unchanged'
    restored = tm.TaskManager(state_path=manager._state_path)
    assert restored.get_task(tasks[0].task_id) is None
    assert len(restored.list_tasks()) == len(tasks) - 1
    assert manager.clear_completed() == 0


def test_retry_keeps_original_directory_when_same_nas_account_changes_folder(manager, monkeypatch):
    settings = dict(enabled=True, mode='smb', host='nas', share='share', directory='original', username='user')
    task = manager.add_task('https://missav.ai/mimk-290')
    task.nas_settings = settings.copy()
    task.status = 'failed'
    monkeypatch.setattr(tm.nas, 'load_settings', lambda **kw: dict(settings, directory='new-folder', password='saved-secret'))
    assert manager.restart_task(task.task_id)
    assert task.nas_settings['directory'] == 'original'
    assert task.nas_settings['password'] == 'saved-secret'


@pytest.mark.parametrize('field,value', [('host', 'other-nas'), ('share', 'other-share'), ('username', 'other-user')])
def test_retry_never_borrows_credentials_for_changed_nas_account(manager, monkeypatch, field, value):
    settings = dict(enabled=True, mode='smb', host='nas', share='share', directory='original', username='user')
    task = manager.add_task('https://missav.ai/mimk-290')
    task.nas_settings = settings.copy()
    task.status = 'failed'
    monkeypatch.setattr(tm.nas, 'load_settings', lambda **kw: dict(settings, **{field:value, 'password':'different-account-secret'}))
    with pytest.raises(ValueError, match='NAS 設定已變更'):
        manager.restart_task(task.task_id)
    assert 'password' not in task.nas_settings


def test_task_destination_probe_keeps_original_nas_folder_after_settings_change(manager, monkeypatch):
    from contextlib import nullcontext
    settings = dict(enabled=True, mode='smb', host='nas', share='share', directory='original', username='user')
    task = manager.add_task('https://missav.ai/mimk-290')
    task.nas_settings = settings.copy()
    monkeypatch.setattr(tm.nas, 'load_settings', lambda **kw: dict(settings, directory='new-folder', password='saved-secret'))
    connections = []
    def connect(profile, destination):
        connections.append((profile.copy(), destination))
        return nullcontext()
    monkeypatch.setattr(tm.nas, 'open_smb_storage', connect)
    monkeypatch.setattr(tm, 'probe_destination', lambda destination: None)
    assert manager.check_destination(task.task_id)['success']
    assert connections[0][0]['directory'] == 'original'
    assert connections[0][0]['password'] == 'saved-secret'
    assert connections[0][1] == task.dest


def test_resume_unfinished_requires_no_selection_and_preserves_segments(manager):
    states = ['failed', 'paused', 'pending', 'downloading', 'completed', 'cancelled']
    tasks = [manager.add_task(f'https://missav.ai/mimk-{600+i}') for i in range(len(states))]
    manager._queue.clear()
    for task, state in zip(tasks, states):
        task.status = state
    tasks[0].files = tm.DownloadFiles(tasks[0].dest, tasks[0].task_id)
    fragment = Path(tasks[0].files.workdir, 'keep.ts')
    fragment.write_bytes(b'partial video')
    manager._queue.append(tasks[2].task_id)
    result = manager.resume_unfinished()
    assert result == {'resumed': 2, 'errors': [], 'waiting': 3, 'running': 0}
    assert manager._queue == [tasks[2].task_id, tasks[0].task_id, tasks[1].task_id]
    assert [task.status for task in tasks] == ['pending', 'pending', *states[2:]]
    assert fragment.read_bytes() == b'partial video'
    assert manager.resume_unfinished() == {'resumed': 0, 'errors': [], 'waiting': 3, 'running': 0}


def test_resume_unfinished_reports_one_failure_and_continues_others(manager, monkeypatch):
    bad = manager.add_task('https://missav.ai/mimk-600')
    good = manager.add_task('https://missav.ai/mimk-601')
    manager._queue.clear()
    bad.status = good.status = 'failed'
    restart = manager.restart_task
    def attempt(task_id):
        if task_id == bad.task_id:
            raise ValueError('NAS 帳號已變更')
        return restart(task_id)
    monkeypatch.setattr(manager, 'restart_task', attempt)
    result = manager.resume_unfinished()
    assert result['resumed'] == 1
    assert result['errors'] == [{'task_id': bad.task_id, 'error': 'NAS 帳號已變更'}]
    assert bad.status == 'failed' and good.status == 'pending'


def test_resume_unfinished_repairs_waiting_queue_and_wakes_dispatcher(manager, monkeypatch):
    queued = manager.add_task('https://missav.ai/mimk-700')
    orphan = manager.add_task('https://missav.ai/mimk-701')
    running = manager.add_task('https://missav.ai/mimk-702')
    running.status = 'downloading'
    manager._active[running.task_id] = running
    manager._queue = [queued.task_id]
    start = MagicMock()
    monkeypatch.setattr(manager, 'start_dispatcher', start)
    result = manager.resume_unfinished()
    assert manager._queue == [queued.task_id, orphan.task_id]
    assert result['waiting'] == 2 and result['running'] == 1
    start.assert_called_once()
    manager.resume_unfinished()
    assert manager._queue == [queued.task_id, orphan.task_id]


def test_remove_duplicate_codes_preserves_live_tasks_files_history_and_distinct_codes(manager, tmp_path):
    manager._state_path = tmp_path / 'tasks.json'
    live = manager.add_task('https://missav.ai/mimk-054')
    duplicate = manager.add_task('https://jable.tv/videos/MIMK-054-chinese-subtitle/')
    done = manager.add_task('https://missav.ai/mimk-120')
    done.status = 'completed'
    done_duplicate = manager.add_task('https://jable.tv/videos/mimk-120/')
    done_duplicate.status = 'completed'
    partial = manager.add_task('https://missav.ai/mimk-121')
    partial.status = 'failed'
    partial.files = tm.DownloadFiles(partial.dest, partial.task_id)
    fragment = Path(partial.files.workdir, 'keep.ts')
    fragment.write_bytes(b'resumable')
    partial_duplicate = manager.add_task('https://jable.tv/videos/mimk-121/')
    partial_duplicate.status = 'skipped'
    distinct = manager.add_task('https://missav.ai/mimk-0540')
    manager._queue = [live.task_id, distinct.task_id]
    manager._active[live.task_id] = live
    videos = [Path(done.dest, 'MIMK-120.mp4'), Path(done.dest, 'MIMK-120-copy.mp4')]
    for video in videos:
        video.write_bytes(b'completed video')
    tm.get_history_manager().record_download(done.url, title='MIMK-120')
    before_history = tm.get_history_manager().history_file.read_bytes()
    assert manager.remove_duplicates() == {'removed': 3, 'protected': 0}
    assert set(manager._tasks) == {live.task_id, done.task_id, partial.task_id, distinct.task_id}
    assert manager._queue == [live.task_id, distinct.task_id]
    assert fragment.read_bytes() == b'resumable'
    assert all(video.read_bytes() == b'completed video' for video in videos)
    assert tm.get_history_manager().history_file.read_bytes() == before_history
    assert manager.remove_duplicates() == {'removed': 0, 'protected': 0}
    restored = tm.TaskManager(state_path=manager._state_path)
    assert set(restored._tasks) == set(manager._tasks)


def test_remove_duplicates_keeps_both_tasks_when_each_has_recovery_state(manager):
    first = manager.add_task('https://missav.ai/mimk-200')
    first.status = 'failed'
    first.cleanup_unverified = True
    second = manager.add_task('https://jable.tv/videos/mimk-200/')
    second.status = 'paused'
    manager._queue.clear()
    assert manager.remove_duplicates() == {'removed': 0, 'protected': 1}
    assert set(manager._tasks) == {first.task_id, second.task_id}


def test_remove_duplicates_does_not_merge_unidentified_titles(manager):
    a = manager.add_task('https://example.com/watch?id=1')
    b = manager.add_task('https://example.com/watch?id=2')
    a.name = b.name = 'same title without code'
    a.status = b.status = 'cancelled'
    manager._queue.clear()
    assert manager.remove_duplicates() == {'removed': 0, 'protected': 0}


@pytest.mark.parametrize('source', ['history', 'removed_completed', 'removed_queue'])
def test_remove_duplicates_removes_lone_skipped_record(manager, tmp_path, source):
    manager._state_path = tmp_path / 'tasks.json'
    if source == 'history':
        tm.get_history_manager().record_download('https://missav.ai/mimk-290', title='MIMK-290')
    else:
        original = manager.add_task('https://missav.ai/mimk-290')
        if source == 'removed_completed':
            original.status = 'completed'
    skipped = manager.add_task('https://jable.tv/videos/mimk-290/')
    assert skipped.status == 'skipped'
    if source != 'history':
        manager._queue.clear()
        if source == 'removed_completed':
            manager.clear_completed()
        else:
            original.status = 'cancelled'
            manager.remove_task(original.task_id)
    assert list(manager._tasks) == [skipped.task_id]
    history_before = tm.get_history_manager().history_file.read_bytes() if source == 'history' else None
    assert manager.remove_duplicates() == {'removed': 1, 'protected': 0}
    assert not manager._tasks
    if history_before is not None:
        assert tm.get_history_manager().history_file.read_bytes() == history_before
    assert not tm.TaskManager(state_path=manager._state_path)._tasks


def test_remove_duplicates_clears_all_skips_but_waits_for_live_worker(manager):
    tm.get_history_manager().record_download('https://missav.ai/mimk-290', title='MIMK-290')
    first = manager.add_task('https://missav.ai/mimk-290')
    second = manager.add_task('https://jable.tv/videos/mimk-290/')
    manager._active[first.task_id] = first
    assert manager.remove_duplicates() == {'removed': 1, 'protected': 0}
    assert manager.get_task(first.task_id) is first
    assert manager.get_task(second.task_id) is None
    manager._active.clear()
    assert manager.remove_duplicates() == {'removed': 1, 'protected': 0}
    assert not manager._tasks


def test_staging_finishes_video_and_subtitles_before_publishing(manager, monkeypatch, tmp_path):
    staging = tmp_path / 'staging'
    task = manager.add_task('https://missav.ai/mimk-290', staging_dir=str(staging), enable_subtitles=True, subtitle_mode='zh')
    final = Path(task.dest)
    def download(job):
        assert Path(job.dest_folder()).parent == staging
        assert not (final / 'MIMK-290.mp4').exists()
        (Path(job.dest_folder()) / 'MIMK-290.mp4').write_bytes(b'complete-video')
        return True
    def subtitles(current, job):
        assert not (final / 'MIMK-290.mp4').exists()
        (Path(job.dest_folder()) / 'MIMK-290.zh.srt').write_text('subtitle')
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    monkeypatch.setattr(manager, '_run_subtitles', subtitles)
    manager._run_task(task)
    assert task.status == 'completed', task.error
    assert (final / 'MIMK-290.mp4').read_bytes() == b'complete-video'
    assert (final / 'MIMK-290.zh.srt').read_text() == 'subtitle'
    assert not list(staging.iterdir())


def test_staging_keeps_sources_after_publish_failure_and_retries_owned_outputs(manager, monkeypatch, tmp_path):
    manager._state_path = tmp_path / 'tasks.json'
    staging = str(tmp_path / 'staging')
    task = manager.add_task('https://missav.ai/mimk-290', staging_dir=staging)
    def download(job):
        (Path(job.dest_folder()) / 'MIMK-290.mp4').write_bytes(b'complete-video')
        (Path(job.dest_folder()) / 'MIMK-290.zh.srt').write_bytes(b'subtitle')
        return True
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    fs = tm.storage_fs(task.dest)
    publish = fs.publish
    def fail_subtitle(source, target):
        if target.endswith('.srt'):
            raise OSError('destination disconnected')
        return publish(source, target)
    monkeypatch.setattr(fs, 'publish', fail_subtitle)
    manager._run_task(task)
    assert task.status == 'failed'
    assert (Path(task.files.workdir) / 'MIMK-290.mp4').read_bytes() == b'complete-video'
    assert not list(Path(task.dest).glob('*.part'))
    monkeypatch.setattr(fs, 'publish', publish)
    restored = tm.TaskManager(state_path=manager._state_path)
    task = restored.get_task(task.task_id)
    assert restored.restart_task(task.task_id)
    restored._run_task(task)
    assert task.status == 'completed', task.error
    assert (Path(task.dest) / 'MIMK-290.zh.srt').read_bytes() == b'subtitle'
    assert not list(Path(staging).iterdir())


def test_new_subtitle_request_failure_keeps_download_completed(manager, monkeypatch, tmp_path):
    from uav_downloader.subtitles.profiles import ProfileSnapshot

    request = {
        'video_id': 'downloaded-video',
        'selected_stages': ['vad'],
        'target_languages': [],
        'profile_snapshots': {'vad': ProfileSnapshot(
            '{"capabilities":["vad"],"profile_id":"vad"}', None, 'snapshot')},
        'video_path': '/tmp/untrusted-video.mp4',
        'allow_reasoning': True,
        'api_key': 'must-not-persist',
    }
    task = manager.add_task('https://missav.ai/mimk-290', enable_subtitles=True,
                            subtitle_mode='ja', subtitle_request=request)
    calls = []
    def download(job):
        calls.append('download')
        Path(job.dest_folder()).mkdir(parents=True, exist_ok=True)
        (Path(job.dest_folder()) / 'MIMK-290.mp4').write_bytes(b'completed-video')
        return True
    class FailingSubtitleQueue:
        arguments = None
        def submit(self, *args, **kwargs):
            self.arguments = (args, kwargs)
            raise OSError('subtitle queue unavailable')

    queue = FailingSubtitleQueue()
    manager._subtitle_task_manager = queue
    monkeypatch.setattr(manager, '_run_subtitles', lambda *_: pytest.fail('new request must use the independent queue'))
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    manager._run_task(task)

    assert task.status == 'completed'
    assert task.error == '影片完成，字幕未能加入佇列'
    assert (Path(task.dest) / 'MIMK-290.mp4').read_bytes() == b'completed-video'
    assert calls == ['download']
    assert queue.arguments[1]['video_path'] == str(Path(task.dest) / 'MIMK-290.mp4')
    assert queue.arguments[1]['allow_reasoning'] is True
    assert queue.arguments[0][0].startswith('source-v1:')
    assert queue.arguments[0][0] != 'downloaded-video'
    with manager._subtitle_file_manager.resolve(queue.arguments[0][0]) as registered:
        assert registered.video_path == queue.arguments[1]['video_path']
    assert 'api_key' not in task.subtitle_request
    assert 'video_path' not in task.subtitle_request


@pytest.mark.parametrize('policy,expected', [('merge', 'skipped'), ('overwrite', 'completed'), ('new', 'completed')])
def test_batch_policy_controls_existing_video_and_history(manager, monkeypatch, tmp_path, policy, expected):
    from uav_downloader.daemon import download_batch
    source = 'https://missav.ai/mimk-290'
    tm.get_history_manager().record_download(source, title='MIMK-290')
    folder = tmp_path / 'videos' / 'query'
    folder.mkdir(parents=True)
    (folder / 'MIMK-290.mp4').write_bytes(b'old')
    (folder / 'keep.mp4').write_bytes(b'keep')
    options = dict(name='query', policy=policy, urls=[source], request_id='batch-test')
    preview = download_batch.destination(manager, options)
    result = download_batch.submit(manager, dict(options, expected_destination=preview['destination']))
    task = manager.get_task(result['tasks'][0]['task_id'])
    def download(job):
        Path(job.dest_folder(), 'MIMK-290.mp4').write_bytes(b'new')
        return True
    monkeypatch.setattr(tm.M3U8Sites, 'CreateSite', lambda url, savepath, **kw: job_at(savepath, download))
    manager._run_task(task)
    assert task.status == expected, task.error
    assert (folder / 'keep.mp4').read_bytes() == b'keep'
    assert (folder / 'MIMK-290.mp4').read_bytes() == (b'new' if policy == 'overwrite' else b'old')
    if policy == 'new':
        assert Path(task.dest, 'MIMK-290.mp4').read_bytes() == b'new'


def test_batch_rolls_back_queue_when_persistence_fails(manager, monkeypatch):
    from uav_downloader.daemon import download_batch
    options = dict(name='query', urls=['https://example.com/a','https://example.com/b'], request_id='failed')
    preview = download_batch.destination(manager, options)
    monkeypatch.setattr(manager, '_save_tasks', MagicMock(side_effect=OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        download_batch.submit(manager, dict(options, expected_destination=preview['destination']))
    assert not manager._tasks and not manager._queue


@pytest.mark.parametrize('staging', [False, True])
def test_restore_shared_smb_roots_keeps_task_workspaces(manager, tmp_path, staging):
    import json
    import os
    from uav_downloader.core.storage import SMB_PATH_ROOT
    root = os.path.join(SMB_PATH_ROOT, 'shared-batch')
    stage = str(tmp_path / 'staging') if staging else ''
    records = []
    for index in range(2):
        task_id = f'task_{index}'
        task = tm.DownloadTask(task_id, f'https://example.com/{index}', root, status='failed',
                               staging_dir=stage, nas_settings={'enabled': True, 'mode': 'smb', '_smb_path': root,
                                                             'host': 'nas.example', 'share': 'videos', 'directory': ''})
        record = {key: getattr(task, key) for key in ('task_id', 'url', 'dest', 'status', 'staging_dir', 'nas_settings')}
        record['files'] = {'workdir': os.path.join(stage or root, f'.uav-{task_id}-partial'),
                           'published': [os.path.join(root, f'{index}.mp4')]}
        records.append(record)
    state = tmp_path / 'shared-tasks.json'
    state.write_text(json.dumps(records))
    restored = tm.TaskManager(state_path=state)
    first, second = list(restored._tasks.values())
    assert first.dest != second.dest
    for task in (first, second):
        assert task.nas_settings['_smb_path'] == task.dest
        assert task.files.destination == task.dest
        assert os.path.dirname(task.files.workdir) == (stage or task.dest)
        assert os.path.dirname(task.files.published[0]) == task.dest
        assert task.files.workdir.endswith(f'.uav-{task.task_id}-partial')
    restored._save_tasks()
    reloaded = tm.TaskManager(state_path=state)
    assert [task.dest for task in reloaded._tasks.values()] == [first.dest, second.dest]


def test_batch_edit_assigns_independent_smb_roots(manager, monkeypatch):
    first = manager.add_task('https://example.com/first')
    second = manager.add_task('https://example.com/second')
    monkeypatch.setattr(tm.nas, 'load_settings', lambda **kw: {'enabled': True, 'mode': 'smb'})
    monkeypatch.setattr(tm.nas, 'test_connection', lambda *a, **kw: None)
    result = manager.update_tasks([first.task_id, second.task_id], storage='settings')
    assert len(result['updated']) == 2
    assert first.dest != second.dest
    assert first.nas_settings['_smb_path'] == first.dest
    assert second.nas_settings['_smb_path'] == second.dest


def batch_payload(manager, request_id, urls, **options):
    from uav_downloader.daemon import download_batch
    body = dict(name='collection', policy='merge', request_id=request_id, urls=urls, **options)
    return dict(body, expected_destination=download_batch.destination(manager, body)['destination'])


@pytest.mark.parametrize('status', ['pending', 'downloading', 'paused', 'cancelling'])
@pytest.mark.parametrize('storage', ['query', 'settings'])
def test_batch_append_preserves_original_and_skips_duplicates(manager, tmp_path, status, storage):
    manager._state_path = tmp_path / 'tasks.json'
    from uav_downloader.daemon import download_batch
    first_body = batch_payload(manager, 'first', ['https://missav.ai/mimk-290'], storage=storage)
    first = download_batch.submit(manager, first_body)['tasks'][0]
    original = manager.get_task(first['task_id'])
    original.status = status
    snapshot = original.to_dict()
    body = batch_payload(manager, 'second', ['https://jable.tv/videos/mimk-290/', 'https://missav.ai/mimk-291'], storage=storage)
    preview = download_batch.destination(manager, body)
    assert preview['busy'] and not preview['overwrite_busy'] and preview['can_submit']
    result = download_batch.submit(manager, body)
    assert [task['status'] for task in result['tasks']] == ['skipped', 'pending']
    assert manager.get_task(first['task_id']).to_dict() == snapshot
    assert {task['dest'] for task in result['tasks']} == {original.dest}
    assert download_batch.submit(manager, body) == result
    assert len(manager._tasks) == 3
    duplicates = download_batch.submit(manager, dict(body, request_id='all-duplicates'))
    assert all(task['status'] == 'skipped' for task in duplicates['tasks'])
    assert len(manager._queue) == 2
    assert not download_batch.destination(manager, dict(body, policy='overwrite'))['can_submit']
    restored = tm.TaskManager(state_path=manager._state_path)
    assert set(restored._tasks) == set(manager._tasks)
    assert restored.get_task(result['tasks'][1]['task_id']).dest == original.dest


def test_batch_append_rechecks_overwrite_after_preview(manager):
    from uav_downloader.daemon import download_batch
    append = batch_payload(manager, 'append', ['https://missav.ai/mimk-291'])
    overwrite = dict(append, request_id='overwrite', policy='overwrite', urls=['https://missav.ai/mimk-290'])
    download_batch.submit(manager, overwrite)
    before_tasks, before_queue = dict(manager._tasks), list(manager._queue)
    preview = download_batch.destination(manager, append)
    assert preview['busy'] and preview['overwrite_busy'] and not preview['can_submit']
    with pytest.raises(ValueError, match='覆寫任務'):
        download_batch.submit(manager, append)
    assert manager._tasks == before_tasks and manager._queue == before_queue


def test_batch_append_concurrent_retry_and_duplicate_requests(manager):
    from concurrent.futures import ThreadPoolExecutor
    from uav_downloader.daemon import download_batch
    first = batch_payload(manager, 'first', ['https://missav.ai/mimk-290'])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: download_batch.submit(manager, first), range(16)))
    assert all(result == results[0] for result in results)
    assert len(manager._tasks) == len(manager._queue) == 1
    bodies = [batch_payload(manager, str(i), ['https://missav.ai/mimk-291']) for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda body: download_batch.submit(manager, body), bodies))
    assert sum(result['tasks'][0]['status'] == 'pending' for result in results) == 1
    assert len(manager._queue) == 2


def test_batch_append_save_failure_preserves_original(manager, monkeypatch, tmp_path):
    manager._state_path = tmp_path / 'tasks.json'
    from uav_downloader.daemon import download_batch
    body = batch_payload(manager, 'first', ['https://missav.ai/mimk-290'])
    first = download_batch.submit(manager, body)
    before_tasks, before_queue = dict(manager._tasks), list(manager._queue)
    before_state = Path(manager._state_path).read_bytes()
    monkeypatch.setattr(manager, '_save_tasks', MagicMock(side_effect=OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        download_batch.submit(manager, batch_payload(manager, 'second', ['https://missav.ai/mimk-291']))
    assert manager._tasks == before_tasks and manager._queue == before_queue
    assert Path(manager._state_path).read_bytes() == before_state
    assert download_batch.submit(manager, body) == first


def test_batch_append_cancel_preserves_other_batch_files(manager, tmp_path):
    manager._state_path = tmp_path / 'tasks.json'
    from uav_downloader.daemon import download_batch
    from uav_downloader.daemon.download_files import DownloadFiles
    first = download_batch.submit(manager, batch_payload(manager, 'first', ['https://missav.ai/mimk-290']))
    second = download_batch.submit(manager, batch_payload(manager, 'second', ['https://missav.ai/mimk-291']))
    tasks = [manager.get_task(result['tasks'][0]['task_id']) for result in (first, second)]
    for index, task in enumerate(tasks):
        task.files = DownloadFiles(task.dest, task.task_id)
        Path(task.files.workdir, f'{index}.mp4').write_bytes(bytes([index]))
        task.files.publish(task.cancelled_event)
    manager._save_tasks()
    restored = tm.TaskManager(state_path=manager._state_path)
    assert restored.cancel_task(tasks[0].task_id)
    assert not Path(tasks[0].dest, '0.mp4').exists()
    assert Path(tasks[1].dest, '1.mp4').read_bytes() == bytes([1])
    assert Path(tasks[1].files.workdir).is_dir()


@pytest.mark.parametrize('status', ['pending', 'paused', 'failed', 'cancelled'])
def test_start_now_bypasses_limit_once_and_preserves_settings(manager, monkeypatch, status):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    manager.max_concurrent = 1
    occupied = manager.add_task('https://missav.ai/mimk-100')
    task = manager.add_task('https://missav.ai/mimk-101')
    task.status = status
    if status != 'pending':
        manager._queue.remove(task.task_id)
    manager._queue.remove(occupied.task_id)
    manager._active[occupied.task_id] = occupied
    started, release = threading.Event(), threading.Event()
    calls = []
    def worker(current):
        calls.append(current.task_id)
        started.set()
        release.wait(3)
        with manager._lock:
            manager._active.pop(current.task_id, None)
    monkeypatch.setattr(manager, '_run_task', worker)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: manager.start_now(task.task_id), range(16)))
        assert started.wait(1)
        assert results.count(True) == 1
        assert calls == [task.task_id]
        assert manager.max_concurrent == 1 and len(manager._active) == 2
        assert occupied.task_id in manager._active and task.task_id not in manager._queue
    finally:
        release.set()
        if task.thread:
            task.thread.join(2)


def test_start_now_does_not_release_extra_normal_slots(manager, monkeypatch):
    import threading
    manager.max_concurrent = 1
    tasks = [manager.add_task(f'https://missav.ai/mimk-{i}') for i in (100,101,102)]
    occupied, forced, waiting = tasks
    manager._queue.remove(occupied.task_id)
    manager._active[occupied.task_id] = occupied
    forced_started, waiting_started, release = threading.Event(), threading.Event(), threading.Event()
    def worker(task):
        (forced_started if task is forced else waiting_started).set()
        release.wait(3)
        with manager._lock:
            manager._active.pop(task.task_id, None)
    monkeypatch.setattr(manager, '_run_task', worker)
    dispatcher = threading.Thread(target=manager._dispatch_loop)
    try:
        assert manager.start_now(forced.task_id)
        assert forced_started.wait(1)
        dispatcher.start()
        assert not waiting_started.wait(.1)
        release.set()
        forced.thread.join(2)
        assert not waiting_started.wait(.1), 'The ordinary limit must still count the occupied task'
        with manager._lock:
            manager._active.pop(occupied.task_id)
        assert waiting_started.wait(2)
    finally:
        manager._running = False
        release.set()
        if dispatcher.ident:
            dispatcher.join(2)
        if waiting.thread:
            waiting.thread.join(2)


def test_start_now_rejects_duplicates_and_finished_tasks(manager, monkeypatch):
    running = manager.add_task('https://missav.ai/mimk-100')
    duplicate = manager.add_task('https://jable.tv/videos/mimk-100/')
    duplicate.status = 'pending'
    with pytest.raises(ValueError, match='相同'):
        manager.start_now(duplicate.task_id)
    for status in ('completed', 'skipped', 'downloading', 'pausing'):
        running.status = status
        assert not manager.start_now(running.task_id)
    assert not manager.start_now('missing')


@pytest.mark.parametrize('failure', ['construct', 'start'])
def test_start_now_thread_failure_preserves_queue(manager, monkeypatch, failure):
    task = manager.add_task('https://missav.ai/mimk-100')
    before = list(manager._queue)
    if failure == 'construct':
        monkeypatch.setattr(tm.threading, 'Thread', MagicMock(side_effect=RuntimeError('no threads')))
    else:
        monkeypatch.setattr(tm.threading.Thread, 'start', MagicMock(side_effect=RuntimeError('no threads')))
    with pytest.raises(RuntimeError, match='no threads'):
        manager.start_now(task.task_id)
    assert manager._queue == before and not manager._active


def test_start_now_preserves_subtitle_only_retry_history_exemption(manager, monkeypatch):
    task = manager.add_task('https://missav.ai/mimk-100', enable_subtitles=True, subtitle_mode='ja')
    task.name, task.status = 'MIMK-100', 'completed'
    tm.get_history_manager().record_download(task.url, title=task.name)
    assert manager.restart_task(task.task_id, subtitles_only=True)
    occupied = manager.add_task('https://missav.ai/mimk-101')
    manager._queue.remove(occupied.task_id)
    manager._active[occupied.task_id] = occupied
    manager.max_concurrent = 1
    thread = MagicMock()
    monkeypatch.setattr(tm.threading, 'Thread', MagicMock(return_value=thread))
    assert manager.start_now(task.task_id)
    thread.start.assert_called_once_with()
    assert task.subtitles_only and len(manager._active) == 2
