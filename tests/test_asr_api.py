import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
import wave

import pytest

from uav_downloader.subtitles import asr, engine
from uav_downloader.subtitles.models import SpeechInterval, SubtitleSegment
from uav_downloader.subtitles.profiles import ProfileSnapshot


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path / 'config'))


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
    {'start': 0, 'end': 41, 'text': 'bad'},
    {'start': True, 'end': 1, 'text': 'bad'},
    {'start': 1, 'end': 0, 'text': 'bad'},
    {'start': 0, 'end': 1, 'text': None},
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
            encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
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


def test_stage_asr_uses_vad_intervals_and_rejects_out_of_range_seconds(tmp_path, api_server):
    settings, responses, requests, _ = api_server
    responses.append((200, {'segments': [segment(.2, .8, '台詞')]}))
    wav = tmp_path / 'audio.wav'
    audio(wav, 3)
    snapshot = ProfileSnapshot(json.dumps({
        'profile_id': 'asr', 'display_name': 'ASR', 'kind': 'asr',
        'provider': 'localai', 'capabilities': ['asr'],
        'base_url': settings['base_url'], 'model': settings['model'],
        'parameters': {},
    }), None, 'snapshot')
    result = asr.transcribe_stage(str(wav), [SpeechInterval(1, 2)], snapshot, 'ja', lambda: False)
    assert result == [SubtitleSegment('segment-000000-0001', 1.2, 1.8, '台詞')]
    assert [request[0] for request in requests] == ['/v1/audio/transcriptions']
    assert b'timestamp_granularities[]' in requests[0][2]
    assert b'word' not in requests[0][2]


def test_stage_asr_does_not_clamp_invalid_seconds(tmp_path, api_server):
    settings, responses, _, _ = api_server
    responses.append((200, {'segments': [segment(.8, 1.2, '越界')]}))
    wav = tmp_path / 'audio.wav'
    audio(wav, 2)
    snapshot = ProfileSnapshot(json.dumps({
        'profile_id': 'asr', 'display_name': 'ASR', 'kind': 'asr',
        'provider': 'localai', 'capabilities': ['asr'],
        'base_url': settings['base_url'], 'model': settings['model'],
        'parameters': {},
    }), None, 'snapshot')
    from uav_downloader.subtitles.models import StageRequestError
    with pytest.raises(StageRequestError, match='秒單位'):
        asr.transcribe_stage(str(wav), [SpeechInterval(1, 2)], snapshot, 'ja', lambda: False)


def test_stage_asr_rejects_whisper_whole_second_tail_beyond_short_chunk(tmp_path, api_server):
    settings, responses, _, _ = api_server
    # LocalAI/Faster-Whisper reproduced this timestamp shape for a 2.364s VAD chunk.
    responses.append((200, {'segments': [segment(0, 2, '第一段'), segment(2, 3, '第二段')]}))
    wav = tmp_path / 'audio.wav'
    audio(wav, 5)
    snapshot = ProfileSnapshot(json.dumps({
        'profile_id': 'asr', 'display_name': 'ASR', 'kind': 'asr',
        'provider': 'localai', 'capabilities': ['asr'],
        'base_url': settings['base_url'], 'model': settings['model'],
        'parameters': {},
    }), None, 'snapshot')
    from uav_downloader.subtitles.models import StageRequestError
    with pytest.raises(StageRequestError, match='秒單位'):
        asr.transcribe_stage(
            str(wav), [SpeechInterval(2.370, 4.734)], snapshot, 'ja', lambda: False)


def test_stage_asr_requests_configured_timestamp_granularities(tmp_path, api_server):
    settings, responses, requests, _ = api_server
    responses.append((200, {'segments': [segment(0, 1.62, '句子'), segment(1.84, 2.06, '下一句')]}))
    wav = tmp_path / 'audio.wav'
    audio(wav, 3)
    snapshot = ProfileSnapshot(json.dumps({
        'profile_id': 'asr', 'display_name': 'ASR', 'kind': 'asr',
        'provider': 'localai', 'capabilities': ['asr'],
        'base_url': settings['base_url'], 'model': settings['model'],
        'parameters': {'timestamp_granularities': ['segment', 'word']},
    }), None, 'snapshot')
    result = asr.transcribe_stage(str(wav), [SpeechInterval(0, 2.364)], snapshot, 'ja', lambda: False)
    assert [(item.start, item.end) for item in result] == [(0, 1.62), (1.84, 2.06)]
    body = requests[0][2]
    values = [part.split(b'\r\n\r\n', 1)[1].split(b'\r\n', 1)[0]
              for part in body.split(b'--') if b'name="timestamp_granularities[]"' in part]
    assert values == [b'segment', b'word']


