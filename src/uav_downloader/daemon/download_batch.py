"""Preview and confirm one batch's destination without changing preferences."""
import os
import re
import uuid
from contextlib import ExitStack
from datetime import datetime

from uav_downloader.core import config
from uav_downloader.core.storage import storage_fs
from uav_downloader.daemon import nas


def folder_name(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError('資料夾名稱無效')
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip().rstrip('. ')[:100]
    if not value or value in ('.', '..'):
        value = '下載_' + datetime.now().strftime('%Y%m%d')
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value):
        value = '_' + value
    return value


def destination(manager, body, *, commit=False):
    mode = body.get('storage', 'query')
    policy = body.get('policy', 'new')
    naming = body.get('naming', 'date')
    if mode not in ('query', 'settings') or policy not in ('new', 'merge', 'overwrite') or naming not in ('date', 'number', 'custom'):
        raise ValueError('不支援的資料夾選項')
    name = folder_name(body.get('name', '')) if mode == 'query' else ''
    settings = nas.load_settings(secret=True)
    base = os.path.abspath(config._load_prefs().get('download_dir') or os.environ.get('DOWNLOAD_DIR') or 'download')
    if settings.get('enabled') and settings.get('mode') == 'direct':
        base = nas.direct_destination(settings)
    remote = settings.get('enabled') and settings.get('mode') in ('smb', 'upload')
    with ExitStack() as stack:
        root = base
        if remote:
            root = nas.smb_destination()
            # Register the backend without creating folders during preview.
            from uav_downloader.core.smb_storage import SmbStorage
            from uav_downloader.core.storage import registered_storage
            backend = SmbStorage(settings, root)
            stack.callback(backend.close)
            stack.enter_context(registered_storage(root, backend))
        filesystem = storage_fs(root)
        target = os.path.join(root, name) if name else root
        exists = filesystem.exists(target)
        if filesystem.islink(target):
            raise ValueError('資料夾不可使用符號連結')
        if exists and not filesystem.isdir(target):
            raise ValueError('此名稱已被檔案使用，請修改資料夾名稱')
        original_exists = exists
        if name and exists and policy == 'new' and naming != 'custom':
            stem = name + ('_' + datetime.now().strftime('%Y%m%d') if naming == 'date' else '')
            candidate = stem if naming == 'date' else stem + '_2'
            number = 2 if naming == 'date' else 3
            while filesystem.exists(os.path.join(root, candidate)):
                candidate = stem + '_' + str(number)
                number += 1
            name, target = candidate, os.path.join(root, candidate)
        final_settings = dict(settings)
        if name and remote:
            final_settings['directory'] = '/'.join(filter(None, [settings.get('directory', '').replace('\\', '/'), name]))
        if name and settings.get('enabled') and settings.get('mode') == 'direct':
            final_settings['direct_path'] = os.path.join(base, name)
        display = nas.public_destination(final_settings) if remote else os.path.join(base, name) if name else base
        def same_destination(task):
            return (nas.public_destination(task.nas_settings) == display if remote and task.nas_settings.get('enabled') and task.nas_settings.get('mode') in ('smb', 'upload') else os.path.abspath(task.dest) == display)
        with manager._lock:
            busy = any(same_destination(task) and task.status not in ('completed', 'skipped', 'cancelled', 'failed')
                       for task in manager._tasks.values())
        count = sum(filesystem.isfile(os.path.join(target, item)) and not item.startswith('.uav-') for item in filesystem.listdir(target)) if filesystem.isdir(target) else 0
        result = {'success': True, 'name': name, 'destination': display,
                  'base_destination': nas.public_destination(settings) if remote else base, 'exists': original_exists,
                  'file_count': count, 'busy': busy, 'can_submit': not busy and not (name and original_exists and policy == 'new' and naming == 'custom')}
        if not commit:
            return result
        if body.get('expected_destination') != display:
            raise ValueError('儲存位置已變更，請重新確認資料夾')
        if not result['can_submit']:
            raise ValueError('資料夾已有未完成任務或同名項目，請使用新的名稱')
        if name and policy == 'new':
            filesystem.makedirs(target, exist_ok=False)
        else:
            filesystem.makedirs(target, exist_ok=True)
        local = os.path.join(base, name) if name else base
        if remote and settings.get('mode') == 'upload':
            local = os.path.join(base, '.uav-batches', uuid.uuid4().hex)
        if remote and settings.get('mode') == 'smb':
            local = nas.smb_destination()
            final_settings['_smb_path'] = local
        return result, local, final_settings


def submit(manager, body):
    urls = body.get('urls')
    if not isinstance(urls, list) or not urls or len(urls) > 500 or any(not isinstance(url, str) or not url.startswith(('http://', 'https://')) or len(url) > 4096 for url in urls):
        raise ValueError('請提供 1 至 500 個有效影片網址')
    request_id = body.get('request_id')
    if not isinstance(request_id, str) or not request_id or len(request_id) > 100:
        raise ValueError('批次識別碼無效，請重新開啟下載視窗')
    with manager._lock:
        if not hasattr(manager, '_batch_results'):
            manager._batch_results = {}
        if request_id in manager._batch_results:
            previous_body, response = manager._batch_results[request_id]
            if previous_body != body:
                raise ValueError('此批次已加入佇列，請關閉視窗後重新下載')
            return response
        result, dest, settings = destination(manager, body, commit=True)
        previous_ids = set(manager._tasks)
        try:
            tasks = [manager.add_task(url, batch_destination=(dest, settings),
                                      folder_policy=body.get('policy', 'new'), defer_start=True)
                     for url in dict.fromkeys(urls)]
            manager._save_tasks()
        except Exception:
            for task_id in set(manager._tasks) - previous_ids:
                manager._tasks.pop(task_id)
                if task_id in manager._queue:
                    manager._queue.remove(task_id)
            raise
        manager.start_dispatcher()
        response = dict(result, tasks=[task.to_dict() for task in tasks])
        manager._batch_results[request_id] = (dict(body), response)
        if len(manager._batch_results) > 100:
            del manager._batch_results[next(iter(manager._batch_results))]
        return response
