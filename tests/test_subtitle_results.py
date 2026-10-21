import json

import pytest

from uav_downloader.subtitles import results
from uav_downloader.subtitles.models import SpeechInterval, SubtitleSegment
from uav_downloader.subtitles.profiles import ProfileSnapshot


def _snapshot(stage='asr'):
    kind = 'text' if stage in ('correction', 'translation') else stage
    profile = {
        'profile_id': 'test', 'display_name': 'Test', 'kind': kind,
        'provider': 'localai', 'capabilities': [stage], 'base_url': 'http://127.0.0.1:1',
        'model': 'fixture', 'parameters': {},
    }
    return ProfileSnapshot(json.dumps(profile), None, f'snapshot-{stage}')


def _result(video='video-1', stage='asr', language=None, inputs=(), text='hello'):
    return results.StageResult(
        result_id='', video_id=video, stage=stage, language=language,
        input_result_ids=tuple(inputs), profile_snapshot=_snapshot(stage),
        intervals=None,
        segments=(SubtitleSegment('seg-1', 0.0, 1.0, text),),
    )


def test_successful_versions_keep_latest_previous_and_adoption_separate(
        monkeypatch, tmp_path):
    monkeypatch.setattr(results, '_results_root', lambda: tmp_path)
    first = results.commit_success(_result(text='first'))
    results.commit_success(_result(stage='correction', inputs=(first.result_id,), text='child'))
    second = results.commit_success(_result(text='second'))
    third = results.commit_success(_result(text='third'))

    assert results.latest('video-1', 'asr').result_id == third.result_id
    assert results.previous('video-1', 'asr').result_id == second.result_id
    assert results.adopted('video-1', 'asr').result_id == third.result_id
    assert results.get('video-1', first.result_id).segments is None
    assert results.get('video-1', first.result_id).content_available is False
    listed = {item.result_id: item for item in results.list_results('video-1')}
    assert listed[first.result_id].stale is False
    assert listed[first.result_id].content_available is False
    assert listed[second.result_id].content_available is True
    assert listed[third.result_id].adopted is True


def test_result_snapshot_serialization_preserves_reasoning_verification_binding():
    snapshot = ProfileSnapshot(
        profile_json='{"capabilities":["correction"],"profile_id":"text"}',
        credential_ref="credential-ref", snapshot_id="snapshot-text",
        reasoning_verification_json='{"status":"supported","stage":"correction"}')
    record = {
        'result_id': 'result-1', 'video_id': 'video-1', 'stage': 'correction',
        'language': None, 'input_result_ids': [], 'profile_snapshot': results._snapshot_to_dict(snapshot),
        'intervals': None, 'segments': None, 'created_at': 1.0,
        'content_available': False,
    }

    restored = results._from_dict(record)

    assert restored.profile_snapshot == snapshot


def test_restore_previous_adopts_without_creating_a_successful_version(
        monkeypatch, tmp_path):
    monkeypatch.setattr(results, '_results_root', lambda: tmp_path)
    first = results.commit_success(_result(text='first'))
    second = results.commit_success(_result(text='second'))

    restored = results.restore_previous('video-1', 'asr')

    assert restored.result_id == first.result_id
    assert restored.segments[0].text == 'first'
    assert results.latest('video-1', 'asr').result_id == second.result_id
    assert results.previous('video-1', 'asr').result_id == first.result_id
    assert results.latest('video-1', 'asr').adopted is False
    assert results.adopted('video-1', 'asr').result_id == first.result_id
    assert results.adopted('video-1', 'asr').adopted is True


def test_current_adopted_inputs_determine_transitive_staleness(
        monkeypatch, tmp_path):
    monkeypatch.setattr(results, '_results_root', lambda: tmp_path)
    vad_old = results.commit_success(results.StageResult(
        '', 'video-1', 'vad', None, (), _snapshot('vad'),
        (SpeechInterval(0, 2),), None))
    asr_old = results.commit_success(_result(
        stage='asr', inputs=(vad_old.result_id,), text='old chain'))
    vad_new = results.commit_success(results.StageResult(
        '', 'video-1', 'vad', None, (), _snapshot('vad'),
        (SpeechInterval(0, 3),), None))
    correction_old = results.commit_success(_result(
        stage='correction', inputs=(asr_old.result_id,), text='corrected'))

    assert results.get('video-1', asr_old.result_id).stale is True
    assert results.get('video-1', correction_old.result_id).stale is True
    results.restore_previous('video-1', 'vad')
    assert results.get('video-1', asr_old.result_id).stale is False
    assert results.get('video-1', correction_old.result_id).stale is False
    assert results.latest('video-1', 'vad').result_id == vad_new.result_id


def test_checkpoint_is_bound_to_stage_profile_inputs_and_plan(
        monkeypatch, tmp_path):
    monkeypatch.setattr(results, '_results_root', lambda: tmp_path)
    key = results.checkpoint_key(
        'execution-1', 'vad', 'audio-sha256', _snapshot('vad'), (),
        {'index': 0, 'end': 42})
    results.save_checkpoint('video-1', key, [SpeechInterval(0.1, 0.5)])

    assert results.load_checkpoint('video-1', key) == [SpeechInterval(0.1, 0.5)]
    assert results.load_checkpoint('video-1', key + 'different') is None


def test_failed_atomic_commit_leaves_previous_success_selectable(
        monkeypatch, tmp_path):
    monkeypatch.setattr(results, '_results_root', lambda: tmp_path)
    first = results.commit_success(_result(text='first'))
    monkeypatch.setattr(
        results, '_write_atomic',
        lambda *_args: (_ for _ in ()).throw(OSError('disk full')))

    with pytest.raises(OSError):
        results.commit_success(_result(text='failed'))

    assert results.latest('video-1', 'asr').result_id == first.result_id
    assert results.adopted('video-1', 'asr').result_id == first.result_id
