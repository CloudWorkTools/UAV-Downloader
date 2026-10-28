"""Deploy using this computer's Git-ignored SSH target configuration."""
import ipaddress
import argparse
import base64
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tempfile

from deploy_artifact import load_ci_image

ROOT = Path(__file__).resolve().parents[1]
SPEACHES_IMAGE = 'ghcr.io/speaches-ai/speaches@sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8'


def load_config(path=None):
    if path is None:
        path = ROOT / '.deploy.local.json'
        if not path.exists():
            first = subprocess.check_output(
                ['git', 'worktree', 'list', '--porcelain', '-z'], cwd=ROOT, text=True).split('\0', 1)[0]
            path = Path(first.removeprefix('worktree ')) / '.deploy.local.json'
    config = json.loads(path.read_text())
    if ipaddress.ip_address(config['host']).version != 4:
        raise ValueError('Deployment requires an IPv4 address')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', config['user']):
        raise ValueError('Invalid SSH user')
    folder = PurePosixPath(config['remote_dir'])
    if not folder.is_absolute() or str(folder) == '/' or '..' in folder.parts or not re.fullmatch(r'[A-Za-z0-9_./-]+', config['remote_dir']):
        raise ValueError('Invalid remote directory')
    if type(config['port']) is not int or not 1024 <= config['port'] <= 65535:
        raise ValueError('Invalid service port')
    if 'staging_host_dir' in config:
        staging = config['staging_host_dir']
        if not isinstance(staging, str) or not staging.startswith('/') or staging == '/' or '..' in PurePosixPath(staging).parts or not re.fullmatch(r'[A-Za-z0-9_./-]+', staging):
            raise ValueError('Invalid staging host directory')
    return config


STORAGE_PROBE = r'''
import errno, os, sys
from uav_downloader.core import config
from uav_downloader.core.storage import probe_destination
from uav_downloader.daemon import nas
stage = 'Storage settings'
try:
    prefs = config._load_prefs()
    staging = prefs.get('staging_dir', '').strip()
    if staging:
        stage = 'Staging write/cleanup'
        probe_destination(staging)
        print(stage + ': OK', flush=True)
    else:
        print('Staging: SKIPPED (disabled)', flush=True)
    stage = 'NAS settings'
    settings = nas.load_settings()
    if not settings['enabled']:
        print('NAS: SKIPPED (disabled)', flush=True)
        stage = 'Destination write/cleanup'
        probe_destination(prefs.get('download_dir') or os.environ.get('DOWNLOAD_DIR') or os.path.abspath('download'))
    elif settings['mode'] == 'direct':
        stage = 'NAS mount'
        destination = nas.direct_destination(settings)
        print(stage + ': OK', flush=True)
        stage = 'NAS destination write/cleanup'
        probe_destination(destination)
    else:
        settings = nas.load_settings(secret=True)
        stage = 'NAS connection'
        with nas._connection(settings, create_directory=False) as (smb, root, options):
            smb.stat(root, **options)
        print(stage + ': OK', flush=True)
        stage = 'NAS destination write/cleanup'
        destination = nas.smb_destination()
        with nas.open_smb_storage(settings, destination):
            probe_destination(destination)
    print(stage + ': OK', flush=True)
    if settings['enabled'] and settings['mode'] == 'smb':
        stage = 'NAS concurrent batch write/read/cleanup'
        from uav_downloader.daemon.storage_smoke import check_batch_smb
        if not check_batch_smb(settings):
            raise RuntimeError('SMB batch check did not run')
        print(stage + ': OK', flush=True)
        stage = 'NAS MP4 Unicode remux/faststart/seek/cleanup'
        from uav_downloader.daemon.storage_smoke import check_mp4_smb
        if not check_mp4_smb(settings):
            raise RuntimeError('SMB MP4 check did not run')
        print(stage + ': OK', flush=True)
    if settings['enabled'] and settings['mode'] == 'upload':
        stage = 'Destination write/cleanup'
        probe_destination(prefs.get('download_dir') or os.environ.get('DOWNLOAD_DIR') or os.path.abspath('download'))
        print(stage + ': OK', flush=True)
except Exception as exc:
    reason = {errno.EROFS: 'read-only filesystem', errno.EACCES: 'permission denied',
              errno.EPERM: 'operation not permitted', errno.ENOSPC: 'no space left',
              errno.ENOENT: 'directory missing'}.get(getattr(exc, 'errno', None), 'check configuration, connection and storage permissions')
    print(stage + ': FAILED (' + reason + ')', flush=True)
    sys.exit(1)
'''

