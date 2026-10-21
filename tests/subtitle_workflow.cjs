const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const {once} = require('node:events');
const {Cdp} = require('./subtitle_cdp.cjs');
const {createFixture} = require('./subtitle_model_fixture.cjs');

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'uav-subtitle-workflow-'));
const configDir = path.join(root, 'config');
const downloadsDir = path.join(root, 'downloads');
fs.mkdirSync(configDir); fs.mkdirSync(downloadsDir);
const downloadsOriginalMode = fs.statSync(downloadsDir).mode & 0o777;
const fixture = createFixture({host: fs.existsSync('/usr/local/bin/uav-web') ? '127.0.0.1' : '0.0.0.0'});
let fixturePort;
let app;
let runtime;
let directLaunch;
let chrome;
let cdp;
let appContainer;
let imageBuilt = false;
let image = null;
let appLog = '';
let chromeLog = '';
let evidence = [];
const screenshots = [];
const fixtureEvidenceByCase = Object.create(null);
const scenarioDetails = Object.create(null);
let currentCase = null;
let cleanupPromise = null;
let downloadsReadOnly = false;
let testReport;
let testFailure;
const cleanupErrors = [];

function command(program, args, options = {}) {
  const result = spawnSync(program, args, {encoding: 'utf8', timeout: 60000, ...options});
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${program} ${args.join(' ')} failed (${result.status}): ${(result.stderr || result.stdout || '').slice(-4000)}`);
  return result.stdout.trim();
}

function commandAsync(program, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(program, args, {stdio: ['ignore', 'pipe', 'pipe'], ...options});
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    const timeout = setTimeout(() => { child.kill('SIGTERM'); reject(new Error(`${program} timed out: ${args.join(' ')}`)); }, 20 * 60 * 1000);
    child.once('error', error => { clearTimeout(timeout); reject(error); });
    child.once('exit', (code, signal) => {
      clearTimeout(timeout);
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(`${program} ${args.join(' ')} failed (${signal || code}): ${(stderr || stdout).slice(-5000)}`));
    });
  });
}

function freePort() {
  const server = require('node:net').createServer();
  return new Promise((resolve, reject) => server.listen(0, '127.0.0.1', () => {
    const port = server.address().port;
    server.close(error => error ? reject(error) : resolve(port));
  }));
}

async function waitUntil(check, label, timeoutMs = 30000) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    last = await check();
    if (last) return last;
    if (app?.exitCode !== null && app?.exitCode !== undefined) throw new Error(`app exited while waiting for ${label}: ${appLog.slice(-3000)}`);
    if (chrome?.exitCode !== null && chrome?.exitCode !== undefined) throw new Error(`Chrome exited while waiting for ${label}: ${chromeLog.slice(-3000)}`);
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`timed out waiting for ${label}; last=${JSON.stringify(last)}`);
}

async function stopChild(child, processGroup = false) {
  if (!child || child.exitCode !== null) return;
  try { processGroup ? process.kill(-child.pid, 'SIGTERM') : child.kill('SIGTERM'); }
  catch { try { child.kill('SIGTERM'); } catch {} }
  await Promise.race([once(child, 'exit'), new Promise(resolve => setTimeout(resolve, 3000))]);
  if (child.exitCode === null) {
    try { processGroup ? process.kill(-child.pid, 'SIGKILL') : child.kill('SIGKILL'); }
    catch { try { child.kill('SIGKILL'); } catch {} }
    await Promise.race([once(child, 'exit'), new Promise(resolve => setTimeout(resolve, 1000))]);
  }
}

function makeMedia() {
  const ffmpeg = process.env.FFMPEG || 'ffmpeg';
  for (const name of ['subtitle-fixture-a.mp4', 'subtitle-fixture-b.mp4', 'subtitle-fixture-c.mp4', 'subtitle-fixture-d.mp4', 'subtitle-fixture-history.mp4']) {
    command(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
      'color=c=black:s=320x240:r=15:d=3', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
      '-shortest', '-c:v', 'mpeg4', '-q:v', '8', '-c:a', 'aac', '-y', path.join(downloadsDir, name)]);
  }
  command(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
    'color=c=black:s=320x240:r=15:d=35', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=35',
    '-shortest', '-c:v', 'mpeg4', '-q:v', '10', '-c:a', 'aac', '-y', path.join(downloadsDir, 'subtitle-fixture-long.mp4')]);
  command(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
    'color=c=black:s=320x240:r=15:d=3', '-f', 'lavfi', '-i', 'aevalsrc=0:d=3:s=16000',
    '-shortest', '-c:v', 'mpeg4', '-q:v', '10', '-c:a', 'aac', '-y', path.join(downloadsDir, 'subtitle-fixture-silent.mp4')]);
}

function seedLegacyStartupData(fixtureUrl, direct) {
  const productDataDir = path.join(direct ? root : configDir, 'appdata', 'UAV Downloader');
  fs.mkdirSync(productDataDir, {recursive: true});
  const legacyAsrPath = path.join(configDir, 'asr.json');
  const legacyTranslationPath = path.join(productDataDir, 'translation_api.json');
  const legacyAsr = JSON.stringify({base_url: fixtureUrl, model: 'legacy-asr'});
  const legacyTranslation = JSON.stringify({schema: 1, selected_provider: 'openai-compatible', profiles: {
    'openai-compatible': {base_url: fixtureUrl, model: 'legacy-text', api_key_protected: ''},
  }});
  fs.writeFileSync(legacyAsrPath, legacyAsr);
  fs.writeFileSync(legacyTranslationPath, legacyTranslation);
  const historyPath = path.join(productDataDir, 'download_history.json');
  const historyVideo = path.join(direct ? downloadsDir : '/downloads', 'subtitle-fixture-history.mp4');
  fs.writeFileSync(historyPath, JSON.stringify({version: 1, entries: {
    'w8-history-source': {url: 'https://example.invalid/w8-history-source', title: 'subtitle-fixture-history.mp4',
      code: 'W8-HISTORY-SOURCE', clean_title: 'subtitle fixture history mp4', dest_dir: downloadsDir,
      file_path: historyVideo, site: 'fixture', downloaded_at: '2026-10-20T00:00:00Z'},
  }}));
  return {legacyAsrPath, legacyTranslationPath, legacyAsr, legacyTranslation};
}

function chromeExecutable() {
  for (const candidate of [process.env.CHROME_BIN, '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser']) {
    if (candidate && fs.existsSync(candidate)) return candidate;
  }
  throw new Error('Chrome/Chromium executable is required for subtitle browser acceptance');
}

async function startApp() {
  const binary = '/usr/local/bin/uav-web';
  if (fs.existsSync(binary)) {
    const appPort = await freePort();
    const daemonPort = await freePort();
    const isolatedEnv = Object.fromEntries(Object.entries(process.env)
      .filter(([name]) => !/^(UAV_|ALOS_|JABLE_|OPENAI_|ANTHROPIC_|GEMINI_|GOOGLE_|OLLAMA_|LMSTUDIO_|MODEL_|TRANSLATION_)/i.test(name)));
    directLaunch = {binary, env: {...isolatedEnv, CONFIG_DIR: configDir, DOWNLOAD_DIR: downloadsDir,
      APPDATA: path.join(root, 'appdata'),
      UAV_HOST: '127.0.0.1', UAV_PORT: String(appPort), UAV_DAEMON_HOST: '127.0.0.1',
      UAV_DAEMON_PORT: String(daemonPort), UAV_FIRECRAWL_ENABLED: '0'}};
    spawnDirectApp();
    runtime = {url: `http://127.0.0.1:${appPort}/`, fixtureUrl: `http://127.0.0.1:${fixturePort}/v1`, mode: 'direct-binary'};
    return runtime;
  }

  if (!process.env.SUBTITLE_IMAGE) {
    image = `uav-subtitle-workflow:${process.pid}-${Date.now()}`;
    imageBuilt = true;
    await commandAsync('docker', ['build', '--pull=false', '--tag', image, '--file', 'Dockerfile.web', '.']);
  } else image = process.env.SUBTITLE_IMAGE;
  appContainer = `uav-subtitle-workflow-${process.pid}-${Date.now()}`;
  command('docker', ['run', '-d', '--name', appContainer, '-p', '127.0.0.1::8080',
    '--add-host=host.docker.internal:host-gateway', '-e', 'CONFIG_DIR=/config', '-e', 'APPDATA=/config/appdata', '-e', 'DOWNLOAD_DIR=/downloads',
    '-e', 'UAV_FIRECRAWL_ENABLED=0', '-e', 'PUID=1000', '-e', 'PGID=1000',
    '-v', `${configDir}:/config`, '-v', `${downloadsDir}:/downloads`, image]);
  const portText = command('docker', ['port', appContainer, '8080/tcp']);
  const appPort = Number(portText.match(/:(\d+)$/)?.[1]);
  assert.ok(appPort > 0, `invalid mapped app port: ${portText}`);
  runtime = {url: `http://127.0.0.1:${appPort}/`, fixtureUrl: `http://host.docker.internal:${fixturePort}/v1`, mode: 'docker-image'};
  return runtime;
}

function spawnDirectApp() {
  const {binary, ...options} = directLaunch;
  app = spawn(binary, [], {...options, detached: true});
  app.stdout.on('data', bytes => { appLog += bytes.toString(); });
  app.stderr.on('data', bytes => { appLog += bytes.toString(); });
}

async function restartRuntime() {
  if (appContainer) {
    command('docker', ['restart', appContainer]);
    const portText = command('docker', ['port', appContainer, '8080/tcp']);
    const appPort = Number(portText.match(/:(\d+)$/)?.[1]);
    assert.ok(appPort > 0, `invalid mapped app port after restart: ${portText}`);
    runtime.url = `http://127.0.0.1:${appPort}/`;
  }
  else {
    await stopChild(app, true);
    spawnDirectApp();
  }
  await waitForApp(runtime.url, 90000);
  await cdp.command('Network.setBlockedURLs', {urls: []});
  await cdp.command('Network.clearBrowserCache');
  const styleEventStart = cdp.eventLog.length;
  await cdp.command('Page.navigate', {url: runtime.url});
  await cdp.until(`document.readyState === 'complete'`);
  await assertStyledFrontend(styleEventStart);
}

async function waitForApp(url, timeoutMs = 45000) {
  let lastResponse = 'no response';
  try { await waitUntil(async () => {
    try {
      const response = await fetch(new URL('/api/status', url));
      const body = await response.text();
      lastResponse = `HTTP ${response.status}: ${body.slice(0, 240)}`;
      if (response.ok) {
        const status = JSON.parse(body);
        if (status.status === 'ok' && status.version) return status;
      }
      const tasks = await fetch(new URL('/api/subtitles/tasks', url));
      const taskBody = await tasks.text();
      lastResponse += `; /api/subtitles/tasks HTTP ${tasks.status}: ${taskBody.slice(0, 240)}`;
      return tasks.ok ? {ready: true} : false;
    } catch (error) { lastResponse = String(error); return false; }
  }, 'Rust web and supervised Python daemon readiness', timeoutMs); }
  catch (error) { throw new Error(`${error.message}; last response: ${lastResponse}`); }
}