@pytest.mark.parametrize('granularities', [
    [], ['word', 'word'], ['other'], ['segment', 'word', 'segment'], 'word', [None],
])
def test_asr_rejects_invalid_timestamp_granularities(granularities):
    with pytest.raises(ValueError, match='timestamp_granularities'):
        asr.validate_asr_parameters({'timestamp_granularities': granularities})


@pytest.mark.parametrize(('status', 'retryable'), [(429, True), (503, True), (401, False)])
def test_stage_asr_classifies_http_failures_without_exposing_provider_body(
        tmp_path, api_server, status, retryable):
    from uav_downloader.subtitles.models import StageRequestError
    settings, responses, _, _ = api_server
    responses.append((status, {'error': 'private-provider-body'}))
    wav = tmp_path / 'audio.wav'
    audio(wav, 1)
    profile = ProfileSnapshot(json.dumps({
        'profile_id': 'asr', 'display_name': 'ASR', 'kind': 'asr',
        'provider': 'localai', 'capabilities': ['asr'], 'base_url': settings['base_url'],
        'model': settings['model'], 'parameters': {},
    }), None, 'snapshot')
    chunk = asr.plan_transcription_chunks(str(wav), [SpeechInterval(0, 1)])[0]
    with pytest.raises(StageRequestError) as error:
        asr.transcribe_chunk(str(wav), chunk, profile, 'ja')
    assert error.value.retryable is retryable
    assert 'private-provider-body' not in str(error.value)


def test_stage_asr_invalid_json_is_permanent(tmp_path, api_server):
    from uav_downloader.subtitles.models import StageRequestError
    settings, responses, _, _ = api_server
    responses.append((200, b'{not-json'))
    wav = tmp_path / 'audio.wav'
    audio(wav, 1)
    profile = ProfileSnapshot(json.dumps({
        'profile_id': 'asr', 'display_name': 'ASR', 'kind': 'asr',
        'provider': 'localai', 'capabilities': ['asr'], 'base_url': settings['base_url'],
        'model': settings['model'], 'parameters': {},
    }), None, 'snapshot')
    chunk = asr.plan_transcription_chunks(str(wav), [SpeechInterval(0, 1)])[0]
    with pytest.raises(StageRequestError) as error:
        asr.transcribe_chunk(str(wav), chunk, profile, 'ja')
    assert error.value.retryable is False
    assert 'JSONDecodeError' not in str(error.value)


def test_stage_asr_connection_failure_is_retryable(tmp_path):
    from uav_downloader.subtitles.models import StageRequestError
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    wav = tmp_path / 'audio.wav'
    audio(wav, 1)
    profile = ProfileSnapshot(json.dumps({
        'profile_id': 'asr', 'display_name': 'ASR', 'kind': 'asr',
        'provider': 'localai', 'capabilities': ['asr'], 'base_url': f'http://127.0.0.1:{port}/v1',
        'model': 'model', 'parameters': {},
    }), None, 'snapshot')
    chunk = asr.plan_transcription_chunks(str(wav), [SpeechInterval(0, 1)])[0]
    with pytest.raises(StageRequestError) as error:
        asr.transcribe_chunk(str(wav), chunk, profile, 'ja')
    assert error.value.retryable is True
    assert '127.0.0.1' not in str(error.value)


