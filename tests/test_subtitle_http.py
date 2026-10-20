import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from uav_downloader.daemon.server import DaemonServer
from uav_downloader.daemon.task_manager import TaskManager
from uav_downloader.subtitles import profiles


@pytest.fixture
def subtitle_http(monkeypatch, tmp_path):
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('DOWNLOAD_DIR', str(tmp_path / 'videos'))
    monkeypatch.setattr(TaskManager, 'start_dispatcher', lambda self: None)
    monkeypatch.setattr('uav_downloader.daemon.task_manager.nas.load_settings',
                        lambda **kw: {'enabled': False})
    server = DaemonServer(host='127.0.0.1', port=0, auth_token='daemon-secret')
    server.start()
    base = f'http://127.0.0.1:{server.server.server_address[1]}'

    def request(path, method='GET', data=None, content_type='application/json', raw=None, declared_length=None):
        headers = {'Authorization': 'Bearer daemon-secret'}
        if content_type is not None:
            headers['Content-Type'] = content_type
        body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        req = Request(base + path, data=body, headers=headers, method=method)
        if declared_length is not None:
            req.add_header('Content-Length', str(declared_length))
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    yield request, server, tmp_path
    server.stop()


def profile_payload(**changes):
    return {
        'profile_id': 'text-main', 'display_name': '文字服務', 'kind': 'text',
        'provider': 'lmstudio', 'capabilities': ['correction', 'translation'],
        'base_url': 'http://127.0.0.1:1234', 'model': 'local-model',
        'parameters': {'temperature': 0.2}, **changes,
    }


def test_profiles_api_saves_profiles_without_returning_credentials(subtitle_http):
    request, _, _ = subtitle_http
    profile = profile_payload(api_key='secret-profile-token')
    status, saved = request('/api/subtitles/profiles', 'PUT', {'profiles': [profile]})
    assert status == 200
    assert 'secret-profile-token' not in json.dumps(saved)
    assert saved['profiles'][0]['credential_configured'] is True
    status, loaded = request('/api/subtitles/profiles')
    assert status == 200
    assert loaded['profiles'][0]['profile_id'] == 'text-main'
    assert loaded['profiles'][0]['parameters'] == {'temperature': 0.2}
    assert 'secret-profile-token' not in json.dumps(loaded)


def test_profiles_api_rejects_unknown_fields_and_unsupported_parameters_before_save(subtitle_http):
    request, _, _ = subtitle_http
    status, error = request('/api/subtitles/profiles', 'PUT', {
        'profiles': [profile_payload(parameters={'headers': {'Authorization': 'secret'}})]})
    assert status == 400
    assert 'secret' not in json.dumps(error)
    assert profiles.load_profiles() == []
    status, _ = request('/api/subtitles/profiles', 'PUT', {
        'profiles': [profile_payload(extra='ignored')]})
    assert status == 400
    status, _ = request('/api/subtitles/profiles', 'PUT', {
        'profiles': [profile_payload(parameters={'temperature': 0.2, 'extra_option': 'x'})]})
    assert status == 400
    assert profiles.load_profiles() == []


def test_subtitle_post_requires_json_and_rejects_non_object_or_oversized_bodies(subtitle_http, monkeypatch):
    request, server, _ = subtitle_http
    server.subtitle_task_manager.submit = lambda *a, **kw: pytest.fail('No action should run')
    status, _ = request('/api/subtitles/tasks', 'POST', raw=b'{"items": []}',
                        content_type='text/plain')
    assert status == 415
    status, _ = request('/api/subtitles/tasks', 'POST', raw=b'[]')
    assert status == 400
    status, _ = request('/api/subtitles/tasks', 'POST', raw=b'', declared_length=1024 * 1024 + 1)
    assert status == 413


