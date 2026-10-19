"""Persistent SMB destination and safe uploads for the Web download queue."""
from __future__ import annotations

import base64
import errno
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import ntpath
import os
import re
import shutil
import time
from pathlib import Path
import tempfile
import threading
import uuid
from urllib.parse import unquote, urlsplit

from Crypto.Cipher import AES
from uav_downloader.core.paths import product_data_dir
from uav_downloader.core.storage import SMB_PATH_ROOT, registered_storage, storage_fs
from uav_downloader.core.smb_storage import smb_lock

_lock = threading.RLock()
_FIELDS = ('enabled', 'mode', 'host', 'share', 'directory', 'username', 'direct_path')


class NasError(ValueError):
    pass


def _directory():
    return Path(os.environ['CONFIG_DIR']) if os.environ.get('CONFIG_DIR') else product_data_dir()


def _read():
    try:
        value = json.loads((_directory() / 'nas.json').read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Expected settings object')
        return value
    except FileNotFoundError:
        return {}
    except Exception:
        raise NasError('NAS 設定無法讀取，請重新設定') from None


def _key():
    path = _directory() / 'nas.key'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(fd, 'wb') as stream:
        key = os.urandom(32)
        stream.write(key)
    return key


def encrypt_password(password):
    with _lock:
        _directory().mkdir(parents=True, exist_ok=True)
        cipher = AES.new(_key(), AES.MODE_GCM)
        encrypted, tag = cipher.encrypt_and_digest(password.encode('utf-8'))
        return base64.b64encode(cipher.nonce + tag + encrypted).decode('ascii')


def decrypt_password(value):
    try:
        data = base64.b64decode(value, validate=True)
        cipher = AES.new((_directory() / 'nas.key').read_bytes(), AES.MODE_GCM, nonce=data[:16])
        return cipher.decrypt_and_verify(data[32:], data[16:32]).decode('utf-8')
    except Exception:
        raise NasError('NAS 密碼無法解密，請重新輸入並保存') from None


def load_settings(*, secret=False):
    with _lock:
        raw = _read()
        result = {k: raw.get(k, False if k == 'enabled' else 'upload' if k == 'mode' else '') for k in _FIELDS}
        result['password_saved'] = bool(raw.get('password_encrypted'))
        if secret:
            result['password'] = ''
            if result['password_saved']:
                result['password'] = decrypt_password(raw['password_encrypted'])
        return result



def _normalize_address(settings):
    address = settings['host'].replace('\\', '/')
    if '/' in address or ':' in address:
        try:
            parsed = urlsplit(address if '://' in address else 'smb://' + address.lstrip('/'))
            if parsed.scheme.lower() != 'smb' or parsed.query or parsed.fragment or parsed.port not in (None, 445):
                raise ValueError()
            if parsed.username is not None or parsed.password is not None:
                raise NasError('請將帳號與密碼填入各自欄位，不要放在 NAS 網址中')
            host = parsed.hostname or ''
            path = unquote(parsed.path)
            if any(ord(c) < 32 for c in path):
                raise ValueError()
            parts = path.strip('/').split('/')
            if parts[0]:
                settings['share'] = parts[0]
                settings['directory'] = '/'.join(parts[1:])
        except NasError:
            raise
        except (ValueError, TypeError):
            raise NasError('NAS 網址格式錯誤，請使用 smb://主機/共享資料夾 或 主機/共享資料夾；帳密請分開填寫') from None
    else:
        host = address
    host = host.lower().rstrip('.')
    if host:
        try:
            if ipaddress.ip_address(host).version != 4:
                raise ValueError()
        except ValueError:
            if (len(host) > 253 or re.fullmatch(r'[0-9.]+', host) or
                    not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                            for label in host.split('.'))):
                raise NasError('請輸入有效的 NAS IP 或主機名稱') from None
    settings['host'] = host


