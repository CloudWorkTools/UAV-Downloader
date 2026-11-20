const assert = require('node:assert/strict');
const {execFileSync, spawn} = require('node:child_process');
const {once} = require('node:events');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {setTimeout: delay} = require('node:timers/promises');
const {Cdp} = require(path.resolve('tests/subtitle_cdp.cjs'));

const frontend = path.resolve('uav-web/frontend');
const languages = ['ja', 'zh-TW'];
const taskId = key => 'subtitle_' + key.repeat(32);
const trackKeys = ['a', 'b'].flatMap(key => languages.map(lang => `${key}/${lang}`));
const captions = {
  'a/zh-TW': '較早結果：早安，今天去散步。',
  'a/ja': '前の結果：おはよう、散歩に行こう。',
  'b/zh-TW': '較新結果：現在要出發了。',
  'b/ja': '新しい結果：そろそろ出かけよう。',
};
const vtt = text => `WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n${text}\n\n`;

function* permutations(keys) {
  if (!keys.length) yield [];
  for (const key of keys) {
    for (const rest of permutations(keys.filter(other => other !== key))) yield [key, ...rest];
  }
}

function serveFixture(media) {
  const videos = [{
    video_id: 'fixture', title: '字幕試播｜合成測試影片', source: 'configured',
    folder: '驗收素材', subtitle_languages: languages,
  }];
  const tasks = ['b', 'a'].map((key, index) => ({
    task_id: taskId(key), video_id: 'fixture', library_translation: true,
    status: 'completed', progress: 100, created_at: 2 - index, trial_seconds: 120,
  }));
  const types = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'};
  return http.createServer((req, res) => {
    const route = new URL(req.url, 'http://127.0.0.1').pathname;
    const send = (type, data) => res.writeHead(200, {'Content-Type': type, 'Cache-Control': 'no-store'}).end(data);
    if (route === '/api/subtitles/videos/fixture/media') {
      const range = /^bytes=(\d+)-(\d*)$/.exec(req.headers.range || '');
      if (!range) return send('video/webm', media);
      const start = Number(range[1]);
      const end = range[2] ? Math.min(Number(range[2]), media.length - 1) : media.length - 1;
      if (start > end) return res.writeHead(416, {'Content-Range': `bytes */${media.length}`}).end();
      return res.writeHead(206, {'Content-Type': 'video/webm', 'Accept-Ranges': 'bytes',
        'Content-Length': end - start + 1, 'Content-Range': `bytes ${start}-${end}/${media.length}`})
        .end(media.subarray(start, end + 1));
    }
    const sidecar = /^\/api\/subtitles\/videos\/fixture\/tracks\/(ja|zh-TW)$/.exec(route);
    if (sidecar) return send('text/vtt; charset=utf-8', vtt(`檔案原有字幕 ${sidecar[1]}`));
    if (route === '/api/subtitles/tasks') return send('application/json', JSON.stringify({tasks}));
    if (route === '/api/subtitles/videos') return send('application/json', JSON.stringify({videos}));
    if (route.startsWith('/api/')) return send('application/json', '{}');
    const asset = route === '/' ? 'index.html' : route.slice(1);
    if (path.basename(asset) === asset && fs.existsSync(path.join(frontend, asset))) {
      return send(types[path.extname(asset)], fs.readFileSync(path.join(frontend, asset)));
    }
    res.writeHead(404).end();
  });
}

const installTrackFixture = `(() => {
  const fetchOriginal = window.fetch;
  const pending = window.captionFixture = {};
  window.fetch = (url, ...options) => {
    const match = /^\\/api\\/subtitles\\/tasks\\/subtitle_([ab])[ab]{31}\\/tracks\\/(ja|zh-TW)$/.exec(String(url));
    if (!match) return fetchOriginal(url, ...options);
    return new Promise(resolve => {
      const key = match[1] + '/' + match[2];
      pending[key] = text => {
        delete pending[key];
        resolve(new Response(JSON.stringify({vtt: (${vtt.toString()})(text)})));
      };
    });
  };
})();`;

