window.NasSettings = (() => {
    let readingSpace = false;
    let testedSpace = false;

    function formatSize(bytes) {
      const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
      let unit = 0;
      while (bytes >= 1000 && unit < units.length - 1) { bytes /= 1000; unit++; }
      return bytes.toLocaleString('zh-TW', {minimumFractionDigits: unit ? 2 : 0, maximumFractionDigits: unit ? 2 : 0}) + ' ' + units[unit];
    }

    function renderSpace(result, id) {
      const panel = document.getElementById(id);
      const values = document.getElementById(id + '-values');
      if (!panel || !values) return;
      const space = result.space;
      if (result.enabled === false || !space || ![space.total_bytes, space.free_bytes, space.available_bytes].every(value => Number.isFinite(value) && value >= 0) || !space.total_bytes || space.free_bytes > space.total_bytes || space.available_bytes > space.free_bytes) {
        panel.classList.toggle('hidden', true);
        values.textContent = '';
        return;
      }
      panel.classList.toggle('hidden', false);
      const used = space.total_bytes - space.free_bytes;
      const percent = (used / space.total_bytes * 100).toFixed(1);
      const checked = result.checked_at ? new Date(result.checked_at * 1000).toLocaleTimeString('zh-TW') : '';
      values.innerHTML = `<div class="flex flex-wrap gap-x-8 gap-y-2 mb-3"><div>總容量 <strong class="text-white ml-1">${formatSize(space.total_bytes)}</strong></div><div>剩餘空間 <strong class="text-emerald-400 ml-1">${formatSize(space.free_bytes)}</strong></div></div><progress class="w-full h-2 accent-pink-600" max="100" value="${percent}" aria-label="NAS 已使用 ${percent}%"></progress><div class="flex flex-wrap justify-between gap-2 mt-1 text-xs text-gray-400"><span>已使用 ${formatSize(used)}（${percent}%）</span><span>${checked ? '回報時間：' + checked : ''}</span></div>${space.available_bytes < space.free_bytes ? `<p class="mt-2 text-xs text-amber-400">此帳號可用空間：${formatSize(space.available_bytes)}</p>` : ''}`;
    }

    async function refreshSpace() {
      if (readingSpace) return;
      readingSpace = true;
      try {
        const response = await fetch('/api/download/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'nas_space'})});
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.error || 'NAS 容量讀取失敗');
        renderSpace(result, 'nas-space-queue');
        if (!testedSpace) renderSpace(result, 'nas-space-settings');
      } catch (error) {
        const result = {space_error:error.message};
        renderSpace(result, 'nas-space-queue');
        if (!testedSpace) renderSpace(result, 'nas-space-settings');
      } finally { readingSpace = false; }
    }

    function load(settings = {}) {
      testedSpace = false;
      document.getElementById('nas-space-settings-label').textContent = '已儲存 NAS 的容量';
      document.getElementById('nas-storage-mode').value = settings.enabled ? (['direct', 'smb'].includes(settings.mode) ? settings.mode : 'upload') : 'local';
      document.getElementById('nas-direct-path').value = settings.direct_path || '';
      updateMode();
      ['host', 'share', 'directory', 'username'].forEach(key => document.getElementById('nas-' + key).value = settings[key] || '');
      document.getElementById('nas-password').value = '';
      document.getElementById('nas-status').textContent = settings.password_saved ? '密碼已保存；留空可沿用。變更 NAS 位址或帳號時請重新輸入密碼。' : '尚未保存 NAS 密碼';
    }
    function payload() {
      const mode = document.getElementById('nas-storage-mode').value;
      const settings = {enabled: mode !== 'local', mode: mode === 'local' ? 'upload' : mode, direct_path: document.getElementById('nas-direct-path').value};
      ['host', 'share', 'directory', 'username', 'password'].forEach(key => settings[key] = document.getElementById('nas-' + key).value);
      return settings;
    }

    function updateMode() {
      const mode = document.getElementById('nas-storage-mode').value;
      if (mode === 'local') document.getElementById('nas-space-settings')?.classList.toggle('hidden', true);
      document.getElementById('nas-direct-fields').classList.toggle('hidden', mode !== 'direct');
      document.getElementById('nas-upload-fields').classList.toggle('hidden', !['upload', 'smb'].includes(mode));
      document.getElementById('nas-address-help').classList.toggle('hidden', !['upload', 'smb'].includes(mode));
      document.getElementById('nas-credentials-help').textContent = mode === 'direct'
        ? '此進階模式使用系統已掛載路徑。若要使用這裡的帳密直接連線，請改選「透過 SMB 直接儲存至 NAS」。'
        : mode === 'smb' ? '以帳密直接連線 SMB，影片分段、合成檔與字幕寫入 NAS，不需事先掛載。請先測試連線，再儲存設定；啟用 AI 字幕時僅在本機暫存辨識音訊與處理檔，完成後清除。'
        : mode === 'local' ? '可先填寫 NAS 帳號與密碼；選擇「透過 SMB 直接儲存至 NAS」後使用。'
        : '填寫 NAS 帳號與密碼，按測試連線後儲存設定；密碼會加密保存。';
      document.getElementById('nas-test-button').disabled = mode === 'local';
      document.getElementById('setting-download-dir').disabled = ['direct', 'smb'].includes(mode);
      const pathFields = document.getElementById('setting-local-path-fields');
      if (pathFields) pathFields.hidden = ['direct', 'smb'].includes(mode);
    }

    async function testConnection() {
      const button = document.getElementById('nas-test-button');
      const status = document.getElementById('nas-status');
      button.disabled = true;
      status.textContent = '連線與寫入測試中…';
      try {
        const settings = payload();
        if (settings.mode === 'direct' && /^smb:/i.test(settings.direct_path.trim())) {
          throw new Error('此欄位需要已掛載路徑，例如 /nas。若要以網址與帳密直接寫入 NAS，請選「透過 SMB 直接儲存至 NAS」，不需掛載。');
        }
        const response = await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({test_nas: true, nas: settings})});
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.error || '測試失敗');
        status.textContent = '連線與寫入成功；請按下方儲存設定以永久保存';
        testedSpace = true;
        document.getElementById('nas-space-settings-label').textContent = '本次連線測試容量';
        renderSpace(result, 'nas-space-settings');
      } catch (error) {
        status.textContent = error.message;
        testedSpace = true;
        renderSpace({}, 'nas-space-settings');
      }
      finally { updateMode(); }
    }

    return {load, payload, updateMode, testConnection, refreshSpace, renderSpace};
})();
