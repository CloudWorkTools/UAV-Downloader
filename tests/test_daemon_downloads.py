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
    assert not list(Path(task.dest).iterdir())


def test_cleanup_failure_remains_visible_and_retryable(manager, monkeypatch):
    task = manager.add_task('https://missav.ai/mimk-290')
    task.status = 'failed'
    task.files = SimpleNamespace(cleanup=MagicMock(side_effect=OSError('offline')))
    assert manager.cancel_task(task.task_id)
    assert task.status == 'failed'
    assert '清除失敗' in task.error
    task.files.cleanup.side_effect = None
    assert manager.cancel_task(task.task_id)
    assert task.status == 'cancelled'


def test_pending_cancel_never_starts_download(manager):
    task = manager.add_task('https://missav.ai/mimk-290')
    assert manager.cancel_task(task.task_id)
    assert task.status == 'cancelled'
    assert task.task_id not in manager._queue
    assert not Path(task.dest).exists()


@pytest.mark.parametrize('status', ['pending', 'preparing', 'downloading', 'subtitles', 'uploading', 'cancelling'])
def test_restart_keeps_unfinished_tasks(manager, tmp_path, status):
    path = tmp_path / 'tasks.json'
    manager._state_path = path
    task = manager.add_task('https://missav.ai/mimk-290')
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
    assert tm.TaskManager(state_path=path).get_task(task.task_id).status == status


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
