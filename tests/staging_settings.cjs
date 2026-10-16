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
let settings = {subtitle_pref:'all', recognition_quality:'quality', download_cover:false,
  subtitle_runtime_supported:false, proxy_mode:'system', download_concurrency:3, max_workers_per_video:6};
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
  settings.staging_dir = '/offline/ssd';
  await context.loadSettings();
  assert.equal(element('setting-staging-enabled').checked, true);
  assert.equal(element('setting-staging-dir').disabled, false);
  element('setting-staging-enabled').checked = false;
  await context.saveSettings();
  assert.equal(saved.staging_dir, '');
  await context.loadSettings();
  assert.equal(element('setting-staging-enabled').checked, false);
  assert.equal(element('setting-staging-dir').disabled, true);
  element('setting-staging-enabled').checked = true;
  element('setting-staging-dir').value = '/downloads/staging';
  await context.saveSettings();
  assert.equal(saved.staging_dir, '/downloads/staging');
  console.log('Global staging switch: PASS');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
