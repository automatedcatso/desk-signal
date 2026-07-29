/* Lightweight API client for the Notice Studio. */
const API = {
  _url(path) {
    return `${window.APP_BASE || ''}${path}`;
  },

  async _json(url, opts = {}) {
    const res = await fetch(this._url(url), opts);
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-json */ }
    if (!res.ok && data.ok === undefined) {
      throw new Error(data.error || `Request failed (${res.status})`);
    }
    return data;
  },

  upload(file) {
    const fd = new FormData();
    fd.append('file', file);
    return this._json('/api/upload', { method: 'POST', body: fd });
  },

  stats() { return this._json('/api/records/stats'); },
  filters(layer = '') {
    const q = layer ? `?layer=${encodeURIComponent(layer)}` : '';
    return this._json(`/api/records/filters${q}`);
  },
  list(params) {
    const q = new URLSearchParams(params).toString();
    return this._json(`/api/records/list?${q}`);
  },
  record(id) { return this._json(`/api/records/${id}`); },
  updateRecord(id, body) {
    return this._json(`/api/records/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
  },
  bulkName(ids, name) {
    return this._json('/api/records/bulk-name', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, reference_name: name })
    });
  },
  preview(id, params) {
    const q = new URLSearchParams(params).toString();
    return this._json(`/api/records/preview/${id}?${q}`);
  },
  errorReport() { return this._json('/api/records/error-report'); },
  saveSignatory(body) {
    return this._json('/api/session/signatory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
  },
  clearSession() {
    return this._json('/api/session/clear', { method: 'POST' });
  },
  startGeneration(body) {
    return this._json('/api/generate/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
  },
  genStatus(jobId) { return this._json(`/api/generate/status/${jobId}`); },
  downloadUrl(jobId) { return this._url(`/api/generate/download/${jobId}`); }
};
