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

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / '.deploy.local.json'
SPEACHES_IMAGE = 'ghcr.io/speaches-ai/speaches@sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8'


def load_config(path=CONFIG):
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
if old:
    docker('stop', name)
    docker('rename', name, name + '-previous')
try:
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
    print('Service startup: OK', flush=True)
    docker('exec', name, 'sh', '-c',
           'if [ "$PUID" -ne 0 ]; then exec gosu "$PUID:$PGID" python -c "$1"; else exec python -c "$1"; fi',
           'storage-probe', STORAGE_PROBE)
except Exception:
    subprocess.run(['docker', 'rm', '-f', name])
    if old:
        docker('rename', name + '-previous', name)
        docker('start', name)
    raise
print(f'Deployed and healthy: http://{host}:{port}')
if bundle and install_firecrawl:
    smoke = subprocess.run(['docker', 'exec', name, 'python', '-c',
        "from uav_downloader.core.firecrawl import scrape; scrape('https://example.com')"])
    print('Private Firecrawl functional scrape: ' + ('OK; test each target site in settings' if smoke.returncode == 0 else 'FAILED; keep disabled and inspect the stack'))
'''
REMOTE_INSTALL = REMOTE_INSTALL.replace('STORAGE_PROBE)', repr(STORAGE_PROBE) + ')')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-firecrawl', action='store_true', default=True, help='Build and deploy the private Firecrawl stack (default)')
    parser.add_argument('--without-firecrawl', action='store_false', dest='with_firecrawl', help='Skip installing Firecrawl; disable it in settings when no stack is running')
    options = parser.parse_args()
    config = load_config()
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise RuntimeError('Commit source changes before deploying; local target settings remain ignored')
    target = config['user'] + '@' + config['host']
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', target]
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    image = 'uav-downloader:' + commit
    images = [image, SPEACHES_IMAGE]
    bundle = base64.b64encode(json.dumps({
        'docker-compose.yml': (ROOT / 'docker-compose.yml').read_text(),
    }).encode()).decode()
    subprocess.run([*ssh, 'docker info >/dev/null'], check=True)
    if options.with_firecrawl:
        subprocess.run(['docker', 'buildx', 'version'], check=True)
    subprocess.run(['docker', 'pull', SPEACHES_IMAGE], check=True)
    subprocess.run(['docker', 'build', '-f', 'Dockerfile.web', '-t', image, '.'], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory(prefix='uav-deploy-') as temporary:
        if options.with_firecrawl:
            for component, folder in [('api', 'api'), ('browser', 'playwright-service-ts'), ('postgres', 'nuq-postgres')]:
                builder = ['docker', 'buildx', 'build', '--load'] if component == 'api' else ['docker', 'build']
                subprocess.run([*builder, '-t', f'uav-firecrawl-{component}:2.11.162',
                                f'https://github.com/firecrawl/firecrawl.git#v2.11.162:apps/{folder}'], check=True)
            for dependency in ('redis:7.4-alpine', 'rabbitmq:3.13-management'):
                subprocess.run(['docker', 'pull', dependency], check=True)
            images += ['uav-firecrawl-api:2.11.162', 'uav-firecrawl-browser:2.11.162',
                       'uav-firecrawl-postgres:2.11.162', 'redis:7.4-alpine', 'rabbitmq:3.13-management']
        archive = Path(temporary) / 'image.tar'
        subprocess.run(['docker', 'save', '-o', str(archive), *images], check=True)
        with archive.open('rb') as source:
            subprocess.run([*ssh, 'docker load'], stdin=source, check=True)
    args = [config['remote_dir'], image, config['host'], str(config['port']), bundle, '1' if options.with_firecrawl else '0']
    if config.get('staging_host_dir'):
        args.append(config['staging_host_dir'])
    subprocess.run([*ssh, 'python3 - ' + ' '.join(map(shlex.quote, args))], input=REMOTE_INSTALL, text=True, check=True)


if __name__ == '__main__':
    main()