const playback = `(() => {
  const video = document.getElementById('subtitle-library-video');
  const selector = document.getElementById('subtitle-library-caption');
  return {
    languages: [...video.textTracks].map(track => track.language).sort(),
    showing: [...video.textTracks].filter(track => track.mode === 'showing').map(track => track.language),
    text: [...video.textTracks].flatMap(track => track.mode === 'showing'
      ? [...(track.activeCues || [])].map(cue => cue.text) : []),
    selected: selector?.value,
    choices: selector ? [...selector.options].map(option => option.value).sort() : null,
    error: document.getElementById('subtitle-library-error').textContent,
  };
})()`;

async function seek(cdp, seconds) {
  await cdp.until(`document.getElementById('subtitle-library-video').readyState >= 2`);
  await cdp.byId('subtitle-library-video', `element.currentTime = ${seconds}`);
  await cdp.until(`(() => {const v=document.getElementById('subtitle-library-video');
    return !v.seeking && Math.abs(v.currentTime - ${seconds}) < 0.02;})()`);
}

async function settle(cdp) {
  await cdp.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
}

async function checkSchedule(cdp, order, evidence, viewport, language) {
  const context = `${viewport}／${language}，較新結果 → 較早結果，送達 ${order.join(' → ')}`;
  await cdp.click(`subtitle-action-results-${taskId('b')}`);
  await cdp.until(`Object.keys(window.captionFixture).length === 2`);
  await cdp.click(`subtitle-action-results-${taskId('a')}`);
  await cdp.until(`Object.keys(window.captionFixture).length === 4`);
  assert.ok(await cdp.evaluate(`Boolean(document.getElementById('subtitle-library-caption')?.getClientRects().length)`),
    `${context}：找不到可操作的字幕語言選單`);
  if (language === 'ja') await cdp.selectId('subtitle-library-caption', language);
  await seek(cdp, 1.5);
  const arrived = new Set();
  const assertChoice = async (language, expectedText) => {
    const current = await cdp.evaluate(playback);
    assert.deepEqual(current.languages, languages, `${context}：每種語言只能有一條字幕軌`);
    assert.deepEqual(current.choices, ['', ...languages], `${context}：字幕選單的選項重複或遺失`);
    assert.deepEqual(current.showing, language ? [language] : [], `${context}：同時顯示多條字幕或顯示語言錯誤`);
    assert.equal(current.selected, language, `${context}：選單與播放器語言不同`);
    assert.deepEqual(current.text, expectedText, `${context}：播放了其他結果或時間不符，已送達 ${[...arrived].join('、')}`);
    assert.equal(current.error, '', `${context}：播放器回報錯誤`);
  };
  const selectedResult = `a/${language}`;
  for (const key of order) {
    await cdp.evaluate(`window.captionFixture[${JSON.stringify(key)}](${JSON.stringify(captions[key])})`);
    arrived.add(key);
    const expected = arrived.has(selectedResult) ? captions[selectedResult] : `檔案原有字幕 ${language}`;
    // A showing track loads asynchronously, so wait until its cue (even a wrong cue) is observable.
    await cdp.until(`(${playback}).text.length > 0`);
    await settle(cdp);
    await assertChoice(language, [expected]);
  }
  if (evidence) {
    await cdp.byId('subtitle-library-player', "element.scrollIntoView({block:'center'})");
    await cdp.evaluate(`(async () => {
      const video = document.getElementById('subtitle-library-video');
      video.muted = true;
      await video.play();
      video.pause();
    })()`);
    await settle(cdp);
    await assertChoice(language, [captions[selectedResult]]);
    const shot = await cdp.command('Page.captureScreenshot', {format: 'png'});
    fs.writeFileSync(path.join(evidence, `${viewport}-zh-TW.png`), Buffer.from(shot.data, 'base64'));
  }
  await seek(cdp, 2.5);
  await settle(cdp);
  await assertChoice(language, []);
  await seek(cdp, 1.5);
  const otherLanguage = language === 'ja' ? 'zh-TW' : 'ja';
  await cdp.selectId('subtitle-library-caption', otherLanguage);
  await cdp.until(`(${playback}).text.length > 0`);
  await assertChoice(otherLanguage, [captions[`a/${otherLanguage}`]]);
  await cdp.selectId('subtitle-library-caption', '');
  await assertChoice('', []);
  await cdp.selectId('subtitle-library-caption', 'zh-TW');
  await cdp.byId('subtitle-library-close', 'element.click()');
}