REMOTE_INSTALL = r'''
import base64, json, pathlib, secrets, subprocess, sys, time, urllib.request
root, image, host, port, bundle = sys.argv[1:6]
assets = json.loads(sys.argv[8]) if len(sys.argv) > 8 else {}
install_firecrawl = len(sys.argv) < 7 or sys.argv[6] == '1'
staging_dir = sys.argv[7] if len(sys.argv) > 7 else ''
root = pathlib.Path(root)
name = 'uav-downloader'
def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, **kwargs)
mount_args = []
if staging_dir:
    if not pathlib.Path(staging_dir).is_dir():
        raise ValueError('Staging host directory does not exist')
    mount_args = ['--mount', f'type=bind,source={staging_dir},target={staging_dir}']
for folder in ('config', 'downloads', 'appdata'):
    (root / folder).mkdir(parents=True, exist_ok=True, mode=0o700)
stack = root / 'firecrawl'
if bundle:
    stack.mkdir(parents=True, exist_ok=True, mode=0o700)
    for filename, content in json.loads(base64.b64decode(bundle)).items():
        if filename != 'docker-compose.yml':
            raise ValueError('Invalid bundled compose file')
        (stack / filename).write_text(content)
    env_file = stack / '.env'
    if not env_file.exists():
        with open(env_file, 'x', opener=lambda path, flags: __import__('os').open(path, flags, 0o600)) as output:
            output.write('FIRECRAWL_POSTGRES_PASSWORD=' + secrets.token_urlsafe(40) + '\n')
    docker('compose', '-p', 'uav-firecrawl', '--env-file', str(env_file),
           '-f', str(stack / 'docker-compose.yml'),
           'up', '-d', '--no-build', '--wait', '--wait-timeout', '180',
           'uav-speaches', *(['uav-firecrawl-api'] if install_firecrawl else []))
    for obsolete in ('docker-compose.web.yml', 'docker-compose.firecrawl.yml'):
        (stack / obsolete).unlink(missing_ok=True)
network_args = []
if (stack / '.env').exists() and subprocess.run(
        ['docker', 'network', 'inspect', 'uav-firecrawl'], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL).returncode == 0:
    network_args = ['--network', 'uav-firecrawl', '-e', 'UAV_FIRECRAWL_URL=http://uav-firecrawl-api:3002']
existing = subprocess.check_output(['docker', 'ps', '-a', '--format', '{{.Names}}'], text=True).splitlines()
if name + '-previous' in existing:
    docker('rm', name + '-previous')
old = name in existing
renamed = False
replacement_attempted = False
try:
    if old:
        docker('stop', name)
        docker('rename', name, name + '-previous')
        renamed = True
    replacement_attempted = True
    docker('run', '-d', '--name', name, '--restart', 'unless-stopped',
           '-p', f'{host}:{port}:8080', '-e', 'TZ=Asia/Taipei',
           '-e', 'APPDATA=/appdata',
           '-v', f'{root}/config:/config', '-v', f'{root}/downloads:/downloads',
           '-v', f'{root}/appdata:/appdata', *mount_args, *network_args, image)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(60):
        try:
            with opener.open(f'http://{host}:{port}/api/status', timeout=2) as response:
                if json.load(response).get('status') == 'ok':
                    break
        except Exception:
            pass
        time.sleep(1)
    else:
        raise RuntimeError('New deployment failed health check')
    if assets:
        import hashlib
        info = json.loads(subprocess.check_output(['docker', 'inspect', name], text=True))[0]
        if (not info['State']['Running'] or info['Config']['Image'] != image
                or (len(sys.argv) > 10 and info['Image'] != sys.argv[10])):
            raise RuntimeError('Deployed revision mismatch')
        for path, digest in assets.items():
            with opener.open(f'http://{host}:{port}' + path, timeout=20) as response:
                if hashlib.sha256(response.read()).hexdigest() != digest:
                    raise RuntimeError('Deployed frontend asset mismatch')
        print('Deployed revision and frontend assets: OK', flush=True)
    print('Service startup: OK', flush=True)
    docker('exec', name, 'sh', '-c',
           'if [ "$PUID" -ne 0 ]; then exec gosu "$PUID:$PGID" python -c "$1"; else exec python -c "$1"; fi',
           'storage-probe', STORAGE_PROBE)
except Exception:
    if replacement_attempted:
        subprocess.run(['docker', 'rm', '-f', name])
    if renamed:
        docker('rename', name + '-previous', name)
    if old:
        docker('start', name)
    raise
print('Deployed and healthy')
if bundle and install_firecrawl:
    smoke = subprocess.run(['docker', 'exec', name, 'python', '-c',
        "from uav_downloader.core.firecrawl import scrape; scrape('https://example.com')"])
    print('Private Firecrawl functional scrape: ' + ('OK; test each target site in settings' if smoke.returncode == 0 else 'FAILED; keep disabled and inspect the stack'))
'''
REMOTE_INSTALL = REMOTE_INSTALL.replace('STORAGE_PROBE)', repr(STORAGE_PROBE) + ')')
QUEUE_SOURCE = (ROOT / 'scripts/deploy_queue.py').read_text()
REMOTE_INSTALL = QUEUE_SOURCE + """
import sys, subprocess
queue = DeploymentQueue(sys.argv[1], 'http://' + sys.argv[3] + ':' + sys.argv[4])
queue.lock()
if queue.path.exists():
    raise RuntimeError('Queue recovery required: run --recover-queue before deploying')
existing = subprocess.check_output(['docker', 'ps', '-a', '--format', '{{.Names}}'], text=True).splitlines()
if len(sys.argv) > 9:
    expected = sys.argv[9]
    current = (subprocess.check_output(['docker', 'inspect', 'uav-downloader', '--format', '{{.Config.Image}}'], text=True).strip()
               if 'uav-downloader' in existing else '-')
    if current != expected:
        raise RuntimeError('Running deployment changed after revision check; retry deployment')
if len(sys.argv) > 10:
    loaded = subprocess.check_output(['docker', 'image', 'inspect', sys.argv[2], '--format', '{{.Id}}'], text=True).strip()
    if loaded != sys.argv[10]:
        raise RuntimeError('Loaded CI image ID mismatch; service remains unchanged')
try:
    if 'uav-downloader' in existing:
        queue.pause()
    exec(INSTALL_SOURCE)
finally:
    queue.resume()
""".replace('INSTALL_SOURCE', repr(REMOTE_INSTALL))


