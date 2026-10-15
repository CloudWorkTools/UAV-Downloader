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
    return config


REMOTE_INSTALL = r'''
import base64, json, pathlib, secrets, subprocess, sys, time, urllib.request
root, image, host, port, bundle = sys.argv[1:]
root = pathlib.Path(root)
name = 'uav-downloader'
def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, **kwargs)
for folder in ('config', 'downloads', 'appdata'):
    (root / folder).mkdir(parents=True, exist_ok=True, mode=0o700)
stack = root / 'firecrawl'
if bundle:
    stack.mkdir(parents=True, exist_ok=True, mode=0o700)
    for filename, content in json.loads(base64.b64decode(bundle)).items():
        if filename not in ('docker-compose.web.yml', 'docker-compose.firecrawl.yml'):
            raise ValueError('Invalid bundled compose file')
        (stack / filename).write_text(content)
    env_file = stack / '.env'
    if not env_file.exists():
        with open(env_file, 'x', opener=lambda path, flags: __import__('os').open(path, flags, 0o600)) as output:
            output.write('FIRECRAWL_POSTGRES_PASSWORD=' + secrets.token_urlsafe(40) + '\n')
    docker('compose', '-p', 'uav-firecrawl', '--env-file', str(env_file),
           '-f', str(stack / 'docker-compose.web.yml'), '-f', str(stack / 'docker-compose.firecrawl.yml'),
           '--profile', 'firecrawl', 'up', '-d', '--no-build', '--wait', '--wait-timeout', '180', 'uav-firecrawl-api')
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
           '-v', f'{root}/appdata:/appdata', *network_args, image)
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
except Exception:
    subprocess.run(['docker', 'rm', '-f', name])
    if old:
        docker('rename', name + '-previous', name)
        docker('start', name)
    raise
print(f'Deployed and healthy: http://{host}:{port}')
if bundle:
    smoke = subprocess.run(['docker', 'exec', name, 'python', '-c',
        "from uav_downloader.core.firecrawl import scrape; scrape('https://example.com')"])
    print('Private Firecrawl functional scrape: ' + ('OK; test each target site in settings' if smoke.returncode == 0 else 'FAILED; keep disabled and inspect the stack'))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-firecrawl', action='store_true', help='Build and deploy a separate private Firecrawl stack')
    options = parser.parse_args()
    config = load_config()
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise RuntimeError('Commit source changes before deploying; local target settings remain ignored')
    target = config['user'] + '@' + config['host']
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', target]
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    image = 'uav-downloader:' + commit
    images = [image]
    bundle = ''
    subprocess.run([*ssh, 'docker info >/dev/null'], check=True)
    subprocess.run(['docker', 'build', '-f', 'Dockerfile.web', '-t', image, '.'], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory(prefix='uav-deploy-') as temporary:
        if options.with_firecrawl:
            for component, folder in [('api', 'api'), ('browser', 'playwright-service-ts'), ('postgres', 'nuq-postgres')]:
                subprocess.run(['docker', 'build', '-t', f'uav-firecrawl-{component}:2.11.162',
                                f'https://github.com/firecrawl/firecrawl.git#v2.11.162:apps/{folder}'], check=True)
            for dependency in ('redis:7.4-alpine', 'rabbitmq:3.13-management'):
                subprocess.run(['docker', 'pull', dependency], check=True)
            images += ['uav-firecrawl-api:2.11.162', 'uav-firecrawl-browser:2.11.162',
                       'uav-firecrawl-postgres:2.11.162', 'redis:7.4-alpine', 'rabbitmq:3.13-management']
            files = {filename: (ROOT / filename).read_text() for filename in
                     ('docker-compose.web.yml', 'docker-compose.firecrawl.yml')}
            bundle = base64.b64encode(json.dumps(files).encode()).decode()
        archive = Path(temporary) / 'image.tar'
        subprocess.run(['docker', 'save', '-o', str(archive), *images], check=True)
        with archive.open('rb') as source:
            subprocess.run([*ssh, 'docker load'], stdin=source, check=True)
    args = [config['remote_dir'], image, config['host'], str(config['port']), bundle]
    subprocess.run([*ssh, 'python3 - ' + ' '.join(map(shlex.quote, args))], input=REMOTE_INSTALL, text=True, check=True)


if __name__ == '__main__':
    main()
