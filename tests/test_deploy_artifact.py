import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location('deploy_artifact', Path(__file__).parents[1] / 'scripts/deploy_artifact.py')
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)
SHA = 'a' * 40
IMAGE_ID = 'sha256:' + 'b' * 64


def write_artifact(directory):
    (directory / 'image.tar').write_bytes(b'tested Docker archive')
    data = dict(version=1, commit=SHA, run_id='123', run_attempt='2', image=f'uav-downloader:{SHA}',
                image_id=IMAGE_ID, archive_sha256=artifact.digest(directory / 'image.tar'), assets={'/': 'c' * 64})
    (directory / 'metadata.json').write_text(json.dumps(data))
    return data


@pytest.mark.parametrize('field,value', [('commit', 'd' * 40), ('run_id', '456'), ('run_attempt', '1'),
    ('image', 'wrong:tag'), ('archive_sha256', '0' * 64), ('image_id', 'wrong'),
    ('assets', {'/../secret': 'c' * 64, '/': 'c' * 64})])
def test_invalid_metadata_never_loads(tmp_path, field, value):
    data = write_artifact(tmp_path)
    data[field] = value
    (tmp_path / 'metadata.json').write_text(json.dumps(data))
    with patch.object(artifact, 'run') as run, pytest.raises(ValueError):
        artifact.verify_archive(SHA, '123', 2, tmp_path)
    run.assert_not_called()


def test_missing_metadata_never_loads(tmp_path):
    with patch.object(artifact, 'run') as run, pytest.raises(FileNotFoundError):
        artifact.verify_archive(SHA, '123', 2, tmp_path)
    run.assert_not_called()


def test_corrupt_archive_never_loads(tmp_path):
    write_artifact(tmp_path)
    (tmp_path / 'image.tar').write_bytes(b'tampered')
    with patch.object(artifact, 'run') as run, pytest.raises(ValueError):
        artifact.verify_archive(SHA, '123', 2, tmp_path)
    run.assert_not_called()


def test_loaded_image_identity(tmp_path):
    write_artifact(tmp_path)
    with patch.object(artifact, 'run', side_effect=[f'Loaded image: uav-downloader:{SHA}', 'sha256:' + 'd' * 64]), pytest.raises(ValueError):
        artifact.verify_archive(SHA, '123', 2, tmp_path)


def trusted_run():
    return dict(head_sha=SHA, status='completed', conclusion='success', path='.github/workflows/web-check.yml',
                event='workflow_dispatch', run_attempt=2, repository={'full_name': artifact.REPOSITORY},
                head_repository={'full_name': artifact.REPOSITORY})


def trusted_jobs():
    return {'jobs': [dict(name='Web / UI compatibility', conclusion='success', steps=[
        dict(name='將上游候選版本與主分支合併測試', conclusion='skipped'),
        dict(name='驗證部署提交', conclusion='success'), dict(name=artifact.EXPORT_STEP, conclusion='success')])]}


def test_download_and_load_trusted_artifact(tmp_path):
    calls = []
    def run(*args):
        calls.append(args)
        if args[:2] == ('gh', 'api'):
            return json.dumps(trusted_jobs() if '/jobs?' in args[2] else trusted_run())
        if args[:3] == ('gh', 'run', 'download'):
            write_artifact(tmp_path)
            return ''
        return IMAGE_ID if args[:3] == ('docker', 'image', 'inspect') else f'Loaded image: uav-downloader:{SHA}'
    with patch.object(artifact, 'run', side_effect=run):
        result = artifact.load_ci_image(SHA, '123', tmp_path)
    assert result['archive'] == tmp_path / 'image.tar'
    assert any('deployment-image-' + SHA + '-2' in call for call in calls)
    assert ('docker', 'load', '--input', str(tmp_path / 'image.tar')) in calls


@pytest.mark.parametrize('field,value', [('event', 'pull_request'), ('head_sha', 'd' * 40),
    ('conclusion', 'failure'), ('path', 'other.yml'), ('head_repository', {'full_name': 'fork/repo'})])
def test_untrusted_run_never_downloads(tmp_path, field, value):
    data = trusted_run()
    data[field] = value
    with patch.object(artifact, 'run', return_value=json.dumps(data)) as run, pytest.raises(ValueError):
        artifact.load_ci_image(SHA, '123', tmp_path)
    assert run.call_count == 1


