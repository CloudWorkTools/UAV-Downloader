"""Unified browse and search service across JableTV, MissAV, SupJav, and Hanime1."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import shlex
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

from uav_downloader.sites.hanime1 import (
    Hanime1Browser,
    HANIME1_FILTER_TAG_GROUPS,
    _filter_url,
)
from uav_downloader.sites.jabletv import JableTVBrowser
from uav_downloader.sites.missav import MissAVBrowser
from uav_downloader.sites.supjav import SupJavBrowser
from uav_downloader.sites.base import MirrorsBlockedError

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
        if len(terms) == 1:
            return cls.fetch_videos(site_name, cls.get_search_url(site_name, terms[0]), page)

        # Cache source results briefly so paging does not repeat every upstream request.
        bucket = int(time.monotonic() // 300)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda term: cls._search_term(site_name, term, bucket), terms))
        common = set(results[0][0])
        for videos, _ in results[1:]:
            common.intersection_update(videos)
        videos = [video for key, video in results[0][0].items() if key in common]
        start = (page - 1) * 24
        selected = videos[start:start + 24]
        truncated = any(result[1] for result in results)
        return {
            "site": site_name, "page": page, "videos": selected,
            "count": len(selected), "total": len(videos), "blocked": False,
            "keywords": list(terms), "has_more": start + 24 < len(videos),
            "warning": "每個關鍵字最多比對原站前 20 頁，以下為此範圍的共同結果" if truncated else "",
        }

    @classmethod
    @lru_cache(maxsize=32)
    def _search_term(cls, site_name, term, bucket):
        url = cls.get_search_url(site_name, term)
        videos = {}
        # shortcut: scan at most 20 pages per term; add background indexing for larger searches.
        for page in range(1, 21):
            result = cls.fetch_videos(site_name, url, page)
            if result.get("blocked") or result.get("error"):
                raise RuntimeError(f"關鍵字「{term}」查詢失敗：{result.get('error') or '站台防護阻擋'}")
            added = 0
            for video in result["videos"]:
                parsed = urlsplit(video.get("url", ""))
                if not parsed.path or not video.get("url"):
                    continue
                # Mirror hosts and trailing slashes differ; retain query IDs (Hanime1).
                key = (parsed.path.rstrip('/'), parsed.query)
                if key not in videos:
                    videos[key] = video
                    added += 1
            if not added:
                return videos, False
        return videos, True
