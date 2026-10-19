window.createVideoSelection = ({startDownload, startBatch}) => {
    const selectedUrls = new Set();
    let visibleUrls = [];
    let submittingDownloads = false;
    function setVisible(urls) {
      visibleUrls = urls;
      update();
    }
    function togglePage() {
      const allSelected = visibleUrls.length > 0 && visibleUrls.every(url => selectedUrls.has(url));
      visibleUrls.forEach(url => allSelected ? selectedUrls.delete(url) : selectedUrls.add(url));
      document.querySelectorAll('#video-grid input[type="checkbox"]').forEach(c => c.checked = !allSelected);
      update();
    }

    function toggle(url, checked) {
      if (checked) selectedUrls.add(url);
      else selectedUrls.delete(url);
      update();
    }

    function update() {
      const bar = document.getElementById('selection-bar');
      const countEl = document.getElementById('selected-count');
      countEl.textContent = selectedUrls.size;
      const selectButton = document.getElementById('select-page-button');
      selectButton.disabled = !visibleUrls.length || submittingDownloads;
      selectButton.textContent = visibleUrls.length && visibleUrls.every(url => selectedUrls.has(url)) ? '清除勾選' : '全選';
      const downloadButton = document.getElementById('download-selected-button');
      downloadButton.disabled = !selectedUrls.size || submittingDownloads;
      downloadButton.textContent = submittingDownloads ? '加入中…' : `下載已選 (${selectedUrls.size})`;
      if (selectedUrls.size > 0) bar.classList.remove('hidden');
      else bar.classList.add('hidden');
    }

    function clear() {
      selectedUrls.clear();
      update();
      document.querySelectorAll('#video-grid input[type="checkbox"]').forEach(c => c.checked = false);
    }

    async function download() {
      if (submittingDownloads || !selectedUrls.size) return;
      submittingDownloads = true;
      update();
      const urls = Array.from(selectedUrls);
      try {
        if (startBatch) {
          if (await startBatch(urls)) urls.forEach(url => selectedUrls.delete(url));
        } else {
          for (const url of urls) {
            if (await startDownload(url)) selectedUrls.delete(url);
          }
        }
      } finally {
        submittingDownloads = false;
        document.querySelectorAll('#video-grid input[type="checkbox"]').forEach((c, i) => c.checked = selectedUrls.has(visibleUrls[i]));
        update();
      }
    }

    return {setVisible, has: url => selectedUrls.has(url), togglePage, toggle, clear, download};
};
