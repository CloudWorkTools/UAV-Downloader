"""Web preferences must survive restarts and control actual download behavior."""
import json
from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from uav_downloader.core import config
from uav_downloader.daemon.server import DaemonServer
from uav_downloader.daemon.task_manager import TaskManager
from uav_downloader.sites.base import M3U8Crawler


@pytest.fixture
def web_settings(monkeypatch, tmp_path):
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('DOWNLOAD_DIR', str(tmp_path / 'videos'))
    monkeypatch.setattr(TaskManager, 'start_dispatcher', lambda self: None)
    monkeypatch.setattr('uav_downloader.daemon.task_manager.nas.load_settings', lambda **kw: {'enabled': False})
    history = MagicMock()
    history.find_downloaded.return_value = None
    monkeypatch.setattr('uav_downloader.daemon.task_manager.get_history_manager', lambda: history)
    server = DaemonServer(port=0)
    server.start()

    def request(path, data=None):
        req = Request(f'http://127.0.0.1:{server.server.server_address[1]}{path}',
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    yield request, server, tmp_path
    server.stop()


@pytest.mark.parametrize('mode', ['none', 'ja', 'en', 'zh', 'all'])
def test_saved_preferences_apply_to_web_downloads_and_survive_restart(web_settings, mode):
    request, server, root = web_settings
    preferences = {'subtitle_pref': mode, 'recognition_quality': 'balanced', 'download_cover': False, 'download_concurrency': 7}
    assert request('/api/settings', preferences)[0] == 200
    status, settings = request('/api/settings')
    assert status == 200
    for key, value in preferences.items():
        assert settings[key] == value
    status, response = request('/api/download/start', {'url': 'https://missav.ai/test'})
    assert status == 200
    task = response['task']
    assert task['subtitle_mode'] == mode
    assert task['enable_subtitles'] is (mode != 'none')
    assert task['download_cover'] is False
    saved = json.loads((root / 'config' / 'ui_prefs.json').read_text())
    assert saved['recognition_quality'] == 'balanced'
    server.stop()
    restarted = DaemonServer()
    assert restarted.task_manager.max_concurrent == 7
    restored = restarted.task_manager.get_task(task['task_id'])
    assert restored.subtitle_mode == mode
    assert restored.download_cover is False


def test_explicit_task_options_override_defaults_and_null_uses_defaults(web_settings):
    request, _, _ = web_settings
    request('/api/settings', {'subtitle_pref': 'all', 'download_cover': False})
    _, result = request('/api/download/start', {'url': 'https://missav.ai/first',
                                              'enable_subtitles': False, 'download_cover': True})
    assert result['task']['subtitle_mode'] == 'none'
    assert result['task']['download_cover'] is True
    _, result = request('/api/download/start', {'url': 'https://missav.ai/second',
                                              'enable_subtitles': None, 'subtitle_mode': None,
                                              'download_cover': None})
    assert result['task']['subtitle_mode'] == 'all'
    assert result['task']['download_cover'] is False


@pytest.mark.parametrize('payload', [{'download_cover': 'false'}, {'subtitle_pref': 'invalid'},
                                     {'recognition_quality': 'invalid'}])
def test_invalid_settings_are_rejected(web_settings, payload):
    request, _, _ = web_settings
    assert request('/api/settings', payload)[0] == 400


@pytest.mark.parametrize('enabled', [True, False])
def test_cover_switch_controls_image_download(enabled):
    crawler = object.__new__(M3U8Crawler)
    crawler._cancel_job = False
    crawler.download_cover = enabled
    crawler._create_dest_folder = MagicMock()
    crawler.download_image = MagicMock()
    crawler.is_target_video_exist = lambda: True
    assert crawler.start_download()
    assert crawler.download_image.call_count == int(enabled)


def test_linux_subtitle_failure_is_explicit_without_downloading_windows_runtime():
    import os
    from uav_downloader.daemon.task_manager import DownloadTask
    if os.name == 'nt':
        pytest.skip('Linux-only runtime limitation')
    task = DownloadTask(task_id='test', url='https://example.com/video', dest='/unused')
    TaskManager()._run_subtitles(task, None)
    assert 'Linux／Docker' in task.error


def test_saved_download_directory_overrides_environment_default(web_settings):
    request, _, root = web_settings
    destination = str(root / 'chosen')
    assert request('/api/settings', {'download_dir': destination})[0] == 200
    assert request('/api/settings')[1]['download_dir'] == destination
    status, response = request('/api/download/start', {'url': 'https://missav.ai/test'})
    assert status == 200
    assert response['task']['dest'] == destination
