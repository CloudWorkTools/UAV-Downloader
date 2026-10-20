const assert = require('node:assert/strict');

class Cdp {
  constructor(chrome) {
    this.chrome = chrome;
    this.nextId = 1;
    this.pending = new Map();
    this.buffer = '';
    this.targetId = null;
    this.sessionId = null;
    this.dialogResponses = [];
    this.eventLog = [];
    chrome.stdio[4].setEncoding('utf8');
    chrome.stdio[4].on('data', chunk => this.receive(chunk));
  }

  receive(chunk) {
    this.buffer += chunk;
    for (;;) {
      const end = this.buffer.indexOf('\0');
      if (end < 0) return;
      const raw = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 1);
      if (!raw) continue;
      const message = JSON.parse(raw);
      if (message.method === 'Page.javascriptDialogOpening') {
        const response = this.dialogResponses.shift() || {accept: true};
        this.command('Page.handleJavaScriptDialog', response, message.sessionId || this.sessionId).catch(() => {});
        continue;
      }
      if (message.method && !message.id) {
        this.eventLog.push({method: message.method, params: message.params || {}});
        continue;
      }
      if (!message.id || !this.pending.has(message.id)) continue;
      const pending = this.pending.get(message.id);
      this.pending.delete(message.id);
      clearTimeout(pending.timer);
      message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result || {});
    }
  }

  command(method, params = {}, sessionId = this.sessionId) {
    const id = this.nextId++;
    const message = {id, method, params, ...(sessionId ? {sessionId} : {})};
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP command timed out: ${method}`));
      }, 10000);
      this.pending.set(id, {resolve, reject, timer});
      this.chrome.stdio[3].write(JSON.stringify(message) + '\0');
    });
  }

  async start(url) {
    const target = await this.command('Target.createTarget', {url: 'about:blank'}, null);
    this.targetId = target.targetId;
    const attached = await this.command('Target.attachToTarget', {targetId: this.targetId, flatten: true}, null);
    this.sessionId = attached.sessionId;
    await this.command('Page.enable');
    await this.command('Runtime.enable');
    await this.command('Network.enable');
    await this.command('Page.navigate', {url});
    await this.until('document.readyState === "complete"');
  }

  async evaluate(expression) {
    const result = await this.command('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true,
    });
    if (result.exceptionDetails) throw new Error(`Browser evaluation failed: ${JSON.stringify(result.exceptionDetails)}`);
    return result.result?.value;
  }

  async until(expression, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    let last;
    while (Date.now() < deadline) {
      last = await this.evaluate(`Boolean(${expression})`);
      if (last) return;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error(`Browser condition timed out: ${expression}; last=${last}`);
  }

  async selector(testId) {
    const selector = `[data-testid=${JSON.stringify(String(testId))}]`;
    const value = await this.evaluate(`(() => {
      const element = document.querySelector(${JSON.stringify(selector)});
      return element ? {tag: element.tagName, text: element.textContent, value: element.value ?? null,
        disabled: Boolean(element.disabled), checked: Boolean(element.checked), hidden: element.hidden} : null;
    })()`);
    assert.ok(value, `missing [data-testid="${testId}"]`);
    return value;
  }

  async click(testId) {
    const selector = `[data-testid=${JSON.stringify(String(testId))}]`;
    await this.evaluate(`(() => {
      const element = document.querySelector(${JSON.stringify(selector)});
      if (!element) throw new Error('missing ${testId}');
      element.click();
      return true;
    })()`);
  }

  async fill(testId, value) {
    const selector = `[data-testid=${JSON.stringify(String(testId))}]`;
    const encoded = JSON.stringify(String(value));
    await this.evaluate(`(() => {
      const element = document.querySelector(${JSON.stringify(selector)});
      if (!element) throw new Error('missing ${testId}');
      element.focus(); element.value = ${encoded};
      element.dispatchEvent(new Event('input', {bubbles:true}));
      element.dispatchEvent(new Event('change', {bubbles:true}));
      return true;
    })()`);
  }

  async byId(id, operation) {
    const encodedId = JSON.stringify(id);
    return this.evaluate(`(() => {
      const element = document.getElementById(${encodedId});
      if (!element) throw new Error('missing element id: ' + ${encodedId});
      ${operation}
      return true;
    })()`);
  }

  async fillId(id, value) {
    const encoded = JSON.stringify(String(value));
    return this.byId(id, `element.focus(); element.value = ${encoded};
      element.dispatchEvent(new Event('input', {bubbles:true}));
      element.dispatchEvent(new Event('change', {bubbles:true}));`);
  }

  async setChecked(id, checked) {
    return this.byId(id, `element.checked = ${Boolean(checked)};
      element.dispatchEvent(new Event('change', {bubbles:true}));`);
  }

  async selectId(id, value) {
    const encoded = JSON.stringify(String(value));
    return this.byId(id, `element.value = ${encoded};
      element.dispatchEvent(new Event('change', {bubbles:true}));`);
  }

  respondToNextDialog(response) { this.dialogResponses.push(response); }

  close() {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error('CDP closed'));
    }
    this.pending.clear();
  }
}

module.exports = {Cdp};
