#!/usr/bin/env python
# coding: utf-8

import csv
import json
import os
from pathlib import Path
import tempfile

from uav_downloader.core.history import (
    DownloadHistoryManager,
    clean_title_for_matching,
    is_same_video,
    title_similarity,
)


def test_clean_title_for_matching():
    raw1 = '[中文字幕] SSIS-123 漂亮姐姐 1080P 【4K高畫質】'
    cleaned1 = clean_title_for_matching(raw1)
    assert '中文字幕' not in cleaned1
    assert '1080p' not in cleaned1
    assert '4k' not in cleaned1
    assert 'ssis 123' in cleaned1

    raw2 = 'SSIS-123 漂亮姐姐'
    cleaned2 = clean_title_for_matching(raw2)
    assert cleaned1 == cleaned2


def test_is_same_video_code_matching():
    v1 = {
        'url': 'https://jable.tv/videos/ssis-123/',
        'title': '[中文字幕] SSIS-123 漂亮姐姐 1080P',
    }
    v2 = {
        'url': 'https://missav.ai/cn/ssis-123-uncensored-leak',
        'title': 'SSIS-123 【無碼流出】',
    }
    same, reason = is_same_video(v1, v2)
    assert same is True
    assert reason == 'code:ssis-123'


def test_is_same_video_different_codes_never_collide():
    v1 = {
        'url': 'https://jable.tv/videos/ssis-123/',
        'title': 'SSIS-123 漂亮姐姐',
    }
    v2 = {
        'url': 'https://jable.tv/videos/ssis-124/',
        'title': 'SSIS-124 漂亮姐姐',
    }
    same, _ = is_same_video(v1, v2)
    assert same is False


def test_is_same_video_fuzzy_title_without_codes():
    v1 = {
        'url': 'https://hanime1.me/watch?v=1001',
        'title': '【繁中】這個勇者明明超強卻過分慎重 第01話 [1080P]',
    }
    v2 = {
        'url': 'https://hanime1.me/watch?v=1002',
        'title': '這個勇者明明超強卻過分慎重 01',
    }
    same, reason = is_same_video(v1, v2, similarity_threshold=0.80)
    assert same is True
    assert reason.startswith('fuzzy:')


def test_history_manager_record_and_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        history_file = Path(tmpdir) / 'download_history.json'
        mgr = DownloadHistoryManager(history_file=history_file)

        # Record download
        dest_folder = r'D:\Downloads\JAV\SSIS-123'
        mgr.record_download(
            url='https://jable.tv/videos/ssis-123/',
            title='[中文字幕] SSIS-123 漂亮姐姐',
            dest_dir=dest_folder,
            site='JableTV',
        )

        assert history_file.is_file()

        # Re-load in a new manager instance to verify persistence
        mgr2 = DownloadHistoryManager(history_file=history_file)
        match = mgr2.find_downloaded({
            'url': 'https://missav.ai/ssis-123',
            'title': 'SSIS-123 無碼',
        })
        assert match is not None
        assert match['dest_dir'] == dest_folder
        assert match['code'] == 'ssis-123'


def test_history_manager_cold_start_from_csv():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        csv_file = tmp_path / 'download_queue.csv'
        history_file = tmp_path / 'download_history.json'

        # Create dummy download_queue.csv
        with open(csv_file, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['狀態', '名稱', '進度', '速度', '網址', '目標', '字幕來源證據'])
            w.writerow(['已下載', 'IPZZ-905 女優名', '100%', '', 'https://jable.tv/videos/ipzz-905/', r'C:\Videos\IPZZ-905', ''])
            w.writerow(['等待中', 'SSIS-999 未下載', '0%', '', 'https://jable.tv/videos/ssis-999/', r'C:\Videos', ''])

        mgr = DownloadHistoryManager(history_file=history_file)

        # IPZZ-905 was marked '已下載', should be migrated
        match = mgr.find_downloaded({'url': 'https://missav.ai/ipzz-905', 'title': 'IPZZ-905'})
        assert match is not None
        assert match['dest_dir'] == r'C:\Videos\IPZZ-905'

        # SSIS-999 was '等待中', should not be in history
        match_pending = mgr.find_downloaded({'url': 'https://jable.tv/videos/ssis-999/', 'title': 'SSIS-999'})
        assert match_pending is None


def test_check_video_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = DownloadHistoryManager(history_file=Path(tmpdir) / 'test_hist.json')
        mgr.record_download(
            url='https://jable.tv/videos/ssis-100/',
            title='SSIS-100 已下載過',
            dest_dir=r'E:\Media\SSIS-100',
        )

        class DummyQueueItem:
            def __init__(self, url, name, state):
                self.url = url
                self.name = name
                self.state = state

        queue = [
            DummyQueueItem('https://jable.tv/videos/ssis-200/', 'SSIS-200 在佇列', '下載中'),
        ]
        selected = {'https://jable.tv/videos/ssis-300/'}

        # Case 1: Video is downloaded
        s1 = mgr.check_video_status(
            {'url': 'https://missav.ai/ssis-100', 'title': 'SSIS-100 其他站點'},
            selected_urls=selected,
            queue_items=queue,
        )
        assert s1['is_downloaded'] is True
        assert s1['dest_dir'] == r'E:\Media\SSIS-100'

        # Case 2: Video is in queue
        s2 = mgr.check_video_status(
            {'url': 'https://missav.ai/ssis-200', 'title': 'SSIS-200 佇列中'},
            selected_urls=selected,
            queue_items=queue,
        )
        assert s2['is_in_queue'] is True
        assert s2['queue_state'] == '下載中'

        # Case 3: Video is duplicate of another selected card
        s3 = mgr.check_video_status(
            {'url': 'https://missav.ai/ssis-300-leak', 'title': 'SSIS-300 選取重複'},
            selected_urls=selected,
            queue_items=queue,
        )
        assert s3['is_selected_duplicate'] is True
        assert s3['duplicate_url'] == 'https://jable.tv/videos/ssis-300/'
