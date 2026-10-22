const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {Cdp} = require('../../tests/subtitle_cdp.cjs');

// Exercise the shipped application; only the in-memory browse results are seeded.
async function checkMobileActions(url) {
  assert.ok(url, 'a running ephemeral application URL is required');
  const chrome = spawn(process.env.CHROME_BIN || 'google-chrome',
    ['--headless', '--no-sandbox', '--disable-gpu', '--remote-debugging-pipe'],
    {stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe']});
  const c = new Cdp(chrome);
  const click = selector => c.evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
  const openSelection = async () => {
    await click('#mobile-action-fab');
    await click('[data-mobile-group=selection]');
  };
  try {
    await c.start(url);
    await c.command('Emulation.setDeviceMetricsOverride', {width:390, height:844, deviceScaleFactor:1, mobile:true});
    await c.until('document.querySelector("#mobile-action-fab") && !document.querySelector("#mobile-action-fab").hidden');
    await c.until('document.querySelector("#mobile-action-fab img").complete && document.querySelector("#mobile-action-fab img").naturalWidth > 0');
    assert.equal(await c.evaluate('document.querySelector("#mobile-action-fab img").getAttribute("src")'), '/mascot-fab.svg');
    assert.equal(await c.evaluate('getComputedStyle(document.querySelector("#mobile-action-fab")).position'), 'fixed');
    const position = await c.evaluate('(() => {const r=document.querySelector("#mobile-action-fab").getBoundingClientRect();return r.bottom <= 844 && r.right <= 390 && r.top >= 0 && r.left >= 0})()');
    assert.equal(position, true);
    assert.equal(await c.evaluate('getComputedStyle(document.querySelector("#select-page-button")).display'), 'none');
    await c.evaluate('renderVideos([{url:"https://example.invalid/mobile-check",title:"Mobile acceptance",thumbnail:"/mascot-fab.svg"}])');
    await openSelection();
    assert.equal(await c.evaluate('document.querySelector("[data-mobile-source=download-selected-button]").disabled'), true);
    await click('[data-mobile-source=select-page-button]');
    assert.equal(await c.evaluate('document.querySelector("#video-grid input[type=checkbox]").checked'), true);
    assert.equal(await c.evaluate('document.activeElement.id'), 'mobile-action-fab');
    await openSelection();
    await c.until('!document.querySelector("[data-mobile-source=download-selected-button]").disabled && document.querySelector("[data-mobile-source=download-selected-button]").textContent.includes("(1)")');
    await click('[data-mobile-source=download-selected-button]');
    await c.until('document.querySelector("#download-batch-dialog").open');
    await click('#batch-cancel');
    await c.until('!document.querySelector("#download-batch-dialog").open');
    await openSelection();
    await click('[data-mobile-source=select-page-button]');
    await openSelection();
    await c.until('document.querySelector("[data-mobile-source=download-selected-button]").disabled');
    await click('#mobile-action-close');
    assert.equal(await c.evaluate('document.activeElement.id'), 'mobile-action-fab');
    await click('#mobile-action-fab');
    await c.command('Input.dispatchKeyEvent', {type:'keyDown', key:'Escape', code:'Escape', windowsVirtualKeyCode:27});
    await c.until('document.querySelector("#mobile-action-dialog").hidden');
    await click('#mobile-action-fab');
    await c.command('Input.dispatchMouseEvent', {type:'mousePressed', x:1, y:1, button:'left', clickCount:1});
    await c.command('Input.dispatchMouseEvent', {type:'mouseReleased', x:1, y:1, button:'left', clickCount:1});
    await c.until('document.querySelector("#mobile-action-dialog").hidden');
    await c.evaluate('switchTab("queue")');
    await click('#mobile-action-fab');
    await click('[data-mobile-source=queue-add-mobile]');
    await c.until('!document.querySelector("#quick-modal").classList.contains("hidden")');
    await c.evaluate('closeQuickDownloadModal()');
    await click('#mobile-action-fab');
    await c.command('Emulation.setDeviceMetricsOverride', {width:1280, height:900, deviceScaleFactor:1, mobile:false});
    await c.until('document.querySelector("#mobile-action-dialog").hidden');
    await c.evaluate('switchTab("browse")');
    assert.equal(await c.evaluate('getComputedStyle(document.querySelector("#mobile-action-fab")).display'), 'none');
    assert.notEqual(await c.evaluate('getComputedStyle(document.querySelector("#select-page-button")).display'), 'none');
    assert.notEqual(await c.evaluate('getComputedStyle(document.querySelector("#download-selected-button")).display'), 'none');
    const writes = c.eventLog.filter(event => event.method === 'Network.requestWillBeSent')
      .map(event => event.params.request).filter(request => !['GET', 'HEAD', 'OPTIONS'].includes(request.method));
    assert.ok(writes.every(request => {
      const path = new URL(request.url).pathname;
      const action = JSON.parse(request.postData).action;
      return (path === '/api/download/batch' && action === 'preview')
        || (path === '/api/download/action' && action === 'nas_space');
    }), 'acceptance must only preview/read space; never submit or mutate tasks/settings');
    console.log('Full application mobile actions, selection, dialogs and desktop controls: PASS');
  } finally { c.close(); chrome.kill(); }
}
module.exports = {checkMobileActions};
if (require.main === module) checkMobileActions(process.argv[2]).catch(error => {console.error(error); process.exitCode = 1;});
