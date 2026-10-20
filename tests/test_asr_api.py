import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
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
    with pytest.raises(engine.SubtitleError, match='秒單位'):
        asr.transcribe_stage(str(wav), [SpeechInterval(1, 2)], snapshot, 'ja', lambda: False)


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
