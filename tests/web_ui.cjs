// No browser dependencies: exercise selection and API failure handling with a small DOM fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('uav-web/frontend/index.html', 'utf8');
const script = html.slice(html.indexOf('    let currentSite'), html.lastIndexOf('  </script>'));
const elements = new Map();
let boxes = [];
let taskBoxes = [];
const element = id => {
  if (!elements.has(id)) elements.set(id, {value: '', checked: false, disabled: false,
    textContent: '', innerHTML: '', classList: {add() {}, remove() {}, toggle() {}}, appendChild() {}});
  return elements.get(id);
};
const requests = [];
const alerts = [];
let failedUrl = '';
const context = vm.createContext({
  document: {getElementById: element, createElement: () => ({}), querySelectorAll: selector => selector.startsWith('#tasks-container') ? taskBoxes : boxes},
  window: {addEventListener() {}},
  alert: message => alerts.push(message),
  fetch: async (path, options) => {
    assert.equal(path, '/api/download/start');
    const body = JSON.parse(options.body);
    requests.push(body.url);
    return {ok: body.url !== failedUrl, json: async () => ({success: body.url !== failedUrl, error: 'test failure'})};
  },
});
vm.runInContext(fs.readFileSync('uav-web/frontend/video-selection.js', 'utf8'), context);
context.createVideoSelection = context.window.createVideoSelection;
vm.runInContext(fs.readFileSync('uav-web/frontend/task-selection.js', 'utf8'), context);
context.createTaskSelection = context.window.createTaskSelection;
vm.runInContext(script, context);
const selection = vm.runInContext('selection', context);
vm.runInContext("switchTab = () => {}", context);
async function run() {
  context.renderVideos([{url: 'a'}, {url: 'b'}]);
  boxes = [{checked: false}, {checked: false}];
  assert.equal(element('download-selected-button').disabled, true);
  selection.togglePage();
  assert.equal(element('select-page-button').textContent, '取消全選');
  assert.equal(element('download-selected-button').textContent, '下載已選 (2)');
  assert.ok(boxes.every(b => b.checked));
  selection.togglePage();
  assert.equal(element('download-selected-button').disabled, true);
  assert.ok(boxes.every(b => !b.checked));
  selection.toggle('earlier-page', true);
  selection.togglePage();
  assert.equal(element('download-selected-button').textContent, '下載已選 (3)');
  failedUrl = 'b';
  await Promise.all([selection.download(), selection.download()]);
  assert.deepEqual(requests, ['earlier-page', 'a', 'b']);
  assert.equal(alerts.length, 1);
  assert.equal(element('download-selected-button').textContent, '下載已選 (1)');
  failedUrl = '';
  await selection.download();
  assert.equal(element('download-selected-button').disabled, true);
  context.renderVideos([]);
  assert.equal(element('select-page-button').disabled, true);
  vm.runInContext(fs.readFileSync('uav-web/frontend/nas-settings.js', 'utf8'), context);
  const nas = context.window.NasSettings;
  nas.load({host: '192.168.1.10', username: 'user', enabled: true, password_saved: true});
  assert.equal(nas.payload().host, '192.168.1.10');
  element('nas-host').value = 'smb://nas.local/download';
  element('nas-username').value = 'nas-user';
  element('nas-password').value = 'test-password';
  assert.equal(nas.payload().host, 'smb://nas.local/download');
  assert.equal(nas.payload().username, 'nas-user');
  assert.equal(nas.payload().password, 'test-password');
  element('nas-password').value = '';
  assert.equal(nas.payload().password, '');
  assert.ok(element('nas-status').textContent.includes('密碼已保存'));
  nas.load({enabled: true, mode: 'direct', direct_path: '/nas/videos'});
  assert.equal(nas.payload().mode, 'direct');
  assert.equal(nas.payload().direct_path, '/nas/videos');
  assert.equal(element('setting-download-dir').disabled, true);
  nas.load({enabled: true, mode: 'smb', host: 'nas.local', username: 'nas-user', password_saved: true});
  assert.equal(nas.payload().mode, 'smb');
  assert.equal(nas.payload().host, 'nas.local');
  assert.equal(element('setting-download-dir').disabled, true);
  nas.load({enabled: false});
  assert.equal(nas.payload().enabled, false);
  assert.equal(element('setting-download-dir').disabled, false);
  const tasks = [
    {task_id:'a', status:'pending'}, {task_id:'b', status:'subtitles'},
    {task_id:'c', status:'completed'}, {task_id:'d', status:'failed'},
    {task_id:'e', status:'cancelled'}, {task_id:'f', status:'skipped'}
  ];
  context.renderTasks(tasks);
  const taskSelection = vm.runInContext('taskSelection', context);
  taskSelection.toggleAll();
  assert.equal(element('cancel-tasks-button').textContent, '取消所選 (3)');
  assert.match(element('tasks-container').innerHTML, /已略過重複影片/);
  assert.ok(!element('tasks-container').innerHTML.includes('data-task-id="c"'));
  context.renderTasks(tasks);
  assert.match(element('tasks-container').innerHTML, /data-task-id="a"[^>]+checked/);
  const cancellations = [];
  context.fetch = async (_path, options) => {
    const id = JSON.parse(options.body).task_id;
    cancellations.push(id);
    return {ok: id !== 'd', json: async () => ({success:id !== 'd', error:'test failure'})};
  };
  await Promise.all([taskSelection.cancel(), taskSelection.cancel()]);
  assert.deepEqual(cancellations, ['a','b','d']);
  assert.equal(taskSelection.has('d'), true, 'Keep failed cancellations selected for retry');
  assert.equal(element('cancel-tasks-button').textContent, '取消所選 (1)');
  context.renderTasks(tasks.map(t => ({...t, status:'completed'})));
  assert.equal(element('cancel-tasks-button').disabled, true);
  assert.equal(element('select-tasks-button').disabled, true);
  console.log('Video/task selection, batch cancellation, failure retention and NAS password UI: PASS');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
