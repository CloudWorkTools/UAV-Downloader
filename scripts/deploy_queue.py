"""Remote queue journal; source is bundled into the SSH installer."""
import json
import fcntl
import os
import subprocess
import time
import urllib.request
from pathlib import Path


class DeploymentQueue:
    def __init__(self, root, url):
        self.path = Path(root) / '.deployment-queue.json'
        self.url = url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_file = open(self.path.parent / ".deployment-queue.lock", "a",
                              opener=lambda path, flags: os.open(path, flags, 0o600))
        fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def restore_service(self):
        if not self.path.exists():
            return
        names = subprocess.check_output(['docker', 'ps', '-a', '--format', '{{.Names}}'], text=True).splitlines()
        name = 'uav-downloader'
        if name not in names and name + '-previous' in names:
            subprocess.run(['docker', 'rename', name + '-previous', name], check=True)
            names.append(name)
        if name in names:
            subprocess.run(['docker', 'start', name], check=True, stdout=subprocess.DEVNULL)

    def call(self, path, body=None):
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=30) as response:
            return json.load(response)

    def tasks(self):
        return {task['task_id']: task for task in self.call('/api/download/tasks')['tasks']}

    def save(self, journal):
        temporary = self.path.with_suffix('.tmp')
        with open(temporary, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(journal, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)
        descriptor = os.open(self.path.parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def pause(self):
        if self.path.exists():
            raise RuntimeError('Queue recovery required: run --recover-queue before deploying')
        before = self.tasks()
        journal = {'identities': {tid: {key: task.get(key) for key in ('url', 'final_dest', 'folder_policy')}
                                  for tid, task in before.items()}, 'resume_ids': []}
        self.save(journal)
        for _ in range(180):
            active = [task for task in self.tasks().values()
                      if task['status'] not in ('completed', 'skipped', 'cancelled', 'failed', 'paused')]
            if not active:
                return
            for task in active:
                if task['status'] in ('pending', 'preparing', 'downloading'):
                    tid = task['task_id']
                    if tid not in journal['resume_ids']:
                        journal['identities'][tid] = {key: task.get(key) for key in ('url', 'final_dest', 'folder_policy')}
                        journal['resume_ids'].append(tid)
                        self.save(journal)  # Persist intent before the request can pause the task.
                    result = self.call('/api/download/action', {'action': 'pause', 'task_id': tid})
                    if not result.get('success'):
                        raise RuntimeError('Queue pause failed; recovery journal retained')
            time.sleep(1)
        raise RuntimeError('Queue did not reach safe pause; recovery journal retained')

    def resume(self):
        if not self.path.exists():
            return
        journal = json.loads(self.path.read_text())
        for attempt in range(30):
            try:
                current = self.tasks()
                break
            except OSError:
                if attempt == 29:
                    raise RuntimeError('Queue unavailable; recovery journal retained') from None
                time.sleep(1)
        if any(tid not in current or any(current[tid].get(key) != value for key, value in identity.items())
               for tid, identity in journal['identities'].items()):
            raise RuntimeError('Queue identity changed; recovery journal retained')
        for tid in list(journal['resume_ids']):
            if current[tid]['status'] == 'paused':
                result = self.call('/api/download/action', {'action': 'resume', 'task_id': tid})
                if not result.get('success'):
                    raise RuntimeError('Queue resume failed; recovery journal retained')
            elif current[tid]['status'] not in ('pending', 'preparing', 'downloading', 'completed', 'skipped', 'cancelled'):
                raise RuntimeError('Queue task cannot resume safely; recovery journal retained')
            journal['resume_ids'].remove(tid)
            self.save(journal)
        self.path.unlink()
