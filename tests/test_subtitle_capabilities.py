import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest

from uav_downloader.subtitles import engine, llm
from uav_downloader.subtitles.profiles import ProfileSnapshot, capability_report
from uav_downloader.subtitles.models import SubtitleSegment


def snapshot(provider, base_url='http://127.0.0.1:1234'):
    profile = {'profile_id': 'text', 'display_name': 'Text', 'kind': 'text',
               'provider': provider, 'capabilities': ['correction', 'translation'],
               'base_url': base_url, 'model': 'instruct-model', 'parameters': {}}
    return ProfileSnapshot(json.dumps(profile), None, 'snapshot')


def test_capability_report_does_not_treat_openai_compatibility_as_all_stages():
    assert llm.profile_capabilities(snapshot('openai-compatible')) == ('correction', 'translation')
    assert llm.profile_capabilities(snapshot('openai-compatible', 'http://host')) == ('correction', 'translation')
    assert capability_report(snapshot('openai-compatible'))['reasoning_disable'] == 'unknown'


def test_localai_vad_parameters_are_reported_as_server_side_only():
    profile = json.loads(snapshot('localai').profile_json)
    profile.update(kind='vad', capabilities=['vad'], parameters={'threshold': 0.4})
    report = capability_report(ProfileSnapshot(json.dumps(profile), None, 'snapshot'))
    assert report['stages'] == ('vad',)
    assert report['vad_parameters'] == 'service_side_model_configuration'


def test_localai_reasoning_capability_reports_template_control_limit():
    profile = json.loads(snapshot('localai').profile_json)
    report = capability_report(ProfileSnapshot(json.dumps(profile), None, 'snapshot'))
    assert report['reasoning_disable'] == 'documented_request_controls_without_machine_confirmation'


@pytest.mark.parametrize('provider', ['localai', 'openai-compatible'])
def test_unknown_reasoning_control_blocks_required_disable_before_network(provider, monkeypatch):
    monkeypatch.setattr(llm.requests, 'Session', lambda: pytest.fail('unknown must block before network'))
    with pytest.raises(llm.LlmTranslationError, match='未確認'):
        llm.transform_segments('correction', [SubtitleSegment('a', 1, 2, '文字')],
                               snapshot(provider), require_reasoning_disabled=True)


def test_lmstudio_native_request_confirms_reasoning_off_and_bypasses_environment_proxy(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    monkeypatch.setenv('no_proxy', '')
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            calls.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            body = json.dumps({'model_instance_id': 'instruct-model', 'output': [
                {'type': 'message', 'content': '[{"id":"a","text":"修正文字"}]'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        profile = snapshot('lmstudio', f'http://127.0.0.1:{server.server_port}')
        original = SubtitleSegment('a', 1.25, 3.5, '原始文字')
        result = llm.transform_segments('correction', [original], profile)
        assert result == [SubtitleSegment('a', 1.25, 3.5, '修正文字')]
        assert calls[0][0] == '/api/v1/chat'
        assert calls[0][1]['reasoning'] == 'off'
        assert calls[0][1]['model'] == 'instruct-model'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('text', ['含有\x00NUL', '含有\x1b控制字元', 'x' * 16385])
def test_transform_rejects_returned_text_outside_subtitle_contract(text):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body = json.dumps({'model_instance_id': 'instruct-model', 'output': [
                {'type': 'message', 'content': json.dumps([{'id': 'a', 'text': text}])}
            ]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        profile = snapshot('lmstudio', f'http://127.0.0.1:{server.server_port}')
        with pytest.raises(llm.LlmTranslationError, match='回傳'):
            llm.transform_segments('correction', [SubtitleSegment('a', 1, 2, '原文')], profile)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_transform_allows_normal_newline_and_punctuation_in_returned_text():
    assert llm._valid_subtitle_text('第一行，。\n第二行！')


def test_ollama_requires_thinking_capability_metadata_then_sends_think_false():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, body))
            payload = ({'thinking': {'values': [True, False], 'default': True}}
                       if self.path == '/api/show'
                       else {'message': {'content': '[{"id":"a","text":"已翻譯"}]'}})
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        profile = snapshot('ollama', f'http://127.0.0.1:{server.server_port}')
        result = llm.transform_segments('translation', [SubtitleSegment('a', 1, 2, 'hello')],
                                        profile, '繁體中文')
        assert result == [SubtitleSegment('a', 1, 2, '已翻譯')]
        assert [call[0] for call in calls] == ['/api/show', '/api/chat']
        assert calls[0][1] == {'model': 'instruct-model'}
        assert calls[1][1]['think'] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_localai_documented_thinking_controls_are_explicit_payload_fields():
    _, payload = llm._stage_payload('localai', 'model-alias', '', {}, 'text', 'system', True)
    assert payload['reasoning_effort'] == 'none'
    assert payload['metadata'] == {'enable_thinking': 'false'}


def test_ollama_missing_false_value_in_model_metadata_blocks_before_chat():
    paths = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            paths.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            body = json.dumps({'thinking': {'values': ['low', 'high'], 'default': 'high'}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        profile = snapshot('ollama', f'http://127.0.0.1:{server.server_port}')
        with pytest.raises(llm.LlmTranslationError, match='未確認'):
            llm.transform_segments('correction', [SubtitleSegment('a', 1, 2, 'text')], profile)
        assert paths == ['/api/show']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_transform_batches_by_count_and_output_budget():
    segments = [SubtitleSegment(str(index), index, index + 1, 'x') for index in range(41)]
    batches = llm._segment_batches(segments, {})
    assert [len(batch) for batch in batches] == [20, 20, 1]
    constrained = llm._segment_batches(segments[:3], {'max_output_tokens': 4})
    assert [len(batch) for batch in constrained] == [2, 1]
    with pytest.raises(llm.LlmTranslationError, match='輸出預算'):
        llm._segment_batches([SubtitleSegment('long', 0, 1, '12345')],
                             {'max_output_tokens': 4})
    planned = llm.plan_transform_batches(segments, snapshot('lmstudio'))
    assert [len(batch) for batch in planned] == [20, 20, 1]
    assert all(isinstance(batch, tuple) for batch in planned)


def test_unknown_model_parameter_is_rejected_before_request(monkeypatch):
    profile = json.loads(snapshot('lmstudio').profile_json)
    profile['parameters'] = {'unknown_sampling_switch': True}
    broken = ProfileSnapshot(json.dumps(profile), None, 'snapshot')
    monkeypatch.setattr(engine, '_run_process', lambda *args, **kwargs: pytest.fail('request must not run'))
    with pytest.raises(llm.LlmTranslationError, match='模型參數不受支援'):
        llm.transform_segments('correction', [SubtitleSegment('a', 0, 1, 'text')], broken)


def test_transform_cancels_while_http_request_is_waiting_for_response():
    received = threading.Event()
    release = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            received.set()
            release.wait(5)
            body = b'{"model_instance_id":"model","output":[]}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        profile = snapshot('lmstudio', f'http://127.0.0.1:{server.server_port}')
        with pytest.raises(engine.SubtitleCancelled):
            llm.transform_segments('correction', [SubtitleSegment('a', 1, 2, 'text')],
                                   profile, cancel_check=lambda: received.is_set()
                                   and time.monotonic() - started > 0.25)
        assert received.is_set(), 'server accepted request before cancellation'
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()