def test_stage_asr_invalid_segments_schema_is_permanent(tmp_path, api_server):
    from uav_downloader.subtitles.models import StageRequestError
    settings, responses, _, _ = api_server
    responses.append((200, {'segments': 'not-a-list'}))
    wav = tmp_path / 'audio.wav'
    audio(wav, 1)
    profile = ProfileSnapshot(json.dumps({
        'profile_id': 'asr', 'display_name': 'ASR', 'kind': 'asr',
        'provider': 'localai', 'capabilities': ['asr'], 'base_url': settings['base_url'],
        'model': settings['model'], 'parameters': {},
    }), None, 'snapshot')
    chunk = asr.plan_transcription_chunks(str(wav), [SpeechInterval(0, 1)])[0]
    with pytest.raises(StageRequestError) as error:
        asr.transcribe_chunk(str(wav), chunk, profile, 'ja')
    assert error.value.retryable is False


@pytest.mark.parametrize('parameters', [
    {'prompt': {'unexpected': 'object'}},
    {'prompt': 'x' * 4097},
    {'prompt': 'contains\x00nul'},
    {'temperature': True},
    {'temperature': '0.4'},
    {'temperature': {'value': 0.4}},
    {'temperature': float('nan')},
    {'temperature': -0.1},
    {'temperature': 2.1},
    {'timestamp_granularities': []},
    {'timestamp_granularities': ['word', 'word']},
    {'timestamp_granularities': ['segment', 'unknown']},
    {'timestamp_granularities': ['segment', 'word', 'segment']},
    {'timestamp_granularities': 'word'},
    {'timestamp_granularities': None},
    {'timestamp_granularities': [['word']]},
])
def test_stage_asr_rejects_invalid_multipart_parameter_values_before_request(
        tmp_path, api_server, parameters):
    settings, _, requests, _ = api_server
    wav = tmp_path / 'audio.wav'
    audio(wav, 2)
    profile = json.loads(ProfileSnapshot(json.dumps({
        'profile_id': 'asr', 'display_name': 'ASR', 'kind': 'asr',
        'provider': 'localai', 'capabilities': ['asr'],
        'base_url': settings['base_url'], 'model': settings['model'],
        'parameters': parameters,
    }), None, 'snapshot').profile_json)
    snapshot = ProfileSnapshot(json.dumps(profile), None, 'snapshot')
    chunk = asr.plan_transcription_chunks(str(wav), [SpeechInterval(0, 2)])[0]
    with pytest.raises(ValueError, match='參數'):
        asr.transcribe_chunk(str(wav), chunk, snapshot, 'ja')
    assert requests == []


def test_stage_asr_overlap_preserves_context_without_duplicate_boundary_segments(
        tmp_path, monkeypatch, api_server):
    settings, responses, requests, _ = api_server
    monkeypatch.setattr(asr, 'CHUNK_SECONDS', 2)
    monkeypatch.setattr(asr, 'OVERLAP_SECONDS', 0.5)
    responses.extend((200, {'segments': items}) for items in [
        [segment(.5, 1, '首段'), segment(1.5, 2.5, '接縫上下文')],
        [segment(.5, 1, '接縫上下文'), segment(2.2, 2.8, '次段接縫')],
        [segment(.7, 1.3, '尾段')],
    ])
    wav = tmp_path / 'audio.wav'
    audio(wav, 5)
    snapshot = ProfileSnapshot(json.dumps({
        'profile_id': 'asr', 'display_name': 'ASR', 'kind': 'asr',
        'provider': 'localai', 'capabilities': ['asr'],
        'base_url': settings['base_url'], 'model': settings['model'], 'parameters': {},
    }), None, 'snapshot')
    result = asr.transcribe_stage(str(wav), [SpeechInterval(0, 5)], snapshot, 'ja', lambda: False)
    assert [item.text for item in result] == ['首段', '接縫上下文', '尾段']
    assert [(item.start, item.end) for item in result] == [(.5, 1.0), (2.0, 2.5), (4.2, 4.8)]
    assert len(requests) == 3