def check_ci(commit, run_id=None):
    if run_id is None:
        runs = json.loads(subprocess.check_output(['gh', 'run', 'list', '--workflow', 'web-check.yml',
            '--commit', commit, '--limit', '20', '--json', 'databaseId,event,status,conclusion'], cwd=ROOT, text=True))
        runs = [run for run in runs if run.get('event') in ('push', 'workflow_dispatch')
                and run.get('status') == 'completed' and run.get('conclusion') == 'success']
        if not runs:
            raise RuntimeError('No successful exact-head deployment CI; dispatch web-check.yml for this branch')
        run_id = str(runs[0]['databaseId'])
    if not str(run_id).isdecimal():
        raise ValueError('Invalid CI run ID')
    run = json.loads(subprocess.check_output(['gh', 'api', 'repos/{owner}/{repo}/actions/runs/' + str(run_id)], cwd=ROOT, text=True))
    if (run.get('head_sha') != commit or run.get('status') != 'completed'
            or run.get('conclusion') != 'success' or run.get('path') != '.github/workflows/web-check.yml'):
        raise RuntimeError('Deployment requires successful same-HEAD web-check.yml CI')
    jobs = json.loads(subprocess.check_output(['gh', 'api', 'repos/{owner}/{repo}/actions/runs/' + str(run_id) + '/jobs?per_page=100'], cwd=ROOT, text=True))
    if not any(job.get('name') == 'Web / UI compatibility' and job.get('conclusion') == 'success'
               and any(step.get('name') == '驗證部署提交' and step.get('conclusion') == 'success'
                       for step in job.get('steps', [])) for job in jobs.get('jobs', [])):
        raise RuntimeError('CI lacks successful checked-out deployment revision proof')
    print('Exact-revision GitHub CI: OK', flush=True)
    return str(run_id)


def remote_deployed_image(ssh):
    code = """import subprocess
names = subprocess.check_output(['docker', 'ps', '-a', '--format', '{{.Names}}'], text=True).splitlines()
print(subprocess.check_output(['docker', 'inspect', 'uav-downloader', '--format', '{{.Config.Image}}'], text=True).strip() if 'uav-downloader' in names else '-')
"""
    return subprocess.check_output([*ssh, 'python3 -'], input=code, text=True).strip()


