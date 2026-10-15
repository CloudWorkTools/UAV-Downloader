"""Optional HTML transport for the application's private Firecrawl stack."""

import os
import threading
import time
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from uav_downloader.core import config, activity

_lock = threading.Lock()
_status = {}
_PROBES = {
    'JableTV': ('https://jable.tv/search/?q=MIMK', 'div.video-img-box'),
    'MissAV': ('https://missav.ai/search/MIMK-054', 'div.thumbnail'),
    'SupJav': ('https://supjav.com/?s=MIMK-054', 'div.post'),
    'Hanime1': ('https://hanime1.me/search', 'a[href*="watch?v="]'),
}


def settings():
    default = os.environ.get('UAV_FIRECRAWL_ENABLED', '0') == '1'
    enabled = config._load_prefs().get('firecrawl_enabled', default) is True
    with _lock:
        status = dict(_status)
    return {'enabled': enabled, 'status': status if enabled else {'state': 'disabled'}}


def save_settings(value):
    if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
        raise ValueError('Firecrawl 開關必須為布林值')
    with config._prefs_lock:
        prefs = config._load_prefs()
        prefs['firecrawl_enabled'] = value['enabled']
        config._save_prefs(prefs)
    with _lock:
        _status.clear()


def scrape(url):
    """Return a requests-compatible HTML response, never generated or partial text."""
    activity.report('透過獨立 Firecrawl 擷取原站網頁')
    parsed = urlsplit(url)
    allowed = {host for hosts in config.MIRRORS.values() for host in hosts}
    allowed.update({'hanime1.me', 'example.com'})
    if parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.username or parsed.password:
        raise ValueError('Firecrawl 僅允許支援站台的 HTTPS 網頁')
    base = os.environ.get('UAV_FIRECRAWL_URL', 'http://uav-firecrawl-api:3002').rstrip('/')
    headers = {}
    key = os.environ.get('UAV_FIRECRAWL_API_KEY')
    if key:
        headers['Authorization'] = 'Bearer ' + key
    try:
        result = requests.post(
            base + '/v2/scrape', headers=headers,
            json={'url': url, 'formats': ['rawHtml'], 'onlyMainContent': False,
                  'maxAge': 0, 'timeout': 60000}, timeout=(5, 70), allow_redirects=False,
            proxies={'http': '', 'https': ''})
        result.raise_for_status()
        payload = result.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
            raise RuntimeError('Firecrawl 回應格式無效')
        data = payload.get('data') or {}
        metadata = data.get('metadata') or {}
        if not isinstance(metadata, dict):
            raise RuntimeError('Firecrawl 回應缺少有效的頁面資料')
        html = data.get('rawHtml')
        final_url = metadata.get('url') or metadata.get('sourceURL') or url
        final = urlsplit(final_url)
        if (payload.get('success') is not True or metadata.get('statusCode') != 200
                or metadata.get('error') or not isinstance(html, str) or not html.strip()
                or final.scheme != 'https' or final.hostname not in allowed):
            raise RuntimeError('Firecrawl 未回傳有效的原站 HTML')
        response = requests.Response()
        response.status_code = 200
        response.url = final_url
        response.encoding = 'utf-8'
        response._content = html.encode('utf-8')
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        return response
    except (requests.RequestException, ValueError) as exc:
        # API exception messages can contain credentials or private service addresses.
        raise RuntimeError('Firecrawl 無法連線或擷取失敗，請測試服務狀態') from exc


def fallback(url):
    if settings()['enabled']:
        return scrape(url)
    return None


@activity.track('Firecrawl 站台測試')
def probe(site='JableTV'):
    activity.report(f'測試 {site} 的實際影片清單')
    if site not in _PROBES:
        raise ValueError('不支援的測試站台')
    if not settings()['enabled']:
        return {'state': 'disabled', 'site': site}
    with _lock:
        _status.clear()
        _status.update({'state': 'checking', 'site': site})
    url, selector = _PROBES[site]
    try:
        response = scrape(url)
        if not BeautifulSoup(response.content, 'html.parser').select_one(selector):
            raise RuntimeError('服務有回應，但未取得此站影片清單；可能遭站台防護阻擋')
        status = {'state': 'ready', 'site': site, 'checked_at': time.time()}
    except RuntimeError as exc:
        status = {'state': 'unavailable', 'site': site, 'error': str(exc), 'checked_at': time.time()}
    with _lock:
        _status.clear()
        _status.update(status)
    return status
