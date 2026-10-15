"""Bounded, in-memory progress for work running between HTTP responses."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
import uuid

_lock = threading.RLock()
_current = ContextVar('uav_activity', default=None)
_records = {}


@contextmanager
def track(title, site='', query='', page=1):
    key = uuid.uuid4().hex
    now = time.time()
    with _lock:
        if len(_records) >= 128:
            del _records[next(iter(_records))]
        _records[key] = dict(id=key, title=title, site=site, query=query, page=page,
                             message='準備執行', state='running', started_at=now, updated_at=now, parts={})
    token = _current.set(key)
    try:
        yield
    except Exception:
        report('工作失敗，請查看查詢或設定頁的錯誤訊息', state='failed')
        raise
    finally:
        with _lock:
            record = _records.get(key)
            if record and record['state'] == 'running':
                record['state'] = 'completed'
            if record:
                record['updated_at'] = time.time()
        _current.reset(token)


def report(message, *, part=None, state=None):
    with _lock:
        record = _records.get(_current.get())
        if record is None:
            return
        record['message'] = message
        record['updated_at'] = time.time()
        if part is not None:
            record['parts'][part] = message
        if state:
            record['state'] = state


def snapshot():
    now = time.time()
    with _lock:
        for key, record in list(_records.items()):
            if record['state'] != 'running' and now - record['updated_at'] > 60:
                del _records[key]
        return [dict(record, parts=dict(record['parts']), elapsed_seconds=round(
                    (now if record['state'] == 'running' else record['updated_at']) - record['started_at'], 1))
                for record in reversed(list(_records.values()))]
