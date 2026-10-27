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
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      const tailwind = process.env.MOBILE_SCREENSHOT ? '<script src="https://cdn.tailwindcss.com"></script>' : '';
      res.end(html.replace('</head>', tailwind + '</head>').replace('</body>', '<script src="/video-selection.js"></script><script src="/mobile-actions.js"></script></body>'));
    } else {
      const file = root + req.url.slice(1);
      if (!fs.existsSync(file)) {res.writeHead(404);res.end();return;}
      if (file.endsWith('.svg')) res.setHeader('Content-Type', 'image/svg+xml');
      if (file.endsWith('.css')) res.setHeader('Content-Type', 'text/css');
      if (file.endsWith('.js')) res.setHeader('Content-Type', 'text/javascript');
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
    assert.equal(await c.evaluate('document.querySelector("#mobile-selection-count").hidden'), true);
    assert.equal(await c.evaluate('getComputedStyle(document.querySelector("#select-page-button")).display'), 'none');
    await c.evaluate(`document.querySelector('#tab-queue').classList.add('hidden');document.querySelector('#tab-browse').classList.remove('hidden');window.hits=0;const b=document.querySelector('#select-page-button');b.disabled=false;b.onclick=()=>hits++`);
    await c.evaluate('document.querySelector("#mobile-action-fab").click()');
    assert.equal(await c.evaluate('document.querySelector("#mobile-action-dialog").contains(document.querySelector("#mobile-action-fab"))'), true);
    await c.evaluate('document.querySelector("#mobile-action-fab").click()');
    assert.equal(await c.evaluate('document.querySelector("#mobile-action-dialog").hidden'), true);
    await c.evaluate('document.querySelector("#mobile-action-fab").click()');
    await c.evaluate('document.querySelector("[data-mobile-group=selection]").click()');
    await c.evaluate('document.querySelector("[data-mobile-source=select-page-button]").click()');
    assert.equal(await c.evaluate('hits'), 1);
    assert.equal(await c.evaluate('document.activeElement.id'), 'mobile-action-fab');
    await c.evaluate('document.querySelector("#mobile-action-fab").click();document.querySelector("[data-mobile-group=selection]").click()');
    assert.equal(await c.evaluate('document.querySelector("[data-mobile-source=download-selected-button]").hidden'), true);
    await c.evaluate(`window.actualSelection=createVideoSelection({startBatch:async () => true});actualSelection.setVisible(['a','b']);document.querySelector('#select-page-button').onclick=()=>actualSelection.togglePage();document.querySelector('#browse-clear-mobile').onclick=()=>actualSelection.clear()`);
    await c.evaluate('document.querySelector("[data-mobile-source=select-page-button]").click()');
    await c.until('document.querySelector("#mobile-selection-count").textContent === "2"');
    assert.equal(await c.evaluate('document.querySelector("#mobile-selection-count").hidden'), false);
    await c.evaluate('document.querySelector("#mobile-action-fab").click();document.querySelector("[data-mobile-group=selection]").click()');
    await c.until('document.querySelector("[data-mobile-source=download-selected-button]").textContent.includes("(2)")');
    assert.equal(await c.evaluate('document.querySelector("[data-mobile-source=select-page-button]").textContent'), '清除本頁');
    assert.equal(await c.evaluate('document.querySelector("[data-mobile-source=browse-clear-mobile]").textContent'), '清除全部');
    assert.match(await c.evaluate('document.querySelector("#mobile-action-fab").getAttribute("aria-label")'), /關閉.*2/);
    assert.equal(await c.evaluate('document.querySelector("[data-mobile-source=download-selected-button]").hidden'), false);
    assert.equal(await c.evaluate('getComputedStyle(document.querySelector("[data-mobile-source=download-selected-button]")).borderRadius'), '50%');
    if (process.env.MOBILE_SCREENSHOT) {
      await c.evaluate('document.querySelector("#mobile-action-items").getAnimations({subtree:true}).forEach(animation => animation.finish())');
      const image = await c.command('Page.captureScreenshot', {format:'png'});
      fs.writeFileSync(process.env.MOBILE_SCREENSHOT, Buffer.from(image.data, 'base64'));
    }
    await c.command('Input.dispatchKeyEvent', {type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await c.until('document.querySelector("#mobile-action-fab").getAttribute("aria-expanded") === "false"');
    await c.evaluate('document.querySelector("#tab-browse").classList.add("hidden");document.querySelector("#tab-queue").classList.remove("hidden")');
    await c.until('!document.querySelector("#mobile-action-fab").hidden');
    await c.evaluate(`['queue-selection-mode','select-tasks-button','pause-tasks-button','resume-tasks-button','retry-tasks-button','edit-tasks-button','cancel-tasks-button'].forEach(id => {const b=document.getElementById(id);b.disabled=false;b.hidden=false;})`);
    await c.evaluate('document.querySelector("#mobile-action-fab").click();document.querySelector("[data-mobile-group=batch]").click()');
    for (const width of [320, 390]) {
      await c.command('Emulation.setDeviceMetricsOverride', {width,height:568,deviceScaleFactor:1,mobile:true});
      await c.evaluate('document.querySelector("#mobile-action-items").getAnimations({subtree:true}).forEach(animation => animation.finish())');
      const geometry = await c.evaluate(`(() => {
        const buttons=[...document.querySelectorAll('#mobile-action-items button:not([hidden]), #mobile-action-fab, #mobile-action-dialog header')];
        const rects=buttons.map(b=>b.getBoundingClientRect());
        return {count:rects.length, inside:rects.every(r=>r.left>=0 && r.top>=0 && r.right<=innerWidth && r.bottom<=innerHeight),
          separate:rects.every((a,i)=>rects.slice(i+1).every(b=>a.right<=b.left || b.right<=a.left || a.bottom<=b.top || b.bottom<=a.top)),
          hittable:buttons.every(b=>{const r=b.getBoundingClientRect();return b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})};
      })()`);
      assert.deepEqual(geometry, {count:9,inside:true,separate:true,hittable:true});
    }
    await c.evaluate('document.querySelector("#mobile-action-back").click();document.querySelector("[data-mobile-group=organize]").click()');
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
