"""Search intersects source result sets across pages, never just title text."""
from urllib.parse import parse_qs, urlsplit

import pytest

from uav_downloader.daemon.browse_service import BrowseService


@pytest.fixture(autouse=True)
def clear_search_cache(monkeypatch):
    BrowseService._search_term.cache_clear()
    BrowseService._search_tags.cache_clear()
    monkeypatch.setattr(BrowseService, '_search_tags', classmethod(lambda cls, site, bucket: ()))
    yield
    BrowseService._search_term.cache_clear()


def mock_pages(monkeypatch, pages):
    calls = []

    def fetch(site, url, page=1):
        params = parse_qs(urlsplit(url).query)
        term = (params.get('q') or params['s'])[0]
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
    result = BrowseService.search_videos('SupJav', 'first second third')
    assert result['videos'] == [b]
    assert result['total'] == 1
    assert not result['has_more']
    assert not result['warning']
    assert ('second', 2) in calls


def test_phrases_repeated_terms_and_whitespace(monkeypatch):
    calls = mock_pages(monkeypatch, {
        ('actor name', 1): [video('a')], ('字幕', 1): [video('a')],
    })
    result = BrowseService.search_videos('SupJav', '  "actor name"  字幕\t字幕  ')
    assert result['keywords'] == ['actor name', '字幕']
    assert len(result['videos']) == 1
    assert calls.count(('字幕', 1)) == 1


def test_single_keyword_preserves_upstream_pagination(monkeypatch):
    calls = mock_pages(monkeypatch, {('actor name', 3): [video('a')]})
    result = BrowseService.search_videos('SupJav', '"actor name"', page=3)
    assert result['videos'] == [video('a')]
    assert calls == [('actor name', 3)]


def test_intersection_paginates_after_filtering_and_reuses_cache(monkeypatch):
    items = [video(str(i)) for i in range(30)]
    calls = mock_pages(monkeypatch, {('a', 1): items, ('b', 1): list(reversed(items))})
    monkeypatch.setattr('uav_downloader.daemon.browse_service.time.monotonic', lambda: 100)
    first = BrowseService.search_videos('SupJav', 'a b')
    assert first['videos'] == items[:24]
    assert first['has_more']
    count = len(calls)
    second = BrowseService.search_videos('SupJav', 'a b', 2)
    assert second['videos'] == items[24:]
    assert not second['has_more']
    assert len(calls) == count
    assert BrowseService.search_videos('SupJav', 'a b', 3)['videos'] == []


def test_hanime_query_ids_remain_distinct(monkeypatch):
    a = {'url': 'https://hanime1.me/watch?v=1'}
    b = {'url': 'https://hanime1.me/watch?v=2'}
    mock_pages(monkeypatch, {('a', 1): [a], ('b', 1): [b]})
    assert BrowseService.search_videos('SupJav', 'a b')['videos'] == []


@pytest.mark.parametrize('blocked', [False, True])
def test_source_failures_are_not_presented_as_empty_intersections(monkeypatch, blocked):
    def fetch(site, url, page):
        return {'videos': [], 'blocked': blocked, 'error': 'upstream unavailable'}
    monkeypatch.setattr(BrowseService, 'fetch_videos', staticmethod(fetch))
    with pytest.raises(RuntimeError, match='查詢失敗'):
        BrowseService.search_videos('SupJav', 'a b')
    assert BrowseService._search_term.cache_info().currsize == 0


def test_repeated_source_page_reports_incomplete_scan(monkeypatch):
    calls = mock_pages(monkeypatch, {
        (term, page): [video('a')] for term in ('a', 'b') for page in range(1, 5)
    })
    with pytest.raises(RuntimeError, match='無法確認完整搜尋結果'):
        BrowseService.search_videos('SupJav', 'a b')


def test_intersection_scans_beyond_twenty_pages(monkeypatch):
    calls = mock_pages(monkeypatch, {
        (term, page): [video(str(page))] for term in ('a', 'b') for page in range(1, 26)
    })
    result = BrowseService.search_videos('SupJav', 'a b')
    assert result['total'] == 25
    assert result['complete']
    assert not result['warning']
    assert ('a', 25) in calls and ('b', 25) in calls


