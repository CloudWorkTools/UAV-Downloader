// Real DOM regression: progress updates must not replace an open task menu.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const html = fs.readFileSync('uav-web/frontend/index.html', 'utf8');
const script = html.slice(html.indexOf('    let currentSite'), html.lastIndexOf('    // Init'));
const fixture = html.slice(html.indexOf('<body'), html.indexOf('  <script src=', html.indexOf('<body')));
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'uav-task-menu-'));
try {
  const page = path.join(temporary, 'test.html');
  fs.writeFileSync(page, fixture + '<script>' +
    fs.readFileSync('uav-web/frontend/video-selection.js', 'utf8') +
    fs.readFileSync('uav-web/frontend/task-selection.js', 'utf8') + script + `
    const check = (condition, message) => { if (!condition) throw new Error(message); };
    (async () => {
      const task = {task_id:'menu-test', status:'downloading', name:'Test video', progress:10, total_segments:100};
      renderTasks([task]);
      const menu = document.querySelector('#tasks-container details');
      menu.querySelector('summary').click();
      await new Promise(resolve => setTimeout(resolve, 0));
      const button = menu.querySelector('button');
      button.focus();
      for (let progress = 11; progress <= 20; progress++) renderTasks([{...task, progress}]);
      check(menu.isConnected && menu.open, 'Background update replaced or closed the open menu');
      check(document.activeElement === button, 'Background update lost keyboard focus');
      check(latestDownloadTasks[0].progress === 20, 'Latest task snapshot was lost');
      menu.querySelector('summary').click();
      await new Promise(resolve => setTimeout(resolve, 30));
      check(document.getElementById('tasks-container').textContent.includes('20%'), 'Closing menu did not apply latest progress');
      let requestedAction = '';
      window.fetch = async (_url, options) => {
        requestedAction = JSON.parse(options.body).action;
        return {ok:true, json:async () => ({success:true})};
      };
      const nextMenu = document.querySelector('#tasks-container details');
      nextMenu.querySelector('summary').click();
      nextMenu.querySelector('button[onclick*=taskAction]').click();
      await new Promise(resolve => setTimeout(resolve, 30));
      check(requestedAction === 'pause', 'Menu action was not submitted');
      check(!document.querySelector('#tasks-container details[open]'), 'Choosing an action left the menu open');
      const lastMenu = document.querySelector('#tasks-container details');
      lastMenu.querySelector('summary').click();
      renderTasks([]);
      check(lastMenu.isConnected, 'Task removal interrupted the open menu');
      lastMenu.querySelector('summary').click();
      await new Promise(resolve => setTimeout(resolve, 30));
      check(document.getElementById('tasks-container').textContent.includes('目前沒有下載任務'), 'Closing menu did not apply task removal');
      document.body.innerHTML = '<p>MENU_TEST_PASS</p>';
    })().catch(error => { document.body.innerHTML = '<p>MENU_TEST_FAIL: ' + error.message + '</p>'; });
    </script>`);
  const result = spawnSync(process.env.CHROME_BIN || 'google-chrome', [
    '--headless', '--no-sandbox', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(temporary, 'profile'), '--dump-dom',
    '--virtual-time-budget=1500', 'file://' + page,
  ], {encoding:'utf8', timeout:20000, maxBuffer:4 * 1024 * 1024});
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  const output = result.stdout.replace(/<script[\s\S]*?<\/script>/g, '');
  assert.match(output, /<body[^>]*>\s*<p>MENU_TEST_PASS<\/p>/, output.match(/<p>MENU_TEST_FAIL:[^<]*/)?.[0] || 'Browser test did not complete');
  console.log('Task menu remains open and focused during progress updates: PASS');
} finally {
  fs.rmSync(temporary, {recursive:true, force:true});
}
