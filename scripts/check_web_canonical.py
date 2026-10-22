#!/usr/bin/env python3
"""Run the repo's canonical web test image against a pinned clean source SHA."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import docker_evidence as evidence  # noqa: E402

SOURCE_LABEL = 'io.uav-downloader.canonical.source-sha'
RUN_LABEL = 'io.uav-downloader.canonical.run-id'
BASE_IMAGE_LABEL = 'io.uav-downloader.canonical.base-app-image-id'
NETWORK = 'bridge'
TEST_WORKDIR = '/workspace'


def validate_source(*, head: str, expected: str, status: str, uid: int, gid: int,
                    network: str = NETWORK) -> None:
    if not re.fullmatch(r'[0-9a-f]{40}', expected) or head != expected:
        raise evidence.EvidenceError('source SHA does not match HEAD')
    if status:
        raise evidence.EvidenceError('source checkout is dirty; commit or remove tracked and untracked changes')
    if type(uid) is not int or type(gid) is not int or uid <= 0 or gid < 0:
        raise evidence.EvidenceError('canonical test runner requires a non-root UID and valid GID')
    if network != NETWORK:
        raise evidence.EvidenceError('canonical test runner requires the CI bridge network')


def validate_image(obj: dict, *, image_id: str, tag: str, source_sha: str, run_id: str,
                   base_app_image_id: str | None = None,
                   base_app_layers: list[str] | None = None) -> None:
    config = obj.get('Config') or {}
    labels = config.get('Labels') or {}
    if obj.get('Id') != image_id or tag not in (obj.get('RepoTags') or []):
        raise evidence.EvidenceError('built image identity is stale or does not match its run tag')
    if labels.get(SOURCE_LABEL) != source_sha or labels.get(RUN_LABEL) != run_id:
        raise evidence.EvidenceError('built image source/run labels do not match this evidence run')
    if base_app_image_id is not None and labels.get(BASE_IMAGE_LABEL) != base_app_image_id:
        raise evidence.EvidenceError('test image was not built from the pinned app image ID')
    if base_app_layers is not None:
        layers = ((obj.get('RootFS') or {}).get('Layers'))
        if (not base_app_layers or not isinstance(layers, list)
                or layers[:len(base_app_layers)] != base_app_layers):
            raise evidence.EvidenceError('test image root filesystem does not descend from the app image')


def validate_app_tag_stable(before: dict, after: dict, *, tag: str,
                            image_id: str, layers: list[str]) -> None:
    if (before.get('Id') != image_id or after.get('Id') != image_id
            or tag not in (before.get('RepoTags') or [])
            or after.get('RepoTags') != before.get('RepoTags')):
        raise evidence.EvidenceError('app image tag changed during test image build')
    if ((before.get('RootFS') or {}).get('Layers') != layers
            or (after.get('RootFS') or {}).get('Layers') != layers):
        raise evidence.EvidenceError('app image root filesystem changed during test image build')


def validate_test_container(obj: dict, *, container_id: str, image_id: str,
                            uid: int, gid: int, source: str,
                            config_volume: str, downloads_volume: str) -> None:
    config = obj.get('Config') or {}
    host = obj.get('HostConfig') or {}
    if obj.get('Id') != container_id or obj.get('Image') != image_id:
        raise evidence.EvidenceError('test container identity or immutable image changed; preserving it')
    if host.get('NetworkMode') != NETWORK:
        raise evidence.EvidenceError('test container runtime network is not the approved bridge network')
    if config.get('User') != f'{uid}:{gid}' or config.get('WorkingDir') != TEST_WORKDIR:
        raise evidence.EvidenceError('test container runtime user or working directory does not match the runner')

    expected = {
        ('bind', TEST_WORKDIR): (str(Path(source).resolve()), False, None),
        ('volume', '/config'): (None, True, config_volume),
        ('volume', '/downloads'): (None, True, downloads_volume),
    }
    actual = {}
    for mount in obj.get('Mounts') or []:
        key = (mount.get('Type'), mount.get('Destination'))
        if key in actual:
            raise evidence.EvidenceError('test container has duplicate runtime mounts')
        actual[key] = mount
    if set(actual) != set(expected):
        raise evidence.EvidenceError('test container runtime mounts do not match the isolated runner mounts')
    for key, (source_path, writable, volume_name) in expected.items():
        mount = actual[key]
        if type(mount.get('RW')) is not bool or mount['RW'] is not writable:
            raise evidence.EvidenceError('test container runtime mount access does not match the runner')
        if source_path is not None and str(Path(mount.get('Source', '')).resolve()) != source_path:
            raise evidence.EvidenceError('test container source bind does not match the clean checkout')
        if volume_name is not None and mount.get('Name') != volume_name:
            raise evidence.EvidenceError('test container scratch volume identity changed')


def git_output(*args: str) -> str:
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def inspect_image(docker: evidence.Docker, image: str) -> dict:
    result = docker.call(['image', 'inspect', image])
    raw = evidence.parse_json(result, 'could not inspect built image')
    if not isinstance(raw, list) or len(raw) != 1:
        raise evidence.EvidenceError('image inspect returned an unexpected result')
    return raw[0]


def build_image(docker: evidence.Docker, args: list[str], label: str, timeout: float) -> None:
    result = docker.call(args, timeout=timeout, save_logs=True, log_prefix=label)
    if result.returncode:
        raise evidence.EvidenceError(f'{label} image build failed; inspect private evidence')


def test_image_build_command(tag: str, source_sha: str, run_id: str,
                             app_image_tag: str, app_image_id: str) -> list[str]:
    args = ['build', '-f', str(ROOT / 'Dockerfile.web-tests'), '--build-arg',
            f'APP_IMAGE={app_image_tag}', '-t', tag]
    for label in (f'{SOURCE_LABEL}={source_sha}', f'{RUN_LABEL}={run_id}',
                  f'{BASE_IMAGE_LABEL}={app_image_id}'):
        args.extend(['--label', label])
    args.append(str(ROOT))
    return args


def app_image_build_command(tag: str, source_sha: str, run_id: str) -> list[str]:
    args = ['build', '-f', str(ROOT / 'Dockerfile.web'), '-t', tag]
    for label in (f'{SOURCE_LABEL}={source_sha}', f'{RUN_LABEL}={run_id}'):
        args.extend(['--label', label])
    args.append(str(ROOT))
    return args


def execute(args: argparse.Namespace) -> int:
    head = git_output('rev-parse', 'HEAD')
    status = git_output('status', '--porcelain', '--untracked-files=all')
    uid, gid = os.getuid(), os.getgid()
    validate_source(head=head, expected=args.source_sha, status=status, uid=uid, gid=gid)

    ev = evidence.Evidence(args.evidence_root, 'canonical-web-check')
    docker = evidence.Docker(ev)
    run_id = ev.run_id
    app_tag = f'uav-web-check:{head[:12]}-{run_id[:8]}'
    test_tag = f'uav-web-tests:{head[:12]}-{run_id[:8]}'
    summary = {'run_id': run_id, 'source_sha': head, 'status': 'failed',
               'evidence_path': str(ev.path)}
    old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def interrupt(signum, _frame):
        raise evidence.Interrupted(signum)

    for signum in old_handlers:
        signal.signal(signum, interrupt)

    try:
        fingerprint = evidence.daemon_fingerprint(docker)
        ev.update(source_sha=head, runner_uid=uid, runner_gid=gid, network=NETWORK,
                  daemon_context_fingerprint=fingerprint, status='building')

        build_image(docker, app_image_build_command(app_tag, head, run_id),
                    'app-build', args.timeout)
        app = evidence.image_metadata(docker, app_tag, ev)
        app_image_id = app.get('image_id')
        if not isinstance(app_image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', app_image_id):
            raise evidence.EvidenceError('app image did not provide an immutable image ID')
        app_inspect = inspect_image(docker, app_tag)
        validate_image(app_inspect, image_id=app_image_id, tag=app_tag,
                       source_sha=head, run_id=run_id)
        app_layers = ((app_inspect.get('RootFS') or {}).get('Layers'))
        if not isinstance(app_layers, list) or not app_layers:
            raise evidence.EvidenceError('app image did not provide root filesystem layers')

        test_build = test_image_build_command(test_tag, head, run_id, app_tag, app_image_id)
        ev.update(app_image_id=app_image_id, app_image_tag=app_tag,
                  test_image_tag=test_tag, test_build_base_image_id=app_image_id)
        build_image(docker, test_build, 'test-build', args.timeout)
        test = evidence.image_metadata(docker, test_tag, ev)
        test_image_id = test.get('image_id')
        if not isinstance(test_image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', test_image_id):
            raise evidence.EvidenceError('test image did not provide an immutable image ID')
        validate_image(inspect_image(docker, test_image_id), image_id=test_image_id, tag=test_tag,
                       source_sha=head, run_id=run_id, base_app_image_id=app_image_id,
                       base_app_layers=app_layers)
        app_after = inspect_image(docker, app_tag)
        validate_app_tag_stable(app_inspect, app_after, tag=app_tag,
                                image_id=app_image_id, layers=app_layers)
        ev.update(test_image_id=test_image_id,
                  app_image_metadata=app, test_image_metadata=test)

        config_volume = evidence.volume_metadata(
            docker=docker, name=evidence.new_volume_name(ev, 'config'), ev=ev)
        downloads_volume = evidence.volume_metadata(
            docker=docker, name=evidence.new_volume_name(ev, 'downloads'), ev=ev)
        resource = evidence.create_container(
            docker, ev, test_image_id, [],
            mounts=[(config_volume['id'], '/config', False),
                    (downloads_volume['id'], '/downloads', False)],
            bind_mounts=[(str(ROOT), TEST_WORKDIR, True)], network=NETWORK,
            user=(uid, gid), workdir=TEST_WORKDIR, name_suffix='canonical-test')
        runtime = evidence.owned_container(docker, resource, ev)
        validate_test_container(
            runtime, container_id=resource['id'], image_id=test_image_id,
            uid=uid, gid=gid, source=str(ROOT),
            config_volume=config_volume['id'], downloads_volume=downloads_volume['id'])
        validate_source(head=git_output('rev-parse', 'HEAD'), expected=head,
                        status=git_output('status', '--porcelain', '--untracked-files=all'),
                        uid=os.getuid(), gid=os.getgid())
        ev.update(status='running')
        exit_code, state = evidence.start_and_wait(docker, ev, resource, args.timeout)
        run_state = ev.manifest.get('status')
        if run_state == 'timeout':
            summary['status'] = 'timeout'
        elif run_state == 'interrupted':
            summary.update(status='interrupted', signal=ev.manifest.get('signal'))
        else:
            summary['status'] = 'passed' if exit_code == 0 and state == 'exited' else 'failed'
        summary.update(
                       payload_exit_code=exit_code, container_state=state,
                       container_id=resource['id'], app_image_id=app_image_id,
                       test_image_id=test_image_id)
        if summary['status'] not in ('timeout', 'interrupted'):
            ev.update(status=summary['status'], payload_exit_code=exit_code, container_state=state)
        evidence.add_log_status(summary, resource)
    except evidence.Interrupted as exc:
        ev.update(status='interrupted', signal=exc.signum)
        summary.update(status='interrupted', signal=exc.signum)
    except (evidence.EvidenceError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        ev.update(status='failed', failure_type=type(exc).__name__)
        summary['error'] = str(exc) if isinstance(exc, evidence.EvidenceError) else type(exc).__name__
    finally:
        retained = evidence.cleanup_resources(docker, ev)
        summary['cleanup'] = 'complete' if not retained else 'pending'
        if retained:
            summary['retained_resources'] = retained
        ev._save_manifest()
        summary['manifest_status'] = ev.manifest.get('status')
        summary['evidence_path'] = str(ev.path)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)

    print(json.dumps(summary, sort_keys=True))
    if summary['status'] == 'interrupted':
        return 128 + int(summary.get('signal', signal.SIGINT))
    if summary['status'] == 'passed' and summary['cleanup'] == 'complete':
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-sha', required=True, help='full expected clean Git HEAD SHA')
    parser.add_argument('--evidence-root', type=Path, default=evidence.DEFAULT_EVIDENCE_ROOT)
    parser.add_argument('--timeout', type=evidence.positive_timeout, default=1200,
                        help='build or test timeout in seconds (maximum 86400)')
    args = parser.parse_args(argv)
    try:
        return execute(args)
    except evidence.EvidenceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError) as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
