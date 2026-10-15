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
