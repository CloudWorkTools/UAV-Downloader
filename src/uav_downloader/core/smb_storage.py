"""SMB files and bounded-memory media streaming, without an OS mount."""
import errno
import ntpath
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from uav_downloader.core.storage import StorageWriteError


def smb_lock(path, **options):
    import smbclient
    from smbprotocol.header import NtStatus
    from smbprotocol.open import CreateOptions, FilePipePrinterAccessMask as Access
    try:
        return smbclient.open_file(
            path, mode='a+b', share_access=None,
            desired_access=Access.GENERIC_READ | Access.GENERIC_WRITE | Access.DELETE,
            create_options=CreateOptions.FILE_DELETE_ON_CLOSE, **options)
    except OSError as exc:
        if getattr(exc, 'ntstatus', None) in (NtStatus.STATUS_SHARING_VIOLATION, NtStatus.STATUS_DELETE_PENDING):
            raise BlockingIOError(errno.EBUSY, 'NAS 下載鎖仍由其他連線持有') from None
        raise


class SmbStorage:
    remote = True

    def __init__(self, settings, virtual_root):
        import smbclient
        self.client = smbclient
        self.virtual_root = virtual_root
        self.root = ntpath.join('\\\\' + settings['host'], settings['share'], settings['directory'])
        self.options = {'connection_cache': {}}
        try:
            smbclient.register_session(settings['host'], username=settings['username'],
                                       password=settings['password'], connection_timeout=10,
                                       **self.options)
        except Exception:
            self.close()
            raise OSError(errno.EIO, 'NAS SMB 登入失敗，請檢查位址、名稱解析與帳密') from None

    def close(self):
        self.client.reset_connection_cache(fail_on_error=False, **self.options)

    def _path(self, path):
        relative = os.path.relpath(path, self.virtual_root)
        if relative == '..' or relative.startswith('..' + os.path.sep) or os.path.isabs(relative):
            raise ValueError('Path is outside the SMB destination')
        return self.root if relative == '.' else ntpath.join(self.root, *relative.split(os.path.sep))

    def _call(self, method, *paths, **kwargs):
        try:
            return method(*(self._path(path) for path in paths), **kwargs, **self.options)
        except OSError as exc:
            code = exc.errno or errno.EIO
            kind = {errno.ENOENT: FileNotFoundError, errno.EEXIST: FileExistsError,
                    errno.ENOTDIR: NotADirectoryError, errno.EISDIR: IsADirectoryError}.get(code, OSError)
            raise kind(code, 'NAS SMB 檔案操作失敗，請檢查連線與寫入權限') from None
        except Exception:
            raise OSError(errno.EIO, 'NAS SMB 連線中斷或檔案操作失敗') from None

    def open(self, path, mode, buffering=-1, **kwargs):
        # FFmpeg opens overlapping HTTP ranges while an earlier SMB read is still open.
        if 'r' in mode and '+' not in mode:
            kwargs.setdefault('share_access', 'r')
        return self._call(self.client.open_file, path, mode=mode, buffering=buffering, **kwargs)

    def lock(self, path):
        return self._call(smb_lock, path)

    def stat(self, path, **kwargs):
        return self._call(self.client.stat, path, **kwargs)

    def exists(self, path):
        try:
            self.stat(path)
            return True
        except FileNotFoundError:
            return False

    def isfile(self, path):
        try:
            return stat.S_ISREG(self.stat(path).st_mode)
        except FileNotFoundError:
            return False

    def isdir(self, path):
        try:
            return stat.S_ISDIR(self.stat(path).st_mode)
        except FileNotFoundError:
            return False

    def islink(self, path):
        try:
            return stat.S_ISLNK(self.stat(path, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            return False

    def getsize(self, path):
        return self.stat(path).st_size

    def listdir(self, path):
        return self._call(self.client.listdir, path)

    @contextmanager
    def scandir(self, path):
        """Stream SMB directory entries so bounded callers can stop early."""
        try:
            with self.client.scandir(self._path(path), **self.options) as entries:
                yield entries
        except OSError as exc:
            code = exc.errno or errno.EIO
            kind = {errno.ENOENT: FileNotFoundError, errno.ENOTDIR: NotADirectoryError}.get(code, OSError)
            raise kind(code, 'NAS SMB 目錄離線或無讀取權限') from None
        except Exception:
            raise OSError(errno.EIO, 'NAS SMB 連線中斷或目錄讀取失敗') from None

    def makedirs(self, path, exist_ok=False):
        return self._call(self.client.makedirs, path, exist_ok=exist_ok)

    def mkdtemp(self, *, prefix, dir):
        path = os.path.join(dir, prefix + uuid.uuid4().hex)
        self._call(self.client.mkdir, path)
        return path

    def remove(self, path):
        return self._call(self.client.remove, path)

    def removedirs(self, path):
        # Only remove the task directory, never walk up into the NAS share.
        return self._call(self.client.rmdir, path)

    def rmtree(self, path, ignore_errors=False):
        from smbclient.shutil import rmtree
        return self._call(rmtree, path, ignore_errors=ignore_errors)

    def replace(self, source, destination):
        return self._call(self.client.replace, source, destination)

    def publish(self, source, destination):
        return self._call(self.client.rename, source, destination)

    @contextmanager
    def media_source(self, path):
        """Expose one seekable SMB file to local media tools using a random URL."""
        backend = self
        extension = os.path.splitext(path)[1].lower()
        token = '/' + uuid.uuid4().hex + (extension if re.fullmatch(r'\.[a-z0-9]+', extension) else '')
        failures = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_HEAD(self):
                self.serve(False)

            def do_GET(self):
                self.serve(True)

            def serve(self, body):
                if self.path != token:
                    self.send_error(404)
                    return
                try:
                    size = backend.getsize(path)
                    start, end = 0, size - 1
                    value = self.headers.get('Range')
                    if value:
                        match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
                        if not match or not any(match.groups()):
                            self.send_error(416)
                            return
                        if not match[1]:
                            start = max(0, size - int(match[2]))
                        else:
                            start = int(match[1])
                            if match[2]:
                                end = min(end, int(match[2]))
                        if start > end or start >= size:
                            self.send_response(416)
                            self.send_header('Content-Range', f'bytes */{size}')
                            self.end_headers()
                            return
                    self.send_response(206 if value else 200)
                    self.send_header('Content-Type', 'video/mp2t' if extension == '.ts' else 'video/mp4' if extension == '.mp4' else 'application/octet-stream')
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Content-Length', str(max(0, end - start + 1)))
                    if value:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    self.end_headers()
                    if body:
                        with backend.open(path, 'rb') as stream:
                            stream.seek(start)
                            remaining = end - start + 1
                            while remaining > 0:
                                chunk = stream.read(min(256 * 1024, remaining))
                                if not chunk:
                                    raise OSError(errno.EIO, 'NAS SMB 媒體檔案讀取不完整')
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Media tools may close a range after reading enough metadata.
                except Exception:
                    failures.append(True)
                    self.close_connection = True

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}{token}'
            if failures:
                raise StorageWriteError(path, OSError(errno.EIO, 'NAS SMB 媒體讀取失敗'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def remux_to_mp4(self, source, destination, cancelled):
        import av
        from uav_downloader.core.storage import storage_open

        if cancelled():
            return False
        class MediaIO:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, size=-1):
                if cancelled():
                    raise InterruptedError('MP4 remux cancelled')
                return self.stream.read(size)

            def write(self, data):
                if cancelled():
                    raise InterruptedError('MP4 remux cancelled')
                return self.stream.write(data)

        # Faststart reopens the output to move its index ahead of the media data.
        try:
            with ExitStack() as files:
                def open_media(path, mode, **kwargs):
                    return MediaIO(files.enter_context(storage_open(path, mode, **kwargs)))

                incoming = open_media(source, 'rb')
                outgoing = open_media(destination, 'w+b', share_access='r')
                def reopen_output(url, flags, options):
                    return open_media(destination, 'rb', share_access='rw')
                with av.open(incoming, options={'fflags': '+genpts'}) as media:
                    with av.open(outgoing, mode='w', format='mp4',
                                 io_open=reopen_output,
                                 options={'avoid_negative_ts': 'make_zero', 'movflags': '+faststart'}) as output:
                        streams = {stream.index: output.add_stream_from_template(stream)
                                   for stream in media.streams if stream.type in ('video', 'audio')}
                        if not streams:
                            return False
                        for packet in media.demux():
                            if cancelled():
                                return False
                            if packet.dts is None or packet.stream.index not in streams:
                                continue
                            packet.stream = streams[packet.stream.index]
                            output.mux(packet)
        except InterruptedError:
            if not cancelled():
                raise
            return False
        return not cancelled() and self.getsize(destination) > 0