def test_asr_chunk_plan_exposes_stable_keys_offsets_and_one_request_executor(
        tmp_path, monkeypatch, api_server):
    settings, responses, requests, _ = api_server
    monkeypatch.setattr(asr, 'CHUNK_SECONDS', 2)
    monkeypatch.setattr(asr, 'OVERLAP_SECONDS', 0.5)
    wav = tmp_path / 'audio.wav'
    audio(wav, 5)
    chunks = asr.plan_transcription_chunks(str(wav), [SpeechInterval(0, 5)])
    assert [(chunk.index, chunk.interval_index, chunk.core_start_frame,
             chunk.core_end_frame, chunk.start_frame, chunk.end_frame) for chunk in chunks] == [
        (0, 0, 0, 32000, 0, 40000),
        (1, 0, 32000, 64000, 24000, 72000),
        (2, 0, 64000, 80000, 56000, 80000),
    ]
    responses.append((200, {'segments': [segment(.5, 1, '第一個可保存片段')]}))
    profile = ProfileSnapshot(json.dumps({
        'profile_id': 'asr', 'display_name': 'ASR', 'kind': 'asr',
        'provider': 'localai', 'capabilities': ['asr'],
        'base_url': settings['base_url'], 'model': settings['model'], 'parameters': {},
    }), None, 'snapshot')
    saved = asr.transcribe_chunk(str(wav), chunks[0], profile, 'ja', lambda: False)
    assert saved == [SubtitleSegment('segment-000000-0001', .5, 1.0, '第一個可保存片段')]
    assert len(requests) == 1


def test_stage_asr_never_posts_model_install_endpoint(tmp_path, monkeypatch, api_server):
    settings, responses, requests, _ = api_server
    monkeypatch.setattr(asr, 'DEFAULT_URL', settings['base_url'])
    responses.append((200, {'segments': []}))
    wav = tmp_path / 'chunk.wav'
    audio(wav, 1)
    result = asr._request(dict(settings, _install_model=False), str(wav))
    assert result == {'segments': []}
    assert [request[0] for request in requests] == ['/v1/audio/transcriptions']


def test_legacy_asr_keeps_custom_prefix_endpoint_contract(tmp_path, api_server):
    settings, responses, requests, _ = api_server
    responses.append((200, {'segments': []}))
    wav = tmp_path / 'chunk.wav'
    audio(wav, 1)
    asr._request(dict(settings, base_url=settings['base_url'].replace('/v1', '/api')),
                 str(wav))
    assert requests[-1][0] == '/api/audio/transcriptions'


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


def test_whisper_tail_rounding_is_clipped_to_audio_duration():
    assert asr._segments({'segments': [segment(11.3, 13.3, '日本語')]}, 13) == [(11.3, 13.0, '日本語')]


def test_production_overflow_retains_valid_audio_only():
    payload = {'segments': [segment(116.75, 121.15, '正常'),
                            segment(121.15, 123.15, '跨界'),
                            segment(123.15, 125.15, '範圍外')]}
    assert asr._segments(payload, 122) == [(116.75, 121.15, '正常'), (121.15, 122.0, '跨界')]


def test_overflow_retranscribes_real_tail_audio(tmp_path, monkeypatch, api_server):
    settings, responses, requests, _ = api_server
    monkeypatch.setattr(asr, 'CHUNK_SECONDS', 200)
    responses.extend([(200, {'segments': [segment(0, 1, '保留'), segment(121.15, 123.15, '錯誤尾端'), segment(123.15, 125.15, '越界')]}),
                      (200, {'segments': [segment(28, 29.5, '修復尾端')]})])
    wav = tmp_path / 'audio.wav'
    audio(wav, 122)
    path = asr.transcribe(settings, str(wav), str(tmp_path/'out'), str(tmp_path/'log'), None, None)
    cues = engine.parse_srt(Path(path).read_text())
    assert [cue.text for cue in cues] == ['保留', '修復尾端']
    assert cues[-1].timing == '00:02:00,000 --> 00:02:01,500'
    assert len(requests) == 2


