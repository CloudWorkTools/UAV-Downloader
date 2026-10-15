"""Unified browse and search service across JableTV, MissAV, SupJav, and Hanime1."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import lru_cache
import re
import shlex
import time
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
from uav_downloader.core import activity

from uav_downloader.sites.hanime1 import (
    Hanime1Browser,
    HANIME1_FILTER_TAG_GROUPS,
    _filter_url,
)
from uav_downloader.sites.jabletv import JableTVBrowser
from uav_downloader.sites.missav import MissAVBrowser
from uav_downloader.sites.supjav import SupJavBrowser
from uav_downloader.sites.base import MirrorsBlockedError, fetch_with_mirrors

SITES = {
    "JableTV": JableTVBrowser,
    "MissAV": MissAVBrowser,
    "SupJav": SupJavBrowser,
    "Hanime1": Hanime1Browser,
}


class _SearchSession:
    """Freeze returned pages while advancing only enough sources for the next one."""

    def __init__(self, service, site, terms, bucket):
        self.service, self.site, self.terms, self.bucket = service, site, terms, bucket
        self.lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.states = [{'pages': self.service._search_pages(self.site, term, self.bucket),
                        'videos': {}, 'done': False} for term in self.terms]
        self.pages, self.buffer, self.matches = [], [], {}
        self.complete, self.failed = False, False

    @staticmethod
    def _advance(state):
        try:
            state['videos'].update(next(state['pages']))
        except StopIteration:
            state['done'] = True

    def get_page(self, page):
        activity.report('接續上次的標題與標籤交集比對')
        with self.lock:
            if self.failed:
                pages, buffer, matches = self.pages, self.buffer, self.matches
                self._reset()
                self.pages, self.buffer, self.matches = pages, buffer, matches
            try:
                rounds = 0
                with ThreadPoolExecutor(max_workers=4) as pool:
                    while len(self.pages) < page:
                        if self.buffer:
                            self.pages.append(self.buffer[:24])
                            del self.buffer[:24]
                            continue
                        if self.complete or rounds >= 4:
                            break
                        finished = [state for state in self.states if state['done']]
                        candidates = set.intersection(*(set(state['videos']) for state in finished)) if finished else None
                        active = [state for state in self.states if not state['done'] and
                                  (candidates is None or not candidates <= state['videos'].keys())]
                        futures = [pool.submit(copy_context().run, self._advance, state) for state in active]
                        for future in futures:
                            future.result()
                        rounds += 1
                        common = set.intersection(*(set(state['videos']) for state in self.states))
                        for key, video in self.states[0]['videos'].items():
                            if key in common and key not in self.matches:
                                self.matches[key] = video
                                self.buffer.append(video)
                        finished = [state for state in self.states if state['done']]
                        if finished:
                            candidates = set.intersection(*(set(state['videos']) for state in finished))
                            self.complete = candidates <= common
            except Exception:
                self.failed = True
                raise
            selected = self.pages[page - 1] if page <= len(self.pages) else []
            pending = page > len(self.pages) and not self.complete
            activity.report(f'已確認 {len(self.matches)} 筆交集結果' + ('，繼續比對中' if pending else '，本頁已就緒'))
            return dict(videos=selected[:], count=len(selected), total=len(self.matches) if self.complete else None,
                        complete=self.complete, pending=pending,
                        has_more=page < len(self.pages) or bool(self.buffer) or not self.complete,
                        warning='' if self.complete else '下一頁會接續搜尋，已顯示的結果均符合全部條件。')


class BrowseService:
    """Provides uniform API for browsing categories, tags, search results."""

    _catalog_lock = threading.RLock()

    @staticmethod
    def get_supported_sites() -> List[str]:
        return list(SITES.keys())

    @staticmethod
    def get_categories(site_name: str) -> List[Dict[str, Any]]:
        browser = SITES.get(site_name)
        if not browser:
            raise ValueError(f"不支援的站台: {site_name}")
        activity.report('讀取站台分類與標籤目錄')
        return browser.fetch_categories()

    @staticmethod
    def get_page_url(site_name: str, base_url: str, page: int) -> str:
        if page <= 1:
            return base_url
        if site_name == "JableTV":
            if "?" in base_url:
                return f"{base_url}&from={page}"
            return f"{base_url.rstrip('/')}/?from={page}"
        elif site_name == "MissAV":
            return MissAVBrowser.page_url(base_url, page)
        elif site_name == "SupJav":
            return SupJavBrowser.page_url(base_url, page)
        elif site_name == "Hanime1":
            return Hanime1Browser.page_url(base_url, page)
        return base_url

    @staticmethod
    def get_search_url(site_name: str, query: str) -> str:
        q = quote(query.strip(), safe="")
        if site_name == "JableTV":
            return f"https://jable.tv/search/?q={q}"
        elif site_name == "MissAV":
            return f"https://missav.ai/search/{q}"
        elif site_name == "SupJav":
            return SupJavBrowser.search_url(query)
        elif site_name == "Hanime1":
            return _filter_url(query=query.strip())
        raise ValueError(f"不支援的站台: {site_name}")

    @classmethod
    @lru_cache(maxsize=16)
    def _search_tags(cls, site_name, bucket):
        if site_name == "MissAV":
            return ()
        if site_name == "Hanime1":
            return tuple((tag, _filter_url(**{'tags[]': [tag]}))
                         for tag in Hanime1Browser.fetch_filter_catalog()['tags'])
        catalogs = {
            "JableTV": ("jable", "https://jable.tv/tags/", "/tags/"),
            "SupJav": ("supjav", "https://supjav.com/zh/tag", "/tag/"),
        }
        site_key, url, path = catalogs[site_name]
        selector = f'a[href*="{path}"]'
        tags = []
        pending = [url]
        visited = set()
        while pending:
            catalog_url = pending.pop(0)
            if catalog_url in visited:
                continue
            visited.add(catalog_url)
            activity.report(f'讀取標籤目錄第 {len(visited)} 頁')
            response, _, reason = fetch_with_mirrors(
                SITES[site_name]._get_scraper(), catalog_url, site_key,
                lambda resp: resp.status_code == 200 and bool(
                    BeautifulSoup(resp.content, 'html.parser').select(selector)))
            if reason != 'ok':
                raise RuntimeError("標籤目錄查詢失敗，請稍後重試")
            soup = BeautifulSoup(response.content, 'html.parser')
            for anchor in soup.select(selector):
                parsed = urlsplit(urljoin(url, anchor['href']))
                # Ignore catalog pagination and keep mirror links on the canonical host.
                if not re.fullmatch(r'(?:/zh)?' + path + r'[^/]+/?', parsed.path):
                    continue
                target = urljoin(url, parsed.path)
                if site_name == "SupJav":
                    target = target.replace('/zh/tag/', '/tag/')
                name = re.sub(r'\s*\([\d,]+\)\s*$', '', anchor.get_text(' ', strip=True))
                tags.append((name, target))
                tags.append((unquote(parsed.path.rstrip('/').rsplit('/', 1)[-1]).replace('-', ' '), target))
            for anchor in soup.select('.pagination a[href], a[rel="next"][href]'):
                parsed = urlsplit(urljoin(catalog_url, anchor['href']))
                if re.fullmatch(r'/zh/tag/page/\d+/?', parsed.path):
                    pending.append(urljoin(url, parsed.path))
        return tuple(dict.fromkeys(tags))

    @classmethod
    def _search_urls(cls, site_name, term, bucket):
        urls = [cls.get_search_url(site_name, term)]
        if site_name == "MissAV":
            # Genre names are URL keys; the directory itself spans dozens of pages.
            urls.append(f"https://missav.ai/genres/{quote(term, safe='')}")
        with cls._catalog_lock:
            tags = cls._search_tags(site_name, bucket)
        urls.extend(url for name, url in tags if name.casefold() == term.casefold())
        return tuple(dict.fromkeys(urls))

    @classmethod
    def fetch_videos(
        cls, site_name: str, base_url: str, page: int = 1
    ) -> Dict[str, Any]:
        browser = SITES.get(site_name)
        if not browser:
            raise ValueError(f"不支援的站台: {site_name}")

        page_url = cls.get_page_url(site_name, base_url, page)
        try:
            activity.report(f'讀取第 {page} 頁影片清單')
            raw_videos = browser.fetch_page(page_url)
            activity.report(f'整理第 {page} 頁影片資料')
            videos = []
            for item in raw_videos or []:
                videos.append({
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "thumbnail": item.get("thumbnail", ""),
                    "duration": item.get("duration", ""),
                    "date": item.get("date", ""),
                    "author": item.get("author", ""),
                })
            activity.report(f'第 {page} 頁影片清單完成，共 {len(videos)} 筆')
            return {
                "site": site_name,
                "page": page,
                "page_url": page_url,
                "videos": videos,
                "count": len(videos),
                "blocked": False,
            }
        except MirrorsBlockedError:
            activity.report('站台防護阻擋，無法取得清單', state='failed')
            return {
                "site": site_name,
                "page": page,
                "page_url": page_url,
                "videos": [],
                "count": 0,
                "blocked": True,
                "error": "Cloudflare 或站台防護阻擋",
            }
        except Exception as exc:
            activity.report('影片清單讀取失敗', state='failed')
            return {
                "site": site_name,
                "page": page,
                "page_url": page_url,
                "videos": [],
                "count": 0,
                "blocked": False,
                "error": str(exc),
            }

    @classmethod
    def search_videos(
        cls, site_name: str, query: str, page: int = 1, search_id: Optional[str] = None
    ) -> Dict[str, Any]:
        if page < 1:
            raise ValueError("頁碼必須大於零")
        try:
            lexer = shlex.shlex(query, posix=True)
            lexer.whitespace_split = True
            lexer.whitespace += "\u3000\u00a0"
            lexer.commenters = ""
            lexer.quotes = '"'
            terms = tuple(dict.fromkeys(lexer))
        except ValueError as exc:
            raise ValueError('完整詞組的雙引號必須成對') from exc
        terms = tuple(term for term in terms if term.strip())
        if not terms:
            raise ValueError("請提供搜尋關鍵字")
        if len(terms) > 8:
            raise ValueError("一次最多查詢 8 個關鍵字")
        if site_name == 'JableTV':
            # Jable's native AND search covers titles and tags without scanning tag pages.
            native_query = ' '.join('"' + term + '"' if any(c.isspace() for c in term) else term
                                    for term in terms)
            return cls.fetch_videos(site_name, cls.get_search_url(site_name, native_query), page)
        if search_id is not None and (not isinstance(search_id, str) or not re.fullmatch(r'\d{1,16}', search_id)):
            raise ValueError('搜尋接續識別碼無效')
        bucket = int(search_id) if search_id is not None else int(time.monotonic() // 300)
        cls.get_search_url(site_name, terms[0])
        if len(terms) == 1:
            urls = cls._search_urls(site_name, terms[0], bucket)
            if len(urls) == 1:
                return cls.fetch_videos(site_name, urls[0], page)
        result = cls._search_term(site_name, terms, bucket).get_page(page)
        return dict(result, site=site_name, page=page, blocked=False,
                    keywords=list(terms), search_id=str(bucket))

    @classmethod
    def _search_pages(cls, site_name, term, bucket):
        native = cls.get_search_url(site_name, term)
        sources = [{'url': native, 'page': 1, 'seen': set(), 'done': False}]
        tags_loaded = False
        while not tags_loaded or any(not source['done'] for source in sources):
            for source in sources:
                if source['done']:
                    continue
                page = source['page']
                activity.report(f'條件「{term}」：讀取第 {page} 頁', part=term)
                result = cls.fetch_videos(site_name, source['url'], page)
                if result.get('blocked') or result.get('error'):
                    raise RuntimeError(f"關鍵字「{term}」查詢失敗：{result.get('error') or '站台防護阻擋'}")
                if not result['videos']:
                    source['done'] = True
                    activity.report(f'條件「{term}」：此來源已讀完 {page - 1} 頁', part=term)
                    if not tags_loaded or any(not item['done'] for item in sources):
                        yield {}
                    continue
                videos = {}
                for video in result['videos']:
                    parsed = urlsplit(video.get('url', ''))
                    if not parsed.path or not video.get('url'):
                        continue
                    key = (parsed.path.rstrip('/'), parsed.query)
                    videos[key] = video
                if not videos or not set(videos) - source['seen']:
                    raise RuntimeError('原站重複分頁或回傳無效影片，無法確認完整搜尋結果')
                source['seen'].update(videos)
                source['page'] += 1
                activity.report(f'條件「{term}」：已讀第 {page} 頁，累計 {len(source["seen"])} 筆', part=term)
                yield videos
            if not tags_loaded:
                tags = [{'url': url, 'page': 1, 'seen': set(), 'done': False}
                        for url in cls._search_urls(site_name, term, bucket) if url != native]
                sources = tags + sources
                tags_loaded = True

    @classmethod
    @lru_cache(maxsize=32)
    def _search_term(cls, site_name, terms, bucket):
        return _SearchSession(cls, site_name, terms, bucket)
