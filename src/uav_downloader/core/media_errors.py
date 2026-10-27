"""Bounded, thread-local FFmpeg diagnostics without private media locations."""
from collections import deque
from contextlib import contextmanager
import logging
import re
import threading


class MediaRemuxError(RuntimeError):
    """A media failure whose message is safe to persist and display."""


def safe_media_message(message):
    text = str(message)
    text = re.sub(r'''(["'])[^\n]*?\1''', '[redacted]', text)
    text = re.sub(r'(?i)\b(?:authorization|cookie|username|user|password|passwd|token|secret|api[_-]?key|secure)\s*[:=][^\n]*',
                  '[redacted]', text)
    text = re.sub(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+', '[redacted]', text)
    text = re.sub(r'(?<!\w)(?:\\\\|[a-zA-Z]:[\\/]|/)[^\n]*', '[redacted]', text)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text).strip()


_local = threading.local()


class _RemuxLogHandler(logging.Handler):
    def emit(self, record):
        messages = getattr(_local, 'messages', None)
        if messages is not None:
            messages.append(safe_media_message(f'{record.name}: {record.getMessage()}')[-512:])


_logger = logging.getLogger('libav')
_logger.setLevel(logging.ERROR)
_logger.addHandler(_RemuxLogHandler())
# Raw FFmpeg messages can contain signed URLs; only expose the safe task error.
_logger.propagate = False


@contextmanager
def capture_remux_errors():
    import av
    av.logging.set_level(av.logging.ERROR)
    # Global repeat suppression would hide a second task's identical failure.
    av.logging.set_skip_repeated(False)
    previous = getattr(_local, 'messages', None)
    messages = _local.messages = deque(maxlen=8)
    try:
        yield
    except av.FFmpegError as exc:
        detail = '\n'.join(messages)[-2048:] or safe_media_message(exc.strerror)[-2048:]
        raise MediaRemuxError(f'MP4 重新封裝失敗（FFmpeg {exc.errno}）：{detail}') from None
    finally:
        _local.messages = previous
