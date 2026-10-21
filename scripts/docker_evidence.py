#!/usr/bin/env python3
"""Record private evidence for isolated Docker checks without adopting resources."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

OWNER_LABEL = 'io.uav-downloader.evidence-owner'
DEFAULT_EVIDENCE_ROOT = Path.home() / '.local' / 'state' / 'uav-docker-evidence'
MAX_TIMEOUT_SECONDS = 24 * 60 * 60


class EvidenceError(Exception):
    pass


class Interrupted(EvidenceError):
    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f'interrupted by signal {signum}')


class Evidence:
    def __init__(self, root: Path, kind: str):
        root = root.expanduser().absolute()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise EvidenceError('evidence root must be owned by this user and private')
        self.run_id = str(uuid.uuid4())
        self.path = root / self.run_id
        self.path.mkdir(mode=0o700)
        self.path.chmod(0o700)
        self.token = secrets.token_urlsafe(32)
        self.manifest_path = self.path / 'manifest.json'
        self.events_path = self.path / 'events.jsonl'
        self.manifest = {
            'schema_version': 1,
            'run_id': self.run_id,
            'kind': kind,
            'created_at': self.now(),
            'status': 'preparing',
            'owner_label': OWNER_LABEL,
            'owner_token': self.token,
            'daemon_context_fingerprint': None,
            'resources': [],
        }
        self._save_manifest()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _save_manifest(self) -> None:
        temp = self.path / '.manifest.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(self.manifest, f, sort_keys=True, indent=2)
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.manifest_path)
            os.chmod(self.manifest_path, 0o600)
            dir_fd = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if temp.exists():
                temp.unlink()

    def update(self, **fields) -> None:
        self.manifest.update(fields)
        self._save_manifest()

    def add_resource(self, resource: dict) -> None:
        self.manifest['resources'].append(resource)
        self._save_manifest()

    def event(self, operation: str, phase: str, **fields) -> None:
        record = {'time': self.now(), 'monotonic_ns': time.monotonic_ns(),
                  'operation': operation, 'phase': phase, **fields}
        fd = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'a') as f:
                f.write(json.dumps(record, sort_keys=True) + '\n')
                f.flush()
                os.fsync(f.fileno())
        finally:
            os.chmod(self.events_path, 0o600)

    def private_file(self, name: str, data: str, *, exclusive: bool = False) -> None:
        path = self.path / name
        flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
        fd = os.open(path, flags, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(path, 0o600)


class Docker:
    def __init__(self, evidence: Evidence):
        self.evidence = evidence

    def save_stderr(self, call_id: str, operation: str, stderr: str) -> None:
        safe_operation = re.sub(r'[^A-Za-z0-9._-]+', '_', operation)
        self.evidence.private_file(f'docker-{call_id}-{safe_operation}.stderr', stderr, exclusive=True)

    def save_payload_logs(self, call_id: str, prefix: str, stdout: str, stderr: str) -> None:
        self.evidence.private_file(f'{prefix}-{call_id}.stdout', stdout, exclusive=True)
        self.evidence.private_file(f'{prefix}-{call_id}.stderr', stderr, exclusive=True)
        self.evidence.private_file(f'{prefix}.stdout', stdout)
        self.evidence.private_file(f'{prefix}.stderr', stderr)

    @staticmethod
    def operation(args: list[str]) -> str:
        if args[:2] == ['image', 'inspect']:
            return 'image.inspect'
        if args[:2] == ['volume', 'create']:
            return 'volume.create'
        if args[:2] == ['volume', 'inspect']:
            return 'volume.inspect'
        if args[:2] == ['volume', 'rm']:
            return 'volume.remove'
        if args[:2] == ['context', 'show']:
            return 'context.show'
        if args[:2] == ['context', 'inspect']:
            return 'context.inspect'
        return args[0] if args else 'docker'

    def call(self, args: list[str], *, timeout: float = 30, save_logs: bool = False,
             log_prefix: str = 'payload') -> subprocess.CompletedProcess:
        op = self.operation(args)
        call_id = uuid.uuid4().hex
        signature = hashlib.sha256(json.dumps(args).encode()).hexdigest()
        self.evidence.event(op, 'start', call_id=call_id, args_sha256=signature)
        try:
            proc = subprocess.Popen(['docker', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            self.evidence.event(op, 'end', call_id=call_id, args_sha256=signature,
                                failure_type=type(exc).__name__)
            raise EvidenceError('could not start the Docker CLI') from None
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            self.evidence.event(op, 'end', call_id=call_id, args_sha256=signature, timeout=True, signal=None)
            self.save_stderr(call_id, op, stderr)
            if save_logs:
                self.save_payload_logs(call_id, log_prefix, stdout, stderr)
            raise TimeoutError(op)
        except Interrupted as exc:
            proc.kill()
            stdout, stderr = proc.communicate()
            self.evidence.event(op, 'end', call_id=call_id, args_sha256=signature,
                                timeout=False, signal=exc.signum)
            self.save_stderr(call_id, op, stderr)
            if save_logs:
                self.save_payload_logs(call_id, log_prefix, stdout, stderr)
            raise
        self.evidence.event(op, 'end', call_id=call_id, args_sha256=signature, exit_code=proc.returncode)
        if proc.returncode:
            self.save_stderr(call_id, op, stderr)
        if save_logs:
            self.save_payload_logs(call_id, log_prefix, stdout, stderr)
        return subprocess.CompletedProcess(['docker', *args], proc.returncode, stdout, stderr)


def parse_json(result: subprocess.CompletedProcess, message: str):
    if result.returncode:
        raise EvidenceError(message)
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise EvidenceError(message) from None


def daemon_fingerprint(docker: Docker, *, save: bool = True) -> str:
    context_result = docker.call(['context', 'show'])
    if context_result.returncode:
        raise EvidenceError('could not read Docker context')
    context_name = context_result.stdout.strip()
    context = parse_json(docker.call(['context', 'inspect', context_name]), 'could not inspect Docker context')
    endpoint = context[0].get('Endpoints', {}).get('docker', {}).get('Host', '') if context else ''
    info = parse_json(docker.call(['info', '--format', '{{json .}}']), 'could not read Docker daemon identity')
    data = {'context_name': context_name,
            'endpoint_sha256': hashlib.sha256(endpoint.encode()).hexdigest(),
            'daemon_id': info.get('ID'), 'daemon_name': info.get('Name'),
            'docker_root': info.get('DockerRootDir'), 'server_version': info.get('ServerVersion')}
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    if save:
        docker.evidence.update(daemon_context_fingerprint=fingerprint)
    return fingerprint


def inspect_container(docker: Docker, identifier: str) -> dict:
    data = parse_json(docker.call(['inspect', identifier]), 'could not inspect owned container')
    if not isinstance(data, list) or not data:
        raise EvidenceError('owned container inspect returned no resource')
    return data[0]


def safe_mount(mount: dict) -> dict:
    fields = ('Type', 'Name', 'Source', 'Destination', 'Driver', 'Mode', 'RW', 'Propagation')
    return {key: mount.get(key) for key in fields if key in mount}


def safe_container_path(value: str) -> bool:
    return (value.startswith('/') and re.fullmatch(r'/[A-Za-z0-9._/-]*', value) is not None
            and str(PurePosixPath(value)) == value
            and all(part not in ('.', '..') for part in PurePosixPath(value).parts))


def owned_container(docker: Docker, resource: dict, ev: Evidence) -> dict:
    if not resource.get('id'):
        raise EvidenceError('resource has no recorded exact ID; preserving it')
    obj = inspect_container(docker, resource['id'])
    labels = (obj.get('Config') or {}).get('Labels') or {}
    if obj.get('Id') != resource['id'] or obj.get('Name', '').lstrip('/') != resource.get('name'):
        raise EvidenceError('resource identity changed; preserving it')
    if labels.get(OWNER_LABEL) != ev.token or resource.get('labels', {}).get(OWNER_LABEL) != ev.token:
        raise EvidenceError('resource ownership label does not match; preserving it')
    return obj


def image_metadata(docker: Docker, image: str, ev: Evidence) -> dict:
    raw = parse_json(docker.call(['image', 'inspect', image]), 'could not inspect target image')
    if not isinstance(raw, list) or not raw:
        raise EvidenceError('image inspect returned no image')
    obj = raw[0]
    config = obj.get('Config') or {}
    entrypoint = config.get('Entrypoint') or []
    cmd = config.get('Cmd') or []
    user = str(config.get('User') or '')
    safe_program = None
    if entrypoint:
        base = Path(str(entrypoint[0])).name
        if re.fullmatch(r'[A-Za-z0-9._+-]+', base):
            safe_program = base
    selected = {}
    for item in config.get('Env') or []:
        key, sep, value = str(item).partition('=')
        if not sep:
            continue
        if key in ('CONFIG_DIR', 'APPDATA', 'DOWNLOAD_DIR'):
            if not value.startswith('/'):
                raise EvidenceError(f'image {key} must be an absolute container path')
            selected[key] = value
        elif key in ('PUID', 'PGID'):
            if not value.isdecimal():
                raise EvidenceError(f'image {key} must be a non-negative integer')
            selected[key] = int(value)
    image_id = obj.get('Id')
    metadata = {
        'image_id': image_id,
        'entrypoint_program': safe_program,
        'entrypoint_sha256': hashlib.sha256(json.dumps(entrypoint).encode()).hexdigest(),
        'cmd_sha256': hashlib.sha256(json.dumps(cmd).encode()).hexdigest(),
        'default_user_is_set': bool(user),
        'default_user_sha256': hashlib.sha256(user.encode()).hexdigest(),
        'declared_volume_paths': sorted((config.get('Volumes') or {}).keys()),
        'selected_defaults': selected,
    }
    ev.update(image_id=image_id, image_metadata=metadata)
    return metadata


def create_container(docker: Docker, ev: Evidence, image_id: str, command: list[str], *,
                     env: dict[str, str] | None = None, mounts: list[tuple[str, str, bool]] | None = None,
                     name_suffix: str = 'run') -> dict:
    name = f'uav-evidence-{ev.run_id[:12]}-{name_suffix}'
    labels = {OWNER_LABEL: ev.token}
    resource = {'kind': 'container', 'id': None, 'name': name, 'labels': labels,
                'mounts': [], 'mounts_recorded': False, 'removed': False}
    ev.add_resource(resource)
    args = ['create', '--network', 'none', '--name', name, '--label', f'{OWNER_LABEL}={ev.token}']
    for key, value in (env or {}).items():
        args.extend(['--env', f'{key}={value}'])
    for volume, destination, readonly in (mounts or []):
        spec = f'type=volume,src={volume},dst={destination}' + (',readonly' if readonly else '')
        args.extend(['--mount', spec])
    if ev.manifest['kind'] == 'preflight':
        sentinel = ev.path / 'client-sentinel'
        args.extend(['--mount', f'type=bind,src={sentinel},dst=/run/uav-client-sentinel,readonly'])
    args.extend([image_id, *command])
    result = docker.call(args)
    if result.returncode:
        raise EvidenceError('Docker could not create the owned probe container')
    identifier = result.stdout.strip()
    if not re.fullmatch(r'[a-fA-F0-9]{12,64}|container-[0-9]+', identifier):
        raise EvidenceError('Docker create returned an invalid resource ID')
    resource['id'] = identifier
    ev._save_manifest()
    obj = inspect_container(docker, identifier)
    labels_from_inspect = (obj.get('Config') or {}).get('Labels') or {}
    if obj.get('Id') != identifier or obj.get('Name', '').lstrip('/') != name or labels_from_inspect.get(OWNER_LABEL) != ev.token:
        raise EvidenceError('new container did not retain its exact ownership identity')
    resource['mounts'] = [safe_mount(x) for x in obj.get('Mounts') or []]
    resource['mounts_recorded'] = True
    resource['state'] = (obj.get('State') or {}).get('Status')
    ev._save_manifest()
    return resource


def capture_logs(docker: Docker, ev: Evidence, resource: dict, prefix: str) -> None:
    try:
        result = docker.call(['logs', resource['id']], save_logs=True, log_prefix=prefix)
        resource['logs_status'] = 'captured' if result.returncode == 0 else 'failed'
    except Interrupted:
        raise
    except (EvidenceError, TimeoutError):
        resource['logs_status'] = 'failed'
    ev._save_manifest()


def resolve_stopped_state(docker: Docker, ev: Evidence, resource: dict) -> tuple[int | None, str]:
    try:
        state = ensure_stopped(docker, ev, resource)
    except (EvidenceError, TimeoutError):
        resource['state'] = 'pending'
        ev._save_manifest()
        return None, 'pending'
    if state.get('Status') == 'exited':
        capture_logs(docker, ev, resource, resource['name'].rsplit('-', 1)[-1]
                     if ev.manifest['kind'] == 'preflight' else 'payload')
        return state.get('ExitCode'), 'exited'
    return None, state.get('Status', 'pending')


def start_and_wait(docker: Docker, ev: Evidence, resource: dict, timeout: float) -> tuple[int | None, str]:
    identifier = resource['id']
    prefix = resource['name'].rsplit('-', 1)[-1] if ev.manifest['kind'] == 'preflight' else 'payload'
    try:
        start = docker.call(['start', identifier])
        if start.returncode:
            raise EvidenceError('owned container failed to start')
        ev.update(status='running')
        wait = docker.call(['wait', identifier], timeout=timeout)
        if wait.returncode:
            raise EvidenceError('Docker wait failed; container state is unknown')
        code = int(wait.stdout.strip())
        state = inspect_container(docker, identifier).get('State') or {}
        resource['state'] = state.get('Status')
        resource['exit_code'] = state.get('ExitCode')
        ev._save_manifest()
        capture_logs(docker, ev, resource, prefix)
        return code, state.get('Status', 'unknown')
    except TimeoutError:
        ev.update(status='timeout')
        return resolve_stopped_state(docker, ev, resource)
    except Interrupted as exc:
        ev.update(status='interrupted', signal=exc.signum)
        return resolve_stopped_state(docker, ev, resource)


def ensure_stopped(docker: Docker, ev: Evidence, resource: dict) -> dict:
    obj = owned_container(docker, resource, ev)
    state = obj.get('State') or {}
    if state.get('Running'):
        if not on_expected_daemon(docker, ev.manifest.get('daemon_context_fingerprint')):
            raise EvidenceError('Docker daemon/context changed; owned payload remains pending')
        stopped = docker.call(['stop', '--time', '5', resource['id']], timeout=10)
        resource['stop_result'] = 'requested' if stopped.returncode == 0 else 'failed'
    obj = owned_container(docker, resource, ev)
    state = obj.get('State') or {}
    resource['state'] = state.get('Status')
    resource['exit_code'] = state.get('ExitCode')
    if state.get('Running'):
        ev.manifest['cleanup_status'] = 'pending'
        resource['cleanup_reason'] = 'payload_still_running'
    ev._save_manifest()
    return state


def volume_metadata(docker: Docker, name: str, ev: Evidence) -> dict:
    resource = {'kind': 'volume', 'id': None, 'name': name,
                'labels': {OWNER_LABEL: ev.token}, 'mounts': [],
                'removed': False}
    ev.add_resource(resource)
    result = docker.call(['volume', 'create', '--label', f'{OWNER_LABEL}={ev.token}', name])
    if result.returncode or result.stdout.strip() != name:
        raise EvidenceError('Docker could not create the owned scratch volume')
    resource['id'] = name
    ev._save_manifest()
    raw = parse_json(docker.call(['volume', 'inspect', name]), 'could not inspect owned scratch volume')
    if not raw or raw[0].get('Name') != name or (raw[0].get('Labels') or {}).get(OWNER_LABEL) != ev.token:
        raise EvidenceError('new scratch volume did not retain its exact ownership identity')
    resource['driver'] = raw[0].get('Driver')
    resource['labels'] = raw[0].get('Labels') or {}
    resource['mountpoint'] = raw[0].get('Mountpoint')
    resource['scope'] = raw[0].get('Scope')
    ev._save_manifest()
    return resource


def referenced_volume_names(docker: Docker) -> set[str] | None:
    ps = docker.call(['ps', '-a', '-q', '--no-trunc'])
    if ps.returncode:
        return None
    refs = set()
    for identifier in filter(None, ps.stdout.splitlines()):
        try:
            obj = parse_json(docker.call(['inspect', identifier]), 'could not inspect containers for volume references')
            if not obj:
                return None
            for mount in obj[0].get('Mounts') or []:
                if mount.get('Type') == 'volume':
                    refs.add(mount.get('Name') or mount.get('Source'))
        except EvidenceError:
            return None
    return refs


def on_expected_daemon(docker: Docker, fingerprint: str | None) -> bool:
    if not fingerprint:
        return False
    try:
        return daemon_fingerprint(docker, save=False) == fingerprint
    except EvidenceError:
        return False


def cleanup_resources(docker: Docker, ev: Evidence) -> list[str]:
    retained = []
    expected = ev.manifest.get('daemon_context_fingerprint')
    if not on_expected_daemon(docker, expected):
        retained = [resource.get('id') or resource.get('name', 'unknown')
                    for resource in ev.manifest['resources'] if not resource.get('removed')]
        ev.update(cleanup_status='pending', retained_resources=retained)
        return retained
    for resource in ev.manifest['resources']:
        if resource.get('kind') != 'container':
            continue
        if resource.get('removed'):
            retained.extend(f'volume:{name}' for name in resource.get('retained_mounts', []))
            continue
        try:
            obj = owned_container(docker, resource, ev)
            if not resource.get('mounts_recorded'):
                retained.append(resource['id'])
                resource['cleanup_reason'] = 'mounts_not_recorded'
                ev._save_manifest()
                continue
            if (obj.get('State') or {}).get('Running'):
                retained.append(resource['id'])
                resource['cleanup_reason'] = 'running_container_preserved'
                ev._save_manifest()
                continue
            if resource.get('logs_status') == 'failed':
                retained.append(resource['id'])
                resource['cleanup_reason'] = 'payload_logs_unavailable'
                ev._save_manifest()
                continue
            if not on_expected_daemon(docker, expected):
                retained.append(resource['id'])
                continue
            removed = docker.call(['rm', resource['id']])
            if removed.returncode:
                retained.append(resource['id'])
                continue
            resource['removed'] = True
            resource['state'] = 'removed'
            mount_names = sorted({mount.get('Name') for mount in resource.get('mounts', [])
                                  if mount.get('Type') == 'volume' and mount.get('Name')})
            unowned_mount_names = [name for name in mount_names
                                   if not any(item.get('kind') == 'volume' and item.get('name') == name
                                              for item in ev.manifest['resources'])]
            resource['retained_mounts'] = unowned_mount_names
            retained.extend(f'volume:{name}' for name in unowned_mount_names)
            ev._save_manifest()
        except (EvidenceError, TimeoutError):
            retained.append(resource.get('id') or resource['name'])

    refs = referenced_volume_names(docker) if any(resource.get('kind') == 'volume' and not resource.get('removed')
                                                  for resource in ev.manifest['resources']) else None
    for resource in ev.manifest['resources']:
        if resource.get('kind') != 'volume' or resource.get('removed'):
            continue
        name = resource.get('id')
        try:
            if not name:
                retained.append(resource.get('name', 'unknown'))
                continue
            if refs is None or name in refs:
                retained.append(name or resource['name'])
                continue
            raw = parse_json(docker.call(['volume', 'inspect', name]), 'could not inspect owned scratch volume')
            if (not raw or raw[0].get('Name') != resource.get('name') or
                    (raw[0].get('Labels') or {}).get(OWNER_LABEL) != ev.token):
                retained.append(name)
                continue
            if not on_expected_daemon(docker, expected):
                retained.append(name)
                continue
            removed = docker.call(['volume', 'rm', name])
            if removed.returncode:
                retained.append(name)
                continue
            resource['removed'] = True
            ev._save_manifest()
        except (EvidenceError, TimeoutError):
            retained.append(name or resource['name'])
    ev.update(cleanup_status='complete' if not retained else 'pending', retained_resources=retained)
    return retained


def pending_resources(ev: Evidence) -> list[dict]:
    return [{'kind': resource.get('kind'), 'id': resource.get('id'), 'name': resource.get('name'),
             'mounts_recorded': resource.get('mounts_recorded', resource.get('kind') == 'volume'),
             'state': resource.get('state')}
            for resource in ev.manifest['resources'] if not resource.get('removed')]


def add_log_status(summary: dict, resource: dict | None) -> None:
    if not resource or not resource.get('logs_status'):
        return
    summary['logs_status'] = resource['logs_status']
    if resource['logs_status'] == 'failed':
        summary['logs_warning'] = 'Docker logs were unavailable; inspect the private evidence directory.'


def new_volume_name(ev: Evidence, suffix: str) -> str:
    return f'uav-evidence-{ev.run_id[:12]}-{suffix}'


def preflight(args) -> int:
    ev = Evidence(args.evidence_root, 'preflight')
    docker = Docker(ev)
    summary = {'run_id': ev.run_id, 'evidence_path': str(ev.path), 'status': 'failed'}
    try:
        daemon_fingerprint(docker)
        metadata = image_metadata(docker, args.image, ev)
        config_dir = metadata['selected_defaults'].get('CONFIG_DIR', '/config')
        appdata_dir = metadata['selected_defaults'].get('APPDATA', '/appdata')
        downloads_dir = metadata['selected_defaults'].get('DOWNLOAD_DIR', '/downloads')
        if not all(safe_container_path(path) for path in (config_dir, appdata_dir, downloads_dir)):
            raise EvidenceError('image path configuration is not safe for an isolated mount probe')
        summary.update({'daemon_fingerprint': ev.manifest['daemon_context_fingerprint'],
                        'image_id': metadata['image_id'], 'entrypoint_program': metadata['entrypoint_program'],
                        'entrypoint_sha256': metadata['entrypoint_sha256'],
                        'default_user_is_set': metadata['default_user_is_set'],
                        'default_puid': metadata['selected_defaults'].get('PUID', 1000),
                        'default_pgid': metadata['selected_defaults'].get('PGID', 1000),
                        'config_dir_is_default': config_dir == '/config',
                        'appdata_dir_is_default': appdata_dir == '/appdata'})
        if args.probe:
            sentinel = ev.path / 'client-sentinel'
            if ',' in str(sentinel) or '=' in str(sentinel):
                raise EvidenceError('evidence path cannot be represented as an exact Docker bind source')
            sentinel.write_text(secrets.token_hex(32) + '\n')
            sentinel.chmod(0o444)
            config_name = new_volume_name(ev, 'config')
            appdata_name = new_volume_name(ev, 'appdata')
            downloads_name = new_volume_name(ev, 'downloads')
            for name in (config_name, appdata_name, downloads_name):
                volume_metadata(docker, name, ev)
            mounts = [(name, dest, False) for name, dest in (
                (config_name, config_dir), (appdata_name, appdata_dir), (downloads_name, downloads_dir))]
            uid = metadata['selected_defaults'].get('PUID', 1000) if args.uid is None else args.uid
            gid = metadata['selected_defaults'].get('PGID', 1000) if args.gid is None else args.gid
            precondition = {'appdata_owner_prepared': {'uid': uid, 'gid': gid}}
            ev.update(probe_preconditions=precondition)
            bootstrap = create_container(docker, ev, metadata['image_id'],
                ['python', '-c', f"import os; os.makedirs({appdata_dir!r}, exist_ok=True); os.chown({appdata_dir!r}, {uid}, {gid})"],
                env={'PUID': '0', 'PGID': '0', 'CONFIG_DIR': config_dir, 'APPDATA': appdata_dir},
                mounts=mounts, name_suffix='prepare')
            code, state = start_and_wait(docker, ev, bootstrap, args.timeout)
            if code != 0 or state != 'exited' or bootstrap.get('logs_status') != 'captured':
                raise EvidenceError('scratch volume ownership preparation failed')
            nonce = secrets.token_hex(24)
            expected_payload_hash = hashlib.sha256(nonce.encode()).hexdigest()
            write_code = ("import hashlib,os,pathlib; "
                          f"v={nonce!r}; "
                          "p=pathlib.Path(os.environ['CONFIG_DIR'])/'uav-probe'; p.write_text(v); "
                          "q=pathlib.Path(os.environ['APPDATA'])/'uav-probe'; q.write_text(v); "
                          "b=pathlib.Path('/run/uav-client-sentinel').read_text().strip(); "
                          "print('identity:'+str(os.getuid())+':'+str(os.getgid())); "
                          "print('bind:'+hashlib.sha256(b.encode()).hexdigest())")
            writer = create_container(docker, ev, metadata['image_id'], ['python', '-c', write_code],
                env={'PUID': str(uid), 'PGID': str(gid), 'CONFIG_DIR': config_dir, 'APPDATA': appdata_dir},
                mounts=mounts, name_suffix='writer')
            code, state = start_and_wait(docker, ev, writer, args.timeout)
            if code != 0 or state != 'exited' or writer.get('logs_status') != 'captured':
                raise EvidenceError('target entrypoint could not write or read the probe paths')
            writer_out = (ev.path / 'writer.stdout').read_text()
            if f'identity:{uid}:{gid}' not in writer_out:
                raise EvidenceError('target entrypoint did not run with the selected PUID/PGID')
            client_hash = hashlib.sha256(sentinel.read_text().strip().encode()).hexdigest()
            if f'bind:{client_hash}' not in writer_out:
                raise EvidenceError('Docker daemon could not read the exact client-side bind sentinel')
            read_code = ("import hashlib,os,pathlib; "
                         "print('identity:'+str(os.getuid())+':'+str(os.getgid())); "
                         "v=pathlib.Path(os.environ['CONFIG_DIR'],'uav-probe').read_text(); "
                         "w=pathlib.Path(os.environ['APPDATA'],'uav-probe').read_text(); "
                         "print('config:'+hashlib.sha256(v.encode()).hexdigest()); "
                         "print('appdata:'+hashlib.sha256(w.encode()).hexdigest())")
            reader = create_container(docker, ev, metadata['image_id'], ['python', '-c', read_code],
                env={'PUID': str(uid), 'PGID': str(gid), 'CONFIG_DIR': config_dir, 'APPDATA': appdata_dir},
                mounts=mounts, name_suffix='reader')
            code, state = start_and_wait(docker, ev, reader, args.timeout)
            if code != 0 or state != 'exited' or reader.get('logs_status') != 'captured':
                raise EvidenceError('target entrypoint could not read its probe paths')
            reader_out = (ev.path / 'reader.stdout').read_text()
            if f'identity:{uid}:{gid}' not in reader_out:
                raise EvidenceError('target entrypoint did not read with the selected PUID/PGID')
            if f'config:{expected_payload_hash}' not in reader_out or f'appdata:{expected_payload_hash}' not in reader_out:
                raise EvidenceError('target container could not read back daemon-written CONFIG_DIR and APPDATA data')
            summary['probe'] = {'bind_readback': 'passed', 'config_write_readback': 'passed',
                                'appdata_write_readback': 'passed', 'uid': uid, 'gid': gid,
                                'config_dir_is_default': config_dir == '/config',
                                'appdata_dir_is_default': appdata_dir == '/appdata',
                                'appdata_owner_prepared': True}
        ev.update(status='passed')
        summary['status'] = 'passed'
    except Interrupted as exc:
        ev.update(status='interrupted', signal=exc.signum)
        summary['error'] = f'interrupted by signal {exc.signum}'
    except (EvidenceError, OSError, TimeoutError, ValueError) as exc:
        if ev.manifest.get('status') not in ('interrupted', 'timeout'):
            ev.update(status='failed', failure_type=type(exc).__name__)
        summary['status'] = ev.manifest.get('status', 'failed')
        summary['error'] = str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
    finally:
        if args.probe:
            retained = cleanup_resources(docker, ev)
            summary['cleanup'] = 'complete' if not retained else 'pending'
        ev._save_manifest()
        summary['manifest_status'] = ev.manifest.get('status')
        summary['evidence_path'] = str(ev.path)
        pending = pending_resources(ev)
        if pending:
            summary['pending_resources'] = pending
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary['status'] == 'passed' and summary.get('cleanup', 'complete') == 'complete' else 1


def run(args) -> int:
    ev = Evidence(args.evidence_root, 'run')
    docker = Docker(ev)
    summary = {'run_id': ev.run_id, 'evidence_path': str(ev.path), 'status': 'failed'}
    resource = None
    code = None
    state = 'unknown'
    signum = None
    try:
        daemon_fingerprint(docker)
        metadata = image_metadata(docker, args.image, ev)
        safe_command_hash = hashlib.sha256(json.dumps(args.command).encode()).hexdigest()
        ev.update(command_sha256=safe_command_hash)
        resource = create_container(docker, ev, metadata['image_id'], args.command, name_suffix='run')
        ev.update(status='created')
        code, state = start_and_wait(docker, ev, resource, args.timeout)
        signum = ev.manifest.get('signal')
        if ev.manifest.get('status') == 'timeout':
            summary['status'] = 'timeout'
        elif ev.manifest.get('status') == 'interrupted':
            summary['status'] = 'interrupted'
        elif state == 'exited' and code is not None:
            summary['status'] = 'completed'
            ev.update(status='completed')
        else:
            summary['status'] = 'pending'
            ev.update(status='pending')
        summary.update({'payload_exit_code': code, 'container_state': state,
                        'container_id': resource['id'], 'container_name': resource['name'],
                        'daemon_fingerprint': ev.manifest['daemon_context_fingerprint']})
        add_log_status(summary, resource)
        if ev.manifest.get('cleanup_status'):
            summary['cleanup_status'] = ev.manifest['cleanup_status']
        if signum is not None:
            summary['signal'] = signum
    except Interrupted as exc:
        signum = exc.signum
        summary['status'] = 'interrupted'
        if resource:
            try:
                state_obj = ensure_stopped(docker, ev, resource)
                state = state_obj.get('Status', 'pending')
                code = state_obj.get('ExitCode')
            except (EvidenceError, TimeoutError):
                state = 'pending'
        summary.update({'signal': signum, 'container_state': state,
                        'container_id': resource.get('id') if resource else None,
                        'payload_exit_code': code})
        add_log_status(summary, resource)
        pending = pending_resources(ev)
        if pending:
            summary['pending_resources'] = pending
            summary['cleanup_status'] = 'pending'
            ev.update(cleanup_status='pending', pending_resources=pending)
        ev.update(status='interrupted', signal=signum)
    except (EvidenceError, OSError, TimeoutError, ValueError) as exc:
        summary['error'] = str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
        ev.update(status='failed', failure_type=type(exc).__name__)
        if resource:
            try:
                state_obj = ensure_stopped(docker, ev, resource)
                state = state_obj.get('Status', 'pending')
                code = state_obj.get('ExitCode')
            except (EvidenceError, TimeoutError):
                state, code = 'pending', None
            summary.update({'container_state': state, 'container_id': resource.get('id'),
                            'payload_exit_code': code})
        add_log_status(summary, resource)
        pending = pending_resources(ev)
        if pending:
            summary['pending_resources'] = pending
            summary['cleanup_status'] = 'pending'
            ev.update(cleanup_status='pending', pending_resources=pending)
    finally:
        ev._save_manifest()
        summary['evidence_path'] = str(ev.path)
        summary['run_id'] = ev.run_id
        summary['manifest_status'] = ev.manifest.get('status')
    print(json.dumps(summary, sort_keys=True))
    if summary['status'] == 'timeout':
        return 124
    if summary['status'] == 'interrupted':
        return 128 + (signum or signal.SIGINT)
    if summary['status'] == 'completed':
        return int(code or 0)
    return 1


def load_evidence(root: Path, run_id: str) -> Evidence:
    try:
        parsed_id = str(uuid.UUID(run_id))
    except ValueError:
        raise EvidenceError('invalid run ID') from None
    path = root.expanduser().absolute() / parsed_id
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise EvidenceError('run evidence directory is not privately owned')
    manifest_path = path / 'manifest.json'
    info = manifest_path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise EvidenceError('run manifest is not privately owned')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('run_id') != parsed_id or not isinstance(manifest.get('owner_token'), str):
        raise EvidenceError('run manifest ownership proof is invalid')
    ev = object.__new__(Evidence)
    ev.run_id = parsed_id
    ev.path = path
    ev.token = manifest['owner_token']
    ev.manifest_path = manifest_path
    ev.events_path = path / 'events.jsonl'
    ev.manifest = manifest
    return ev


def cleanup(args) -> int:
    try:
        ev = load_evidence(args.evidence_root, args.run_id)
        docker = Docker(ev)
        fingerprint = daemon_fingerprint(docker, save=False)
        if fingerprint != ev.manifest.get('daemon_context_fingerprint'):
            raise EvidenceError('different Docker daemon/context fingerprint; preserving resources')
        retained = cleanup_resources(docker, ev)
        ev.update(status='cleaned' if not retained else 'cleanup_pending')
        print(json.dumps({'run_id': ev.run_id, 'status': ev.manifest['status'],
                          'retained_resources': retained, 'evidence_path': str(ev.path)}, sort_keys=True))
        return 0 if not retained else 1
    except (EvidenceError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def positive_timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('timeout must be a positive number') from None
    if not math.isfinite(result) or result <= 0 or result > MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            f'timeout must be finite, positive, and no greater than {MAX_TIMEOUT_SECONDS:g} seconds')
    return result


def positive_id(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('UID/GID must be non-negative integers') from None
    if result < 0:
        raise argparse.ArgumentTypeError('UID/GID must be non-negative integers')
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    run_parser = sub.add_parser('run', help='record one owned container execution')
    run_parser.add_argument('--evidence-root', type=Path, default=DEFAULT_EVIDENCE_ROOT)
    run_parser.add_argument('--image', required=True)
    run_parser.add_argument('--timeout', type=positive_timeout, default=600,
                            help='maximum wait in seconds (maximum 86400, or 24 hours)')
    run_parser.add_argument('command', nargs=argparse.REMAINDER)
    pre_parser = sub.add_parser('preflight', help='inspect an image and optionally probe isolated scratch resources')
    pre_parser.add_argument('--evidence-root', type=Path, default=DEFAULT_EVIDENCE_ROOT)
    pre_parser.add_argument('--image', required=True)
    pre_parser.add_argument('--probe', action='store_true')
    pre_parser.add_argument('--timeout', type=positive_timeout, default=120,
                            help='maximum wait in seconds (maximum 86400, or 24 hours)')
    pre_parser.add_argument('--uid', type=positive_id)
    pre_parser.add_argument('--gid', type=positive_id)
    cleanup_parser = sub.add_parser('cleanup', help='remove only resources proven by a run manifest')
    cleanup_parser.add_argument('run_id')
    cleanup_parser.add_argument('--evidence-root', type=Path, default=DEFAULT_EVIDENCE_ROOT)
    args = parser.parse_args(argv)
    old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    def interrupt(received, _frame):
        raise Interrupted(received)

    for sig in old_handlers:
        signal.signal(sig, interrupt)
    try:
        if args.action == 'run':
            if not args.command:
                parser.error('run requires a command after --')
            command = args.command[1:] if args.command and args.command[0] == '--' else args.command
            if not command:
                parser.error('run requires a command after --')
            args.command = command
            return run(args)
        if args.action == 'preflight':
            return preflight(args)
        return cleanup(args)
    except EvidenceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    sys.exit(main())
