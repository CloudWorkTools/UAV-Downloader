import ast
from concurrent.futures import ThreadPoolExecutor
from inspect import getsourcelines
from itertools import permutations, product
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
from unittest.mock import patch

from uav_downloader.daemon.subtitle_files import SubtitleFileManager
from uav_downloader.daemon.subtitle_tasks import LIBRARY_TRIAL_SECONDS, SubtitleTaskManager
from uav_downloader.subtitles import engine


def check_schedule(folder, trial, order):
    movie = folder / 'movie.mp4'
    movie.write_bytes(b'media fixture')
    movie.with_suffix('.ja.srt').write_text(
        '1\n00:00:01,000 --> 00:00:02,000\nおはようございます\n', encoding='utf-8')
    files = SubtitleFileManager(folder / 'files.json')
    video_id = files.register(str(movie), allowed_roots=[str(folder)]).video_id
    state = folder / 'tasks.json'
    manager = SubtitleTaskManager(state, source_manager=files)
    trial_seconds = LIBRARY_TRIAL_SECONDS if trial else None
    task_id = manager.submit_library_translation(video_id, trial_seconds)['task_id']
    with manager._lock:
        manager._active[task_id] = True
        manager._queue.remove(task_id)

    worker = SubtitleTaskManager._run_library_translation
    source, start_line = getsourcelines(worker)
    function_body = ast.parse(dedent(''.join(source))).body[0].body
    locks = [node for node in function_body if isinstance(node, ast.With)
             and any(ast.unparse(item.context_expr) == 'self._lock' for item in node.items)]
    assert len(locks) == 1, '無法定位發布前的任務鎖，請核對排程注入點'
    final_lock, = locks
    lock_line = start_line + final_lock.lineno - 1
    reached_final_lock = False
    cancelled = []
    publish_lock_held = []
    unlocked_lines = set()
    publish = files.publish_result

    def can_take_task_lock():
        if not manager._lock.acquire(blocking=False):
            return False
        manager._lock.release()
        return True

    def trace(frame, event, _arg):
        nonlocal reached_final_lock
        if frame.f_code is not worker.__code__:
            return None
        if event != 'line':
            return trace
        line = frame.f_lineno
        if line == lock_line and not reached_final_lock:
            reached_final_lock = True
            if order[0] == '取消':
                cancelled.append(manager.cancel(task_id))
        if line > lock_line and contender.submit(can_take_task_lock).result():
            unlocked_lines.add(line)
        return trace

    def observed_publish(*args, **kwargs):
        publish_lock_held.append(not contender.submit(can_take_task_lock).result())
        return publish(*args, **kwargs)

    previous_trace = sys.gettrace()
    with (ThreadPoolExecutor(max_workers=1) as contender,
          patch.object(files, 'publish_result', observed_publish)):
        sys.settrace(trace)
        try:
            manager._run_task(task_id)
        finally:
            sys.settrace(previous_trace)
    if order[0] == '完成':
        cancelled.append(manager.cancel(task_id))

    label = f'{"試翻" if trial else "整片"}：{" → ".join(order)}'
    assert reached_final_lock, f'{label}：流程未到達最後一道鎖，檢查無效'
    restored = SubtitleTaskManager(state, source_manager=files)
    task = restored.get_task(task_id)
    exported = sorted(path.name for path in folder.glob('movie.zh-TW*.srt'))
    if order[0] == '取消':
        assert cancelled == [True], f'{label}：取消未被接受：{cancelled}'
        assert not exported and not publish_lock_held, f'{label}：取消後仍發布：{exported}'
        assert not task['publish_status'], f'{label}：取消後仍留下發布紀錄'
        assert task['status'] == 'cancelled', f'{label}：取消後狀態為 {task["status"]}'
        preview_folder = manager._library_track_path(task_id, 'zh-TW').parent
        assert not preview_folder.exists(), f'{label}：取消後仍保留預覽'
    else:
        assert cancelled == [False], f'{label}：完成後仍接受取消：{cancelled}'
        assert task['status'] == 'completed', f'{label}：完成後狀態為 {task["status"]}'
        assert '早安' in restored.library_track(task_id, 'zh-TW'), f'{label}：重讀後缺少繁中預覽'
        assert len(exported) == (0 if trial else 1), f'{label}：發布檔案數有誤：{exported}'
        assert publish_lock_held == ([] if trial else [True]), f'{label}：發布時仍可搶到取消所用的鎖'
        if trial:
            assert not task['publish_status'], f'{label}：試翻仍留下發布紀錄'
        else:
            published = task['publish_status'].get('zh-TW')
            assert published == {'result_id': task_id, 'status': 'published', 'path': exported[0]}, (
                f'{label}：發布紀錄和實際檔案不符：{published}')
            assert '早安' in (folder / exported[0]).read_text(encoding='utf-8'), f'{label}：發布檔案缺少繁中字幕'
    assert not unlocked_lines, f'{label}：發布或標記完成前已放開任務鎖，程式行 {sorted(unlocked_lines)}'
    print(f'通過 {label}')


def main():
    if not __debug__:
        raise SystemExit('驗證需要啟用 assert，請移除 -O 或 PYTHONOPTIMIZE')
    with (patch.object(SubtitleTaskManager, 'start_dispatcher', lambda _manager: None),
          patch.object(engine, 'translate_cues', lambda texts, *_args: ['早安'] * len(texts)),
          patch.object(SubtitleTaskManager, '_library_asr', side_effect=AssertionError('找不到合成字幕'))):
        for trial, order in product((False, True), permutations(('取消', '完成'))):
            with TemporaryDirectory(prefix='subtitle-publication-') as folder:
                check_schedule(Path(folder), trial, order)


if __name__ == '__main__':
    main()
