from types import SimpleNamespace

from uav_downloader.daemon.subtitle_files import SubtitleFileManager
from uav_downloader.daemon.subtitle_tasks import SubtitleTaskManager
from uav_downloader.subtitles import asr, engine


def test_trial_clips_cues_and_full_uses_safe_publication(tmp_path, monkeypatch):
    movie = tmp_path / 'movie.mp4'
    movie.write_bytes(b'complete media')
    original = tmp_path / 'movie.ja.srt'
    original.write_text(
        '1\n00:01:59,000 --> 00:02:01,000\n一つ\n\n'
        '2\n00:02:00,000 --> 00:02:03,000\n二つ\n', encoding='utf-8')
    old_translation = tmp_path / 'movie.zh-TW.srt'
    old_translation.write_text('手動字幕', encoding='utf-8')
    files = SubtitleFileManager(tmp_path / 'files.json')
    video_id = files.register(str(movie), allowed_roots=[str(tmp_path)]).video_id
    monkeypatch.setattr(SubtitleTaskManager, 'start_dispatcher', lambda self: None)
    manager = SubtitleTaskManager(tmp_path / 'tasks.json', source_manager=files)

    def translate(texts, source, target, stage, callback, _cancel):
        assert (source, target) == ('ja', 'zh-TW')
        assert engine._translation_provider_state.profile is None
        callback(stage, 50)
        return ['繁中字幕'] * len(texts)

    monkeypatch.setattr(engine, 'translate_cues', translate)
    with engine._translation_profile_scope(SimpleNamespace(uses_api=True)):
        trial = manager.submit_library_translation(video_id, 120)
        manager._run_task(trial['task_id'])
        assert manager.get_task(trial['task_id'])['status'] == 'completed'
        segments = engine.parse_srt(manager.library_track(trial['task_id'], 'zh-TW'))
        assert [cue.timing for cue in segments] == ['00:01:59,000 --> 00:02:00,000']
        assert list(tmp_path.glob('movie.zh-TW*.srt')) == [old_translation]

        full = manager.submit_library_translation(video_id)
        manager._run_task(full['task_id'])
        completed = manager.get_task(full['task_id'])
        assert (completed['progress'], completed['processed_cues'], completed['processed_seconds']) == (100, 2, 123)
        exported = tmp_path / completed['publish_status']['zh-TW']['path']
        assert exported != old_translation
        assert [cue.timing for cue in engine.parse_srt(exported.read_text())] == [
            '00:01:59,000 --> 00:02:01,000', '00:02:00,000 --> 00:02:03,000']
        assert old_translation.read_text() == '手動字幕'


def test_linux_recognition_uses_local_sidecar_if_configured_endpoint_is_cloud(tmp_path, monkeypatch):
    from uav_downloader.daemon import subtitle_tasks

    monkeypatch.setattr(subtitle_tasks.sys, 'platform', 'linux')
    monkeypatch.setattr(engine, '_validate_pcm16_wav', lambda _path: (1920000, 120))
    durations = []
    monkeypatch.setattr(engine, '_extract_audio',
                        lambda *_args, duration_seconds: durations.append(duration_seconds))
    monkeypatch.setattr(asr, 'load_settings',
                        lambda: {'base_url': 'https://cloud.invalid/v1', 'model': 'cloud'})

    def transcribe(settings, _wav, output, *_args):
        assert settings == {'base_url': asr.DEFAULT_URL, 'model': asr.DEFAULT_MODEL}
        return output + '.srt'

    monkeypatch.setattr(asr, 'transcribe', transcribe)
    path, seconds = SubtitleTaskManager._library_asr(
        'video.mp4', str(tmp_path), 120, lambda *_: None, lambda: False)
    assert path == str(tmp_path / 'japanese.srt')
    assert seconds == 120
    assert durations == [120]
