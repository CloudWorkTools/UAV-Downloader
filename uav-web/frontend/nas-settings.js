window.NasSettings = (() => {
    function load(settings = {}) {
      document.getElementById('nas-storage-mode').value = settings.enabled ? (settings.mode === 'direct' ? 'direct' : 'upload') : 'local';
      document.getElementById('nas-direct-path').value = settings.direct_path || '';
      updateMode();
      ['host', 'share', 'directory', 'username'].forEach(key => document.getElementById('nas-' + key).value = settings[key] || '');
      document.getElementById('nas-password').value = '';
      document.getElementById('nas-status').textContent = settings.password_saved ? '密碼已保存；留空可沿用。變更 NAS 位址或帳號時請重新輸入密碼。' : '尚未保存 NAS 密碼';
    }
    function payload() {
      const mode = document.getElementById('nas-storage-mode').value;
      const settings = {enabled: mode !== 'local', mode: mode === 'direct' ? 'direct' : 'upload', direct_path: document.getElementById('nas-direct-path').value};
      ['host', 'share', 'directory', 'username', 'password'].forEach(key => settings[key] = document.getElementById('nas-' + key).value);
      return settings;
    }

    function updateMode() {
      const mode = document.getElementById('nas-storage-mode').value;
      document.getElementById('nas-direct-fields').classList.toggle('hidden', mode !== 'direct');
      document.getElementById('nas-upload-fields').classList.toggle('hidden', mode !== 'upload');
      document.getElementById('nas-address-help').classList.toggle('hidden', mode !== 'upload');
      document.getElementById('nas-test-button').disabled = mode === 'local';
      document.getElementById('setting-download-dir').disabled = mode === 'direct';
    }

    async function testConnection() {
      const button = document.getElementById('nas-test-button');
      const status = document.getElementById('nas-status');
      button.disabled = true;
      status.textContent = '連線與寫入測試中…';
      try {
        const settings = payload();
        if (settings.mode === 'direct' && /^smb:/i.test(settings.direct_path.trim())) {
          throw new Error('此欄位需要已掛載路徑，例如 /nas。SMB 網址與帳密請使用 SMB 傳送模式；直接儲存則需先完成系統掛載。');
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
