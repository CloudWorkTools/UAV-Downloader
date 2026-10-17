const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('uav-web/frontend/index.html', 'utf8');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {value:'', checked:false, classList:{toggle(name, on) {on ? classes.add(name) : classes.delete(name);}, contains(name) {return classes.has(name);}}});
  }
  return elements.get(id);
};
let settings = {subtitle_pref:'all', translation_quality:'quality', download_cover:false,
  proxy_mode:'system', download_concurrency:3, max_workers_per_video:6};
let saved;
const errors = [];
const context = vm.createContext({
  document:{getElementById:element}, window:{addEventListener() {}},
  createVideoSelection: () => ({}), createTaskSelection: () => ({}),
  NasSettings:{load() {}, refreshSpace() {}, payload:() => ({enabled:false})}, alert:msg => errors.push(msg),
  fetch:async (_url, options) => {
    if (options) {saved = JSON.parse(options.body);settings = saved;}
    return {ok:true, json:async () => options ? {success:true} : settings};
  }
});
vm.runInContext(html.slice(html.indexOf('    let currentSite'), html.lastIndexOf('  </script>')), context);
async function run() {
  await context.loadSettings();
  assert.equal(element('setting-subtitles').checked, true);
  assert.equal(element('setting-asr-url').value, 'http://uav-speaches:8000/v1');
  assert.match(element('setting-asr-key-status').textContent, /Speaches/);
  assert.equal(element('setting-subtitle-mode').value, 'all');
  assert.equal(element('setting-download-cover').checked, false);
  assert.equal(element('setting-translation-quality').value, 'quality');
  assert.equal(element('setting-proxy-mode').value, 'system');
  assert.equal(element('subtitles-options').classList.contains('hidden'), false);
  element('setting-subtitle-mode').value = 'ja';
  element('setting-download-cover').checked = true;
  await context.saveSettings();
  assert.equal(saved.subtitle_pref, 'ja');
  assert.equal(saved.download_cover, true);
  assert.equal(saved.translation_quality, 'quality');
  assert.equal(saved.proxy_mode, 'system');
  element('setting-subtitles').checked = false;
  context.updateSubtitleOptions();
  assert.equal(element('subtitles-options').classList.contains('hidden'), true);
  await context.saveSettings();
  assert.equal(saved.subtitle_pref, 'none');
  assert.ok(errors.every(msg => msg.includes('設定已成功儲存')));
  console.log('Web subtitle languages, translation quality, cover and proxy settings: PASS');
}
run().catch(error => {console.error(error);process.exitCode = 1;});