def test_download_subtitle_selection_is_validated_before_download_submission(subtitle_http):
    request, server, _ = subtitle_http
    called = []
    server.task_manager.add_task = lambda **kwargs: called.append(kwargs) or SimpleNamespace(
        to_dict=lambda: {'task_id': 'download-1'})
    for selection in [
        {'selected_stages': [{'bad': 'stage'}], 'profile_ids': {}},
        {'selected_stages': ['translation'], 'profile_ids': {'translation': 'missing'},
         'target_languages': ['fr']},
        {'selected_stages': ['translation'], 'profile_ids': {'translation': 'missing'},
         'target_languages': []},
    ]:
        status, _ = request('/api/download/start', 'POST', {
            'url': 'https://missav.ai/fixture', 'subtitle_request': selection})
        assert status == 400
    assert called == []


def test_profile_test_requires_external_call_confirmation_and_redacts_errors(subtitle_http, monkeypatch):
    from uav_downloader.daemon import subtitle_http as api

    request, _, _ = subtitle_http
    profiles.save_profile(profiles.StageProfile(
        profile_id='test-vad', display_name='VAD', kind='vad', provider='localai',
        capabilities=('vad',), base_url='http://127.0.0.1:1234', model='vad-alias',
        parameters_json='{}'))
    called = []
    monkeypatch.setattr(api, '_test_profile', lambda *args, **kwargs: called.append(args) or {
        'success': True, 'checked': 'external_request'})
    status, _ = request('/api/subtitles/profiles/test', 'POST', {
        'profile_id': 'test-vad', 'stage': 'vad'})
    assert status == 400
    assert called == []
    status, result = request('/api/subtitles/profiles/test', 'POST', {
        'profile_id': 'test-vad', 'stage': 'vad', 'confirm_external_call': True})
    assert status == 200
    assert result['success'] and result['checked'] == 'external_request'
    monkeypatch.setattr(api, '_test_profile', lambda *args: (_ for _ in ()).throw(
        RuntimeError('Authorization: secret-provider-value')))
    status, error = request('/api/subtitles/profiles/test', 'POST', {
        'profile_id': 'test-vad', 'stage': 'vad', 'confirm_external_call': True})
    assert status == 200
    assert error['success'] is False
    assert error['format_pass'] is None
    assert error['reasoning_control'] == 'unknown'
    assert error['quality'] == 'unverified'
    assert 'secret-provider-value' not in json.dumps(error)


def test_model_discovery_failure_keeps_manual_model_entry_available(subtitle_http, monkeypatch):
    from uav_downloader.daemon import subtitle_http as api

    request, _, _ = subtitle_http
    profiles.save_profile(profiles.StageProfile(
        profile_id='text-main', display_name='文字服務', kind='text', provider='lmstudio',
        capabilities=('correction', 'translation'), base_url='http://127.0.0.1:1234',
        model='typed-model', parameters_json='{}'))
    monkeypatch.setattr(api, '_discover_models', lambda profile: {
        'available': False, 'models': [], 'message': '模型清單目前無法載入，仍可手動輸入模型名稱'})
    status, result = request('/api/subtitles/profiles/test', 'POST', {
        'action': 'discover_models', 'profile_id': 'text-main', 'confirm_external_call': True})
    assert status == 200
    assert result['available'] is False
    assert result['models'] == []
    assert result['manual_entry_allowed'] is True


def test_lmstudio_model_discovery_uses_documented_v0_catalog_shape():
    from uav_downloader.daemon import subtitle_http as api

    requested = []

    class CatalogHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            body = json.dumps({
                'object': 'list',
                'data': [
                    {'id': 'qwen-local', 'type': 'llm'},
                    {'id': 'nomic-embed', 'type': 'embeddings'},
                ],
            }).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    fixture = ThreadingHTTPServer(('127.0.0.1', 0), CatalogHandler)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    profile = profiles.StageProfile(
        profile_id='lmstudio-catalog', display_name='LM Studio', kind='text',
        provider='lmstudio', capabilities=('correction',),
        base_url=f'http://127.0.0.1:{fixture.server_address[1]}', model=None,
        parameters_json='{}')
    try:
        result = api._discover_models(profile)
    finally:
        fixture.shutdown()
        fixture.server_close()
        thread.join(timeout=2)

    assert requested == ['/api/v0/models']
    assert result == {'available': True, 'models': ['qwen-local'],
                      'manual_entry_allowed': True}