def test_upstream_candidate_never_downloads(tmp_path):
    jobs = trusted_jobs()
    jobs['jobs'][0]['steps'][0]['conclusion'] = 'success'
    with patch.object(artifact, 'run', side_effect=[json.dumps(trusted_run()), json.dumps(jobs)]) as run, pytest.raises(ValueError):
        artifact.load_ci_image(SHA, '123', tmp_path)
    assert run.call_count == 2


def test_export_roundtrip(tmp_path):
    frontend = tmp_path / 'uav-web/frontend'
    frontend.mkdir(parents=True)
    (frontend / 'index.html').write_text('UI')
    output = tmp_path / 'artifact'
    calls = []
    def run(*args):
        calls.append(args)
        if args[0] == 'git':
            return SHA if args[-1] == 'HEAD' else ''
        if args[:2] == ('docker', 'save'):
            Path(args[3]).write_bytes(b'archive')
        return IMAGE_ID if args[:3] == ('docker', 'image', 'inspect') else f'Loaded image: uav-downloader:{SHA}'
    with patch.object(artifact, 'ROOT', tmp_path), patch.object(artifact, 'run', side_effect=run):
        artifact.export_image('tested:image', SHA, '123', '2', output)
    metadata = json.loads((output / 'metadata.json').read_text())
    assert metadata['assets']['/'] == artifact.digest(frontend / 'index.html')
    assert metadata['archive_sha256'] == artifact.digest(output / 'image.tar')
    assert ('docker', 'load', '--input', str(output / 'image.tar')) in calls


def test_archive_without_expected_tag_is_rejected(tmp_path):
    write_artifact(tmp_path)
    with patch.object(artifact, 'run', return_value='Loaded image: other:tag') as run, pytest.raises(ValueError):
        artifact.verify_archive(SHA, '123', 2, tmp_path)
    assert run.call_count == 1


@pytest.mark.parametrize('head,dirty', [(SHA, ' M Dockerfile.web'), ('d' * 40, '')])
def test_export_requires_clean_exact_checkout(tmp_path, head, dirty):
    with patch.object(artifact, 'run', side_effect=[head, dirty]) as run, pytest.raises(ValueError):
        artifact.export_image('tested:image', SHA, '123', '2', tmp_path / 'artifact')
    assert all(call.args[0] == 'git' for call in run.call_args_list)


def test_expired_artifact_does_not_build(tmp_path):
    import subprocess
    with patch.object(artifact, 'run', side_effect=[json.dumps(trusted_run()), json.dumps(trusted_jobs()),
            subprocess.CalledProcessError(1, 'gh run download')]) as run, pytest.raises(subprocess.CalledProcessError):
        artifact.load_ci_image(SHA, '123', tmp_path)
    assert all(call.args[0] == 'gh' for call in run.call_args_list)


def test_export_rejects_commit_tag_changed_by_concurrent_run(tmp_path):
    frontend = tmp_path / 'uav-web/frontend'
    frontend.mkdir(parents=True)
    (frontend / 'index.html').write_text('UI')
    calls = []
    def run(*args):
        calls.append(args)
        if args[0] == 'git':
            return SHA if args[-1] == 'HEAD' else ''
        if args[:2] == ('docker', 'save'):
            Path(args[3]).write_bytes(b'archive from raced commit tag')
        if args[:3] == ('docker', 'image', 'inspect'):
            return IMAGE_ID if args[-1] == 'tested:unique-run' else 'sha256:' + 'd' * 64
        return f'Loaded image: uav-downloader:{SHA}'
    with patch.object(artifact, 'ROOT', tmp_path), patch.object(artifact, 'run', side_effect=run), pytest.raises(ValueError, match='identity mismatch'):
        artifact.export_image('tested:unique-run', SHA, '123', '2', tmp_path / 'artifact')
    inspected = ('docker', 'image', 'inspect', '--format', '{{.Id}}', 'tested:unique-run')
    assert calls.index(inspected) < calls.index(('docker', 'tag', 'tested:unique-run', f'uav-downloader:{SHA}'))
    metadata = json.loads((tmp_path / 'artifact/metadata.json').read_text())
    assert metadata['image_id'] == IMAGE_ID
