"""FFmpeg diagnostics stay bounded, private, and local to one remux."""
from concurrent.futures import ThreadPoolExecutor
import threading

import av
import pytest


def test_remux_error_keeps_only_the_last_eight_messages():
    from uav_downloader.core.media_errors import capture_remux_errors, MediaRemuxError
    with pytest.raises(MediaRemuxError) as error:
        with capture_remux_errors():
            for index in range(12):
                av.logging.log(av.logging.ERROR, 'mp4', f'packet-{index:02d} failed\n')
            raise av.error.ArgumentError(22, 'Invalid argument')
    message = str(error.value)
    assert 'FFmpeg 22' in message
    assert 'packet-03' not in message
    assert all(f'packet-{index:02d}' in message for index in range(4, 12))


def test_remux_error_redacts_credentials_urls_and_paths_before_storage():
    from uav_downloader.core.media_errors import capture_remux_errors, MediaRemuxError
    with pytest.raises(MediaRemuxError) as error:
        with capture_remux_errors():
            for message in (
                'Cannot open https://user:private-password@cdn.test/video?token=private-token',
                'Cannot open smb://nas/share/private-video.mp4',
                'Cannot open "/private folder/private-title.mp4"',
                r'Cannot open C:\private folder\title.mp4 or \\nas\share\private folder\title.mp4',
                'Authorization: Bearer private-bearer',
                'Cookie: session=private-cookie; other=private-value',
                'password=private-password',
                'non monotonically increasing dts in stream 1: 100 >= 90',
            ):
                av.logging.log(av.logging.ERROR, 'mp4', message + '\n')
            raise av.error.ArgumentError(22, 'Invalid argument', '/private/file.mp4')
    message = str(error.value)
    assert 'private' not in message
    assert 'cdn.test' not in message
    assert 'nas' not in message
    assert '100 >= 90' in message
    assert '[redacted]' in message


def test_remux_error_limits_message_and_total_length():
    from uav_downloader.core.media_errors import capture_remux_errors, MediaRemuxError
    with pytest.raises(MediaRemuxError) as error:
        with capture_remux_errors():
            for index in range(12):
                av.logging.log(av.logging.ERROR, 'mp4', 'x' * 900 + f' tail-{index}\n')
            raise av.error.ArgumentError(22, 'Invalid argument')
    message = str(error.value)
    assert len(message) <= 2200
    assert max(map(len, message.splitlines())) <= 600
    assert 'tail-11' in message


def test_parallel_remux_errors_and_retries_do_not_share_messages():
    from uav_downloader.core.media_errors import capture_remux_errors, MediaRemuxError
    barrier = threading.Barrier(2)
    def fail(label):
        with pytest.raises(MediaRemuxError) as error:
            with capture_remux_errors():
                av.logging.log(av.logging.ERROR, 'mp4', f'{label} timestamp failure\n')
                barrier.wait(timeout=5)
                raise av.error.ArgumentError(22, 'Invalid argument')
        return str(error.value)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(fail, ['first-task', 'second-task']))
    assert 'first-task' in first and 'second-task' not in first
    assert 'second-task' in second and 'first-task' not in second
    with pytest.raises(MediaRemuxError) as error:
        with capture_remux_errors():
            raise av.error.ArgumentError(22, 'No diagnostic emitted')
    assert 'No diagnostic emitted' in str(error.value)
    assert 'timestamp failure' not in str(error.value)


@pytest.mark.parametrize('kind', [OSError, InterruptedError])
def test_non_ffmpeg_errors_keep_their_original_type(kind):
    from uav_downloader.core.media_errors import capture_remux_errors
    with pytest.raises(kind, match='original failure'):
        with capture_remux_errors():
            raise kind('original failure')
