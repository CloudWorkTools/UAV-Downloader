const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('uav-web/frontend/index.html', 'utf8');
const elements = new Map();
const cards = [];
const element = id => {
  if (!elements.has(id)) elements.set(id, {value: '', innerHTML: '', textContent: '', options: [], get selectedOptions() {return this.options.filter(option => option.value === this.value);}, classList: {add() {}, remove() {}, toggle() {}}, appendChild(child) { if (id === 'video-grid') cards.push(child); if (id === 'category-select') this.options.push(child); }});
  return elements.get(id);
};
const pending = [];
const context = vm.createContext({
  document: {getElementById: element, createElement: () => ({}), querySelectorAll: () => []},
  window: {addEventListener() {}}, alert() {},
  fetch: (path, options) => new Promise(resolve => pending.push({path, options, resolve})),
});
vm.runInContext(fs.readFileSync('uav-web/frontend/video-selection.js', 'utf8'), context);
context.createVideoSelection = context.window.createVideoSelection;
let chooseCategory;
context.window.createCategoryBrowser = (container, onSelect) => {chooseCategory=onSelect;return {render() {}, select() {}};};
vm.runInContext(html.slice(html.indexOf('    let currentSite'), html.lastIndexOf('  </script>')), context);
const respond = (request, body, ok=true) => request.resolve({ok, json: async () => body});
async function run() {
  context.renderVideos([{url: 'https://example.com/video', thumbnail: 'https://example.com/image.jpg'}]);
  assert.match(cards.at(-1).innerHTML, /referrerpolicy="no-referrer"/, 'Preview must not send the application Referer to image CDNs');
  const first = context.selectSite('JableTV');
  const second = context.selectSite('MissAV');
  respond(pending[1], {categories: [{name:'Latest',url:'https://missav.ai/latest'}]});
  await new Promise(setImmediate);
  respond(pending[0], {categories: [{name:'Latest',url:'https://jable.tv/latest'}]});
  await new Promise(setImmediate);
  assert.equal(pending.length, 3, 'Stale category responses must not trigger another browse');
  assert.equal(JSON.parse(pending[2].options.body).url, 'https://missav.ai/latest');
  respond(pending[2], {videos:[]});
  await Promise.all([first,second]);
  const older = context.fetchBrowse();
  const newer = context.fetchBrowse();
  respond(pending[4], {videos:[{url:'new'}]});
  await newer;
  respond(pending[3], {videos:[{url:'old'}]});
  await older;
  assert.match(cards.at(-1).innerHTML, /'new'/, 'Stale lists must not replace the current page');
  const failure = context.fetchBrowse();
  respond(pending[5], {videos:[],blocked:true,error:'站台防護阻擋'});
  await failure;
  assert.match(cards.at(-1).textContent, /站台防護阻擋/, 'Backend failures must remain visible');
  vm.runInContext('currentPage = 5', context);
  chooseCategory({name:'衣著標籤',url:'https://jable.tv/tags/uniform/'});
  const chosen = JSON.parse(pending[6].options.body);
  assert.equal(chosen.url, 'https://jable.tv/tags/uniform/');
  assert.equal(chosen.page, 1);
  assert.equal(element('selected-category-label').textContent, '衣著標籤');
  respond(pending[6], {videos:[]});
  await new Promise(setImmediate);
  console.log('Preview referrer, site-switch race, stale lists and browse errors: PASS');
}
run().catch(e => {console.error(e);process.exitCode=1;});
