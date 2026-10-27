window.addEventListener('DOMContentLoaded', () => {
  const mobile = matchMedia('(max-width: 639px)');
  const byId = id => document.getElementById(id);
  const fab = document.createElement('button');
  fab.id = 'mobile-action-fab';
  fab.type = 'button';
  fab.setAttribute('aria-label', '開啟功能選單');
  fab.setAttribute('aria-expanded', 'false');
  fab.setAttribute('aria-controls', 'mobile-action-dialog');
  fab.innerHTML = '<img src="/mascot-fab.svg" alt="" width="64" height="64">';
  const badge = document.createElement('span');
  badge.id = 'mobile-selection-count';
  badge.hidden = true;
  fab.append(badge);
  const dialog = document.createElement('dialog');
  dialog.id = 'mobile-action-dialog';
  dialog.hidden = true;
  dialog.setAttribute('aria-labelledby', 'mobile-action-title');
  dialog.innerHTML = '<header><button type="button" id="mobile-action-back" aria-label="返回上一層">‹ 返回</button><strong id="mobile-action-title"></strong><button type="button" id="mobile-action-close" aria-label="關閉功能選單">✕</button></header><div id="mobile-action-items"></div>';
  document.body.append(fab, dialog);
  const groups = {
    browse: ['瀏覽功能', [['影片選取', 'selection'], ['新增網址', 'queue-add-mobile']]],
    queue: ['下載功能', [['新增任務', 'queue-add-mobile'], ['批次處理', 'batch'], ['佇列整理', 'organize']]],
    selection: ['影片選取', ['select-page-button', 'download-selected-button', 'browse-clear-mobile']],
    batch: ['批次處理', ['queue-selection-mode', 'select-tasks-button', 'pause-tasks-button', 'resume-tasks-button', 'retry-tasks-button', 'edit-tasks-button', 'cancel-tasks-button']],
    organize: ['佇列整理', ['resume-unfinished-button', 'remove-duplicate-tasks-button', 'clear-completed-button', 'queue-probe-mobile']],
  };
  let stack = [];
  let page;
  function close() {
    if (!dialog.hidden) dialog.close();
    dialog.hidden = true;
    document.body.append(fab);
    fab.setAttribute('aria-expanded', 'false');
    syncRows();
    if (!fab.hidden && mobile.matches) fab.focus();
  }
  function syncRows() {
    dialog.querySelectorAll('[data-mobile-source]').forEach(button => {
      const source = byId(button.dataset.mobileSource);
      button.textContent = source.id === 'browse-clear-mobile' ? '清除全部' :
        source.id === 'select-page-button' && source.textContent === '清除勾選' ? '清除本頁' : source.textContent;
      button.hidden = source.disabled || source.hidden || (source.id === 'browse-clear-mobile' && byId('selected-count').textContent === '0');
    });
    const count = Number(byId('selected-count').textContent) || 0;
    dialog.querySelectorAll('[data-mobile-group]').forEach(button => {
      const entries = groups[button.dataset.mobileGroup][1];
      button.hidden = !entries.some(id => {
        const source = byId(id);
        return source && !source.disabled && !source.hidden && (id !== 'browse-clear-mobile' || count > 0);
      });
    });
    const visible = [...dialog.querySelectorAll('#mobile-action-items button:not([hidden])')];
    dialog.style.setProperty('--menu-height', visible.length > 4 ? '330px' : '270px');
    visible.forEach((button, index) => {
      const outer = visible.length > 4 && index >= 3;
      const ringCount = visible.length > 4 ? (outer ? visible.length - 3 : 3) : visible.length;
      const slot = outer ? index - 3 : index;
      button.style.setProperty('--angle', `${ringCount === 1 ? 45 : slot * 90 / (ringCount - 1)}deg`);
      button.style.setProperty('--radius', `${visible.length > 4 ? (outer ? 192 : 104) : 140}px`);
      button.style.setProperty('--delay', `${index * 25}ms`);
    });
    badge.textContent = count;
    badge.hidden = !count || page !== 'browse';
    const label = dialog.hidden ? '開啟功能選單' : '關閉功能選單';
    fab.setAttribute('aria-label', count && page === 'browse' ? `${label}，已選 ${count} 部` : label);
  }
  function render() {
    const [title, entries] = groups[stack.at(-1)];
    byId('mobile-action-title').textContent = title;
    byId('mobile-action-back').hidden = stack.length === 1;
    byId('mobile-action-items').replaceChildren(...entries.map(entry => {
      const button = document.createElement('button');
      button.type = 'button';
      if (Array.isArray(entry) && entry[1] in groups) {
        button.textContent = entry[0] + ' ›';
        button.dataset.mobileGroup = entry[1];
        button.onclick = () => {stack.push(entry[1]);render();};
      } else {
        const id = Array.isArray(entry) ? entry[1] : entry;
        button.dataset.mobileSource = id;
        button.onclick = () => {const source = byId(id);if (source.disabled || source.hidden) return;close();source.click();};
      }
      return button;
    }));
    syncRows();
    (dialog.querySelector('#mobile-action-items button:not([hidden])') || byId('mobile-action-close')).focus();
  }
  fab.onclick = () => {
    if (!dialog.hidden) {close();return;}
    dialog.append(fab);
    stack = [page];
    dialog.hidden = false;
    dialog.showModal();
    fab.setAttribute('aria-expanded', 'true');
    fab.setAttribute('aria-label', '關閉功能選單');
    render();
  };
  byId('mobile-action-close').onclick = close;
  byId('mobile-action-back').onclick = () => {stack.pop();render();};
  dialog.addEventListener('cancel', event => {event.preventDefault();close();});
  dialog.addEventListener('click', event => {if (event.target === dialog) {const r = dialog.getBoundingClientRect();if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) close();}});
  function syncPage() {
    const next = ['browse', 'queue'].find(id => !byId('tab-' + id).classList.contains('hidden'));
    if (next !== page || !mobile.matches) close();
    page = next;
    fab.hidden = !next;
    syncRows();
  }
  const tabObserver = new MutationObserver(syncPage);
  ['browse', 'queue'].forEach(id => tabObserver.observe(byId('tab-' + id), {attributes:true, attributeFilter:['class']}));
  const stateObserver = new MutationObserver(syncRows);
  Object.values(groups).flatMap(group => group[1]).filter(entry => typeof entry === 'string').forEach(id => {
    stateObserver.observe(byId(id), {attributes:true, attributeFilter:['disabled','hidden'],childList:true,subtree:true,characterData:true});
  });
  stateObserver.observe(byId('selected-count'), {childList:true, subtree:true, characterData:true});
  mobile.addEventListener('change', syncPage);
  syncPage();
});
