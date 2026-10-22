"""Tests for the guarded local canonical web test runner."""

from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
import io
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'scripts' / 'check_web_canonical.py'
SPEC = importlib.util.spec_from_file_location('check_web_canonical', RUNNER)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class CanonicalRunnerTests(unittest.TestCase):
    def test_runner_exposes_help(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, str(RUNNER), '--help'], cwd=temporary,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--source-sha', result.stdout)

    def test_wrong_source_sha_is_rejected_before_docker_or_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence_root = Path(temporary) / 'evidence'
            stderr = io.StringIO()
            # A readonly managed worktree may point .git outside the mounted workspace.
            with (mock.patch.object(runner, 'git_output', side_effect=lambda *args: 'a' * 40
                                    if args == ('rev-parse', 'HEAD') else ''),
                  mock.patch.object(runner.evidence, 'Evidence') as evidence_factory,
                  mock.patch.object(runner.evidence, 'Docker') as docker_factory,
                  redirect_stderr(stderr)):
                result = runner.main(['--source-sha', '0' * 40,
                                      '--evidence-root', str(evidence_root)])
            self.assertEqual(result, 1)
            self.assertIn('source SHA does not match HEAD', stderr.getvalue())
            evidence_factory.assert_not_called()
            docker_factory.assert_not_called()
            self.assertFalse(evidence_root.exists())

    def test_source_and_environment_mismatches_fail_closed(self):
        good = dict(head='a' * 40, expected='a' * 40, status='', uid=1000, gid=1000)
        runner.validate_source(**good)
        invalid = [
            dict(good, expected='b' * 40),
            dict(good, status='?? untracked.txt'),
            dict(good, status=' M tracked.py'),
            dict(good, uid=0),
            dict(good, network='host'),
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(runner.evidence.EvidenceError):
                runner.validate_source(**values)

    def test_image_identity_rejects_stale_or_unpinned_builds(self):
        obj = {'Id': 'sha256:' + '1' * 64, 'RepoTags': ['uav-web-tests:test'], 'Config': {'Labels': {
            runner.SOURCE_LABEL: 'a' * 40, runner.RUN_LABEL: 'run-1',
            runner.BASE_IMAGE_LABEL: 'sha256:' + '2' * 64,
        }}, 'RootFS': {'Layers': ['app-layer', 'test-layer']}}
        expected = dict(image_id='sha256:' + '1' * 64, tag='uav-web-tests:test',
                        source_sha='a' * 40, run_id='run-1',
                        base_app_image_id='sha256:' + '2' * 64,
                        base_app_layers=['app-layer'])
        runner.validate_image(obj, **expected)
        stale = dict(obj, Id='sha256:' + '3' * 64)
        with self.assertRaises(runner.evidence.EvidenceError):
            runner.validate_image(stale, **expected)
        for label, value in ((runner.SOURCE_LABEL, 'b' * 40),
                             (runner.RUN_LABEL, 'old-run'),
                             (runner.BASE_IMAGE_LABEL, 'sha256:' + '4' * 64)):
            stale = {'Id': obj['Id'], 'RepoTags': obj['RepoTags'], 'Config': {'Labels': dict(obj['Config']['Labels'], **{label: value})}}
            with self.assertRaises(runner.evidence.EvidenceError):
                runner.validate_image(stale, **expected)
        wrong_ancestry = dict(obj, RootFS={'Layers': ['other-app-layer', 'test-layer']})
        with self.assertRaises(runner.evidence.EvidenceError):
            runner.validate_image(wrong_ancestry, **expected)

    def test_app_tag_change_during_build_is_rejected(self):
        before = {'Id': 'sha256:' + 'a' * 64, 'RepoTags': ['uav-web-check:run'],
                  'RootFS': {'Layers': ['app-layer']}}
        runner.validate_app_tag_stable(before, dict(before), tag='uav-web-check:run',
                                       image_id=before['Id'], layers=['app-layer'])
        changed_tag = dict(before, RepoTags=['uav-web-check:foreign'])
        with self.assertRaises(runner.evidence.EvidenceError):
            runner.validate_app_tag_stable(before, changed_tag, tag='uav-web-check:run',
                                           image_id=before['Id'], layers=['app-layer'])

    def test_test_image_build_command_uses_run_scoped_app_tag_and_records_id(self):
        app_id = 'sha256:' + 'a' * 64
        app_tag = 'uav-web-check:bbbbbbbbbbbb-run-1'
        command = runner.test_image_build_command('uav-web-tests:test', 'b' * 40, 'run-1', app_tag, app_id)
        self.assertIn(f'APP_IMAGE={app_tag}', command)
        self.assertIn(f'{runner.BASE_IMAGE_LABEL}={app_id}', command)
        self.assertEqual(command[-1], str(ROOT))
        self.assertEqual(command[command.index('-f') + 1], str(ROOT / 'Dockerfile.web-tests'))

    def test_app_build_command_uses_absolute_repository_context(self):
        command = runner.app_image_build_command('uav-web-check:test', 'b' * 40, 'run-1')
        self.assertEqual(command[-1], str(ROOT))
        self.assertEqual(command[command.index('-f') + 1], str(ROOT / 'Dockerfile.web'))

    def test_runtime_container_inspection_fails_closed_on_mismatch(self):
        good = {
            'Id': 'container-1', 'Image': 'sha256:' + 'a' * 64,
            'HostConfig': {'NetworkMode': 'bridge'},
            'Config': {'User': '1000:1000', 'WorkingDir': '/workspace'},
            'Mounts': [
                {'Type': 'bind', 'Source': str(ROOT), 'Destination': '/workspace', 'RW': False},
                {'Type': 'volume', 'Name': 'config-owned', 'Destination': '/config', 'RW': True},
                {'Type': 'volume', 'Name': 'downloads-owned', 'Destination': '/downloads', 'RW': True},
            ],
        }
        expected = dict(container_id='container-1', image_id='sha256:' + 'a' * 64,
                        uid=1000, gid=1000, source=str(ROOT),
                        config_volume='config-owned', downloads_volume='downloads-owned')
        runner.validate_test_container(good, **expected)
        variants = [
            dict(good, Image='sha256:' + 'b' * 64),
            dict(good, HostConfig={'NetworkMode': 'host'}),
            dict(good, Config={'User': '0:0', 'WorkingDir': '/workspace'}),
            dict(good, Config={'User': '1000:1000', 'WorkingDir': '/tmp'}),
            dict(good, Mounts=good['Mounts'][:-1]),
            dict(good, Mounts=[dict(good['Mounts'][0], RW=True), *good['Mounts'][1:]]),
            dict(good, Mounts=[{key: value for key, value in good['Mounts'][0].items() if key != 'RW'}, *good['Mounts'][1:]]),
            dict(good, Mounts=[dict(good['Mounts'][0], Source='/tmp'), *good['Mounts'][1:]]),
            dict(good, Mounts=[good['Mounts'][0], dict(good['Mounts'][1], Name='foreign'), good['Mounts'][2]]),
            dict(good, Mounts=[*good['Mounts'], {'Type': 'volume', 'Name': 'extra', 'Destination': '/extra', 'RW': True}]),
        ]
        for runtime in variants:
            with self.subTest(runtime=runtime), self.assertRaises(runner.evidence.EvidenceError):
                runner.validate_test_container(runtime, **expected)

    def test_execute_runs_complete_orchestration_without_docker(self):
        head = 'a' * 40
        app_id = 'sha256:' + '1' * 64
        test_id = 'sha256:' + '2' * 64
        phases = []
        fake_docker = SimpleNamespace(evidence=None)
        resource = {'id': 'c' * 64, 'name': 'canonical-test', 'logs_status': 'captured'}

        def docker_for(ev):
            fake_docker.evidence = ev
            return fake_docker

        def metadata(docker, image, ev):
            phases.append('metadata')
            return {'image_id': app_id if image.startswith('uav-web-check:') else test_id}

        def inspect(docker, image):
            phases.append('image-inspect')
            labels = {runner.SOURCE_LABEL: head, runner.RUN_LABEL: fake_docker.evidence.run_id}
            if image == test_id:
                labels[runner.BASE_IMAGE_LABEL] = app_id
                return {'Id': image, 'RepoTags': [f'uav-web-tests:{head[:12]}-{fake_docker.evidence.run_id[:8]}'],
                        'Config': {'Labels': labels}, 'RootFS': {'Layers': ['app-layer', 'test-layer']}}
            return {'Id': app_id, 'RepoTags': [f'uav-web-check:{head[:12]}-{fake_docker.evidence.run_id[:8]}'],
                    'Config': {'Labels': labels}, 'RootFS': {'Layers': ['app-layer']}}

        def volume(docker, name, ev):
            self.assertIs(docker, fake_docker)
            self.assertIs(ev, fake_docker.evidence)
            self.assertIsInstance(name, str)
            phases.append('volume')
            return {'id': name, 'name': name}

        runtime = {
            'Id': resource['id'], 'Image': test_id,
            'HostConfig': {'NetworkMode': 'bridge'},
            'Config': {'User': f'{os.getuid()}:{os.getgid()}',
                       'WorkingDir': '/workspace'},
            'Mounts': [
                {'Type': 'bind', 'Source': str(ROOT), 'Destination': '/workspace', 'RW': False},
                {'Type': 'volume', 'Name': 'config-volume', 'Destination': '/config', 'RW': True},
                {'Type': 'volume', 'Name': 'downloads-volume', 'Destination': '/downloads', 'RW': True},
            ],
        }

        def create(docker, ev, image, command, **kwargs):
            phases.append('container-create')
            runtime['Mounts'][1]['Name'] = kwargs['mounts'][0][0]
            runtime['Mounts'][2]['Name'] = kwargs['mounts'][1][0]
            return resource

        def start(docker, ev, owned, timeout):
            phases.append('payload-start')
            self.assertIs(owned, resource)
            return 0, 'exited'

        def runtime_inspect(docker, owned, ev):
            phases.append('runtime-inspect')
            self.assertIs(owned, resource)
            return runtime

        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(source_sha=head, evidence_root=Path(temporary) / 'evidence', timeout=30)
            output = io.StringIO()
            with (mock.patch.object(runner, 'git_output', side_effect=lambda *args: head if args[0] == 'rev-parse' else ''),
                  mock.patch.object(runner.evidence, 'Docker', side_effect=docker_for),
                  mock.patch.object(runner.evidence, 'daemon_fingerprint', return_value='fingerprint'),
                  mock.patch.object(runner, 'build_image', side_effect=lambda *args, **kwargs: phases.append('build')),
                  mock.patch.object(runner.evidence, 'image_metadata', side_effect=metadata),
                  mock.patch.object(runner, 'inspect_image', side_effect=inspect),
                  mock.patch.object(runner.evidence, 'volume_metadata', autospec=True, side_effect=volume),
                  mock.patch.object(runner.evidence, 'create_container', side_effect=create),
                  mock.patch.object(runner.evidence, 'owned_container', side_effect=runtime_inspect),
                  mock.patch.object(runner.evidence, 'start_and_wait', side_effect=start),
                  mock.patch.object(runner.evidence, 'cleanup_resources', side_effect=lambda *args: phases.append('cleanup') or []),
                  redirect_stdout(output)):
                result = runner.execute(args)

        summary = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(summary['status'], 'passed')
        self.assertEqual(summary['cleanup'], 'complete')
        self.assertEqual(phases.count('build'), 2)
        self.assertEqual(phases.count('volume'), 2)
        self.assertLess(phases.index('container-create'), phases.index('payload-start'))
        self.assertLess(phases.index('runtime-inspect'), phases.index('payload-start'))
        self.assertLess(phases.index('payload-start'), phases.index('cleanup'))


if __name__ == '__main__':
    unittest.main()