def test_retry_reuses_only_completed_audio_chunks(tmp_path, monkeypatch, api_server):
    settings, responses, requests, _ = api_server
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path/'config'))
    monkeypatch.setattr(asr, 'CHUNK_SECONDS', 2)
    monkeypatch.setattr(asr, 'OVERLAP_SECONDS', 0)
    wav=tmp_path/'audio.wav'
    audio(wav, 4)
    responses.extend([(200, {'segments':[segment(.2, .8, '完成片段')]}), (503, {})])
    with pytest.raises(engine.SubtitleError, match='HTTP 503'):
        asr.transcribe(settings, str(wav), str(tmp_path/'out'), str(tmp_path/'log'), None, None)
    responses.append((200, {'segments':[segment(.2, .8, '重試片段')]}))
    path=asr.transcribe(settings, str(wav), str(tmp_path/'out'), str(tmp_path/'log'), None, None)
    assert len(requests)==3, 'Do not repeat the successful first request'
    assert [cue.text for cue in engine.parse_srt(Path(path).read_text())]==['完成片段', '重試片段']
    # A different model must not reuse these cached transcripts.
    responses.extend([(200, {'segments':[]}), (200, {'segments':[]})])
    asr.transcribe(dict(settings, model='other'), str(wav), str(tmp_path/'other'), str(tmp_path/'log'), None, None)
    assert len(requests)==5


@pytest.mark.parametrize('header,sequential', [(b'ftyp moov mvex', True), (b'ftyp normal legacy MP4', False)])
def test_fragmented_smb_audio_streams_without_remote_tail_scan(monkeypatch, header, sequential):
    import io
    from contextlib import nullcontext
    from types import SimpleNamespace
    calls=[]
    monkeypatch.setattr(engine, 'locate_ffmpeg', lambda:'ffmpeg')
    monkeypatch.setattr(engine, 'storage_fs', lambda _:SimpleNamespace(remote=True, media_source=lambda _:nullcontext('http://127.0.0.1/media')))
    monkeypatch.setattr(engine, 'storage_open', lambda *a,**k:io.BytesIO(header))
    monkeypatch.setattr(engine, '_run_process', lambda args,*a,**k:calls.append(args))
    engine._extract_audio('/remote/video.mp4','/tmp/audio.wav','/tmp/log',None)
    assert ('-seekable' in calls[0]) is sequential


def test_all_outside_audio_does_not_masquerade_as_no_speech(tmp_path, monkeypatch, api_server):
    settings, responses, _, _=api_server
    monkeypatch.setattr(asr, 'CHUNK_SECONDS', 200)
    wav=tmp_path/'audio.wav'
    audio(wav, 5)
    responses.extend([(200, {'segments':[segment(6, 7, 'outside')]}), (200, {'segments':[]})])
    with pytest.raises(engine.SubtitleError, match='尾段重試後仍無有效時間戳'):
        asr.transcribe(settings,str(wav),str(tmp_path/'out'),str(tmp_path/'log'),None,None)
    assert not (tmp_path/'out.srt').exists()


def word_payload(*segments):
    return {'segments': list(segments), 'words': [
        {'start': cue['start'], 'end': cue['end'], 'word': cue['text']} for cue in segments
    ]}


@pytest.fixture
def managed_worker(tmp_path, monkeypatch):
    responses, requests = [], []
    settings = dict(base_url=asr.DEFAULT_URL, model=asr.DEFAULT_MODEL, api_key='private-test-key')

    def run(command, *args, **kwargs):
        request, wav, response = map(Path, command[-3:])
        with wave.open(str(wav), 'rb') as clip:
            requests.append((json.loads(request.read_text()), clip.getnframes() / clip.getframerate()))
        payload = responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        response.write_text(json.dumps({'ok': True, 'payload': payload}), encoding='utf-8')

    def transcribe(seconds, **kwargs):
        wav = tmp_path / 'library.wav'
        audio(wav, seconds)
        return asr.transcribe(settings, str(wav), str(tmp_path / 'library'),
                                      str(tmp_path / 'log'), **kwargs)

    monkeypatch.setattr(engine, '_run_process', run)
    return transcribe, responses, requests


