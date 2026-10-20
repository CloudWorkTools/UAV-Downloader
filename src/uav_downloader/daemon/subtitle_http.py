"""HTTP boundary for subtitle profiles, tasks, and stored results."""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from urllib.parse import unquote

from uav_downloader.subtitles import profiles, results


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def _obj(value, label='請求格式無效'):
    if not isinstance(value, dict):
        raise ApiError(400, label)
    return value


def _keys(value, allowed, label='請求包含不支援的欄位'):
    if set(value) - set(allowed):
        raise ApiError(400, label)


def _id(value, pattern=r'[A-Za-z0-9_.:-]{1,512}', label='識別碼格式無效'):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ApiError(400, label)
    return value


def _public_profile(profile):
    value = asdict(profile)
    value['parameters'] = json.loads(value.pop('parameters_json'))
    value.pop('credential_ref', None)
    value['credential_configured'] = bool(profile.credential_ref)
    return value


def _profile_from_payload(value):
    _obj(value, '模型設定檔格式無效')
    _keys(value, {'profile_id', 'display_name', 'kind', 'provider', 'capabilities',
                  'base_url', 'model', 'parameters', 'api_key'}, '模型設定檔包含不支援的欄位')
    parameters = value.get('parameters', {})
    if not isinstance(parameters, dict):
        raise ApiError(400, '模型參數必須是 JSON 物件')
    kind, provider = value.get('kind'), value.get('provider')
    try:
        profiles.validate_stage_parameters(kind, provider, parameters)
        profile = profiles.StageProfile(
            profile_id=value.get('profile_id'), display_name=value.get('display_name'),
            kind=kind, provider=provider,
            capabilities=tuple(value.get('capabilities', ())), base_url=value.get('base_url', ''),
            model=value.get('model'), parameters_json=json.dumps(parameters, ensure_ascii=False,
                                                                  allow_nan=False))
        # Validate structural fields before any persistent writes.
        profiles._validate_profile(profile)
    except Exception:
        raise ApiError(400, '模型設定檔或參數格式無效') from None
    key = value.get('api_key')
    import re
    if key is not None and (not isinstance(key, str) or len(key) > 4096 or
                            re.search(r'[\x00-\x20\x7f]', key)):
        raise ApiError(400, 'API 金鑰格式無效')
    return profile, key


def _profiles_put(body):
    _keys(body, {'profiles'})
    entries = body.get('profiles')
    if not isinstance(entries, list) or len(entries) > 100:
        raise ApiError(400, '模型設定檔清單格式無效')
    parsed = [_profile_from_payload(item) for item in entries]
    ids = [profile.profile_id for profile, _ in parsed]
    if len(set(ids)) != len(ids):
        raise ApiError(400, '模型設定檔 ID 不可重複')
    saved = [profiles.save_profile(profile, api_key=key) for profile, key in parsed]
    return {'profiles': [_public_profile(profile) for profile in saved]}


def _discover_models(profile):
    """Make one bounded, non-redirecting model-list request; failure stays optional."""
    import requests
    from urllib.parse import urlsplit
    provider = profile.provider
    base = profile.base_url.rstrip('/')
    if provider in ('openai', 'openai-compatible', 'localai'):
        endpoint = base if urlsplit(base).path.rstrip('/').endswith('/v1') else base + '/v1'
        endpoint += '/models'
    elif provider == 'ollama':
        endpoint = base + '/api/tags'
    elif provider == 'lmstudio':
            # LM Studio documents the legacy REST model catalog as
            # {"object":"list","data":[{"id": ..., "type": ...}]}.
            # Keep this endpoint/schema explicit instead of guessing between
            # the native v1 catalog and OpenAI-compatible model-list shapes.
        endpoint = base + '/api/v0/models'
    elif provider == 'anthropic':
        endpoint = base + '/v1/models'
    elif provider == 'gemini':
        endpoint = base + '/v1beta/models'
    else:
        return {'available': False, 'models': [], 'message': '此模型服務不支援自動載入清單',
                'manual_entry_allowed': True}
    headers = {}
    key = profiles.resolve_credential(profile.credential_ref, profile.base_url)
    if key:
        if provider == 'anthropic':
            headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
        elif provider == 'gemini':
            headers['x-goog-api-key'] = key
        else:
            headers['Authorization'] = 'Bearer ' + key
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(endpoint, headers=headers, timeout=(3, 8), allow_redirects=False,
                                   stream=True)
            if response.status_code != 200:
                raise ValueError('model listing unavailable')
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 1024 * 1024:
                    raise ValueError('model listing too large')
                chunks.append(chunk)
            payload = json.loads(b''.join(chunks))
        if provider == 'lmstudio':
            entries = payload.get('data') if isinstance(payload, dict) else None
            if (not isinstance(payload, dict) or payload.get('object') != 'list'
                    or not isinstance(entries, list)):
                raise ValueError('unexpected LM Studio model catalog shape')
            names = [item.get('id') for item in entries
                     if isinstance(item, dict) and item.get('type') in (None, 'llm', 'vlm')
                     and isinstance(item.get('id'), str)]
        elif isinstance(payload, dict) and isinstance(payload.get('data'), list):
            names = [item.get('id') for item in payload['data'] if isinstance(item, dict)]
        elif isinstance(payload, dict) and isinstance(payload.get('models'), list):
            names = [item.get('name') for item in payload['models'] if isinstance(item, dict)]
        else:
            names = []
        cleaned = []
        for name in names:
            if isinstance(name, str) and name and len(name) <= 160:
                cleaned.append(name.removeprefix('models/'))
            if len(cleaned) >= 200:
                break
        return {'available': True, 'models': cleaned, 'manual_entry_allowed': True}
    except Exception:
        return {'available': False, 'models': [], 'message': '模型清單目前無法載入，仍可手動輸入模型名稱',
                'manual_entry_allowed': True}


