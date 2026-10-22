import importlib.util
import json
import subprocess
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('deploy_remote', Path(__file__).parents[1] / 'scripts/deploy_remote.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_target_validation(tmp_path):
    path = tmp_path / 'config.json'
    config = dict(host='192.0.2.10', user='operator', port=8080, remote_dir='/home/operator/service')
    path.write_text(json.dumps(config))
    assert deploy.load_config(path) == config
    for field, value in [('host', 'host;command'), ('user', '-root'), ('port', True), ('remote_dir', '/'), ('remote_dir', '/home/../etc')]:
        path.write_text(json.dumps(dict(config, **{field: value})))
        with pytest.raises(ValueError):
            deploy.load_config(path)


@pytest.fixture
def deployment_worktree(tmp_path, monkeypatch):
    main, linked = tmp_path / 'main repo', tmp_path / 'linked worktree'
    main.mkdir()
    subprocess.run(['git', 'init', str(main)], check=True, capture_output=True)
    (main / '.gitignore').write_text('.deploy.local.json\n')
    subprocess.run(['git', '-C', str(main), 'add', '.gitignore'], check=True)
    subprocess.run(['git', '-C', str(main), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-m', 'initial'],
                   check=True, capture_output=True)
    subprocess.run(['git', '-C', str(main), 'worktree', 'add', '--detach', str(linked)],
                   check=True, capture_output=True)
    monkeypatch.setattr(deploy, 'ROOT', linked)
    return main, linked


@pytest.mark.parametrize('override', [False, True])
def test_worktree_loads_existing_config_without_copying(deployment_worktree, override):
    main, linked = deployment_worktree
    config = dict(host='192.0.2.10', user='operator', port=8080, remote_dir='/home/operator/service')
    (main / '.deploy.local.json').write_text(json.dumps(config))
    expected = dict(config, port=8081) if override else config
    if override:
        (linked / '.deploy.local.json').write_text(json.dumps(expected))
    assert deploy.load_config() == expected
    assert (linked / '.deploy.local.json').exists() is override


def test_worktree_never_replaces_invalid_or_explicit_missing_config(deployment_worktree):
    main, linked = deployment_worktree
    config = dict(host='192.0.2.10', user='operator', port=8080, remote_dir='/home/operator/service')
    (main / '.deploy.local.json').write_text(json.dumps(config))
    (linked / '.deploy.local.json').write_text('{invalid')
    with pytest.raises(json.JSONDecodeError):
        deploy.load_config()
    with pytest.raises(FileNotFoundError):
        deploy.load_config(linked / 'missing.json')


def test_worktree_reports_missing_config(deployment_worktree):
    with pytest.raises(FileNotFoundError):
        deploy.load_config()


def test_remote_installer_compiles():
    compile(deploy.REMOTE_INSTALL, '<remote installer>', 'exec')


@pytest.mark.parametrize('mount_staging', [False, True])
def test_remote_private_stack_keeps_its_own_secrets_and_network(monkeypatch, tmp_path, mount_staging):
    import base64
    import io
    import sys
    from types import SimpleNamespace
    import urllib.request

    (tmp_path / 'firecrawl').mkdir()
    for obsolete in ('docker-compose.web.yml', 'docker-compose.firecrawl.yml'):
        (tmp_path / 'firecrawl' / obsolete).write_text('old config')
    calls = []
    monkeypatch.setattr(deploy.subprocess, 'run', lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(deploy.subprocess, 'check_output', lambda *a, **kw: '')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: SimpleNamespace(
        open=lambda *a, **kw: io.BytesIO(b'{"status":"ok"}')))
    bundle = base64.b64encode(json.dumps({'docker-compose.yml': 'services: {}'}).encode()).decode()
    staging = tmp_path / 'staging'
    staging.mkdir()
    args = ['installer', str(tmp_path), 'app:test', '192.0.2.10', '8080', bundle]
    if mount_staging:
        args.extend(['1', str(staging)])
    monkeypatch.setattr(sys, 'argv', args)
    exec(deploy.REMOTE_INSTALL, {})
    secret = tmp_path / 'firecrawl' / '.env'
    assert secret.stat().st_mode & 0o777 == 0o600
    assert len(secret.read_text().split('=', 1)[1].strip()) >= 32
    compose = next(args for args in calls if args[:2] == ['docker', 'compose'])
    assert '--no-build' in compose and '--wait' in compose
    assert 'uav-speaches' in compose
    assert compose.count('-f') == 1 and '--profile' not in compose
    assert Path(compose[compose.index('-f') + 1]).name == 'docker-compose.yml'
    assert sorted(p.name for p in secret.parent.iterdir()) == ['.env', 'docker-compose.yml']
    run = next(args for args in calls if args[:2] == ['docker', 'run'])
    assert run[run.index('--network') + 1] == 'uav-firecrawl'
    assert 'UAV_FIRECRAWL_URL=http://uav-firecrawl-api:3002' in run
    assert ('--mount' in run) is mount_staging
    if mount_staging:
        assert f'type=bind,source={staging},target={staging}' in run


@pytest.mark.parametrize('value', ['/', '../staging', '/tmp/../etc', '/tmp/staging,readonly', 123])
def test_invalid_staging_mount_is_rejected(tmp_path, value):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(dict(host='192.0.2.10', user='operator', port=8080,
                                   remote_dir='/home/operator/service', staging_host_dir=value)))
    with pytest.raises(ValueError):
        deploy.load_config(path)


@pytest.mark.parametrize('arguments,expected', [([], True), (['--without-firecrawl'], False)])
def test_deployment_bundles_private_firecrawl_by_default(monkeypatch, tmp_path, arguments, expected):
    import sys
    calls = []
    monkeypatch.setattr(sys, 'argv', ['deploy_remote.py', *arguments])
    monkeypatch.setattr(deploy, 'load_config', lambda: dict(host='192.0.2.10', user='operator',
                                                          port=8080, remote_dir='/home/operator/service'))
    monkeypatch.setattr(deploy.subprocess, 'check_output',
                        lambda args, **kw: '' if 'status' in args else 'abc123')

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ['docker', 'save']:
            Path(args[3]).write_bytes(b'fake image archive')

    monkeypatch.setattr(deploy.subprocess, 'run', run)
    deploy.main()
    assert any('uav-firecrawl-api:2.11.162' in args for args in calls) is expected
    save = next(args for args in calls if args[:2] == ['docker', 'save'])
    assert deploy.SPEACHES_IMAGE in save
    assert deploy.SPEACHES_IMAGE in (deploy.ROOT / 'docker-compose.yml').read_text()
    remote = calls[-1][-1]
    assert remote.endswith('1' if expected else '0')
    assert ' 192.0.2.10 8080 ' in f' {remote} '
    ssh_calls = [args for args in calls if args and args[0] == 'ssh']
    assert ssh_calls
    assert all('-p' not in args for args in ssh_calls)
    assert all(args[:5] == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8'] for args in ssh_calls)
    assert "http://{host}:{port}/api/status" in deploy.REMOTE_INSTALL
    checked_image = next(args[-1] for args in calls if args[:2] == ['bash', 'scripts/check_web_container.sh'])
    assert checked_image == 'uav-downloader:abc123'
    assert checked_image in save
    assert calls.index(next(args for args in calls if args[:2] == ['bash', 'scripts/check_smb_batch_container.sh'])) < calls.index(save)


@pytest.mark.parametrize('failed_stage', ['test-build', 'web-tests', 'web-smoke', 'smb-smoke'])
def test_image_check_failure_blocks_remote_deployment(monkeypatch, failed_stage):
    import sys
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(sys, 'argv', ['deploy_remote.py', '--without-firecrawl'])
    monkeypatch.setattr(deploy, 'load_config', lambda: dict(host='192.0.2.10', user='operator',
                                                          port=8080, remote_dir='/home/operator/service'))
    monkeypatch.setattr(deploy.subprocess, 'check_output',
                        lambda args, **kw: '' if 'status' in args else 'abc123')
    def run(args, **kwargs):
        calls.append(args)
        stage = ('test-build' if 'Dockerfile.web-tests' in args else
                 'web-tests' if args[:2] == ['docker', 'run'] else
                 'web-smoke' if args[:2] == ['bash', 'scripts/check_web_container.sh'] else
                 'smb-smoke' if args[:2] == ['bash', 'scripts/check_smb_batch_container.sh'] else '')
        if stage == failed_stage:
            raise subprocess.CalledProcessError(7, args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(deploy.subprocess, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        deploy.main()
    assert not any(args[0] == 'ssh' or args[:2] == ['docker', 'save'] for args in calls)
    assert ['docker', 'image', 'rm', 'uav-downloader:abc123-tests'] in calls


def test_container_storage_probe_uses_effective_settings_and_cleans_files(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    staging, destination = tmp_path / 'staging', tmp_path / 'videos'
    staging.mkdir()
    destination.mkdir()
    video = destination / 'existing.mp4'
    video.write_bytes(b'keep')
    (tmp_path / 'ui_prefs.json').write_text(json.dumps({'staging_dir': str(staging), 'download_dir': str(destination)}))
    exec(deploy.STORAGE_PROBE, {})
    assert 'Staging write/cleanup: OK' in capsys.readouterr().out
    assert list(staging.iterdir()) == []
    assert list(destination.iterdir()) == [video]
    assert video.read_bytes() == b'keep'


@pytest.mark.parametrize('failure,reason', [(30, 'read-only filesystem'), (13, 'permission denied')])
def test_storage_probe_reports_write_failure_without_sensitive_details(monkeypatch, tmp_path, capsys, failure, reason):
    import os
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    (tmp_path / 'ui_prefs.json').write_text(json.dumps({'staging_dir': str(tmp_path)}))
    mkdir = os.mkdir

    def fail_probe(path, *args, **kwargs):
        if '.uav-write-test-' in str(path):
            raise OSError(failure, 'private-password')
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, 'mkdir', fail_probe)
    with pytest.raises(SystemExit) as failed:
        exec(deploy.STORAGE_PROBE, {})
    assert failed.value.code == 1
    output = capsys.readouterr().out
    assert f'Staging write/cleanup: FAILED ({reason})' in output
    assert 'private-password' not in output


def test_storage_probe_reports_cleanup_failure(monkeypatch, tmp_path, capsys):
    import shutil
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    monkeypatch.setenv('DOWNLOAD_DIR', str(tmp_path))

    def fail_cleanup(*args, **kwargs):
        raise PermissionError(13, 'private-password')

    monkeypatch.setattr(shutil, 'rmtree', fail_cleanup)
    with pytest.raises(SystemExit):
        exec(deploy.STORAGE_PROBE, {})
    output = capsys.readouterr().out
    assert 'Staging: SKIPPED (disabled)' in output
    assert 'Destination write/cleanup: FAILED (permission denied)' in output
    assert 'private-password' not in output


@pytest.mark.parametrize('failure_stage', ['connection', 'write'])
def test_storage_probe_distinguishes_nas_connection_from_write_failure(monkeypatch, tmp_path, capsys, failure_stage):
    import os
    import stat
    import smbclient
    from uav_downloader.daemon import nas
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    nas.save_settings(dict(enabled=True, mode='smb', host='nas.example', share='videos',
                           directory='', username='operator', password='private-password'))
    before = (tmp_path / 'nas.json').read_bytes()

    def fail(*args, **kwargs):
        raise PermissionError(13, 'private-password')

    monkeypatch.setattr(smbclient, 'register_session', fail if failure_stage == 'connection' else lambda *a, **kw: None)
    monkeypatch.setattr(smbclient, 'reset_connection_cache', lambda **kw: None)
    monkeypatch.setattr(smbclient, 'stat', lambda *a, **kw: os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 0, 0, 0, 0, 0, 0, 0)))
    monkeypatch.setattr(smbclient, 'mkdir', fail)
    with pytest.raises(SystemExit):
        exec(deploy.STORAGE_PROBE, {})
    output = capsys.readouterr().out
    expected = 'NAS connection: FAILED' if failure_stage == 'connection' else 'NAS destination write/cleanup: FAILED'
    assert expected in output
    assert ('NAS connection: OK' in output) is (failure_stage == 'write')
    assert 'private-password' not in output
    assert (tmp_path / 'nas.json').read_bytes() == before


@pytest.mark.parametrize('failure_stage', ['storage', 'mp4'])
def test_failed_container_storage_check_rolls_back_healthy_service(monkeypatch, tmp_path, capsys, failure_stage):
    import io
    import subprocess
    import sys
    from types import SimpleNamespace
    import urllib.request
    calls = []
    if failure_stage == 'mp4':
        from contextlib import nullcontext
        from uav_downloader.daemon import nas, storage_smoke
        monkeypatch.setenv('CONFIG_DIR', str(tmp_path / 'probe-config'))
        monkeypatch.setattr(nas, 'load_settings', lambda **kw: {'enabled': True, 'mode': 'smb'})
        monkeypatch.setattr(nas, '_connection', lambda *a, **kw: nullcontext((SimpleNamespace(stat=lambda *a, **kw: None), 'test', {})))
        monkeypatch.setattr(nas, 'open_smb_storage', lambda *a: nullcontext())
        monkeypatch.setattr('uav_downloader.core.storage.probe_destination', lambda *a: None)
        monkeypatch.setattr(storage_smoke, 'check_batch_smb', lambda settings: True)
        def fail_mp4(settings):
            raise ValueError('private-path-and-password')
        monkeypatch.setattr(storage_smoke, 'check_mp4_smb', fail_mp4)

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ['docker', 'exec']:
            assert args[2:5] == ['uav-downloader', 'sh', '-c']
            assert 'gosu "$PUID:$PGID"' in args[5]
            assert '"$PUID" -ne 0' in args[5]
            assert args[-1] == deploy.STORAGE_PROBE
            if failure_stage == 'mp4':
                with pytest.raises(SystemExit) as error:
                    exec(args[-1], {})
                assert error.value.code == 1
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(deploy.subprocess, 'run', run)
    monkeypatch.setattr(deploy.subprocess, 'check_output', lambda *a, **kw: 'uav-downloader\n')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: SimpleNamespace(
        open=lambda *a, **kw: io.BytesIO(b'{"status":"ok"}')))
    monkeypatch.setattr(sys, 'argv', ['installer', str(tmp_path), 'app:test', '192.0.2.10', '8080', ''])
    with pytest.raises(subprocess.CalledProcessError):
        exec(deploy.REMOTE_INSTALL, {})
    assert calls[-3:] == [['docker', 'rm', '-f', 'uav-downloader'],
                          ['docker', 'rename', 'uav-downloader-previous', 'uav-downloader'],
                          ['docker', 'start', 'uav-downloader']]
    output = capsys.readouterr().out
    assert 'Service startup: OK' in output
    if failure_stage == 'mp4':
        assert 'NAS MP4 Unicode remux/faststart/seek/cleanup: FAILED' in output
        assert 'private-path-and-password' not in output


def test_disabled_nas_does_not_require_saved_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    monkeypatch.setenv('DOWNLOAD_DIR', str(tmp_path))
    (tmp_path / 'nas.json').write_text(json.dumps({'enabled': False, 'password_encrypted': 'invalid'}))
    exec(deploy.STORAGE_PROBE, {})
    assert 'NAS: SKIPPED (disabled)' in capsys.readouterr().out


@pytest.mark.parametrize('staging_enabled', [False, True])
def test_upload_nas_still_requires_writable_local_destination(monkeypatch, tmp_path, capsys, staging_enabled):
    import errno
    import ntpath
    import os
    import shutil
    import smbclient
    import smbclient.shutil
    from uav_downloader.daemon import nas
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    local, remote, staging = [tmp_path / name for name in ('local', 'remote', 'staging')]
    for folder in (local, remote, staging):
        folder.mkdir()
    (tmp_path / 'ui_prefs.json').write_text(json.dumps({
        'download_dir': str(local), 'staging_dir': str(staging) if staging_enabled else ''}))
    nas.save_settings(dict(enabled=True, mode='upload', host='nas.example', share='videos',
                           directory='', username='operator', password='private-password'))

    def mapped(path):
        return remote.joinpath(*ntpath.relpath(path, r'\\nas.example\videos').split('\\'))

    monkeypatch.setattr(smbclient, 'register_session', lambda *a, **kw: None)
    monkeypatch.setattr(smbclient, 'reset_connection_cache', lambda **kw: None)
    monkeypatch.setattr(smbclient, 'stat', lambda path, **kw: mapped(path).stat())
    monkeypatch.setattr(smbclient, 'makedirs', lambda path, **kw: mapped(path).mkdir(exist_ok=kw['exist_ok']))
    monkeypatch.setattr(smbclient, 'mkdir', lambda path, **kw: mapped(path).mkdir())
    monkeypatch.setattr(smbclient, 'open_file', lambda path, **kw: mapped(path).open(kw['mode']))
    monkeypatch.setattr(smbclient, 'rename', lambda source, target, **kw: mapped(source).rename(mapped(target)))
    monkeypatch.setattr(smbclient, 'remove', lambda path, **kw: mapped(path).unlink())
    monkeypatch.setattr(smbclient.shutil, 'rmtree', lambda path, **kw: shutil.rmtree(mapped(path)))
    mkdir = os.mkdir

    def deny_local_probe(path, *args, **kwargs):
        if Path(path).parent == local and Path(path).name.startswith('.uav-write-test-'):
            raise PermissionError(errno.EACCES, 'private-password')
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, 'mkdir', deny_local_probe)
    with pytest.raises(SystemExit) as failed:
        exec(deploy.STORAGE_PROBE, {})
    assert failed.value.code == 1
    output = capsys.readouterr().out
    assert 'NAS destination write/cleanup: OK' in output
    assert 'Destination write/cleanup: FAILED (permission denied)' in output
    assert 'private-password' not in output
    assert list(remote.iterdir()) == []
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize('fails', [False, True])
def test_production_smb_batch_gate_runs_after_basic_write_probe(monkeypatch, tmp_path, capsys, fails):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from uav_downloader.daemon import nas, storage_smoke
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(nas, 'load_settings', lambda **kw: {'enabled': True, 'mode': 'smb'})
    monkeypatch.setattr(nas, '_connection', lambda *a, **kw: nullcontext((SimpleNamespace(stat=lambda *a, **kw: None), 'test', {})))
    monkeypatch.setattr(nas, 'open_smb_storage', lambda *a: nullcontext())
    monkeypatch.setattr('uav_downloader.core.storage.probe_destination', lambda *a: None)
    calls = []
    def check(settings):
        calls.append(settings['mode'])
        if fails:
            raise OSError('test failure')
        return True
    monkeypatch.setattr(storage_smoke, 'check_batch_smb', check)
    monkeypatch.setattr(storage_smoke, 'check_mp4_smb', lambda settings: True)
    if fails:
        with pytest.raises(SystemExit):
            exec(deploy.STORAGE_PROBE, {})
    else:
        exec(deploy.STORAGE_PROBE, {})
    assert calls == ['smb']
    output = capsys.readouterr().out
    assert 'NAS destination write/cleanup: OK' in output
    assert 'NAS concurrent batch write/read/cleanup: ' + ('FAILED' if fails else 'OK') in output


