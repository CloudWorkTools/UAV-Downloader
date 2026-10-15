"""Deploy using this computer's Git-ignored SSH target configuration."""
import ipaddress
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
import json, pathlib, subprocess, sys, time, urllib.request
root, image, host, port = sys.argv[1:]
root = pathlib.Path(root)
name = 'uav-downloader'
def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, **kwargs)
for folder in ('config', 'downloads', 'appdata'):
    (root / folder).mkdir(parents=True, exist_ok=True, mode=0o700)
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
           '-v', f'{root}/appdata:/appdata', image)
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
'''


def main():
    config = load_config()
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise RuntimeError('Commit source changes before deploying; local target settings remain ignored')
    target = config['user'] + '@' + config['host']
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', target]
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    image = 'uav-downloader:' + commit
    subprocess.run([*ssh, 'docker info >/dev/null'], check=True)
    subprocess.run(['docker', 'build', '-f', 'Dockerfile.web', '-t', image, '.'], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory(prefix='uav-deploy-') as temporary:
        archive = Path(temporary) / 'image.tar'
        subprocess.run(['docker', 'save', '-o', str(archive), image], check=True)
        with archive.open('rb') as source:
            subprocess.run([*ssh, 'docker load'], stdin=source, check=True)
    args = [config['remote_dir'], image, config['host'], str(config['port'])]
    subprocess.run([*ssh, 'python3 - ' + ' '.join(map(shlex.quote, args))], input=REMOTE_INSTALL, text=True, check=True)


if __name__ == '__main__':
    main()