def _test_profile(profile, stage, *, allow_reasoning=False):
    import tempfile
    import wave
    from array import array
    from uav_downloader.subtitles import asr, llm, vad
    from uav_downloader.subtitles.models import SpeechInterval, SubtitleSegment
    snapshot = profiles.snapshot(profile.profile_id, stage)
    with tempfile.NamedTemporaryFile(prefix='subtitle-probe-', suffix='.wav') as audio:
        with wave.open(audio.name, 'wb') as writer:
            writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(16000)
            writer.writeframes(array('h', [0] * 16000).tobytes())
        if stage == 'vad':
            vad.detect_speech(audio.name, snapshot)
        elif stage == 'asr':
            asr.transcribe_stage(audio.name, [SpeechInterval(0, 0.5)], snapshot, 'ja')
        else:
            operation = stage
            target = 'en' if stage == 'translation' else None
            llm.transform_segments(operation, [SubtitleSegment('probe-1', 0.0, 0.25, 'test')],
                                   snapshot, target, allow_reasoning=allow_reasoning)
    return {'success': True, 'stage': stage, 'checked': 'external_request',
            'format_pass': True, 'reasoning_control': 'unknown', 'quality': 'unverified'}


def _profile_test(body):
    _keys(body, {'action', 'profile_id', 'stage', 'confirm_external_call', 'allow_reasoning'})
    action = body.get('action')
    if action not in (None, 'test', 'discover_models'):
        raise ApiError(400, '不支援的模型設定檔測試操作')
    if body.get('confirm_external_call') is not True:
        raise ApiError(400, '此操作會連線至模型服務，請明確確認後再測試')
    profile_id = _id(body.get('profile_id'), r'[A-Za-z0-9_-]{1,80}', '模型設定檔 ID 格式無效')
    profile = next((item for item in profiles.load_profiles() if item.profile_id == profile_id), None)
    if profile is None:
        raise ApiError(404, '找不到字幕模型設定檔')
    if action == 'discover_models':
        report = _discover_models(profile)
        report['manual_entry_allowed'] = True
        return report
    stage = body.get('stage')
    if stage not in profile.capabilities:
        raise ApiError(400, '模型設定檔不支援指定階段')
    allow_reasoning = body.get('allow_reasoning', False)
    if not isinstance(allow_reasoning, bool):
        raise ApiError(400, '推理模式選項無效')
    if stage in ('correction', 'translation') and profile.provider not in ('lmstudio', 'ollama') and not allow_reasoning:
        return {'success': False, 'test_status': 'not_run', 'format_pass': None,
                'reasoning_control': 'unknown', 'quality': 'unverified',
                'message': '尚未確認此模型服務能停用額外思考；如要測試格式相容性，可明確允許模型思考。'}
    try:
        report = _test_profile(profile, stage, allow_reasoning=allow_reasoning)
        report['reasoning_control'] = (
            'not_applicable' if stage in ('vad', 'asr') else
            'unknown' if allow_reasoning else
            'supported' if profile.provider in ('lmstudio', 'ollama') else 'unknown')
        return report
    except Exception as exc:
        reason = 'unsupported' if ('思考' in str(exc) or 'reasoning control' in str(exc).casefold()
                                   or 'ignored reasoning' in str(exc).casefold()) else 'unknown'
        message = str(exc)
        format_pass = False if ('response format' in message.casefold()
                                or any(term in message for term in (
                                    '回應格式無效', '回傳格式無效', '必須回傳', '必須回傳合法',
                                    '回應 JSON 格式無效', '片段格式錯誤'))) else None
        return {'success': False, 'test_status': 'failed', 'format_pass': format_pass,
                'reasoning_control': reason, 'quality': 'unverified',
                'message': ('模型回應格式不符合要求。' if format_pass is False else
                            '模型服務測試未完成，請檢查服務設定與連線。')}


