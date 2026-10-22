"""The shared Web check command must fail closed and discover frontend tests."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('python_status,node_status,expected', [(9, 0, 9), (0, 7, 7), (0, 0, 0)])
def test_web_check_command(tmp_path, python_status, node_status, expected):
    tools = tmp_path / 'tools'
    tools.mkdir()
    log = tmp_path / 'calls'
    for name, status in [('python-check', python_status), ('node', node_status)]:
        executable = tools / name
        executable.write_text(f'#!/bin/sh\nprintf "%s" "$PYTHONPATH" > "$CHECK_IMPORTS"\nprintf "%s\\n" "$*" >> "$CHECK_LOG"\nexit {status}\n')
        executable.chmod(0o755)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['bash', str(root / 'scripts/check_web.sh')], cwd=tmp_path,
                            env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
                                 'PYTHON': str(tools / 'python-check'), 'CHECK_LOG': str(log),
                                 'PYTHONPATH': '/extra/modules', 'CHECK_IMPORTS': str(tmp_path / 'imports')},
                            capture_output=True, text=True)
    assert result.returncode == expected, result.stderr
    assert (tmp_path / 'imports').read_text().split(os.pathsep) == [str(root / 'src'), '/extra/modules']
    calls = log.read_text().splitlines()
    assert calls[0].startswith('-m pytest -q ')
    assert 'tests/test_crawler_naming.py' in calls[0].split()
    if python_status:
        assert len(calls) == 1
    elif node_status:
        assert len(calls) == 2
    else:
        assert calls[1:] == [str(path.relative_to(root)) for path in sorted((root / 'tests').glob('*.cjs'))]


@pytest.mark.parametrize('append_status', [0, 7])
def test_smb_container_gate_runs_append_and_propagates_failure(tmp_path, append_status):
    import json
    import sys
    tools = tmp_path / 'tools'
    tools.mkdir()
    log = tmp_path / 'docker.jsonl'
    docker = tools / 'docker'
    docker.write_text(f'''#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
append = args[0] == 'run' and any(arg.endswith('-append') for arg in args)
source = sys.stdin.read() if append else ''
with open(os.environ['DOCKER_LOG'], 'a') as output:
    output.write(json.dumps(dict(args=args, source=source)) + '\\n')
sys.exit(int(os.environ['APPEND_STATUS']) if append else 0)
''')
    docker.chmod(0o755)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['bash', 'scripts/check_smb_batch_container.sh', 'test-image'], cwd=root,
                            env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
                                 'DOCKER_LOG': str(log), 'APPEND_STATUS': str(append_status)},
                            capture_output=True, text=True, timeout=10)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    append = [call for call in calls if call['args'][0] == 'run' and any(arg.endswith('-append') for arg in call['args'])]
    assert len(append) == 1, 'CI must run the dedicated SMB append acceptance'
    assert 'batch append SMB: PASS' in append[0]['source']
    assert result.returncode == append_status, result.stderr
    assert any(call['args'][:2] == ['rm', '-fv'] and any(arg.endswith('-append') for arg in call['args']) for call in calls)
    if append_status:
        assert not any(call['args'][0] == 'run' and any(arg.endswith('-subtitles') for arg in call['args']) for call in calls)
