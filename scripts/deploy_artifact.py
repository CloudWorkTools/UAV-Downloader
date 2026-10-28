"""Export and consume the exact application image verified by trusted CI."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'CloudWorkTools/UAV-Downloader'
EXPORT_STEP = '匯出並驗證部署映像'


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def validate(metadata, commit, run_id, attempt, directory):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid deployment commit')
    expected = {'version': 1, 'commit': commit, 'run_id': str(run_id),
                'run_attempt': str(attempt), 'image': f'uav-downloader:{commit}'}
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise ValueError('Deployment artifact provenance mismatch')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', metadata.get('image_id', '')):
        raise ValueError('Invalid image identity')
    assets = metadata.get('assets')
    if not isinstance(assets, dict) or '/' not in assets:
        raise ValueError('Missing frontend identities')
    for path, value in assets.items():
        if (not re.fullmatch(r'/[A-Za-z0-9_./-]*', path)
                or any(part in ('.', '..') for part in path.split('/'))
                or '//' in path or not re.fullmatch(r'[0-9a-f]{64}', value)):
            raise ValueError('Invalid frontend identity')
    archive = directory / 'image.tar'
    if (not re.fullmatch(r'[0-9a-f]{64}', metadata.get('archive_sha256', ''))
            or digest(archive) != metadata['archive_sha256']):
        raise ValueError('Deployment archive checksum mismatch')
    return dict(metadata, archive=archive)


def verify_archive(commit, run_id, attempt, directory):
    directory = Path(directory)
    metadata = validate(json.loads((directory / 'metadata.json').read_text()),
                        commit, run_id, attempt, directory)
    loaded = run('docker', 'load', '--input', str(metadata['archive']))
    if f"Loaded image: {metadata['image']}" not in loaded.splitlines():
        raise ValueError('Archive did not load the expected image tag')
    if run('docker', 'image', 'inspect', '--format', '{{.Id}}', metadata['image']) != metadata['image_id']:
        raise ValueError('Loaded image identity mismatch')
    return metadata


def load_ci_image(commit, run_id, directory):
    if not str(run_id).isdecimal() or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid CI deployment identity')
    workflow = json.loads(run('gh', 'api', f'repos/{REPOSITORY}/actions/runs/{run_id}'))
    if (workflow.get('head_sha') != commit or workflow.get('status') != 'completed'
            or workflow.get('conclusion') != 'success'
            or workflow.get('path') != '.github/workflows/web-check.yml'
            or workflow.get('event') not in ('push', 'workflow_dispatch')
            or workflow.get('repository', {}).get('full_name') != REPOSITORY
            or workflow.get('head_repository', {}).get('full_name') != REPOSITORY):
        raise ValueError('Untrusted deployment workflow run')
    attempt = workflow['run_attempt']
    jobs = json.loads(run('gh', 'api', f'repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100'))
    matching = [job for job in jobs['jobs'] if job['name'] == 'Web / UI compatibility']
    steps = {step['name']: step.get('conclusion') for job in matching for step in job.get('steps', [])}
    if (len(matching) != 1 or matching[0].get('conclusion') != 'success'
            or steps.get('將上游候選版本與主分支合併測試') != 'skipped'
            or steps.get('驗證部署提交') != 'success' or steps.get(EXPORT_STEP) != 'success'):
        raise ValueError('CI did not verify an exact deployment commit')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError('Artifact download directory must be empty')
    run('gh', 'run', 'download', str(run_id), '-R', REPOSITORY, '--name',
        f'deployment-image-{commit}-{attempt}', '--dir', str(directory))
    return verify_archive(commit, run_id, attempt, directory)


def export_image(image, commit, run_id, attempt, directory):
    if not re.fullmatch(r'[0-9a-f]{40}', commit) or not str(run_id).isdecimal() or not str(attempt).isdecimal():
        raise ValueError('Invalid CI identity')
    if run('git', '-C', str(ROOT), 'rev-parse', 'HEAD') != commit or run('git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('Image export requires a clean exact checkout')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    frontend = ROOT / 'uav-web/frontend'
    assets = {'/' if p.name == 'index.html' and p.parent == frontend else '/' + p.relative_to(frontend).as_posix(): digest(p)
              for p in frontend.rglob('*') if p.is_file()}
    # Check the embedded UI actually served by the exported binary.
    check = '''import hashlib,json,subprocess,time,urllib.request,sys
p=subprocess.Popen(['uav-web','--no-supervisor'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
 for attempt in range(60):
  try:
   urllib.request.urlopen('http://127.0.0.1:8080/',timeout=1); break
  except OSError: time.sleep(.5)
 for path,expected in json.loads(sys.argv[1]).items():
  actual=urllib.request.urlopen('http://127.0.0.1:8080'+path,timeout=5).read()
  assert hashlib.sha256(actual).hexdigest()==expected,path
finally: p.terminate(); p.wait()
'''
    run('docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python', image, '-c', check, json.dumps(assets))
    tag = f'uav-downloader:{commit}'
    image_id = run('docker', 'image', 'inspect', '--format', '{{.Id}}', image)
    run('docker', 'tag', image, tag)
    archive = directory / 'image.tar'
    run('docker', 'save', '--output', str(archive), tag)
    metadata = dict(version=1, commit=commit, run_id=str(run_id), run_attempt=str(attempt),
                    image=tag, image_id=image_id, archive_sha256=digest(archive), assets=assets)
    (directory / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    verify_archive(commit, run_id, attempt, directory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export')
    for name in ('image', 'commit', 'run-id', 'run-attempt', 'directory'):
        export.add_argument('--' + name, required=True)
    args = parser.parse_args()
    export_image(args.image, args.commit, args.run_id, args.run_attempt, args.directory)