def prepare_settings(body, *, use_saved_password=True):
    if not isinstance(body, dict) or not isinstance(body.get('enabled', False), bool):
        raise NasError('NAS 設定格式錯誤')
    old = load_settings() if use_saved_password else None
    result = {'enabled': body.get('enabled', False), 'mode': body.get('mode', 'upload')}
    if result['mode'] not in ('upload', 'direct', 'smb'):
        raise NasError('不支援的 NAS 儲存模式')
    for key in ('host', 'share', 'directory', 'username', 'direct_path'):
        value = body.get(key, '')
        if not isinstance(value, str) or len(value) > (4096 if key == 'direct_path' else 255) or any(ord(c) < 32 for c in value):
            raise NasError('NAS 欄位格式錯誤')
        result[key] = value.strip()
    if result['mode'] != 'direct' and result['direct_path'].lower().startswith('smb:'):
        result['direct_path'] = ''
    _normalize_address(result)
    password = body.get('password', '')
    if not isinstance(password, str) or len(password) > 1024:
        raise NasError('NAS 密碼格式錯誤')
    same_account = old is not None and all(result[k] == old[k] for k in ('host', 'username'))
    result['password'] = password or (load_settings(secret=True)['password'] if result['mode'] in ('upload', 'smb') and same_account and old['password_saved'] else '')
    if any(c in result['share'] for c in '\\/:*?"<>|') or result['share'] in ('.', '..'):
        raise NasError('共享資料夾名稱不可包含路徑或特殊符號')
    parts = result['directory'].replace('\\', '/').split('/')
    if any(p in ('.', '..') or any(c in p for c in ':*?"<>|') for p in parts):
        raise NasError('NAS 子目錄格式錯誤')
    result['directory'] = '\\'.join(p for p in parts if p)
    if result['enabled'] and result['mode'] == 'direct':
        result['direct_path'] = direct_destination(result, probe=True)
    if result['enabled'] and result['mode'] in ('upload', 'smb') and not all(result[k] for k in ('host', 'share', 'username', 'password')):
        raise NasError('請填寫 NAS 位址、共享資料夾、帳號及密碼')
    return result