async function startChrome(url) {
  chrome = spawn(chromeExecutable(), ['--headless=new', '--no-sandbox', '--disable-gpu',
    '--disable-dev-shm-usage', '--no-first-run', '--disable-background-networking',
    '--remote-debugging-pipe', `--user-data-dir=${path.join(root, 'chrome-profile')}`,
    'about:blank'], {detached: true, stdio: ['ignore', 'ignore', 'pipe', 'pipe', 'pipe']});
  chrome.stderr.setEncoding('utf8');
  chrome.stderr.on('data', data => { chromeLog += data; });
  cdp = new Cdp(chrome);
  await cdp.start(url);
  await cdp.command('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
}

async function assertStyledFrontend(eventStart = 0) {
  const events = () => cdp.eventLog.slice(eventStart);
  const ready = `(() => {const body=getComputedStyle(document.body),hidden=document.querySelector('.tab-content.hidden');return body.backgroundColor==='rgb(18, 18, 23)'&&hidden&&getComputedStyle(hidden).display==='none';})()`;
  try { await cdp.until(ready, 30000); }
  catch (error) {
    const pageStyles = await cdp.evaluate(`(() => {const body=getComputedStyle(document.body),hidden=document.querySelector('.tab-content.hidden');return {bodyBackground:body.backgroundColor,bodyColor:body.color,hiddenTabDisplay:hidden?getComputedStyle(hidden).display:null,hiddenTabClass:hidden?.className||null,tailwindScript:[...document.scripts].some(item=>item.src.includes('cdn.tailwindcss.com'))};})()`);
    const requestById = new Map(events().filter(item => item.method === 'Network.requestWillBeSent'
      && item.params.request?.url?.includes('cdn.tailwindcss.com'))
      .map(item => [item.params.requestId, item.params.request.url]));
    const network = events().filter(item => (item.method === 'Network.responseReceived'
      || item.method === 'Network.loadingFailed') && requestById.has(item.params.requestId))
      .map(item => ({method: item.method, url: requestById.get(item.params.requestId),
        status: item.params.response?.status, errorText: item.params.errorText, blockedReason: item.params.blockedReason}));
    const exceptions = events().filter(item => item.method === 'Runtime.exceptionThrown')
      .map(item => ({text: item.params.exceptionDetails?.text, message: item.params.exceptionDetails?.exception?.description?.slice(0, 500)}));
    throw new Error(`${error.message}; frontend style diagnostics=${JSON.stringify({pageStyles, network, exceptions})}`);
  }
  frontendStyleEvidence = await cdp.evaluate(`(() => {const body=getComputedStyle(document.body),hidden=document.querySelector('.tab-content.hidden');return {bodyBackground:body.backgroundColor,hiddenTabDisplay:getComputedStyle(hidden).display,tailwindScript:[...document.scripts].some(item=>item.src.includes('cdn.tailwindcss.com'))};})()`);
  assert.equal(frontendStyleEvidence.bodyBackground, 'rgb(18, 18, 23)', 'Tailwind CDN styles set the expected product background');
  assert.equal(frontendStyleEvidence.hiddenTabDisplay, 'none', 'Tailwind hidden utility keeps inactive tabs hidden');
  const tailwindRequests = new Map(events().filter(item => item.method === 'Network.requestWillBeSent'
    && item.params.request?.url?.includes('cdn.tailwindcss.com'))
    .map(item => [item.params.requestId, item.params.request.url]));
  const tailwindResponses = events().filter(item => item.method === 'Network.responseReceived'
    && tailwindRequests.has(item.params.requestId))
    .map(item => ({url: tailwindRequests.get(item.params.requestId), status: item.params.response?.status}));
  assert.ok(tailwindResponses.some(item => item.status >= 200 && item.status < 400),
    `real Tailwind CDN script request succeeds before external HTTPS is blocked: ${JSON.stringify({tailwindResponses,
      failures: events().filter(item => item.method === 'Network.loadingFailed' && tailwindRequests.has(item.params.requestId))
        .map(item => ({url: tailwindRequests.get(item.params.requestId), errorText: item.params.errorText, blockedReason: item.params.blockedReason}))})}`);
  await cdp.command('Network.setBlockedURLs', {urls: ['https://*/*']});
}

async function click(testId) { return cdp.click(testId); }
async function clickId(id) { return cdp.evaluate(`document.getElementById(${JSON.stringify(id)})?.click()`); }
async function pressEnter() {
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
  await cdp.command('Input.dispatchKeyEvent', {type: 'char', key: 'Enter', code: 'Enter', text: '\r', unmodifiedText: '\r', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
}
async function clickRenderedId(id) {
  const rect = await cdp.evaluate(`(() => {const element=document.getElementById(${JSON.stringify(id)});if(!element)return null;element.scrollIntoView({block:'center',inline:'nearest'});const r=element.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,width:r.width,height:r.height};})()`);
  assert.ok(rect && rect.width > 0 && rect.height > 0, `button ${id} must have a visible hit target`);
  await cdp.command('Input.dispatchMouseEvent', {type: 'mouseMoved', x: rect.x, y: rect.y});
  await cdp.command('Input.dispatchMouseEvent', {type: 'mousePressed', x: rect.x, y: rect.y, button: 'left', clickCount: 1});
  await cdp.command('Input.dispatchMouseEvent', {type: 'mouseReleased', x: rect.x, y: rect.y, button: 'left', clickCount: 1});
}
async function clickVisibleButtonIn(testId, label) {
  const rect = await cdp.evaluate(`(() => {const row=document.querySelector('[data-testid="${testId}"]');const button=[...(row?.querySelectorAll('button')||[])].find(item=>item.textContent.includes(${JSON.stringify(label)}));if(!button)return null;button.scrollIntoView({block:'center',inline:'nearest'});const r=button.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,width:r.width,height:r.height};})()`);
  assert.ok(rect && rect.width > 0 && rect.height > 0, `${label} action in ${testId} must have a visible hit target`);
  await cdp.command('Input.dispatchMouseEvent', {type: 'mouseMoved', x: rect.x, y: rect.y});
  await cdp.command('Input.dispatchMouseEvent', {type: 'mousePressed', x: rect.x, y: rect.y, button: 'left', clickCount: 1});
  await cdp.command('Input.dispatchMouseEvent', {type: 'mouseReleased', x: rect.x, y: rect.y, button: 'left', clickCount: 1});
}
async function fill(testId, value) { return cdp.fill(testId, value); }
async function fillId(id, value) { return cdp.fillId(id, value); }
async function check(testId, label) {
  const result = await cdp.selector(testId);
  assert.equal(result.hidden, false, `${label || testId} should be visible`);
  return result;
}
async function fixtureRecords() { return fixture.snapshot(); }
async function clearFixture() { await fixture.reset(); }
async function assertStages(expected) {
  const records = fixtureEvidenceByCase.B2 || [];
  const actual = records.filter(item => ['vad', 'asr', 'correction', 'translation'].includes(item.kind)).map(item => item.kind);
  assert.deepEqual(actual.slice(0, expected.length), expected);
  return records;
}

let fixtureBaseUrl;
let legacyFixture;
let selectedVideoId = null;
let frontendStyleEvidence = null;
const videoIds = {};

async function saveProfile({id, name, kind, model, capabilities, key = '', provider = 'localai', baseUrl = fixtureBaseUrl}) {
  await cdp.evaluate(`document.getElementById('subtitle-profile-new')?.click()`);
  await cdp.fillId('subtitle-profile-id', id);
  await fill('subtitle-profile-name', name);
  await cdp.selectId('subtitle-profile-kind', kind);
  await cdp.selectId('subtitle-profile-provider', provider);
  await fill('subtitle-profile-base-url', baseUrl);
  await fill('subtitle-profile-model', model);
  await fill('subtitle-profile-api-key', key);
  for (const stage of ['vad', 'asr', 'correction', 'translation'])
    await cdp.setChecked(`subtitle-profile-cap-${stage}`, capabilities.includes(stage));
  await click('subtitle-profile-save');
  await cdp.until(`document.querySelector('[data-testid="subtitle-profile-list"]')?.textContent.includes(${JSON.stringify(name)})`);
}

async function setPipeline({stages = ['vad', 'asr', 'correction', 'translation'], outputs = ['zh-TW'], target = ['zh-TW'], allowReasoning = true} = {}) {
  for (const stage of ['vad', 'asr', 'correction', 'translation']) {
    await cdp.setChecked(`subtitle-stage-enabled-${stage}`, stages.includes(stage));
    const profileId = stage === 'vad' ? 'workflow-vad' : stage === 'asr' ? 'workflow-asr' : 'workflow-text';
    if (stages.includes(stage)) await cdp.selectId(`subtitle-stage-profile-${stage}`, profileId);
  }
  await cdp.evaluate(`for(const e of document.querySelectorAll('input[name="subtitle-target-language"]')){e.checked=${JSON.stringify(target)}.includes(e.value);e.dispatchEvent(new Event('change',{bubbles:true}));}
    for(const e of document.querySelectorAll('input[name="subtitle-output-language"]')){e.checked=${JSON.stringify(outputs)}.includes(e.value);e.dispatchEvent(new Event('change',{bubbles:true}));}`);
  await cdp.setChecked('subtitle-allow-reasoning', allowReasoning);
}

async function selectVideoByName(name) {
  const video = await cdp.evaluate(`(() => {
    const row = [...document.querySelectorAll('[data-testid^="subtitle-video-"]')]
      .find(item => item.closest('label')?.textContent.includes(${JSON.stringify(name)}));
    return row ? {id: row.dataset.testid, value: row.value} : null;
  })()`);
  assert.ok(video, `video ${name} must appear in server-side list`);
  selectedVideoId = video.value;
  videoIds[name] = video.value;
  await cdp.click(video.id);
  return video.value;
}

async function clearVideoSelection() {
  await cdp.evaluate(`for(const item of document.querySelectorAll('[data-testid^="subtitle-video-"]')){item.checked=false;item.dispatchEvent(new Event('change',{bubbles:true}));}`);
}

async function submitSelected({doubleClick = false} = {}) {
  await click('subtitle-submit');
  await cdp.until(`document.getElementById('subtitle-confirm-dialog')?.open`);
  await click('subtitle-confirm-submit');
  if (doubleClick) await click('subtitle-confirm-submit');
  await cdp.until(`document.querySelector('[data-testid="subtitle-task-list"]')?.querySelector('[data-testid^="subtitle-task-"]')`);
}

async function taskIds() {
  return cdp.evaluate(`Array.from(document.querySelectorAll('[data-testid^="subtitle-task-subtitle_"]')).map(e=>e.dataset.testid.slice('subtitle-task-'.length))`);
}

async function appTaskIds() {
  return cdp.evaluate(`window.subtitleTasks.state.tasks.map(item=>item.task_id)`);
}

async function taskStatus(taskId) {
  return cdp.evaluate(`window.subtitleTasks.state.tasks.find(item=>item.task_id===${JSON.stringify(taskId)})?.status || null`);
}

async function waitTaskStatus(taskId, expected, timeoutMs = 60000) {
  const statuses = Array.isArray(expected) ? expected : [expected];
  try { await waitUntil(async () => {
    const value = await taskStatus(taskId);
    if (statuses.includes(value)) return value;
    await cdp.evaluate('window.subtitleTasks.refresh().then(()=>true)');
    return statuses.includes(await taskStatus(taskId)) ? await taskStatus(taskId) : false;
  }, `task ${taskId} status ${statuses.join('|')}`, timeoutMs); }
  catch (error) {
    const diagnostic = await cdp.evaluate(`(() => {const task=window.subtitleTasks.state.tasks.find(item=>item.task_id===${JSON.stringify(taskId)});const row=document.querySelector('[data-testid="subtitle-task-${taskId}"]');const action=document.querySelector('[data-testid="subtitle-action-cancel-${taskId}"]');return {task:task?{status:task.status,current_stage:task.current_stage,progress:task.progress,error:task.error}:null,row:row?.innerText||null,active:document.activeElement?.dataset?.testid||document.activeElement?.id||'',cancelButton:action?{disabled:action.disabled,connected:action.isConnected}:null};})()`);
    throw new Error(`${error.message}; rendered task diagnostic=${JSON.stringify(diagnostic)}`);
  }
}

async function selectResults(videoId) {
  await cdp.selectId('subtitle-result-video', videoId);
  await cdp.evaluate(`(()=>{window.__subtitleLoadClickObserved=false;window.__subtitleLoadClickValue=null;
    window.__subtitleResultMutations=0;
    const subtitleResultsBox=document.getElementById('subtitle-results');
    if(subtitleResultsBox)new MutationObserver(()=>window.__subtitleResultMutations++).observe(subtitleResultsBox,{childList:true,subtree:true});
    document.getElementById('subtitle-load-results')?.addEventListener('click',()=>{window.__subtitleLoadClickObserved=true;
      window.__subtitleLoadClickValue=document.getElementById('subtitle-result-video')?.value||null;
      window.__subtitleActiveAtClick=window.subtitleTasks?.state?.activeVideo||null;},{capture:true});})()`);
  const before = await cdp.evaluate(`({activeTag:document.activeElement?.tagName||'',activeId:document.activeElement?.id||'',
    activeInside:document.getElementById('subtitle-results')?.contains(document.activeElement)||false,
    selected:document.getElementById('subtitle-result-video')?.value||'',tasksExists:!!window.subtitleTasks,
    childCount:document.getElementById('subtitle-results')?.children.length||0,
    panel:document.getElementById('subtitle-results')?.innerText||''})`);
  const eventStart = cdp.eventLog.length;
  await clickRenderedId('subtitle-load-results');
  const responseEvent = await waitUntil(async () => cdp.eventLog.slice(eventStart).find(item =>
    item.method === 'Network.responseReceived' && item.params.response?.url?.includes(`/api/subtitles/videos/${encodeURIComponent(videoId)}/results`)) || false,
  `browser result response for ${videoId}`, 15000);
  await waitUntil(async () => {
    const state = await cdp.evaluate(`({rows:document.querySelectorAll('[data-testid="subtitle-results"] [data-testid^="subtitle-result-"]').length,
      error:document.querySelector('[data-testid="subtitle-error"]')?.textContent || ''})`);
    return state.rows || state.error ? state : false;
  }, `results for video ${videoId}`, 15000);
  const visibleState = await cdp.evaluate(`({rows:document.querySelectorAll('[data-testid="subtitle-results"] [data-testid^="subtitle-result-"]').length,
    error:document.querySelector('[data-testid="subtitle-error"]')?.textContent || '',
    selected:document.getElementById('subtitle-result-video')?.value || '',
    activeTag:document.activeElement?.tagName || '', activeId:document.activeElement?.id || '',
    activeInside:document.getElementById('subtitle-results')?.contains(document.activeElement) || false,
    childCount:document.getElementById('subtitle-results')?.children.length || 0,
    stateResults:window.subtitleTasks?.state?.results?.length ?? null,
    clickObserved:window.__subtitleLoadClickObserved ?? false,
    clickedValue:window.__subtitleLoadClickValue ?? null,
    activeAtClick:window.__subtitleActiveAtClick ?? null,
    resultMutations:window.__subtitleResultMutations||0,
    active:document.activeElement?.outerHTML?.slice(0,500) || '',
    panel:document.getElementById('subtitle-results')?.innerText || '',before:${JSON.stringify(before)}})`);
  if (!visibleState.rows) {
    const apiDiagnostic = await cdp.evaluate(`fetch('/api/subtitles/videos/'+encodeURIComponent(${JSON.stringify(videoId)})+'/results')
      .then(async response=>({status:response.status,body:await response.json()})).catch(error=>({error:String(error)}))`);
    const moduleDiagnostic = await cdp.evaluate(`(async()=>{
      const source=await fetch('/subtitle-tasks.js').then(response=>response.text());
      const before={activeVideo:window.subtitleTasks?.state?.activeVideo||null,results:window.subtitleTasks?.state?.results?.length??null,
        counterInitialized:/resultsRequest\\s*=\\s*0/.test(source)};
      await window.subtitleTasks?.loadResults(${JSON.stringify(videoId)});
      return {before,after:{activeVideo:window.subtitleTasks?.state?.activeVideo||null,results:window.subtitleTasks?.state?.results?.length??null,
        rows:document.querySelectorAll('[data-testid="subtitle-results"] [data-testid^="subtitle-result-"]').length}};
    })()`);
    assert.ok(visibleState.rows, `results panel should contain versions for ${videoId}; network=${JSON.stringify({status:responseEvent.params.response.status,url:responseEvent.params.response.url})}; uiState=${JSON.stringify(visibleState)}; api=${JSON.stringify(apiDiagnostic)}; module=${JSON.stringify(moduleDiagnostic)}`);
  }
}

async function resultRow(stage) {
  const stageLabel = {vad: '語音區間', asr: '語音辨識', correction: '字幕校正', translation: '字幕翻譯'}[stage] || stage;
  const selector = await cdp.evaluate(`(() => [...document.querySelectorAll('[data-testid="subtitle-results"] [data-testid^="subtitle-result-"]')]
    .find(row => row.querySelector('h3')?.textContent.includes(${JSON.stringify(` · ${stageLabel} · `)}))?.dataset.testid || null)()`);
  assert.ok(selector, `saved ${stage} result must be visible`);
  return selector;
}

async function redoOnlyStage(taskId, stage, profileId, inputStage = null, language = 'zh-TW') {
  const stageLabels = {vad: '語音區間', asr: '語音辨識', correction: '字幕校正', translation: '字幕翻譯'};
  const beforeIds = await appTaskIds();
  await click(`subtitle-action-redo-${taskId}`);
  await cdp.until(`document.getElementById('subtitle-redo-dialog')?.open`);
  await cdp.selectId('subtitle-redo-stage', stage);
  await cdp.until(`document.getElementById('subtitle-redo-profile')?.options.length > 0`);
  await cdp.selectId('subtitle-redo-profile', profileId);
  let inputResultId = null;
  if (inputStage) {
    await cdp.until(`document.getElementById('subtitle-redo-input')?.options.length > 1`);
    inputResultId = await cdp.evaluate(`(() => [...document.getElementById('subtitle-redo-input').options]
      .find(option=>option.textContent.trim().startsWith(${JSON.stringify(stageLabels[inputStage] || inputStage)}))?.value || null)()`);
    assert.ok(inputResultId, `${stage} redo exposes an explicit ${inputStage} upstream result`);
    await cdp.selectId('subtitle-redo-input', inputResultId);
  }
  if (stage === 'translation') await fillId('subtitle-redo-language', language);
  const summary = await cdp.evaluate(`document.getElementById('subtitle-redo-summary')?.textContent || ''`);
  assert.ok(summary.includes(stageLabels[stage] || stage), `redo summary names the requested ${stage} stage`);
  if (inputResultId) assert.ok(summary.includes(inputResultId), `redo summary names selected upstream result ${inputResultId}`);
  await clickId('subtitle-redo-confirm');
  await clickId('subtitle-redo-confirm');
  await cdp.until(`window.subtitleTasks.state.tasks.length > ${beforeIds.length}`);
  const newTaskId = (await appTaskIds()).find(id => !beforeIds.includes(id));
  await waitTaskStatus(newTaskId, 'completed');
  await clickId('subtitle-refresh');
  return {taskId: newTaskId, inputResultId};
}

// Scenario bodies are kept separate so each case can reset and assert the HTTP fixture independently.
async function B1() {
  await click('subtitle-tab');
  await cdp.until(`document.querySelector('[data-testid="subtitle-panel"]') && !document.querySelector('[data-testid="subtitle-panel"]').classList.contains('hidden')`);
  await check('subtitle-vad-capability', 'local VAD capability');
  await check('subtitle-profile-form', 'profile editor');
  await fixture.control({discovery: 'error', expectedKey: 'uav-test-secret-never-log'});
  await saveProfile({id: 'workflow-vad', name: 'Fixture VAD', kind: 'vad', model: 'fixture-vad', capabilities: ['vad']});
  await saveProfile({id: 'workflow-vad-alt', name: 'Fixture VAD Alternate', kind: 'vad', model: 'fixture-vad-alt', capabilities: ['vad']});
  await saveProfile({id: 'workflow-asr', name: 'Fixture ASR', kind: 'asr', model: 'fixture-asr', capabilities: ['asr']});
  await saveProfile({id: 'workflow-text', name: 'Fixture Text', kind: 'text', model: 'fixture-text', capabilities: ['correction', 'translation'], key: 'uav-test-secret-never-log'});
  await click('subtitle-discover-models');
  await cdp.until(`document.getElementById('subtitle-settings-status')?.textContent.includes('手動輸入')`);
  const discovery = await fixtureRecords();
  assert.equal(discovery.filter(item => item.kind === 'discovery').length, 1);
  assert.equal(discovery.find(item => item.kind === 'discovery').status, 503);
  assert.equal(discovery.find(item => item.kind === 'discovery').authMatchesExpected, true);
  await clearFixture();
  await fixture.control({expectedKey: 'uav-test-secret-never-log'});
  await cdp.selectId('subtitle-test-stage', 'correction');
  await click('subtitle-test-profile');
  await cdp.until(`document.getElementById('subtitle-settings-status')?.textContent.includes('格式符合')`);
  const verificationRecords = await fixtureRecords();
  assert.deepEqual(verificationRecords.map(item => item.kind),
    ['reasoning-probe-false', 'reasoning-probe-true'],
    'LocalAI profile verification sends the bounded false/true synthetic pair');
  assert.deepEqual(verificationRecords.map(item => item.reasoningEnabled), [false, true]);
  assert.ok(verificationRecords.every(item => item.authMatchesExpected), 'both probes use the configured credential');
  await cdp.selectId('subtitle-test-stage', 'translation');
  cdp.respondToNextDialog({accept: true});
  await click('subtitle-test-profile');
  await waitUntil(async () => (await fixtureRecords()).length === 4, 'correction and translation verification requests');
  await cdp.until(`document.getElementById('subtitle-settings-status')?.textContent.includes('格式符合')`);
  const bothStageVerificationRecords = await fixtureRecords();
  assert.deepEqual(bothStageVerificationRecords.map(item => [item.stage, item.kind]), [
    ['correction', 'reasoning-probe-false'], ['correction', 'reasoning-probe-true'],
    ['translation', 'reasoning-probe-false'], ['translation', 'reasoning-probe-true'],
  ], 'correction and translation each receive their own bounded false/true verification');
  await cdp.command('Page.reload');
  await cdp.until(`document.querySelector('[data-testid="subtitle-profile-list"]')?.textContent.includes('Fixture Text')`);
  const persistedProfiles = await cdp.evaluate(`fetch('/api/subtitles/profiles').then(response=>response.json())`);
  const persistedVerification = persistedProfiles.profiles.find(item => item.profile_id === 'workflow-text')
    ?.reasoning_verification;
  assert.equal(persistedVerification?.correction?.status, 'supported', 'correction evidence survives page reload');
  assert.equal(persistedVerification?.translation?.status, 'supported', 'translation evidence survives page reload');
  const pageText = await cdp.evaluate('document.body.innerText');
  assert.ok(!pageText.includes('uav-test-secret-never-log'), 'API key must not appear in UI');
  const keyValue = await cdp.evaluate(`document.getElementById('subtitle-profile-key')?.value || ''`);
  assert.equal(keyValue, '', 'saved API key input is cleared');
  const publicProfiles = await cdp.evaluate(`fetch('/api/subtitles/profiles').then(response=>response.text())`);
  assert.ok(!publicProfiles.includes('uav-test-secret-never-log'), 'profile API must not return the saved API key');

  await clearFixture();
  const nativeBaseUrl = fixtureBaseUrl.replace(/\/v1$/, '');
  await saveProfile({id: 'workflow-ollama', name: 'Fixture Ollama', kind: 'text', model: 'fixture-text',
    capabilities: ['correction', 'translation'], provider: 'ollama', baseUrl: nativeBaseUrl});
  await click('subtitle-discover-models');
  await cdp.until(`document.getElementById('subtitle-settings-status')?.textContent.includes('已載入 1 個模型')`);
  await cdp.selectId('subtitle-test-stage', 'correction');
  await cdp.setChecked('subtitle-test-allow-reasoning', false);
  await click('subtitle-test-profile');
  await cdp.until(`document.getElementById('subtitle-settings-status')?.textContent.includes('格式符合')`);
  await saveProfile({id: 'workflow-lmstudio', name: 'Fixture LM Studio', kind: 'text', model: 'fixture-text',
    capabilities: ['correction', 'translation'], provider: 'lmstudio', baseUrl: nativeBaseUrl});
  await click('subtitle-discover-models');
  await cdp.until(`document.getElementById('subtitle-settings-status')?.textContent.includes('已載入 1 個模型')`);
  await click('subtitle-test-profile');
  await cdp.until(`document.getElementById('subtitle-settings-status')?.textContent.includes('格式符合')`);
  const nativeRecords = await fixtureRecords();
  assert.deepEqual(nativeRecords.map(item => item.kind),
    ['discovery', 'ollama-show', 'ollama-correction', 'discovery', 'lmstudio-correction'],
    'browser probes Ollama thinking support and chat, plus LM Studio catalog and native chat routes');
  assert.deepEqual(nativeRecords.map(item => item.path),
    ['/api/tags', '/api/show', '/api/chat', '/api/v0/models', '/api/v1/chat']);
  await cdp.evaluate(`document.getElementById('subtitle-profile-form')?.scrollIntoView({block:'start'})`);
  await cdp.until(`document.getElementById('subtitle-profile-form')?.getBoundingClientRect().top < 120`);
  const settingsCapture = await cdp.command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
  const settingsImagePath = path.join(os.tmpdir(), `uav-subtitle-workflow-settings-${process.pid}.png`);
  fs.writeFileSync(settingsImagePath, Buffer.from(settingsCapture.data, 'base64'), {mode: 0o600});
  screenshots.push(settingsImagePath);
}

async function B2() {
  await click('subtitle-tab');
  await cdp.until(`document.querySelector('[data-testid="subtitle-panel"]') && !document.querySelector('[data-testid="subtitle-panel"]').classList.contains('hidden')`);
  await check('subtitle-videos', 'server-side video list');
  await cdp.until(`document.body.textContent.includes('subtitle-fixture-a.mp4')`);
  const videoSources = await cdp.evaluate(`fetch('/api/subtitles/videos').then(response=>response.json()).then(data=>data.videos.map(video=>({name:video.name,source:video.source,video_id:video.video_id})))`);
  const configuredSource = videoSources.find(video => video.name === 'subtitle-fixture-a.mp4');
  const historySource = videoSources.find(video => video.name === 'subtitle-fixture-history.mp4');
  assert.equal(configuredSource?.source, 'configured', 'configured-root media remains selectable as a configured source');
  assert.equal(historySource?.source, 'history', 'a completed download-history record is listed as a history source');
  const defaults = await cdp.evaluate(`({stages:['vad','asr','correction','translation'].filter(stage=>document.getElementById('subtitle-stage-enabled-'+stage)?.checked),outputs:[...document.querySelectorAll('input[name="subtitle-output-language"]:checked')].map(input=>input.value)})`);
  assert.deepEqual(defaults, {stages: ['vad', 'asr'], outputs: ['ja']},
    'new-run defaults enable VAD and ASR with Japanese output only');
  await setPipeline({allowReasoning: false});
  await selectVideoByName('subtitle-fixture-a.mp4');
  await click('subtitle-submit');
  await cdp.until(`document.getElementById('subtitle-confirm-dialog')?.open`);
  const summary = await cdp.evaluate(`document.getElementById('subtitle-submit-summary')?.textContent || ''`);
  assert.match(summary, /影片：subtitle-fixture-a\.mp4/);
  assert.match(summary, /語音區間.*語音辨識.*字幕校正.*字幕翻譯/);
  await click('subtitle-confirm-submit');
  await click('subtitle-confirm-submit');
  await cdp.until(`document.querySelectorAll('[data-testid^="subtitle-task-subtitle_"]').length === 1`);
  const taskId = (await taskIds())[0];
  await waitTaskStatus(taskId, 'completed');
  const calls = await fixtureRecords();
  const stageCalls = calls.filter(item => ['vad', 'asr', 'correction', 'translation'].includes(item.kind));
  assert.deepEqual(stageCalls.map(item => item.kind),
    ['vad', 'asr', 'correction', 'translation'], 'submitted full pipeline reaches each configured fixture stage');
  assert.ok(stageCalls.filter(item => ['correction', 'translation'].includes(item.kind))
    .every(item => item.reasoningEnabled === false), 'normal correction and translation requests keep reasoning disabled');
  await clearVideoSelection();
  await setPipeline({stages: ['vad']});
  const historyVideoId = await selectVideoByName('subtitle-fixture-history.mp4');
  await submitSelected();
  await cdp.until(`document.querySelectorAll('[data-testid^="subtitle-task-subtitle_"]').length === 2`);
  const historyTaskId = (await appTaskIds()).find(id => id !== taskId);
  await waitTaskStatus(historyTaskId, 'completed');
  const historyTask = await cdp.evaluate(`window.subtitleTasks.state.tasks.find(item=>item.task_id===${JSON.stringify(historyTaskId)})`);
  assert.equal(historyTask.video_id, historyVideoId, 'history selection creates a subtitle task for the selected source');
  assert.equal((await fixtureRecords()).filter(item => ['vad', 'asr', 'correction', 'translation'].includes(item.kind)).length, 5,
    'history selection runs only its requested subtitle stage after the configured-root task');
  const downloadTasks = await cdp.evaluate(`fetch('/api/download/tasks').then(response=>response.json())`);
  assert.equal(downloadTasks.tasks?.length || 0, 0, 'subtitle selection does not create a download task');
  videoIds['subtitle-fixture-a.mp4'] = configuredSource.video_id;
  selectedVideoId = configuredSource.video_id;
}

async function legacyStartupMigration() {
  await click('subtitle-tab');
  await cdp.until(`document.querySelector('[data-testid="subtitle-profile-list"]')?.textContent.includes('Legacy openai-compatible')`);
  await cdp.evaluate(`(()=>{const row=[...document.querySelectorAll('#subtitle-profile-list > div')].find(item=>item.textContent.includes('Legacy openai-compatible'));row?.querySelector('button')?.click();})()`);
  const editor = await cdp.evaluate(`({id:document.getElementById('subtitle-profile-id')?.value||'',model:document.getElementById('subtitle-profile-model')?.value||'',parameters:JSON.parse(document.getElementById('subtitle-profile-parameters')?.value||'{}')})`);
  assert.deepEqual(editor, {id: 'legacy-text-openai_compatible', model: 'legacy-text', parameters: {quality: 'balanced'}},
    'the browser can select and inspect the migrated legacy text profile');
  await cdp.selectId('subtitle-test-stage', 'correction');
  await cdp.setChecked('subtitle-test-allow-reasoning', true);
  cdp.respondToNextDialog({accept: true});
  await click('subtitle-test-profile');
  await cdp.until(`document.getElementById('subtitle-settings-status')?.textContent.includes('格式符合')`);
  const migrationProbeRecords = await fixtureRecords();
  assert.equal(migrationProbeRecords.length, 1, 'the migrated profile performs one real correction probe');
  assert.equal(migrationProbeRecords[0].kind, 'correction');
  assert.equal(migrationProbeRecords[0].maxTokenField, 'max_tokens');
  assert.equal(migrationProbeRecords[0].qualitySent, false,
    'legacy quality metadata is preserved without sending an unsupported provider field');
  const profilesResponse = await fetch(new URL('/api/subtitles/profiles', runtime.url));
  assert.equal(profilesResponse.status, 200, 'legacy startup profile migration is exposed through the browser API');
  const profiles = (await profilesResponse.json()).profiles;
  assert.ok(profiles.some(profile => profile.profile_id === 'legacy-asr' && profile.kind === 'asr'),
    'legacy ASR settings become a selectable subtitle profile');
  const migratedText = profiles.find(profile => profile.profile_id === 'legacy-text-openai_compatible');
  assert.ok(migratedText && migratedText.kind === 'text' && migratedText.model === 'legacy-text',
    'legacy text settings become a selectable subtitle profile');
  assert.deepEqual(migratedText.parameters, {quality: 'balanced'},
    'legacy text quality is retained in the migrated profile');
  assert.equal(fs.readFileSync(legacyFixture.legacyAsrPath, 'utf8'), legacyFixture.legacyAsr,
    'migration preserves the original ASR settings bytes');
  assert.equal(fs.readFileSync(legacyFixture.legacyTranslationPath, 'utf8'), legacyFixture.legacyTranslation,
    'migration preserves the original translation settings bytes');
  const probe = await fetch(new URL('/api/subtitles/profiles/test', runtime.url), {method: 'POST',
    headers: {'content-type': 'application/json'}, body: JSON.stringify({action: 'test', profile_id: migratedText.profile_id,
      stage: 'correction', confirm_external_call: true, allow_reasoning: true})}).then(response => response.json());
  assert.equal(probe.success, true, 'the migrated legacy profile passes its correction probe');
  assert.equal(probe.format_pass, true);
  assert.equal((await fixtureRecords()).length, 2, 'the API probe also reaches the real model fixture');
  await cdp.setChecked('subtitle-test-allow-reasoning', false);
  scenarioDetails.Migration = {legacyFilesPreserved: true, migratedProfiles: ['legacy-asr', migratedText.profile_id],
    legacyTextProbe: 'passed correction probe and parsed fixture response', requests: migrationProbeRecords.length + 1,
    legacyQualitySent: false};
}

async function B3() {
  await waitTaskStatus((await taskIds())[0], 'completed');
  await clickId('subtitle-refresh');
  await assertStages(['vad', 'asr', 'correction', 'translation']);
  await selectResults(selectedVideoId);
  const firstResult = await resultRow('translation');
  await cdp.evaluate(`document.querySelector('[data-testid="${firstResult}"] button')?.click()`);
  await cdp.until(`document.querySelector('[data-testid="subtitle-preview"]')?.textContent.includes('zh-TW:')`);
  await check('subtitle-preview', 'SRT preview');
  const snapshotText = await cdp.evaluate(`document.querySelector('[data-testid="${firstResult}"]')?.textContent || ''`);
  assert.match(snapshotText, /不可變模型快照：Fixture Text · localai \/ fixture-text/, 'result row identifies the friendly profile, provider, and model');
  await cdp.evaluate(`document.querySelector('[data-testid="${firstResult}"] details summary')?.click()`);
  const expandedSnapshot = await cdp.evaluate(`document.querySelector('[data-testid="${firstResult}"] details[open]')?.textContent || ''`);
  assert.match(expandedSnapshot, /模型：fixture-text/, 'expanded saved snapshot exposes the model id');
  assert.match(expandedSnapshot, /參數：\{/, 'expanded saved snapshot exposes the saved parameters');
  await saveProfile({id: 'workflow-text', name: 'Fixture Text Updated', kind: 'text', model: 'fixture-text-updated', capabilities: ['correction', 'translation'], key: 'uav-test-secret-never-log'});
  const afterProfileEdit = await cdp.evaluate(`document.querySelector('[data-testid="${firstResult}"] details[open]')?.textContent || ''`);
  assert.match(afterProfileEdit, /模型：fixture-text/, 'editing a profile leaves the completed result snapshot immutable');
  assert.doesNotMatch(afterProfileEdit, /fixture-text-updated/, 'old result does not adopt the edited profile model');
  await cdp.evaluate(`document.querySelector('[data-testid="${firstResult}"] details summary')?.click()`);
  await cdp.until(`!document.querySelector('[data-testid="${firstResult}"] details[open]')`);

  await clearFixture();
  await setPipeline({stages: ['vad', 'asr', 'translation'], outputs: ['en'], target: ['en']});
  const selectedLanguage = await cdp.evaluate(`({target:[...document.querySelectorAll('input[name="subtitle-target-language"]:checked')].map(item=>item.value),outputs:[...document.querySelectorAll('input[name="subtitle-output-language"]:checked')].map(item=>item.value)})`);
  assert.deepEqual(selectedLanguage, {target: ['en'], outputs: ['en']}, 'English is the only selected target and output language');
  await clearVideoSelection();
  await selectVideoByName('subtitle-fixture-b.mp4');
  const beforeIds = await appTaskIds();
  await submitSelected({doubleClick: true});
  await cdp.until(`window.subtitleTasks.state.tasks.length === ${beforeIds.length + 1}`);
  const taskId = (await appTaskIds()).find(id => !beforeIds.includes(id));
  await waitTaskStatus(taskId, 'completed');
  const directTranslation = await fixtureRecords();
  assert.deepEqual(directTranslation.map(item => item.kind), ['vad', 'asr', 'translation'],
    'translation without correction skips the correction stage');
  const translation = directTranslation.find(item => item.kind === 'translation');
  assert.equal(translation.language, 'en');
  assert.equal(translation.inputFromCorrection, false,
    'translation receives uncorrected ASR cues when correction is disabled');
  const englishOutput = path.join(downloadsDir, 'subtitle-fixture-b.en.srt');
  assert.ok(fs.existsSync(englishOutput), 'English-only output publishes one English SRT');
  assert.match(fs.readFileSync(englishOutput, 'utf8'), /en:こんにちは/);
  assert.equal(fs.existsSync(path.join(downloadsDir, 'subtitle-fixture-b.zh-TW.srt')), false,
    'unselected Traditional Chinese output is not published');
  assert.equal(fs.existsSync(path.join(downloadsDir, 'subtitle-fixture-b.ja.srt')), false,
    'source-language Japanese output is not published');
}

async function B4() {
  const retryEvidence = {};
  await clearFixture();
  await clearVideoSelection();
  const initialCount = (await taskIds()).length;
  await fixture.control({delayMs: 5000});
  await setPipeline({stages: ['vad'], outputs: [], target: []});
  await selectVideoByName('subtitle-fixture-b.mp4');
  await selectVideoByName('subtitle-fixture-c.mp4');
  await selectVideoByName('subtitle-fixture-d.mp4');
  await submitSelected();
  await cdp.until(`document.querySelectorAll('[data-testid^="subtitle-task-subtitle_"]').length === ${initialCount + 3}`);
  const beforeIds = await appTaskIds();
  const batchIds = beforeIds.slice(initialCount);
  assert.equal(batchIds.length, 3, 'batch selection creates three independent subtitle tasks');
  await waitUntil(async () => {
    const rendered = await cdp.evaluate(`document.querySelector('[data-testid="subtitle-task-list"]')?.innerText || ''`);
    return rendered.includes('處理中') && (rendered.match(/排隊中/g) || []).length >= 2 ? rendered : false;
  }, 'visible running task and queued siblings', 10000);
  await cdp.selectId('subtitle-stage-profile-vad', 'workflow-vad-alt');
  const unstartedId = await cdp.evaluate(`window.subtitleTasks.state.tasks.find(item=>item.status==='queued'&&!item.ever_started&&!item.inputs?.length)?.task_id`);
  assert.ok(unstartedId, 'a queued sibling has not started and can receive new settings');
  await click(`subtitle-action-settings-${unstartedId}`);
  await cdp.until(`document.querySelector('[data-testid="subtitle-task-${unstartedId}"]')?.textContent.includes('Fixture VAD Alternate')`);
  const appliedSnapshot = await cdp.evaluate(`document.querySelector('[data-testid="subtitle-task-${unstartedId}"]')?.innerText || ''`);
  assert.match(appliedSnapshot, /已保存階段設定：語音區間 · localai \/ fixture-vad-alt/,
    'apply-only-unstarted updates the visible stage/provider/model snapshot');
  await cdp.evaluate(`document.querySelector('[data-testid="subtitle-task-${unstartedId}"] details summary')?.click()`);
  const expandedTaskSnapshot = await cdp.evaluate(`document.querySelector('[data-testid="subtitle-task-${unstartedId}"] details[open]')?.textContent || ''`);
  assert.match(expandedTaskSnapshot, /顯示名稱：Fixture VAD Alternate/,
    'expanded queued task snapshot identifies the applied saved profile');
  await cdp.evaluate(`document.querySelector('[data-testid="subtitle-task-${unstartedId}"] details summary')?.click()`);
  await cdp.until(`!document.querySelector('[data-testid="subtitle-task-${unstartedId}"] details[open]')`);
  const queueBefore = await cdp.evaluate(`window.subtitleTasks.state.tasks.filter(item=>item.status==='queued').map(item=>item.task_id)`);
  assert.ok(queueBefore.length >= 2, 'at least two queued tasks expose a real reorder opportunity');
  const reorderId = queueBefore[0];
  await click(`subtitle-action-down-${reorderId}`);
  await cdp.until(`window.subtitleTasks.state.tasks.filter(item=>item.status==='queued').map(item=>item.task_id).indexOf(${JSON.stringify(reorderId)}) > 0`);
  assert.notDeepEqual(await cdp.evaluate(`window.subtitleTasks.state.tasks.filter(item=>item.status==='queued').map(item=>item.task_id)`),
    queueBefore, 'moving a queued task down changes the visible task order');
  const runningId = await cdp.evaluate(`window.subtitleTasks.state.tasks.find(item=>item.status==='running')?.task_id`);
  assert.ok(runningId, `one batch task is executing while siblings remain queued: ${JSON.stringify(await cdp.evaluate(`({tasks:window.subtitleTasks.state.tasks.map(item=>({task_id:item.task_id,status:item.status,current_stage:item.current_stage})),ui:document.getElementById('subtitle-task-list')?.innerText||''})`))}`);
  const cancelTestId = `subtitle-action-cancel-${runningId}`;
  await cdp.evaluate(`document.querySelector('[data-testid="${cancelTestId}"]')?.focus()`);
  const liveBeforeFocusPoll = await cdp.evaluate(`(() => {const row=document.querySelector('[data-testid="subtitle-task-${runningId}"]');return {title:row?.querySelector('[data-task-live-title]')?.textContent||'',progress:row?.querySelector('[data-task-live-progress]')?.textContent||''};})()`);
  const liveAfterFocusPoll = await waitUntil(async () => cdp.evaluate(`(() => {const row=document.querySelector('[data-testid="subtitle-task-${runningId}"]');const title=row?.querySelector('[data-task-live-title]')?.textContent||'',progress=row?.querySelector('[data-task-live-progress]')?.textContent||'';return document.activeElement?.dataset.testid===${JSON.stringify(cancelTestId)} && (title!==${JSON.stringify(liveBeforeFocusPoll.title)} || progress!==${JSON.stringify(liveBeforeFocusPoll.progress)}) ? {title,progress}:false;})()`),
    'focused task row receives a visible status or progress update without losing action focus', 20000);
  assert.equal(await cdp.evaluate('document.activeElement?.dataset.testid'), cancelTestId,
    `focused task action remains focused after visible update: ${JSON.stringify(liveAfterFocusPoll)}`);
  await waitTaskStatus(runningId, 'completed');
  await cdp.until(`document.querySelector('[data-testid="subtitle-task-${runningId}"] [data-task-live-title]')?.textContent.includes('已完成')`);
  assert.equal(await cdp.evaluate('document.activeElement?.dataset.testid'), cancelTestId,
    'focus remains on the original row action after the delayed task completes');
  await cdp.evaluate(`document.querySelector('[data-testid="subtitle-refresh"]')?.focus()`);
  const cancelledId = await waitUntil(async () => cdp.evaluate(`window.subtitleTasks.state.tasks.find(item=>item.status==='running')?.task_id||false`),
    'a currently running batch task is available for an independent cancel action', 20000);
  await cdp.until(`document.querySelector('[data-testid="subtitle-task-${cancelledId}"] [data-testid="subtitle-action-cancel-${cancelledId}"]')`);
  await click(`subtitle-action-cancel-${cancelledId}`);
  await waitTaskStatus(cancelledId, 'cancelled');
  for (const siblingId of batchIds.filter(id => id !== cancelledId)) await waitTaskStatus(siblingId, 'completed');
  const networkCount = urlPart => cdp.eventLog.filter(item => item.method === 'Network.responseReceived'
    && (() => { try { return new URL(item.params.response?.url).pathname === urlPart; } catch { return false; } })()).length;
  const tasksBeforeManualRefresh = networkCount('/api/subtitles/tasks');
  const videosBeforeManualRefresh = networkCount('/api/subtitles/videos');
  await clickId('subtitle-refresh');
  await check('subtitle-task-list', 'independent subtitle queue');
  await waitUntil(async () => networkCount('/api/subtitles/tasks') > tasksBeforeManualRefresh,
    'manual queue refresh network response', 10000);
  await waitUntil(async () => networkCount('/api/subtitles/videos') > videosBeforeManualRefresh,
    'manual video inventory refresh network response', 10000);
  const videosAfterManualRefresh = networkCount('/api/subtitles/videos');
  const tasksAfterManualRefresh = networkCount('/api/subtitles/tasks');
  await waitUntil(async () => networkCount('/api/subtitles/tasks') > tasksAfterManualRefresh,
    'periodic background task refresh', 10000);
  assert.equal(networkCount('/api/subtitles/videos'), videosAfterManualRefresh,
    'periodic queue polling does not reload the video inventory');
  const completedModels = (await fixtureRecords()).filter(item => item.kind === 'vad').map(item => item.model);
  assert.ok(completedModels.includes('fixture-vad-alt'), 'the explicitly updated unstarted task uses its new model snapshot');
  assert.ok(completedModels.includes('fixture-vad'), 'started task keeps its original immutable model snapshot');

  await fixture.reset();
  await fixture.control({failPath: '/v1/vad', failStatus: 503});
  await clearVideoSelection();
  const beforeFailureIds = await appTaskIds();
  await setPipeline({stages: ['vad'], outputs: [], target: []});
  await selectVideoByName('subtitle-fixture-a.mp4');
  await submitSelected();
  await cdp.until(`window.subtitleTasks.state.tasks.some(item=>!${JSON.stringify(beforeFailureIds)}.includes(item.task_id))`);
  const failedTaskId = (await appTaskIds()).find(id => !beforeFailureIds.includes(id));
  await cdp.until(`document.querySelector('[data-testid="subtitle-task-${failedTaskId}"]')`);
  await waitTaskStatus(failedTaskId, 'failed');
  const initialPermanentFailures = (await fixtureRecords()).filter(item => item.kind === 'vad' && item.status === 503).length;
  assert.equal(initialPermanentFailures, 3, 'permanent 503 response is retried at most twice before the task pauses in failed state');
  const resumeTestId = `subtitle-action-resume-${failedTaskId}`;
  const retryAction = await cdp.selector(resumeTestId);
  assert.equal(retryAction.disabled, false, 'permanent model failure exposes an enabled explicit retry');
  const failedCount = (await fixtureRecords()).filter(item => item.kind === 'vad' && item.status === 503).length;
  await click(resumeTestId);
  await waitUntil(async () => {
    const retries = (await fixtureRecords()).filter(item => item.kind === 'vad' && item.status === 503).length;
    return retries > failedCount ? retries : false;
  }, 'explicit retry reaches the failing VAD fixture', 60000);
  await waitTaskStatus(failedTaskId, 'failed');
  assert.equal((await fixtureRecords()).filter(item => item.kind === 'vad' && item.status === 503).length, failedCount + 1,
    'explicit resume allows one final bounded retry without duplicating successful siblings');
  retryEvidence.retry503 = {initialAttempts: initialPermanentFailures, explicitResumeAttempts: 1,
    observedCalls: (await fixtureRecords()).filter(item => item.kind === 'vad').map(item => ({kind: item.kind, model: item.model, status: item.status}))};

  await fixture.reset();
  await fixture.control({failPath: '/v1/vad', failStatus: 503});
  await clearVideoSelection();
  const beforeRetryCancelIds = await appTaskIds();
  await setPipeline({stages: ['vad'], outputs: [], target: []});
  await selectVideoByName('subtitle-fixture-d.mp4');
  await submitSelected();
  await cdp.until(`window.subtitleTasks.state.tasks.length === ${beforeRetryCancelIds.length + 1}`);
  const retryCancelId = (await appTaskIds()).find(id => !beforeRetryCancelIds.includes(id));
  await waitTaskStatus(retryCancelId, 'retry_wait');
  const retryCancelTestId = `subtitle-action-cancel-${retryCancelId}`;
  assert.equal((await cdp.selector(retryCancelTestId)).hidden, false,
    'retry_wait exposes an enabled cancellation action');
  await click(retryCancelTestId);
  await waitTaskStatus(retryCancelId, 'cancelled');
  await new Promise(resolve => setTimeout(resolve, 1200));
  const cancelledRetryRequests = (await fixtureRecords()).filter(item => item.kind === 'vad');
  assert.equal(cancelledRetryRequests.length, 1,
    'cancelling during the bounded retry wait prevents the next upstream request');
  assert.equal(cancelledRetryRequests[0].status, 503);
  retryEvidence.retryWaitCancel = {status: 'cancelled', observedRequests: cancelledRetryRequests.length};

  await fixture.reset();
  await fixture.control({failPath: '/v1/vad', failStatus: 401});
  await clearVideoSelection();
  const before401Ids = await appTaskIds();
  await setPipeline({stages: ['vad'], outputs: [], target: []});
  await selectVideoByName('subtitle-fixture-c.mp4');
  await submitSelected();
  await cdp.until(`window.subtitleTasks.state.tasks.length === ${before401Ids.length + 1}`);
  const unauthorizedTaskId = (await appTaskIds()).find(id => !before401Ids.includes(id));
  await waitTaskStatus(unauthorizedTaskId, 'failed');
  const unauthorizedCalls = await fixtureRecords();
  assert.deepEqual(unauthorizedCalls.map(item => ({kind: item.kind, status: item.status})), [{kind: 'vad', status: 401}],
    '401 stops without retry or provider fallback');
  retryEvidence.unauthorized401 = unauthorizedCalls.map(item => ({kind: item.kind, model: item.model, status: item.status}));

  await fixture.reset();
  await fixture.control({vad: 'two-chunks', delayPath: '/audio/transcriptions', delayAfterCount: 2, delayMs: 30000});
  await clearVideoSelection();
  await setPipeline({stages: ['vad', 'asr'], outputs: ['ja'], target: []});
  await selectVideoByName('subtitle-fixture-long.mp4');
  const beforeRestartIds = await appTaskIds();
  await submitSelected();
  await cdp.until(`window.subtitleTasks.state.tasks.length === ${beforeRestartIds.length + 1}`);
  const interruptedTaskId = (await appTaskIds()).find(id => !beforeRestartIds.includes(id));
  await waitUntil(async () => {
    const requests = await fixtureRecords();
    return requests.filter(item => item.kind === 'vad').length === 2
      && requests.filter(item => item.kind === 'asr').length === 2 ? requests : false;
  }, 'checkpointed task completes the first ASR chunk and reaches the held second chunk', 60000);
  const beforeRestartRequests = await fixtureRecords();
  assert.equal(beforeRestartRequests.filter(item => item.kind === 'vad').length, 2,
    '35-second fixture records two VAD requests before the two ASR chunks');
  assert.equal(beforeRestartRequests.filter(item => item.kind === 'asr').length, 2,
    'one ASR chunk finishes and the second is held before the service restart');
  scenarioDetails.B4 = {...retryEvidence, beforeRestartRequests};
  await restartRuntime();
  await click('subtitle-tab');
  await cdp.until(`document.querySelector('[data-testid="subtitle-task-${interruptedTaskId}"]')?.textContent.includes('等待手動續跑')`);
  const resumeButton = await cdp.selector(`subtitle-action-resume-${interruptedTaskId}`);
  assert.equal(resumeButton.hidden, false, 'service restart exposes an explicit manual resume action');
  await fixture.reset();
  await click(`subtitle-action-resume-${interruptedTaskId}`);
  await waitTaskStatus(interruptedTaskId, 'completed');
  const resumedRequests = await fixtureRecords();
  assert.deepEqual(resumedRequests.map(item => item.kind), ['asr'],
    'manual resume reuses the completed VAD and first ASR chunk checkpoints');
  scenarioDetails.B4.resumedRequests = resumedRequests;
}

async function B5() {
  await clearFixture();
  await setPipeline({stages: ['vad', 'asr', 'correction', 'translation'], outputs: ['zh-TW'], target: ['zh-TW'], allowReasoning: true});
  await selectResults(videoIds['subtitle-fixture-a.mp4']);
  const beforeIds = await appTaskIds();
  const originalTask = await cdp.evaluate(`window.subtitleTasks.state.tasks.find(item=>item.video_id===${JSON.stringify(videoIds['subtitle-fixture-a.mp4'])}&&item.status==='completed')?.task_id`);
  assert.ok(originalTask, 'redo source task has completed');
  await cdp.evaluate(`document.querySelector('[data-testid="subtitle-action-redo-${originalTask}"]')?.focus()`);
  await click(`subtitle-action-redo-${originalTask}`);
  await cdp.until(`document.getElementById('subtitle-redo-dialog')?.open`);
  await cdp.evaluate(`document.getElementById('subtitle-redo-stage')?.focus()`);
  await cdp.evaluate(`window.subtitleTasks.refresh().then(()=>true)`);
  await cdp.until(`document.getElementById('subtitle-redo-dialog')?.open && document.activeElement?.id==='subtitle-redo-stage'`);
  await clickId('subtitle-redo-cancel');
  await cdp.until(`!document.getElementById('subtitle-redo-dialog')?.open`);
  await cdp.until(`document.activeElement?.dataset.testid === ${JSON.stringify(`subtitle-action-redo-${originalTask}`)}`);
  const restoredFocus = await cdp.evaluate(`document.activeElement?.dataset.testid || document.activeElement?.id || ''`);
  assert.equal(restoredFocus, `subtitle-action-redo-${originalTask}`, 'closing redo restores keyboard focus to its trigger after background refresh');
  await click(`subtitle-action-redo-${originalTask}`);
  await cdp.until(`document.getElementById('subtitle-redo-dialog')?.open`);
  await cdp.selectId('subtitle-redo-stage', 'correction');
  await cdp.until(`document.getElementById('subtitle-redo-profile')?.options.length > 0 && document.getElementById('subtitle-redo-input')?.options.length > 1`);
  await cdp.selectId('subtitle-redo-profile', 'workflow-text');
  const inputResult = await cdp.evaluate(`document.getElementById('subtitle-redo-input')?.options[1]?.value`);
  assert.ok(inputResult, 'correction redo requires a specific upstream version');
  const missingInputDisabled = await cdp.evaluate(`document.getElementById('subtitle-redo-confirm')?.disabled`);
  assert.equal(missingInputDisabled, true, 'redo stays disabled until an upstream version is explicitly selected');
  assert.deepEqual(await fixtureRecords(), [], 'missing explicit upstream selection does not call the model');
  await cdp.selectId('subtitle-redo-input', inputResult);
  try { await cdp.until(`document.getElementById('subtitle-redo-dialog')?.open && document.getElementById('subtitle-redo-confirm')?.disabled===false`, 5000); }
  catch (error) {
    const controls = await cdp.evaluate(`({dialogOpen:document.getElementById('subtitle-redo-dialog')?.open||false,stage:document.getElementById('subtitle-redo-stage')?.value||'',profile:document.getElementById('subtitle-redo-profile')?.value||'',input:document.getElementById('subtitle-redo-input')?.value||'',inputResult:${JSON.stringify(inputResult)},confirmDisabled:document.getElementById('subtitle-redo-confirm')?.disabled??null,warning:document.getElementById('subtitle-redo-warning')?.textContent||''})`);
    throw new Error(`${error.message}; redo controls=${JSON.stringify(controls)}`);
  }
  let redoFailureEvidence;
  await cdp.evaluate(`document.getElementById('subtitle-redo-confirm')?.focus()`);
  const redoConfirmFocus = await cdp.evaluate(`({activeId:document.activeElement?.id||'',disabled:document.getElementById('subtitle-redo-confirm')?.disabled||false,profile:document.getElementById('subtitle-redo-profile')?.value||'',input:document.getElementById('subtitle-redo-input')?.value||''})`);
  assert.equal(redoConfirmFocus.activeId, 'subtitle-redo-confirm', `redo confirmation can be focused for keyboard activation: ${JSON.stringify(redoConfirmFocus)}`);
  await cdp.command('Network.emulateNetworkConditions', {offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1});
  await pressEnter();
  await cdp.until(`document.getElementById('subtitle-redo-error')?.textContent.trim()`);
  const redoError = await cdp.evaluate(`document.getElementById('subtitle-redo-error')?.textContent || ''`);
  assert.ok(redoError, 'offline redo failure is shown inside the redo dialog');
  await cdp.command('Network.emulateNetworkConditions', {offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1});
  await cdp.until(`document.getElementById('subtitle-redo-dialog')?.open && document.activeElement?.id==='subtitle-redo-confirm'`);
  const redoRetryFocus = await cdp.evaluate(`({dialogOpen:document.getElementById('subtitle-redo-dialog')?.open||false,activeId:document.activeElement?.id||'',confirmDisabled:document.getElementById('subtitle-redo-confirm')?.disabled||false,error:document.getElementById('subtitle-redo-error')?.textContent||''})`);
  assert.equal(redoRetryFocus.activeId, 'subtitle-redo-confirm', `failed keyboard redo restores focus for retry: ${JSON.stringify(redoRetryFocus)}`);
  redoFailureEvidence = {error: redoRetryFocus.error, focusAfterFailure: redoRetryFocus.activeId, confirmEnabled: !redoRetryFocus.confirmDisabled};
  await pressEnter();
  await cdp.until(`!document.getElementById('subtitle-redo-dialog')?.open`);
  await cdp.until(`window.subtitleTasks.state.tasks.length > ${beforeIds.length}`);
  const newTaskId = (await appTaskIds()).find(id => !beforeIds.includes(id));
  await waitTaskStatus(newTaskId, 'completed');
  await clickId('subtitle-refresh');
  assert.equal((await fixtureRecords()).filter(item => item.kind === 'correction').length, 1,
    'correction-only redo calls the selected model stage');
  assert.equal((await fixtureRecords()).some(item => item.kind === 'translation'), false,
    'redo does not implicitly rerun downstream translation');
  const correctionRecords = await fixtureRecords();
  const correctionInputId = inputResult;
  assert.equal((await cdp.evaluate(`document.querySelector('[data-testid="${await resultRow('translation')}"] h3')?.textContent || ''`)).includes('上游版本已更新'),
    true, 'correction redo marks downstream translation output stale without automatically rerunning it');

  await clearFixture();
  const englishRedo = await redoOnlyStage(originalTask, 'translation', 'workflow-text', 'asr', 'en');
  const englishRedoCalls = await fixtureRecords();
  assert.deepEqual(englishRedoCalls.map(item => item.kind), ['translation']);
  assert.equal(englishRedoCalls[0].language, 'en', 'English result is created for the same video before Traditional Chinese redo');
  await clickId('subtitle-refresh');
  const englishResult = await cdp.evaluate(`(() => [...document.querySelectorAll('[data-testid="subtitle-results"] [data-testid^="subtitle-result-"]')]
    .find(row => row.querySelector('h3')?.textContent.includes(' · 字幕翻譯 · 英文 · '))?.dataset.testid || null)()`);
  assert.ok(englishResult, 'same-video English translation result is visible');
  await cdp.evaluate(`document.querySelector('[data-testid="${englishResult}"] button')?.click()`);
  await cdp.until(`document.querySelector('[data-testid="subtitle-preview"]')?.textContent.includes('en:')`);
  const englishPreview = await cdp.evaluate(`document.querySelector('[data-testid="subtitle-preview"]')?.textContent || ''`);
  const englishResultText = await cdp.evaluate(`document.querySelector('[data-testid="${englishResult}"]')?.innerText || ''`);

  await clearFixture();
  const traditionalRedo = await redoOnlyStage(originalTask, 'translation', 'workflow-text', 'asr', 'zh-TW');
  const traditionalRedoCalls = await fixtureRecords();
  assert.deepEqual(traditionalRedoCalls.map(item => item.kind), ['translation'],
    'single-language translation redo calls only translation');
  assert.equal(traditionalRedoCalls[0].language, 'zh-TW', 'second redo is Traditional Chinese only');
  await selectResults(videoIds['subtitle-fixture-a.mp4']);
  await cdp.until(`document.querySelector('[data-testid="${englishResult}"]')?.innerText.includes('英文')`);
  assert.equal(await cdp.evaluate(`document.querySelector('[data-testid="${englishResult}"]')?.innerText || ''`), englishResultText,
    'zh-TW-only redo does not alter the prior English result ID or visible row content');
  await cdp.evaluate(`document.querySelector('[data-testid="${englishResult}"] button')?.click()`);
  await cdp.until(`document.querySelector('[data-testid="subtitle-preview"]')?.textContent.includes('en:')`);
  assert.equal(await cdp.evaluate(`document.querySelector('[data-testid="subtitle-preview"]')?.textContent || ''`), englishPreview,
    'English result remains readable with the same content after zh-TW redo');
  await clearFixture();
  const asrRedo = await redoOnlyStage(originalTask, 'asr', 'workflow-asr', 'vad');
  const asrRecords = await fixtureRecords();
  assert.deepEqual(asrRecords.map(item => item.kind), ['asr'],
    'ASR redo consumes its explicitly selected VAD result and does not rerun other stages');
  await clearFixture();
  const vadRedo = await redoOnlyStage(originalTask, 'vad', 'workflow-vad');
  const vadRecords = await fixtureRecords();
  assert.deepEqual(vadRecords.map(item => item.kind), ['vad'],
    'VAD redo reads the source media and does not invoke ASR or text models');
  await selectResults(videoIds['subtitle-fixture-a.mp4']);
  const staleText = await cdp.evaluate(`document.querySelector('[data-testid="subtitle-results"]')?.innerText || ''`);
  assert.match(staleText, /上游版本已更新/, 'later redo leaves downstream versions visible and marked stale');
  const compactCalls = records => records.map(item => ({kind: item.kind, model: item.model, language: item.language ?? null,
    status: item.status, inputFromCorrection: item.inputFromCorrection ?? null}));
  scenarioDetails.B5 = {
    correction: {calls: compactCalls(correctionRecords), selectedInputId: correctionInputId, redoFailureRetry: redoFailureEvidence},
    english: {calls: compactCalls(englishRedoCalls), selectedInputId: englishRedo.inputResultId,
      resultId: englishResult.slice('subtitle-result-'.length), visibleResult: englishResultText},
    zhTW: {calls: compactCalls(traditionalRedoCalls), selectedInputId: traditionalRedo.inputResultId,
      retainedEnglishResultId: englishResult.slice('subtitle-result-'.length)},
    asr: {calls: compactCalls(asrRecords), selectedInputId: asrRedo.inputResultId},
    vad: {calls: compactCalls(vadRecords), selectedInputId: vadRedo.inputResultId},
  };
}

async function B6() {
  await clearFixture();
  await selectResults(videoIds['subtitle-fixture-a.mp4']);
  const translation = await cdp.evaluate(`(() => [...document.querySelectorAll('[data-testid="subtitle-results"] [data-testid^="subtitle-result-"]')]
    .find(row => row.querySelector('h3')?.textContent.includes(' · 字幕翻譯 · 繁體中文 · '))?.dataset.testid || null)()`);
  assert.ok(translation, 'B6 selects a Traditional Chinese translation result compatible with zh-TW publication');
  const translationSelector = `[data-testid="${translation}"]`;
  await cdp.evaluate(`document.querySelector('${translationSelector} button')?.click()`);
  await cdp.until(`document.querySelector('[data-testid="subtitle-download"]')?.href.startsWith('blob:')`);
  await check('subtitle-download', 'download result');
  const before = (await fixtureRecords()).length;
  const previousCorrection = await cdp.evaluate(`(() => [...document.querySelectorAll('[data-testid="subtitle-results"] [data-testid^="subtitle-result-"]')]
    .find(row => row.querySelector('h3')?.textContent.includes(' · 字幕校正 · ') && row.querySelectorAll('button').length >= 3)?.dataset.testid || null)()`);
  assert.ok(previousCorrection, 'previous correction version can be restored');
  await cdp.respondToNextDialog({accept: true, promptText: ''});
  await cdp.evaluate(`document.querySelector('[data-testid="${previousCorrection}"] button:nth-of-type(3)')?.click()`);
  await cdp.until(`document.querySelector('[data-testid="subtitle-error"]')?.textContent.includes('恢復')`);
  assert.equal((await fixtureRecords()).length, before, 'restore is storage-only and makes no model request');
  await clickId('subtitle-refresh');

  const canonical = path.join(downloadsDir, 'subtitle-fixture-a.zh-TW.srt');
  const handEdited = 'human edit must survive automatic publish';
  fs.writeFileSync(canonical, handEdited, 'utf8');
  const row = translationSelector;
  const translationResultId = translation.slice('subtitle-result-'.length);
  const resultSuffix = translationResultId.replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 12);
  const blockedVersionedPath = path.join(downloadsDir, `subtitle-fixture-a.zh-TW.uav-${resultSuffix}.srt`);
  const beforeStorageRetry = (await fixtureRecords()).length;
  fs.chmodSync(downloadsDir, 0o555);
  downloadsReadOnly = true;
  cdp.respondToNextDialog({accept: true, promptText: 'zh-TW'});
  cdp.respondToNextDialog({accept: false});
  const publishEventStart = cdp.eventLog.length;
  try {
    await clickVisibleButtonIn(translation, '發布字幕');
    try { await cdp.until(`document.querySelector('[data-testid="subtitle-error"]')?.textContent.includes('字幕發布失敗')`); }
    catch (error) {
      const diagnostic = await cdp.evaluate(`fetch('/api/subtitles/tasks').then(response=>response.json()).then(data=>({
        uiError:document.querySelector('[data-testid="subtitle-error"]')?.textContent||'',
        publications:(data.publications||[]).map(item=>({video_id:item.video_id,result_id:item.result_id,language:item.language,publish_status:item.publish_status,error:item.error||null})),
        publicationText:document.getElementById('subtitle-publications')?.textContent||''}))`);
      const requests = cdp.eventLog.slice(publishEventStart).filter(item=>item.method==='Network.requestWillBeSent'
        && item.params.request?.url?.includes(`/results/${translationResultId}/action`))
        .map(item=>({method:item.params.request.method,url:item.params.request.url}));
      throw new Error(`${error.message}; publication diagnostic=${JSON.stringify({diagnostic,requests})}`);
    }
  } finally {
    fs.chmodSync(downloadsDir, downloadsOriginalMode);
    downloadsReadOnly = false;
  }
  await cdp.until(`document.getElementById('subtitle-publications')?.textContent.includes('儲存失敗')`);
  const publicationFailureText = await cdp.evaluate(`document.getElementById('subtitle-publications')?.textContent || ''`);
  const retryTestId = await cdp.evaluate(`document.querySelector('[data-testid^="subtitle-publication-retry-"]')?.dataset.testid || null`);
  assert.ok(retryTestId, 'failed publication exposes a storage-only retry in the visible publication history');
  assert.equal((await cdp.selector(retryTestId)).disabled, false, 'storage-only retry is enabled after publication failure');
  const retryPublicationId = retryTestId.slice('subtitle-publication-retry-'.length);
  await click(retryTestId);
  await cdp.until(`window.subtitleTasks.state.publications.find(item=>item.publication_id===${JSON.stringify(retryPublicationId)})?.publish_status==='published'
    && !Object.hasOwn(window.subtitleTasks.state.publications.find(item=>item.publication_id===${JSON.stringify(retryPublicationId)}),'error')`);
  const clearedRetry = await cdp.evaluate(`window.subtitleTasks.state.publications.find(item=>item.publication_id===${JSON.stringify(retryPublicationId)})||null`);
  assert.equal(clearedRetry.publish_status, 'published');
  assert.equal(Object.hasOwn(clearedRetry, 'error'), false,
    'successful publication retry clears the persisted failure from the live UI model');
  assert.equal((await fixtureRecords()).length, beforeStorageRetry,
    'publication retry uses the saved result for storage and makes no model request');
  assert.ok(fs.existsSync(blockedVersionedPath) && fs.statSync(blockedVersionedPath).isFile(),
    'storage-only retry writes the previously blocked versioned SRT');
  scenarioDetails.B6 = {publicationFailure: publicationFailureText, storageRetry: 'published', retryTestId,
    modelCalls: await fixtureRecords(), versionedFile: path.basename(blockedVersionedPath)};

  cdp.respondToNextDialog({accept: true, promptText: 'zh-TW'});
  cdp.respondToNextDialog({accept: false});
  await cdp.evaluate(`document.querySelector('${row} button:nth-of-type(2)')?.click()`);
  await cdp.until(`document.querySelector('[data-testid="subtitle-error"]')?.textContent.includes('字幕已發布')`);
  assert.equal(fs.readFileSync(canonical, 'utf8'), handEdited, 'existing human-edited SRT is never replaced implicitly');
  assert.ok(fs.readdirSync(downloadsDir).some(name => name.includes('.zh-TW.uav-') && name.endsWith('.srt')),
    'safe publication writes a distinct versioned SRT');
  cdp.respondToNextDialog({accept: true, promptText: 'zh-TW'});
  cdp.respondToNextDialog({accept: true});
  await cdp.evaluate(`document.querySelector('${row} button:nth-of-type(2)')?.click()`);
  await waitUntil(async () => fs.existsSync(canonical) && fs.readFileSync(canonical, 'utf8') !== handEdited,
    'explicitly approved replacement writes the selected canonical SRT');
  assert.notEqual(fs.readFileSync(canonical, 'utf8'), handEdited,
    'explicit replacement replaces the selected SRT destination');
  const afterPublish = (await fixtureRecords()).length;
  assert.equal(afterPublish, before, 'publishing or restoring does not call a model');
}

async function B7() {
  await check('subtitle-quality-status', 'honest quality status');
  await clearFixture();
  await clearVideoSelection();
  await fixture.control({asr: 'quality-edge'});
  await setPipeline({stages: ['vad', 'asr'], outputs: [], target: []});
  await selectVideoByName('subtitle-fixture-long.mp4');
  const beforeIds = await appTaskIds();
  await submitSelected();
  await cdp.until(`window.subtitleTasks.state.tasks.length === ${beforeIds.length + 1}`, 10000);
  const newTaskId = (await appTaskIds()).find(id => !beforeIds.includes(id));
  await waitTaskStatus(newTaskId, 'completed');
  const longRecords = await fixtureRecords();
  assert.equal(longRecords.filter(item => item.kind === 'vad').length, 2, '35-second media crosses the 30-second VAD chunk boundary');
  assert.equal(longRecords.filter(item => item.kind === 'asr').length, 2);
  await selectResults(videoIds['subtitle-fixture-long.mp4']);
  const asrRow = await resultRow('asr');
  await cdp.evaluate(`document.querySelector('[data-testid="${asrRow}"] button')?.click()`);
  await cdp.until(`document.querySelector('[data-testid="subtitle-preview"]')?.textContent.trim().length > 0`);
  const srt = await cdp.evaluate(`document.querySelector('[data-testid="subtitle-preview"]')?.textContent || ''`);
  assert.ok(!srt.includes('[laughter]'), `false positive event tokens are removed from SRT: ${JSON.stringify(srt)}`);
  const cueStartSeconds = [...srt.matchAll(/^\d+\n(\d{2}):(\d{2}):(\d{2}),(\d{3}) -->/gm)]
    .map(([, hours, minutes, seconds, milliseconds]) => Number(hours) * 3600 + Number(minutes) * 60 + Number(seconds) + Number(milliseconds) / 1000);
  assert.ok(cueStartSeconds.some(start => Math.abs(start - 29.78) <= 0.02),
    `second-chunk SRT retains fixture-derived offset (29.750s VAD context + 0.030s ASR onset): ${JSON.stringify({cueStartSeconds, srt})}`);
  assert.ok(srt.includes('はい'), `brief low-volume fixture utterance remains in the SRT: ${JSON.stringify(srt)}`);

  await clearFixture();
  await fixture.control({vad: 'empty'});
  await clearVideoSelection();
  await setPipeline({stages: ['vad', 'asr'], outputs: ['ja'], target: []});
  await selectVideoByName('subtitle-fixture-silent.mp4');
  const silentBefore = await taskIds();
  await submitSelected();
  await cdp.until(`document.querySelectorAll('[data-testid^="subtitle-task-subtitle_"]').length === ${silentBefore.length + 1}`, 10000);
  const silentTask = (await taskIds()).find(id => !silentBefore.includes(id));
  await cdp.until(`document.querySelector('[data-testid="subtitle-task-${silentTask}"]')?.textContent.includes('已完成')`, 60000);
  assert.deepEqual((await fixtureRecords()).map(item => item.kind), ['vad'], 'empty VAD response stops before ASR and text stages');
  const silentOutput = path.join(downloadsDir, 'subtitle-fixture-silent.ja.srt');
  assert.ok(fs.existsSync(silentOutput), 'selected Japanese output is materialized for valid silent input');
  const silentSrt = fs.readFileSync(silentOutput, 'utf8');
  assert.equal(silentSrt.trim(), '', 'silent input produces a valid zero-cue empty SRT without invented text');
  assert.equal(fs.existsSync(path.join(downloadsDir, 'subtitle-fixture-silent.en.srt')), false,
    'silent input does not publish unselected English output');
  assert.equal(fs.existsSync(path.join(downloadsDir, 'subtitle-fixture-silent.zh-TW.srt')), false,
    'silent input does not publish unselected Traditional Chinese output');
}

async function B8() {
  try {
    const image = await cdp.command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    const imagePath = path.join(os.tmpdir(), `uav-subtitle-workflow-desktop-${process.pid}.png`);
    fs.writeFileSync(imagePath, Buffer.from(image.data, 'base64'), {mode: 0o600});
    screenshots.push(imagePath);
  } catch {}
  await cdp.command('Emulation.setDeviceMetricsOverride', {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  await click('subtitle-tab');
  await cdp.until(`document.querySelector('[data-testid="subtitle-panel"]') && !document.querySelector('[data-testid="subtitle-panel"]').classList.contains('hidden')`);
  await cdp.evaluate(`document.getElementById('subtitle-profile-name').focus()`);
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Tab', code: 'Tab'});
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Tab', code: 'Tab'});
  const active = await cdp.evaluate('document.activeElement?.id');
  assert.equal(active, 'subtitle-profile-kind', `keyboard Tab moves through profile controls: ${active}`);
  await setPipeline({stages: ['vad'], outputs: [], target: []});
  await clearVideoSelection();
  const keyboardVideo = await cdp.evaluate(`(() => {const item=[...document.querySelectorAll('[data-testid^="subtitle-video-"]')].find(input=>input.closest('label')?.textContent.includes('subtitle-fixture-a.mp4'));return item?{testid:item.dataset.testid,value:item.value}:null;})()`);
  assert.ok(keyboardVideo, 'synthetic fixture video is available for keyboard selection');
  videoIds['subtitle-fixture-a.mp4'] = keyboardVideo.value;
  await cdp.evaluate(`document.querySelector('[data-testid="${keyboardVideo.testid}"]')?.focus()`);
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyDown', key: ' ', code: 'Space'});
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyUp', key: ' ', code: 'Space'});
  assert.equal(await cdp.evaluate(`document.querySelector('[data-testid="${keyboardVideo.testid}"]')?.checked`), true,
    'Space selects a video from the keyboard');
  let keyboardTarget = '';
  for (let index = 0; index < 30; index += 1) {
    await cdp.command('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Tab', code: 'Tab'});
    await cdp.command('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Tab', code: 'Tab'});
    keyboardTarget = await cdp.evaluate('document.activeElement?.id || document.activeElement?.dataset?.testid || ""');
    if (keyboardTarget === 'subtitle-submit') break;
  }
  assert.equal(keyboardTarget, 'subtitle-submit', 'keyboard can move from selected video controls to task submission');
  await cdp.evaluate(`window.__subtitleKeyboardProbe=[];for(const type of ['keydown','keyup'])document.addEventListener(type,event=>window.__subtitleKeyboardProbe.push({type,key:event.key,code:event.code,keyCode:event.keyCode,defaultPrevented:event.defaultPrevented,isTrusted:event.isTrusted,targetId:event.target?.id||''}),true);`);
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
  await cdp.command('Input.dispatchKeyEvent', {type: 'char', key: 'Enter', code: 'Enter', text: '\r', unmodifiedText: '\r', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
  const keyboardSubmit = await cdp.evaluate(`({dialogOpen:document.getElementById('subtitle-confirm-dialog')?.open||false,focused:document.activeElement?.id||'',submitDisabled:document.getElementById('subtitle-submit')?.disabled||false,error:document.getElementById('subtitle-error')?.textContent||'',events:window.__subtitleKeyboardProbe})`);
  assert.ok(keyboardSubmit.dialogOpen, `Enter activates the focused submit button: ${JSON.stringify(keyboardSubmit)}`);
  const beforeIds = await appTaskIds();
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Tab', code: 'Tab'});
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Tab', code: 'Tab'});
  assert.equal(await cdp.evaluate('document.activeElement?.id'), 'subtitle-confirm-submit',
    'Tab moves from the dialog cancel button to keyboard submission');
  await cdp.command('Network.emulateNetworkConditions', {offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1});
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
  await cdp.command('Input.dispatchKeyEvent', {type: 'char', key: 'Enter', code: 'Enter', text: '\r', unmodifiedText: '\r', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
  await cdp.until(`document.getElementById('subtitle-confirm-error')?.textContent.trim()`);
  const submitError = await cdp.evaluate(`document.getElementById('subtitle-confirm-error')?.textContent || ''`);
  assert.ok(submitError, 'offline submission failure is shown inside the open confirmation dialog');
  await cdp.command('Network.emulateNetworkConditions', {offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1});
  await cdp.until(`document.getElementById('subtitle-confirm-dialog')?.open`);
  const failedSubmitFocus = await cdp.evaluate(`({dialogOpen:document.getElementById('subtitle-confirm-dialog')?.open||false,activeId:document.activeElement?.id||'',activeTag:document.activeElement?.tagName||'',confirmDisabled:document.getElementById('subtitle-confirm-submit')?.disabled||false,error:document.getElementById('subtitle-confirm-error')?.textContent||'',events:window.__subtitleKeyboardProbe})`);
  assert.equal(failedSubmitFocus.activeId, 'subtitle-confirm-submit',
    `failed keyboard submission keeps the confirm control focused for retry: ${JSON.stringify(failedSubmitFocus)}`);
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
  await cdp.command('Input.dispatchKeyEvent', {type: 'char', key: 'Enter', code: 'Enter', text: '\r', unmodifiedText: '\r', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
  await cdp.command('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13});
  await cdp.until(`window.subtitleTasks.state.tasks.length > ${beforeIds.length}`);
  const keyboardTaskId = (await appTaskIds()).find(id => !beforeIds.includes(id));
  await waitTaskStatus(keyboardTaskId, 'completed');
  const overflow = await cdp.evaluate('document.documentElement.scrollWidth - window.innerWidth');
  assert.ok(overflow <= 1, `narrow subtitle page should not overflow horizontally: ${overflow}px`);
  const records = await fixtureRecords();
  assert.equal(records.some(item => JSON.stringify(item).includes('uav-test-secret-never-log')), false,
    'fixture metadata must not contain the API key');
  try {
    const image = await cdp.command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    const imagePath = path.join(os.tmpdir(), `uav-subtitle-workflow-mobile-${process.pid}.png`);
    fs.writeFileSync(imagePath, Buffer.from(image.data, 'base64'), {mode: 0o600});
    screenshots.push(imagePath);
  } catch {}
}

const runPromise = (async () => {
  const nodeVersion = process.version;
  const chromeVersion = command(chromeExecutable(), ['--version']);
  fixturePort = await fixture.listen();
  makeMedia();
  const direct = fs.existsSync('/usr/local/bin/uav-web');
  fixtureBaseUrl = direct ? `http://127.0.0.1:${fixturePort}/v1` : `http://host.docker.internal:${fixturePort}/v1`;
  legacyFixture = seedLegacyStartupData(fixtureBaseUrl, direct);
  runtime = await startApp();
  fixtureBaseUrl = runtime.fixtureUrl;
  await waitForApp(runtime.url);
  await startChrome(runtime.url);
  await assertStyledFrontend();
  currentCase = 'LegacyStartupMigration';
  await legacyStartupMigration();
  const migrationRecords = await fixtureRecords();
  evidence.push({case: currentCase, result: 'PASS', fixture: migrationRecords.map(({method, path, kind, model, status}) => ({method, path, kind, model, status})), details: scenarioDetails.Migration});
  const scenarios = [B1, B2, B3, B4, B5, B6, B7, B8];
  for (const scenario of scenarios) {
    currentCase = scenario.name;
    await clearFixture();
    await clearVideoSelection();
    await scenario();
    const records = await fixtureRecords();
    fixtureEvidenceByCase[scenario.name] = records;
    evidence.push({case: scenario.name, result: 'PASS', fixture: records.map(({method, path, kind, model, language, status}) => ({method, path, kind, model, language, status})),
      ...(scenarioDetails[scenario.name] ? {details: scenarioDetails[scenario.name]} : {})});
  }
  testReport = {result: 'PASS', runtime: runtime.mode, nodeVersion, chromeVersion, frontendStyleEvidence,
    daemon: await (await fetch(new URL('/api/status', runtime.url))).json(), cases: evidence, screenshots};
})().catch(async error => {
  testFailure = error;
  if (currentCase) {
    try {
      const records = await fixtureRecords();
      evidence.push({case: currentCase, result: 'FAIL', fixture: records.map(({method, path, kind, model, language, status}) => ({method, path, kind, model, language, status}))});
    } catch {}
  }
  process.exitCode = 1;
});

async function cleanup() {
  if (cleanupPromise) return cleanupPromise;
  cleanupPromise = (async () => {
  if (downloadsReadOnly) {
    try { fs.chmodSync(downloadsDir, downloadsOriginalMode); downloadsReadOnly = false; }
    catch (error) { cleanupErrors.push(`restore download directory permissions: ${error.message}`); }
  }
  try { cdp?.close(); } catch (error) { cleanupErrors.push(`CDP: ${error.message}`); }
  try { await stopChild(chrome, true); } catch (error) { cleanupErrors.push(`Chrome: ${error.message}`); }
  try { await stopChild(app, Boolean(directLaunch)); } catch (error) { cleanupErrors.push(`app: ${error.message}`); }
  if (appContainer) {
    try { appLog = command('docker', ['logs', appContainer], {timeout: 10000}); } catch (error) { cleanupErrors.push(`docker logs: ${error.message}`); }
    try { command('docker', ['rm', '-fv', appContainer]); } catch (error) { cleanupErrors.push(`docker rm: ${error.message}`); }
  }
  try { await fixture.close(); } catch (error) { cleanupErrors.push(`fixture: ${error.message}`); }
  if (imageBuilt && image) {
    try { command('docker', ['image', 'rm', '-f', image]); } catch (error) { cleanupErrors.push(`image rm: ${error.message}`); }
  }
  try { fs.rmSync(root, {recursive: true, force: true}); } catch (error) { cleanupErrors.push(`temp root cleanup: ${error.message}`); }
  if (testFailure) {
    console.error(testFailure);
    const sanitized = value => value.replace(/Bearer\s+\S+/gi, 'Bearer [redacted]')
      .replaceAll('uav-test-secret-never-log', '[redacted]').slice(-8000);
    if (appLog) console.error(`APP_LOG_REDACTED=${sanitized(appLog)}`);
    if (chromeLog) console.error(`CHROME_LOG=${chromeLog.slice(-2000)}`);
    const failurePath = path.join(os.tmpdir(), `uav-subtitle-workflow-failure-${process.pid}.txt`);
    const failureReport = [String(testFailure), `APP_LOG_REDACTED=${sanitized(appLog)}`, `CHROME_LOG=${chromeLog.slice(-2000)}`,
      `FRONTEND_STYLE_STATE=${JSON.stringify(frontendStyleEvidence)}`, `FIXTURE_LOG=${JSON.stringify(evidence)}`].join('\n\n');
    fs.writeFileSync(failurePath, failureReport, {mode: 0o600});
    console.error(`FIXTURE_LOG=${JSON.stringify(evidence)}`);
    console.error(`FAILURE_ARTIFACT=${failurePath}`);
  } else {
    testReport.cleanup = cleanupErrors.length ? cleanupErrors : `Chrome, app, fixture, and temp data removed; ${imageBuilt ? 'temporary image removed' : 'supplied image retained by design'}`;
    const reportPath = path.join(os.tmpdir(), `uav-subtitle-workflow-report-${process.pid}.json`);
    fs.writeFileSync(reportPath, JSON.stringify(testReport, null, 2), {mode: 0o600});
    console.log(JSON.stringify({...testReport, reportPath}));
  }
  if (cleanupErrors.length) {
    process.exitCode = 1;
    console.error(`CLEANUP_ERRORS=${JSON.stringify(cleanupErrors)}`);
  }
  })();
  return cleanupPromise;
}

async function handleSignal(signal) {
  testFailure ||= new Error(`subtitle browser test interrupted by ${signal}`);
  process.exitCode = signal === 'SIGINT' ? 130 : 143;
  if (currentCase) {
    try {
      const records = await fixtureRecords();
      if (!evidence.some(item => item.case === currentCase && item.result === 'FAIL')) {
        evidence.push({case: currentCase, result: 'INTERRUPTED', fixture: records.map(({method, path, kind, model, language, status}) => ({method, path, kind, model, language, status}))});
      }
    } catch {}
  }
  await cleanup();
  process.exit(signal === 'SIGINT' ? 130 : 143);
}
process.once('SIGINT', () => { void handleSignal('SIGINT'); });
process.once('SIGTERM', () => { void handleSignal('SIGTERM'); });
void runPromise.finally(cleanup);