def _local_vad_capability():
    import sys
    reason = ('目前部署未提供獨立本機 VAD 執行環境；既有 Windows 引擎尚未接入獨立字幕佇列。'
              if sys.platform == 'win32' else
              '目前 Linux 部署未提供獨立本機 VAD 執行環境；既有引擎僅支援 Windows whisper-vad-speech-segments.exe。')
    return {'available': False, 'reason': reason + ' ASR 內建 VAD 不可單獨使用。'}


def _snapshot_selection(stage_list, ids):
    if (not isinstance(stage_list, list) or not stage_list or len(stage_list) > 4
            or any(not isinstance(stage, str) for stage in stage_list)
            or len(set(stage_list)) != len(stage_list)
            or any(stage not in profiles.STAGES for stage in stage_list)):
        raise ApiError(400, '請選擇有效的字幕階段')
    if not isinstance(ids, dict) or set(ids) - set(stage_list) - {'text'}:
        raise ApiError(400, '字幕設定檔選擇格式無效')
    snapshots = {}
    for stage in stage_list:
        key = stage if stage in ids else 'text'
        if key not in ids:
            raise ApiError(400, '缺少所選字幕階段的設定檔')
        profile_id = _id(ids[key], r'[A-Za-z0-9_-]{1,80}', '模型設定檔 ID 格式無效')
        try:
            snapshots[key] = profiles.snapshot(profile_id, stage)
        except ValueError:
            raise ApiError(400, '所選字幕設定檔無效或不支援此階段') from None
    return snapshots


def prepare_download_request(spec):
    """Validate and snapshot optional download-time subtitle work before queueing."""
    spec = _obj(spec, '字幕工作要求格式無效')
    _keys(spec, {'selected_stages', 'profile_ids', 'target_languages',
                 'output_languages', 'allow_reasoning', 'input_result_id'})
    stages = spec.get('selected_stages')
    snapshots = _snapshot_selection(stages, spec.get('profile_ids'))
    languages, outputs = spec.get('target_languages', []), spec.get('output_languages', [])
    if (not isinstance(languages, list) or len(languages) > 2
            or any(not isinstance(language, str) or language not in ('en', 'zh-TW') for language in languages)
            or len(set(languages)) != len(languages)):
        raise ApiError(400, '字幕翻譯語言清單無效')
    if 'translation' in stages and not languages:
        raise ApiError(400, '請指定字幕翻譯語言')
    if (not isinstance(outputs, list) or len(outputs) > 3
            or any(not isinstance(language, str) or language not in ('ja', 'en', 'zh-TW') for language in outputs)
            or len(set(outputs)) != len(outputs)):
        raise ApiError(400, '字幕輸出語言清單無效')
    reasoning = spec.get('allow_reasoning', False)
    if not isinstance(reasoning, bool):
        raise ApiError(400, '推理模式選項無效')
    input_result = spec.get('input_result_id')
    if input_result is not None:
        _id(input_result, r'[A-Za-z0-9_-]{1,128}', '字幕上游結果識別碼格式無效')
    return {'selected_stages': stages, 'profile_snapshots': snapshots,
            'target_languages': languages, 'output_languages': outputs,
            'allow_reasoning': reasoning,
            **({'input_result_id': input_result} if input_result is not None else {})}