def save_settings(body):
    with _lock:
        values = prepare_settings(body)
        folder = _directory()
        folder.mkdir(parents=True, exist_ok=True)
        raw = {k: values[k] for k in _FIELDS}
        if values['password']:
            raw['password_encrypted'] = encrypt_password(values['password'])
        elif all(values[k] == _read().get(k, '') for k in ('host', 'username')):
            saved = _read().get('password_encrypted')
            if saved:
                raw['password_encrypted'] = saved
        fd, temporary = tempfile.mkstemp(dir=folder, prefix='.nas-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(raw, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, folder / 'nas.json')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return load_settings()


@contextmanager
def _connection(settings, *, create_directory=True):
    import smbclient
    cache = {}
    try:
        smbclient.register_session(settings['host'], username=settings['username'],
                                   password=settings['password'], connection_timeout=10,
                                   connection_cache=cache)
        root = ntpath.join('\\\\' + settings['host'], settings['share'], settings['directory'])
        if settings['directory'] and create_directory:
            smbclient.makedirs(root, exist_ok=True, connection_cache=cache)
        yield smbclient, root, {'connection_cache': cache}
    except Exception:
        message = 'NAS 連線或傳送失敗，請檢查位址或名稱解析、帳密、共享目錄與寫入權限'
        if settings.get('mode', 'upload') == 'upload':
            message += '；本機檔案已保留'
        raise NasError(message) from None
    finally:
        smbclient.reset_connection_cache(fail_on_error=False, connection_cache=cache)


def _space_values(total, free, available):
    if any(type(value) is not int for value in (total, free, available)) or total <= 0 or not 0 <= free <= total or not 0 <= available <= total:
        raise ValueError('NAS 回報無效容量')
    return {'total_bytes': total, 'free_bytes': free, 'available_bytes': min(free, available),
            'used_bytes': total - free}


def _smb_space(smb, root, options):
    volume = smb.stat_volume(root, **options)
    return _space_values(volume.total_size, volume.actual_available_size, volume.caller_available_size)


def storage_space(settings=None):
    try:
        settings = load_settings(secret=True) if settings is None else settings
        if not settings.get('enabled'):
            return {'enabled': False, 'space': None}
        if settings.get('mode') == 'direct':
            usage = shutil.disk_usage(direct_destination(settings, probe=False))
            space = _space_values(usage.total, usage.free, usage.free)
        else:
            with _connection(settings, create_directory=False) as (smb, root, options):
                space = _smb_space(smb, root, options)
        return {'enabled': True, 'space': space, 'checked_at': time.time()}
    except Exception:
        return {'enabled': True, 'space': None, 'space_error': '目前無法讀取 NAS 容量，請確認連線、權限及 NAS 是否支援容量查詢'}


def test_connection(body, *, use_saved_password=True):
    if not isinstance(body, dict):
        raise NasError('NAS 設定格式錯誤')
    settings = prepare_settings({**body, 'enabled': True}, use_saved_password=use_saved_password)
    if settings['mode'] == 'direct':
        return storage_space(settings)
    with _connection(settings) as (smb, root, options):
        probe = ntpath.join(root, '.uav-test-' + uuid.uuid4().hex)
        with smb.open_file(probe, mode='xb', **options) as stream:
            stream.write(b'UAV connection test')
        smb.remove(probe, **options)
        try:
            return {'enabled': True, 'space': _smb_space(smb, root, options), 'checked_at': time.time()}
        except Exception:
            return {'enabled': True, 'space': None, 'space_error': '連線與寫入測試成功，但 NAS 無法回報容量'}


def upload_video(settings, video_file, cancelled, published=None, *, subtitles_only=False):
    video = Path(video_file)
    if not video.is_file():
        raise NasError('找不到已下載影片，無法傳送到 NAS')
    files = ([] if subtitles_only else [video]) + sorted(p for p in video.parent.iterdir()
                             if p.is_file() and p.name.startswith(video.stem + '.') and p.suffix == '.srt')
    with _connection(settings) as (smb, root, options):
        lock = ntpath.join(root, _target_lock_name(video.name))
        if published is None:
            published = []
        try:
            with smb_lock(lock, **options):
                for source in files:
                    if cancelled.is_set():
                        return
                    target = ntpath.join(root, source.name)
                    temporary = ntpath.join(root, '.uav-' + uuid.uuid4().hex + '.part')
                    try:
                        with source.open('rb') as local, smb.open_file(temporary, mode='xb', **options) as remote:
                            published.append(temporary)
                            while chunk := local.read(1024 * 1024):
                                if cancelled.is_set():
                                    return
                                remote.write(chunk)
                        if cancelled.is_set():
                            return
                        # rename refuses existing targets, preserving files already on the NAS.
                        replacing = smb.path.exists(target, **options)
                        if settings.get('_overwrite'):
                            if smb.path.islink(target, **options):
                                raise NasError('目的地檔案不可使用符號連結')
                            smb.replace(temporary, target, **options)
                        else:
                            smb.rename(temporary, target, **options)
                        if not replacing:
                            published.append(target)
                        published.remove(temporary)
                    finally:
                        if smb.path.exists(temporary, **options):
                            smb.remove(temporary, **options)
                        if temporary in published:
                            published.remove(temporary)
        finally:
            if cancelled.is_set():
                _remove_uploaded(smb, published, options)


def _remove_uploaded(smb, published, options):
    for target in published[:]:
        if smb.path.exists(target, **options):
            smb.remove(target, **options)
        published.remove(target)


def cleanup_upload(settings, published):
    with _connection(settings) as (smb, _root, options):
        _remove_uploaded(smb, published, options)



def _linux_mount_type(path, mountinfo):
    best = ('', '')
    for line in mountinfo.splitlines():
        fields, filesystem = line.split(' - ', 1)
        mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields.split()[4])
        if path == mount or path.startswith(mount.rstrip('/') + '/'):
            if len(mount) > len(best[0]):
                best = (mount, filesystem.split()[0])
    return best[1]


