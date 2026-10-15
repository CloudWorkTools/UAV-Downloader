window.createTaskSelection = ({cancelTask, taskAction}) => {
  const selected = new Set();
  let eligible = new Set();
  let busy = false;
  const canCancel = task => ['pending', 'preparing', 'downloading', 'subtitles', 'uploading', 'publishing', 'paused', 'failed'].includes(task?.status);
  let tasksById = new Map();
  const canAction = (task, action) => ({
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
    for (const action of ['pause', 'resume', 'retry']) {
      document.getElementById(`${action}-tasks-button`).disabled = busy || ![...selected].some(id => canAction(tasksById.get(id), action));
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
  return {setTasks, canAction, perform, canCancel, has: id => selected.has(id), toggle, toggleAll, cancel, update};
};
