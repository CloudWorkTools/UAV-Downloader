"""Progress follows actual work across request and search worker threads."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import threading

import pytest

from uav_downloader.core import activity
from uav_downloader.daemon.browse_service import BrowseService


@pytest.fixture(autouse=True)
def records(monkeypatch):
    monkeypatch.setattr(activity, '_records', {})


def test_running_work_is_visible_before_response_and_context_isolated():
    started, release = threading.Event(), threading.Event()
    def work():
        with activity.track('搜尋影片', 'MissAV', 'term'):
            activity.report('讀取第 3 頁', part='term')
            started.set()
            assert release.wait(3)
    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert started.wait(3)
        record = activity.snapshot()[0]
        assert record['state'] == 'running'
        assert record['parts'] == {'term': '讀取第 3 頁'}
        assert record['elapsed_seconds'] >= 0
        activity.report('Unrelated thread must not overwrite progress')
        assert activity.snapshot()[0]['message'] == '讀取第 3 頁'
    finally:
        release.set()
        thread.join(3)
    assert activity.snapshot()[0]['state'] == 'completed'


def test_thread_pool_parts_are_reported_and_failures_are_sanitized():
    with activity.track('搜尋影片', 'SupJav'):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(copy_context().run, activity.report, f'條件 {i}', part=str(i)) for i in range(2)]
            for future in futures:
                future.result()
        assert len(activity.snapshot()[0]['parts']) == 2
    with pytest.raises(RuntimeError):
        with activity.track('Firecrawl 測試'):
            raise RuntimeError('private credential details')
    record = activity.snapshot()[0]
    assert record['state'] == 'failed'
    assert 'private credential' not in str(record)


def test_records_expire_and_are_bounded(monkeypatch):
    now = [1000]
    monkeypatch.setattr(activity.time, 'time', lambda: now[0])
    for i in range(140):
        with activity.track(str(i)):
            pass
    assert len(activity.snapshot()) == 128
    now[0] += 61
    assert activity.snapshot() == []


def test_native_list_reports_real_count_and_blocked_state(monkeypatch):
    monkeypatch.setattr('uav_downloader.daemon.browse_service.JableTVBrowser.fetch_page', lambda url: [{'url':'https://jable.tv/videos/test/'}])
    with activity.track('載入影片清單', 'JableTV'):
        BrowseService.fetch_videos('JableTV', 'https://jable.tv/latest-updates/', 2)
    assert '第 2 頁影片清單完成，共 1 筆' in activity.snapshot()[0]['message']


def test_cancellation_interrupts_rate_limit_wait():
    started = threading.Event()
    stopped = threading.Event()
    def work():
        try:
            with activity.track('搜尋影片', job_id='cancel-wait-fixture'):
                started.set()
                activity.wait(60)
        except activity.Cancelled:
            stopped.set()
    thread = threading.Thread(target=work)
    thread.start()
    assert started.wait(2)
    activity.cancel('cancel-wait-fixture')
    thread.join(2)
    assert stopped.is_set() and not thread.is_alive()
    assert not any(item.get('job_id') == 'cancel-wait-fixture' for item in activity.snapshot())


def test_cancel_during_failed_request_prevents_retry_and_fallback():
    from uav_downloader.sites.base import fetch_with_mirrors
    calls = []
    class Session:
        def get(self, url, **kwargs):
            calls.append(url)
            activity.cancel('cancel-network-fixture')
            raise OSError('request interrupted')
    with pytest.raises(activity.Cancelled):
        with activity.track('搜尋影片', job_id='cancel-network-fixture'):
            fetch_with_mirrors(Session(), 'https://jable.tv/search/?q=x', 'jable', lambda response: True)
    assert len(calls) == 1
