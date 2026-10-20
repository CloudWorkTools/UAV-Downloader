import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import wave

import pytest

from uav_downloader.subtitles import vad
from uav_downloader.subtitles.profiles import ProfileSnapshot
from uav_downloader.subtitles.models import SpeechInterval


@pytest.fixture
def vad_server():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.path, self.headers.get('Authorization'), payload))
            body = json.dumps({'segments': [{'start': .25, 'end': .75}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def snapshot(base_url, parameters='{}'):
    profile = {'profile_id': 'vad', 'display_name': 'VAD', 'kind': 'vad',
               'provider': 'localai', 'capabilities': ['vad'], 'base_url': base_url,
               'model': 'silero-alias', 'parameters': json.loads(parameters)}
    return ProfileSnapshot(json.dumps(profile), None, 'snapshot')


def make_wav(path, seconds):
    with wave.open(str(path), 'wb') as out:
        out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        out.writeframes(b'\x00\x00' * 16000 * seconds)


def test_vad_chunks_float_pcm_and_restores_video_time(tmp_path, monkeypatch, vad_server):
    base_url, seen = vad_server
    monkeypatch.setattr(vad, 'CHUNK_SECONDS', 1)
    wav = tmp_path / 'audio.wav'
    make_wav(wav, 2)
    intervals = vad.detect_speech(str(wav), snapshot(base_url), lambda: False)
    assert intervals == [SpeechInterval(.25, 1.5)]
    assert len(seen) == 2
    assert all(path == '/v1/vad' and auth is None for path, auth, _ in seen)
    assert all(item['model'] == 'silero-alias' and len(item['audio']) == 20000
               and all(type(sample) is float for sample in item['audio'])
               for _, _, item in seen)


def test_localai_vad_server_side_overrides_are_rejected_before_request(tmp_path):
    wav = tmp_path / 'audio.wav'
    make_wav(wav, 1)
    profile = snapshot('http://127.0.0.1:1/v1', '{"threshold":0.7}')
    with pytest.raises(ValueError, match='服務端.*模型別名'):
        vad.detect_speech(str(wav), profile)


def test_vad_overlap_stitches_speech_across_chunk_boundary(tmp_path):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(payload)
            segment = ({'start': 29.8, 'end': 30.25} if len(seen) == 1
                       else {'start': .05, 'end': .5})
            body = json.dumps({'segments': [segment]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wav = tmp_path / 'long.wav'
        make_wav(wav, 31)
        profile = snapshot(f'http://127.0.0.1:{server.server_port}/v1')
        plan = vad.plan_speech_chunks(str(wav))
        assert [(chunk.index, chunk.core_start_frame, chunk.core_end_frame,
                 chunk.start_frame, chunk.end_frame) for chunk in plan] == [
            (0, 0, 480000, 0, 484000), (1, 480000, 496000, 476000, 496000)]
        partials = [interval for chunk in plan
                    for interval in vad.detect_speech_chunk(str(wav), profile, chunk, lambda: False)]
        assert vad.merge_speech_intervals(partials) == [SpeechInterval(29.8, 30.25)]
        assert [len(call['audio']) for call in seen] == [484000, 20000]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('interval', [
    {'start': -1, 'end': 1}, {'start': 0, 'end': 0},
    {'start': 0, 'end': float('inf')}, {'start': True, 'end': 1},
])
def test_vad_rejects_invalid_second_intervals(interval):
    with pytest.raises(ValueError):
        vad._validate_intervals({'segments': [interval]}, 2)


def test_cancellation_terminates_pending_vad_request(tmp_path):
    received, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            received.set()
            release.wait(5)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"segments":[]}')
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wav = tmp_path / 'audio.wav'
    make_wav(wav, 1)
    profile = snapshot(f'http://127.0.0.1:{server.server_port}')
    failures = []
    def run():
        try:
            vad.detect_speech(str(wav), profile, cancelled.is_set)
        except Exception as exc:
            failures.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert received.wait(3)
        cancelled.set()
        worker.join(3)
        assert not worker.is_alive()
        from uav_downloader.subtitles.engine import SubtitleCancelled
        assert len(failures) == 1 and isinstance(failures[0], SubtitleCancelled)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(('status', 'retryable'), [(429, True), (503, True), (401, False)])
def test_vad_stage_classifies_http_failures_without_provider_body(tmp_path, status, retryable):
    from uav_downloader.subtitles.models import StageRequestError
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body = b'{"error":"private-provider-body"}'
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wav = tmp_path / 'audio.wav'
        make_wav(wav, 1)
        profile = snapshot(f'http://127.0.0.1:{server.server_port}/v1')
        chunk = vad.plan_speech_chunks(str(wav))[0]
        with pytest.raises(StageRequestError) as error:
            vad.detect_speech_chunk(str(wav), profile, chunk)
        assert error.value.retryable is retryable
        assert 'private-provider-body' not in str(error.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_vad_stage_connection_failure_is_retryable(tmp_path):
    from uav_downloader.subtitles.models import StageRequestError
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    wav = tmp_path / 'audio.wav'
    make_wav(wav, 1)
    profile = snapshot(f'http://127.0.0.1:{port}/v1')
    chunk = vad.plan_speech_chunks(str(wav))[0]
    with pytest.raises(StageRequestError) as error:
        vad.detect_speech_chunk(str(wav), profile, chunk)
    assert error.value.retryable is True
    assert '127.0.0.1' not in str(error.value)


def test_vad_invalid_segments_schema_is_permanent(tmp_path):
    from uav_downloader.subtitles.models import StageRequestError
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body = b'{"segments":"not-a-list"}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wav = tmp_path / 'audio.wav'
        make_wav(wav, 1)
        profile = snapshot(f'http://127.0.0.1:{server.server_port}/v1')
        chunk = vad.plan_speech_chunks(str(wav))[0]
        with pytest.raises(StageRequestError) as error:
            vad.detect_speech_chunk(str(wav), profile, chunk)
        assert error.value.retryable is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
