from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from uav_downloader.core.storage import LocalStorage, registered_storage
from uav_downloader.daemon import nas
from uav_downloader.daemon.server import DaemonRequestHandler
from uav_downloader.daemon.subtitle_files import SubtitleFileManager


@pytest.fixture
def library(monkeypatch, tmp_path):
    root = tmp_path / '下載'
    root.mkdir()
    handler = type('LibraryHandler', (DaemonRequestHandler,), {'auth_token': 'preview-secret'})
    files = SubtitleFileManager(state_path=tmp_path / 'sources.json')
    monkeypatch.setattr(files, '_configured_roots', lambda: [str(root)])
    monkeypatch.setattr(nas, 'load_settings', lambda **kwargs: {'enabled': False})
    monkeypatch.setattr('uav_downloader.core.history.get_history_manager',
                        lambda: SimpleNamespace(_entries={}))
    with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        server.subtitle_file_manager = files
        server.subtitle_task_manager = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def get(path, *, range_header=None, authenticated=True):
            headers = {'Authorization': 'Bearer preview-secret'} if authenticated else {}
            if range_header is not None:
                headers['Range'] = range_header
            req = Request(f'http://127.0.0.1:{server.server_port}' + path, headers=headers)
            try:
                response = urlopen(req, timeout=5)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, response.headers, response.read()

        yield files, root, get
        server.shutdown()
        thread.join()


def test_preview_serves_bounded_ranges_and_escaped_subtitle_text(library):
    files, root, get = library
    folder = root / '2026' / '一月'
    folder.mkdir(parents=True)
    video = folder / 'ABC-123 測試.mp4'
    video.write_bytes(b'0123456789')
    video.with_suffix('.ja.srt').write_text(
        '1\n00:00:01,250 --> 00:00:03,500\n<b>日文</b> & 文字\n', encoding='utf-8')
    pending = root / '.uav-download'
    pending.mkdir()
    (pending / 'pending.mp4').write_bytes(b'not yet published')

    status, _, raw = get('/api/subtitles/videos')
    assert status == 200
    item, = json.loads(raw)['videos']
    assert item['folder'] == '2026/一月'
    assert item['code'] == 'abc-123'
    assert item['subtitle_languages'] == ['ja']
    assert str(root) not in raw.decode()
    endpoint = '/api/subtitles/videos/' + item['video_id']
    assert get(endpoint + '/media', authenticated=False)[0] == 401
    status, headers, body = get(endpoint + '/media', range_header='bytes=2-5')
    assert (status, headers['Content-Range'], body) == (206, 'bytes 2-5/10', b'2345')
    assert get(endpoint + '/media', range_header='bytes=-2')[2] == b'89'
    assert get(endpoint + '/media', range_header='bytes=8-')[2] == b'89'
    assert get(endpoint + '/media')[2] == b'0123456789'
    for invalid in ('bytes=10-', 'bytes=-0', 'bytes=0-1,4-5', 'bytes='):
        status, headers, body = get(endpoint + '/media', range_header=invalid)
        assert (status, headers['Content-Range'], body) == (416, 'bytes */10', b'')
    status, headers, body = get(endpoint + '/tracks/ja')
    assert status == 200
    assert headers['Content-Type'] == 'text/vtt; charset=utf-8'
    assert body.decode() == 'WEBVTT\n\n00:00:01.250 --> 00:00:03.500\n&lt;b&gt;日文&lt;/b&gt; &amp; 文字\n\n'
    assert get(endpoint + '/tracks/../../other')[0] == 404
    assert get(endpoint + '/tracks/zh-TW')[0] == 404
    assert files._sources == {}
    video.write_bytes(b'new media contents')
    assert get(endpoint + '/media')[0] == 409


def test_preview_revalidates_root_and_refuses_linked_tracks(library, tmp_path, monkeypatch):
    files, root, get = library
    video = root / 'movie.mp4'
    video.write_bytes(b'media')
    outside = tmp_path / 'unapproved.srt'
    outside.write_text('1\n00:00:01,000 --> 00:00:02,000\nprivate')
    video.with_suffix('.ja.srt').symlink_to(outside)

    item, = json.loads(get('/api/subtitles/videos')[2])['videos']
    assert item['subtitle_languages'] == []
    endpoint = '/api/subtitles/videos/' + item['video_id']
    assert get(endpoint + '/tracks/ja')[0] == 404
    monkeypatch.setattr(files, '_configured_roots', lambda: [str(root / 'missing')])
    assert get(endpoint + '/media')[0] == 409