def test_unknown_reasoning_control_requires_separate_explicit_permission(subtitle_http, monkeypatch):
    from uav_downloader.daemon import subtitle_http as api

    request, _, _ = subtitle_http
    profiles.save_profile(profiles.StageProfile(
        profile_id='localai-text', display_name='LocalAI', kind='text', provider='localai',
        capabilities=('correction',), base_url='http://127.0.0.1:1234/v1',
        model='text-model', parameters_json='{}'))
    calls = []
    monkeypatch.setattr(api, '_test_profile', lambda *args, **kwargs: calls.append(args) or {
        'success': True, 'format_pass': True, 'quality': 'unverified'})
    status, result = request('/api/subtitles/profiles/test', 'POST', {
        'profile_id': 'localai-text', 'stage': 'correction', 'confirm_external_call': True})
    assert status == 200
    assert result['test_status'] == 'not_run'
    assert result['reasoning_control'] == 'unknown'
    assert calls == []
    status, result = request('/api/subtitles/profiles/test', 'POST', {
        'profile_id': 'localai-text', 'stage': 'correction',
        'confirm_external_call': True, 'allow_reasoning': True})
    assert status == 200
    assert result['success'] is True
    assert result['reasoning_control'] == 'unknown'
    assert calls


def test_video_listing_returns_safe_paths_and_local_vad_capability(subtitle_http):
    request, server, _ = subtitle_http
    server.subtitle_file_manager.list_videos = lambda **kw: [{
        'video_id': 'source-v1.abc', 'name': 'clip.mp4', 'title': 'clip', 'source': 'configured'}]
    status, result = request('/api/subtitles/videos')
    assert status == 200
    assert result['videos'][0]['video_id'] == 'source-v1.abc'
    assert 'path' not in json.dumps(result)
    vad = result['capabilities']['local_vad']
    assert vad['available'] is False
    assert 'Windows' in vad['reason']


def test_download_start_snapshots_selected_profiles_without_changing_legacy_subtitles(subtitle_http):
    request, server, _ = subtitle_http
    profile = profiles.StageProfile(
        profile_id='asr-one', display_name='ASR', kind='asr', provider='localai',
        capabilities=('asr',), base_url='http://127.0.0.1:1234/v1', model='whisper',
        parameters_json='{}')
    profiles.save_profile(profile, api_key='private-download-token')
    status, response = request('/api/download/start', 'POST', {
        'url': 'https://missav.ai/fixture', 'enable_subtitles': False,
        'subtitle_request': {
            'selected_stages': ['asr'], 'profile_ids': {'asr': 'asr-one'},
            'target_languages': [], 'output_languages': ['ja']}})
    assert status == 200
    task = server.task_manager.get_task(response['task']['task_id'])
    assert task.subtitle_mode == 'none'
    assert task.subtitle_request['profile_snapshots']['asr']['snapshot_id']
    assert 'private-download-token' not in json.dumps(task.subtitle_request)