def _is_network_path(path):
    if os.name == 'nt':
        import ctypes
        import ntpath
        root = ntpath.splitdrive(path)[0] + '\\'
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        get_type.argtypes = [ctypes.c_wchar_p]
        get_type.restype = ctypes.c_uint
        return get_type(root) == 4
    try:
        filesystem = _linux_mount_type(path, Path('/proc/self/mountinfo').read_text())
    except OSError:
        raise NasError('此平台無法驗證 NAS 掛載，請使用 Linux Docker 或 Windows 網路磁碟機') from None
    return filesystem in ('cifs', 'smb3', 'nfs', 'nfs4')


def direct_destination(settings, *, probe=False):
    value = settings.get('direct_path', '')
    if isinstance(value, str) and value.lower().startswith('smb:'):
        raise NasError('這是 SMB 連線網址，不是已掛載的絕對路徑。直接儲存請先將 NAS 掛載到系統，再填寫容器內路徑，例如 /nas；帳密直接連線請選「透過 SMB 直接儲存至 NAS」')
    if not isinstance(value, str) or not os.path.isabs(value):
        raise NasError('請填寫已掛載的 NAS 絕對路徑，例如 /nas 或 Windows 網路路徑')
    try:
        path = str(Path(value).resolve(strict=True))
        if not Path(path).is_dir() or not _is_network_path(path):
            raise NasError('NAS 路徑未掛載為網路檔案系統，已停止操作，避免誤存至本機')
        if probe:
            with tempfile.TemporaryFile(dir=path) as stream:
                stream.write(b'UAV NAS write test')
                stream.flush()
                os.fsync(stream.fileno())
    except OSError:
        raise NasError('NAS 路徑不存在或無法寫入，請確認掛載與權限') from None
    return path


def _target_lock_name(filename):
    return '.uav-' + hashlib.sha256(filename.casefold().encode()).hexdigest() + '.lock'


def smb_destination():
    return os.path.join(SMB_PATH_ROOT, uuid.uuid4().hex)


def public_destination(settings, filename=''):
    from urllib.parse import quote
    parts = [settings['share'], *settings.get('directory', '').replace('\\', '/').split('/')]
    if filename:
        parts.append(filename)
    return 'smb://' + settings['host'] + '/' + '/'.join(quote(part, safe='') for part in parts if part)


@contextmanager
def open_smb_storage(settings, destination):
    from uav_downloader.core.smb_storage import SmbStorage
    backend = SmbStorage(settings, destination)
    try:
        with registered_storage(destination, backend):
            if settings['directory']:
                backend.makedirs(destination, exist_ok=True)
            if not backend.isdir(destination):
                raise NasError('NAS 共享資料夾或子目錄不存在')
            yield backend
    finally:
        backend.close()


@contextmanager
def reserve_direct_target(settings, filename, *, owned_paths=(), overwrite=False):
    folder = (settings['_smb_path'] if settings.get('mode') == 'smb'
              else direct_destination(settings))
    target = os.path.join(folder, filename)
    lock = os.path.join(folder, _target_lock_name(filename))
    filesystem = storage_fs(folder)
    try:
        handle = filesystem.lock(lock)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EBUSY) or isinstance(exc, BlockingIOError):
            raise NasError('此 NAS 檔案已有下載任務使用中，請等待該任務結束後再試') from None
        raise NasError('NAS 無法取得下載鎖，請確認連線及讀寫、刪除權限') from None
    try:
        if filesystem.islink(target) or (not overwrite and filesystem.exists(target) and target not in owned_paths):
            raise NasError('NAS 已存在同名影片，已停止下載以避免覆寫')
        yield
    finally:
        handle.close()
