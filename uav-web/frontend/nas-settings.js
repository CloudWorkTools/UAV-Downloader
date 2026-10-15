window.NasSettings = (() => {
    function load(settings = {}) {
      document.getElementById('nas-enabled').checked = !!settings.enabled;
      ['host', 'share', 'directory', 'username'].forEach(key => document.getElementById('nas-' + key).value = settings[key] || '');
      document.getElementById('nas-password').value = '';
      document.getElementById('nas-status').textContent = settings.password_saved ? '密碼已保存；留空可沿用。變更 IP 或帳號時請重新輸入密碼。' : '尚未保存 NAS 密碼';
    }
    function payload() {
      const settings = {enabled: document.getElementById('nas-enabled').checked};
      ['host', 'share', 'directory', 'username', 'password'].forEach(key => settings[key] = document.getElementById('nas-' + key).value);
      return settings;
    }

    async function testConnection() {
      const button = document.getElementById('nas-test-button');
      const status = document.getElementById('nas-status');
      button.disabled = true;
      status.textContent = '連線與寫入測試中…';
      try {
        const response = await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({test_nas: true, nas: payload()})});
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.error || '測試失敗');
        status.textContent = '連線與寫入成功；請按下方儲存設定以永久保存';
      } catch (error) { status.textContent = error.message; }
      finally { button.disabled = false; }
    }

    return {load, payload, testConnection};
})();
