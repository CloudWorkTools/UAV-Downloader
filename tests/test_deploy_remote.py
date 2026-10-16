import importlib.util
import json
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
