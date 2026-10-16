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
        executable.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "$CHECK_LOG"\nexit {status}\n')
        executable.chmod(0o755)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['bash', str(root / 'scripts/check_web.sh')], cwd=tmp_path,
                            env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
                                 'PYTHON': str(tools / 'python-check'), 'CHECK_LOG': str(log)},
                            capture_output=True, text=True)
    assert result.returncode == expected, result.stderr
    calls = log.read_text().splitlines()
    assert calls[0].startswith('-m pytest -q ')
    if python_status:
        assert len(calls) == 1
    elif node_status:
        assert len(calls) == 2
    else:
        assert calls[1:] == [str(path.relative_to(root)) for path in sorted((root / 'tests').glob('*.cjs'))]
