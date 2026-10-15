/* The view accepts a catalog and reports a selection; site adapters own URLs. */
(function () {
  window.createCategoryBrowser = function (container, onSelect) {
    let selectedUrl = '';
    let buttons = [];
    const select = url => {
      selectedUrl = url;
      buttons.forEach(button => {
        const selected = button.dataset.url === url;
        button.setAttribute('aria-pressed', String(selected));
        button.classList.toggle('bg-brand-600', selected);
        button.classList.toggle('text-white', selected);
      });
    };
    return {
      select,
      render(groups, url) {
        container.replaceChildren();
        buttons = [];
        selectedUrl = url || '';
        groups.forEach(group => {
          if (!group.items?.length) return;
          const details = document.createElement('details');
          details.className = 'rounded-lg border border-[#3e3e4e] bg-[#20202a]';
          const summary = document.createElement('summary');
          summary.className = 'cursor-pointer px-3 py-2 text-sm font-medium text-gray-200';
          summary.textContent = `${group.name} (${group.items.length})`;
          details.appendChild(summary);
          const list = document.createElement('div');
          list.className = 'flex flex-wrap gap-2 p-3 pt-1';
          group.items.forEach(item => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'h-10 px-3 rounded-lg border border-[#3e3e4e] text-sm text-gray-300 hover:border-brand-500';
            button.textContent = item.name;
            button.dataset.url = item.url;
            button.addEventListener('click', () => {
              select(item.url);
              onSelect(item);
            });
            buttons.push(button);
            list.appendChild(button);
          });
          details.appendChild(list);
          container.appendChild(details);
        });
        select(selectedUrl);
      },
    };
  };
})();
