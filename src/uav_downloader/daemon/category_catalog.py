"""Traditional Chinese browse groups shared with the existing category registry."""
from uav_downloader.apps.watch_categories import SITES
from uav_downloader.i18n.locales import STRINGS
from uav_downloader.i18n.sites import CATEGORY_I18N


def category_groups(site, categories=()):
    if site not in SITES:
        raise ValueError(f'不支援的站台: {site}')
    groups = []
    known = set()
    for group in SITES[site]['groups']:
        group_id = group['id']
        label_key = ('st_hanime_tags_' + group_id.split(':', 1)[1]
                     if group.get('hanime_tag_group') else 'st_group_' + group_id)
        items = []
        for target in group['targets']:
            url = target['url']
            items.append({'name': CATEGORY_I18N.get(url, {}).get('zh', target['name']), 'url': url})
            known.add(url)
        groups.append({'id': group_id, 'name': STRINGS['zh'].get(label_key, group['name']), 'items': items})
    extra = []
    for category in categories:
        if category['url'] not in known:
            extra.append(dict(category, name=CATEGORY_I18N.get(category['url'], {}).get('zh', category['name'])))
            known.add(category['url'])
    if extra:
        groups.append({'id': 'site-categories', 'name': '網站其他分類', 'items': extra})
    return groups