@pytest.mark.parametrize('query,page', [('"broken', 1), ('   ', 1), ('""', 1), ('a b', 0), ('a b c d e f g h i', 1)])
def test_invalid_search_is_rejected(query, page):
    with pytest.raises(ValueError):
        BrowseService.search_videos('SupJav', query, page)


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
                          data=json.dumps({'site': 'SupJav', 'query': query}).encode(),
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
    result = BrowseService.search_videos('SupJav', "actor's\u3000字幕")
    assert result['keywords'] == ["actor's", '字幕']
    assert result['videos'] == [video('a')]


@pytest.mark.parametrize('site,tag_url', [
    ('MissAV', 'https://missav.ai/genres/%E4%BA%BA%E5%A6%BB'),
    ('SupJav', 'https://supjav.com/tag/married-woman'),
    ('Hanime1', 'https://hanime1.me/search?tags%5B%5D=%E4%BA%BA%E5%A6%BB'),
])
def test_tag_only_matches_participate_in_intersection(monkeypatch, site, tag_url):
    shared = video('shared')
    monkeypatch.setattr(BrowseService, '_search_tags', classmethod(
        lambda cls, site, bucket: (('人妻', tag_url),)))
    calls = []

    def fetch(site_name, url, page):
        calls.append((url, page))
        items = [shared] if page == 1 and url in (
            tag_url, BrowseService.get_search_url(site, 'actor')) else []
        return {'videos': items}

    monkeypatch.setattr(BrowseService, 'fetch_videos', staticmethod(fetch))
    result = BrowseService.search_videos(site, '人妻 actor')
    assert result['videos'] == [shared]
    assert (tag_url, 1) in calls


def test_tag_scan_continues_after_page_already_in_keyword_results(monkeypatch):
    a, b = video('a'), video('b')
    tag_url = 'https://jable.tv/tags/wife/'
    monkeypatch.setattr(BrowseService, '_search_tags', classmethod(
        lambda cls, site, bucket: (('人妻', tag_url),)))

    def fetch(site, url, page):
        pages = {tag_url: {1: [a], 2: [b]},
                 BrowseService.get_search_url(site, '人妻'): {1: [a]}}
        return {'videos': pages.get(url, {}).get(page, [])}

    monkeypatch.setattr(BrowseService, 'fetch_videos', staticmethod(fetch))
    assert BrowseService.search_videos('SupJav', '人妻')['videos'] == [a, b]


def test_tag_source_failure_does_not_silently_drop_tag_matches(monkeypatch):
    tag_url = 'https://jable.tv/tags/wife/'
    monkeypatch.setattr(BrowseService, '_search_tags', classmethod(
        lambda cls, site, bucket: (('人妻', tag_url),)))
    monkeypatch.setattr(BrowseService, 'fetch_videos', staticmethod(
        lambda site, url, page: {'videos': [], 'blocked': url == tag_url}))
    with pytest.raises(RuntimeError, match='查詢失敗'):
        BrowseService.search_videos('SupJav', '人妻 actor')


def test_live_catalog_parsing_and_pagination(monkeypatch):
    from types import SimpleNamespace
    from uav_downloader.daemon import browse_service
    monkeypatch.undo()  # Exercise the real catalog loader, replacing only HTTP.
    monkeypatch.setattr(browse_service.SupJavBrowser, '_get_scraper', lambda: None)
    calls = []

    def fetch(scraper, url, site, validate):
        calls.append(url)
        html = ('<a href="https://supjav.com/zh/tag/married-woman">人妻 (1,234)</a>'
                '<div class="pagination"><a href="/zh/tag/page/2">2</a></div>')
        if url.endswith('/2'):
            html = ('<a href="https://supjav.com/zh/tag/kiss">接吻 (123)</a>'
                    '<div class="pagination"><a href="/zh/tag">1</a></div>')
        response = SimpleNamespace(content=html.encode(), status_code=200)
        assert validate(response)
        return response, 'supjav.com', 'ok'

    monkeypatch.setattr(browse_service, 'fetch_with_mirrors', fetch)
    tags = BrowseService._search_tags('SupJav', 1)
    assert ('人妻', 'https://supjav.com/tag/married-woman') in tags
    assert ('married woman', 'https://supjav.com/tag/married-woman') in tags
    assert ('接吻', 'https://supjav.com/tag/kiss') in tags
    assert all('/page/' not in url for _, url in tags)
    assert len(calls) == 2
    BrowseService._search_tags('SupJav', 1)
    assert len(calls) == 2
    BrowseService._search_tags.cache_clear()