@pytest.mark.skipif(os.open not in os.supports_dir_fd, reason='需要 descriptor-relative open')
def test_preview_does_not_follow_a_directory_link_swapped_after_validation(
        library, tmp_path, monkeypatch):
    from uav_downloader.daemon import subtitle_files

    _, root, get = library
    folder = root / 'library'
    folder.mkdir()
    (folder / 'movie.mp4').write_bytes(b'local media')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'movie.mp4').write_bytes(b'private file')
    item, = json.loads(get('/api/subtitles/videos')[2])['videos']
    open_file = subtitle_files.open_preview_file

    def swap_folder(path):
        folder.rename(root / 'moved')
        folder.symlink_to(outside, target_is_directory=True)
        return open_file(path)

    monkeypatch.setattr(subtitle_files, 'open_preview_file', swap_folder)
    status, _, body = get('/api/subtitles/videos/' + item['video_id'] + '/media')
    assert status == 503
    assert b'private file' not in body


def test_preview_validates_the_opened_file_before_sending_its_bytes(library, monkeypatch):
    from uav_downloader.daemon import subtitle_files

    _, root, get = library
    video = root / 'movie.mp4'
    video.write_bytes(b'original media')
    item, = json.loads(get('/api/subtitles/videos')[2])['videos']
    endpoint = '/api/subtitles/videos/' + item['video_id']
    open_file = subtitle_files.open_preview_file
    replacement = root / 'replacement.mp4'
    replacement.write_bytes(b'replaced media')

    def replace_media(path):
        replacement.replace(path)
        return open_file(path)

    monkeypatch.setattr(subtitle_files, 'open_preview_file', replace_media)
    status, _, body = get(endpoint + '/media')
    assert status == 409
    assert b'replaced media' not in body


def test_smb_preview_reopens_a_fresh_storage_context(library, monkeypatch, tmp_path):
    _, _, get = library
    remote = tmp_path / 'NAS'
    folder = remote / 'test'
    folder.mkdir(parents=True)
    (folder / 'ABC-123.mp4').write_bytes(b'remote media')
    (folder / 'ABC-123.zh-TW.srt').write_text(
        '1\n00:00:00,000 --> 00:00:01,500\n繁中字幕\n', encoding='utf-8')
    settings = {'enabled': True, 'mode': 'smb', 'host': 'nas', 'share': 'share'}
    roots = []
    monkeypatch.setattr(nas, 'load_settings', lambda **kwargs: settings)

    @contextmanager
    def open_storage(_settings, root, *, create_directory):
        assert create_directory is False
        class MountedShare(LocalStorage):
            remote = True

            def open(self, path, *args, **kwargs):
                return super().open(self.local_path(path), *args, **kwargs)

            def stat(self, path, **kwargs):
                return super().stat(self.local_path(path), **kwargs)

            def isfile(self, path):
                return super().isfile(self.local_path(path))

            def islink(self, path):
                return super().islink(self.local_path(path))

            def scandir(self, path):
                return os.scandir(self.local_path(path))

            def local_path(self, path):
                return remote / Path(path).relative_to(root)

        roots.append(root)
        with registered_storage(root, MountedShare()):
            yield

    monkeypatch.setattr(nas, 'open_smb_storage', open_storage)
    item, = json.loads(get('/api/subtitles/videos')[2])['videos']
    assert (item['source'], item['folder'], item['subtitle_languages']) == ('smb', 'test', ['zh-TW'])
    endpoint = '/api/subtitles/videos/' + item['video_id']
    assert get(endpoint + '/media', range_header='bytes=7-')[2] == b'media'
    assert '繁中字幕' in get(endpoint + '/tracks/zh-TW')[2].decode()
    assert len(roots) == len(set(roots)) == 3


def test_read_only_smb_connection_preserves_missing_configured_directory(monkeypatch):
    from uav_downloader.core import smb_storage

    writes = []

    class MissingDirectory:
        def __init__(self, settings, destination):
            self.destination = destination

        def isdir(self, path):
            return path in writes

        def makedirs(self, path, *, exist_ok):
            writes.append(path)

        def close(self):
            pass

    monkeypatch.setattr(smb_storage, 'SmbStorage', MissingDirectory)
    settings = {'directory': 'unavailable'}
    destination = nas.smb_destination()
    with pytest.raises(nas.NasError, match='子目錄不存在'):
        with nas.open_smb_storage(settings, destination, create_directory=False):
            pytest.fail('唯讀連線不應打開不存在的目錄')
    assert writes == []
    with nas.open_smb_storage(settings, destination):
        assert writes == [destination]
