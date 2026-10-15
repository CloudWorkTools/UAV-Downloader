import importlib.util
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location('compare_upstream', Path(__file__).parents[1] / 'scripts/compare_upstream.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.check_output(['git', *args], text=True).strip()
    git('init', '-b', 'master')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    git('commit', '--allow-empty', '-m', 'base')
    git('branch', 'upstream')
    return git


def commit(git, path, text, message='change'):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git('add', path)
    git('commit', '-m', message)


def test_document_only_update_and_local_extension(repository):
    git = repository
    commit(git, 'uav-web/frontend/nas-settings.js', 'local NAS')
    git('switch', 'upstream')
    commit(git, 'docs/assets/star-history.svg', 'upstream chart', '<script>untrusted title</script>')
    git('switch', 'master')
    before = git('status', '--porcelain')
    report = module.compare('HEAD', 'upstream')
    assert '建議暫緩同步' in report
    assert '**1** 筆' in report
    assert '| Web／UI | 0 | 1 |' in report
    assert '| 文件／圖表 | 1 | 0 |' in report
    assert '&lt;script&gt;' in report
    assert '<script>' not in report
    assert git('status', '--porcelain') == before


def test_conflict_is_reported_without_merging(repository):
    git = repository
    commit(git, 'src/uav_downloader/core/config.py', 'base')
    git('branch', '-f', 'upstream', 'HEAD')
    commit(git, 'src/uav_downloader/core/config.py', 'local')
    git('switch', 'upstream')
    commit(git, 'src/uav_downloader/core/config.py', 'remote')
    git('switch', 'master')
    head = git('rev-parse', 'HEAD')
    report = module.compare('HEAD', 'upstream')
    assert '存在合併衝突' in report
    assert '`src/uav_downloader/core/config.py`' in report
    assert Path('src/uav_downloader/core/config.py').read_text() == 'local'
    assert git('rev-parse', 'HEAD') == head
    assert not Path('.git/MERGE_HEAD').exists()


def test_functional_change_requires_compatibility_checks(repository):
    git = repository
    git('switch', 'upstream')
    commit(git, 'src/uav_downloader/sites/jabletv.py', 'new parser')
    git('switch', 'master')
    report = module.compare('HEAD', 'upstream')
    assert '仍須通過 Web／UI 相容性檢查' in report
    assert '| 下載／字幕核心 | 1 | 0 |' in report


def test_up_to_date(repository):
    assert '無需同步' in module.compare('HEAD', 'upstream')
