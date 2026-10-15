"""The optional private transport must prove it can retrieve actual site HTML."""
import json
from types import SimpleNamespace

import pytest
import requests

from uav_downloader.core import firecrawl


@pytest.fixture(autouse=True)
def private_settings(monkeypatch, tmp_path):
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    monkeypatch.delenv('UAV_FIRECRAWL_URL', raising=False)
    monkeypatch.delenv('UAV_FIRECRAWL_API_KEY', raising=False)
    firecrawl.save_settings({'enabled': False})


def response(data, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    return result


def success(html='<div class="video-img-box">video</div>', url='https://jable.tv/search/?q=MIMK'):
    return {'success': True, 'data': {'rawHtml': html, 'metadata': {'statusCode': 200, 'url': url}}}


def test_disabled_transport_does_not_contact_service(monkeypatch):
    monkeypatch.setattr(firecrawl.requests, 'post', lambda *a, **kw: pytest.fail('Disabled transport contacted API'))
    assert firecrawl.fallback('https://jable.tv/') is None
    assert firecrawl.probe()['state'] == 'disabled'


def test_enabled_transport_uses_own_service_and_original_html(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return response(success())

    monkeypatch.setattr(firecrawl.requests, 'post', post)
    firecrawl.save_settings({'enabled': True})
    result = firecrawl.fallback('https://jable.tv/search/?q=MIMK')
    assert 'video-img-box' in result.text
    assert result.url.startswith('https://jable.tv/')
    assert calls[0][0] == 'http://uav-firecrawl-api:3002/v2/scrape'
    assert calls[0][1]['json']['formats'] == ['rawHtml']
    assert calls[0][1]['json']['maxAge'] == 0
    assert calls[0][1]['allow_redirects'] is False
    assert firecrawl.probe()['state'] == 'ready'
    firecrawl.save_settings({'enabled': False})
    assert firecrawl.settings()['status']['state'] == 'disabled'


@pytest.mark.parametrize('payload', [
    {'success': False},
    {'success': True, 'data': {'rawHtml': '', 'metadata': {'statusCode': 200}}},
    {'success': True, 'data': {'rawHtml': 'oops', 'metadata': {'statusCode': 403}}},
    success(url='https://outside.example/page'),
])
def test_api_success_alone_does_not_prove_valid_scrape(monkeypatch, payload):
    firecrawl.save_settings({'enabled': True})
    monkeypatch.setattr(firecrawl.requests, 'post', lambda *a, **kw: response(payload))
    assert firecrawl.probe()['state'] == 'unavailable'


def test_challenge_or_wrong_page_is_not_reported_ready(monkeypatch):
    firecrawl.save_settings({'enabled': True})
    monkeypatch.setattr(firecrawl.requests, 'post', lambda *a, **kw: response(success('<html>Just a moment</html>')))
    assert firecrawl.probe()['state'] == 'unavailable'


def test_connection_errors_do_not_disclose_service_address_or_key(monkeypatch):
    firecrawl.save_settings({'enabled': True})

    def post(*args, **kwargs):
        raise requests.ConnectionError('private address and secret token')

    monkeypatch.setattr(firecrawl.requests, 'post', post)
    status = firecrawl.probe()
    assert status['state'] == 'unavailable'
    assert 'secret token' not in status['error']
    assert 'private address' not in status['error']


@pytest.mark.parametrize('url', ['http://jable.tv/', 'https://localhost/', 'https://user:password@jable.tv/'])
def test_scrape_rejects_unsupported_targets(url):
    with pytest.raises(ValueError):
        firecrawl.scrape(url)


def test_mirror_transport_uses_firecrawl_after_direct_failure(monkeypatch):
    from uav_downloader.sites import base
    firecrawl.save_settings({'enabled': True})
    monkeypatch.setattr(base.config, 'MIRRORS', {'jable': ['jable.tv']})
    direct = SimpleNamespace(status_code=403, content=b'Just a moment', headers={}, url='https://jable.tv/')
    monkeypatch.setattr(firecrawl.requests, 'post', lambda *a, **kw: response(success()))
    session = SimpleNamespace(get=lambda *a, **kw: direct)
    result, host, reason = base.fetch_with_mirrors(session, 'https://jable.tv/', 'jable', lambda r: b'video-img-box' in r.content)
    assert reason == 'ok' and host == 'jable.tv'
    assert b'video-img-box' in result.content


def test_hanime_uses_same_optional_transport(monkeypatch):
    from uav_downloader.sites import hanime1
    firecrawl.save_settings({'enabled': True})
    direct = SimpleNamespace(status_code=403, text='Just a moment')
    monkeypatch.setattr(firecrawl.requests, 'post', lambda *a, **kw: response(success(
        '<a href="https://hanime1.me/watch?v=1">Video</a>', 'https://hanime1.me/search')))
    session = SimpleNamespace(get=lambda *a, **kw: direct)
    assert hanime1._request(session, 'https://hanime1.me/search').status_code == 200


def test_settings_api_persists_switch_and_returns_actual_probe(monkeypatch):
    from urllib.request import Request, urlopen
    from uav_downloader.daemon.server import DaemonServer
    from uav_downloader.daemon.task_manager import TaskManager
    monkeypatch.setattr(TaskManager, 'start_dispatcher', lambda self: None)
    monkeypatch.setattr(firecrawl.requests, 'post', lambda *a, **kw: response(success()))
    server = DaemonServer(port=0)
    server.start()
    try:
        endpoint = f'http://127.0.0.1:{server.server.server_address[1]}/api/settings'
        def send(body=None):
            request = Request(endpoint, data=None if body is None else json.dumps(body).encode(),
                              headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=5) as result:
                return json.load(result)
        assert send()['firecrawl']['enabled'] is False
        assert send({'firecrawl': {'enabled': True}})['success']
        assert send()['firecrawl']['enabled'] is True
        assert send({'test_firecrawl': True, 'site': 'JableTV'})['state'] == 'ready'
        assert send()['firecrawl']['status']['site'] == 'JableTV'
        assert 'UAV_FIRECRAWL_API_KEY' not in json.dumps(send())
    finally:
        server.stop()


def test_empty_listing_does_not_trigger_firecrawl_or_become_an_error(monkeypatch):
    from uav_downloader.sites import base
    firecrawl.save_settings({'enabled': True})
    monkeypatch.setattr(base.config, 'MIRRORS', {'jable': ['jable.tv']})
    monkeypatch.setattr(firecrawl.requests, 'post', lambda *a, **kw: pytest.fail('A real empty listing is not a challenge'))
    direct = SimpleNamespace(status_code=200, content=b'<html>No results</html>', headers={}, url='https://jable.tv/')
    assert base.fetch_with_mirrors(SimpleNamespace(get=lambda *a, **kw: direct),
                                   'https://jable.tv/', 'jable', lambda r: False)[2] == 'empty'
