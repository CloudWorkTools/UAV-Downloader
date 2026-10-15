const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {value:'', checked:false, textContent:'', disabled:false,
    focus() {}, showModal() { this.open = true; }, close() { this.open = false; }});
  return elements.get(id);
};
const requests = [];
let paused = false;
let failPause = true;
let polls = 0;
const context = vm.createContext({window:{}, document:{getElementById:element, querySelectorAll:() => []},
  setTimeout: callback => callback(),
  fetch:async (path, options) => {
    const body = options ? JSON.parse(options.body) : null;
    requests.push({path, body});
    if (path === '/api/settings') return {ok:true, json:async () => ({})};
    if (path === '/api/download/tasks') return {ok:true, json:async () => ({tasks:[{task_id:'one', status:paused && ++polls > 1 ? 'paused' : 'pausing'}]})};
    if (body.action === 'pause') {
      if (failPause) return {ok:false, json:async () => ({error:'無法暫停'})};
      paused = true;
      return {ok:true, json:async () => ({success:true})};
    }
    assert.equal(paused, true, 'The worker must stop before changing its settings');
    assert.ok(polls > 1, 'Wait until pausing has actually finished');
    return {ok:true, json:async () => ({success:true, updated:['one'], skipped:[]})};
  }});
vm.runInContext(fs.readFileSync('uav-web/frontend/task-selection.js', 'utf8'), context);
const selection = context.window.createTaskSelection({});
selection.setTasks([{task_id:'one', status:'downloading'}]);
selection.toggle('one', true);
assert.equal(element('edit-tasks-button').disabled, false, 'A single running task must be editable');
(async () => {
  await selection.edit('one');
  assert.equal(element('task-edit-dialog').open, true);
  assert.equal(requests.some(request => request.body?.action === 'pause'), false, 'Opening the editor must not pause a download');
  element('task-edit-subtitles').value = 'none';
  await selection.saveEdit({preventDefault() {}});
  assert.equal(element('task-edit-dialog').open, true);
  assert.equal(element('task-edit-status').textContent, '無法暫停');
  assert.equal(requests.some(request => request.body?.action === 'update_settings'), false);
  failPause = false;
  await selection.saveEdit({preventDefault() {}});
  assert.equal(element('task-edit-dialog').open, false);
  assert.equal(requests.find(request => request.body?.action === 'pause').body.task_id, 'one');
  assert.equal(requests.find(request => request.body?.action === 'update_settings').body.subtitle_mode, 'none');
  console.log('Single running task opens settings and safely pauses before applying: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
