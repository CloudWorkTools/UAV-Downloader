"""Verify batch append against the disposable Samba share used by CI."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from uav_downloader.core import config
from uav_downloader.core.storage import storage_fs
from uav_downloader.daemon import download_batch, nas
from uav_downloader.daemon import task_manager as tm


nas.save_settings(dict(enabled=True, mode='smb', host='storage-check', share='check',
                       directory='', username='smbtest', password='smb-ci-only'))
config.set_subtitle_pref('none')
config.set_download_cover(False)
settings = nas.load_settings(secret=True)
root = nas.smb_destination()
name = '.uav-append-中文-' + uuid.uuid4().hex
manager = tm.TaskManager(state_path=Path(os.environ['CONFIG_DIR']) / 'append-tasks.json')
manager.start_dispatcher = lambda: None


def submit(request_id, urls, policy='merge'):
    body = dict(name=name, policy=policy, request_id=request_id, urls=urls)
    preview = download_batch.destination(manager, body)
    return download_batch.submit(manager, dict(body, expected_destination=preview['destination']))


def media_fixture(url, savepath, **kwargs):
    code = 'MIMK-290' if '290' in url else 'MIMK-291'
    def download():
        target = os.path.join(savepath, code + '.mp4')
        with storage_fs(target).open(target, 'xb') as stream:
            stream.write(code.encode())
        return True
    return SimpleNamespace(dest_folder=lambda: savepath, target_name=lambda: code,
                           is_url_vaildate=lambda: True, start_download=download,
                           cancel_download=lambda **kw: None, _cancel_job=False)


tm.M3U8Sites.CreateSite = media_fixture
with nas.open_smb_storage(settings, root):
    filesystem = storage_fs(root)
    target = os.path.join(root, name)
    try:
        first = submit('first', ['https://missav.ai/mimk-290'])
        second = submit('second', ['https://jable.tv/videos/mimk-290/', 'https://missav.ai/mimk-291'])
        assert [task['status'] for task in second['tasks']] == ['skipped', 'pending']
        tasks = [manager.get_task(result['tasks'][-1]['task_id']) for result in (first, second)]
        assert tasks[0].dest != tasks[1].dest
        assert tasks[0].to_dict()['final_dest'] == tasks[1].to_dict()['final_dest']
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(manager._run_task, tasks))
        assert [task.status for task in tasks] == ['completed', 'completed'], [task.error for task in tasks]
        third = submit('third', ['https://missav.ai/mimk-292'])
        assert manager.cancel_task(third['tasks'][0]['task_id'])
        overwrite = submit('overwrite', ['https://missav.ai/mimk-293'], policy='overwrite')
        blocked = download_batch.destination(manager, dict(name=name, policy='merge'))
        assert blocked['overwrite_busy'] and not blocked['can_submit']
        assert manager.cancel_task(overwrite['tasks'][0]['task_id'])
        restored = tm.TaskManager(state_path=manager._state_path)
        assert sorted(task.status for task in restored._tasks.values()) == [
            'cancelled', 'cancelled', 'completed', 'completed', 'skipped']
        for code in ('MIMK-290', 'MIMK-291'):
            with filesystem.open(os.path.join(target, code + '.mp4'), 'rb') as stream:
                assert stream.read() == code.encode()
    finally:
        if filesystem.exists(target):
            filesystem.rmtree(target)
    assert not filesystem.exists(target), 'SMB append acceptance left its test folder behind'
print('batch append SMB: PASS (append, duplicates, concurrent publish, cancel, overwrite guard, reload, content, cleanup)')
