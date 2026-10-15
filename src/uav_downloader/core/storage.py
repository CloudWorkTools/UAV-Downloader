"""Identify destination I/O failures separately from retryable transport errors."""

import errno
import re
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager


def storage_failure_message(message):
    return bool(re.search(
        r'no space left|disk (?:is )?full|input/output error|i/o error|'
        r'device (?:is )?not ready|no such device|device not connected|'
        r'error (?:writing|closing)|failed to (?:write|flush)',
        str(message), re.IGNORECASE))


class StorageWriteError(OSError):
    """Destination storage failed; stop writes and require an explicit retry."""

    def __init__(self, path, cause):
        self.path = str(path)
        self.cause = cause
        super().__init__(
            getattr(cause, 'errno', errno.EIO),
            f'Destination storage I/O failed: {cause}. Downloads stopped; '
            'check free space and device health before retrying.', self.path)


class _StorageFile:
    def __init__(self, path, mode, *args, **kwargs):
        self.path = path
        try:
            self.file = storage_fs(path).open(path, mode, *args, **kwargs)
        except OSError as exc:
            raise StorageWriteError(path, exc) from exc

    def _io(self, method, *args, **kwargs):
        try:
            return getattr(self.file, method)(*args, **kwargs)
        except OSError as exc:
            raise StorageWriteError(self.path, exc) from exc
        except Exception:
            if storage_fs(self.path).remote:
                raise StorageWriteError(self.path, OSError(errno.EIO, 'NAS SMB I/O failed')) from None
            raise

    def write(self, data):
        written = self._io('write', data)
        if written != len(data):
            raise StorageWriteError(self.path, OSError(errno.EIO, 'short write'))
        return written

    def read(self, *args):
        return self._io('read', *args)

    def seek(self, *args):
        return self._io('seek', *args)

    def truncate(self, *args):
        return self._io('truncate', *args)

    def flush(self):
        return self._io('flush')

    def __getattr__(self, name):
        return getattr(self.file, name)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            self._io('close')
        except StorageWriteError:
            if kind is None:
                raise


def storage_open(path, mode, *args, **kwargs):
    return _StorageFile(path, mode, *args, **kwargs)


def probe_destination(directory):
    """Exercise the exact output directory without touching existing files."""
    filesystem = storage_fs(directory)
    filesystem.makedirs(directory, exist_ok=True)
    probe = filesystem.mkdtemp(prefix='.uav-write-test-', dir=directory)
    source, target = os.path.join(probe, 'source.tmp'), os.path.join(probe, 'renamed.tmp')
    stage = '寫入'
    try:
        content = b'video-storage-probe\0' * 4096
        with storage_open(source, 'xb') as stream:
            stream.write(content)
            stream.flush()
        stage = '重新命名'
        filesystem.publish(source, target)
        stage = '讀回'
        with storage_open(target, 'rb') as stream:
            if stream.read() != content:
                raise OSError(errno.EIO, '目的地讀回資料不一致')
        stage = '刪除'
        filesystem.remove(target)
    except OSError as exc:
        raise StorageWriteError(directory, OSError(exc.errno or errno.EIO, f'目的地{stage}測試失敗：{exc}')) from exc
    finally:
        filesystem.rmtree(probe)


# Virtual paths are never local folders, including after a task disconnects.
SMB_PATH_ROOT = os.path.abspath(os.path.join(os.path.sep, '__uav_smb__'))
_backends = {}
_backend_lock = threading.RLock()


class LocalStorage:
    remote = False

    def open(self, path, mode, *args, **kwargs):
        return open(path, mode, *args, **kwargs)

    def lock(self, path):
        # Keep the inode: unlinking a POSIX lock lets another process bypass its owner.
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        handle = os.fdopen(descriptor, 'r+b')
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        return handle

    def stat(self, path, **kwargs):
        return os.stat(path, **kwargs)

    def exists(self, path):
        return os.path.exists(path)

    def isfile(self, path):
        return os.path.isfile(path)

    def isdir(self, path):
        return os.path.isdir(path)

    def islink(self, path):
        return os.path.islink(path)

    def getsize(self, path):
        return os.path.getsize(path)

    def listdir(self, path):
        return os.listdir(path)

    def makedirs(self, path, exist_ok=False):
        return os.makedirs(path, exist_ok=exist_ok)

    def mkdtemp(self, *, prefix, dir):
        return tempfile.mkdtemp(prefix=prefix, dir=dir)

    def remove(self, path):
        return os.remove(path)

    def removedirs(self, path):
        return os.removedirs(path)

    def rmtree(self, path, ignore_errors=False):
        return shutil.rmtree(path, ignore_errors=ignore_errors)

    def replace(self, source, destination):
        return os.replace(source, destination)

    def publish(self, source, destination):
        # Task workspaces live on the destination volume; refuse existing files.
        try:
            os.link(source, destination)
        except OSError as exc:
            if exc.errno not in (errno.EOPNOTSUPP, errno.ENOSYS, errno.EPERM, errno.EXDEV):
                raise
            # Some mounted NAS filesystems do not support hard links.
            with open(destination, 'xb') as target:
                try:
                    with open(source, 'rb') as stream:
                        shutil.copyfileobj(stream, target, 1024 * 1024)
                except BaseException:
                    os.remove(destination)
                    raise
        os.remove(source)

    @contextmanager
    def media_source(self, path):
        yield path


_local = LocalStorage()


def storage_fs(path):
    value = os.fsdecode(path)
    if value == SMB_PATH_ROOT or value.startswith(SMB_PATH_ROOT + os.path.sep):
        with _backend_lock:
            for root, backend in _backends.items():
                if value == root or value.startswith(root + os.path.sep):
                    return backend
        raise StorageWriteError(value, OSError(errno.ENOTCONN, 'NAS SMB connection is not active'))
    return _local


@contextmanager
def registered_storage(root, backend):
    with _backend_lock:
        if root in _backends:
            raise ValueError('Storage destination is already active')
        _backends[root] = backend
    try:
        yield backend
    finally:
        with _backend_lock:
            _backends.pop(root, None)
