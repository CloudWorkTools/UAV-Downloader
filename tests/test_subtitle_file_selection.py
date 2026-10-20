from pathlib import Path

import pytest
from types import SimpleNamespace

from uav_downloader.subtitles.models import SubtitleSegment


def test_registered_video_is_persisted_and_copy_keeps_its_own_locator(tmp_path):
    from uav_downloader.daemon.subtitle_files import SubtitleFileManager

    root = tmp_path / 'videos'
    root.mkdir()
    video = root / 'movie.mp4'
    video.write_bytes((b'original-media-' * 10000) + b'end')
    state = tmp_path / 'subtitle_sources.json'

    sources = SubtitleFileManager(state_path=state)
    first = sources.register(str(video), allowed_roots=[str(root)])
    restarted = SubtitleFileManager(state_path=state)

    with restarted.resolve(first.video_id) as resolved:
        assert resolved.video_path == str(video)
        assert resolved.identity == first.identity
    copied = root / 'copy.mp4'
    copied.write_bytes(video.read_bytes())
    second = restarted.register(str(copied), allowed_roots=[str(root)])
    assert second.video_id != first.video_id
    with restarted.resolve(first.video_id) as resolved:
        assert resolved.video_path == str(video)
    with restarted.resolve(second.video_id) as resolved:
        assert resolved.video_path == str(copied)


def test_identical_selected_copies_publish_to_their_registered_files(tmp_path):
    from uav_downloader.daemon.subtitle_files import SubtitleFileManager

    root = tmp_path / 'videos'
    root.mkdir()
    first_path = root / 'first.mp4'
    second_path = root / 'second.mp4'
    first_path.write_bytes(b'same media')
    second_path.write_bytes(b'same media')
    files = SubtitleFileManager(state_path=tmp_path / 'sources.json')
    first = files.register(str(first_path), allowed_roots=[str(root)])
    second = files.register(str(second_path), allowed_roots=[str(root)])

    files.publish_result(first.video_id, result(first.video_id, 'result0001', 'first file'), 'ja')
    files.publish_result(second.video_id, result(second.video_id, 'result0002', 'second file'), 'ja')

    assert (root / 'first.ja.srt').read_text(encoding='utf-8').endswith('first file\n')
    assert (root / 'second.ja.srt').read_text(encoding='utf-8').endswith('second file\n')


@pytest.mark.parametrize('target', ['outside', 'missing', 'root_link'])
def test_video_selection_rejects_paths_outside_or_through_symlinks(tmp_path, target):
    from uav_downloader.daemon.subtitle_files import SubtitleFileManager

    root = tmp_path / 'videos'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    video = outside / 'movie.mp4'
    video.write_bytes(b'media')
    path = str(video)
    if target == 'missing':
        path = str(root / 'missing.mp4')
    elif target == 'root_link':
        link = tmp_path / 'root-link'
        link.symlink_to(root, target_is_directory=True)
        path = str(link / 'movie.mp4')

    sources = SubtitleFileManager(state_path=tmp_path / 'sources.json')
    with pytest.raises(ValueError):
        sources.register(path, allowed_roots=[str(root)])


def test_video_selection_rejects_file_symlinks_even_when_the_target_is_allowed(tmp_path):
    from uav_downloader.daemon.subtitle_files import SubtitleFileManager

    root = tmp_path / 'videos'
    root.mkdir()
    real = root / 'real.mp4'
    real.write_bytes(b'media')
    alias = root / 'alias.mp4'
    alias.symlink_to(real)

    sources = SubtitleFileManager(state_path=tmp_path / 'sources.json')
    with pytest.raises(ValueError):
        sources.register(str(alias), allowed_roots=[str(root)])


def test_registered_source_is_invalid_after_media_changes(tmp_path):
    from uav_downloader.daemon.subtitle_files import SubtitleFileManager

    root = tmp_path / 'videos'
    root.mkdir()
    video = root / 'movie.mp4'
    video.write_bytes(b'original')
    sources = SubtitleFileManager(state_path=tmp_path / 'sources.json')
    source = sources.register(str(video), allowed_roots=[str(root)])
    video.write_bytes(b'replaced media, different size')

    with pytest.raises(ValueError, match='影片已變更'):
        with sources.resolve(source.video_id):
            pass


def result(video_id, result_id, text):
    return SimpleNamespace(
        video_id=video_id,
        result_id=result_id,
        segments=() if text is None else (SubtitleSegment('seg-1', 0, 1, text),),
    )


