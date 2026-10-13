#!/usr/bin/env python
# coding: utf-8
"""Persistent download history management and fuzzy duplicate detection for UAV Downloader."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import difflib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Dict, List, Optional, Tuple, Set
import unicodedata

from uav_downloader.core.paths import product_data_dir, select_portable_state_dir
from uav_downloader.core.video_identity import (
    canonical_code,
    site_from_url,
    video_code,
)

# Common noise tags and quality tokens in adult/anime video titles
_NOISE_PATTERNS = [
    r'\[\s*(?:中文字幕|中文字幕版|中字|繁中|簡中|无码|無碼|無碼流出|无码破解|破壞版|破坏版|AI破解|4K|1080P|720P|HD|FHD|VR|English Subtitles?|Reducing Mosaic|Mosaic Reduced)[^\]]*\]',
    r'【\s*(?:中文字幕|中文字幕版|中字|繁中|簡中|无码|無碼|無碼流出|无码破解|破壞版|破坏版|AI破解|4K|1080P|720P|HD|FHD|VR)[^】]*】',
    r'\(\s*(?:中文字幕|中字|無碼|无码|4K|1080P)[^\)]*\)',
    r'(?i)\b(?:1080p|720p|4k|fhd|hd|uncensored|leak|chinese\s+sub(?:title)?s?)\b',
]


def clean_title_for_matching(title: str) -> str:
    """Normalize and strip version/quality tags for fuzzy comparison."""
    if not title:
        return ''
    text = unicodedata.normalize('NFKC', str(title))
    for pat in _NOISE_PATTERNS:
        text = re.sub(pat, ' ', text, flags=re.IGNORECASE)
    # Strip common site punctuation and dividers
    text = re.sub(r'[\-_/\\|~`!@#$%^&*+=<>?:;"\']', ' ', text)
    # Collapse multiple whitespaces and lowercase
    return ' '.join(text.casefold().split())


def title_similarity(t1: str, t2: str) -> float:
    """Calculate normalized title similarity ratio using SequenceMatcher."""
    c1 = clean_title_for_matching(t1)
    c2 = clean_title_for_matching(t2)
    if not c1 or not c2:
        return 0.0
    if c1 == c2:
        return 1.0
    return difflib.SequenceMatcher(None, c1, c2).ratio()


def is_same_video(
    v1: dict | str,
    v2: dict | str,
    similarity_threshold: float = 0.82,
) -> tuple[bool, str]:
    """Check if two video entries represent the same video.

    Returns:
        (is_same, reason) where reason is 'url', 'code:<val>', or 'fuzzy:<ratio>'.
    """
    v1_dict = v1 if isinstance(v1, dict) else {'url': str(v1), 'title': str(v1)}
    v2_dict = v2 if isinstance(v2, dict) else {'url': str(v2), 'title': str(v2)}

    u1 = str(v1_dict.get('url') or '').strip().casefold()
    u2 = str(v2_dict.get('url') or '').strip().casefold()
    if u1 and u2 and u1 == u2:
        return True, 'url'

    code1 = video_code(v1_dict) or canonical_code(v1_dict.get('title', ''))
    code2 = video_code(v2_dict) or canonical_code(v2_dict.get('title', ''))

    if code1 and code2:
        if code1 == code2:
            return True, f'code:{code1}'
        return False, ''

    # If at least one video lacks a canonical code, fallback to fuzzy title matching
    t1 = str(v1_dict.get('title') or '')
    t2 = str(v2_dict.get('title') or '')
    if t1 and t2:
        sim = title_similarity(t1, t2)
        if sim >= similarity_threshold:
            return True, f'fuzzy:{sim:.2f}'

    return False, ''


class DownloadHistoryManager:
    """Manages persistent history of completed downloads and handles deduplication."""

    DEFAULT_FILENAME = 'download_history.json'

    def __init__(self, history_file: Optional[Path | str] = None):
        if history_file is not None:
            self.history_file = Path(history_file)
        else:
            self.history_file = product_data_dir() / self.DEFAULT_FILENAME

        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._by_url: Dict[str, Dict[str, Any]] = {}
        self._by_code: Dict[str, Dict[str, Any]] = {}
        self.load()

    def _index_entry(self, entry: Dict[str, Any]) -> None:
        url = str(entry.get('url') or '').strip().casefold()
        code = str(entry.get('code') or '').strip().casefold()
        if url:
            self._by_url[url] = entry
        if code:
            self._by_code[code] = entry

    def load(self) -> None:
        """Load history from JSON, or cold-start migrate from CSV/seen.json."""
        with self._lock:
            self._entries.clear()
            self._by_url.clear()
            self._by_code.clear()

            if self.history_file.is_file():
                try:
                    with open(self.history_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    entries = data.get('entries', {}) if isinstance(data, dict) else {}
                    if isinstance(entries, dict):
                        for key, item in entries.items():
                            if isinstance(item, dict):
                                self._entries[key] = item
                                self._index_entry(item)
                    return
                except Exception:
                    pass

            # Cold start migration if history_file does not exist
            self._migrate_cold_start()

    def _migrate_cold_start(self) -> None:
        """Import completed items from download_queue.csv and seen.json."""
        folder = self.history_file.parent

        # 1. Migrate from download_queue.csv
        queue_csv = folder / 'download_queue.csv'
        if not queue_csv.is_file():
            # Check legacy working directory
            cwd_csv = Path(os.getcwd()) / 'download_queue.csv'
            if cwd_csv.is_file():
                queue_csv = cwd_csv
            else:
                jable_csv = Path(os.getcwd()) / 'JableTV.csv'
                if jable_csv.is_file():
                    queue_csv = jable_csv

        if queue_csv.is_file():
            try:
                with open(queue_csv, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        state = row.get('狀態', '').strip()
                        url = row.get('網址', '').strip()
                        title = row.get('名稱', '').strip()
                        dest = row.get('目標', '').strip()
                        if url and state == '已下載':
                            self._add_entry_locked(
                                url=url,
                                title=title,
                                dest_dir=dest,
                                at=datetime.now(timezone.utc).isoformat(),
                            )
            except Exception:
                pass

        # 2. Migrate from seen.json (UAV Watcher)
        seen_path = folder / '.uav-watcher' / 'seen.json'
        if not seen_path.is_file():
            portable_seen = select_portable_state_dir(os.getcwd()) / 'seen.json'
            if portable_seen.is_file():
                seen_path = portable_seen

        if seen_path.is_file():
            try:
                with open(seen_path, 'r', encoding='utf-8') as f:
                    seen_data = json.load(f)
                if isinstance(seen_data, dict):
                    for url, item in seen_data.items():
                        if isinstance(item, dict) and not item.get('skipped'):
                            if url.casefold() not in self._by_url:
                                self._add_entry_locked(
                                    url=url,
                                    title=item.get('title', ''),
                                    dest_dir='',
                                    site=item.get('site', ''),
                                    at=item.get('at', ''),
                                )
            except Exception:
                pass

        if self._entries:
            self._save_locked()

    def _add_entry_locked(
        self,
        url: str,
        title: str = '',
        dest_dir: str = '',
        file_path: str = '',
        site: str = '',
        at: str = '',
    ) -> Dict[str, Any]:
        url_clean = str(url or '').strip()
        site_val = site or site_from_url(url_clean)
        code_val = video_code({'url': url_clean, 'title': title, 'site': site_val}) or canonical_code(title)
        clean_t = clean_title_for_matching(title)
        at_val = at or datetime.now(timezone.utc).isoformat()

        key = code_val or url_clean.casefold()
        existing = self._entries.get(key)
        if existing:
            if dest_dir and not existing.get('dest_dir'):
                existing['dest_dir'] = dest_dir
            if file_path and not existing.get('file_path'):
                existing['file_path'] = file_path
            if title and not existing.get('title'):
                existing['title'] = title
            return existing

        entry = {
            'url': url_clean,
            'title': title,
            'code': code_val,
            'clean_title': clean_t,
            'dest_dir': dest_dir,
            'file_path': file_path,
            'site': site_val,
            'downloaded_at': at_val,
        }
        self._entries[key] = entry
        self._index_entry(entry)
        return entry

    def _save_locked(self) -> None:
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.history_file.with_suffix('.tmp')
            payload = {
                'version': 1,
                'entries': self._entries,
            }
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.history_file)
        except Exception as exc:
            print(f'[History] Failed to save history: {exc}', flush=True)

    def record_download(
        self,
        url: str,
        title: str = '',
        dest_dir: str = '',
        file_path: str = '',
        site: str = '',
    ) -> Dict[str, Any]:
        """Record a completed download to history."""
        with self._lock:
            entry = self._add_entry_locked(
                url=url,
                title=title,
                dest_dir=dest_dir,
                file_path=file_path,
                site=site,
            )
            self._save_locked()
            return entry

    def find_downloaded(
        self,
        video: dict | str,
        similarity_threshold: float = 0.82,
    ) -> Optional[Dict[str, Any]]:
        """Look up whether a video was previously downloaded."""
        v_dict = video if isinstance(video, dict) else {'url': str(video), 'title': str(video)}
        url = str(v_dict.get('url') or '').strip().casefold()
        code = video_code(v_dict) or canonical_code(v_dict.get('title', ''))

        with self._lock:
            # 1. Match by code (100% precision)
            if code and code.casefold() in self._by_code:
                return self._by_code[code.casefold()]

            # 2. Match by exact URL
            if url and url in self._by_url:
                return self._by_url[url]

            # 3. Match by clean title similarity
            title = str(v_dict.get('title') or '')
            if title:
                clean_target = clean_title_for_matching(title)
                if len(clean_target) >= 4:
                    for entry in self._entries.values():
                        entry_clean = entry.get('clean_title') or clean_title_for_matching(entry.get('title', ''))
                        if not entry_clean:
                            continue
                        sim = difflib.SequenceMatcher(None, clean_target, entry_clean).ratio()
                        if sim >= similarity_threshold:
                            return entry

        return None

    def check_video_status(
        self,
        video: dict,
        selected_urls: Optional[Set[str] | List[str]] = None,
        queue_items: Optional[List[Any]] = None,
        similarity_threshold: float = 0.82,
    ) -> Dict[str, Any]:
        """Check all duplicate and download states for a video card.

        Returns a dictionary containing:
            - is_downloaded: bool
            - dest_dir: str
            - downloaded_entry: dict | None
            - is_in_queue: bool
            - queue_state: str
            - is_selected_duplicate: bool
            - duplicate_url: str
            - code: str
        """
        sel_set = set(selected_urls or ())
        current_url = str(video.get('url') or '').strip()
        code = video_code(video) or canonical_code(video.get('title', ''))

        # Check downloaded history
        downloaded = self.find_downloaded(video, similarity_threshold=similarity_threshold)
        is_downloaded = downloaded is not None
        dest_dir = (downloaded.get('dest_dir') or '') if downloaded else ''

        # Check download queue items
        is_in_queue = False
        queue_state = ''
        if queue_items:
            for item in queue_items:
                i_url = getattr(item, 'url', None) or (item.get('url') if isinstance(item, dict) else '')
                i_state = getattr(item, 'state', None) or (item.get('state') if isinstance(item, dict) else '')
                i_name = getattr(item, 'name', None) or (item.get('name') if isinstance(item, dict) else '')

                # Same video in queue
                same, _ = is_same_video(
                    video,
                    {'url': i_url, 'title': i_name},
                    similarity_threshold=similarity_threshold,
                )
                if same:
                    is_in_queue = True
                    queue_state = i_state
                    break

        # Check if another card representing the same video is currently selected
        is_selected_dup = False
        duplicate_url = ''
        if sel_set and current_url not in sel_set:
            for sel_u in sel_set:
                same, _ = is_same_video(
                    video,
                    {'url': sel_u, 'title': ''},
                    similarity_threshold=similarity_threshold,
                )
                if same:
                    is_selected_dup = True
                    duplicate_url = sel_u
                    break

        return {
            'is_downloaded': is_downloaded,
            'dest_dir': dest_dir,
            'downloaded_entry': downloaded,
            'is_in_queue': is_in_queue,
            'queue_state': queue_state,
            'is_selected_duplicate': is_selected_dup,
            'duplicate_url': duplicate_url,
            'code': code,
        }


# Global singleton instance for easy cross-module sharing
_global_history: Optional[DownloadHistoryManager] = None
_global_history_lock = threading.Lock()


def get_history_manager() -> DownloadHistoryManager:
    """Return the global DownloadHistoryManager singleton."""
    global _global_history
    if _global_history is None:
        with _global_history_lock:
            if _global_history is None:
                _global_history = DownloadHistoryManager()
    return _global_history
