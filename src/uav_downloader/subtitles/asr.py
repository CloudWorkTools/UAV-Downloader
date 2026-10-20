"""OpenAI-compatible speech recognition for the Web/Docker subtitle pipeline."""
from __future__ import annotations

import base64
import hashlib
import math
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit
import wave

from Crypto.Cipher import AES
import requests

from uav_downloader.core import config

DEFAULT_URL = 'http://uav-speaches:8000/v1'
DEFAULT_MODEL = 'Systran/faster-whisper-small'
CHUNK_SECONDS = 120
OVERLAP_SECONDS = 2
PIPELINE_VERSION = 'openai-asr-tail-retry-cache-v2'
TAIL_SECONDS = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_LOCK = threading.RLock()


def _folder():
    return Path(config._ui_prefs_path()).parent


def _read():
    try:
        raw = json.loads((_folder() / 'asr.json').read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            raise ValueError()
        return raw
    except FileNotFoundError:
        return {}
    except Exception:
        raise ValueError('語音辨識設定無法讀取，請重新設定') from None


def _validate(value):
    if not isinstance(value, dict):
        raise ValueError('語音辨識設定格式錯誤')
    result = {}
    for field, default in (('base_url', DEFAULT_URL), ('model', DEFAULT_MODEL)):
        text = value.get(field, default)
        if not isinstance(text, str) or not text.strip() or len(text) > 2048 or re.search(r'[\x00-\x20\x7f]', text.strip()):
            raise ValueError('請填寫有效的辨識 API 網址與模型')
        result[field] = text.strip().rstrip('/') if field == 'base_url' else text.strip()
    url = urlsplit(result['base_url'])
    try:
        port = url.port
    except ValueError:
        raise ValueError('辨識 API 連接埠無效') from None
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment or port == 0):
        raise ValueError('辨識 API 僅接受 HTTP／HTTPS base URL，帳密請分開填寫')
    if len(result['model']) > 160:
        raise ValueError('辨識模型名稱過長')
    return result


def _encrypt_secret(secret):
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    key_path = folder / 'asr.key'
    try:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(os.urandom(32))
    cipher = AES.new(key_path.read_bytes(), AES.MODE_GCM)
    encrypted, tag = cipher.encrypt_and_digest(secret.encode('utf-8'))
    return base64.b64encode(cipher.nonce + tag + encrypted).decode('ascii')


def _decrypt_secret(encoded):
    data = base64.b64decode(encoded, validate=True)
    cipher = AES.new((_folder() / 'asr.key').read_bytes(), AES.MODE_GCM, nonce=data[:16])
    return cipher.decrypt_and_verify(data[32:], data[16:32]).decode('utf-8')


def load_settings(*, secret=False):
    with _LOCK:
        raw = _read()
        result = _validate(raw)
        environment_key = os.environ.get('UAV_ASR_API_KEY', '')
        result['api_key_saved'] = bool(raw.get('api_key_encrypted') or environment_key)
        if secret:
            result['api_key'] = environment_key
            if raw.get('api_key_encrypted'):
                try:
                    result['api_key'] = _decrypt_secret(raw['api_key_encrypted'])
                except Exception:
                    raise ValueError('辨識 API 金鑰無法解密，請重新輸入') from None
        return result