def _submission(body, files, manager):
    _keys(body, {'video_id', 'selected_stages', 'profile_ids', 'target_languages',
                 'output_languages', 'input_result_id', 'allow_reasoning'})
    video_id = _id(body.get('video_id'), label='字幕影片識別碼格式無效')
    stages = body.get('selected_stages')
    snapshots = _snapshot_selection(stages, body.get('profile_ids'))
    languages = body.get('target_languages', [])
    outputs = body.get('output_languages', [])
    if (not isinstance(languages, list) or len(languages) > 2 or
            any(not isinstance(item, str) or item not in ('en', 'zh-TW') for item in languages)
            or len(set(languages)) != len(languages)):
        raise ApiError(400, '字幕翻譯語言清單無效')
    if (not isinstance(outputs, list) or len(outputs) > 3 or
            any(item not in ('ja', 'en', 'zh-TW') for item in outputs)):
        raise ApiError(400, '字幕輸出語言清單無效')
    reasoning = body.get('allow_reasoning', False)
    if not isinstance(reasoning, bool):
        raise ApiError(400, '推理模式選項無效')
    input_result = body.get('input_result_id')
    if input_result is not None:
        _id(input_result, r'[A-Za-z0-9_-]{1,128}', '字幕上游結果識別碼格式無效')
    try:
        source = files.register_history(video_id)
    except ApiError:
        raise
    except ValueError as exc:
        raise ApiError(400, str(exc)[:240]) from None
    except OSError:
        raise ApiError(503, '字幕影片來源離線或無讀取權限，請確認下載或 NAS 目錄') from None
    try:
        return manager.submit(video_id, stages, snapshots, languages,
                              video_path=getattr(source, 'video_path', None),
                              output_languages=outputs, input_result_id=input_result,
                              allow_reasoning=reasoning)
    except ApiError:
        raise
    except ValueError as exc:
        raise ApiError(400, str(exc)[:240]) from None


def _task_view(task):
    value = {key: value for key, value in task.items()
             if key not in ('request_key', 'audio_path', 'video_path')}
    if isinstance(value.get('profile_snapshots'), dict):
        value['profile_snapshots'] = {
            key: _snapshot_summary(item) for key, item in value['profile_snapshots'].items()}
    return value


def _snapshot_summary(value):
    try:
        profile = json.loads(value['profile_json'])
        profile.pop('credential_ref', None)
        return {'snapshot_id': value.get('snapshot_id'), 'profile': profile,
                'credential_configured': bool(value.get('credential_ref'))}
    except (TypeError, KeyError, json.JSONDecodeError):
        return {'snapshot_id': None, 'profile': {}, 'credential_configured': False}


def _result_view(item):
    return {'result_id': item.result_id, 'video_id': item.video_id, 'stage': item.stage,
            'language': item.language, 'input_result_ids': list(item.input_result_ids),
            'created_at': item.created_at, 'content_available': item.content_available,
            'execution_id': item.execution_id, 'stale': item.stale, 'adopted': item.adopted,
            'profile_snapshot': _snapshot_summary({'profile_json': item.profile_snapshot.profile_json,
                'credential_ref': item.profile_snapshot.credential_ref,
                'snapshot_id': item.profile_snapshot.snapshot_id})}