def test_task_batch_reports_each_video_and_result_retry_is_storage_only(subtitle_http, monkeypatch):
    request, server, _ = subtitle_http
    profiles.save_profile(profiles.StageProfile(
        profile_id='asr-one', display_name='ASR', kind='asr', provider='localai',
        capabilities=('asr',), base_url='http://127.0.0.1:1234/v1', model='whisper',
        parameters_json='{}'))
    server.subtitle_file_manager.register_history = lambda video_id: (
        SimpleNamespace(video_id=video_id) if video_id == 'good' else
        (_ for _ in ()).throw(ValueError('字幕影片不在下載紀錄或允許目錄中')))
    submitted = []
    server.subtitle_task_manager.submit = lambda video_id, *args, **kwargs: (
        submitted.append(video_id) or {'task_id': 'subtitle_' + video_id, 'status': 'queued'})
    status, result = request('/api/subtitles/tasks', 'POST', {'items': [
        {'video_id': 'good', 'selected_stages': ['asr'], 'profile_ids': {'asr': 'asr-one'},
         'target_languages': [], 'output_languages': ['ja']},
        {'video_id': 'bad', 'selected_stages': ['asr'], 'profile_ids': {'asr': 'asr-one'},
         'target_languages': [], 'output_languages': ['ja']}]})
    assert status == 200
    assert [item['success'] for item in result['items']] == [True, False]
    assert result['items'][1]['error']
    assert submitted == ['good']
    retry_calls = []
    server.subtitle_task_manager.get_publication = lambda publication_id: {
        'publication_id': publication_id, 'video_id': 'good', 'purpose': 'restore',
        'publish_status': 'failed'}
    server.subtitle_task_manager.retry_restored_publish = lambda publication_id: (
        retry_calls.append(publication_id) or {'publication_id': publication_id,
            'restore_status': 'restored', 'publish_status': 'published'})
    status, result = request('/api/subtitles/videos/good/results/subtitle_pub_0123456789abcdef0123456789abcdef/action',
                             'POST', {'action': 'retry_publish'})
    assert status == 200
    assert result['publish_status'] == 'published'
    assert retry_calls == ['subtitle_pub_0123456789abcdef0123456789abcdef']


def test_batch_item_failure_does_not_leak_unvalidated_video_ids_or_abort_neighbors(subtitle_http):
    request, server, _ = subtitle_http
    secret_sentinel = 'invalid-secret-' + 'x' * 600
    server.subtitle_file_manager.register_history = lambda _video_id: (_ for _ in ()).throw(
        OSError('storage temporarily unavailable'))
    profiles.save_profile(profiles.StageProfile(
        profile_id='asr-one', display_name='ASR', kind='asr', provider='localai',
        capabilities=('asr',), base_url='http://127.0.0.1:1234/v1', model='whisper',
        parameters_json='{}'))
    status, result = request('/api/subtitles/tasks', 'POST', {'items': [
        {'video_id': secret_sentinel, 'selected_stages': ['asr'],
         'profile_ids': {'asr': 'asr-one'}, 'target_languages': [], 'output_languages': []},
        {'video_id': 'second-item', 'selected_stages': ['asr'],
         'profile_ids': {'asr': 'asr-one'}, 'target_languages': [], 'output_languages': []},
    ]})
    assert status == 200
    assert len(result['items']) == 2
    assert [item['success'] for item in result['items']] == [False, False]
    assert result['items'][0]['video_id'] is None
    assert secret_sentinel not in json.dumps(result)


def test_result_details_preserve_snapshot_versions_and_render_selected_content(subtitle_http):
    from uav_downloader.subtitles import results
    from uav_downloader.subtitles.models import SpeechInterval, SubtitleSegment

    request, _, _ = subtitle_http
    vad_profile = profiles.StageProfile(
        profile_id='vad-one', display_name='VAD 版本', kind='vad', provider='localai',
        capabilities=('vad',), base_url='http://127.0.0.1:1234/v1', model='vad-model',
        parameters_json='{}')
    asr_profile = profiles.StageProfile(
        profile_id='asr-one', display_name='ASR 版本', kind='asr', provider='localai',
        capabilities=('asr',), base_url='http://127.0.0.1:1234/v1', model='asr-model',
        parameters_json='{}')
    profiles.save_profile(vad_profile, api_key='result-secret')
    profiles.save_profile(asr_profile)
    video_id = 'source-v1:result-fixture'
    vad_snapshot = profiles.snapshot('vad-one', 'vad')
    asr_snapshot = profiles.snapshot('asr-one', 'asr')
    vad_result = results.commit_success(results.StageResult(
        result_id='vad-result-1', video_id=video_id, stage='vad', language=None,
        input_result_ids=(), profile_snapshot=vad_snapshot,
        intervals=(SpeechInterval(0.1, 0.8),), segments=None))
    asr_result = results.commit_success(results.StageResult(
        result_id='asr-result-1', video_id=video_id, stage='asr', language='ja',
        input_result_ids=(vad_result.result_id,), profile_snapshot=asr_snapshot,
        intervals=None, segments=(SubtitleSegment('s1', 0.1, 0.8, 'こんにちは'),)))
    status, listing = request(f'/api/subtitles/videos/{video_id}/results')
    assert status == 200
    assert listing['results'][0]['profile_snapshot']['profile']['model'] == 'asr-model'
    assert listing['results'][0]['stale'] is False
    assert 'result-secret' not in json.dumps(listing)
    status, detail = request(f'/api/subtitles/videos/{video_id}/results/{asr_result.result_id}')
    assert status == 200
    assert detail['segments'][0]['text'] == 'こんにちは'
    assert detail['inputs'][0]['stage'] == 'vad'
    status, content = request(f'/api/subtitles/videos/{video_id}/results/{asr_result.result_id}/content')
    assert status == 200
    assert content['content_type'] == 'application/x-subrip'
    assert 'こんにちは' in content['content']
    status, content = request(f'/api/subtitles/videos/{video_id}/results/{vad_result.result_id}/content')
    assert status == 200
    assert content['content_type'] == 'application/json'
    assert content['content'] == [{'start': 0.1, 'end': 0.8}]


