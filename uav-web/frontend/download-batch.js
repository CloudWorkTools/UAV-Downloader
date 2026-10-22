// Decode SMB URL paths for display only; requests retain the original destination.
window.formatStoragePath = value => {
  const path = String(value ?? '');
  if (!path.startsWith('smb://')) return path;
  try { return decodeURIComponent(path); } catch { return path; }
};

window.createDownloadBatch = ({getQuery, getSubtitleRequest, onTasks}) => {
  const dialog = document.getElementById('download-batch-dialog');
  const form = document.getElementById('download-batch-form');
  const field = id => document.getElementById('batch-' + id);
  const preferenceKey = 'uav-download-storage';
  let preferredStorage = 'query';
  try {
    const saved = localStorage.getItem(preferenceKey);
    if (saved === 'settings' || saved === 'query') preferredStorage = saved;
  } catch {}
  let urls = [], resolve, preview, revision = 0, submitting = false, requestId, pendingRequest;
  const settle = success => { ++revision; const done = resolve; resolve = null; if (done) done(success); };
  const storage = () => form.querySelector('input[name="batch-storage"]:checked').value;
  const values = () => ({storage: storage(), name: field('name').value,
    policy: field('policy').value, naming: field('naming').value});
  function updateSubtitleSummary() {
    const summary=document.getElementById('batch-subtitle-summary');
    if(!summary)return;
    if(!field('subtitles').checked){summary.textContent='未啟用獨立字幕工作；沿用既有下載字幕設定。啟用後依字幕工作頁目前選取階段與設定檔快照排入字幕任務。';return;}
    try{const spec=getSubtitleRequest?.();const profiles=window.subtitleSettings?.state?.profiles||[];const stages=spec?.selected_stages.map(stage=>{const id=spec.profile_ids?.[stage]||spec.profile_ids?.text;const profile=profiles.find(item=>item.profile_id===id);return `${stage}：${profile?.display_name||'設定檔'}（${profile?.provider||'服務未知'} / ${profile?.model||'模型未知'}）`;})||[];summary.textContent=spec?`辨識來源：日文；字幕階段與模型：${stages.join('；')}；翻譯目標：${spec.target_languages.join('、')||'未選擇'}；輸出字幕：${spec.output_languages.join('、')||'無'}。加入下載時保存設定檔快照。`:'請先選擇字幕階段。';}
    catch(error){summary.textContent=error.message;}
  }
  async function request(body) {
    const response = await fetch('/api/download/batch', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok || !data.success) {
      const error = Error(data.error || '資料夾操作失敗');
      error.serverRejected = true;
      throw error;
    }
    return data;
  }
  async function refresh() {
    const version = ++revision;
    preview = null;
    field('submit').disabled = true;
    field('status').textContent = '正在檢查儲存位置…';
    if (storage() === 'settings' && field('policy').value === 'new') field('policy').value = 'merge';
    field('policy').querySelector('option[value="new"]').disabled = storage() === 'settings';
    const options = values();
    field('name').disabled = options.storage === 'settings';
    try {
      const data = await request({action:'preview', ...options});
      if (version !== revision || !dialog.open) return;
      preview = data;
      field('base-destination').textContent = window.formatStoragePath(data.base_destination);
      field('destination').textContent = window.formatStoragePath(data.destination);
      field('conflict').hidden = !data.exists;
      field('naming-row').hidden = options.policy !== 'new' || options.storage === 'settings';
      field('status').textContent = !data.can_submit ? (data.overwrite_busy ? '此資料夾有未完成的覆寫任務，請待任務結束後再追加。' :
        data.busy ? '此資料夾有未完成任務，請使用合併模式或待任務結束後再試。' : '此名稱已存在，請修改名稱。') :
        data.busy ? '將追加至此資料夾，相同影片會略過。' :
        options.policy === 'overwrite' ? `資料夾目前有 ${data.file_count} 個檔案。只覆寫本次產生的同名檔案，其餘保留；實際同名檔案於解析影片後確認，已覆寫的檔案不會因取消而還原。` :
        data.exists ? '已找到同名資料夾，請確認處理方式與儲存位置。' : '確認後才會建立資料夾。';
      field('submit').textContent = options.policy === 'overwrite' ? '覆寫並下載' : '開始下載';
      field('submit').disabled = !data.can_submit;
    } catch (error) {
      if (version === revision) field('status').textContent = error.message;
    }
  }
  form.addEventListener('input', event => {
    if (submitting) return;
    if (event.target.name === 'batch-storage') {
      preferredStorage = storage();
      try { localStorage.setItem(preferenceKey, preferredStorage); } catch {}
      field('policy').value = preferredStorage === 'settings' ? 'merge' : 'new';
    }
    if(event.target.id==='batch-subtitles')updateSubtitleSummary();
    refresh();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting || !preview || field('submit').disabled) return;
    submitting = true;
    field('submit').disabled = true;
    field('cancel').disabled = true;
    for (const control of form.querySelectorAll('input, select')) control.disabled = true;
    try {
      if(field('subtitles').checked&&!pendingRequest){
        try{const subtitle_request=getSubtitleRequest?.();if(!subtitle_request)throw Error('請先選擇字幕階段');pendingRequest={action:'submit',...values(),expected_destination:preview.destination,urls,request_id:requestId,subtitle_request};}
        catch(error){field('status').textContent=error.message;submitting=false;field('submit').disabled=false;field('cancel').disabled=false;return;}
      }
      pendingRequest ||= {action:'submit', ...values(), expected_destination:preview.destination, urls, request_id:requestId};
      const data = await request(pendingRequest);
      pendingRequest = null;
      onTasks(data.tasks);
      dialog.close('submitted');
      settle(true);
    } catch (error) {
      if (error.serverRejected) {
        pendingRequest = null;
        await refresh();
        field('status').textContent = error.message + '；請重新確認後再試。';
      } else {
        field('status').textContent = '未收到加入結果，請重試確認同一批任務，避免重複加入。';
        field('submit').disabled = false;
      }
    } finally {
      submitting = false;
      field('cancel').disabled = false;
      for (const control of form.querySelectorAll('input, select')) control.disabled = !!pendingRequest;
      field('name').disabled = !!pendingRequest || storage() === 'settings';
    }
  });
  field('cancel').addEventListener('click', () => {dialog.close(); settle(false);});
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    if (!submitting) {dialog.close(); settle(false);}
  });
  dialog.addEventListener('close', () => { if (!dialog.open) settle(dialog.returnValue === 'submitted'); });
  return {open(selected) {
    if (dialog.open || !selected.length) return Promise.resolve(false);
    urls = [...selected];
    pendingRequest = null;
    for (const control of form.querySelectorAll('input, select')) control.disabled = false;
    requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
    form.reset();
    field('storage-' + preferredStorage).checked = true;
    field('base-destination').textContent = '讀取設定中…';
    field('destination').textContent = '';
    field('count').textContent = urls.length;
    const now = new Date();
    const date = `${now.getFullYear()}${String(now.getMonth()+1).padStart(2,'0')}${String(now.getDate()).padStart(2,'0')}`;
    field('name').value = getQuery() || `下載_${date}`;
    updateSubtitleSummary();
    dialog.returnValue = '';
    dialog.showModal();
    refresh();
    return new Promise(done => { resolve = done; });
  }};
};