def route(handler, method, path, body):
    """Handle one `/api/subtitles` request. Return (status, JSON) or None."""
    if path != '/api/subtitles' and not path.startswith('/api/subtitles/'):
        return None
    server = handler.server
    tasks, files = server.subtitle_task_manager, server.subtitle_file_manager
    segments = [unquote(part) for part in path.split('/') if part]
    try:
        if segments == ['api', 'subtitles', 'profiles']:
            if method == 'GET':
                return 200, {'profiles': [_public_profile(p) for p in profiles.load_profiles()]}
            if method == 'PUT':
                return 200, _profiles_put(_obj(body))
            raise ApiError(405, '不支援的字幕設定操作')
        if segments == ['api', 'subtitles', 'profiles', 'test'] and method == 'POST':
            return 200, _profile_test(_obj(body))
        if segments == ['api', 'subtitles', 'videos'] and method == 'GET':
            videos = files.list_videos(max_entries=1000)
            errors = [{'source': item.get('source'), 'error': item.get('error')}
                      for item in files.list_errors[:100]]
            return 200, {'videos': videos, 'errors': errors,
                         'partial': files.list_partial,
                         'notice': ('影片清單已達掃描上限，可能仍有未列出的影片。' if files.list_partial else None),
                         'capabilities': {'local_vad': _local_vad_capability()}}
        if segments == ['api', 'subtitles', 'tasks']:
            if method == 'GET':
                return 200, {'tasks': [_task_view(t) for t in tasks.list_tasks()[:1000]],
                             'publications': tasks.list_publications()[:1000]}
            if method == 'POST':
                body = _obj(body)
                if 'items' in body:
                    _keys(body, {'items'})
                    items = body['items']
                    if not isinstance(items, list) or len(items) > 100:
                        raise ApiError(400, '字幕批次必須包含 1 至 100 個項目')
                    outcomes = []
                    for index, item in enumerate(items):
                        try:
                            accepted = _submission(_obj(item), files, tasks)
                            outcomes.append({'index': index, 'video_id': item.get('video_id'),
                                             'success': True, 'task': _task_view(accepted)})
                        except ApiError as exc:
                            raw_id = item.get('video_id') if isinstance(item, dict) else None
                            safe_id = raw_id if isinstance(raw_id, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,512}', raw_id) else None
                            outcomes.append({'index': index, 'video_id': safe_id,
                                             'success': False, 'error': exc.message})
                        except Exception:
                            raw_id = item.get('video_id') if isinstance(item, dict) else None
                            safe_id = raw_id if isinstance(raw_id, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,512}', raw_id) else None
                            outcomes.append({'index': index, 'video_id': safe_id,
                                             'success': False, 'error': '字幕來源目前無法使用'})
                    return 200, {'items': outcomes}
                accepted = _submission(body, files, tasks)
                return 202, {'task': _task_view(accepted)}
        if len(segments) == 4 and segments[:3] == ['api', 'subtitles', 'tasks']:
            task_id = _id(segments[3], r'subtitle_[0-9a-f]{32}', '字幕任務 ID 格式無效')
            task = tasks.get_task(task_id)
            if task is None:
                raise ApiError(404, '字幕任務不存在')
            if method == 'GET':
                return 200, {'task': _task_view(task)}
        if len(segments) == 5 and segments[:3] == ['api', 'subtitles', 'tasks'] and segments[4] == 'action' and method == 'POST':
            task_id = _id(segments[3], r'subtitle_[0-9a-f]{32}', '字幕任務 ID 格式無效')
            body = _obj(body); action = body.get('action')
            _keys(body, {'action', 'position', 'profile_ids', 'language', 'stage', 'profile_id',
                         'input_result_id', 'allow_reasoning', 'output_languages'})
            task = tasks.get_task(task_id)
            if not task: raise ApiError(404, '字幕任務不存在')
            if action == 'cancel':
                ok = tasks.cancel(task_id)
                if not ok: raise ApiError(409, '字幕任務目前無法取消')
            elif action == 'resume':
                if not tasks.resume(task_id): raise ApiError(409, '字幕任務目前無法續跑')
            elif action == 'reorder':
                if type(body.get('position')) is not int: raise ApiError(400, '字幕工作位置無效')
                try: tasks.reorder(task_id, body['position'])
                except ValueError as exc: raise ApiError(409, str(exc)) from None
            elif action == 'apply_new_settings':
                snapshots = _snapshot_selection(task['selected_stages'], body.get('profile_ids'))
                try: tasks.apply_new_settings(task_id, snapshots)
                except ValueError as exc: raise ApiError(409, str(exc)) from None
            elif action == 'retry_publish':
                try: tasks.retry_publish(task_id, body.get('language'))
                except ValueError as exc: raise ApiError(409, str(exc)) from None
            elif action == 'redo':
                stage = body.get('stage'); profile_id = body.get('profile_id')
                if stage not in task['selected_stages'] and stage not in profiles.STAGES:
                    raise ApiError(400, '字幕階段無效')
                snapshot = _snapshot_selection([stage], {stage: profile_id})[stage]
                language = body.get('language')
                input_id = body.get('input_result_id')
                if input_id is not None: _id(input_id, r'[A-Za-z0-9_-]{1,128}')
                try:
                    accepted = tasks.redo(task['video_id'], stage, snapshot, language,
                                          input_result_id=input_id,
                                          allow_reasoning=body.get('allow_reasoning', False),
                                          output_languages=body.get('output_languages', ()))
                except ValueError as exc: raise ApiError(400, str(exc)) from None
                return 202, {'task': _task_view(accepted)}
            else:
                raise ApiError(400, '不支援的字幕任務操作')
            return 200, {'task': _task_view(tasks.get_task(task_id))}
        if len(segments) == 5 and segments[:3] == ['api', 'subtitles', 'videos'] and segments[4] == 'results' and method == 'GET':
            video_id = _id(segments[3])
            values = results.list_results(video_id)
            return 200, {'results': [_result_view(item) for item in values]}
        if len(segments) >= 6 and segments[:3] == ['api', 'subtitles', 'videos'] and segments[4] == 'results':
            video_id = _id(segments[3])
            # Retry restored storage by publication id, using the same result action route.
            result_id = segments[5]
            if len(segments) == 7 and segments[6] == 'content' and method == 'GET':
                result = results.get(video_id, _id(result_id, r'[A-Za-z0-9_-]{1,128}'))
                if result is None: raise ApiError(404, '字幕結果不存在')
                if not result.content_available:
                    raise ApiError(409, '此字幕版本的內容已清理，仍可查看版本資訊')
                if result.stage == 'vad':
                    content = [{'start': item.start, 'end': item.end} for item in result.intervals or ()]
                    kind, extension = 'application/json', 'json'
                else:
                    from uav_downloader.subtitles import engine
                    cues = [engine.SrtCue(str(index),
                            f'{engine._format_srt_timestamp(item.start)} --> {engine._format_srt_timestamp(item.end)}',
                            item.text) for index, item in enumerate(result.segments or (), 1)]
                    content = engine.render_srt(cues)
                    kind, extension = 'application/x-subrip', 'srt'
                if len(json.dumps(content, ensure_ascii=False).encode('utf-8')) > 2 * 1024 * 1024:
                    raise ApiError(413, '字幕檔案超過大小限制')
                return 200, {'content_type': kind,
                             'filename': f'{video_id[-12:]}-{result.stage}-{result.language or "ja"}.{extension}',
                             'content': content}
            if len(segments) == 7 and segments[6] == 'action' and method == 'POST':
                body = _obj(body); _keys(body, {'action', 'stage', 'language', 'output_language', 'replace_existing'})
                if body.get('action') == 'retry_publish':
                    publication = tasks.get_publication(result_id)
                    if not publication: raise ApiError(404, '字幕發布紀錄不存在')
                    if publication.get('video_id') != video_id: raise ApiError(404, '字幕發布紀錄不存在')
                    try: value = tasks.retry_restored_publish(result_id)
                    except ValueError as exc: raise ApiError(409, str(exc)) from None
                    return 200, value
                if body.get('action') == 'publish':
                    value = results.get(video_id, _id(result_id, r'[A-Za-z0-9_-]{1,128}'))
                    if value is None: raise ApiError(404, '字幕結果不存在')
                    language = body.get('output_language')
                    replace_existing = body.get('replace_existing')
                    if type(replace_existing) is not bool:
                        raise ApiError(400, '請明確選擇是否替換現有字幕')
                    try:
                        published = tasks.publish_result(video_id, result_id, language,
                                                         replace_existing=replace_existing)
                    except ValueError as exc: raise ApiError(409, str(exc)) from None
                    return 200, published
                if body.get('action') == 'restore':
                    stage, language = body.get('stage'), body.get('language')
                    _id(result_id, r'[A-Za-z0-9_-]{1,128}')
                    previous = results.previous(video_id, stage, language)
                    if previous is None or previous.result_id != result_id:
                        raise ApiError(409, '只能恢復上一個已保存的字幕版本')
                    try:
                        value = tasks.restore_previous(video_id, stage, language,
                            output_language=body.get('output_language'),
                            replace_existing=body.get('replace_existing', False))
                    except ValueError as exc: raise ApiError(409, str(exc)) from None
                    return 200, value
                raise ApiError(400, '不支援的字幕結果操作')
            if len(segments) == 6 and method == 'GET':
                value = results.get(video_id, _id(result_id, r'[A-Za-z0-9_-]{1,128}'))
                if value is None: raise ApiError(404, '字幕結果不存在')
                return 200, {'result': _result_view(value),
                             'intervals': [{'start': item.start, 'end': item.end}
                                           for item in value.intervals or ()],
                             'segments': [{'segment_id': item.segment_id, 'start': item.start,
                                           'end': item.end, 'text': item.text}
                                          for item in value.segments or ()],
                             'inputs': [_result_view(parent_value) for parent in value.input_result_ids
                                        if (parent_value := results.get(video_id, parent)) is not None]}
        raise ApiError(404, '字幕 API 路徑不存在')
    except ApiError as exc:
        return exc.status, {'success': False, 'error': exc.message}
    except ValueError as exc:
        return 400, {'success': False, 'error': str(exc)[:240]}
    except Exception:
        return 500, {'success': False, 'error': '字幕服務發生內部錯誤'}
