const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {textContent: '', disabled: false,
    showModal() { this.open = true; }, close() { this.open = false; }});
  return elements.get(id);
};
const tasks = [{task_id:'running',status:'downloading'}, {task_id:'waiting',status:'pending'},
  {task_id:'failed',status:'failed'}, {task_id:'done',status:'completed'}];
const requests = [], alerts = [];
let polls = 0, release, delayed = false;
const context = vm.createContext({window: {}, document: {getElementById: element, querySelectorAll: () => []},
  alert: message => alerts.push(message), setTimeout: callback => callback(),
  fetch: async (path, options) => {
    if (options) {
      assert.equal(path, '/api/download/action');
      const body = JSON.parse(options.body);
      requests.push(body);
      assert.equal(body.action, 'cancel_remove');
      return {ok:true,json:async()=>({success:true,accepted:body.task_ids.filter(id => id !== 'done'),skipped:body.task_ids.filter(id => id === 'done'),errors:[]})};
    }
    assert.equal(path, '/api/download/tasks');
    polls++;
    if (delayed) { delayed = false; await new Promise(resolve => { release = resolve; }); }
    const submitted = requests.at(-1).task_ids;
    return {ok:true,json:async()=>({tasks: tasks.filter(t => !submitted.includes(t.task_id) || t.task_id === 'failed').map(t => ({...t, error: t.task_id === 'failed' ? 'NAS 離線' : null}))})};
  }});
vm.runInContext(fs.readFileSync('uav-web/frontend/task-selection.js', 'utf8'), context);
const selection = context.window.createTaskSelection({});
const submit = () => selection.confirmCancel({preventDefault() {}});
(async () => {
  selection.setTasks(tasks);
  assert.equal(selection.canAction(tasks[2], 'remove'), false, 'Failed tasks need cancellation before removal');
  selection.toggleAll();
  assert.ok(tasks.every(task => selection.has(task.task_id)), 'Select all includes completed downloads');
  selection.cancel();
  assert.equal(element('task-remove-dialog').open, true);
  assert.equal(requests.length, 0, 'Opening the choices must not mutate downloads');
  assert.equal(element('task-remove-running-count').textContent, '1');
  assert.equal(element('task-remove-completed-count').textContent, '1');
  assert.equal(element('task-remove-waiting-count').textContent, '2');
  for (const group of ['running', 'completed', 'waiting']) element(`task-remove-${group}`).checked = false;
  await submit();
  assert.equal(requests.length, 0);
  assert.match(element('task-remove-error').textContent, /至少/);
  element('task-remove-completed').checked = true;
  await submit();
  assert.deepEqual(requests, [{action:'cancel_remove',task_ids:['done']}]);
  assert.equal(selection.has('done'), false);
  assert.equal(selection.has('running'), true, 'Unchecked categories stay selected and untouched');
  assert.equal(selection.has('waiting'), true);
  assert.equal(selection.has('failed'), true);
  assert.equal(element('task-remove-dialog').open, false);
  requests.length = 0;
  delayed = true;
  selection.cancel();
  const pending = submit();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(element('cancel-tasks-button').disabled, true);
  assert.match(element('queue-action-status').textContent, /清理/);
  await submit();
  selection.cancel();
  assert.equal(element('task-remove-dialog').open, false, 'Busy actions cannot reopen the dialog');
  assert.equal(requests.length, 1, 'Duplicate submissions do not issue another batch');
  release();
  await pending;
  assert.deepEqual(requests, [{action:'cancel_remove',task_ids:['running','waiting','failed']}]);
  assert.equal(selection.has('running'), false);
  assert.equal(selection.has('waiting'), false);
  assert.equal(selection.has('failed'), true);
  assert.match(element('queue-action-status').textContent, /已移除 2/);
  assert.match(element('queue-action-status').textContent, /1 個失敗/);
  assert.match(element('queue-action-status').textContent, /NAS 離線/);
  assert.deepEqual(alerts, [], 'Batch failures are summarized inline');
  console.log('Task removal category counts, filtering, confirmation and cleanup failures: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
