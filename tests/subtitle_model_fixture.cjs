const http = require('node:http');

function createFixture({host = '0.0.0.0'} = {}) {
  const records = [];
  let mode = {vad: 'speech', asr: 'speech', text: 'ok', discovery: 'ok', delayMs: 0, expectedKey: null};
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      const raw = Buffer.concat(chunks);
      const respond = (status, payload) => {
        res.writeHead(status, {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET,POST,OPTIONS', 'Access-Control-Allow-Headers': 'content-type,authorization'});
        res.end(JSON.stringify(payload));
      };
      if (req.method === 'OPTIONS') return respond(204, {});
      if (req.url === '/__records' && req.method === 'GET') return respond(200, {records});
      if (req.url === '/__records' && req.method === 'DELETE') {
        records.length = 0;
        return respond(200, {ok: true});
      }
      if (req.url === '/__control' && req.method === 'POST') {
        try { mode = {...mode, ...JSON.parse(raw.toString('utf8'))}; }
        catch { return respond(400, {error: 'invalid fixture control'}); }
        return respond(200, {ok: true});
      }

      let body = null;
      const contentType = String(req.headers['content-type'] || '');
      if (contentType.includes('application/json')) {
        try { body = JSON.parse(raw.toString('utf8')); } catch {}
      }
      const auth = req.headers.authorization || req.headers['x-api-key'] || '';
      const record = {method: req.method, path: req.url, authPresent: Boolean(auth),
        authMatchesExpected: mode.expectedKey === null ? null : auth === `Bearer ${mode.expectedKey}`,
        status: 200};
      record.requestOrdinal = records.filter(item => item.kind === record.kind).length + 1;
      const model = body && typeof body.model === 'string' ? body.model : null;
      if (model) record.model = model.slice(0, 160);

      let reply;
      let status = 200;
      if (req.method === 'GET' && /\/(v1\/models|api\/tags|api\/v0\/models)$/.test(req.url)) {
        record.kind = 'discovery';
        if (mode.discovery === 'error') { status = 503; reply = {error: 'fixture discovery unavailable'}; }
        else if (req.url.endsWith('/api/v0/models')) reply = {object: 'list', data: [{id: 'fixture-text', type: 'llm'}]};
        else if (req.url.endsWith('/api/tags')) reply = {models: [{name: 'fixture-text'}]};
        else reply = {object: 'list', data: [{id: 'fixture-text'}]};
      } else if (req.method === 'POST' && /\/v1\/vad$/.test(req.url)) {
        record.kind = 'vad';
        record.requestOrdinal = records.filter(item => item.kind === record.kind).length + 1;
        record.sampleCount = Array.isArray(body?.audio) ? body.audio.length : null;
        reply = mode.vad === 'empty' ? {segments: []} : mode.vad === 'two-chunks'
          ? {segments: record.requestOrdinal === 1 ? [{start: 0, end: 2}, {start: 6, end: 8}] : []}
          : {segments: [{start: 0, end: 2}]};
      } else if (req.method === 'POST' && /\/audio\/transcriptions$/.test(req.url)) {
        record.kind = 'asr';
        record.requestOrdinal = records.filter(item => item.kind === record.kind).length + 1;
        record.language = readMultipartField(raw, contentType, 'language');
        record.audioBytes = readMultipartFileBytes(raw, contentType);
        reply = mode.asr === 'empty' ? {segments: []} : mode.asr === 'quality-edge' ? {segments: [
          {start: 0.03, end: 0.16, text: 'はい'},
          {start: 0.4, end: 0.9, text: '[laughter]'},
        ]} : {segments: [
          {start: 0.2, end: 0.8, text: 'こんにちは'},
          {start: 1.0, end: 1.8, text: '確認用の字幕です'},
        ]};
      } else if (req.method === 'POST' && /\/api\/show$/.test(req.url)) {
        record.kind = 'ollama-show';
        reply = {thinking: {values: [false, true]}};
      } else if (req.method === 'POST' && /\/api\/chat$/.test(req.url)) {
        const prompt = body?.messages?.find(item => item.role === 'user')?.content || '';
        const operation = /^Correct transcription/.test(prompt) ? 'correction' : 'translation';
        record.kind = `ollama-${operation}`;
        record.cueCount = parsePromptCues(prompt).length;
        reply = {message: {content: JSON.stringify(parsePromptCues(prompt).map(cue => ({id: cue.id, text: `修正:${cue.text}`})))}};
      } else if (req.method === 'POST' && /\/api\/v1\/chat$/.test(req.url)) {
        const prompt = body?.input || '';
        const operation = /^Correct transcription/.test(prompt) ? 'correction' : 'translation';
        record.kind = `lmstudio-${operation}`;
        record.cueCount = parsePromptCues(prompt).length;
        reply = {model_instance_id: 'fixture-model', output: [{type: 'message', content: JSON.stringify(parsePromptCues(prompt).map(cue => ({id: cue.id, text: `修正:${cue.text}`})))}]};
      } else if (req.method === 'POST' && /\/chat\/completions$/.test(req.url)) {
        const prompt = body?.messages?.find(item => item.role === 'user')?.content || '';
        const target = prompt.match(/Translate faithfully into ([A-Za-z-]+)\./)?.[1] || null;
        const operation = /^Correct transcription/.test(prompt) ? 'correction' : 'translation';
        record.kind = operation;
        record.language = target;
        const cues = parsePromptCues(prompt);
        record.cueCount = cues.length;
        record.inputFromCorrection = cues.some(cue => cue.text.startsWith('修正:'));
        const updated = cues.map(cue => ({id: cue.id, text: operation === 'correction'
          ? `修正:${cue.text}` : `${target || '譯'}:${cue.text}`}));
        reply = {choices: [{message: {content: JSON.stringify(updated)}}]};
      } else {
        status = 404;
        reply = {error: 'unknown fixture route'};
      }

      if (mode.failPath && req.url.includes(mode.failPath)) {
        status = Number(mode.failStatus || 503);
        reply = {error: 'fixture controlled failure'};
      }
      if (mode.malformedPath && req.url.includes(mode.malformedPath)) reply = {malformed: true};
      record.status = status;
      records.push(record);
      if (mode.disconnectPath && req.url.includes(mode.disconnectPath)) return req.socket.destroy();
      if (mode.redirectPath && req.url.includes(mode.redirectPath)) {
        res.writeHead(302, {Location: mode.redirectTo || '/v1/models'});
        return res.end();
      }
      const send = () => respond(status, reply);
      const shouldDelay = Number(mode.delayMs) > 0 && record.kind !== 'discovery'
        && (!mode.delayPath || req.url.includes(mode.delayPath))
        && (!Number(mode.delayAfterCount) || record.requestOrdinal >= Number(mode.delayAfterCount));
      if (shouldDelay) setTimeout(send, Math.min(Number(mode.delayMs), 30000));
      else send();
    });
  });

  return {
    server,
    async listen() {
      await new Promise((resolve, reject) => server.listen(0, host, resolve).once('error', reject));
      return server.address().port;
    },
    async control(value) { return fixtureRequest(`http://127.0.0.1:${server.address().port}/__control`, 'POST', value); },
    async reset() {
      await fixtureRequest(`http://127.0.0.1:${server.address().port}/__records`, 'DELETE');
      mode = {vad: 'speech', asr: 'speech', text: 'ok', discovery: 'ok', delayMs: 0, expectedKey: null};
    },
    async snapshot() { return (await fixtureRequest(`http://127.0.0.1:${server.address().port}/__records`)).records; },
    close() {
      return new Promise(resolve => {
        server.close(resolve);
        server.closeAllConnections?.();
      });
    },
  };
}