@pytest.mark.parametrize('name, seconds, intervals, expected', [
    ('散步', 18, [{'start': 1000, 'end': 2400}, {'start': 11000, 'end': 14000}], [
        ('00:00:01,000 --> 00:00:01,800', '朝の道'),
        ('00:00:11,200 --> 00:00:12,500', '空を見上げる'),
    ]),
    ('會議', 47, [{'start': 30500, 'end': 33000}], None),
])
def test_download_subtitles_use_managed_quality_checks(
        tmp_path, monkeypatch, api_server, name, seconds, intervals, expected):
    from uav_downloader.daemon.task_manager import TaskManager

    settings, responses, requests, _ = api_server
    monkeypatch.setattr(asr, 'DEFAULT_URL', settings['base_url'])
    monkeypatch.setattr(asr, 'load_settings', lambda **_kwargs: settings)
    monkeypatch.setattr(engine, '_extract_audio', lambda _video, wav, *_args: audio(Path(wav), seconds))
    video = tmp_path / (name + '.mp4')
    video.write_bytes(b'video')
    responses.append((200, intervals))
    if expected:
        responses.extend([
            (200, word_payload(segment(1, 1.8, '朝の道'))),
            (200, {'segments': [segment(0, 6, '空を見上げる')],
                   'words': [{'start': 2.2, 'end': 3.5, 'word': '空を見上げる'}]}),
        ])
    else:
        repetition = word_payload(segment(2, 3, '同じ文' * 12))
        responses.extend([(200, repetition), (200, repetition)])
    task = SimpleNamespace(dest=str(tmp_path), name=name, subtitle_mode='ja',
                           cancelled_event=threading.Event(), set_phase=lambda *_args: None, error=None)

    TaskManager._run_subtitles(None, task, None)

    assert [path for path, _, _ in requests] == [
        '/v1/audio/speech/timestamps', '/v1/audio/transcriptions', '/v1/audio/transcriptions']
    subtitle = video.with_suffix('.ja.srt')
    if expected:
        assert task.error is None
        assert [(cue.timing, cue.text) for cue in engine.parse_srt(subtitle.read_text())] == expected
    else:
        assert '30.500 至 33.000 秒無法確認字幕' in task.error
        assert not subtitle.exists()


def test_managed_library_http_keeps_words_on_their_side_of_silence(tmp_path, monkeypatch, api_server):
    settings, responses, requests, _ = api_server
    monkeypatch.setattr(asr, 'DEFAULT_URL', settings['base_url'])
    responses.extend([
        (200, [dict(start=1840, end=3120), dict(start=115536, end=117392)]),
        (200, word_payload(segment(1.84, 2.32, 'はい'))),
        (200, {
            'segments': [segment(0, 5.85, '別の言葉')],
            'words': [{'start': 1.86, 'end': 3.54, 'word': '別の言葉'}],
        }),
    ])
    wav = tmp_path / 'library.wav'
    audio(wav, 120)
    path = asr.transcribe(settings, str(wav), str(tmp_path / 'out'), str(tmp_path / 'log'))
    assert [(cue.timing, cue.text) for cue in engine.parse_srt(Path(path).read_text())] == [
        ('00:00:01,840 --> 00:00:02,320', 'はい'),
        ('00:01:55,396 --> 00:01:57,076', '別の言葉'),
    ]
    assert [path for path, _, _ in requests] == [
        '/v1/audio/speech/timestamps', '/v1/audio/transcriptions', '/v1/audio/transcriptions']
    assert all(auth == 'Bearer test-token' for _, auth, _ in requests)
    assert all(value in requests[0][2] for value in (
        b'threshold', b'0.5', b'min_silence_duration_ms', b'2000', b'speech_pad_ms', b'400'))
    assert all(b'timestamp_granularities[]' in body and b'word' in body
               and b'vad_filter' in body and b'true' in body
               and len(body) < 32000 * 6 for _, _, body in requests[1:])
    assert not asr._folder().joinpath('asr-cache').exists()


@pytest.mark.parametrize('left,right', [
    ((119.6, 120.2), (119.8, 120.4)),
    ((119.8, 120.4), (119.6, 120.2)),
])
def test_managed_library_joins_opposite_seam_estimates(managed_worker, left, right, monkeypatch):
    from uav_downloader.subtitles import asr_library

    transcribe, responses, requests = managed_worker
    monkeypatch.setattr(asr_library, '_VAD_WINDOW_SECONDS', 130)
    responses.extend([
        [dict(start=0, end=123000)],
        word_payload(segment(*left, '境界')),
        word_payload(segment(right[0] - 118, right[1] - 118, '境界')),
    ])
    path = transcribe(123)
    assert [cue.text for cue in engine.parse_srt(Path(path).read_text())] == ['境界']
    assert [duration for _, duration in requests] == [123, 122, 5]
    assert not responses


