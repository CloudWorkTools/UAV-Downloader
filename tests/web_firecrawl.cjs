const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('uav-web/frontend/index.html', 'utf8');
const nodes = new Map();
const element = id => {
  if (!nodes.has(id)) nodes.set(id, {value:'', checked:false, disabled:false, textContent:'', classList:{toggle() {}}});
  return nodes.get(id);
};
const responses = [];
const calls = [];
const context = vm.createContext({
  document:{getElementById:element}, window:{addEventListener() {}}, alert() {},
  createVideoSelection:() => ({}), createTaskSelection:() => ({}),
  NasSettings:{load() {}, payload:() => ({enabled:false})},
  fetch:async (url, options) => {
    calls.push(JSON.parse(options.body));
    return {ok:true, json:async () => responses.shift()};
  }
});
vm.runInContext(html.slice(html.indexOf('    let currentSite'), html.lastIndexOf('  </script>')), context);
async function run() {
  context.updateFirecrawlStatus();
  assert.equal(element('firecrawl-test-button').disabled, true);
  element('firecrawl-enabled').checked = true;
  context.updateFirecrawlStatus();
  assert.match(element('firecrawl-status').textContent, /尚未測試/);
  responses.push({success:true}, {state:'ready', site:'JableTV'});
  await context.testFirecrawl();
  assert.equal(calls[0].firecrawl.enabled, true);
  assert.equal(calls[1].test_firecrawl, true);
  assert.equal(calls[1].site, 'JableTV');
  assert.match(element('firecrawl-status').textContent, /JableTV 實際擷取成功/);
  responses.push({success:true}, {state:'unavailable', error:'站台防護阻擋'});
  await context.testFirecrawl();
  assert.match(element('firecrawl-status').textContent, /目前無法使用.*站台防護阻擋/);
  assert.equal(element('firecrawl-test-button').disabled, false);
  element('firecrawl-enabled').checked = false;
  context.updateFirecrawlStatus();
  assert.match(element('firecrawl-status').textContent, /已關閉/);
  console.log('Firecrawl enable, actual per-site test, failure and disable: PASS');
}
run().catch(e => {console.error(e);process.exitCode=1;});
