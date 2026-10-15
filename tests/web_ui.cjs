// No browser dependencies: exercise selection and API failure handling with a small DOM fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('uav-web/frontend/index.html', 'utf8');
const script = html.slice(html.indexOf('    let currentSite'), html.lastIndexOf('  </script>'));
const elements = new Map();
let boxes = [];
const element = id => {
  if (!elements.has(id)) elements.set(id, {value: '', checked: false, disabled: false,
    textContent: '', innerHTML: '', classList: {add() {}, remove() {}, toggle() {}}, appendChild() {}});
  return elements.get(id);
};
const requests = [];
const alerts = [];
let failedUrl = '';
const context = vm.createContext({
  document: {getElementById: element, createElement: () => ({}), querySelectorAll: () => boxes},
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
  console.log('Selection, duplicate submission, failed-item retention and NAS password UI: PASS');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