@pytest.mark.parametrize('conflicting', [
    word_payload(segment(1.8, 2.4, '不同句子')),
    word_payload(segment(1.8, 2.05, '境界'), segment(2.1, 2.45, '不同句子')),
])
def test_managed_library_conflicting_seam_preserves_existing_file(
        managed_worker, tmp_path, monkeypatch, conflicting):
    from uav_downloader.subtitles import asr_library

    transcribe, responses, requests = managed_worker
    monkeypatch.setattr(asr_library, '_VAD_WINDOW_SECONDS', 130)
    old = tmp_path / 'library.srt'
    old.write_text('原字幕', encoding='utf-8')
    responses.extend([
        [dict(start=0, end=123000)], word_payload(segment(119.6, 120.2, '境界')),
        conflicting, conflicting,
    ])
    with pytest.raises(asr.RecognitionQualityError, match='原片第 120.000 至 123.000 秒無法確認字幕'):
        transcribe(123)
    assert old.read_text(encoding='utf-8') == '原字幕'
    assert len(requests) == 4 and not responses


def test_managed_library_retries_seam_when_close_estimates_do_not_overlap(managed_worker, monkeypatch):
    from uav_downloader.subtitles import asr_library

    transcribe, responses, requests = managed_worker
    monkeypatch.setattr(asr_library, '_VAD_WINDOW_SECONDS', 130)
    responses.extend([
        [dict(start=0, end=123000)],
        word_payload(segment(119.8, 119.95, 'はい')),
        word_payload(segment(2.08, 2.25, 'はい')),
        word_payload(segment(1.85, 2.05, 'はい')),
    ])
    path = transcribe(123)
    assert [cue.text for cue in engine.parse_srt(Path(path).read_text())] == ['はい']
    assert len(requests) == 4 and not responses


def test_managed_library_keeps_two_occurrences_observed_at_seam(managed_worker, monkeypatch):
    from uav_downloader.subtitles import asr_library

    transcribe, responses, requests = managed_worker
    monkeypatch.setattr(asr_library, '_VAD_WINDOW_SECONDS', 130)
    responses.extend([
        [dict(start=0, end=123000)],
        word_payload(segment(119.8, 119.95, 'はい'), segment(120.08, 120.24, 'はい')),
        word_payload(segment(2.09, 2.25, 'はい')),
    ])
    path = transcribe(123)
    assert [cue.text for cue in engine.parse_srt(Path(path).read_text())] == ['はい', 'はい']
    assert len(requests) == 3 and not responses


@pytest.mark.parametrize('invalid', [
    word_payload(segment(.2, .8, 'もう一度' * 120)),
    word_payload(segment(.2, .8, 'はい。'), segment(.2, .8, 'はい！')),
    word_payload(*(segment(.1 + i * .2, .25 + i * .2, ['言葉', 'はい'][i % 2]) for i in range(6))),
    word_payload(*(segment(.1 + i * .2, .25 + i * .2, 'あ') for i in range(9))),
    {'segments': [segment(.2, .8, '全文')], 'words': [{'start': .2, 'end': .8, 'word': '全'}]},
    {'segments': [segment(.2, .8, '全文')], 'words': [{'start': float('nan'), 'end': .8, 'word': '全文'}]},
    {'segments': [segment(.2, .8, '全文')]},
    {'segments': [segment(.2, 3.4, '全文')], 'words': [
        {'start': .2, 'end': .8, 'word': '全'}, {'start': 3, 'end': 3.4, 'word': '文'},
    ]},
])
def test_managed_library_retries_suspect_result_without_accepting_empty(managed_worker, tmp_path, invalid):
    transcribe, responses, requests = managed_worker
    responses.extend([[dict(start=100, end=2000)], invalid, word_payload()])
    progress = []
    with pytest.raises(asr.RecognitionQualityError, match='^原片第 0.100 至 2.000 秒無法確認字幕$'):
        transcribe(4, progress_callback=lambda *args: progress.append(args))
    assert not (tmp_path / 'library.srt').exists()
    assert progress == [('asr_tail_retry', None)]
    assert len(requests) == 3 and not responses
    assert requests[-1][0]['_parameters']['temperature'] == '0.2'


