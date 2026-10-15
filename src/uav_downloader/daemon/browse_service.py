"""Unified browse and search service across JableTV, MissAV, SupJav, and Hanime1."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from itertools import count
import re
import shlex
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

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


class BrowseService:
    """Provides uniform API for browsing categories, tags, search results."""

    @staticmethod
    def get_supported_sites() -> List[str]:
        return list(SITES.keys())

    @staticmethod
    def get_categories(site_name: str) -> List[Dict[str, Any]]:
        browser = SITES.get(site_name)
        if not browser:
            raise ValueError(f"不支援的站台: {site_name}")
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
        urls.extend(url for name, url in cls._search_tags(site_name, bucket)
                    if name.casefold() == term.casefold())
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
            raw_videos = browser.fetch_page(page_url)
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
            return {
                "site": site_name,
                "page": page,
                "page_url": page_url,
                "videos": videos,
                "count": len(videos),
                "blocked": False,
            }
        except MirrorsBlockedError:
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
        cls, site_name: str, query: str, page: int = 1
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
        # Cache source results briefly so paging does not repeat every upstream request.
        bucket = int(time.monotonic() // 300)
        cls.get_search_url(site_name, terms[0])
        cls._search_tags(site_name, bucket)
        if len(terms) == 1:
            urls = cls._search_urls(site_name, terms[0], bucket)
            if len(urls) == 1:
                return cls.fetch_videos(site_name, urls[0], page)
        videos = cls._search_term(site_name, terms, bucket)
        start = (page - 1) * 24
        selected = videos[start:start + 24]
        return {
            "site": site_name, "page": page, "videos": selected,
            "count": len(selected), "total": len(videos), "blocked": False,
            "keywords": list(terms), "has_more": start + 24 < len(videos),
            "warning": "", "complete": True,
        }

    @classmethod
    def _search_pages(cls, site_name, term, bucket):
        for url in cls._search_urls(site_name, term, bucket):
            seen = set()
            for page in count(1):
                result = cls.fetch_videos(site_name, url, page)
                if result.get('blocked') or result.get('error'):
                    raise RuntimeError(f"關鍵字「{term}」查詢失敗：{result.get('error') or '站台防護阻擋'}")
                if not result['videos']:
                    break
                videos = {}
                for video in result['videos']:
                    parsed = urlsplit(video.get('url', ''))
                    if not parsed.path or not video.get('url'):
                        continue
                    key = (parsed.path.rstrip('/'), parsed.query)
                    videos[key] = video
                if not videos or not set(videos) - seen:
                    raise RuntimeError('原站重複分頁或回傳無效影片，無法確認完整搜尋結果')
                seen.update(videos)
                yield videos

    @classmethod
    @lru_cache(maxsize=32)
    def _search_term(cls, site_name, terms, bucket):
        # Advance every condition until the smallest completed set supplies all candidates.
        states = [{'pages': cls._search_pages(site_name, term, bucket),
                   'videos': {}, 'done': False} for term in terms]

        def advance(state):
            try:
                state['videos'].update(next(state['pages']))
            except StopIteration:
                state['done'] = True

        with ThreadPoolExecutor(max_workers=4) as pool:
            while not any(state['done'] for state in states):
                list(pool.map(advance, states))
            seed = min((state for state in states if state['done']),
                       key=lambda state: len(state['videos']))
            candidates = dict(seed['videos'])
            for state in sorted(states, key=lambda state: len(state['videos'])):
                while candidates and not state['done'] and not candidates.keys() <= state['videos'].keys():
                    advance(state)
                candidates = {key: video for key, video in candidates.items()
                              if key in state['videos']}
        return list(candidates.values())