def save_settings(value):
    with _LOCK:
        raw = _validate(value)
        key = value.get('api_key', '')
        clear = value.get('clear_api_key', False)
        if (not isinstance(key, str) or len(key) > 4096 or re.search(r'[\x00-\x20\x7f]', key)
                or type(clear) is not bool):
            raise ValueError('辨識 API 金鑰格式錯誤')
        old = _read()
        # Saved credentials belong to one server URL.
        if not clear and not key and raw['base_url'] == old.get('base_url', DEFAULT_URL):
            raw['api_key_encrypted'] = old.get('api_key_encrypted', '')
        folder = _folder()
        folder.mkdir(parents=True, exist_ok=True)
        if key and not clear:
            raw['api_key_encrypted'] = _encrypt_secret(key)
        fd, temporary = tempfile.mkstemp(dir=folder, prefix='.asr-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(raw, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, folder / 'asr.json')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return load_settings()


def _request(settings, wav_path):
    headers = {}
    if settings.get('api_key'):
        headers['Authorization'] = 'Bearer ' + settings['api_key']
    # Audio and credentials must not follow redirects or the download proxy.
    with requests.Session() as session:
        session.trust_env = False
        if settings['base_url'] == DEFAULT_URL:
            with session.post(
                    DEFAULT_URL + '/models/' + quote(settings['model'], safe='/'),
                    headers=headers, timeout=(10, 900), allow_redirects=False) as installed:
                if installed.status_code not in (200, 201, 204):
                    raise ValueError(f'辨識 API 模型下載失敗 HTTP {installed.status_code}')
        with open(wav_path, 'rb') as audio, session.post(
                settings['base_url'] + '/audio/transcriptions', headers=headers,
                files={'file': ('audio.wav', audio, 'audio/wav')},
                data={'model': settings['model'], 'language': 'ja',
                      'response_format': 'verbose_json', 'timestamp_granularities[]': 'segment'},
                timeout=(10, 900), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError(f'辨識 API 回應 HTTP {response.status_code}')
            content = bytearray()
            for block in response.iter_content(65536):
                content.extend(block)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError('辨識 API 回應過大')
    return json.loads(content)


def _segments(payload, duration):
    if not isinstance(payload, dict) or not isinstance(payload.get('segments'), list) or len(payload['segments']) > 10000:
        raise ValueError('辨識 API 必須回傳附時間戳的 segments')
    result = []
    previous = 0.0
    for item in payload['segments']:
        if not isinstance(item, dict):
            raise ValueError('辨識 API 片段格式錯誤')
        start, end, text = item.get('start'), item.get('end'), item.get('text')
        if type(start) not in (int, float) or type(end) not in (int, float):
            raise ValueError('辨識 API 片段文字或時間戳無效')
        try:
            finite = math.isfinite(start) and math.isfinite(end)
        except OverflowError:
            finite = False
        if (not finite or not 0 <= start < end <= duration + TAIL_SECONDS or start < previous
                or not isinstance(text, str) or len(text) > 16384 or '\x00' in text):
            raise ValueError('辨識 API 片段文字或時間戳無效')
        previous = start
        text = ' '.join(text.split())
        if text and start < duration:
            result.append((float(start), min(float(end), duration), text))
    return result


def _prune_cache(folder):
    files = []
    for path in folder.glob('*.json'):
        try:
            stat = path.stat()
            if stat.st_mtime < time.time() - 7 * 86400:
                path.unlink()
            else:
                files.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            pass
    total = sum(size for _, size, _ in files)
    for _, size, path in sorted(files):
        if total <= 128 * 1024 * 1024:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            pass


def _recognize(source, start, stop, settings, folder, request_path, log_path,
               cancel_check, progress_callback, cache, retry_tail=True):
    from uav_downloader.subtitles import engine

    engine._check_cancel(cancel_check)
    rate = source.getframerate()
    duration = (stop - start) / rate
    chunk_path = os.path.join(folder, 'chunk.wav')
    result_path = os.path.join(folder, 'response.json')
    engine._ensure_asr_temp_space(folder, (stop - start) * 2)
    source.setpos(start)
    content = source.readframes(stop - start)
    identity = hashlib.sha256(json.dumps({
        'pipeline': PIPELINE_VERSION, 'url': settings['base_url'], 'model': settings['model'],
        'retry_tail': retry_tail, 'tail_seconds': TAIL_SECONDS,
        'start_frame': start, 'stop_frame': stop, 'overlap_seconds': OVERLAP_SECONDS,
    }, sort_keys=True).encode() + content).hexdigest()
    saved = cache / (identity + '.json')
    try:
        with saved.open('rb') as stream:
            encoded = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(encoded) <= MAX_RESPONSE_BYTES:
            segments = _segments(json.loads(encoded), duration)
            engine._notify(progress_callback, 'asr_cached', None)
            return segments
    except (OSError, ValueError, TypeError):
        pass
    with wave.open(chunk_path, 'wb') as chunk:
        chunk.setparams(source.getparams())
        chunk.writeframes(content)
    engine._run_process(
        [sys.executable, '-m', __name__, request_path, chunk_path, result_path],
        log_path, cancel_check, timeout_seconds=1850, disk_guard_path=folder)
    try:
        with open(result_path, 'rb') as stream:
            encoded = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ValueError('辨識 API 回應過大')
        envelope = json.loads(encoded)
        if not isinstance(envelope, dict) or envelope.get('ok') is not True:
            raise ValueError(envelope.get('error', '辨識 API 回應無效') if isinstance(envelope, dict) else '辨識 API 回應無效')
        payload = envelope.get('payload')
        segments = _segments(payload, duration)
    except (OSError, ValueError, TypeError) as exc:
        raise engine.SubtitleError(str(exc)) from None
    if retry_tail and any(item['end'] > duration for item in payload['segments']):
        engine._notify(progress_callback, 'asr_tail_retry', None)
        cut = max(0, duration - TAIL_SECONDS)
        # Start before a crossing cue so retrying cannot discard half its sentence.
        cut = min([cut] + [begin for begin, end, _ in segments if begin < cut < end])
        prefix = [cue for cue in segments if cue[1] <= cut]
        retried = []
        step = TAIL_SECONDS * rate
        first = start + int(cut * rate)
        for position in range(first, stop, step):
            left = max(first, position - OVERLAP_SECONDS * rate)
            right = min(stop, position + step + OVERLAP_SECONDS * rate)
            for begin, end, text in _recognize(
                    source, left, right, settings, folder, request_path, log_path,
                    cancel_check, progress_callback, cache, retry_tail=False):
                begin += (left - start) / rate
                end += (left - start) / rate
                midpoint = (begin + end) / 2
                if (position - start) / rate <= midpoint < (min(stop, position + step) - start) / rate:
                    retried.append((begin, end, text))
        # Preserve valid original speech if the smaller retry returns no usable cues.
        if retried:
            segments = prefix + retried
        elif not segments and any(str(item['text']).strip() for item in payload['segments']):
            raise engine.SubtitleError('辨識 API 尾段重試後仍無有效時間戳')
    engine._check_cancel(cancel_check)
    cached = {'segments': [dict(start=begin, end=end, text=text) for begin, end, text in segments]}
    engine._atomic_write_text(str(saved), json.dumps(cached, ensure_ascii=False))
    return segments


def transcribe(settings, wav_path, output_base, log_path, cancel_check=None, progress_callback=None):
    from uav_downloader.subtitles import engine

    cache = _folder() / 'asr-cache'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    _prune_cache(cache)
    cues = []
    with tempfile.TemporaryDirectory(prefix='asr-api-', dir=os.path.dirname(wav_path)) as folder:
        request_path = os.path.join(folder, 'request.json')
        fd = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(settings, stream)
        with wave.open(wav_path, 'rb') as source:
            rate = source.getframerate()
            frames = source.getnframes()
            if source.getnchannels() != 1 or source.getsampwidth() != 2 or rate != 16000:
                raise engine.SubtitleError('辨識音訊必須為 16 kHz 單聲道 PCM')
            step = CHUNK_SECONDS * rate
            overlap = OVERLAP_SECONDS * rate
            for position in range(0, frames, step):
                start = max(0, position - overlap)
                stop = min(frames, position + step + overlap)
                try:
                    segments = _recognize(source, start, stop, settings, folder, request_path,
                                          log_path, cancel_check, progress_callback, cache)
                except engine.SubtitleCancelled:
                    raise
                except engine.SubtitleError as exc:
                    raise engine.SubtitleError(
                        f'第 {start / rate:.1f} 至 {stop / rate:.1f} 秒辨識失敗：{exc}') from exc
                for begin, end, text in segments:
                    absolute_begin, absolute_end = begin + start / rate, end + start / rate
                    midpoint = (absolute_begin + absolute_end) / 2
                    if position / rate <= midpoint < min(frames, position + step) / rate:
                        cues.append((absolute_begin, absolute_end, text))
                if len(cues) > engine.MAX_WHISPER_CUES:
                    raise engine.SubtitleError('辨識 API 字幕片段過多')
                engine._notify(progress_callback, 'transcribe_ja', min(100, int((position + step) * 100 / frames)))
    _prune_cache(cache)
    if not cues:
        return None
    path = output_base + '.srt'
    ordered = [engine.SrtCue(str(index),
        f'{engine._format_srt_timestamp(begin)} --> {engine._format_srt_timestamp(end)}', text)
        for index, (begin, end, text) in enumerate(sorted(cues, key=lambda cue: cue[:2]), 1)]
    engine._atomic_write_text(path, engine.render_srt(ordered))
    return path


def _main():
    try:
        settings = json.loads(Path(sys.argv[1]).read_text())
        result = {'ok': True, 'payload': _request(settings, sys.argv[2])}
    except Exception as exc:
        # Provider bodies and exception URLs may expose secrets; never log them.
        message = str(exc) if isinstance(exc, ValueError) and str(exc).startswith('辨識 API') else '辨識 API 連線或回應失敗，請確認服務、模型與金鑰'
        result = {'error': message}
    Path(sys.argv[3]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    _main()
