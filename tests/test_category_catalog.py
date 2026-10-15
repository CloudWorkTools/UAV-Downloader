from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from uav_downloader.apps.watch_categories import SITES
from uav_downloader.daemon.category_catalog import category_groups
from uav_downloader.sites.jabletv import JableTVBrowser


@pytest.mark.parametrize('site', SITES)
def test_catalog_retains_all_registered_targets_without_mutating_registry(site):
    before = deepcopy(SITES[site]['groups'])
    groups = category_groups(site)
    assert sum(len(group['items']) for group in groups) == sum(len(group['targets']) for group in before)
    assert {item['url'] for group in groups for item in group['items']} == {item['url'] for group in before for item in group['targets']}
    assert groups[0]['name'] == '動態 / 榜單'
    assert SITES[site]['groups'] == before


def test_jable_sidebar_and_new_live_categories_are_available():
    url = 'https://jable.tv/categories/new-category/'
    groups = category_groups('JableTV', [{'name': '新分類', 'url': url}, {'name': '重複', 'url': url}])
    tags = {group['name']: group['items'] for group in groups if group['id'].startswith('tags:')}
    assert set(tags) == set(JableTVBrowser.SIDEBAR_TAGS)
    for name, items in tags.items():
        assert len(items) == len(JableTVBrowser.SIDEBAR_TAGS[name])
    assert groups[-1]['items'] == [{'name': '新分類', 'url': url}]


def test_hanime_tag_urls_keep_query_parameters():
    group = next(group for group in category_groups('Hanime1') if group['id'] == 'hanime-tags:features')
    item = next(item for item in group['items'] if item['name'] == '中文字幕')
    assert parse_qs(urlsplit(item['url']).query)['tags[]'] == ['中文字幕']


def test_unknown_site_is_rejected():
    with pytest.raises(ValueError):
        category_groups('unknown')
