"""Compare committed upstream updates without checking out or executing them."""
from collections import Counter
from datetime import datetime, timezone
import html
import subprocess
import sys


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def category(path):
    if path.startswith('uav-web/'):
        return 'Web／UI'
    if path.startswith('src/uav_downloader/daemon/'):
        return 'NAS／背景服務'
    if path.startswith(('src/uav_downloader/sites/', 'src/uav_downloader/core/', 'src/uav_downloader/subtitles/')):
        return '下載／字幕核心'
    if path.startswith(('src/uav_downloader/apps/', 'src/uav_downloader/ui/', 'src/uav_downloader/i18n/')):
        return '桌面 UI／語系'
    if path.startswith('tests/'):
        return '測試'
    if path.startswith(('docs/', 'README', 'CONTRIBUTING', 'SECURITY', 'WINDOWS_SECURITY', 'THIRD_PARTY', 'LICENSE')):
        return '文件／圖表'
    if path.startswith(('.github/', 'scripts/', 'packaging/', 'Dockerfile', 'docker-compose', 'requirements', 'pyproject.toml')):
        return '工作流程／封裝'
    return '其他'


def compare(base_ref='HEAD', upstream_ref='upstream/master'):
    base = git('rev-parse', '--verify', '--end-of-options', base_ref + '^{commit}')
    upstream = git('rev-parse', '--verify', '--end-of-options', upstream_ref + '^{commit}')
    common = git('merge-base', base, upstream)
    local_count, upstream_count = map(int, git('rev-list', '--left-right', '--count', f'{base}...{upstream}').split())
    upstream_files = git('diff', '--name-only', common, upstream).splitlines()
    local_files = git('diff', '--name-only', common, base).splitlines()
    merge = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'merge-tree', '--write-tree',
                            '--name-only', '--no-messages', base, upstream], capture_output=True, text=True)
    if merge.returncode not in (0, 1):
        raise RuntimeError('無法評估合併狀態：' + merge.stderr)
    conflicts = merge.stdout.strip().splitlines()[1:]
    functional = any(category(path) != '文件／圖表' for path in upstream_files)
    if not upstream_count:
        advice = '上游提交均已納入，無需同步。'
    elif not upstream_files:
        advice = '上游有尚未納入的提交，但檔案內容與共同基準相同，暫無內容更新需要同步。'
    elif not functional:
        advice = '上游僅有文件／圖表更新，建議暫緩同步；沒有待納入的功能檔案變更。'
    elif conflicts:
        advice = '上游有功能或建置變更，但存在合併衝突；先人工整合，再執行 Web／UI 相容性檢查。'
    else:
        advice = '上游有功能或建置變更，可進入整合評估；仍須通過 Web／UI 相容性檢查後才決定是否合併。'
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    lines = ['# 上游與本專案差異追蹤', '', f'比較時間：{now}', '',
             '來源為原作者 master 分支的已提交更新，不讀取或依據上游 PR。', '',
             f'- 本專案版本：`{base}`', f'- 上游版本：`{upstream}`', f'- 共同基準：`{common}`',
             f'- 本專案獨有提交：**{local_count}** 筆', f'- 上游尚未納入提交：**{upstream_count}** 筆', '',
             '## 處理建議', '', advice, '',
             '此建議依檔案範圍與 Git 合併結果產生，沒有執行上游程式，不能代替功能相容性測試。', '',
             '## 模組差異', '', '| 模組 | 上游待納入檔案 | 本專案擴充檔案 |', '| --- | ---: | ---: |']
    incoming = Counter(map(category, upstream_files))
    local = Counter(map(category, local_files))
    for name in sorted(incoming.keys() | local.keys()):
        lines.append(f'| {name} | {incoming[name]} | {local[name]} |')
    lines += ['', '雙方檔案均各自與共同基準比較，可區分上游更新與本專案自行維護的擴充。', '',
              '## 合併衝突', '', '、'.join(f'`{html.escape(p)}`' for p in conflicts) if conflicts else '未偵測到文字合併衝突；不代表功能一定相容。']
    for title, start, end, paths in [('上游待納入更新', base, upstream, upstream_files),
                                     ('本專案自行維護的更新', upstream, base, local_files)]:
        commits = git('log', '-20', '--format=%h %cs %s', f'{start}..{end}')
        lines += ['', f'## {title}', '', '最近最多 20 筆提交：', '', '<pre>', html.escape(commits or '無'), '</pre>', '',
                  f'變更檔案共 {len(paths)} 個（各自相對共同基準；最多列出 80 個）：', '', '<pre>',
                  html.escape('\n'.join(paths[:80]) or '無'), '</pre>']
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    print(compare(*sys.argv[1:]), end='')
