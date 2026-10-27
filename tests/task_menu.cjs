// Real DOM regression: progress updates must not replace an open task menu.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const html = fs.readFileSync('uav-web/frontend/index.html', 'utf8');
const script = html.slice(html.indexOf('    let currentSite'), html.lastIndexOf('    // Init'));
const fixture = html.slice(html.indexOf('<body'), html.indexOf('  <script>\n    let currentSite')).replace(/<script[\s\S]*?<\/script>/g, '');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'uav-task-menu-'));
try {
  const page = path.join(temporary, 'test.html');
  fs.writeFileSync(page, fixture + '<script>' +
    fs.readFileSync('uav-web/frontend/download-batch.js', 'utf8') +
    fs.readFileSync('uav-web/frontend/video-selection.js', 'utf8') +
    fs.readFileSync('uav-web/frontend/task-selection.js', 'utf8') + script + `
    const check = (condition, message) => { if (!condition) throw new Error(message); };
    (async () => {
      const task = {task_id:'menu-test', status:'downloading', name:'Test video', progress:10, total_segments:100};
      renderTasks([task]);
      check(document.getElementById('queue-batch-actions').hidden, 'Batch actions must start hidden');
      document.getElementById('queue-selection-mode').click();
      check(!document.getElementById('queue-batch-actions').hidden, 'Selection mode must expose batch controls');
      document.getElementById('select-tasks-button').click();
      check(document.getElementById('queue-selection-count').textContent === '已選 1 項', 'Selected count must update');
      document.getElementById('queue-selection-mode').click();
      check(document.getElementById('queue-batch-actions').hidden && !document.querySelector('#tasks-container input').checked, 'Leaving selection must clear checks');
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
      let actionRequests = 0;
      window.fetch = async (_url, options) => {
        requestedAction = JSON.parse(options.body).action;
        actionRequests++;
        return {ok:true, json:async () => ({success:true})};
      };
      const nextMenu = document.querySelector('#tasks-container details');
      nextMenu.querySelector('summary').click();
      nextMenu.querySelector('button[onclick*=taskAction]').click();
      await new Promise(resolve => setTimeout(resolve, 30));
      check(requestedAction === 'pause' && actionRequests === 1, 'Menu action must be submitted exactly once');
      check(!document.querySelector('#tasks-container details[open]'), 'Choosing an action left the menu open');
      const lastMenu = document.querySelector('#tasks-container details');
      lastMenu.querySelector('summary').click();
      renderTasks([]);
      check(lastMenu.isConnected, 'Task removal interrupted the open menu');
      lastMenu.querySelector('summary').click();
      await new Promise(resolve => setTimeout(resolve, 30));
      check(document.getElementById('tasks-container').textContent.includes('目前沒有下載任務'), 'Closing menu did not apply task removal');
      // Selection and the modal are independent of the replaceable task cards.
      const paused = {...task, status:'paused', progress:20};
      renderTasks([paused]);
      document.querySelector('#tasks-container input[type=checkbox]').click();
      for (let progress = 21; progress <= 30; progress++) renderTasks([{...paused, progress}]);
      check(document.querySelector('#tasks-container input[type=checkbox]').checked, 'Background updates lost the selected task');
      check(!document.getElementById('edit-tasks-button').hidden, 'Selection no longer offers editing');
      let saveRequests = 0;
      window.fetch = async (url, options) => {
        if (url === '/api/settings') return {ok:true, json:async () => ({download_dir:'/downloads'})};
        saveRequests++;
        const payload = JSON.parse(options.body);
        check(payload.task_ids.join() === 'menu-test' && payload.dest === '/downloads/edited', 'Edited selection or destination was lost');
        return {ok:false, json:async () => ({error:'Destination is offline'})};
      };
      document.getElementById('edit-tasks-button').click();
      await new Promise(resolve => setTimeout(resolve, 0));
      const dialog = document.getElementById('task-edit-dialog');
      check(dialog.open, 'Edit dialog did not open');
      const storage = document.getElementById('task-edit-storage');
      storage.value = 'local';
      storage.dispatchEvent(new Event('change'));
      const destination = document.getElementById('task-edit-dest');
      destination.value = '/downloads/edited';
      destination.focus();
      destination.setSelectionRange(11, 17);
      for (let progress = 31; progress <= 40; progress++) renderTasks([{...paused, progress}]);
      check(dialog.open && destination.value === '/downloads/edited', 'Background updates discarded the unfinished edit');
      check(document.activeElement === destination && destination.selectionStart === 11 && destination.selectionEnd === 17, 'Background updates lost the editing focus or text selection');
      document.getElementById('task-edit-save').click();
      await new Promise(resolve => setTimeout(resolve, 0));
      check(saveRequests === 1, 'Edit must be submitted exactly once');
      check(dialog.open && destination.value === '/downloads/edited', 'Failed save discarded the unfinished edit');
      check(document.getElementById('task-edit-status').textContent === 'Destination is offline', 'Failed save did not show the server error');
      dialog.querySelector('button[type=button]').click();
      check(!dialog.open, 'User could not close the edit dialog');
      check(document.getElementById('tasks-container').textContent.includes('40%'), 'Closing edit did not show the latest task data');
      const pending = {...task,task_id:'immediate-test',status:'pending'};
      renderTasks([pending]);
      const immediate = [...document.querySelectorAll('#tasks-container button')].find(button=>button.textContent==='立即下載');
      check(immediate && !immediate.disabled, 'Pending task has no immediate-download action');
      let immediateRequests=0, finishImmediate;
      window.fetch=async(url,options)=>{
        check(url==='/api/download/action', 'Wrong immediate-download endpoint');
        const body=JSON.parse(options.body);
        check(body.action==='start_now' && body.task_id===pending.task_id, 'Wrong immediate action or task');
        immediateRequests++;
        await new Promise(resolve=>{finishImmediate=resolve;});
        return {ok:true,json:async()=>({success:true})};
      };
      immediate.click(); immediate.click();
      await new Promise(resolve=>setTimeout(resolve,0));
      check(immediateRequests===1, 'Immediate action submitted twice');
      finishImmediate();await new Promise(resolve=>setTimeout(resolve,0));
      for(const status of ['paused','failed','cancelled'])check(taskSelection.canAction({...pending,status},'start_now'),'Retryable task cannot start immediately');
      for(const status of ['downloading','completed','skipped','pausing'])check(!taskSelection.canAction({...pending,status},'start_now'),'Active or finished task permits immediate action');
      renderTasks([]);
      check(document.getElementById('edit-tasks-button').hidden, 'Removed task remained editable');
      const mixed = [{...task, task_id:'running', status:'downloading'},
        {...task, task_id:'done', status:'completed'}, {...task, task_id:'waiting', status:'pending'}];
      renderTasks(mixed);
      taskSelection.toggleAll();
      let removalRequests = [];
      window.fetch = async (url, options) => {
        if (!options) return {ok:true,json:async()=>({tasks:mixed.filter(task => task.task_id !== 'done')})};
        const payload = JSON.parse(options.body);
        removalRequests.push(payload);
        check(payload.task_ids.join() === 'done', 'Unchecked categories were submitted');
        return {ok:true,json:async()=>({success:true,accepted:[],skipped:['done'],errors:[]})};
      };
      document.getElementById('cancel-tasks-button').click();
      const removal = document.getElementById('task-remove-dialog');
      check(removal.open, 'Removal categories did not open in a real modal');
      check(['running','completed','waiting'].every(group => document.getElementById('task-remove-' + group + '-count').textContent === '1'), 'Removal counts are incorrect');
      removal.querySelector('button[type=button]').click();
      check(!removal.open && removalRequests.length === 0, 'Returning from the modal changed tasks');
      document.getElementById('cancel-tasks-button').click();
      document.getElementById('task-remove-running').click();
      document.getElementById('task-remove-waiting').click();
      removal.querySelector('button[type=submit]').click();
      await new Promise(resolve => setTimeout(resolve, 0));
      check(!removal.open && removalRequests.length === 1, 'Confirmed category was not processed once');
      check(taskSelection.has('running') && taskSelection.has('waiting') && !taskSelection.has('done'), 'Removal changed unchecked selections');
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
  console.log('Task menu, selection and unfinished edits survive progress updates: PASS');
} finally {
  fs.rmSync(temporary, {recursive:true, force:true});
}
