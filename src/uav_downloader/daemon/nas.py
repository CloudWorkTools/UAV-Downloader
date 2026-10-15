"""Persistent SMB destination and safe uploads for the Web download queue."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ipaddress
import json
import ntpath
import os
from pathlib import Path
import tempfile
import threading
import uuid

from Crypto.Cipher import AES
from uav_downloader.core.paths import product_data_dir

_lock = threading.RLock()
_FIELDS = ('enabled', 'host', 'share', 'directory', 'username')


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


def load_settings(*, secret=False):
    with _lock:
        raw = _read()
        result = {k: raw.get(k, False if k == 'enabled' else '') for k in _FIELDS}
        result['password_saved'] = bool(raw.get('password_encrypted'))
        if secret:
            result['password'] = ''
            if result['password_saved']:
                try:
                    data = base64.b64decode(raw['password_encrypted'], validate=True)
                    cipher = AES.new((_directory() / 'nas.key').read_bytes(), AES.MODE_GCM, nonce=data[:16])
                    result['password'] = cipher.decrypt_and_verify(data[32:], data[16:32]).decode('utf-8')
                except Exception:
                    raise NasError('NAS 密碼無法解密，請重新輸入並保存') from None
        return result


def prepare_settings(body):
    if not isinstance(body, dict) or not isinstance(body.get('enabled', False), bool):
        raise NasError('NAS 設定格式錯誤')
    old = load_settings()
    result = {'enabled': body.get('enabled', False)}
    for key in _FIELDS[1:]:
        value = body.get(key, '')
        if not isinstance(value, str) or len(value) > 255 or any(ord(c) < 32 for c in value):
            raise NasError('NAS 欄位格式錯誤')
        result[key] = value.strip()
    password = body.get('password', '')
    if not isinstance(password, str) or len(password) > 1024:
        raise NasError('NAS 密碼格式錯誤')
    same_account = all(result[k] == old[k] for k in ('host', 'username'))
    result['password'] = password or (load_settings(secret=True)['password'] if same_account and old['password_saved'] else '')
    if result['host']:
        try:
            if ipaddress.ip_address(result['host']).version != 4:
                raise ValueError()
        except ValueError:
            raise NasError('請輸入有效的 NAS IPv4 位址') from None
    if any(c in result['share'] for c in '\\/:*?"<>|') or result['share'] in ('.', '..'):
        raise NasError('共享資料夾名稱不可包含路徑或特殊符號')
    parts = result['directory'].replace('\\', '/').split('/')
    if any(p in ('.', '..') or any(c in p for c in ':*?"<>|') for p in parts):
        raise NasError('NAS 子目錄格式錯誤')
    result['directory'] = '\\'.join(p for p in parts if p)
    if result['enabled'] and not all(result[k] for k in ('host', 'share', 'username', 'password')):
        raise NasError('請填寫 NAS IP、共享資料夾、帳號及密碼')
    return result


def save_settings(body):
    with _lock:
        values = prepare_settings(body)
        folder = _directory()
        folder.mkdir(parents=True, exist_ok=True)
        raw = {k: values[k] for k in _FIELDS}
        if values['password']:
            cipher = AES.new(_key(), AES.MODE_GCM)
            encrypted, tag = cipher.encrypt_and_digest(values['password'].encode('utf-8'))
            raw['password_encrypted'] = base64.b64encode(cipher.nonce + tag + encrypted).decode('ascii')
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
def _connection(settings):
    import smbclient
    cache = {}
    try:
        smbclient.register_session(settings['host'], username=settings['username'],
                                   password=settings['password'], connection_timeout=10,
                                   connection_cache=cache)
        root = ntpath.join('\\\\' + settings['host'], settings['share'], settings['directory'])
        yield smbclient, root, {'connection_cache': cache}
    except Exception:
        raise NasError('NAS 連線或傳送失敗，請檢查帳密、共享目錄與寫入權限；本機檔案已保留') from None
    finally:
        smbclient.reset_connection_cache(fail_on_error=False, connection_cache=cache)


def test_connection(body):
    if not isinstance(body, dict):
        raise NasError('NAS 設定格式錯誤')
    settings = prepare_settings({**body, 'enabled': True})
    with _connection(settings) as (smb, root, options):
        probe = ntpath.join(root, '.uav-test-' + uuid.uuid4().hex)
        with smb.open_file(probe, mode='xb', **options) as stream:
            stream.write(b'UAV connection test')
        smb.remove(probe, **options)


def upload_video(settings, video_file, cancelled):
    video = Path(video_file)
    if not video.is_file():
        raise NasError('找不到已下載影片，無法傳送到 NAS')
    files = [video] + sorted(p for p in video.parent.iterdir()
                             if p.is_file() and p.name.startswith(video.stem + '.') and p.suffix == '.srt')
    with _connection(settings) as (smb, root, options):
        for source in files:
            if cancelled.is_set():
                return
            target = ntpath.join(root, source.name)
            temporary = ntpath.join(root, '.uav-' + uuid.uuid4().hex + '.part')
            try:
                with source.open('rb') as local, smb.open_file(temporary, mode='xb', **options) as remote:
                    while chunk := local.read(1024 * 1024):
                        if cancelled.is_set():
                            return
                        remote.write(chunk)
                if cancelled.is_set():
                    return
                # rename refuses existing targets, preserving files already on the NAS.
                smb.rename(temporary, target, **options)
            finally:
                if smb.path.exists(temporary, **options):
                    smb.remove(temporary, **options)