function parsePromptCues(prompt) {
  const text = String(prompt);
  const offset = text.lastIndexOf('Input: ');
  const candidate = text.slice(offset >= 0 ? offset + 'Input: '.length : text.lastIndexOf('\n') + 1);
  try {
    const value = JSON.parse(candidate);
  return Array.isArray(value) ? value.filter(item => item && (Number.isInteger(item.id) || typeof item.id === 'string') && typeof item.text === 'string') : [];
  } catch { return []; }
}

function readMultipartField(raw, contentType, fieldName) {
  const boundary = contentType.match(/boundary=([^;]+)/)?.[1];
  if (!boundary) return null;
  const text = raw.toString('latin1');
  const part = text.split(`--${boundary}`).find(value => value.includes(`name="${fieldName}"`));
  return part?.split('\r\n\r\n')[1]?.split('\r\n')[0] || null;
}

function readMultipartFileBytes(raw, contentType) {
  const boundary = contentType.match(/boundary=([^;]+)/)?.[1];
  if (!boundary) return null;
  const text = raw.toString('latin1');
  const part = text.split(`--${boundary}`).find(value => value.includes('filename="audio.wav"'));
  return part ? Math.max(0, Buffer.byteLength(part, 'latin1') - part.indexOf('\r\n\r\n') - 6) : null;
}

async function fixtureRequest(url, method = 'GET', payload) {
  const response = await fetch(url, {method, ...(payload ? {headers: {'content-type': 'application/json'}, body: JSON.stringify(payload)} : {})});
  const value = await response.json();
  if (!response.ok) throw new Error(`fixture control HTTP ${response.status}`);
  return value;
}

module.exports = {createFixture};
