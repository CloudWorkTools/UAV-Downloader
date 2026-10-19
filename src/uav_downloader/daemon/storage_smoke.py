"""Exercise batch SMB registrations and concurrent I/O on an isolated NAS folder."""
import argparse
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

from uav_downloader.core.storage import storage_fs
from uav_downloader.daemon import nas
from uav_downloader.daemon.task_manager import TaskManager


def check_batch_smb(settings):
    if not settings.get('enabled') or settings.get('mode') != 'smb':
        return False
    root = nas.smb_destination()
    with nas.open_smb_storage(settings, root):
        filesystem = storage_fs(root)
        temporary = filesystem.mkdtemp(prefix='.uav-batch-check-', dir=root)
        try:
            child = dict(settings, directory='/'.join(filter(None, [settings.get('directory', '').replace('\\', '/'), os.path.basename(temporary)])))
            manager = TaskManager()
            batch_destination = (nas.smb_destination(), child)
            tasks = [manager.add_task(f'https://example.invalid/storage-check/{index}',
                                      batch_destination=batch_destination, folder_policy='new',
                                      subtitle_mode='none', enable_subtitles=False, download_cover=False,
                                      staging_dir='', defer_start=True) for index in range(2)]
            if any(task.status != 'pending' for task in tasks):
                raise ValueError('Batch tasks were not queued independently')
            with ExitStack() as connections:
                for task in tasks:
                    connections.enter_context(nas.open_smb_storage(task.nas_settings, task.dest))
                if tasks[0].to_dict()['final_dest'] != tasks[1].to_dict()['final_dest']:
                    raise ValueError('Batch destinations differ')
                names = [f'check-{task.task_id}.bin' for task in tasks]
                contents = [b'uav-batch-first', b'uav-batch-second']

                def write(index):
                    target = os.path.join(tasks[index].dest, names[index])
                    with storage_fs(target).open(target, 'xb') as stream:
                        if stream.write(contents[index]) != len(contents[index]):
                            raise OSError('Short write')

                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(write, range(2)))
                for index in range(2):
                    target = os.path.join(tasks[1-index].dest, names[index])
                    with storage_fs(target).open(target, 'rb') as stream:
                        if stream.read() != contents[index]:
                            raise OSError('Cross-connection read mismatch')
        finally:
            filesystem.rmtree(temporary)
        if filesystem.exists(temporary):
            raise OSError('Storage check cleanup failed')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-smb', action='store_true')
    args = parser.parse_args()
    try:
        checked = check_batch_smb(nas.load_settings(secret=True))
        if args.require_smb and not checked:
            raise ValueError('SMB is required')
    except Exception:
        print('SMB concurrent batch write/read/cleanup: FAILED', flush=True)
        raise SystemExit(1) from None
    print('SMB concurrent batch write/read/cleanup: ' + ('OK' if checked else 'SKIPPED (not SMB mode)'), flush=True)


if __name__ == '__main__':
    main()
