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
const tabChanges = [];
let failedUrl = '';
const context = vm.createContext({
  document: {getElementById: element, createElement: () => ({}), querySelectorAll: selector => selector.startsWith('#tasks-container') ? taskBoxes : boxes},
  window: {addEventListener() {}},
  alert: message => alerts.push(message),
  tabChanges,
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
vm.runInContext("switchTab = tab => tabChanges.push(tab)", context);
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
  vm.runInContext("currentPage = 5; currentSearchQuery = 'saved query'", context);
  const browseContent = element('video-grid').innerHTML;
  await context.startSingleDownload('single');
  element('modal-urls-input').value = 'modal-a\nmodal-b';
  await context.submitModalUrls();
  assert.deepEqual(tabChanges, [], 'Adding single, selected or pasted URLs keeps the current tab');
  assert.equal(vm.runInContext('currentPage', context), 5);
  assert.equal(vm.runInContext('currentSearchQuery', context), 'saved query');
  assert.equal(element('video-grid').innerHTML, browseContent);
  assert.deepEqual(requests.slice(-3), ['single', 'modal-a', 'modal-b']);
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
  const mixedTasks = ['completed', 'pending', 'uploading', 'downloading', 'failed', 'downloading', 'preparing', 'subtitles', 'cancelling']
    .map((status, i) => ({task_id: String(i), name: `task-${i}`, status}));
  context.renderTasks(mixedTasks);
  const renderedOrder = () => [...element('tasks-container').innerHTML.matchAll(/<h4[^>]*>(task-\d+)<\/h4>/g)].map(match => match[1]);
  assert.deepEqual(renderedOrder(), ['task-3', 'task-5', 'task-2', 'task-6', 'task-7', 'task-8', 'task-1', 'task-0', 'task-4']);
  assert.equal(mixedTasks[0].status, 'completed', 'Rendering does not mutate the source order');
  mixedTasks[3].status = 'completed';
  mixedTasks[1].status = 'downloading';
  context.renderTasks(mixedTasks);
  assert.deepEqual(renderedOrder().slice(0, 2), ['task-1', 'task-5'], 'Live status updates reorder active downloads');
  const tasks = [
    {task_id:'a', status:'pending'}, {task_id:'b', status:'subtitles'},
    {task_id:'c', status:'completed'}, {task_id:'d', status:'failed'},
    {task_id:'e', status:'cancelled', name:'successfully-cleaned'}, {task_id:'f', status:'skipped'}
  ];
  context.renderTasks(tasks);
  assert.ok(element('tasks-container').innerHTML.includes('successfully-cleaned'));
  assert.match(element('tasks-container').innerHTML, /重新開始/);
  context.renderTasks([{task_id:'failed-cleanup', status:'failed', error:'清除失敗：Permission denied <folder>'}]);
  assert.match(element('tasks-container').innerHTML, /<p role="alert"[^>]*>清除失敗：Permission denied &lt;folder&gt;<\/p>/);
  context.renderTasks([{task_id:'e', status:'cancelled'}]);
  assert.match(element('tasks-container').innerHTML, /已取消/);
  context.renderTasks([]);
  assert.match(element('tasks-container').innerHTML, /目前沒有下載任務/);
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
  assert.deepEqual(cancellations, ['b','a','d']);
  assert.equal(taskSelection.has('d'), true, 'Keep failed cancellations selected for retry');
  assert.equal(element('cancel-tasks-button').textContent, '取消所選 (1)');
  context.renderTasks(tasks.map(t => ({...t, status:'completed'})));
  assert.equal(element('cancel-tasks-button').disabled, true);
  assert.equal(element('select-tasks-button').disabled, true);
  const operations = [];
  context.fetch = async (path, options) => {
    assert.equal(path, '/api/download/action');
    const body = JSON.parse(options.body);
    operations.push(body);
    return {ok: body.task_id !== 'failed-action', json: async () => ({success: body.task_id !== 'failed-action', error:'operation failed', message:'目的地寫入及讀回成功'})};
  };
  context.renderTasks([{task_id:'active',status:'downloading'}, {task_id:'paused',status:'paused'}, {task_id:'failed-action',status:'failed'}, {task_id:'stopped',status:'cancelled'}]);
  taskSelection.toggleAll();
  await taskSelection.perform('pause');
  await taskSelection.perform('resume');
  await taskSelection.perform('retry');
  assert.deepEqual(operations.map(operation => [operation.action, operation.task_id]), [['pause','active'],['resume','paused'],['retry','failed-action'],['retry','stopped']]);
  assert.equal(taskSelection.has('failed-action'), true, 'Failed controls remain selected for retry');
  assert.equal(taskSelection.has('stopped'), false);
  const probeButton = {disabled:false};
  await context.testDownloadDestination(probeButton);
  assert.equal(probeButton.disabled, false);
  assert.equal(element('destination-test-status').textContent, '目的地寫入及讀回成功');
  let release;
  let submissions = 0;
  context.fetch = () => { submissions++; return new Promise(resolve => {release=resolve;}); };
  const firstAction = context.taskAction('retry', 'same-task');
  assert.equal(await context.taskAction('retry', 'same-task'), false);
  release({ok:true,json:async()=>({success:true})});
  assert.equal(await firstAction, true);
  assert.equal(submissions, 1);
  const clearRequests = [];
  context.renderTasks([{task_id:'done', status:'completed'}, {task_id:'keep', status:'downloading'}]);
  assert.equal(element('clear-completed-button').disabled, false);
  context.fetch = async (path, options) => {
    clearRequests.push(path);
    if (options) {
      assert.deepEqual(JSON.parse(options.body), {action:'clear_completed'});
      return {ok:true, json:async()=>({success:true, removed:1})};
    }
    return {ok:true, json:async()=>({tasks:[{task_id:'keep',status:'downloading'}]})};
  };
  await Promise.all([context.clearCompletedTasks(), context.clearCompletedTasks()]);
  assert.deepEqual(clearRequests, ['/api/download/action','/api/download/tasks']);
  assert.equal(element('clear-completed-button').disabled, true);
  assert.match(element('tasks-container').innerHTML, /data-task-id="keep"/);
  context.renderTasks([{task_id:'done',status:'completed'}]);
  context.fetch = async () => ({ok:false,json:async()=>({success:false,error:'clear failed'})});
  await context.clearCompletedTasks();
  assert.equal(element('clear-completed-button').disabled, false);
  assert.equal(alerts.at(-1), 'clear failed');
  context.renderTasks([{task_id:'no-selection-paused',status:'paused'}, {task_id:'no-selection-failed',status:'failed'}, {task_id:'keep-cancelled',status:'cancelled'}]);
  assert.equal(element('resume-unfinished-button').disabled, false);
  const resumeRequests = [];
  let finishResume;
  context.fetch = (path, options) => {
    resumeRequests.push(path);
    if (options) {
      assert.deepEqual(JSON.parse(options.body), {action:'resume_unfinished'});
      return new Promise(resolve => {finishResume = resolve;});
    }
    return Promise.resolve({ok:true,json:async()=>({tasks:[{task_id:'keep-failed',status:'failed'}]})});
  };
  const resume = context.resumeUnfinishedTasks();
  await context.resumeUnfinishedTasks();
  assert.equal(element('resume-unfinished-button').disabled, true);
  finishResume({ok:true,json:async()=>({success:true,resumed:1,errors:[{task_id:'keep-failed',error:'NAS 帳號已變更'}]})});
  await resume;
  assert.deepEqual(resumeRequests, ['/api/download/action','/api/download/tasks']);
  assert.match(element('queue-action-status').textContent, /已續傳 1.*1 筆無法續傳.*NAS 帳號已變更/);
  assert.equal(element('resume-unfinished-button').disabled, false);
  context.fetch = async () => ({ok:false,json:async()=>({error:'服務暫時無法使用'})});
  await context.resumeUnfinishedTasks();
  assert.equal(element('queue-action-status').textContent, '服務暫時無法使用');
  context.renderTasks([{task_id:'already-running',status:'downloading'}, {task_id:'cancelled',status:'cancelled'}]);
  assert.equal(element('resume-unfinished-button').disabled, true);
  context.renderTasks([{task_id:'waiting',status:'pending',progress:82.5}, {task_id:'active',status:'downloading'}]);
  assert.equal(element('resume-unfinished-button').disabled, false, 'Waiting partial downloads must allow resume');
  const waitingRequests = [];
  context.fetch = async (path, options) => {
    waitingRequests.push(path);
    if (options) return {ok:true,json:async()=>({success:true,resumed:0,errors:[],waiting:12,running:6})};
    return {ok:true,json:async()=>({tasks:[{task_id:'waiting',status:'pending'}]})};
  };
  await context.resumeUnfinishedTasks();
  assert.deepEqual(waitingRequests, ['/api/download/action','/api/download/tasks']);
  assert.match(element('queue-action-status').textContent, /6 筆執行中.*12 筆等待/);
  assert.equal(element('resume-unfinished-button').disabled, false);
  console.log('Video/task selection, batch cancellation, failure retention and NAS password UI: PASS');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
