"""Versioned, file-backed results for independent subtitle stages."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass

from uav_downloader.subtitles import asr
from uav_downloader.subtitles.models import SpeechInterval, SubtitleSegment
from uav_downloader.subtitles.profiles import ProfileSnapshot

_LOCK = threading.RLock()
_SCHEMA = 1


@dataclass(frozen=True)
class StageResult:
    result_id: str
    video_id: str
    stage: str
    language: str | None
    input_result_ids: tuple[str, ...]
    profile_snapshot: ProfileSnapshot
    intervals: tuple[SpeechInterval, ...] | None
    segments: tuple[SubtitleSegment, ...] | None
    created_at: float = 0.0
    stale: bool = False
    adopted: bool = False
    content_available: bool = True
    execution_id: str | None = None
    execution_key: str | None = None


def _results_root() -> Path:
    return Path(asr._folder()) / 'subtitle-stage-results'


def _path(video_id: str) -> Path:
    key = hashlib.sha256(video_id.encode('utf-8')).hexdigest()
    return _results_root() / f'{key}.json'


def _read(video_id: str) -> dict:
    try:
        payload = json.loads(_path(video_id).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'schema': _SCHEMA, 'video_id': video_id, 'latest': {},
                'previous': {}, 'adopted': {}, 'results': {}, 'checkpoints': {}}
    if (not isinstance(payload, dict) or payload.get('schema') != _SCHEMA
            or payload.get('video_id') != video_id):
        raise ValueError('字幕階段結果檔案格式錯誤')
    for name in ('latest', 'previous', 'adopted', 'results', 'checkpoints'):
        if not isinstance(payload.get(name), dict):
            raise ValueError('字幕階段結果檔案格式錯誤')
    return payload


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ''
    finally:
        if temporary:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass


def _key(stage: str, language: str | None) -> str:
    return json.dumps([stage, language], ensure_ascii=False, separators=(',', ':'))


def _encode_value(value):
    if isinstance(value, SpeechInterval):
        return {'$type': 'interval', 'start': value.start, 'end': value.end}
    if isinstance(value, SubtitleSegment):
        return {'$type': 'segment', 'segment_id': value.segment_id,
                'start': value.start, 'end': value.end, 'text': value.text}
    if isinstance(value, (tuple, list)):
        return [_encode_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode_value(item) for key, item in value.items()}
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError('字幕階段結果包含不支援的資料')


def _decode_value(value):
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    if isinstance(value, dict):
        kind = value.get('$type')
        if kind == 'interval' and set(value) == {'$type', 'start', 'end'}:
            return SpeechInterval(value['start'], value['end'])
        if kind == 'segment' and set(value) == {'$type', 'segment_id', 'start', 'end', 'text'}:
            return SubtitleSegment(value['segment_id'], value['start'], value['end'], value['text'])
        return {key: _decode_value(item) for key, item in value.items()}
    return value


def _snapshot_to_dict(snapshot: ProfileSnapshot) -> dict:
    if not isinstance(snapshot, ProfileSnapshot):
        raise ValueError('字幕設定快照格式無效')
    try:
        profile = json.loads(snapshot.profile_json)
    except (TypeError, json.JSONDecodeError):
        raise ValueError('字幕設定快照格式無效') from None
    if not isinstance(profile, dict):
        raise ValueError('字幕設定快照格式無效')
    return {'profile_json': json.dumps(profile, ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':'), allow_nan=False),
            'credential_ref': snapshot.credential_ref, 'snapshot_id': snapshot.snapshot_id}


def _result_dict(result: StageResult, content=True) -> dict:
    snapshot = _snapshot_to_dict(result.profile_snapshot)
    return {
        'result_id': result.result_id, 'video_id': result.video_id,
        'stage': result.stage, 'language': result.language,
        'input_result_ids': list(result.input_result_ids),
        'profile_snapshot': snapshot,
        'intervals': _encode_value(result.intervals) if content else None,
        'segments': _encode_value(result.segments) if content else None,
        'created_at': result.created_at,
        'content_available': content,
        'execution_id': result.execution_id,
        'execution_key': result.execution_key,
    }


def _is_stale(payload: dict, result_id: str, memo=None, visiting=None) -> bool:
    memo = {} if memo is None else memo
    visiting = set() if visiting is None else visiting
    if result_id in memo:
        return memo[result_id]
    if result_id in visiting:
        return True
    record = payload['results'].get(result_id)
    if record is None:
        return True
    visiting.add(result_id)
    stale = False
    for input_id in record['input_result_ids']:
        parent = payload['results'].get(input_id)
        if parent is None:
            stale = True
            break
        parent_key = _key(parent['stage'], parent['language'])
        if payload['adopted'].get(parent_key) != input_id or _is_stale(
                payload, input_id, memo, visiting):
            stale = True
            break
    visiting.remove(result_id)
    memo[result_id] = stale
    return stale


def _from_dict(record: dict, stale=False, adopted=False) -> StageResult:
    snapshot = record['profile_snapshot']
    return StageResult(
        result_id=record['result_id'], video_id=record['video_id'],
        stage=record['stage'], language=record['language'],
        input_result_ids=tuple(record['input_result_ids']),
        profile_snapshot=ProfileSnapshot(snapshot['profile_json'],
                                         snapshot.get('credential_ref'),
                                         snapshot['snapshot_id']),
        intervals=tuple(_decode_value(record['intervals']))
            if record.get('content_available') and record.get('intervals') is not None else None,
        segments=tuple(_decode_value(record['segments']))
            if record.get('content_available') and record.get('segments') is not None else None,
        created_at=record.get('created_at', 0.0), stale=stale, adopted=adopted,
        content_available=bool(record.get('content_available')),
        execution_id=record.get('execution_id'),
        execution_key=record.get('execution_key'),
    )


def _public(payload: dict, result_id: str) -> StageResult | None:
    record = payload['results'].get(result_id)
    if record is None:
        return None
    key = _key(record['stage'], record['language'])
    return _from_dict(record, _is_stale(payload, result_id),
                      payload['adopted'].get(key) == result_id)


def _validate_result(result: StageResult) -> None:
    if (not isinstance(result, StageResult) or not isinstance(result.video_id, str)
            or not result.video_id or result.stage not in {'vad', 'asr', 'correction', 'translation'}
            or (result.language is not None and (not isinstance(result.language, str)
                or not result.language.strip()))
            or (result.stage == 'translation' and result.language is None)):
        raise ValueError('字幕階段結果格式無效')
    snapshot = _snapshot_to_dict(result.profile_snapshot)
    profile = json.loads(snapshot['profile_json'])
    if result.stage not in profile.get('capabilities', ()):
        raise ValueError('字幕設定快照不支援此階段')
    if len(set(result.input_result_ids)) != len(result.input_result_ids) or any(
            not isinstance(item, str) or not item for item in result.input_result_ids):
        raise ValueError('字幕階段輸入版本無效')
    if ((result.execution_id is None) != (result.execution_key is None)
            or (result.execution_id is not None
                and (not isinstance(result.execution_id, str)
                     or not result.execution_id
                     or not isinstance(result.execution_key, str)
                     or not result.execution_key))):
        raise ValueError('字幕執行識別資料無效')
    if result.stage == 'vad':
        if result.segments is not None or result.intervals is None:
            raise ValueError('VAD 結果格式無效')
    elif result.intervals is not None or result.segments is None:
        raise ValueError('字幕片段結果格式無效')
    for interval in result.intervals or ():
        if (not isinstance(interval, SpeechInterval) or type(interval.start) not in (int, float)
                or type(interval.end) not in (int, float) or not math.isfinite(interval.start)
                or not math.isfinite(interval.end) or interval.start < 0
                or interval.start >= interval.end):
            raise ValueError('VAD 語音區間結果格式無效')
    ids = set()
    for segment in result.segments or ():
        if (not isinstance(segment, SubtitleSegment) or not isinstance(segment.segment_id, str)
                or not segment.segment_id or segment.segment_id in ids
                or not isinstance(segment.text, str) or type(segment.start) not in (int, float)
                or type(segment.end) not in (int, float) or segment.start < 0
                or segment.start >= segment.end or not math.isfinite(segment.start)
                or not math.isfinite(segment.end)):
            raise ValueError('字幕片段結果格式無效')
        ids.add(segment.segment_id)


def _save(payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False) + '\n'
    _write_atomic(_path(payload['video_id']), text)


def commit_success(result: StageResult, clear_checkpoint_keys=()) -> StageResult:
    """Record one completed result; a failed call never replaces a success."""
    _validate_result(result)
    with _LOCK:
        payload = _read(result.video_id)
        for input_id in result.input_result_ids:
            parent = payload['results'].get(input_id)
            if parent is None or not parent.get('content_available'):
                raise ValueError('字幕階段輸入版本不存在或不可選用')
        key = _key(result.stage, result.language)
        previous_id = payload['latest'].get(key)
        stored = StageResult(
            result_id=result.result_id or uuid.uuid4().hex,
            video_id=result.video_id, stage=result.stage, language=result.language,
            input_result_ids=tuple(result.input_result_ids),
            profile_snapshot=result.profile_snapshot,
            intervals=tuple(result.intervals) if result.intervals is not None else None,
            segments=tuple(result.segments) if result.segments is not None else None,
            created_at=time.time(),
            execution_id=result.execution_id, execution_key=result.execution_key,
        )
        if stored.result_id in payload['results']:
            raise ValueError('字幕階段結果 ID 已存在')
        payload['results'][stored.result_id] = _result_dict(stored)
        if previous_id:
            payload['previous'][key] = previous_id
        payload['latest'][key] = stored.result_id
        payload['adopted'][key] = stored.result_id
        for checkpoint_key in clear_checkpoint_keys:
            payload['checkpoints'].pop(checkpoint_key, None)
        _prune_content(payload)
        _save(payload)
        return _public(payload, stored.result_id)


def _prune_content(payload: dict) -> None:
    keep_content = (set(payload['latest'].values())
                    | set(payload['previous'].values())
                    | set(payload['adopted'].values()))
    keep_metadata = set(keep_content)
    keep_metadata.update(input_id for record in payload['results'].values()
                         for input_id in record['input_result_ids'])
    for result_id in list(payload['results']):
        if result_id not in keep_metadata:
            del payload['results'][result_id]
        elif result_id not in keep_content:
            record = payload['results'][result_id]
            if record.get('content_available'):
                record['intervals'] = None
                record['segments'] = None
                record['content_available'] = False


def latest(video_id: str, stage: str, language: str | None = None) -> StageResult | None:
    with _LOCK:
        payload = _read(video_id)
        return _public(payload, payload['latest'].get(_key(stage, language), ''))


def previous(video_id: str, stage: str, language: str | None = None) -> StageResult | None:
    with _LOCK:
        payload = _read(video_id)
        return _public(payload, payload['previous'].get(_key(stage, language), ''))


def adopted(video_id: str, stage: str, language: str | None = None) -> StageResult | None:
    with _LOCK:
        payload = _read(video_id)
        return _public(payload, payload['adopted'].get(_key(stage, language), ''))


def get(video_id: str, result_id: str) -> StageResult | None:
    with _LOCK:
        return _public(_read(video_id), result_id)


def list_results(video_id: str) -> tuple[StageResult, ...]:
    with _LOCK:
        payload = _read(video_id)
        return tuple(sorted(
            (_public(payload, result_id) for result_id in payload['results']),
            key=lambda result: result.created_at, reverse=True))


def execution_request_key(video_id: str, execution_id: str, stage: str,
                          language: str | None, profile_snapshot: ProfileSnapshot,
                          input_result_ids, input_identity: str,
                          allow_reasoning: bool) -> str:
    value = {
        'video_id': video_id, 'execution_id': execution_id, 'stage': stage,
        'language': language, 'snapshot': _snapshot_to_dict(profile_snapshot),
        'inputs': list(input_result_ids), 'input_identity': input_identity,
        'allow_reasoning': allow_reasoning,
    }
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def completed_execution(video_id: str, execution_id: str,
                        execution_key: str) -> StageResult | None:
    """Find the exact stage success committed before a queue acknowledgement."""
    with _LOCK:
        payload = _read(video_id)
        for result_id, record in payload['results'].items():
            if (record.get('execution_id') == execution_id
                    and record.get('execution_key') == execution_key):
                return _public(payload, result_id)
        return None


def restore_previous(video_id: str, stage: str, language: str | None = None) -> StageResult:
    """Adopt the saved previous success without inventing a new result version."""
    with _LOCK:
        payload = _read(video_id)
        key = _key(stage, language)
        result_id = payload['previous'].get(key)
        record = payload['results'].get(result_id)
        if record is None or not record.get('content_available'):
            raise ValueError('沒有可回復的上一份成功字幕結果')
        payload['adopted'][key] = result_id
        _save(payload)
        return _public(payload, result_id)


def checkpoint_key(execution_id: str, stage: str, audio_identity: str,
                   profile_snapshot: ProfileSnapshot, input_result_ids,
                   chunk_identity) -> str:
    value = {'execution': execution_id, 'stage': stage, 'audio': audio_identity,
             'snapshot': _snapshot_to_dict(profile_snapshot),
             'inputs': list(input_result_ids), 'chunk': _encode_value(chunk_identity)}
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def save_checkpoint(video_id: str, key: str, value) -> None:
    with _LOCK:
        payload = _read(video_id)
        payload['checkpoints'][key] = _encode_value(value)
        _save(payload)


def load_checkpoint(video_id: str, key: str):
    with _LOCK:
        value = _read(video_id)['checkpoints'].get(key)
        return _decode_value(value) if value is not None else None


def clear_checkpoints(video_id: str, keys) -> None:
    with _LOCK:
        payload = _read(video_id)
        changed = False
        for key in keys:
            changed = payload['checkpoints'].pop(key, None) is not None or changed
        if changed:
            _save(payload)
