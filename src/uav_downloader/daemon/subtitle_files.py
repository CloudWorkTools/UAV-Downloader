"""Trusted video sources and atomic SRT publication for subtitle tasks."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from uav_downloader.core import config
from uav_downloader.core.storage import SMB_PATH_ROOT, storage_fs, storage_open
from uav_downloader.daemon import nas
from uav_downloader.subtitles import asr, engine

_LANGUAGES = ('ja', 'en', 'zh-TW')
_VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.ts'}
_NAS_IDENTITY_FIELDS = ('mode', 'host', 'share', 'directory', 'username', 'direct_path')


@dataclass(frozen=True)
class VideoSource:
    video_id: str
    video_path: str
    identity: str
    kind: str
    output_mode: str


class SubtitleFileManager:
    """Resolve server-owned video ids and publish one selected SRT at a time."""

    def __init__(self, state_path=None):
        self.state_path = Path(state_path) if state_path else asr._folder() / 'subtitle_video_sources.json'
        self._lock = threading.RLock()
        self._sources = {}
        self.list_errors = []
        self._listed = {}
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('sources'), dict):
                raise ValueError('字幕影片來源資料格式錯誤')
            self._sources = data['sources']

    def _save_locked(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=self.state_path.name + '.', suffix='.tmp', dir=self.state_path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as target:
                json.dump({'schema': 1, 'sources': self._sources}, target,
                          ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.state_path)
            temporary = ''
        finally:
            if temporary:
                os.unlink(temporary)

    @staticmethod
    def _media_identity(path):
        return engine._media_identity(path)

    @staticmethod
    def _nas_identity(settings):
        return [str(settings.get(key) or '') for key in _NAS_IDENTITY_FIELDS]

    @staticmethod
    def _video_id(descriptor):
        """Bind the opaque source id to both the media and its publish locator."""
        payload = json.dumps(descriptor, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')).encode('utf-8')
        return 'source-v1:' + hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _absolute_without_links(path):
        if not isinstance(path, str) or not path:
            raise ValueError('字幕影片路徑無效')
        absolute = os.path.abspath(path)
        current = os.path.splitdrive(absolute)[0] + os.path.sep
        for part in Path(absolute).parts[1:]:
            current = os.path.join(current, part)
            if os.path.islink(current):
                raise ValueError('字幕影片路徑不可經過符號連結')
        return absolute

    @classmethod
    def _validate_local(cls, path, allowed_roots):
        absolute = cls._absolute_without_links(path)
        if not os.path.isfile(absolute):
            raise ValueError('找不到可讀取的影片檔案')
        if not os.access(absolute, os.R_OK):
            raise ValueError('沒有讀取影片的權限')
        for root in allowed_roots:
            checked_root = cls._absolute_without_links(root)
            if not os.path.isdir(checked_root):
                continue
            try:
                if os.path.commonpath((checked_root, absolute)) == checked_root:
                    return absolute, checked_root
            except ValueError:
                continue
        raise ValueError('影片不在允許的下載或掛載目錄內')

    @staticmethod
    def _validate_smb_locator(filesystem, root, path):
        try:
            relative = os.path.relpath(path, root)
        except ValueError:
            raise ValueError('SMB 影片路徑超出允許目錄') from None
        if relative in ('.', '..') or relative.startswith('..' + os.path.sep) or os.path.isabs(relative):
            raise ValueError('SMB 影片路徑超出允許目錄')
        current = root
        for part in relative.split(os.path.sep):
            current = os.path.join(current, part)
            if filesystem.islink(current):
                raise ValueError('SMB 影片路徑不可經過符號連結')
        return relative

    def register(self, video_path, *, allowed_roots, nas_settings=None, output_mode='local'):
        """Register a local or mounted file after root, link and identity checks."""
        if not isinstance(allowed_roots, (list, tuple)) or not allowed_roots:
            raise ValueError('未設定允許的影片目錄')
        if output_mode not in ('local', 'upload'):
            raise ValueError('影片儲存方式無效')
        path, root = self._validate_local(video_path, allowed_roots)
        identity = self._media_identity(path)
        descriptor = {
            'kind': 'local', 'path': path, 'root': root,
            'identity': identity, 'output_mode': output_mode,
        }
        if nas_settings:
            descriptor['nas_identity'] = self._nas_identity(nas_settings)
        video_id = self._video_id(descriptor)
        with self._lock:
            self._sources[video_id] = descriptor
            self._save_locked()
        return VideoSource(video_id, path, identity, 'local', output_mode)

    def register_smb(self, video_path, *, storage_root, nas_settings, output_mode='smb'):
        """Register an active SMB virtual path without converting it to a local path."""
        if (output_mode != 'smb'
                or not isinstance(storage_root, str) or not storage_root.startswith(SMB_PATH_ROOT + os.path.sep)
                or not isinstance(video_path, str)
                or not (video_path == storage_root or video_path.startswith(storage_root + os.path.sep))):
            raise ValueError('SMB 影片路徑超出允許目錄')
        filesystem = storage_fs(video_path)
        if not filesystem.remote or filesystem.islink(video_path) or not filesystem.isfile(video_path):
            raise ValueError('找不到可讀取的 SMB 影片')
        relative = self._validate_smb_locator(filesystem, storage_root, video_path)
        identity = self._media_identity(video_path)
        descriptor = {
            'kind': 'smb', 'relative_path': relative,
            'identity': identity, 'nas_identity': self._nas_identity(nas_settings),
            'output_mode': output_mode,
        }
        video_id = self._video_id(descriptor)
        with self._lock:
            self._sources[video_id] = descriptor
            self._save_locked()
        return VideoSource(video_id, video_path, identity, 'smb', output_mode)

    def register_upload(self, video_path, *, allowed_roots, nas_settings,
                        remote_root, remote_video_path):
        """Bind a local staging copy to the exact already-uploaded SMB locator."""
        path, root = self._validate_local(video_path, allowed_roots)
        if (not isinstance(remote_root, str) or not remote_root.startswith(SMB_PATH_ROOT + os.path.sep)
                or not isinstance(remote_video_path, str)
                or not remote_video_path.startswith(remote_root + os.path.sep)):
            raise ValueError('NAS 影片路徑超出允許目錄')
        filesystem = storage_fs(remote_video_path)
        if (not filesystem.remote or filesystem.islink(remote_video_path)
                or not filesystem.isfile(remote_video_path)):
            raise ValueError('找不到已上傳的 NAS 影片')
        relative = self._validate_smb_locator(filesystem, remote_root, remote_video_path)
        descriptor = {
            'kind': 'local', 'path': path, 'root': root,
            'identity': self._media_identity(path), 'output_mode': 'upload',
            'nas_identity': self._nas_identity(nas_settings),
            'remote_relative_path': relative,
            'remote_identity': self._media_identity(remote_video_path),
        }
        video_id = self._video_id(descriptor)
        with self._lock:
            self._sources[video_id] = descriptor
            self._save_locked()
        return VideoSource(video_id, path, descriptor['identity'], 'local', 'upload')

    def _nas_settings(self, descriptor):
        settings = nas.load_settings(secret=True)
        if (not settings.get('enabled') or
                self._nas_identity(settings) != descriptor.get('nas_identity')):
            raise ValueError('NAS 設定已變更，請恢復原設定後重試字幕儲存')
        return settings

    @contextmanager
    def resolve(self, video_id):
        with self._lock:
            descriptor = self._sources.get(video_id)
            descriptor = json.loads(json.dumps(descriptor)) if descriptor else None
        if not isinstance(descriptor, dict) or video_id != self._video_id(descriptor):
            raise ValueError('字幕影片來源不存在或識別碼無效')
        if descriptor.get('kind') == 'local':
            path, _root = self._validate_local(descriptor.get('path'), [descriptor.get('root')])
            if self._media_identity(path) != descriptor['identity']:
                raise ValueError('字幕來源影片已變更，請重新選取')
            if descriptor.get('output_mode') == 'upload':
                settings = self._nas_settings(descriptor)
                root = nas.smb_destination()
                with nas.open_smb_storage(settings, root):
                    remote_video = os.path.join(root, descriptor['remote_relative_path'])
                    filesystem = storage_fs(remote_video)
                    if (not filesystem.remote or filesystem.islink(remote_video)
                            or not filesystem.isfile(remote_video)
                            or self._media_identity(remote_video) != descriptor.get('remote_identity')):
                        raise ValueError('NAS 影片已變更或離線，請確認已上傳的影片')
                    yield VideoSource(video_id, path, descriptor['identity'], 'local', 'upload')
                return
            yield VideoSource(video_id, path, descriptor['identity'], 'local', descriptor.get('output_mode', 'local'))
            return
        if descriptor.get('kind') != 'smb':
            raise ValueError('字幕影片來源格式無效')
        settings = self._nas_settings(descriptor)
        root = nas.smb_destination()
        with nas.open_smb_storage(settings, root):
            path = os.path.join(root, descriptor['relative_path'])
            filesystem = storage_fs(path)
            if not filesystem.remote or filesystem.islink(path) or not filesystem.isfile(path):
                raise ValueError('NAS 影片已離線或不存在')
            if self._media_identity(path) != descriptor['identity']:
                raise ValueError('字幕來源影片已變更，請重新選取')
            yield VideoSource(video_id, path, descriptor['identity'], 'smb', descriptor.get('output_mode', 'smb'))

    @staticmethod
    def _srt(result):
        if result.segments is None:
            raise ValueError('所選字幕結果沒有可發布的文字片段')
        cues = [engine.SrtCue(
            str(index),
            f'{engine._format_srt_timestamp(segment.start)} --> {engine._format_srt_timestamp(segment.end)}',
            segment.text,
        ) for index, segment in enumerate(result.segments, 1)]
        return engine.render_srt(cues)

    @staticmethod
    def _file_hash(path):
        return engine._sha256(path)

    @staticmethod
    def _temporary(filesystem, directory, data):
        folder = filesystem.mkdtemp(prefix='.uav-subtitle-', dir=directory)
        path = os.path.join(folder, 'result.tmp')
        try:
            with storage_open(path, 'xb') as target:
                target.write(data)
                target.flush()
            return folder, path
        except BaseException:
            filesystem.rmtree(folder, ignore_errors=True)
            raise

    @staticmethod
    def _safe_result_suffix(result_id):
        value = re.sub(r'[^a-zA-Z0-9_-]', '', str(result_id))[:12]
        if not value:
            raise ValueError('字幕結果識別碼無效')
        return value

    def publish_result(self, video_id, result, language, *, replace_existing=False):
        if (language not in _LANGUAGES or result.video_id != video_id
                or not isinstance(replace_existing, bool)):
            raise ValueError('字幕發布影片或語言不符')
        text = self._srt(result)
        data = text.encode('utf-8')
        expected_hash = hashlib.sha256(data).hexdigest()
        with self.resolve(video_id) as source:
            if source.output_mode == 'upload':
                settings = self._nas_settings(self._sources[video_id])
                root = nas.smb_destination()
                with nas.open_smb_storage(settings, root):
                    descriptor = self._sources[video_id]
                    remote_video = os.path.join(root, descriptor['remote_relative_path'])
                    path = self._publish_path(remote_video, language, result.result_id,
                                              data, expected_hash, replace_existing,
                                              descriptor['remote_identity'], remote_video)
            else:
                path = self._publish_path(source.video_path, language, result.result_id,
                                          data, expected_hash, replace_existing,
                                          source.identity, source.video_path)
        return {'video_id': video_id, 'result_id': result.result_id, 'language': language,
                'publish_status': 'published', 'path': os.path.basename(path)}

    def _publish_path(self, video_path, language, result_id, data, expected_hash,
                      replace_existing, identity, source_path):
        filesystem = storage_fs(video_path)
        stem = os.path.splitext(video_path)[0]
        target = stem + f'.{language}.srt'
        manifest_path = stem + '.uav-subtitles.json'
        manifest = engine._load_subtitle_provenance(manifest_path)
        old_manifest = json.loads(json.dumps(manifest))
        old_entry = manifest['tracks'].get(language)
        exists = filesystem.exists(target)
        linked = filesystem.islink(target)
        if linked and replace_existing:
            raise ValueError('字幕目標不可使用符號連結')
        old_hash = self._file_hash(target) if exists and not linked else ''
        trusted = (exists and not linked and isinstance(old_entry, dict)
                   and old_entry.get('generator') in engine.MANAGED_SUBTITLE_GENERATORS
                   and old_entry.get('media_identity') == identity
                   and old_entry.get('srt_sha256') == old_hash)
        replace_target = bool(exists and (trusted or replace_existing))
        if (exists and not replace_target and old_entry and
                old_entry.get('result_id') == result_id and old_hash == expected_hash):
            return target
        suffix = self._safe_result_suffix(result_id)
        if exists and not replace_target:
            target = stem + f'.{language}.uav-{suffix}.srt'
            counter = 2
            while filesystem.exists(target):
                if not filesystem.islink(target) and self._file_hash(target) == expected_hash:
                    return target
                target = stem + f'.{language}.uav-{suffix}-{counter}.srt'
                counter += 1

        canonical = stem + f'.{language}.srt'
        if target == canonical:
            if self._media_identity(source_path) != identity:
                raise ValueError('字幕來源影片已變更，請重新選取')
            manifest['tracks'][language] = {
                'generator': engine.SUBTITLE_GENERATOR,
                'srt_sha256': expected_hash,
                'media_identity': identity,
                'result_id': result_id,
            }
            engine._save_subtitle_provenance(manifest_path, manifest)
        folder = ''
        try:
            folder, temporary = self._temporary(filesystem, os.path.dirname(target), data)
            if self._media_identity(source_path) != identity:
                raise ValueError('字幕來源影片已變更，請重新選取')
            if replace_target and exists and self._file_hash(canonical) != old_hash:
                raise FileExistsError('字幕檔在發布期間已變更')
            if replace_target:
                filesystem.replace(temporary, target)
            else:
                filesystem.publish(temporary, target)
            return target
        except Exception:
            if target == canonical:
                try:
                    engine._save_subtitle_provenance(manifest_path, old_manifest)
                except Exception:
                    pass
            raise
        finally:
            if folder and filesystem.exists(folder):
                filesystem.rmtree(folder, ignore_errors=True)

    def _configured_roots(self):
        prefs = config._load_prefs()
        roots = [prefs.get('download_dir') or os.environ.get('DOWNLOAD_DIR') or os.path.abspath('download'),
                 prefs.get('staging_dir', '')]
        settings = nas.load_settings()
        if settings.get('enabled') and settings.get('mode') == 'direct':
            try:
                roots.append(nas.direct_destination(settings))
            except ValueError:
                pass
        return [root for root in roots if isinstance(root, str) and root]

    def list_videos(self, *, max_entries=1000):
        """List bounded history/configured-root videos and retain source-safe locators."""
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or not 1 <= max_entries <= 5000:
            raise ValueError('字幕影片清單上限無效')
        from uav_downloader.core.history import get_history_manager

        self.list_errors = []
        self._listed = {}
        candidates = {}
        history = get_history_manager()
        for entry in history._entries.values():
            path = entry.get('file_path')
            if not path or path.startswith('smb://'):
                continue
            candidates[path] = ('history', entry.get('title', ''))
        for root in self._configured_roots():
            try:
                root = self._absolute_without_links(root)
                visited = 0
                def walk_error(error, current_root=root):
                    self.list_errors.append({'source': 'local', 'root': current_root,
                                             'error': '下載或掛載目錄離線或無讀取權限'})
                for current, directories, files in os.walk(root, followlinks=False, onerror=walk_error):
                    directories[:] = [name for name in directories
                                      if not os.path.islink(os.path.join(current, name))]
                    for name in files:
                        path = os.path.join(current, name)
                        if (Path(name).suffix.lower() in _VIDEO_EXTENSIONS
                                and not os.path.islink(path)):
                            candidates.setdefault(path, ('configured', ''))
                            visited += 1
                            if visited >= max_entries:
                                break
                    if visited >= max_entries:
                        break
            except OSError:
                self.list_errors.append({'source': 'local', 'root': str(root),
                                         'error': '下載或掛載目錄離線或無讀取權限'})
        result = []
        for path, (origin, title) in candidates.items():
            try:
                root = os.path.dirname(path)
                checked, _ = self._validate_local(path, [root])
                identity = self._media_identity(checked)
                descriptor = {'kind': 'local', 'path': checked, 'root': root,
                              'identity': identity, 'output_mode': 'local'}
                video_id = self._video_id(descriptor)
                self._listed[video_id] = {'kind': 'local', 'path': checked, 'root': root}
                result.append({'video_id': video_id,
                               'name': os.path.basename(path), 'title': title,
                               'source': origin})
            except (OSError, ValueError):
                self.list_errors.append({'source': origin, 'root': os.path.dirname(path),
                                         'error': '影片離線或無讀取權限'})
        settings = nas.load_settings(secret=True)
        if settings.get('enabled') and settings.get('mode') == 'smb':
            root = nas.smb_destination()
            try:
                with nas.open_smb_storage(settings, root):
                    filesystem = storage_fs(root)
                    pending, visited = [(root, '', 0)], 0
                    while pending and visited < max_entries:
                        current, relative_dir, depth = pending.pop()
                        try:
                            names = filesystem.listdir(current)
                        except OSError:
                            self.list_errors.append({'source': 'smb', 'root': relative_dir,
                                                     'error': 'NAS 目錄離線或無讀取權限'})
                            continue
                        for name in names:
                            path = os.path.join(current, name)
                            relative = os.path.join(relative_dir, name) if relative_dir else name
                            try:
                                if filesystem.islink(path):
                                    continue
                                if filesystem.isdir(path) and depth < 5:
                                    pending.append((path, relative, depth + 1))
                                elif (filesystem.isfile(path)
                                      and Path(name).suffix.lower() in _VIDEO_EXTENSIONS):
                                    identity = self._media_identity(path)
                                    descriptor = {'kind': 'smb', 'relative_path': relative,
                                                  'identity': identity,
                                                  'nas_identity': self._nas_identity(settings),
                                                  'output_mode': 'smb'}
                                    video_id = self._video_id(descriptor)
                                    self._listed[video_id] = {
                                        'kind': 'smb', 'path': path, 'root': root,
                                        'settings': self._nas_identity(settings)}
                                    result.append({'video_id': video_id,
                                                   'name': name, 'title': '', 'source': 'smb'})
                                    visited += 1
                                    if visited >= max_entries:
                                        break
                            except OSError:
                                self.list_errors.append({'source': 'smb', 'root': relative,
                                                         'error': '影片離線或無讀取權限'})
            except (OSError, ValueError):
                self.list_errors.append({'source': 'smb', 'root': '',
                                         'error': 'NAS 離線或無讀取權限'})
        return result

    def register_history(self, video_id):
        """Register a video listed from history/configured roots by its opaque id."""
        if video_id not in self._listed:
            self.list_videos()
        locator = self._listed.get(video_id)
        if locator and locator['kind'] == 'local':
            source = self.register(locator['path'], allowed_roots=[locator['root']])
            if source.video_id != video_id:
                raise ValueError('字幕來源影片已變更，請重新選取')
            return source
        if locator and locator['kind'] == 'smb':
            settings = nas.load_settings(secret=True)
            if self._nas_identity(settings) != locator['settings']:
                raise ValueError('NAS 設定已變更，請重新選取影片')
            with nas.open_smb_storage(settings, locator['root']):
                source = self.register_smb(locator['path'], storage_root=locator['root'],
                                           nas_settings=settings)
            if source.video_id != video_id:
                raise ValueError('字幕來源影片已變更，請重新選取')
            return source
        raise ValueError('字幕影片不在下載紀錄或允許目錄中')
