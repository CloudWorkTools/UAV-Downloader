const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {Cdp} = require('./subtitle_cdp.cjs');
(async () => {
  const root = 'uav-web/frontend/';
  let nestedMenu = false;
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
      if (nestedMenu && file.endsWith('mobile-actions.js')) {
        // A third level exercises breadcrumb jumps without adding an unused production group.
        res.end(fs.readFileSync(file, 'utf8').replace("selection: ['影片選取', [", "selection_more: ['進階選取', ['select-page-button']], selection: ['影片選取', [['進階選取', 'selection_more'], "));
      } else res.end(fs.readFileSync(file));
    }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const chrome = spawn(process.env.CHROME_BIN || 'google-chrome', ['--headless','--no-sandbox','--disable-gpu','--remote-debugging-pipe'], {stdio:['ignore','ignore','ignore','pipe','pipe']});
  const c = new Cdp(chrome);
  try {
    async function checkAnimationFrames(count) {
      const frames = await c.evaluate(`(() => {
        const dialog = document.querySelector('#mobile-action-dialog');
        const animations = dialog.getAnimations({subtree:true});
        animations.forEach(animation => animation.pause());
        const end = Math.max(...animations.map(animation => animation.effect.getComputedTiming().endTime));
        return [0, .5, 1].map(progress => {
          // One shared timeline preserves staggered delays, including the pre-delay fill.
          animations.forEach(animation => {animation.currentTime = progress * end;});
          const buttons = [...dialog.querySelectorAll('#mobile-action-items button:not([hidden]), #mobile-action-fab, [data-mobile-ancestor]')];
          const rects = buttons.map(button => button.getBoundingClientRect());
          const navigation = [...dialog.querySelectorAll('#mobile-action-fab, [data-mobile-ancestor]')];
          return {progress, animations:animations.length, count:buttons.length,
            inside:rects.every(r => r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight),
            navigationUsable:navigation.every(button => {const r = button.getBoundingClientRect();return r.width >= 44 && r.height >= 44 && button.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));}),
            separate:progress !== 1 || rects.every((a, i) => rects.slice(i + 1).every(b => Math.hypot(a.x + a.width / 2 - b.x - b.width / 2, a.y + a.height / 2 - b.y - b.height / 2) >= (a.width + b.width) / 2)),
            hittable:progress !== 1 || buttons.every(button => {const r = button.getBoundingClientRect();return button.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));})};
        });
      })()`);
      for (const {animations, ...frame} of frames) {
        assert.ok(animations > 0, 'normal motion must expose animations to sample');
        assert.deepEqual(frame, {progress:frame.progress, count, inside:true, navigationUsable:true, separate:true, hittable:true});
      }
    }
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
    assert.equal(await c.evaluate('!!document.querySelector("#mobile-action-dialog header")'), false);
    assert.equal(await c.evaluate('document.querySelector("[data-mobile-ancestor]").textContent'), '影片選取');
    assert.match(await c.evaluate('document.querySelector("[data-mobile-ancestor]").getAttribute("aria-label")'), /返回瀏覽功能/);
    assert.equal(await c.evaluate('document.querySelectorAll("#mobile-action-links line").length'), 1);
    await c.evaluate('document.querySelector("[data-mobile-ancestor]").click()');
    assert.equal(await c.evaluate('document.activeElement.dataset.mobileGroup'), 'selection');
    assert.equal(await c.evaluate('document.querySelectorAll("[data-mobile-ancestor]").length'), 0);
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
      await c.evaluate('document.querySelector("#mobile-action-dialog").getAnimations({subtree:true}).forEach(animation => animation.finish())');
      const image = await c.command('Page.captureScreenshot', {format:'png'});
      fs.writeFileSync(process.env.MOBILE_SCREENSHOT, Buffer.from(image.data, 'base64'));
    }
    await c.command('Input.dispatchKeyEvent', {type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await c.until('document.querySelector("#mobile-action-fab").getAttribute("aria-expanded") === "false"');
    await c.evaluate('document.querySelector("#tab-browse").classList.add("hidden");document.querySelector("#tab-queue").classList.remove("hidden")');
    await c.until('!document.querySelector("#mobile-action-fab").hidden');
    await c.evaluate(`['queue-selection-mode','select-tasks-button','pause-tasks-button','resume-tasks-button','retry-tasks-button','edit-tasks-button','cancel-tasks-button'].forEach(id => {const b=document.getElementById(id);b.disabled=false;b.hidden=false;})`);
    await c.evaluate('document.querySelector("#mobile-action-fab").click();document.querySelector("[data-mobile-group=batch]").click()');
    for (const count of [7, 6, 5, 4, 3, 2, 1]) {
      await c.evaluate(`['queue-selection-mode','select-tasks-button','pause-tasks-button','resume-tasks-button','retry-tasks-button','edit-tasks-button','cancel-tasks-button'].forEach((id,index)=>document.getElementById(id).disabled=index>=${count})`);
      await c.until(`document.querySelectorAll('#mobile-action-items button:not([hidden])').length===${count}`);
      for (const width of [320, 390]) {
        await c.command('Emulation.setDeviceMetricsOverride', {width,height:568,deviceScaleFactor:1,mobile:true});
        // Re-enter the group so each viewport samples fresh bloom and breadcrumb animations.
        await c.evaluate('document.querySelector("[data-mobile-ancestor]").click();document.querySelector("[data-mobile-group=batch]").click()');
        await checkAnimationFrames(count + 2);
      }
    }
    await c.evaluate('document.querySelector("[data-mobile-ancestor]").click();document.querySelector("[data-mobile-group=organize]").click()');
    assert.equal(await c.evaluate('!!document.querySelector("[data-mobile-source=clear-completed-button]")'), true);
    await c.evaluate('document.querySelector("#clear-completed-button").disabled=false;document.querySelector("#clear-completed-button").onclick=()=>hits++');
    await c.until('!document.querySelector("[data-mobile-source=clear-completed-button]").disabled');
    await c.evaluate('document.querySelector("[data-mobile-source=clear-completed-button]").click()');
    assert.equal(await c.evaluate('hits'), 2);
    await c.evaluate('document.querySelector("#mobile-action-fab").click()');
    await c.evaluate('document.querySelector("#mobile-action-fab").click();document.querySelector("#tab-queue").classList.add("hidden")');
    await c.until('document.querySelector("#mobile-action-fab").hidden');
    assert.equal(await c.evaluate('getComputedStyle(document.querySelector("#mobile-action-fab")).display'), 'none');
    await c.evaluate('document.querySelector("#tab-queue").classList.remove("hidden")');
    await c.until('!document.querySelector("#mobile-action-fab").hidden');
    await c.evaluate('document.querySelector("#mobile-action-fab").click()');
    await c.command('Emulation.setDeviceMetricsOverride', {width:1280,height:900,deviceScaleFactor:1,mobile:false});
    await c.until('document.querySelector("#mobile-action-dialog").hidden');
    assert.equal(await c.evaluate('getComputedStyle(document.querySelector("#mobile-action-fab")).display'), 'none');
    assert.notEqual(await c.evaluate('getComputedStyle(document.querySelector("#select-page-button")).display'), 'none');
    nestedMenu = true;
    await c.command('Emulation.setDeviceMetricsOverride', {width:320,height:568,deviceScaleFactor:1,mobile:true});
    await c.command('Page.reload');
    await c.until('!!document.querySelector("#mobile-action-fab")');
    await c.evaluate(`document.querySelector('#tab-queue').classList.add('hidden');document.querySelector('#tab-browse').classList.remove('hidden');document.querySelector('#select-page-button').disabled=false;`);
    await c.evaluate('document.querySelector("#mobile-action-fab").click();document.querySelector("[data-mobile-group=selection]").click();document.querySelector("[data-mobile-group=selection_more]").click()');
    await checkAnimationFrames(4);
    await c.command('Emulation.setEmulatedMedia', {features:[{name:'prefers-reduced-motion',value:'reduce'}]});
    await c.command('Page.reload');
    await c.until('!!document.querySelector("#mobile-action-fab")');
    await c.evaluate(`document.querySelector('#tab-queue').classList.add('hidden');document.querySelector('#tab-browse').classList.remove('hidden');document.querySelector('#select-page-button').disabled=false;`);
    await c.evaluate('document.querySelector("#mobile-action-fab").click();document.querySelector("[data-mobile-group=selection]").click();document.querySelector("[data-mobile-group=selection_more]").click()');
    assert.equal(await c.evaluate('document.querySelectorAll("[data-mobile-ancestor]").length'), 2);
    assert.equal(await c.evaluate('document.querySelector("#mobile-action-dialog").getAnimations({subtree:true}).length'), 0);
    assert.equal(await c.evaluate('document.querySelectorAll("#mobile-action-links line").length'), 2);
    assert.equal(await c.evaluate('document.querySelector("#mobile-action-dialog").getAttribute("aria-label")'), '進階選取');
    await c.evaluate('document.querySelector("#mobile-action-dialog").getAnimations({subtree:true}).forEach(animation=>animation.finish())');
    assert.equal(await c.evaluate(`([...document.querySelectorAll('[data-mobile-ancestor]')].every(b=>{const r=b.getBoundingClientRect();return r.width>=44 && r.height>=44 && r.left>=0 && r.top>=0 && r.right<=innerWidth && r.bottom<=innerHeight && b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}))`), true);
    if (process.env.MOBILE_SCREENSHOT) {
      const image = await c.command('Page.captureScreenshot', {format:'png'});
      fs.writeFileSync(process.env.MOBILE_SCREENSHOT + '.deep.png', Buffer.from(image.data, 'base64'));
    }
    await c.evaluate('document.querySelector("[data-mobile-ancestor=selection_more]").click()');
    assert.equal(await c.evaluate('document.activeElement.dataset.mobileGroup'), 'selection_more');
    assert.equal(await c.evaluate('document.querySelectorAll("[data-mobile-ancestor]").length'), 1);
    await c.evaluate('document.querySelector("[data-mobile-group=selection_more]").click();document.querySelector("[data-mobile-ancestor=selection]").click()');
    assert.equal(await c.evaluate('document.activeElement.dataset.mobileGroup'), 'selection');
    assert.equal(await c.evaluate('document.querySelectorAll("[data-mobile-ancestor]").length'), 0);
    assert.equal(await c.evaluate('document.querySelectorAll("#mobile-action-links line").length'), 0);
    console.log('Mobile portrait FAB, animation frames, hierarchy, delegated actions, live state, focus, Escape and desktop isolation: PASS');
  } finally {c.close();chrome.kill();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