def test_result_publish_requires_explicit_replace_choice_and_persisted_retry_is_storage_only(subtitle_http, monkeypatch):
    from uav_downloader.subtitles import results
    from uav_downloader.subtitles.models import SubtitleSegment

    request, server, _ = subtitle_http
    profile = profiles.StageProfile(
        profile_id='asr-one', display_name='ASR', kind='asr', provider='localai',
        capabilities=('asr',), base_url='http://127.0.0.1:1234/v1', model='asr-model',
        parameters_json='{}')
    profiles.save_profile(profile)
    video_id, result_id = 'source-v1:publish-fixture', 'asr-publish-1'
    item = results.commit_success(results.StageResult(
        result_id=result_id, video_id=video_id, stage='asr', language='ja',
        input_result_ids=(), profile_snapshot=profiles.snapshot('asr-one', 'asr'),
        intervals=None, segments=(SubtitleSegment('s1', 0.1, 0.8, 'test'),)))
    calls = []
    monkeypatch.setattr(server.subtitle_file_manager, 'publish_result',
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError('storage offline')))
    path = f'/api/subtitles/videos/{video_id}/results/{item.result_id}/action'
    status, _ = request(path, 'POST', {'action': 'publish', 'output_language': 'ja'})
    assert status == 400
    assert calls == []
    status, mismatch = request(path, 'POST', {'action': 'publish', 'output_language': 'en',
                                               'replace_existing': False})
    assert status == 409
    assert calls == []
    status, published = request(path, 'POST', {'action': 'publish', 'output_language': 'ja',
                                                'replace_existing': False})
    assert status == 200
    assert published['publish_status'] == 'failed'
    assert published['restore_status'] == 'not_requested'
    assert published['publication_id'].startswith('subtitle_pub_')
    publication_id = published['publication_id']
    monkeypatch.setattr(server.subtitle_file_manager, 'publish_result',
                        lambda *args, **kwargs: calls.append((args, kwargs)) or {
                            'path': 'fixture.ja.srt', 'publish_status': 'published'})
    retry_path = f'/api/subtitles/videos/{video_id}/results/{publication_id}/action'
    status, retried = request(retry_path, 'POST', {'action': 'retry_publish'})
    assert status == 200
    assert retried['publish_status'] == 'published'
    assert retried['restore_status'] == 'not_requested'
    assert calls[0][1]['replace_existing'] is False


