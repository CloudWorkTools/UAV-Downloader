"""Unified browse and search service across JableTV, MissAV, SupJav, and Hanime1."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote

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
        search_url = cls.get_search_url(site_name, query)
        return cls.fetch_videos(site_name, search_url, page)