def test_publish_preserves_unknown_srt_and_saves_unique_generated_sidecar(tmp_path):
    from uav_downloader.daemon.subtitle_files import SubtitleFileManager

    root = tmp_path / 'videos'
    root.mkdir()
    video = root / 'movie.mp4'
    video.write_bytes(b'media')
    user_srt = root / 'movie.zh-TW.srt'
    user_srt.write_bytes(b'hand edited\n')
    files = SubtitleFileManager(state_path=tmp_path / 'sources.json')
    source = files.register(str(video), allowed_roots=[str(root)])

    published = files.publish_result(source.video_id, result(source.video_id, 'result1234', '新字幕'), 'zh-TW')

    assert user_srt.read_bytes() == b'hand edited\n'
    assert published['path'] == 'movie.zh-TW.uav-result1234.srt'
    assert (root / published['path']).read_text(encoding='utf-8') == (
        '1\n00:00:00,000 --> 00:00:01,000\n新字幕\n')


def test_publish_replaces_only_unchanged_trusted_srt_or_explicitly_replaced_srt(tmp_path):
    from uav_downloader.daemon.subtitle_files import SubtitleFileManager

    root = tmp_path / 'videos'
    root.mkdir()
    video = root / 'movie.mp4'
    video.write_bytes(b'media')
    canonical = root / 'movie.ja.srt'
    files = SubtitleFileManager(state_path=tmp_path / 'sources.json')
    source = files.register(str(video), allowed_roots=[str(root)])

    files.publish_result(source.video_id, result(source.video_id, 'result0001', 'first'), 'ja')
    files.publish_result(source.video_id, result(source.video_id, 'result0002', 'second'), 'ja')
    assert canonical.read_text(encoding='utf-8').endswith('second\n')

    canonical.write_text('human edit', encoding='utf-8')
    saved = files.publish_result(source.video_id, result(source.video_id, 'result0003', 'third'), 'ja')
    assert saved['path'] == 'movie.ja.uav-result0003.srt'
    assert canonical.read_text(encoding='utf-8') == 'human edit'

    replaced = files.publish_result(source.video_id, result(source.video_id, 'result0004', 'fourth'),
                                    'ja', replace_existing=True)
    assert replaced['path'] == 'movie.ja.srt'
    assert canonical.read_text(encoding='utf-8').endswith('fourth\n')


def test_publish_keeps_conflicting_generated_name_and_allows_empty_srt(tmp_path):
    from uav_downloader.daemon.subtitle_files import SubtitleFileManager

    root = tmp_path / 'videos'
    root.mkdir()
    video = root / 'movie.mp4'
    video.write_bytes(b'media')
    (root / 'movie.en.srt').write_text('manual', encoding='utf-8')
    conflict = root / 'movie.en.uav-result0.srt'
    conflict.write_text('other generated-looking file', encoding='utf-8')
    files = SubtitleFileManager(state_path=tmp_path / 'sources.json')
    source = files.register(str(video), allowed_roots=[str(root)])

    published = files.publish_result(source.video_id, result(source.video_id, 'result0001', None), 'en')

    assert conflict.read_text(encoding='utf-8') == 'other generated-looking file'
    assert published['path'] == 'movie.en.uav-result0001.srt'
    assert (root / published['path']).read_bytes() == b''


def test_failed_trusted_replacement_retains_srt_and_provenance(tmp_path, monkeypatch):
    from uav_downloader.core.storage import storage_fs
    from uav_downloader.daemon.subtitle_files import SubtitleFileManager
    from uav_downloader.subtitles import engine

    root = tmp_path / 'videos'
    root.mkdir()
    video = root / 'movie.mp4'
    video.write_bytes(b'media')
    files = SubtitleFileManager(state_path=tmp_path / 'sources.json')
    source = files.register(str(video), allowed_roots=[str(root)])
    files.publish_result(source.video_id, result(source.video_id, 'result0001', 'first'), 'en')
    target = root / 'movie.en.srt'
    before_srt = target.read_bytes()
    provenance = Path(engine._subtitle_provenance_path(str(video)))
    before_provenance = provenance.read_bytes()
    filesystem = storage_fs(str(root))
    replace = filesystem.replace

    def fail_replace(source_path, target_path):
        if target_path == str(target):
            raise OSError('NAS unavailable')
        return replace(source_path, target_path)

    monkeypatch.setattr(filesystem, 'replace', fail_replace)
    with pytest.raises(OSError):
        files.publish_result(source.video_id, result(source.video_id, 'result0002', 'second'), 'en')

    assert target.read_bytes() == before_srt
    assert provenance.read_bytes() == before_provenance
