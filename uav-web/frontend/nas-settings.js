window.NasSettings = (() => {
    function load(settings = {}) {
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
      } catch (error) { status.textContent = error.message; }
      finally { updateMode(); }
    }

    return {load, payload, updateMode, testConnection};
})();
