"""Bounded, in-memory progress for work running between HTTP responses."""
from contextlib import contextmanager
from collections import OrderedDict
from contextvars import ContextVar
import threading
import time
import uuid

_lock = threading.RLock()
_current = ContextVar('uav_activity', default=None)
_records = {}
_jobs = OrderedDict()
_stops = ContextVar('uav_stops', default=())


class Cancelled(Exception):
    pass


def wait(seconds):
    events = _stops.get()
    if not events:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check_cancelled()
        events[0].wait(min(0.1, max(0, deadline - time.monotonic())))
    check_cancelled()


def check_cancelled():
    if any(event.is_set() for event in _stops.get()):
        raise Cancelled('查詢已取消或已無交集')


@contextmanager
def stop_when(event):
    token = _stops.set((*_stops.get(), event))
    try:
        yield
    finally:
        _stops.reset(token)


def _job(job_id):
    with _lock:
        event = _jobs.setdefault(job_id, threading.Event())
        _jobs.move_to_end(job_id)
        while len(_jobs) > 128:
            _jobs.popitem(last=False)
        return event


def cancel(job_id):
    with _lock:
        _job(job_id).set()
        for key, record in list(_records.items()):
            if record.get('job_id') == job_id:
                del _records[key]


@contextmanager
def track(title, site='', query='', page=1, job_id=None):
    key = uuid.uuid4().hex
    now = time.time()
    with _lock:
        if len(_records) >= 128:
            del _records[next(iter(_records))]
        _records[key] = dict(id=key, title=title, site=site, query=query, page=page,
                             job_id=job_id, message='準備執行', state='running', started_at=now, updated_at=now, parts={})
    token = _current.set(key)
    stops = _stops.set((_job(job_id),) if job_id else ())
    try:
        check_cancelled()
        yield
    except Cancelled:
        with _lock:
            _records.pop(key, None)
        raise
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
        _stops.reset(stops)


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
