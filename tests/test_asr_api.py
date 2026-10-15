import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import wave

import pytest

from uav_downloader.subtitles import asr, engine


@pytest.fixture
def settings_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(asr.config, '_ui_prefs_path', lambda: str(tmp_path / 'prefs.json'))
    monkeypatch.delenv('UAV_ASR_API_KEY', raising=False)
    return tmp_path


def test_settings_defaults_and_secret_lifecycle(settings_folder):
    assert asr.load_settings() == dict(base_url=asr.DEFAULT_URL, model=asr.DEFAULT_MODEL, api_key_saved=False)
    profile = dict(base_url='https://asr.example/v1', model='whisper', api_key='private-test-key')
    public = asr.save_settings(profile)
    assert public['api_key_saved'] and 'api_key' not in public
    assert 'private-test-key' not in (settings_folder / 'asr.json').read_text()
    assert (settings_folder / 'asr.json').stat().st_mode & 0o777 == 0o600
    assert (settings_folder / 'asr.key').stat().st_mode & 0o777 == 0o600
    asr.save_settings(dict(profile, api_key=''))
    assert asr.load_settings(secret=True)['api_key'] == 'private-test-key'
    asr.save_settings(dict(profile, base_url='https://different.example/v1', api_key=''))
    assert asr.load_settings(secret=True)['api_key'] == ''
    asr.save_settings(profile)
    assert not asr.save_settings(dict(profile, api_key='', clear_api_key=True))['api_key_saved']


@pytest.mark.parametrize('values', [
    {'base_url': 'file:///etc/passwd'}, {'base_url': 'https://user:secret@host/v1'},
    {'base_url': 'https://host/v1?key=secret'}, {'model': 'model\nheader'},
    {'base_url': 'http://host:99999/v1'}, {'api_key': 'key\r\nvalue'},
    {'clear_api_key': 'yes'},
])
def test_invalid_settings_do_not_replace_saved_profile(settings_folder, values):
    asr.save_settings({})
    before = (settings_folder / 'asr.json').read_bytes()
    with pytest.raises(ValueError):
        asr.save_settings(values)
    assert (settings_folder / 'asr.json').read_bytes() == before


@pytest.mark.parametrize('segment', [
    {'start': -1, 'end': 1, 'text': 'bad'},
    {'start': 0, 'end': float('nan'), 'text': 'bad'},
    {'start': 0, 'end': 11, 'text': 'bad'},
    {'start': True, 'end': 1, 'text': 'bad'},
    {'start': 1, 'end': 0, 'text': 'bad'},
    {'start': 0, 'end': 1, 'text': None},
    {'start': 10.01, 'end': 10.05, 'text': 'bad'},
    {'start': 0, 'end': 10**1000, 'text': 'bad'},
])
def test_invalid_provider_segments_are_rejected(segment):
    with pytest.raises(ValueError):
        asr._segments({'segments': [segment]}, 10)


@pytest.fixture
def api_server():
    requests = []
    responses = []
    received = threading.Event()
    release = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            requests.append((self.path, self.headers.get('Authorization'), body))
            received.set()
            status, payload = responses.pop(0)
            if payload == 'wait':
                release.wait(5)
                payload = {'segments': []}
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except BrokenPipeError:
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield dict(base_url=f'http://127.0.0.1:{server.server_port}/v1', model='test-model', api_key='test-token'), responses, requests, received
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def audio(path, seconds):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x00\x00' * 16000 * seconds)


def segment(start, end, text):
    return dict(start=start, end=end, text=text)


