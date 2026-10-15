window.createTaskSelection = ({cancelTask, taskAction}) => {
  const selected = new Set();
  let eligible = new Set();
  let busy = false;
  const canCancel = task => ['pending', 'preparing', 'downloading', 'subtitles', 'uploading', 'publishing', 'paused', 'failed'].includes(task?.status);
  let tasksById = new Map();
  const canAction = (task, action) => ({
    edit: task?.subtitles_only ? [] : ['pending', 'paused', 'failed', 'cancelled'],
    pause: ['pending', 'preparing', 'downloading'], resume: ['paused'],
    retry: ['failed', 'cancelled'], remove: ['completed', 'skipped', 'cancelled', 'failed']
  }[action] || []).includes(task?.status);
  function update() {
    const all = eligible.size > 0 && [...eligible].every(id => selected.has(id));
    const toggle = document.getElementById('select-tasks-button');
    toggle.disabled = busy || !eligible.size;
    toggle.textContent = all ? '取消全選' : '全選';
    const cancel = document.getElementById('cancel-tasks-button');
    const cancellable = [...selected].filter(id => canCancel(tasksById.get(id))).length;
    cancel.disabled = busy || !cancellable;
    cancel.textContent = busy ? '處理中…' : `取消所選 (${cancellable})`;
    for (const action of ['pause', 'resume', 'retry', 'edit']) {
      const button = document.getElementById(`${action}-tasks-button`);
      if (button) button.disabled = busy || ![...selected].some(id => canAction(tasksById.get(id), action));
    }
    document.querySelectorAll('#tasks-container input[type="checkbox"]').forEach(box => {
      box.checked = selected.has(box.dataset.taskId);
      box.disabled = busy;
    });
  }
  function setTasks(tasks) {
    tasksById = new Map(tasks.map(task => [task.task_id, task]));
    eligible = new Set(tasks.filter(task => canCancel(task) || canAction(task, 'retry')).map(task => task.task_id));
    for (const id of selected) if (!eligible.has(id)) selected.delete(id);
    update();
  }
  function toggle(id, checked) {
    if (busy || !eligible.has(id)) return;
    if (checked) selected.add(id);
    else selected.delete(id);
    update();
  }
  function toggleAll() {
    if (busy) return;
    if (eligible.size && [...eligible].every(id => selected.has(id))) selected.clear();
    else for (const id of eligible) selected.add(id);
    update();
  }
  async function cancel(ids = [...selected]) {
    if (busy || !ids.length) return;
    busy = true;
    update();
    const failures = [];
    for (const id of ids) {
      if (!eligible.has(id) || !canCancel(tasksById.get(id))) continue;
      try {
        await cancelTask(id);
        selected.delete(id);
        eligible.delete(id);
      } catch (error) {
        failures.push(error.message);
      }
    }
    busy = false;
    update();
    if (failures.length) alert(`有 ${failures.length} 個任務取消失敗：${failures[0]}`);
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
    }
    const input = document.getElementById('task-edit-dest');
    input.disabled = !local;
    input.required = local;
    if (local) input.focus();
  }
  function edit(taskId) {
    if (busy) return;
    editingIds = (taskId ? [taskId] : [...selected]).filter(id => canAction(tasksById.get(id), 'edit'));
    if (!editingIds.length) return;
    document.getElementById('task-edit-count').textContent = editingIds.length;
    document.getElementById('task-edit-storage').value = '';
    document.getElementById('task-edit-dest').value = '';
    for (const key of ['host', 'share', 'directory', 'username', 'password', 'direct-path']) document.getElementById('task-edit-' + key).value = '';
    updateEditStorage();
    document.getElementById('task-edit-subtitles').value = '';
    document.getElementById('task-edit-status').textContent = '';
    document.getElementById('task-edit-dialog').showModal();
  }
  async function saveEdit(event) {
    event.preventDefault();
    if (busy) return;
    const storage = document.getElementById('task-edit-storage').value;
    const subtitles = document.getElementById('task-edit-subtitles').value;
    const status = document.getElementById('task-edit-status');
    if (!storage && !subtitles) { status.textContent = '請至少選擇一項修改'; return; }
    const payload = {action: 'update_settings', task_ids: editingIds};
    if (storage) payload.storage = storage;
    if (['local', 'upload'].includes(storage)) {
      payload.dest = document.getElementById('task-edit-dest').value.trim();
      if (!payload.dest) { status.textContent = '請填寫本機下載 path'; document.getElementById('task-edit-dest').focus(); return; }
    }
    if (['smb', 'upload', 'direct'].includes(storage)) {
      payload.storage = 'custom';
      payload.nas = {enabled: true, mode: storage};
      for (const key of ['host', 'share', 'directory', 'username', 'password']) payload.nas[key] = document.getElementById('task-edit-' + key).value;
      payload.nas.direct_path = document.getElementById('task-edit-direct-path').value;
    }
    if (subtitles) payload.subtitle_mode = subtitles;
    busy = true;
    update();
    const save = document.getElementById('task-edit-save');
    save.disabled = true;
    status.textContent = '驗證目的地並儲存中…';
    try {
      const response = await fetch('/api/download/action', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok || !result.success) throw new Error(result.error || '修改失敗');
      for (const id of result.updated) selected.delete(id);
      document.getElementById('task-edit-password').value = '';
      document.getElementById('queue-action-status').textContent = `已修改 ${result.updated.length} 個任務；略過 ${result.skipped.length} 個（狀態已變更）。已暫停或失敗的任務請按繼續或重試。`;
      document.getElementById('task-edit-dialog').close();
    } catch (error) { status.textContent = error.message; }
    finally { busy = false; save.disabled = false; update(); }
  }
  return {setTasks, canAction, perform, canCancel, has: id => selected.has(id), toggle, toggleAll, cancel, update, edit, saveEdit, updateEditStorage};
};
