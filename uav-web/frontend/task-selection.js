window.createTaskSelection = ({taskAction}) => {
  const selected = new Set();
  let eligible = new Set();
  let busy = false;
  let selecting = false;
  const canCancel = task => ['pending', 'preparing', 'downloading', 'subtitles', 'uploading', 'publishing', 'pausing', 'paused', 'failed'].includes(task?.status);
  let tasksById = new Map();
  const groupOf = task => ['completed', 'skipped'].includes(task.status) ? 'completed' :
    ['pending', 'paused', 'failed', 'cancelled'].includes(task.status) ? 'waiting' : 'running';
  const canAction = (task, action) => ({
    edit: task?.subtitles_only ? [] : ['pending', 'preparing', 'downloading', 'paused', 'failed', 'cancelled'],
    start_now: ['pending', 'paused', 'failed', 'cancelled'],
    pause: ['pending', 'preparing', 'downloading'], resume: ['paused'],
    retry: ['failed', 'cancelled'], remove: ['completed', 'skipped', 'cancelled']
  }[action] || []).includes(task?.status);
  function update() {
    const mode = document.getElementById('queue-selection-mode');
    if (mode) {
      mode.disabled = busy || (!eligible.size && !selecting);
      mode.textContent = selecting ? '完成' : '選取';
      mode.setAttribute?.('aria-pressed', String(selecting));
    }
    const bar = document.getElementById('queue-batch-actions');
    if (bar) bar.hidden = !selecting;
    const count = document.getElementById('queue-selection-count');
    if (count) count.textContent = `已選 ${selected.size} 項`;
    document.getElementById('tasks-container')?.classList?.toggle('selecting', selecting);

    const all = eligible.size > 0 && [...eligible].every(id => selected.has(id));
    const toggle = document.getElementById('select-tasks-button');
    toggle.disabled = busy || !eligible.size;
    toggle.textContent = all ? '清除勾選' : '全選';
    const cancel = document.getElementById('cancel-tasks-button');
    const cancellable = selected.size;
    cancel.hidden = !cancellable;
    cancel.disabled = busy || !cancellable;
    cancel.textContent = busy ? '處理中…' : `取消／移除所選 (${cancellable})`;
    for (const action of ['pause', 'resume', 'retry', 'edit']) {
      const button = document.getElementById(`${action}-tasks-button`);
      if (button) {
        const count = [...selected].filter(id => canAction(tasksById.get(id), action)).length;
        button.hidden = !count;
        button.disabled = busy || !count;
        if (action === 'retry') button.textContent = `重新開始所選 (${count})`;
      }
    }
    document.querySelectorAll('#tasks-container input[type="checkbox"]').forEach(box => {
      box.checked = selected.has(box.dataset.taskId);
      box.disabled = busy;
    });
  }
  function setTasks(tasks) {
    tasksById = new Map(tasks.map(task => [task.task_id, task]));
    eligible = new Set(tasks.map(task => task.task_id));
    for (const id of selected) if (!eligible.has(id)) selected.delete(id);
    update();
  }
  function toggle(id, checked) {
    if (busy || !eligible.has(id)) return;
    if (checked) { selecting = true; selected.add(id); }
    else selected.delete(id);
    update();
  }
  function toggleAll() {
    if (busy) return;
    if (eligible.size && [...eligible].every(id => selected.has(id))) selected.clear();
    else { selecting = true; for (const id of eligible) selected.add(id); }
    update();
  }
  let removalGroups = new Map();
  function cancel(ids = [...selected]) {
    if (busy) return;
    removalGroups = new Map(ids.filter(id => tasksById.has(id)).map(id => [id, groupOf(tasksById.get(id))]));
    if (!removalGroups.size) return;
    for (const group of ['running', 'completed', 'waiting']) {
      const count = [...removalGroups.values()].filter(value => value === group).length;
      const box = document.getElementById(`task-remove-${group}`);
      box.checked = count > 0;
      box.disabled = !count;
      document.getElementById(`task-remove-${group}-count`).textContent = String(count);
    }
    document.getElementById('task-remove-error').textContent = '';
    document.getElementById('task-remove-dialog').showModal();
  }
  async function confirmCancel(event) {
    event.preventDefault();
    if (busy) return;
    const ids = [...removalGroups].filter(([, group]) => document.getElementById(`task-remove-${group}`).checked).map(([id]) => id);
    if (!ids.length) {
      document.getElementById('task-remove-error').textContent = '請至少勾選一種任務';
      return;
    }
    busy = true;
    update();
    document.getElementById('task-remove-dialog').close();
    const status = document.getElementById('queue-action-status');
    status.textContent = `正在取消、清理並移除 ${ids.length} 個任務…`;
    try {
      const response = await fetch('/api/download/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'cancel_remove', task_ids:ids})});
      const result = await response.json();
      if (!response.ok || !result.success) throw new Error(result.error || '批次取消失敗');
      const pending = new Set(ids);
      const failures = result.errors.map(item => item.error);
      for (const item of result.errors) pending.delete(item.task_id);
      let removed = 0;
      for (let attempt = 0; pending.size && attempt < 120; attempt++) {
        const refresh = await fetch('/api/download/tasks');
        const current = await refresh.json();
        if (!refresh.ok) throw new Error(current.error || '無法確認清理結果，請重新整理清單');
        const tasks = new Map(current.tasks.map(task => [task.task_id, task]));
        for (const id of pending) {
          const task = tasks.get(id);
          if (!task) {
            pending.delete(id);
            selected.delete(id);
            removed++;
          } else if (task.status === 'failed') {
            pending.delete(id);
            failures.push(task.error || '檔案清理失敗');
          }
        }
        status.textContent = `已移除 ${removed} 個；${pending.size} 個仍在停止或清理中…`;
        if (pending.size) await new Promise(resolve => setTimeout(resolve, 250));
      }
      status.textContent = `已移除 ${removed} 個；${failures.length} 個失敗${pending.size ? `；${pending.size} 個仍在背景處理，完成後會自動移除紀錄` : ''}。${failures.length ? ` ${failures[0]}` : ''}`;
    } catch (error) {
      status.textContent = `批次移除未完成：${error.message}；請確認清單後重試。`;
    } finally {
      busy = false;
      update();
    }
  }
  async function perform(action) {
    if (busy) return;
    busy = true;
    update();
    try {
      for (const id of [...selected]) {
        if (canAction(tasksById.get(id), action) && await taskAction(action, id)) selected.delete(id);
      }
    } finally { busy = false; update(); }
  }
  let editingIds = [];
  let savedNas = {};
  function updateEditStorage() {
    const mode = document.getElementById('task-edit-storage').value;
    const local = ['local', 'upload'].includes(mode);
    const smb = ['smb', 'upload'].includes(mode);
    document.getElementById('task-edit-local-fields').hidden = !local;
    document.getElementById('task-edit-nas-fields').hidden = !smb;
    document.getElementById('task-edit-direct-fields').hidden = mode !== 'direct';
    for (const key of ['host', 'share', 'directory', 'username', 'password', 'direct-path']) {
      const field = document.getElementById('task-edit-' + key);
      field.disabled = key === 'direct-path' ? mode !== 'direct' : !smb;
      field.required = !field.disabled && ['host', 'username', 'password', 'direct-path'].includes(key);
      if (key === 'password' && savedNas.password_saved) field.required = false;
    }
    const input = document.getElementById('task-edit-dest');
    input.disabled = !local;
    input.required = local;
    if (local) input.focus();
  }
  async function edit(taskId) {
    if (busy) return;
    editingIds = (taskId ? [taskId] : [...selected]).filter(id => canAction(tasksById.get(id), 'edit'));
    if (!editingIds.length) return;
    document.getElementById('task-edit-count').textContent = editingIds.length;
    document.getElementById('task-edit-storage').value = '';
    document.getElementById('task-edit-dest').value = '';
    document.getElementById('task-edit-staging-mode').value = '';
    document.getElementById('task-edit-staging').value = '';
    document.getElementById('task-edit-staging').disabled = true;
    for (const key of ['host', 'share', 'directory', 'username', 'password', 'direct-path']) document.getElementById('task-edit-' + key).value = '';
    savedNas = {};
    busy = true;
    update();
    let message = '';
    try {
      const response = await fetch('/api/settings');
      const settings = await response.json();
      if (!response.ok) throw new Error(settings.error || '讀取失敗');
      savedNas = settings.nas || {};
      for (const key of ['host', 'share', 'directory', 'username']) document.getElementById('task-edit-' + key).value = savedNas[key] || '';
      document.getElementById('task-edit-direct-path').value = savedNas.direct_path || '';
      document.getElementById('task-edit-dest').value = settings.download_dir || '';
      document.getElementById('task-edit-staging').value = settings.staging_dir || '';
      message = '已帶入設定區已儲存的內容，可在此單獨修改。';
    } catch (error) { message = `無法讀取預設設定：${error.message}；仍可自行填寫。`; }
    finally { busy = false; update(); }
    updateEditStorage();
    document.getElementById('task-edit-subtitles').value = '';
    document.getElementById('task-edit-status').textContent = message;
    document.getElementById('task-edit-dialog').showModal();
  }
  async function saveEdit(event) {
    event.preventDefault();
    if (busy) return;
    const storage = document.getElementById('task-edit-storage').value;
    const subtitles = document.getElementById('task-edit-subtitles').value;
    const status = document.getElementById('task-edit-status');
    const stagingMode = document.getElementById('task-edit-staging-mode').value;
    const changeStaging = stagingMode !== '';
    if (!storage && !subtitles && !changeStaging) { status.textContent = '請至少選擇一項修改'; return; }
    const payload = {action: 'update_settings', task_ids: editingIds};
    if (changeStaging) {
      payload.staging_dir = stagingMode === 'off' ? '' : document.getElementById('task-edit-staging').value.trim();
      if (stagingMode === 'on' && !payload.staging_dir) { status.textContent = '請填寫暫存區 path'; return; }
    }
    if (storage) payload.storage = storage;
    if (['local', 'upload'].includes(storage)) {
      payload.dest = document.getElementById('task-edit-dest').value.trim();
      if (!payload.dest) { status.textContent = '請填寫本機下載 path'; document.getElementById('task-edit-dest').focus(); return; }
    }
    if (['smb', 'upload', 'direct'].includes(storage)) {
      payload.storage = 'custom';
      payload.nas = {enabled: true, mode: storage};
      for (const key of ['host', 'share', 'directory', 'username', 'password']) payload.nas[key] = document.getElementById('task-edit-' + key).value;
      payload.nas.use_saved_password = !!savedNas.password_saved && !payload.nas.password;
      payload.nas.direct_path = document.getElementById('task-edit-direct-path').value;
    }
    if (subtitles) payload.subtitle_mode = subtitles;
    busy = true;
    update();
    const save = document.getElementById('task-edit-save');
    save.disabled = true;
    status.textContent = '驗證目的地並儲存中…';
    try {
      const running = editingIds.filter(id => ['preparing', 'downloading'].includes(tasksById.get(id)?.status));
      if (running.length) {
        status.textContent = '正在暫停下載，保留片段後套用設定…';
        for (const id of running) {
          const response = await fetch('/api/download/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'pause', task_id:id})});
          const result = await response.json();
          if (!response.ok || !result.success) throw new Error(result.error || '任務目前無法暫停，請稍後再試');
        }
        let stopped = false;
        for (let attempt = 0; attempt < 120; attempt++) {
          const response = await fetch('/api/download/tasks');
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || '無法確認暫停狀態');
          stopped = running.every(id => !result.tasks.some(task => task.task_id === id && ['preparing', 'downloading', 'pausing'].includes(task.status)));
          if (stopped) break;
          await new Promise(resolve => setTimeout(resolve, 250));
        }
        if (!stopped) throw new Error('任務仍在暫停中，請等下載停止後再套用');
      }
      const response = await fetch('/api/download/action', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok || !result.success) throw new Error(result.error || '修改失敗');
      if (!result.updated.length && result.skipped.length) throw new Error('任務狀態已變更，尚未套用設定；請確認任務已暫停後重試');
      for (const id of result.updated) selected.delete(id);
      document.getElementById('task-edit-password').value = '';
      document.getElementById('queue-action-status').textContent = `已修改 ${result.updated.length} 個任務；略過 ${result.skipped.length} 個（狀態已變更）。已暫停或失敗的任務請按繼續或重試。`;
      document.getElementById('task-edit-dialog').close();
    } catch (error) { status.textContent = error.message; }
    finally { busy = false; save.disabled = false; update(); }
  }
  function toggleMode() {
    if (busy) return;
    selecting = !selecting;
    if (!selecting) selected.clear();
    update();
  }
  return {toggleMode, setTasks, canAction, perform, canCancel, has: id => selected.has(id), toggle, toggleAll, cancel, confirmCancel, update, edit, saveEdit, updateEditStorage};
};
