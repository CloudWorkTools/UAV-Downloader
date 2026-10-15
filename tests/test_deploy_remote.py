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


def test_remote_private_stack_keeps_its_own_secrets_and_network(monkeypatch, tmp_path):
    import base64
    import io
    import sys
    from types import SimpleNamespace
    import urllib.request

    calls = []
    monkeypatch.setattr(deploy.subprocess, 'run', lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(deploy.subprocess, 'check_output', lambda *a, **kw: '')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: SimpleNamespace(
        open=lambda *a, **kw: io.BytesIO(b'{"status":"ok"}')))
    bundle = base64.b64encode(json.dumps({'docker-compose.web.yml': 'services: {}',
                                         'docker-compose.firecrawl.yml': 'services: {}'}).encode()).decode()
    monkeypatch.setattr(sys, 'argv', ['installer', str(tmp_path), 'app:test', '192.0.2.10', '8080', bundle])
    exec(deploy.REMOTE_INSTALL, {})
    secret = tmp_path / 'firecrawl' / '.env'
    assert secret.stat().st_mode & 0o777 == 0o600
    assert len(secret.read_text().split('=', 1)[1].strip()) >= 32
    compose = next(args for args in calls if args[:2] == ['docker', 'compose'])
    assert '--no-build' in compose and '--wait' in compose
    run = next(args for args in calls if args[:2] == ['docker', 'run'])
    assert run[run.index('--network') + 1] == 'uav-firecrawl'
    assert 'UAV_FIRECRAWL_URL=http://uav-firecrawl-api:3002' in run
