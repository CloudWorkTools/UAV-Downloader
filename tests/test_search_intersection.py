"""Search intersects source result sets across pages, never just title text."""
from urllib.parse import parse_qs, urlsplit

import pytest

from uav_downloader.daemon.browse_service import BrowseService


@pytest.fixture(autouse=True)
def clear_search_cache():
    BrowseService._search_term.cache_clear()
    yield
    BrowseService._search_term.cache_clear()


def mock_pages(monkeypatch, pages):
    calls = []

    def fetch(site, url, page=1):
        term = parse_qs(urlsplit(url).query)['q'][0]
        calls.append((term, page))
        return {'videos': pages.get((term, page), []), 'blocked': False}

    monkeypatch.setattr(BrowseService, 'fetch_videos', staticmethod(fetch))
    return calls


def video(name, host='jable.tv', suffix='/'):
    return {'url': f'https://{host}/videos/{name}{suffix}', 'title': 'No keywords in title'}


def test_three_terms_intersect_across_source_pages_and_mirrors(monkeypatch):
    a, b, c = video('a'), video('b'), video('c')
    calls = mock_pages(monkeypatch, {
        ('first', 1): [a, b], ('first', 2): [c, a],
        ('second', 1): [c], ('second', 2): [video('b', 'fs1.app', '')],
        ('third', 1): [b, a],
    })
    result = BrowseService.search_videos('JableTV', 'first second third')
    assert result['videos'] == [b]
    assert result['total'] == 1
    assert not result['has_more']
    assert not result['warning']
    assert ('second', 2) in calls


def test_phrases_repeated_terms_and_whitespace(monkeypatch):
    calls = mock_pages(monkeypatch, {
        ('actor name', 1): [video('a')], ('字幕', 1): [video('a')],
    })
    result = BrowseService.search_videos('JableTV', '  "actor name"  字幕\t字幕  ')
    assert result['keywords'] == ['actor name', '字幕']
    assert len(result['videos']) == 1
    assert calls.count(('字幕', 1)) == 1


def test_single_keyword_preserves_upstream_pagination(monkeypatch):
    calls = mock_pages(monkeypatch, {('actor name', 3): [video('a')]})
    result = BrowseService.search_videos('JableTV', '"actor name"', page=3)
    assert result['videos'] == [video('a')]
    assert calls == [('actor name', 3)]


def test_intersection_paginates_after_filtering_and_reuses_cache(monkeypatch):
    items = [video(str(i)) for i in range(30)]
    calls = mock_pages(monkeypatch, {('a', 1): items, ('b', 1): list(reversed(items))})
    monkeypatch.setattr('uav_downloader.daemon.browse_service.time.monotonic', lambda: 100)
    first = BrowseService.search_videos('JableTV', 'a b')
    assert first['videos'] == items[:24]
    assert first['has_more']
    count = len(calls)
    second = BrowseService.search_videos('JableTV', 'a b', 2)
    assert second['videos'] == items[24:]
    assert not second['has_more']
    assert len(calls) == count
    assert BrowseService.search_videos('JableTV', 'a b', 3)['videos'] == []


def test_hanime_query_ids_remain_distinct(monkeypatch):
    a = {'url': 'https://hanime1.me/watch?v=1'}
    b = {'url': 'https://hanime1.me/watch?v=2'}
    mock_pages(monkeypatch, {('a', 1): [a], ('b', 1): [b]})
    assert BrowseService.search_videos('JableTV', 'a b')['videos'] == []


@pytest.mark.parametrize('blocked', [False, True])
def test_source_failures_are_not_presented_as_empty_intersections(monkeypatch, blocked):
    def fetch(site, url, page):
        return {'videos': [], 'blocked': blocked, 'error': 'upstream unavailable'}
    monkeypatch.setattr(BrowseService, 'fetch_videos', staticmethod(fetch))
    with pytest.raises(RuntimeError, match='查詢失敗'):
        BrowseService.search_videos('JableTV', 'a b')
    assert BrowseService._search_term.cache_info().currsize == 0


def test_repeated_source_page_stops_scan(monkeypatch):
    calls = mock_pages(monkeypatch, {
        (term, page): [video('a')] for term in ('a', 'b') for page in range(1, 5)
    })
    assert len(BrowseService.search_videos('JableTV', 'a b')['videos']) == 1
    assert len(calls) == 4


def test_page_limit_is_visible(monkeypatch):
    calls = mock_pages(monkeypatch, {
        (term, page): [video(str(page))] for term in ('a', 'b') for page in range(1, 21)
    })
    result = BrowseService.search_videos('JableTV', 'a b')
    assert result['total'] == 20
    assert '20 頁' in result['warning']
    assert len(calls) == 40


@pytest.mark.parametrize('query,page', [('"broken', 1), ('   ', 1), ('""', 1), ('a b', 0), ('a b c d e f g h i', 1)])
def test_invalid_search_is_rejected(query, page):
    with pytest.raises(ValueError):
        BrowseService.search_videos('JableTV', query, page)


@pytest.mark.parametrize('query,expected', [('a b', 200), ('"broken', 400)])
def test_search_http_endpoint(monkeypatch, tmp_path, query, expected):
    import json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    from uav_downloader.daemon.server import DaemonServer

    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    mock_pages(monkeypatch, {('a', 1): [video('shared')], ('b', 2): [video('shared')],
                             ('b', 1): [video('other')]})
    server = DaemonServer(port=0)
    server.start()
    try:
        port = server.server.server_address[1]
        request = Request(f'http://127.0.0.1:{port}/api/search',
                          data=json.dumps({'site': 'JableTV', 'query': query}).encode(),
                          headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            assert response.status == expected
            body = json.load(response)
        if expected == 200:
            assert body['videos'] == [video('shared')]
        else:
            assert '雙引號' in body['error']
    finally:
        server.stop()


def test_apostrophes_and_full_width_spaces(monkeypatch):
    mock_pages(monkeypatch, {("actor's", 1): [video('a')], ('字幕', 1): [video('a')]})
    result = BrowseService.search_videos('JableTV', "actor's\u3000字幕")
    assert result['keywords'] == ["actor's", '字幕']
    assert result['videos'] == [video('a')]