@pytest.mark.parametrize('fails', [False, True])
def test_production_mp4_failure_fails_storage_probe_without_leaking_details(monkeypatch, tmp_path, capsys, fails):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from uav_downloader.daemon import nas, storage_smoke
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(nas, 'load_settings', lambda **kw: {'enabled': True, 'mode': 'smb'})
    monkeypatch.setattr(nas, '_connection', lambda *a, **kw: nullcontext((SimpleNamespace(stat=lambda *a, **kw: None), 'test', {})))
    monkeypatch.setattr(nas, 'open_smb_storage', lambda *a: nullcontext())
    monkeypatch.setattr('uav_downloader.core.storage.probe_destination', lambda *a: None)
    monkeypatch.setattr(storage_smoke, 'check_batch_smb', lambda settings: True)
    def check(settings):
        if fails:
            raise ValueError('private-path-and-password')
        return True
    monkeypatch.setattr(storage_smoke, 'check_mp4_smb', check)
    if fails:
        with pytest.raises(SystemExit) as error:
            exec(deploy.STORAGE_PROBE, {})
        assert error.value.code == 1
    else:
        exec(deploy.STORAGE_PROBE, {})
    output = capsys.readouterr().out
    assert 'NAS MP4 Unicode remux/faststart/seek/cleanup: ' + ('FAILED' if fails else 'OK') in output
    assert 'private-path-and-password' not in output