def check_deployment_revision(image, commit, *, allow_rollback=False):
    if image == '-':
        return
    match = re.fullmatch(r'uav-downloader:([0-9a-f]{40})', image)
    if not match:
        raise RuntimeError('Running deployment revision is unknown; inspect it before deploying')
    revision = match.group(1)
    known = subprocess.run(['git', 'cat-file', '-e', revision + '^{commit}'], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not known:
        raise RuntimeError('Running deployment revision is unknown locally; fetch it before deploying')
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', revision, commit], cwd=ROOT).returncode == 0
    if not ancestor and not allow_rollback:
        raise RuntimeError('Running deployment is not an ancestor of the candidate; use --allow-rollback only for an intentional rollback')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--update-services', action='store_true', help='Explicitly update Speaches and the private Firecrawl stack')
    parser.add_argument('--with-firecrawl', action='store_true', dest='update_services', help='Alias for --update-services')
    parser.add_argument('--without-firecrawl', action='store_false', dest='with_firecrawl', default=True, help='Explicitly update Speaches without installing Firecrawl')
    parser.add_argument('--ci-run', help='Successful web-check.yml run ID (default: newest run for HEAD)')
    parser.add_argument('--recover-queue', action='store_true', help='Resume only deployment-paused tasks from the remote journal')
    parser.add_argument('--allow-rollback', action='store_true', help='Allow an intentional deployment that does not contain the running revision')
    options = parser.parse_args()
    config = load_config()
    if options.recover_queue:
        ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', config['user'] + '@' + config['host']]
        code = QUEUE_SOURCE + '\nqueue = DeploymentQueue(' + repr(config['remote_dir']) + ', ' + repr('http://' + config['host'] + ':' + str(config['port'])) + '); queue.lock(); queue.restore_service(); queue.resume()'
        subprocess.run([*ssh, 'python3 -'], input=code, text=True, check=True)
        print('Deployment queue recovery: OK', flush=True)
        return
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise RuntimeError('Commit source changes before deploying; local target settings remain ignored')
    target = config['user'] + '@' + config['host']
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', target]
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    run_id = check_ci(commit, options.ci_run)
    with tempfile.TemporaryDirectory(prefix='uav-deploy-') as temporary:
        artifact = load_ci_image(commit, run_id, Path(temporary) / 'artifact')
        image = artifact['image']
        deployed_image = remote_deployed_image(ssh)
        check_deployment_revision(deployed_image, commit, allow_rollback=options.allow_rollback)
        if deployed_image != '-':
            subprocess.run(['git', 'fetch', 'origin', 'master'], cwd=ROOT, check=True)
            if subprocess.run(['git', 'merge-base', '--is-ancestor', deployed_image.split(':', 1)[1], 'FETCH_HEAD'], cwd=ROOT).returncode:
                print('Warning: deployed revision is not included in origin/master; retain and merge its PR', flush=True)
        subprocess.run([*ssh, 'docker info >/dev/null'], check=True)
        bundle = ''
        if options.update_services or not options.with_firecrawl:
            bundle = base64.b64encode(json.dumps({
                'docker-compose.yml': (ROOT / 'docker-compose.yml').read_text(),
            }).encode()).decode()
            images = [SPEACHES_IMAGE]
            subprocess.run(['docker', 'pull', SPEACHES_IMAGE], check=True)
            if options.with_firecrawl:
                subprocess.run(['docker', 'buildx', 'version'], check=True)
                for component, folder in [('api', 'api'), ('browser', 'playwright-service-ts'), ('postgres', 'nuq-postgres')]:
                    builder = ['docker', 'buildx', 'build', '--load'] if component == 'api' else ['docker', 'build']
                    subprocess.run([*builder, '-t', f'uav-firecrawl-{component}:2.11.162',
                                    f'https://github.com/firecrawl/firecrawl.git#v2.11.162:apps/{folder}'], check=True)
                for dependency in ('redis:7.4-alpine', 'rabbitmq:3.13-management'):
                    subprocess.run(['docker', 'pull', dependency], check=True)
                images += ['uav-firecrawl-api:2.11.162', 'uav-firecrawl-browser:2.11.162',
                           'uav-firecrawl-postgres:2.11.162', 'redis:7.4-alpine', 'rabbitmq:3.13-management']
            dependencies = Path(temporary) / 'services.tar'
            subprocess.run(['docker', 'save', '-o', str(dependencies), *images], check=True)
            with dependencies.open('rb') as source:
                subprocess.run([*ssh, 'docker load'], stdin=source, check=True)
        with artifact['archive'].open('rb') as source:
            subprocess.run([*ssh, 'docker load'], stdin=source, check=True)
        args = [config['remote_dir'], image, config['host'], str(config['port']), bundle, '1' if options.with_firecrawl else '0']
        args.extend([config.get('staging_host_dir', ''), json.dumps(artifact['assets']), deployed_image, artifact['image_id']])
        subprocess.run([*ssh, 'python3 - ' + ' '.join(map(shlex.quote, args))], input=REMOTE_INSTALL, text=True, check=True)



if __name__ == '__main__':
    main()