def test_real_http_worker_merges_chunks_in_video_time(tmp_path, monkeypatch, api_server):
    settings, responses, requests, _ = api_server
    monkeypatch.setattr(asr, 'CHUNK_SECONDS', 2)
    monkeypatch.setattr(asr, 'OVERLAP_SECONDS', 1)
    responses.extend((200, {'segments': items}) for items in [
        [segment(.25, .75, '最初'), segment(2.25, 2.75, '重複')],
        [segment(.25, .75, '重複'), segment(1.25, 1.75, '次'), segment(2.25, 2.75, '三')],
        [segment(.25, .75, '重複'), segment(1.25, 1.75, '最後')],
    ])
    wav = tmp_path / 'audio.wav'
    audio(wav, 5)
    path = asr.transcribe(settings, str(wav), str(tmp_path / 'out'), str(tmp_path / 'log'), None, None)
    cues = engine.parse_srt(Path(path).read_text())
    assert [cue.text for cue in cues] == ['最初', '次', '三', '最後']
    assert [cue.timing for cue in cues] == [
        '00:00:00,250 --> 00:00:00,750', '00:00:02,250 --> 00:00:02,750',
        '00:00:03,250 --> 00:00:03,750', '00:00:04,250 --> 00:00:04,750']
    assert len(requests) == 3
    for path, auth, body in requests:
        assert path == '/v1/audio/transcriptions' and auth == 'Bearer test-token'
        assert b'verbose_json' in body and b'test-model' in body and b'ja' in body
    assert not list(tmp_path.glob('asr-api-*'))


def test_http_failure_hides_provider_body(tmp_path, api_server):
    settings, responses, _, _ = api_server
    responses.append((401, {'error': 'secret-provider-body'}))
    wav = tmp_path / 'audio.wav'
    audio(wav, 1)
    with pytest.raises(engine.SubtitleError, match='HTTP 401') as error:
        asr.transcribe(settings, str(wav), str(tmp_path / 'out'), str(tmp_path / 'log'), None, None)
    assert 'secret-provider-body' not in str(error.value)
    assert not (tmp_path / 'out.srt').exists()


def test_cancellation_terminates_pending_http_worker(tmp_path, api_server):
    settings, responses, _, received = api_server
    responses.append((200, 'wait'))
    wav = tmp_path / 'audio.wav'
    audio(wav, 1)
    with pytest.raises(engine.SubtitleCancelled):
        asr.transcribe(settings, str(wav), str(tmp_path / 'out'), str(tmp_path / 'log'), received.is_set, None)
    assert not list(tmp_path.glob('asr-api-*'))


def test_api_generation_reuses_provenance_and_protects_user_edits(tmp_path, monkeypatch):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')
    calls = []
    def extract(_video, wav, *_):
        audio(Path(wav), 1)
    def transcribe(settings, wav, output, *_):
        calls.append(settings['model'])
        Path(output + '.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\n日本語\n')
        return output + '.srt'
    monkeypatch.setattr(engine, '_extract_audio', extract)
    monkeypatch.setattr(engine, '_prepare_runtime', lambda *_: pytest.fail('Windows runtime used'))
    monkeypatch.setattr(asr, 'transcribe', transcribe)
    settings = dict(base_url=asr.DEFAULT_URL, model=asr.DEFAULT_MODEL)
    engine.generate_subtitles(str(video), 'ja', asr_settings=settings)
    engine.generate_subtitles(str(video), 'ja', asr_settings=settings)
    assert calls == [asr.DEFAULT_MODEL]
    engine.generate_subtitles(str(video), 'ja', asr_settings=dict(settings, model='changed'))
    assert calls == [asr.DEFAULT_MODEL, 'changed']
    subtitle = video.with_suffix('.ja.srt')
    subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\nUser edited\n')
    engine.generate_subtitles(str(video), 'ja', asr_settings=settings)
    assert len(calls) == 2 and 'User edited' in subtitle.read_text()


def test_same_host_model_is_installed_before_transcription(tmp_path, monkeypatch, api_server):
    settings, responses, requests, _ = api_server
    monkeypatch.setattr(asr, 'DEFAULT_URL', settings['base_url'])
    responses.extend([(204, {}), (200, {'segments': []})])
    wav = tmp_path / 'audio.wav'
    audio(wav, 1)
    assert asr._request(settings, wav) == {'segments': []}
    assert [item[0] for item in requests] == ['/v1/models/test-model', '/v1/audio/transcriptions']
    assert all(item[1] == 'Bearer test-token' for item in requests)
