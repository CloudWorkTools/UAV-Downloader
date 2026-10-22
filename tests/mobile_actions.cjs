const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {Cdp} = require('./subtitle_cdp.cjs');
(async () => {
  const root = 'uav-web/frontend/';
  const server = http.createServer((req, res) => {
    if (req.url === '/') {
      const html = fs.readFileSync(root + 'index.html', 'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
      res.end(html.replace('</body>', '<script src="/mobile-actions.js"></script></body>'));
    } else {
      const file = root + req.url.slice(1);
      if (!fs.existsSync(file)) {res.writeHead(404);res.end();return;}
      res.end(fs.readFileSync(file));
    }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const chrome = spawn(process.env.CHROME_BIN || 'google-chrome', ['--headless','--no-sandbox','--disable-gpu','--remote-debugging-pipe'], {stdio:['ignore','ignore','ignore','pipe','pipe']});
  const c = new Cdp(chrome);
  try {
    await c.start(`http://127.0.0.1:${server.address().port}/`);
    await c.command('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
    assert.equal(await c.evaluate('!!document.getElementById("mobile-action-fab")'), true);
    assert.equal(await c.evaluate('document.querySelector("#mobile-action-fab img").getAttribute("src")'), '/mascot-fab.svg');
    assert.equal(await c.evaluate('getComputedStyle(document.querySelector("#select-page-button")).display'), 'none');
    await c.evaluate(`document.querySelector('#tab-queue').classList.add('hidden');document.querySelector('#tab-browse').classList.remove('hidden');window.hits=0;const b=document.querySelector('#select-page-button');b.disabled=false;b.onclick=()=>hits++`);
    await c.evaluate('document.querySelector("#mobile-action-fab").click()');
    await c.evaluate('document.querySelector("[data-mobile-group=selection]").click()');
    await c.evaluate('document.querySelector("[data-mobile-source=select-page-button]").click()');
    assert.equal(await c.evaluate('hits'), 1);
    assert.equal(await c.evaluate('document.activeElement.id'), 'mobile-action-fab');
    await c.evaluate('document.querySelector("#mobile-action-fab").click();document.querySelector("[data-mobile-group=selection]").click()');
    assert.equal(await c.evaluate('document.querySelector("[data-mobile-source=download-selected-button]").disabled'), true);
    await c.evaluate('document.querySelector("#download-selected-button").disabled=false;document.querySelector("#download-selected-button").textContent="下載已選 (2)"');
    await c.until('document.querySelector("[data-mobile-source=download-selected-button]").textContent.includes("(2)")');
    await c.command('Input.dispatchKeyEvent', {type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await c.until('document.querySelector("#mobile-action-fab").getAttribute("aria-expanded") === "false"');
    await c.evaluate('document.querySelector("#tab-browse").classList.add("hidden");document.querySelector("#tab-queue").classList.remove("hidden")');
    await c.until('!document.querySelector("#mobile-action-fab").hidden');
    await c.evaluate('document.querySelector("#mobile-action-fab").click();document.querySelector("[data-mobile-group=organize]").click()');
    assert.equal(await c.evaluate('!!document.querySelector("[data-mobile-source=clear-completed-button]")'), true);
    await c.evaluate('document.querySelector("#clear-completed-button").disabled=false;document.querySelector("#clear-completed-button").onclick=()=>hits++');
    await c.until('!document.querySelector("[data-mobile-source=clear-completed-button]").disabled');
    await c.evaluate('document.querySelector("[data-mobile-source=clear-completed-button]").click()');
    assert.equal(await c.evaluate('hits'), 2);
    await c.evaluate('document.querySelector("#mobile-action-fab").click()');
    await c.evaluate('document.querySelector("#mobile-action-close").click();document.querySelector("#tab-queue").classList.add("hidden")');
    await c.until('document.querySelector("#mobile-action-fab").hidden');
    assert.equal(await c.evaluate('getComputedStyle(document.querySelector("#mobile-action-fab")).display'), 'none');
    await c.evaluate('document.querySelector("#tab-queue").classList.remove("hidden")');
    await c.until('!document.querySelector("#mobile-action-fab").hidden');
    await c.evaluate('document.querySelector("#mobile-action-fab").click()');
    await c.command('Emulation.setDeviceMetricsOverride', {width:1280,height:900,deviceScaleFactor:1,mobile:false});
    await c.until('document.querySelector("#mobile-action-dialog").hidden');
    assert.equal(await c.evaluate('getComputedStyle(document.querySelector("#mobile-action-fab")).display'), 'none');
    assert.notEqual(await c.evaluate('getComputedStyle(document.querySelector("#select-page-button")).display'), 'none');
    console.log('Mobile portrait FAB, hierarchy, delegated actions, live state, focus, Escape and desktop isolation: PASS');
  } finally {c.close();chrome.kill();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