def test_hanime_catalog_builds_tag_filters(monkeypatch):
    from uav_downloader.daemon import browse_service
    monkeypatch.undo()
    monkeypatch.setattr(browse_service.Hanime1Browser, 'fetch_filter_catalog',
                        lambda: {'tags': ('人妻', '中文字幕')})
    tags = BrowseService._search_tags('Hanime1', 1)
    assert parse_qs(urlsplit(tags[0][1]).query) == {'tags[]': ['人妻']}
    BrowseService._search_tags.cache_clear()


def test_jable_catalog_uses_names_and_encoded_slugs(monkeypatch):
    from types import SimpleNamespace
    from uav_downloader.daemon import browse_service
    monkeypatch.undo()
    monkeypatch.setattr(browse_service.JableTVBrowser, '_get_scraper', lambda: None)
    response = SimpleNamespace(status_code=200, content=b'''
        <a href="https://fs1.app/tags/black-pantyhose/">Black Pantyhose</a>
        <a href="https://outside.example/other/">unrelated</a>
    ''')
    monkeypatch.setattr(browse_service, 'fetch_with_mirrors',
                        lambda *args: (response, 'fs1.app', 'ok'))
    urls = BrowseService._search_urls('JableTV', 'black pantyhose', 1)
    assert urls == (BrowseService.get_search_url('JableTV', 'black pantyhose'),
                    'https://jable.tv/tags/black-pantyhose/')
    BrowseService._search_tags.cache_clear()


def test_catalog_failure_is_not_cached(monkeypatch):
    from uav_downloader.daemon import browse_service
    monkeypatch.undo()
    monkeypatch.setattr(browse_service.JableTVBrowser, '_get_scraper', lambda: None)
    monkeypatch.setattr(browse_service, 'fetch_with_mirrors',
                        lambda *args: (None, None, 'blocked'))
    with pytest.raises(RuntimeError, match='標籤目錄查詢失敗'):
        BrowseService.search_videos('SupJav', 'actor 人妻')
    assert BrowseService._search_tags.cache_info().currsize == 0


def test_missav_genre_route_encodes_keyword_without_directory_scan():
    urls = BrowseService._search_urls('MissAV', '人妻', 1)
    assert urls == ('https://missav.ai/search/%E4%BA%BA%E5%A6%BB',
                    'https://missav.ai/genres/%E4%BA%BA%E5%A6%BB')




@pytest.mark.parametrize('query', ['MIMK 吊帶襪', '吊帶襪 MIMK'])
def test_jable_delegates_full_and_query_without_scanning_tags(monkeypatch, query):
    calls = []
    target = video('mimk-054', 'fs1.app')

    def fetch(site, url, page=1):
        calls.append((site, url, page))
        return {'videos': [target], 'blocked': False}

    monkeypatch.setattr(BrowseService, 'fetch_videos', staticmethod(fetch))
    monkeypatch.setattr(BrowseService, '_search_tags', lambda *a: pytest.fail('Native AND must not enumerate tags'))
    assert BrowseService.search_videos('JableTV', query)['videos'] == [target]
    assert len(calls) == 1
    assert parse_qs(urlsplit(calls[0][1]).query)['q'] == [query]
    BrowseService.search_videos('JableTV', query, page=121)
    assert calls[-1][2] == 121


def test_generic_intersection_stops_broad_search_when_candidates_are_proven(monkeypatch):
    target = video('a')
    calls = mock_pages(monkeypatch, {
        ('rare', 1): [target],
        **{('broad', page): [target, video(str(page))] for page in range(1, 122)},
    })
    result = BrowseService.search_videos('SupJav', 'rare broad')
    assert result['videos'] == [target]
    assert result['complete']
    assert max(page for term, page in calls if term == 'broad') <= 2


def test_generic_intersection_finds_matches_after_page_120(monkeypatch):
    target = video('old')
    calls = mock_pages(monkeypatch, {
        ('rare', 1): [target],
        **{('broad', page): [video(str(page))] for page in range(1, 121)},
        ('broad', 121): [target],
    })
    result = BrowseService.search_videos('SupJav', 'rare broad')
    assert result['videos'] == [target]
    assert result['complete']
    assert ('broad', 121) in calls