def test_managed_library_accepts_natural_repetition_after_retry(managed_worker):
    transcribe, responses, requests = managed_worker
    responses.extend([[dict(start=100, end=1600)], word_payload(), word_payload(
        segment(.1, .5, 'はい。'), segment(.55, .95, 'はい！'), segment(1, 1.5, 'はい。'),
    )])
    path = transcribe(4)
    assert [cue.text for cue in engine.parse_srt(Path(path).read_text())] == ['はい。', 'はい！', 'はい。']
    assert len(requests) == 3 and not responses


def test_managed_library_retries_cycle_across_speech_regions(managed_worker):
    transcribe, responses, requests = managed_worker
    responses.extend([
        [dict(start=100, end=1500), dict(start=3500, end=4800)],
        word_payload(segment(.1, .4, 'まず'), segment(.6, 1, '次に'), segment(1.1, 1.4, 'まず')),
        word_payload(segment(1, 1.3, '次に'), segment(1.4, 1.7, 'まず'), segment(1.8, 2.2, '次に')),
        word_payload(segment(1, 1.3, '別の話')),
    ])
    path = transcribe(7)
    assert [cue.text for cue in engine.parse_srt(Path(path).read_text())] == ['まず', '次に', 'まず', '別の話']
    assert [duration for _, duration in requests] == pytest.approx([7, 2.5, 4.3, 4.3])


def test_managed_library_merges_speech_seen_in_overlapping_vad_windows(managed_worker):
    transcribe, responses, requests = managed_worker
    responses.extend([
        [dict(start=119000, end=122000)],
        [dict(start=1000, end=5500)],
        word_payload(segment(2, 3.5, 'ひとつの話')),
    ])
    path = transcribe(126)
    assert engine.parse_srt(Path(path).read_text())[0].timing == '00:01:59,000 --> 00:02:00,500'
    assert [duration for _, duration in requests] == [122, 8, 8.5]


def test_managed_library_redacts_service_error_and_keeps_its_type(managed_worker, tmp_path):
    transcribe, responses, requests = managed_worker
    responses.extend([[dict(start=100, end=1500)], OSError('private data'), OSError('private data')])
    with pytest.raises(engine.SubtitleError, match='^原片第 0.100 至 1.500 秒辨識服務失敗$') as failed:
        transcribe(4)
    assert type(failed.value) is engine.SubtitleError
    assert not (tmp_path / 'library.srt').exists()
    assert len(requests) == 3 and not responses


def test_managed_library_keeps_silent_vad_as_no_speech(managed_worker, tmp_path):
    transcribe, responses, requests = managed_worker
    responses.append([])
    assert transcribe(4) is None
    assert len(requests) == 1
    assert not (tmp_path / 'library.srt').exists()


def test_managed_library_cancellation_skips_publication(managed_worker, tmp_path):
    transcribe, responses, requests = managed_worker
    responses.extend([[dict(start=100, end=1500)], word_payload(segment(.2, 1.3, 'ことば'))])
    with pytest.raises(engine.SubtitleCancelled):
        transcribe(4, cancel_check=lambda: len(requests) == 2)
    assert not (tmp_path / 'library.srt').exists()


def test_compatible_api_accepts_segment_only(tmp_path, api_server):
    settings, responses, requests, _ = api_server
    responses.append((200, {'segments': [segment(.2, .8, '旧応答')]}))
    wav = tmp_path / 'library.wav'
    audio(wav, 2)
    path = asr.transcribe(settings, str(wav), str(tmp_path / 'out'), str(tmp_path / 'log'))
    assert [cue.text for cue in engine.parse_srt(Path(path).read_text())] == ['旧応答']
    assert [path for path, _, _ in requests] == ['/v1/audio/transcriptions']
