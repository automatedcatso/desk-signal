/* Notice Studio workflow. */
(function () {
  'use strict';

  const UNSIGNED = window.APP_CONFIG.unsignedRole;
  const state = {
    layer: '', bank: '', search: '', status: '', sort: 'row_index', dir: 'asc',
    page: 1, perPage: 50, total: 0, pages: 1,
    selected: new Set(), currentRecords: [], drawerId: null
  };
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const debounce = (fn, ms) => {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  };

  function overlay(show, text) {
    $('#overlay-text').textContent = text || 'Working…';
    $('#app-overlay').classList.toggle('d-none', !show);
  }

  function uploadError(message) {
    const element = $('#upload-error');
    element.textContent = message || '';
    element.classList.toggle('d-none', !message);
  }

  function initUpload() {
    const dropzone = $('#dropzone');
    const input = $('#file-input');
    $('#choose-file').addEventListener('click', () => input.click());
    input.addEventListener('change', () => input.files[0] && handleFile(input.files[0]));
    ['dragover', 'dragenter'].forEach((eventName) => dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add('dragover');
    }));
    ['dragleave', 'drop'].forEach((eventName) => dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove('dragover');
    }));
    dropzone.addEventListener('drop', (event) => {
      const file = event.dataTransfer.files[0];
      if (file) handleFile(file);
    });
  }

  async function handleFile(file) {
    uploadError('');
    if (!file.name.toLowerCase().endsWith('.xlsx')) {
      uploadError('Only .xlsx workbooks are accepted.');
      return;
    }
    overlay(true, 'Reading and validating workbook…');
    try {
      const response = await API.upload(file);
      if (!response.ok) {
        let message = response.error || 'Import failed.';
        if (response.missing?.length) message += ` Missing: ${response.missing.join(', ')}.`;
        uploadError(message);
        return;
      }
      $('#view-upload').classList.add('d-none');
      $('#view-workspace').classList.remove('d-none');
      await refreshAll();
    } catch (error) {
      uploadError(error.message);
    } finally {
      overlay(false);
    }
  }

  async function loadStats() {
    const summary = await API.stats();
    if (!summary.ok) return;
    $('#st-total').textContent = summary.total_records;
    $('#st-banks').textContent = summary.total_banks;
    $('#st-layers').textContent = summary.total_layers;
    $('#st-amount').textContent = Number(summary.total_amount || 0).toLocaleString(
      'en-IN', { maximumFractionDigits: 2 }
    );
    $('#st-email').textContent = summary.email_ready;
    $('#st-ready').textContent = summary.ready_notices;
    $('#st-completion-txt').textContent = `${summary.ready_notices} / ${summary.total_records} complete`;
    $('#completion-bar').style.width = `${summary.completion}%`;

    if (summary.sender_role) $('#global-sender-role').value = summary.sender_role;
    if (summary.sender_name) $('#global-sender-name').value = summary.sender_name;
    applySenderVisibility(
      $('#global-sender-role').value,
      '#global-sender-wrap',
      '#global-sender-name'
    );

    const ready = summary.total_records > 0 && summary.pending_names === 0;
    $('#btn-generate-all').disabled = !ready;
    $('#btn-zip-top').classList.toggle('d-none', !ready);
    if (ready && !sessionStorage.getItem('completePrompted')) {
      sessionStorage.setItem('completePrompted', '1');
      $('#complete-modal-text').textContent =
        `${summary.total_records} notices are ready. Build the delivery pack now?`;
      bootstrap.Modal.getOrCreateInstance($('#complete-modal')).show();
    }
    if (!ready) sessionStorage.removeItem('completePrompted');
  }

  async function loadFilters() {
    const response = await API.filters(state.layer);
    if (!response.ok) return;
    const layerElement = $('#layer-filters');
    layerElement.innerHTML = '';
    response.layers.forEach((layer) => {
      const item = document.createElement('div');
      item.className = `filter-item${state.layer === layer ? ' active' : ''}`;
      item.textContent = `Layer ${layer}`;
      item.addEventListener('click', () => {
        state.layer = state.layer === layer ? '' : layer;
        state.bank = '';
        state.page = 1;
        loadFilters();
        loadGrid();
        renderChips();
      });
      layerElement.appendChild(item);
    });
    if (!response.layers.length) layerElement.innerHTML = '<div class="empty-filter">No layers found.</div>';

    const companyElement = $('#bank-filters');
    $('#bank-layer-hint').textContent = state.layer ? `Layer ${state.layer}` : '';
    companyElement.innerHTML = '';
    if (!state.layer) {
      companyElement.innerHTML = '<div class="empty-filter">Choose a layer.</div>';
      return;
    }
    response.banks.forEach((company) => {
      const item = document.createElement('div');
      item.className = `filter-item${state.bank === company ? ' active' : ''}`;
      item.textContent = company;
      item.addEventListener('click', () => {
        state.bank = state.bank === company ? '' : company;
        state.page = 1;
        loadFilters();
        loadGrid();
        renderChips();
      });
      companyElement.appendChild(item);
    });
    if (!response.banks.length) companyElement.innerHTML = '<div class="empty-filter">No companies in this layer.</div>';
  }

  function renderChips() {
    const element = $('#filter-chips');
    element.innerHTML = '';
    const add = (label, clear) => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.innerHTML = `${esc(label)} <i class="bi bi-x-circle"></i>`;
      chip.querySelector('i').addEventListener('click', clear);
      element.appendChild(chip);
    };
    if (state.layer) add(`Layer: ${state.layer}`, () => {
      state.layer = ''; state.bank = ''; loadFilters(); loadGrid(); renderChips();
    });
    if (state.bank) add(`Company: ${state.bank}`, () => {
      state.bank = ''; loadGrid(); renderChips();
    });
    if (state.search) add(`Search: ${state.search}`, () => {
      state.search = ''; $('#search-input').value = ''; loadGrid(); renderChips();
    });
  }

  const statusMeta = {
    missing: ['status-missing', 'Needs reference'],
    ready: ['status-ready', 'Ready'],
    generated: ['status-generated', 'Generated'],
    error: ['status-error', 'Issue']
  };

  async function loadGrid() {
    const response = await API.list({
      layer: state.layer, bank: state.bank, search: state.search, status: state.status,
      sort: state.sort, dir: state.dir, page: state.page, per_page: state.perPage
    });
    if (!response.ok) return;
    state.currentRecords = response.records;
    state.total = response.total;
    state.pages = response.pages;
    state.page = response.page;
    const body = $('#record-body');
    body.innerHTML = '';
    response.records.forEach((record) => body.appendChild(renderRow(record)));
    $('#page-info').textContent = `Showing ${response.records.length} of ${response.total} rows`;
    renderPagination();
    updateBulkButtons();
  }

  function renderRow(record) {
    const row = document.createElement('tr');
    if (record.status === 'error') row.classList.add('row-error');
    const [statusClass, statusLabel] = statusMeta[record.status] || statusMeta.missing;
    row.innerHTML = `
      <td><input type="checkbox" class="row-check" data-id="${record.id}" ${state.selected.has(record.id) ? 'checked' : ''}></td>
      <td>${esc(record.account_no)}</td>
      <td>${esc(record.reference_name) || '<span class="text-danger">—</span>'}</td>
      <td>${esc(record.bank)}</td>
      <td>${record.company_email ? esc(record.company_email) : '<span class="email-missing">Add recipient</span>'}</td>
      <td>${esc(record.layer)}</td>
      <td>${esc(record.transaction_amount)}</td>
      <td><span class="status-dot ${statusClass}"></span>${statusLabel}</td>
      <td><button class="btn btn-sm btn-open" data-id="${record.id}" title="Review"><i class="bi bi-arrow-up-right"></i></button></td>`;
    row.querySelector('.btn-open').addEventListener('click', (event) => {
      event.stopPropagation();
      openDrawer(record.id);
    });
    row.addEventListener('click', (event) => {
      if (!event.target.closest('input,button,a')) openDrawer(record.id);
    });
    row.querySelector('.row-check').addEventListener('change', (event) => {
      if (event.target.checked) state.selected.add(record.id);
      else state.selected.delete(record.id);
      updateBulkButtons();
    });
    return row;
  }

  function renderPagination() {
    const list = $('#pagination');
    list.innerHTML = '';
    const add = (label, page, disabled, active) => {
      const item = document.createElement('li');
      item.className = `page-item ${disabled ? 'disabled' : ''} ${active ? 'active' : ''}`;
      item.innerHTML = `<a class="page-link" href="#">${label}</a>`;
      if (!disabled && !active) item.addEventListener('click', (event) => {
        event.preventDefault();
        state.page = page;
        loadGrid();
      });
      list.appendChild(item);
    };
    add('‹', state.page - 1, state.page <= 1, false);
    const start = Math.max(1, state.page - 2);
    const end = Math.min(state.pages, state.page + 2);
    for (let page = start; page <= end; page += 1) add(page, page, false, page === state.page);
    add('›', state.page + 1, state.page >= state.pages, false);
  }

  function updateBulkButtons() {
    const count = state.selected.size;
    $('#btn-bulk-name').disabled = count === 0;
    $('#btn-generate-selected').disabled = count === 0;
    $('#btn-bulk-name').innerHTML =
      `<i class="bi bi-type"></i> Bulk reference${count ? ` (${count})` : ''}`;
  }

  function applySenderVisibility(role, wrapSelector, inputSelector) {
    const unsigned = role === UNSIGNED;
    const wrap = $(wrapSelector);
    const input = $(inputSelector);
    if (wrap) wrap.style.display = unsigned ? 'none' : '';
    if (input && unsigned) input.value = '';
    if (input) input.disabled = unsigned;
  }

  async function openDrawer(id) {
    state.drawerId = id;
    const response = await API.record(id);
    const record = response.record;
    $('#drawer-reference').value = record.reference_name || '';
    $('#drawer-company-email').value = record.company_email || '';
    $('#drawer-sender-role').value = record.sender_role || '';
    $('#drawer-sender').value = record.sender_name || '';
    applySenderVisibility(
      record.sender_role || $('#global-sender-role').value,
      '#drawer-sender-wrap',
      '#drawer-sender'
    );
    renderDetails(record);
    await refreshPreview();
    $('#notice-drawer').classList.add('open');
    $('#drawer-reference').focus();
  }

  function renderDetails(record) {
    const fields = [
      ['Acknowledgement', record.acknowledgement_no],
      ['Company', record.bank],
      ['Layer', record.layer],
      ['Transaction ID', record.transaction_id],
      ['Transaction date', record.transaction_date],
      ['Transaction amount', record.transaction_amount],
      ['Account', record.account_no],
      ['IFSC', record.ifsc],
      ['Reference number', record.reference_no],
      ['Remarks', record.remarks],
      ['Action taken', record.action_taken],
      ['Action date', record.date_of_action]
    ];
    $('#detail-grid').innerHTML = fields.map(([label, value]) =>
      `<div class="label">${esc(label)}</div><div class="value">${esc(value) || '—'}</div>`
    ).join('');
  }

  const refreshPreview = debounce(async function () {
    if (!state.drawerId) return;
    const senderRole = $('#drawer-sender-role').value || $('#global-sender-role').value;
    const senderName = $('#drawer-sender').value || $('#global-sender-name').value;
    const response = await API.preview(state.drawerId, {
      reference_name: $('#drawer-reference').value,
      company_email: $('#drawer-company-email').value,
      sender_role: senderRole,
      sender_name: senderName
    });
    if (response.ok) renderPreview(response.preview);
  }, 180);

  function renderPreview(preview) {
    let html = '';
    preview.paragraphs.forEach((paragraph) => {
      if (!paragraph.text.trim()) {
        html += '<div style="height:.6em"></div>';
        return;
      }
      const alignment = paragraph.align?.includes('CENTER')
        ? 'center'
        : paragraph.align?.includes('RIGHT') ? 'right' : 'left';
      html += `<div style="text-align:${alignment}">${esc(paragraph.text)}</div>`;
    });
    preview.tables.forEach((rows) => {
      html += '<table>';
      rows.forEach((cells, rowIndex) => {
        html += '<tr>' + cells.map((cell) =>
          rowIndex === 0 ? `<th>${esc(cell)}</th>` : `<td>${esc(cell)}</td>`
        ).join('') + '</tr>';
      });
      html += '</table>';
    });
    $('#notice-preview').innerHTML = html;
  }

  async function saveDrawer(closeAfter) {
    const senderRole = $('#drawer-sender-role').value;
    const referenceName = $('#drawer-reference').value.trim();
    if (!referenceName) {
      $('#drawer-reference').classList.add('is-invalid');
      $('#drawer-reference').focus();
      return false;
    }
    $('#drawer-reference').classList.remove('is-invalid');
    const body = {
      reference_name: referenceName,
      company_email: $('#drawer-company-email').value.trim(),
      sender_role: senderRole,
      sender_name: (senderRole || $('#global-sender-role').value) === UNSIGNED
        ? ''
        : $('#drawer-sender').value.trim()
    };
    await API.updateRecord(state.drawerId, body);
    if (closeAfter) closeDrawer();
    await refreshAll();
    return true;
  }

  function closeDrawer() {
    $('#notice-drawer').classList.remove('open');
    state.drawerId = null;
  }

  function navigate(delta) {
    const ids = state.currentRecords.map((record) => record.id);
    const nextIndex = ids.indexOf(state.drawerId) + delta;
    if (nextIndex >= 0 && nextIndex < ids.length) openDrawer(ids[nextIndex]);
  }

  async function startGeneration(ids) {
    const senderRole = $('#global-sender-role').value;
    const body = {
      sender_role: senderRole,
      sender_name: senderRole === UNSIGNED ? '' : $('#global-sender-name').value.trim()
    };
    if (ids) body.ids = ids;
    const response = await API.startGeneration(body);
    if (!response.ok) {
      let message = response.error || 'Generation could not start.';
      if (response.incomplete_rows) message += `\nRows needing a reference: ${response.incomplete_rows.join(', ')}`;
      if (response.errored_rows) message += `\nRows with issues: ${response.errored_rows.join(', ')}`;
      alert(message);
      return;
    }
    bootstrap.Modal.getOrCreateInstance($('#progress-modal')).show();
    pollJob(response.job_id);
  }

  async function pollJob(jobId) {
    $('#gen-summary').classList.add('d-none');
    $('#gen-download').classList.add('d-none');
    const tick = async () => {
      const job = await API.genStatus(jobId);
      const percent = job.total ? Math.round((job.done / job.total) * 100) : 0;
      $('#gen-bar').style.width = `${percent}%`;
      const titles = {
        preparing: 'Preparing files',
        generating: 'Creating notices and drafts',
        packaging: 'Packaging ZIP',
        ready: 'Delivery pack ready',
        failed: 'Generation failed'
      };
      $('#progress-title').textContent = titles[job.state] || 'Working';
      $('#gen-status').textContent = job.state === 'ready'
        ? 'Your organized delivery pack is ready to download.'
        : job.state === 'failed' ? (job.error || 'Generation failed.') : `${job.done} / ${job.total}`;
      if (job.state === 'ready') {
        $('#gen-summary').classList.remove('d-none');
        $('#sum-generated').textContent = job.generated;
        $('#sum-emails').textContent = job.email_drafts;
        $('#sum-missing-email').textContent = job.missing_emails;
        $('#sum-errored').textContent = job.errored;
        const link = $('#gen-download');
        link.href = API.downloadUrl(jobId);
        link.classList.remove('d-none');
        await refreshAll();
        return;
      }
      if (job.state !== 'failed') setTimeout(tick, 600);
    };
    tick();
  }

  function esc(value) {
    return value === null || value === undefined ? '' : String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  async function refreshAll() {
    await Promise.all([loadStats(), loadFilters(), loadGrid()]);
    renderChips();
  }

  function exportErrorReport(report) {
    const rows = [['Row', 'Account', 'Issues']];
    report.errors.forEach((item) => rows.push([
      item.row, item.account_no, item.issues.join('; ')
    ]));
    const csv = rows.map((row) => row.map((cell) =>
      `"${String(cell).replace(/"/g, '""')}"`
    ).join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'notice_issues.csv';
    link.click();
    URL.revokeObjectURL(link.href);
  }

  function setZoom(value) {
    $('#notice-preview').style.transform = `scale(${value})`;
    localStorage.setItem('noticePreviewZoom', value);
    $$('.zoom-btn').forEach((button) =>
      button.classList.toggle('active', button.dataset.zoom === String(value))
    );
  }

  function bindEvents() {
    $('#search-input').addEventListener('input', debounce((event) => {
      state.search = event.target.value.trim();
      state.page = 1;
      loadGrid();
      renderChips();
    }, 250));
    $$('.record-table th[data-sort]').forEach((header) => header.addEventListener('click', () => {
      const column = header.dataset.sort;
      if (state.sort === column) state.dir = state.dir === 'asc' ? 'desc' : 'asc';
      else { state.sort = column; state.dir = 'asc'; }
      loadGrid();
    }));
    $('#select-all').addEventListener('change', (event) => {
      state.currentRecords.forEach((record) => {
        if (event.target.checked) state.selected.add(record.id);
        else state.selected.delete(record.id);
      });
      $$('.row-check').forEach((checkbox) => { checkbox.checked = event.target.checked; });
      updateBulkButtons();
    });
    $('#btn-bulk-name').addEventListener('click', async () => {
      const name = prompt('Reference name to assign to the selected rows:');
      if (!name) return;
      overlay(true, 'Applying reference name…');
      try {
        await API.bulkName(Array.from(state.selected), name.trim());
        state.selected.clear();
        await refreshAll();
      } finally {
        overlay(false);
      }
    });
    $('#btn-error-report').addEventListener('click', async () => {
      const response = await API.errorReport();
      if (response.ok) exportErrorReport(response.report);
    });
    $('#btn-generate-all').addEventListener('click', () => startGeneration(null));
    $('#btn-zip-top').addEventListener('click', () => startGeneration(null));
    $('#btn-generate-selected').addEventListener('click', () =>
      startGeneration(Array.from(state.selected))
    );
    $('#modal-generate').addEventListener('click', () => {
      bootstrap.Modal.getInstance($('#complete-modal'))?.hide();
      startGeneration(null);
    });
    $('#btn-clear-session').addEventListener('click', async () => {
      if (!confirm('Clear this workbook session and all entered values?')) return;
      overlay(true, 'Clearing local session…');
      await API.clearSession();
      sessionStorage.removeItem('completePrompted');
      window.location.reload();
    });

    $('#global-sender-role').addEventListener('change', (event) =>
      applySenderVisibility(event.target.value, '#global-sender-wrap', '#global-sender-name')
    );
    $('#save-signatory').addEventListener('click', async () => {
      const senderRole = $('#global-sender-role').value;
      await API.saveSignatory({
        sender_role: senderRole,
        sender_name: senderRole === UNSIGNED ? '' : $('#global-sender-name').value.trim()
      });
      await refreshAll();
    });

    $('#drawer-close').addEventListener('click', closeDrawer);
    $('#drawer-cancel').addEventListener('click', closeDrawer);
    $('#drawer-save').addEventListener('click', () => saveDrawer(true));
    $('#drawer-prev').addEventListener('click', () => navigate(-1));
    $('#drawer-next').addEventListener('click', () => navigate(1));
    $('#drawer-reference').addEventListener('input', refreshPreview);
    $('#drawer-company-email').addEventListener('input', refreshPreview);
    $('#drawer-sender').addEventListener('input', refreshPreview);
    $('#drawer-sender-role').addEventListener('change', (event) => {
      applySenderVisibility(
        event.target.value || $('#global-sender-role').value,
        '#drawer-sender-wrap',
        '#drawer-sender'
      );
      refreshPreview();
    });

    const savedZoom = localStorage.getItem('noticePreviewZoom') || '1';
    setZoom(savedZoom);
    $$('.zoom-btn').forEach((button) =>
      button.addEventListener('click', () => setZoom(button.dataset.zoom))
    );
    document.addEventListener('keydown', (event) => {
      if (event.ctrlKey && event.key.toLowerCase() === 'f') {
        event.preventDefault();
        $('#search-input').focus();
        return;
      }
      if (!$('#notice-drawer').classList.contains('open')) return;
      if (event.key === 'Enter' && document.activeElement.id === 'drawer-reference') {
        event.preventDefault();
        saveDrawer(false);
      }
      if (event.altKey && event.key === 'ArrowRight') navigate(1);
      if (event.altKey && event.key === 'ArrowLeft') navigate(-1);
      if (event.key === 'Escape') closeDrawer();
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    initUpload();
    bindEvents();
    if (!$('#view-workspace').classList.contains('d-none')) refreshAll();
  });
}());
