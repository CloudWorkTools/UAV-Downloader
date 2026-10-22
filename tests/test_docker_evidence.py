"""Private Docker evidence and ownership checks."""

import json
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'docker_evidence.py'
SPEC = importlib.util.spec_from_file_location('docker_evidence_module', SCRIPT)
docker_evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(docker_evidence)
FAKE_DOCKER = r'''#!/usr/bin/env python3
import hashlib, json, os, re, sys, time
from pathlib import Path
args = sys.argv[1:]
log = Path(os.environ['FAKE_DOCKER_LOG'])
with log.open('a') as f:
    f.write(json.dumps(args) + '\n')
state_path = Path(os.environ['FAKE_DOCKER_STATE'])
state = json.loads(state_path.read_text()) if state_path.exists() else {'containers': {}, 'volumes': {}}
def save(): state_path.write_text(json.dumps(state))
def config():
    return {'Entrypoint': ['/entrypoint.sh'], 'Cmd': ['uav-web'], 'User': '', 'Env': ['PUID=1000', 'PGID=1000', 'CONFIG_DIR=/config', 'APPDATA=' + os.environ.get('FAKE_APPDATA', '/appdata'), 'SECRET=inspect-secret'], 'Volumes': {'/config': {}, '/downloads': {}}}
def info():
    return {'ID': os.environ.get('FAKE_DAEMON_ID', 'daemon-test-id'), 'Name': 'fake-daemon', 'ServerVersion': '29.1', 'DockerRootDir': '/var/lib/docker'}
def container(identifier):
    c = state['containers'].get(identifier)
    if not c:
        for value in state['containers'].values():
            if value['name'].lstrip('/') == identifier or value['id'].startswith(identifier): c = value; break
    return c
if args == ['context', 'show']:
    print('fake-context')
elif args[:2] == ['context', 'inspect']:
    print(json.dumps([{'Endpoints': {'docker': {'Host': 'ssh://user:context-secret@daemon'}}}]))
elif args[:2] == ['info', '--format']:
    print(json.dumps(info()))
elif args[:2] == ['image', 'inspect']:
    print(json.dumps([{'Id': 'sha256:image-test', 'Config': config()}]))
elif args and args[0] == 'create':
    if os.environ.get('FAKE_CREATE_FAIL'):
        print('create-secret-error', file=sys.stderr); sys.exit(1)
    name = next((args[i + 1] for i, x in enumerate(args[:-1]) if x == '--name'), None)
    labels = [args[i + 1] for i, x in enumerate(args[:-1]) if x == '--label']
    mounts = []
    for item in args:
        if item.startswith('type=volume,'):
            fields = dict(part.split('=', 1) for part in item.split(','))
            mounts.append({'Type': 'volume', 'Name': fields.get('src'), 'Source': '/var/lib/docker/volumes/' + fields.get('src', ''), 'Destination': fields.get('dst'), 'RW': not fields.get('readonly')})
        if item.startswith('type=bind,'):
            fields = dict(part.split('=', 1) for part in item.split(',') if '=' in part)
            mounts.append({'Type': 'bind', 'Name': '', 'Source': fields.get('src'), 'Destination': fields.get('dst'), 'RW': 'readonly' not in item})
    if os.environ.get('FAKE_BIND_FAIL') and any(m['Type'] == 'bind' for m in mounts):
        print('bind source unavailable', file=sys.stderr); sys.exit(1)
    mounted_paths = {m['Destination'] for m in mounts}
    for destination in ('/config', '/downloads'):
        if destination not in mounted_paths:
            anonymous = 'anonymous-' + str(len(state['volumes']) + 1)
            state['volumes'][anonymous] = {'Name': anonymous, 'Driver': 'local', 'Labels': None}
            mounts.append({'Type': 'volume', 'Name': anonymous, 'Source': '/var/lib/docker/volumes/' + anonymous, 'Destination': destination, 'RW': True})
    identifier = 'container-' + str(len(state['containers']) + 1)
    image_index = next(i for i, x in enumerate(args) if x in ('private-image', 'sha256:image-test'))
    env = {args[i + 1].split('=', 1)[0]: args[i + 1].split('=', 1)[1]
           for i, x in enumerate(args[:-1]) if x == '--env'}
    state['containers'][identifier] = {'id': identifier, 'name': name, 'labels': dict(x.split('=', 1) for x in labels), 'mounts': mounts, 'env': env, 'command': args[image_index + 1:], 'status': 'created', 'exit': 0}
    if os.environ.get('FAKE_FOREIGN_REFERENCE') and not any(c['id'] == 'foreign-container' for c in state['containers'].values()):
        volume = next(iter(state['volumes']))
        state['containers']['foreign-container'] = {'id': 'foreign-container', 'name': 'foreign-stopped', 'labels': {}, 'mounts': [{'Type': 'volume', 'Name': volume, 'Source': '/var/lib/docker/volumes/' + volume, 'Destination': '/foreign', 'RW': True}], 'command': [], 'status': 'exited', 'exit': 0}
    save(); print(identifier)
elif args and args[0] == 'inspect' and len(args) > 1:
    c = container(args[1])
    if c:
        if os.environ.get('FAKE_CONTAINER_INSPECT_FAIL'):
            print('inspect-secret-error', file=sys.stderr); sys.exit(1)
        print(json.dumps([{'Id': c['id'], 'Name': '/' + c['name'], 'Config': {'Labels': c['labels'], 'Env': ['SECRET=inspect-secret']}, 'State': {'Status': c['status'], 'Running': c['status'] == 'running', 'ExitCode': c['exit']}, 'Mounts': c['mounts']}]))
    else:
        print(json.dumps([{'Id': 'sha256:image-test', 'Config': config()}]))
elif args and args[0] == 'start':
    if os.environ.get('FAKE_START_FAIL'):
        print('start-secret-error', file=sys.stderr); sys.exit(1)
    c = container(args[-1]); c['status'] = 'running'
    command = ' '.join(c.get('command', []))
    if "v='" in command:
        value = re.search(r"v='([^']+)'", command).group(1)
        for mount in c['mounts']:
            if mount['Type'] == 'volume' and mount['Destination'] in ('/config', '/appdata'):
                state['volumes'][mount['Name']].setdefault('data', {})['uav-probe'] = value
    save()
elif args and args[0] == 'wait':
    if os.environ.get('FAKE_WAIT_SLEEP'):
        print('wait-private-error', file=sys.stderr)
        time.sleep(float(os.environ['FAKE_WAIT_SLEEP']))
    c = container(args[-1]); c['status'] = 'exited'
    default_exit = '0' if 'uav-probe' in ' '.join(c.get('command', [])) or 'os.chown' in ' '.join(c.get('command', [])) else '7'
    c['exit'] = int(os.environ.get('FAKE_EXIT', default_exit)); save(); print(c['exit'])
elif args and args[0] == 'logs':
    if os.environ.get('FAKE_LOGS_FAIL'):
        print('logs-secret-error', file=sys.stderr); sys.exit(1)
    if os.environ.get('FAKE_LOGS_SIGNAL') and not state.get('signal_log_emitted'):
        state['signal_log_emitted'] = True; save()
        print('interrupted-log-stdout')
        sys.stdout.flush()
        print('interrupted-log-stderr', file=sys.stderr)
        state['signal_log_ready'] = True; save()
        time.sleep(5)
    c = container(args[-1]); command = ' '.join(c.get('command', []))
    if 'uav-client-sentinel' in command:
        bind = next(x for x in c['mounts'] if x['Type'] == 'bind')
        print('bind:' + hashlib.sha256(Path(bind['Source']).read_text().strip().encode()).hexdigest())
        print('identity:' + os.environ.get('FAKE_IDENTITY_UID', c['env'].get('PUID', '1000')) + ':' + os.environ.get('FAKE_IDENTITY_GID', c['env'].get('PGID', '1000')))
    elif 'uav-probe' in command:
        print('identity:' + os.environ.get('FAKE_IDENTITY_UID', c['env'].get('PUID', '1000')) + ':' + os.environ.get('FAKE_IDENTITY_GID', c['env'].get('PGID', '1000')))
        for mount in c['mounts']:
            if mount['Type'] == 'volume' and mount['Destination'] in ('/config', '/appdata'):
                data = state['volumes'][mount['Name']].get('data', {}).get('uav-probe', '')
                print(mount['Destination'].lstrip('/') + ':' + hashlib.sha256(data.encode()).hexdigest())
    else:
        print('payload-secret-output')
        print('private-error-output', file=sys.stderr)
elif args and args[0] == 'stop':
    if os.environ.get('FAKE_STOP_FAIL'):
        print('stop-secret-error', file=sys.stderr); sys.exit(1)
    c = container(args[-1]); c['status'] = 'exited'; c['exit'] = 137; save()
elif args and args[:3] == ['ps', '-a', '-q']:
    print('\n'.join(c['id'] for c in state['containers'].values()))
elif args and args[0] == 'rm':
    for identifier in args[1:]:
        c = container(identifier)
        if c: state['containers'].pop(c['id'], None)
    save()
elif args and args[:2] == ['volume', 'create']:
    labels = [args[i + 1] for i, x in enumerate(args[:-1]) if x == '--label']
    name = args[-1]
    state['volumes'][name] = {'Name': name, 'Driver': 'local', 'Labels': dict(x.split('=', 1) for x in labels)}
    save(); print(name)
elif args and args[:2] == ['volume', 'inspect']:
    name = args[-1]
    print(json.dumps([state['volumes'][name]]))
elif args and args[:2] == ['volume', 'rm']:
    for name in args[2:]: state['volumes'].pop(name, None)
    save()
elif args and args[:2] == ['ps', '-a']:
    print('')
else:
    print('unsupported fake docker operation: ' + json.dumps(args), file=sys.stderr); sys.exit(2)
'''


class DockerEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        docker = self.bin / 'docker'
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.log = self.base / 'docker-calls.jsonl'
        self.state = self.base / 'docker-state.json'
        self.evidence = self.base / 'evidence'
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'FAKE_DOCKER_LOG': str(self.log), 'FAKE_DOCKER_STATE': str(self.state)}

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT,
                              env={**self.env, **(env or {})}, capture_output=True, text=True)

    def run_once(self, extra_env=None):
        return self.invoke('run', '--evidence-root', str(self.evidence), '--image', 'private-image',
                           '--timeout', '10', '--', 'python', '-c', "print('command-secret')",
                           env=extra_env)

    def test_create_container_can_match_readonly_bridge_ci_run(self):
        source = self.base / 'source'
        source.mkdir()
        ev = docker_evidence.Evidence(self.evidence, 'canonical-web-check')
        docker = docker_evidence.Docker(ev)

        with patch.dict(os.environ, self.env):
            resource = docker_evidence.create_container(
                docker, ev, 'sha256:image-test', [], network='bridge',
                bind_mounts=[(str(source), '/workspace', True)], user=(1234, 2345),
                workdir='/workspace')

        create = next(json.loads(line) for line in self.log.read_text().splitlines()
                      if json.loads(line)[0] == 'create')
        self.assertEqual(create[create.index('--network') + 1], 'bridge')
        self.assertEqual(create[create.index('--user') + 1], '1234:2345')
        self.assertEqual(create[create.index('--workdir') + 1], '/workspace')
        self.assertIn(f'type=bind,src={source},dst=/workspace,readonly', create)
        self.assertEqual(resource['mounts_recorded'], True)
        self.assertTrue(any(mount['Destination'] == '/workspace' and not mount['RW']
                            for mount in resource['mounts']))

    def test_create_container_rejects_unapproved_network_before_docker_call(self):
        ev = docker_evidence.Evidence(self.evidence, 'canonical-web-check')
        docker = docker_evidence.Docker(ev)
        with patch.dict(os.environ, self.env):
            with self.assertRaises(docker_evidence.EvidenceError):
                docker_evidence.create_container(docker, ev, 'sha256:image-test', [], network='host')
        self.assertFalse(self.log.exists())

    def test_run_records_exact_mounts_before_start_and_keeps_raw_output_private(self):
        result = self.run_once()
        self.assertEqual(result.returncode, 7, result.stderr)
        summary = json.loads(result.stdout)
        run_dir = Path(summary['evidence_path'])
        manifest = json.loads((run_dir / 'manifest.json').read_text())
        self.assertEqual(summary['payload_exit_code'], 7)
        self.assertEqual(manifest['resources'][0]['id'], 'container-1')
        self.assertIn('mounts', manifest['resources'][0])
        self.assertEqual({m['Destination'] for m in manifest['resources'][0]['mounts']}, {'/config', '/downloads'})
        self.assertTrue(all(m['Name'].startswith('anonymous-') for m in manifest['resources'][0]['mounts']))
        self.assertEqual((run_dir / 'payload.stdout').read_text().strip(), 'payload-secret-output')
        self.assertEqual((run_dir / 'payload.stderr').read_text().strip(), 'private-error-output')
        self.assertEqual(run_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((run_dir / 'manifest.json').stat().st_mode & 0o777, 0o600)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        names = [call[0] for call in calls]
        self.assertLess(names.index('create'), names.index('inspect'))
        self.assertLess(names.index('inspect'), names.index('start'))
        stored = '\n'.join(path.read_text(errors='ignore') for path in run_dir.iterdir() if path.is_file())
        for secret in ('command-secret', 'inspect-secret', 'context-secret'):
            self.assertNotIn(secret, result.stdout + stored)
        self.assertNotIn('payload-secret-output', result.stdout)
        self.assertIn('daemon_context_fingerprint', manifest)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        create = next(call for call in calls if call[0] == 'create')
        self.assertIn('sha256:image-test', create)

    def test_timeout_stops_only_after_exact_resource_ownership_is_verified(self):
        result = self.invoke('run', '--evidence-root', str(self.evidence), '--image', 'private-image',
                             '--timeout', '0.1', '--', 'python', '-c', 'pass',
                             env={'FAKE_WAIT_SLEEP': '2'})
        self.assertEqual(result.returncode, 124, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['status'], 'timeout')
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        stop_index = next(i for i, call in enumerate(calls) if call[0] == 'stop')
        self.assertTrue(any(call[0] == 'inspect' and i < stop_index for i, call in enumerate(calls)))
        self.assertTrue(summary['container_state'] in ('exited', 'pending'))
        diagnostic = next(Path(summary['evidence_path']).glob('docker-*-wait.stderr'))
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        self.assertIn('wait-private-error', diagnostic.read_text())

    def test_stop_failure_reports_payload_still_running_and_preserves_container(self):
        result = self.invoke('run', '--evidence-root', str(self.evidence), '--image', 'private-image',
                             '--timeout', '0.1', '--', 'python', '-c', 'pass',
                             env={'FAKE_WAIT_SLEEP': '2', 'FAKE_STOP_FAIL': '1'})
        self.assertEqual(result.returncode, 124)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['container_state'], 'running')
        self.assertEqual(summary['cleanup_status'], 'pending')
        self.assertNotIn('stop-secret-error', result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertFalse(any(call[0] == 'rm' for call in calls))

    def test_sigterm_is_recorded_and_only_owned_payload_is_stopped(self):
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), 'run', '--evidence-root', str(self.evidence),
             '--image', 'private-image', '--timeout', '10', '--', 'python', '-c', 'pass'],
            cwd=ROOT, env={**self.env, 'FAKE_WAIT_SLEEP': '5'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.log.exists() and any(json.loads(line)[0] == 'wait' for line in self.log.read_text().splitlines()):
                break
            if process.poll() is not None:
                self.fail('evidence command exited before Docker wait started')
            time.sleep(0.02)
        else:
            process.kill()
            self.fail('Docker wait did not start')
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary['status'], 'interrupted')
        self.assertEqual(summary['signal'], signal.SIGTERM)
        self.assertEqual(summary['container_state'], 'exited')
        manifest = json.loads((Path(summary['evidence_path']) / 'manifest.json').read_text())
        self.assertEqual(manifest['signal'], signal.SIGTERM)
        events = [json.loads(line) for line in (Path(summary['evidence_path']) / 'events.jsonl').read_text().splitlines()]
        self.assertTrue(any(event['operation'] == 'wait' and event.get('signal') == signal.SIGTERM
                            for event in events))
        diagnostic = next(Path(summary['evidence_path']).glob('docker-*-wait.stderr'))
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        self.assertIn('wait-private-error', diagnostic.read_text())

    def test_sigterm_during_log_capture_keeps_partial_stdout_and_stderr(self):
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), 'run', '--evidence-root', str(self.evidence),
             '--image', 'private-image', '--timeout', '10', '--', 'python', '-c', 'pass'],
            cwd=ROOT, env={**self.env, 'FAKE_LOGS_SIGNAL': '1'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.state.exists() and json.loads(self.state.read_text()).get('signal_log_ready'):
                break
            if process.poll() is not None:
                self.fail('evidence command exited before Docker logs started')
            time.sleep(0.02)
        else:
            process.kill()
            self.fail('Docker logs did not start')
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr)
        summary = json.loads(stdout)
        run_dir = Path(summary['evidence_path'])
        self.assertTrue(any('interrupted-log-stdout' in path.read_text()
                            for path in run_dir.glob('payload-*.stdout')))
        self.assertTrue(any('interrupted-log-stderr' in path.read_text()
                            for path in run_dir.glob('payload-*.stderr')))

    def test_cleanup_refuses_when_daemon_fingerprint_changes(self):
        result = self.run_once()
        self.assertEqual(result.returncode, 7, result.stderr)
        summary = json.loads(result.stdout)
        before = len(self.log.read_text().splitlines())
        cleanup = self.invoke('cleanup', summary['run_id'], '--evidence-root', str(self.evidence),
                              env={'FAKE_DAEMON_ID': 'other-daemon'})
        self.assertNotEqual(cleanup.returncode, 0)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()[before:]]
        self.assertFalse(any(call[0] in ('rm', 'volume') for call in calls))
        self.assertIn('different Docker daemon', cleanup.stderr)

    def test_cleanup_refuses_foreign_container_label(self):
        result = self.run_once()
        summary = json.loads(result.stdout)
        state = json.loads(self.state.read_text())
        state['containers']['container-1']['labels'] = {'io.uav-downloader.evidence-owner': 'foreign'}
        self.state.write_text(json.dumps(state))
        before = len(self.log.read_text().splitlines())
        cleanup = self.invoke('cleanup', summary['run_id'], '--evidence-root', str(self.evidence))
        self.assertNotEqual(cleanup.returncode, 0)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()[before:]]
        self.assertFalse(any(call[0] == 'rm' for call in calls))

    def test_cleanup_never_removes_anonymous_run_volumes(self):
        result = self.run_once()
        summary = json.loads(result.stdout)
        cleanup = self.invoke('cleanup', summary['run_id'], '--evidence-root', str(self.evidence))
        self.assertNotEqual(cleanup.returncode, 0, cleanup.stderr)
        cleanup_summary = json.loads(cleanup.stdout)
        self.assertEqual(cleanup_summary['status'], 'cleanup_pending')
        self.assertTrue(any(item.startswith('volume:anonymous-') for item in cleanup_summary['retained_resources']))
        second_cleanup = self.invoke('cleanup', summary['run_id'], '--evidence-root', str(self.evidence))
        self.assertNotEqual(second_cleanup.returncode, 0)
        self.assertTrue(json.loads(second_cleanup.stdout)['retained_resources'])
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(any(call[0] == 'rm' and call[-1] == 'container-1' for call in calls))
        self.assertFalse(any(call[:2] == ['volume', 'rm'] for call in calls))

    def test_cleanup_preserves_running_container_without_stopping_it(self):
        result = self.run_once()
        summary = json.loads(result.stdout)
        state = json.loads(self.state.read_text())
        state['containers']['container-1']['status'] = 'running'
        self.state.write_text(json.dumps(state))
        before = len(self.log.read_text().splitlines())
        cleanup = self.invoke('cleanup', summary['run_id'], '--evidence-root', str(self.evidence))
        self.assertNotEqual(cleanup.returncode, 0)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()[before:]]
        self.assertFalse(any(call[0] in ('stop', 'rm') for call in calls))

    def test_start_failure_is_recorded_without_leaking_docker_error(self):
        result = self.run_once({'FAKE_START_FAIL': '1'})
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('start-secret-error', result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['status'], 'failed')
        manifest = json.loads((Path(summary['evidence_path']) / 'manifest.json').read_text())
        self.assertEqual(manifest['resources'][0]['state'], 'created')
        self.assertEqual(summary['pending_resources'][0]['id'], 'container-1')
        self.assertEqual(summary['cleanup_status'], 'pending')
        diagnostic = next(Path(summary['evidence_path']).glob('docker-*-start.stderr'))
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        self.assertIn('start-secret-error', diagnostic.read_text())

    def test_inspect_failure_preserves_container_when_mount_ownership_is_unknown(self):
        result = self.run_once({'FAKE_CONTAINER_INSPECT_FAIL': '1'})
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('inspect-secret-error', result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        manifest = json.loads((Path(summary['evidence_path']) / 'manifest.json').read_text())
        resource = manifest['resources'][0]
        self.assertFalse(resource['mounts_recorded'])
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertFalse(any(call[0] == 'start' for call in calls))
        self.assertFalse(any(call[0] == 'rm' for call in calls))
        self.assertEqual(summary['pending_resources'][0]['id'], 'container-1')
        self.assertFalse(summary['pending_resources'][0]['mounts_recorded'])
        self.assertEqual(summary['cleanup_status'], 'pending')

    def test_logs_failure_is_recorded_privately_without_changing_payload_result(self):
        result = self.run_once({'FAKE_LOGS_FAIL': '1'})
        self.assertEqual(result.returncode, 7)
        summary = json.loads(result.stdout)
        manifest = json.loads((Path(summary['evidence_path']) / 'manifest.json').read_text())
        self.assertEqual(summary['status'], 'completed')
        self.assertEqual(manifest['resources'][0]['logs_status'], 'failed')
        self.assertEqual(summary['logs_status'], 'failed')
        self.assertIn('private evidence', summary['logs_warning'])
        self.assertNotIn('logs-secret-error', result.stdout + result.stderr)
        diagnostic = next(Path(summary['evidence_path']).glob('docker-*-logs.stderr'))
        self.assertIn('logs-secret-error', diagnostic.read_text())
        cleanup = self.invoke('cleanup', summary['run_id'], '--evidence-root', str(self.evidence))
        self.assertEqual(cleanup.returncode, 1)
        cleanup_summary = json.loads(cleanup.stdout)
        self.assertEqual(cleanup_summary['status'], 'cleanup_pending')
        self.assertEqual(cleanup_summary['retained_resources'], ['container-1'])
        self.assertFalse(any(call[0] == 'rm' for call in map(json.loads, self.log.read_text().splitlines())))

    def test_repeated_cleanup_failures_keep_unique_diagnostics(self):
        result = self.run_once()
        summary = json.loads(result.stdout)
        for _ in range(2):
            cleanup = self.invoke('cleanup', summary['run_id'], '--evidence-root', str(self.evidence),
                                  env={'FAKE_CONTAINER_INSPECT_FAIL': '1'})
            self.assertEqual(cleanup.returncode, 1)
            self.assertNotIn('inspect-secret-error', cleanup.stdout + cleanup.stderr)
        run_dir = Path(summary['evidence_path'])
        diagnostics = list(run_dir.glob('docker-*-inspect.stderr'))
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(len({path.name for path in diagnostics}), 2)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in diagnostics))
        self.assertTrue(all('inspect-secret-error' in path.read_text() for path in diagnostics))

    def test_create_failure_reports_manifest_resource_without_exact_id(self):
        result = self.run_once({'FAKE_CREATE_FAIL': '1'})
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('create-secret-error', result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['cleanup_status'], 'pending')
        self.assertIsNone(summary['pending_resources'][0]['id'])
        manifest = json.loads((Path(summary['evidence_path']) / 'manifest.json').read_text())
        self.assertIsNone(manifest['resources'][0]['id'])
        diagnostic = next(Path(summary['evidence_path']).glob('docker-*-create.stderr'))
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        self.assertIn('create-secret-error', diagnostic.read_text())

    def test_invalid_and_unsafe_timeouts_are_rejected_before_docker_calls(self):
        unsafe = ['nan', 'inf', '-inf', '1e309', '3000000', '86401']
        for value in unsafe:
            with self.subTest(timeout=value):
                result = self.invoke('run', '--evidence-root', str(self.evidence), '--image', 'private-image',
                                     '--timeout', value, '--', 'true')
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.log.exists())

    def test_docker_event_start_and_end_share_unique_call_id(self):
        result = self.run_once()
        run_dir = Path(json.loads(result.stdout)['evidence_path'])
        events = [json.loads(line) for line in (run_dir / 'events.jsonl').read_text().splitlines()]
        by_id = {}
        for event in events:
            by_id.setdefault(event['call_id'], set()).add(event['phase'])
        self.assertTrue(by_id)
        self.assertTrue(all(phases == {'start', 'end'} for phases in by_id.values()))

    def test_readonly_probe_uses_exact_client_path_and_cleans_its_owned_scratch(self):
        result = self.invoke('preflight', '--evidence-root', str(self.evidence), '--image', 'private-image', '--probe')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['probe']['bind_readback'], 'passed')
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        create = next(call for call in calls if call[0] == 'create')
        mount = next((arg for arg in create if arg.startswith('type=bind,')), '')
        self.assertIn('readonly', mount)
        self.assertIn(f"src={Path(summary['evidence_path']) / 'client-sentinel'}", mount)
        self.assertNotIn(' -v ', ' ' + ' '.join(create) + ' ')
        self.assertTrue(any(call[:2] == ['volume', 'rm'] for call in calls))
        manifest = json.loads((Path(summary['evidence_path']) / 'manifest.json').read_text())
        self.assertTrue(all(resource.get('removed') for resource in manifest['resources']))
        self.assertTrue(any(resource['kind'] == 'container' and resource['mounts'] for resource in manifest['resources']))
        self.assertTrue(any('sha256:image-test' in call for call in calls if call[0] == 'create'))

    def test_private_manifest_keeps_allowed_config_path_out_of_public_summary(self):
        result = self.invoke('preflight', '--evidence-root', str(self.evidence), '--image', 'private-image',
                             env={'FAKE_APPDATA': '/private/application-state'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn('/private/application-state', result.stdout)
        summary = json.loads(result.stdout)
        manifest = json.loads((Path(summary['evidence_path']) / 'manifest.json').read_text())
        self.assertEqual(manifest['image_metadata']['selected_defaults']['APPDATA'], '/private/application-state')

    def test_probe_rejects_entrypoint_that_ignores_selected_uid_gid(self):
        result = self.invoke('preflight', '--evidence-root', str(self.evidence), '--image', 'private-image', '--probe',
                             env={'FAKE_IDENTITY_UID': '0'})
        self.assertEqual(result.returncode, 1)
        summary = json.loads(result.stdout)
        self.assertIn('selected PUID/PGID', summary['error'])
        self.assertEqual(summary['cleanup'], 'complete')

    def test_probe_rejects_unsafe_mount_path_before_creating_resources(self):
        result = self.invoke('preflight', '--evidence-root', str(self.evidence), '--image', 'private-image', '--probe',
                             env={'FAKE_APPDATA': '/private/path,readonly'})
        self.assertEqual(result.returncode, 1)
        summary = json.loads(result.stdout)
        self.assertIn('not safe for an isolated mount probe', summary['error'])
        self.assertFalse(any(json.loads(line)[0] in ('create', 'volume')
                             for line in self.log.read_text().splitlines()))

    def test_probe_retains_owned_volume_referenced_by_a_stopped_foreign_container(self):
        result = self.invoke('preflight', '--evidence-root', str(self.evidence), '--image', 'private-image', '--probe',
                             env={'FAKE_FOREIGN_REFERENCE': '1'})
        self.assertEqual(result.returncode, 1)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['cleanup'], 'pending')
        manifest = json.loads((Path(summary['evidence_path']) / 'manifest.json').read_text())
        retained = [r for r in manifest['resources'] if r['kind'] == 'volume' and not r.get('removed')]
        self.assertEqual(len(retained), 1)
        self.assertNotEqual(retained[0]['name'], 'foreign-container')
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertFalse(any(call[:2] == ['volume', 'rm'] and call[-1] == retained[0]['id'] for call in calls))

    def test_missing_bind_source_fails_closed_and_cleans_owned_volumes(self):
        result = self.invoke('preflight', '--evidence-root', str(self.evidence), '--image', 'private-image', '--probe',
                             env={'FAKE_BIND_FAIL': '1'})
        self.assertEqual(result.returncode, 1)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['status'], 'failed')
        self.assertEqual(summary['cleanup'], 'pending')
        self.assertIn('Docker could not create', summary['error'])
        manifest = json.loads((Path(summary['evidence_path']) / 'manifest.json').read_text())
        self.assertTrue(any(r['kind'] == 'container' and r['id'] is None for r in manifest['resources']))
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(sum(call[:2] == ['volume', 'rm'] for call in calls), 3)


if __name__ == '__main__':
    unittest.main()
