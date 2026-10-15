"""SMB files and bounded-memory media streaming, without an OS mount."""
import errno
import ntpath
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from uav_downloader.core.storage import StorageWriteError


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
        return self._call(self.client.open_file, path, mode=mode, buffering=buffering, **kwargs)

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

    def remux_to_mp4(self, source, destination, ffmpeg, cancelled, on_process):
        from uav_downloader.core.storage import storage_open
        with self.media_source(source) as url, tempfile.TemporaryFile() as log:
            probe = subprocess.Popen([ffmpeg, '-nostdin', '-hide_banner', '-i', url],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log)
            on_process(probe)
            try:
                deadline = time.monotonic() + 30
                while probe.poll() is None:
                    if cancelled():
                        return False
                    if time.monotonic() > deadline:
                        raise StorageWriteError(source, OSError(errno.ETIMEDOUT, 'NAS 媒體讀取逾時'))
                    try:
                        probe.wait(timeout=0.3)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if probe.poll() is None:
                    probe.kill()
                probe.wait()
                on_process(None)
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 8192))
            audio_aac = bool(re.search(rb'Audio:\s*aac\b', log.read()))
            log.seek(0)
            log.truncate()
            command = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                       '-fflags', '+genpts', '-i', url, '-c', 'copy']
            # Fragmented MP4 needs AAC headers before its non-seekable output header.
            if audio_aac:
                command += ['-bsf:a', 'aac_adtstoasc']
            command += ['-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                       '-avoid_negative_ts', 'make_zero', '-f', 'mp4', 'pipe:1']
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log)
            on_process(process)
            stop_watch = threading.Event()
            def watch_cancel():
                while not stop_watch.wait(0.2):
                    if cancelled():
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        return
            watcher = threading.Thread(target=watch_cancel, daemon=True)
            watcher.start()
            try:
                with storage_open(destination, 'wb') as output:
                    while not cancelled():
                        chunk = process.stdout.read(256 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                if cancelled():
                    return False
                result = process.wait()
                if result:
                    log.seek(0, os.SEEK_END)
                    log.seek(max(0, log.tell() - 800))
                    print('[SMB 合成] FFmpeg 失敗：' + log.read().decode('utf-8', errors='replace'), flush=True)
                return result == 0 and self.getsize(destination) > 0
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
                stop_watch.set()
                watcher.join(timeout=1)
                on_process(None)