def test_result_restore_selects_vad_and_asr_without_implicit_publication(subtitle_http):
    from uav_downloader.subtitles import results
    from uav_downloader.subtitles.models import SpeechInterval, SubtitleSegment

    request, server, _ = subtitle_http
    vad_profile = profiles.StageProfile(
        profile_id='vad-restore', display_name='VAD', kind='vad', provider='localai',
        capabilities=('vad',), base_url='http://127.0.0.1:1234/v1', model='vad',
        parameters_json='{}')
    asr_profile = profiles.StageProfile(
        profile_id='asr-restore', display_name='ASR', kind='asr', provider='localai',
        capabilities=('asr',), base_url='http://127.0.0.1:1234/v1', model='asr',
        parameters_json='{}')
    profiles.save_profile(vad_profile)
    profiles.save_profile(asr_profile)
    video_id = 'source-v1:restore-contract'
    prior = {}
    for stage, profile_id, language in [('vad', 'vad-restore', None), ('asr', 'asr-restore', 'ja')]:
        snapshot = profiles.snapshot(profile_id, stage)
        def commit(result_id):
            return results.commit_success(results.StageResult(
                result_id=result_id, video_id=video_id, stage=stage, language=language,
                input_result_ids=(), profile_snapshot=snapshot,
                intervals=(SpeechInterval(0.1, 0.4),) if stage == 'vad' else None,
                segments=(SubtitleSegment(result_id, 0.1, 0.4, '語句'),) if stage == 'asr' else None))
        prior[stage] = commit(stage + '-prior')
        commit(stage + '-latest')

    for stage in ('vad', 'asr'):
        language = None if stage == 'vad' else 'ja'
        status, restored = request(
            f'/api/subtitles/videos/{video_id}/results/{prior[stage].result_id}/action', 'POST',
            {'action': 'restore', 'stage': stage, 'language': language})
        assert status == 200
        assert restored['result_id'] == prior[stage].result_id
        assert restored['restore_status'] == 'restored'
        assert restored['publish_status'] == 'not_requested'
        assert restored['publication_id'] is None
        assert restored['language'] is None
    assert server.subtitle_task_manager.list_publications() == []


def test_profile_probe_exercises_vad_asr_and_native_text_routes(subtitle_http):
    seen = []

    class FixtureHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length)
            seen.append((self.path, body))
            if self.path == '/v1/vad':
                payload = {'segments': []}
            elif self.path == '/v1/audio/transcriptions':
                payload = {'segments': [{'start': 0.0, 'end': 0.25, 'text': 'test'}]}
            elif self.path == '/api/v1/chat':
                payload = {'model_instance_id': 'fixture-model',
                           'output': [{'type': 'message', 'content': '[{"id":"probe-1","text":"test"}]'}]}
            else:
                self.send_error(404)
                return
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    fixture = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{fixture.server_address[1]}'
    try:
        request, _, _ = subtitle_http
        profiles.save_profile(profiles.StageProfile(
            profile_id='probe-vad', display_name='VAD', kind='vad', provider='localai',
            capabilities=('vad',), base_url=base, model='vad-alias', parameters_json='{}'))
        profiles.save_profile(profiles.StageProfile(
            profile_id='probe-asr', display_name='ASR', kind='asr', provider='localai',
            capabilities=('asr',), base_url=base, model='asr-model', parameters_json='{}'))
        profiles.save_profile(profiles.StageProfile(
            profile_id='probe-text', display_name='文字', kind='text', provider='lmstudio',
            capabilities=('correction',), base_url=base, model='text-model', parameters_json='{}'))
        for profile_id, stage in [('probe-vad', 'vad'), ('probe-asr', 'asr'),
                                  ('probe-text', 'correction')]:
            status, result = request('/api/subtitles/profiles/test', 'POST', {
                'profile_id': profile_id, 'stage': stage, 'confirm_external_call': True})
            assert status == 200
            assert result['success'] and result['format_pass'] is True
            assert result['quality'] == 'unverified'
            assert result['reasoning_control'] == ('not_applicable' if stage in ('vad', 'asr') else 'supported')
        status, allowed = request('/api/subtitles/profiles/test', 'POST', {
            'profile_id': 'probe-text', 'stage': 'correction',
            'confirm_external_call': True, 'allow_reasoning': True})
        assert status == 200 and allowed['success']
        assert allowed['reasoning_control'] == 'unknown'
        assert [path for path, _ in seen] == [
            '/v1/vad', '/v1/audio/transcriptions', '/api/v1/chat', '/api/v1/chat']
    finally:
        fixture.shutdown()
        fixture.server_close()
        thread.join(timeout=2)