async function main() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'uav-preview-check-'));
  const evidence = process.env.SUBTITLE_PREVIEW_SCREENSHOTS;
  const chromeBin = process.env.CHROME_BIN || '/usr/bin/google-chrome';
  let server, chrome, cdp, browserError;
  try {
    if (evidence) fs.mkdirSync(evidence, {recursive: true});
    const media = path.join(root, 'preview.webm');
    execFileSync('ffmpeg', ['-loglevel', 'error', '-f', 'lavfi', '-i',
      'color=c=0x222b3a:s=480x270:r=12:d=3', '-c:v', 'libvpx-vp9', media]);
    server = serveFixture(fs.readFileSync(media));
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const url = `http://127.0.0.1:${server.address().port}/`;
    chrome = spawn(chromeBin, ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
      '--disable-background-networking', '--no-first-run', '--remote-debugging-pipe',
      `--user-data-dir=${root}`, 'about:blank'], {stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe']});
    await once(chrome, 'spawn');
    cdp = new Cdp(chrome);
    chrome.stdio[3].on('error', error => {
      browserError ??= new Error('無法與 Chrome 的除錯管線連線', {cause: error});
      cdp.close();
    });
    chrome.once('exit', (code, signal) => {
      browserError ??= new Error(`Chrome 已退出：${signal ?? code}`);
      cdp.close();
    });
    await cdp.start('about:blank');
    await cdp.command('Page.addScriptToEvaluateOnNewDocument', {source: installTrackFixture});
    for (const [viewport, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
      await cdp.command('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: width === 390});
      await cdp.command('Page.navigate', {url: url + '?viewport=' + viewport});
      await cdp.until(`location.search === '?viewport=${viewport}' && document.readyState === 'complete'
        && Boolean(window.captionFixture)`);
      await cdp.until(`getComputedStyle(document.body).backgroundColor === 'rgb(18, 18, 23)'`, 15000);
      await cdp.click('subtitle-tab');
      await cdp.until(`document.querySelector('[data-testid="subtitle-action-results-${taskId('a')}"]')`);
      for (const language of ['zh-TW', 'ja']) {
        let count = 0;
        for (const order of permutations(trackKeys)) {
          try {
            await checkSchedule(cdp, order, language === 'zh-TW' && count === 0 ? evidence : null, viewport, language);
          } catch (error) {
            console.error(`${viewport}／${language}，送達 ${order.join(' → ')}`);
            throw error;
          }
          count++;
        }
        assert.equal(count, 24);
        console.log(`${viewport} ${width}×${height}／${language}：${count} 條回應順序通過`);
      }
    }
    if (evidence) console.log(`畫面證據 ${path.resolve(evidence)}`);
  } catch (error) {
    throw browserError ?? error;
  } finally {
    if (chrome?.pid && chrome.exitCode === null && chrome.signalCode === null) {
      const exited = once(chrome, 'exit');
      cdp?.command('Browser.close', {}, null).catch(() => {});
      const stopped = await Promise.race([exited.then(() => true), delay(2000, false, {ref: false})]);
      if (!stopped) {
        chrome.kill();
        await exited;
      }
    }
    cdp?.close();
    server?.closeAllConnections();
    server?.close();
    fs.rmSync(root, {recursive: true, force: true, maxRetries: 5, retryDelay: 100});
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
