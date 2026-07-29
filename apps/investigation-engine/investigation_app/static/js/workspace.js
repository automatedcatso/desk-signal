/* Investigation Intelligence Engine - workspace controller.
 * Multi-tab navigation, evidence processing views, right-side analysis tabs,
 * export controls, guided mode and autosaved workspace state. Offline-only. */
(function () {
  "use strict";
  var API = window.IIE_BASE || "";
  var saveTimer = null;
  var selectedEvidenceFiles = [];
  var evidencePollTimer = null;
  var evidencePollRounds = 0;
  var wasEvidenceProcessing = false;
  var evidenceFetchInFlight = false;

  function freshState(caseUid) {
    return { caseUid: caseUid || null, tab: "overview", analysisTab: "summary", pinned: [], recentSearches: [] };
  }
  var state = freshState(null);

  function setCloseButtonEnabled(enabled) {
    var btn = document.getElementById("caseCloseBtn");
    if (btn) btn.disabled = !enabled;
  }

  function resetWorkspaceDisplay(message) {
    state = freshState(null);
    selectedEvidenceFiles = [];
    clearTimeout(saveTimer);
    if (evidencePollTimer) { clearTimeout(evidencePollTimer); evidencePollTimer = null; }
    evidencePollRounds = 0;
    wasEvidenceProcessing = false;
    var sel = document.getElementById("casePicker");
    if (sel) sel.value = "";
    var evInput = document.getElementById("evUpload");
    if (evInput) evInput.value = "";
    var selected = document.getElementById("evSelectedFile");
    if (selected) { selected.style.display = "none"; selected.innerHTML = ""; }
    var evList = document.querySelector("#pane-evidence .iie-list");
    if (evList) evList.innerHTML = "";
    var outBox = document.getElementById("analysisOut");
    if (outBox) outBox.innerHTML = '<div class="iie-muted">Select or create an investigation to begin.</div>';
    var searchOut = document.getElementById("searchResults");
    if (searchOut) searchOut.innerHTML = "";
    var reportOut = document.getElementById("reportOut");
    if (reportOut) reportOut.textContent = "";
    setEvidenceLoading(null);
    setCloseButtonEnabled(false);
    selectTab("overview");
    var pane = document.getElementById("pane-overview");
    if (pane) pane.innerHTML = '<div class="iie-muted">' + esc(message || "Investigation closed. Create a new investigation or select another case.") + '</div>';
  }

  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>\"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '\"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtBytes(n) {
    if (!n) return "0 B";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + u[i];
  }
  function money(v) {
    if (v == null || v === "") return "N/A";
    var n = Number(v);
    return isNaN(n) ? esc(v) : "₹" + n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  function fetchJson(url, fallback) {
    return fetch(url).then(function (r) { return r.ok ? r.json() : fallback; }).catch(function () { return fallback; });
  }
  function exportUrl(fmt, kind) {
    kind = kind || (($("#reportKind") && $("#reportKind").value) || "investigation");
    return API + "/api/ai/" + encodeURIComponent(state.caseUid || "") + "/report?kind=" + encodeURIComponent(kind) + "&format=" + fmt + "&download=1";
  }
  function exportLinks(kind) {
    if (!state.caseUid) return "";
    return ["md", "json", "pdf", "docx"].map(function (fmt) {
      return '<a href="' + esc(exportUrl(fmt, kind)) + '">' + fmt.toUpperCase() + '</a>';
    }).join("");
  }

  function setEvidenceLoading(message, active, progress) {
    var box = $("#evLoadingStatus");
    var text = $("#evLoadingText");
    var bar = $("#evLoadingBar");
    var pct = $("#evLoadingPct");
    if (!box) return;
    if (!message) {
      box.style.display = "none";
      box.classList.remove("is-active", "is-indeterminate");
      if (text) text.textContent = "";
      if (bar) bar.style.width = "0%";
      if (pct) pct.textContent = "";
      return;
    }
    box.style.display = "block";
    box.classList.toggle("is-active", active !== false);
    var hasProgress = typeof progress === "number" && isFinite(progress);
    box.classList.toggle("is-indeterminate", active !== false && !hasProgress);
    if (text) text.textContent = message;
    if (bar) {
      var value = hasProgress ? Math.max(0, Math.min(100, progress)) : 0;
      bar.style.width = hasProgress ? value.toFixed(0) + "%" : "";
    }
    if (pct) pct.textContent = hasProgress ? Math.max(0, Math.min(100, progress)).toFixed(0) + "%" : "";
  }


  function evidenceProgressValue(e) {
    var raw = Number(e && e.progress_percent);
    if (!isFinite(raw)) raw = 0;
    if (/^(AI_READY|COMPLETED|DONE)$/i.test(String((e && e.status) || ""))) raw = 100;
    return Math.max(0, Math.min(100, raw));
  }

  function processingSummary(items) {
    var active = (items || []).filter(function (e) {
      return /^(pending|processing|queued|extracting|structuring|indexing)$/i.test(String(e.status || ""));
    });
    if (!active.length) return null;
    var totalPct = active.reduce(function (sum, e) { return sum + evidenceProgressValue(e); }, 0);
    var avg = totalPct / active.length;
    var lead = active.slice().sort(function (a, b) { return evidenceProgressValue(a) - evidenceProgressValue(b); })[0];
    var detail = (lead && lead.progress_detail) ? String(lead.progress_detail) : "Processing evidence";
    var done = Math.round(avg);
    var left = Math.max(0, 100 - done);
    return {
      active: active,
      percent: avg,
      message: detail + " — " + done + "% done, " + left + "% left" + (active.length > 1 ? " (" + active.length + " active files)" : "")
    };
  }

  /* ---- session autosave / restore ---- */
  function scheduleSave() {
    if (!state.caseUid) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      fetch(API + "/api/workspace/" + state.caseUid, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state)
      }).catch(function () {});
    }, 600);
  }
  function restore(caseUid, cb) {
    state = freshState(caseUid);
    fetch(API + "/api/workspace/" + caseUid).then(function (r) { return r.json(); })
      .then(function (s) {
        if (s && s.tab) { state = Object.assign(freshState(caseUid), s); }
        state.caseUid = caseUid;
        setCloseButtonEnabled(!!caseUid);
        cb && cb();
      }).catch(function () { state.caseUid = caseUid; setCloseButtonEnabled(!!caseUid); cb && cb(); });
  }

  /* ---- virtualised list ---- */
  function virtualList(container, items, rowHtml, rowH) {
    if (!container) return;
    rowH = rowH || 44;
    container.innerHTML = "";
    var viewport = el("div", "iie-vp");
    viewport.style.cssText = "position:relative;overflow:auto;max-height:52vh";
    var spacer = el("div");
    spacer.style.height = (items.length * rowH) + "px";
    var layer = el("div");
    layer.style.cssText = "position:absolute;top:0;left:0;right:0";
    viewport.appendChild(spacer);
    viewport.appendChild(layer);
    container.appendChild(viewport);
    function render() {
      var top = viewport.scrollTop;
      var start = Math.max(0, Math.floor(top / rowH) - 5);
      var end = Math.min(items.length, Math.ceil((top + viewport.clientHeight) / rowH) + 5);
      layer.style.transform = "translateY(" + (start * rowH) + "px)";
      layer.innerHTML = "";
      for (var i = start; i < end; i++) {
        var row = el("div", "iie-row", rowHtml(items[i]));
        row.style.height = rowH + "px";
        layer.appendChild(row);
      }
    }
    viewport.addEventListener("scroll", render);
    render();
  }

  /* ---- top tabs ---- */
  var TABS = ["overview", "evidence", "analysis", "search", "report"];

  function selectTab(t) {
    if (TABS.indexOf(t) === -1) t = "overview";
    state.tab = t;
    TABS.forEach(function (name) {
      var btn = document.getElementById("tab-" + name);
      var pane = document.getElementById("pane-" + name);
      if (btn) btn.classList.toggle("active", name === t);
      if (pane) pane.style.display = name === t ? "block" : "none";
    });
    scheduleSave();
    loadTab(t);
  }

  function loadTab(t) {
    if (!state.caseUid) return;
    if (t === "evidence") loadEvidence();
    else if (t === "analysis") loadAnalysis(state.analysisTab || "summary");
    else if (t === "report") loadReport();
  }

  function aiRow(role, label, content) {
    var cls = role === "user" ? "iie-ai-row user" : "iie-ai-row";
    return el("div", cls,
      '<span class="who">' + esc(label) + '</span>' +
      '<span class="msg">' + esc(content) + '</span>');
  }

  function syncEvidenceInputFiles() {
    var input = $("#evUpload");
    if (!input) return;
    try {
      var dt = new DataTransfer();
      selectedEvidenceFiles.forEach(function (f) { dt.items.add(f); });
      input.files = dt.files;
    } catch (e) {
      // Older browsers may not allow assigning FileList. The selectedEvidenceFiles
      // array remains the source of truth for upload in this app.
    }
  }

  function clearSelectedEvidenceFile() {
    var input = $("#evUpload");
    var box = $("#evSelectedFile");
    selectedEvidenceFiles = [];
    if (input) input.value = "";
    if (box) { box.style.display = "none"; box.innerHTML = ""; }
  }

  function removeSelectedEvidenceFile(index) {
    selectedEvidenceFiles.splice(index, 1);
    syncEvidenceInputFiles();
    renderSelectedEvidenceFiles();
  }

  function renderSelectedEvidenceFiles() {
    var box = $("#evSelectedFile");
    if (!box) return;
    if (!selectedEvidenceFiles.length) {
      box.style.display = "none";
      box.innerHTML = "";
      return;
    }
    box.style.display = "flex";
    box.innerHTML = '<div class="iie-selected-head">' + selectedEvidenceFiles.length + ' file(s) ready for safe scan</div>' +
      selectedEvidenceFiles.map(function (f, i) {
        return '<span class="iie-selected-chip"><span class="iie-selected-name">' + esc(f.name) + '</span>' +
          '<span class="iie-selected-size">' + fmtBytes(f.size) + '</span>' +
          '<button type="button" class="iie-mini-x" data-selected-index="' + i + '" title="Remove selected file" aria-label="Remove selected file">&times;</button></span>';
      }).join("");
    $all("#evSelectedFile [data-selected-index]").forEach(function (btn) {
      btn.addEventListener("click", function () { removeSelectedEvidenceFile(Number(btn.getAttribute("data-selected-index"))); });
    });
  }

  function updateSelectedEvidenceFile() {
    var input = $("#evUpload");
    if (!input || !input.files) return;
    var incoming = Array.prototype.slice.call(input.files);
    incoming.forEach(function (f) {
      var key = f.name + "::" + f.size + "::" + f.lastModified;
      var exists = selectedEvidenceFiles.some(function (x) { return (x.name + "::" + x.size + "::" + x.lastModified) === key; });
      if (!exists) selectedEvidenceFiles.push(f);
    });
    syncEvidenceInputFiles();
    renderSelectedEvidenceFiles();
  }


  function reprocessAllEvidence() {
    if (!state.caseUid) { window.alert("Select or create an investigation first."); return; }
    if (!window.confirm("Re-run extraction on all already uploaded evidence in this case?\n\nThis preserves originals and refreshes entities, transactions, search indexes and AI chunks.")) return;
    var btn = $("#evReprocessAllBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Queuing..."; }
    setEvidenceLoading("Queuing all evidence for reprocessing…", true);
    fetch(API + "/api/evidence/" + state.caseUid + "/reprocess-all", { method: "POST" })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) { window.alert((res.d && res.d.error) || "Could not queue reprocess."); return; }
        window.alert((res.d.queued || 0) + " evidence item(s) queued for reprocessing.");
        loadEvidence(); refreshOverview();
        if (state.tab === "analysis") loadAnalysis(state.analysisTab || "summary");
      })
      .catch(function () { window.alert("Could not queue reprocess - is the engine still running?"); })
      .finally(function () { if (btn) { btn.disabled = false; btn.textContent = "Reprocess Existing Evidence"; } loadEvidence(); });
  }

  function reprocessOneEvidence(id, label) {
    if (!state.caseUid || !id) return;
    if (!window.confirm("Re-run extraction for this evidence?\n\n" + label)) return;
    setEvidenceLoading("Queuing evidence for reprocessing…", true);
    fetch(API + "/api/evidence/" + state.caseUid + "/" + encodeURIComponent(id) + "/reprocess", { method: "POST" })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) { window.alert((res.d && res.d.error) || "Could not queue reprocess."); return; }
        loadEvidence(); refreshOverview();
      })
      .catch(function () { window.alert("Could not queue reprocess - is the engine still running?"); });
  }

  function deleteEvidence(evidenceId, evidenceName) {
    if (!state.caseUid || !evidenceId) return;
    var label = evidenceName || ("Evidence #" + evidenceId);
    if (!window.confirm("Remove this evidence from the investigation?\n\n" + label + "\n\nThis cleans its extracted entities, transactions, timeline entries and similarity links from this case.")) return;
    setEvidenceLoading("Removing evidence and cleaning extracted data…", true);
    fetch(API + "/api/evidence/" + encodeURIComponent(state.caseUid) + "/" + encodeURIComponent(evidenceId), { method: "DELETE" })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) { window.alert((res.d && res.d.error) || "Could not remove evidence."); return; }
        loadEvidence();
        refreshOverview();
        if (state.tab === "analysis") loadAnalysis(state.analysisTab || "summary");
      })
      .catch(function () { window.alert("Could not remove evidence - is the engine still running?"); });
  }

  function uploadEvidence() {
    if (!state.caseUid) { window.alert("Select or create an investigation first."); return; }
    var input = $("#evUpload");
    var files = selectedEvidenceFiles.length ? selectedEvidenceFiles.slice() : (input && input.files ? Array.prototype.slice.call(input.files) : []);
    if (!files.length) {
      window.alert("Choose one or more files to import first.");
      return;
    }
    var btn = $("#evUploadBtn");
    var done = 0, failed = 0;
    setEvidenceLoading("Importing evidence files", true, 0);
    if (btn) { btn.disabled = true; btn.textContent = "Importing 0/" + files.length + "..."; }

    function uploadOne(file) {
      var fd = new FormData();
      fd.append("file", file);
      return fetch(API + "/api/evidence/" + state.caseUid + "/upload", { method: "POST", body: fd })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          if (!res.ok) { failed += 1; return; }
          done += 1;
        })
        .catch(function () { failed += 1; })
        .finally(function () {
          var current = done + failed;
          if (btn) btn.textContent = "Importing " + current + "/" + files.length + "...";
          setEvidenceLoading("Importing " + current + "/" + files.length + " evidence file(s)", true, Math.round((current / files.length) * 100));
        });
    }

    files.reduce(function (p, f) { return p.then(function () { return uploadOne(f); }); }, Promise.resolve())
      .then(function () {
        clearSelectedEvidenceFile();
        loadEvidence();
        refreshOverview();
        if (state.tab === "analysis") loadAnalysis(state.analysisTab || "summary");
        if (failed) window.alert(done + " file(s) imported, " + failed + " failed. Check file permissions/engine logs for failed items.");
      })
      .finally(function () { if (btn) { btn.disabled = false; btn.textContent = "Import Evidence"; } });
  }


  function scheduleEvidencePoll(hasProcessing) {
    if (!hasProcessing) {
      if (evidencePollTimer) { clearTimeout(evidencePollTimer); evidencePollTimer = null; }
      if (!evidenceFetchInFlight) setEvidenceLoading(null);
      if (wasEvidenceProcessing) {
        wasEvidenceProcessing = false;
        evidencePollRounds = 0;
        refreshOverview();
        // Heavy panes can involve thousands of rows for big Excel files. Refresh
        // them once after processing completes instead of every poll cycle.
        if (state.tab === "analysis") loadAnalysis(state.analysisTab || "summary");
      }
      return;
    }
    wasEvidenceProcessing = true;
    setEvidenceLoading("Evidence processing is running in the background", true);
    if (evidencePollTimer || evidencePollRounds > 120) return;
    evidencePollRounds += 1;
    var delay = evidencePollRounds < 10 ? 2500 : evidencePollRounds < 40 ? 5000 : 9000;
    evidencePollTimer = setTimeout(function () {
      evidencePollTimer = null;
      loadEvidence();
      refreshOverview();
    }, delay);
  }

  function loadEvidence() {
    if (!state.caseUid) return;
    if (evidenceFetchInFlight) return;
    evidenceFetchInFlight = true;
    setEvidenceLoading("Loading evidence list", true);
    fetchJson(API + "/api/evidence/" + state.caseUid, [])
      .then(function (items) {
        items = items || [];
        var summary = processingSummary(items);
        var hasProcessing = !!summary;
        virtualList($("#pane-evidence .iie-list"), items, function (e) {
          var pct = evidenceProgressValue(e);
          var isActive = /^(pending|processing|queued|extracting|structuring|indexing)$/i.test(String(e.status || ""));
          var pctHtml = isActive ? ' <span class="iie-chip">' + Math.round(pct) + '%</span>' : '';
          var detail = e.progress_detail ? ' &middot; ' + esc(e.progress_detail) : '';
          return '<span class="t">' + esc(e.original_name) + (e.transaction_count ? ' <span class="iie-chip">' + e.transaction_count + ' txns</span>' : '') + pctHtml + '</span>' +
            '<span class="d">' + esc(e.status) + ' &middot; ' + fmtBytes(e.size) + detail + '</span>' +
            '<button type="button" class="iie-mini-action iie-ev-reprocess" data-evidence-id="' + esc(e.id) + '" data-evidence-name="' + esc(e.original_name) + '" title="Reprocess evidence">↻</button>' +
            '<button type="button" class="iie-mini-x iie-ev-remove" data-evidence-id="' + esc(e.id) + '" data-evidence-name="' + esc(e.original_name) + '" title="Remove evidence" aria-label="Remove evidence">&times;</button>';
        }, 52);
        if (summary) {
          setEvidenceLoading(summary.message, true, summary.percent);
        } else {
          setEvidenceLoading(null);
        }
        scheduleEvidencePoll(hasProcessing);
      })
      .catch(function () {
        setEvidenceLoading("Could not refresh evidence list. The engine may still be busy; try again in a moment.", false, 100);
      })
      .finally(function () { evidenceFetchInFlight = false; });
  }

  function loadReport() {
    var kind = ($("#reportKind") && $("#reportKind").value) || "investigation";
    var links = $("#reportExportLinks");
    if (links) links.innerHTML = exportLinks(kind);
    fetch(API + "/api/ai/" + state.caseUid + "/report?kind=" + encodeURIComponent(kind))
      .then(function (r) { return r.text(); })
      .then(function (md) { var out = $("#reportOut"); if (out) out.textContent = md; }).catch(function () {});
  }

  function doSearch() {
    var q = $("#searchInput").value.trim();
    if (!q) return;
    if (state.recentSearches.indexOf(q) === -1) {
      state.recentSearches.unshift(q);
      state.recentSearches = state.recentSearches.slice(0, 10);
      scheduleSave();
    }
    fetchJson(API + "/api/evidence/" + state.caseUid + "/search?q=" + encodeURIComponent(q), []).then(function (rows) {
      var box = $("#searchResults");
      box.innerHTML = rows.length ? "" : '<div class="iie-muted">No matches.</div>';
      rows.forEach(function (m) {
        box.appendChild(el("div", "iie-row",
          '<span class="d">' + esc(m.ref_type) + ' #' + m.ref_id + '</span>' +
          '<span class="t">' + esc(m.snippet || "") + '</span>'));
      });
    });
  }

  /* ---- right-side Analysis tabs ---- */
  function setAnalysisActive(name) {
    var changed = state.analysisTab !== name;
    state.analysisTab = name;
    $all(".iie-atab").forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-analysis-tab") === name); });
    if (changed) scheduleSave();
  }
  function out(html) {
    var box = $("#analysisOut");
    if (box) {
      box.classList.toggle("iie-analysis-out-ai", state.analysisTab === "ai");
      box.innerHTML = html;
    }
  }
  function kpi(label, n) {
    return '<div class="iie-kpi"><div class="n">' + esc(n == null ? 0 : n) + '</div><div class="label">' + esc(label) + '</div></div>';
  }
  function renderTable(cols, rows, maxRows) {
    if (!rows || !rows.length) return '<div class="iie-muted">No records yet.</div>';
    maxRows = maxRows || 500;
    var total = rows.length;
    var shown = rows.slice(0, maxRows);
    var note = total > shown.length
      ? '<div class="iie-analysis-card"><b>Large result set:</b> showing first ' + shown.length + ' of ' + total + ' rows to keep the UI responsive. Use AI filters, Search, or JSON export for full data.</div>'
      : '';
    return note + '<div class="iie-table-scroll"><table><thead><tr>' + cols.map(function (c) { return '<th>' + esc(c[0]) + '</th>'; }).join("") + '</tr></thead><tbody>' +
      shown.map(function (r) {
        return '<tr>' + cols.map(function (c) {
          var v = typeof c[1] === "function" ? c[1](r) : r[c[1]];
          return '<td>' + esc(v == null || v === "" ? "N/A" : v) + '</td>';
        }).join("") + '</tr>';
      }).join("") + '</tbody></table></div>';
  }
  function loadAnalysis(name) {
    if (!state.caseUid) { out('<div class="iie-muted">Select an investigation first.</div>'); return; }
    setAnalysisActive(name || "summary");
    out('<div class="iie-muted">Loading ' + esc(state.analysisTab) + '...</div>');
    if (state.analysisTab === "summary") renderAnalysisSummary();
    else if (state.analysisTab === "evidence") renderAnalysisEvidence();
    else if (state.analysisTab === "entities") renderAnalysisEntities();
    else if (state.analysisTab === "transactions") renderAnalysisTransactions();
    else if (state.analysisTab === "messages") renderAnalysisMessages();
    else if (state.analysisTab === "social") renderAnalysisSocialProfiles();
    else if (state.analysisTab === "timeline") renderAnalysisTimeline();
    else if (state.analysisTab === "technical") renderAnalysisTechnicalIndicators();
    else if (state.analysisTab === "duplicates" || state.analysisTab === "similar") renderAnalysisSimilar();
    else if (state.analysisTab === "relationships") renderAnalysisRelationships();
    else if (state.analysisTab === "ai") renderAnalysisAI();
    else if (state.analysisTab === "export") renderAnalysisExport();
  }
  function renderAnalysisSummary() {
    Promise.all([
      fetchJson(API + "/api/dashboard/summary", {}),
      fetchJson(API + "/api/evidence/" + state.caseUid, []),
      fetchJson(API + "/api/evidence/" + state.caseUid + "/transactions", []),
      fetchJson(API + "/api/evidence/" + state.caseUid + "/duplicates", []),
      fetchJson(API + "/api/evidence/" + state.caseUid + "/messages", []),
      fetchJson(API + "/api/evidence/" + state.caseUid + "/social-profiles", []),
      fetchJson(API + "/api/evidence/" + state.caseUid + "/technical-indicators", [])
    ]).then(function (all) {
      var d = all[0], ev = all[1], tx = all[2], sim = all[3], msg = all[4], social = all[5], tech = all[6];
      var html = '<h3>Case Intelligence Summary</h3>' +
        '<div class="iie-kpi-grid">' +
        kpi("Evidence", ev.length) + kpi("AI Ready", d.ai_ready_evidence || 0) + kpi("Transactions", tx.length) + kpi("Messages", msg.length) +
        kpi("Social Profiles", social.length) + kpi("Technical Indicators", tech.length) + kpi("Similarity Links", sim.length) +
        kpi("Entities", d.entity_count || 0) + kpi("UTRs", d.utrs || 0) + kpi("Accounts", d.accounts || 0) + kpi("Banks", d.banks || 0) +
        '</div>';
      html += '<h4>Evidence Status</h4>' + renderTable([
        ["ID", "id"], ["File", "original_name"], ["Types", function (r) { return (r.evidence_types || []).join(", "); }], ["Status", "status"], ["Txns", "transaction_count"], ["Msgs", "message_count"], ["Social", "social_profile_count"], ["Tech", "technical_indicator_count"], ["Summary", "summary"]
      ], ev.slice(0, 40));
      out(html);
    });
  }
  function renderAnalysisEvidence() {
    fetchJson(API + "/api/evidence/" + state.caseUid, []).then(function (items) {
      out('<h3>Processed Evidence</h3>' + renderTable([
        ["ID", "id"], ["File", "original_name"], ["Status", "status"], ["Types", function (r) { return (r.evidence_types || []).join(", "); }],
        ["Size", function (r) { return fmtBytes(r.size); }], ["Txns", "transaction_count"], ["Msgs", "message_count"],
        ["Social", "social_profile_count"], ["Tech", "technical_indicator_count"], ["Summary", "summary"]
      ], items));
    });
  }

  function renderAnalysisEntities() {
    fetchJson(API + "/api/evidence/" + state.caseUid + "/entities", []).then(function (items) {
      out('<h3>Extracted Entities</h3>' +
        '<div class="iie-analysis-card"><b>Total entities:</b> ' + (items || []).length + '. This tab renders the full stored entity set, not only the first 500 rows.</div>' +
        renderTable([
          ["Type", "type"], ["Value", "value"], ["Normalized", "norm"], ["Evidence Links", "links"]
        ], items, Infinity));
    });
  }
  function renderAnalysisTransactions() {
    fetchJson(API + "/api/evidence/" + state.caseUid + "/transactions", []).then(function (items) {
      out('<h3>Transactions / Money Trail</h3>' + renderTable([
        ["Layer", "layer"], ["Date", "txn_date"], ["UTR", "utr"], ["Amount", function (r) { return money(r.amount); }],
        ["Sender", "sender_account"], ["Receiver/Account", function (r) { return r.receiver_account || r.account_no; }],
        ["IFSC", "ifsc"], ["Bank", "bank"], ["Status", "status"], ["Source", function (r) { return "Evidence #" + r.evidence_id + " " + (r.original_name || r.source_file || "") + " " + (r.source_ref || ""); }]
      ], items));
    });
  }
  function renderAnalysisMessages() {
    fetchJson(API + "/api/evidence/" + state.caseUid + "/messages", []).then(function (items) {
      out('<h3>Messages / Communication Trail</h3>' + renderTable([
        ["Time", "timestamp"], ["Platform", "platform"], ["Sender", "sender"],
        ["Message", function (r) { return String(r.message_text || "").slice(0, 220); }],
        ["Risk Flags", function (r) { return (r.risk_flags || []).join(", "); }],
        ["URLs", function (r) { return (r.urls || []).join(", "); }],
        ["Source", function (r) { return "Evidence #" + r.evidence_id + " " + (r.original_name || "") + " " + (r.source_ref || ""); }]
      ], items));
    });
  }
  function renderAnalysisSocialProfiles() {
    fetchJson(API + "/api/evidence/" + state.caseUid + "/social-profiles", []).then(function (items) {
      out('<h3>Social Profiles / Handles</h3>' + renderTable([
        ["Platform", "platform"], ["Username", "username"], ["Profile URL", "profile_url"], ["Bio/About", "bio"],
        ["Confidence", "confidence"], ["Source", function (r) { return "Evidence #" + r.evidence_id + " " + (r.original_name || ""); }]
      ], items));
    });
  }
  function renderAnalysisTechnicalIndicators() {
    fetchJson(API + "/api/evidence/" + state.caseUid + "/technical-indicators", []).then(function (items) {
      out('<h3>Technical / Forensic Indicators</h3>' + renderTable([
        ["Type", "type"], ["Value", "value"], ["Normalized", "norm"], ["Confidence", "confidence"],
        ["Source", function (r) { return "Evidence #" + r.evidence_id + " " + (r.original_name || "") + " " + (r.source_ref || ""); }]
      ], items));
    });
  }

  function renderAnalysisTimeline() {
    fetchJson(API + "/api/ai/" + state.caseUid + "/timeline", []).then(function (items) {
      out('<h3>Timeline</h3>' + renderTable([
        ["Timestamp", "ts"], ["Kind", "kind"], ["Evidence", "evidence_id"], ["Summary", "summary"]
      ], items));
    });
  }
  function reasonText(reasons) {
    reasons = reasons || [];
    if (!reasons.length) return "";
    return reasons.map(function (r) {
      if (r.values && r.values.length) return (r.label || r.type) + ": " + r.values.slice(0, 8).join(", ");
      if (r.score != null) return (r.label || r.type) + ": " + r.score;
      return r.label || r.type || "reason";
    }).join("; ");
  }
  function renderAnalysisSimilar() {
    fetchJson(API + "/api/evidence/" + state.caseUid + "/duplicates", []).then(function (items) {
      out('<h3>Similar / Linked Evidence</h3>' + renderTable([
        ["Evidence A", function (r) { return "#" + r.a_id + " " + (r.a_name || ""); }],
        ["Evidence B", function (r) { return "#" + r.b_id + " " + (r.b_name || ""); }],
        ["Score", function (r) { return Math.round(Number(r.score || 0) * 1000) / 10 + "%"; }],
        ["Kind", "kind"], ["Reasons", function (r) { return reasonText(r.reasons); }]
      ], items));
    });
  }
  function renderAnalysisRelationships() {
    fetchJson(API + "/api/evidence/" + state.caseUid + "/graph", { nodes: [], edges: [] }).then(function (g) {
      var nodes = {};
      (g.nodes || []).forEach(function (n) { nodes[n.id] = n.type + ": " + n.value; });
      var rows = (g.edges || []).map(function (e) { return { src: nodes[e.src] || e.src, dst: nodes[e.dst] || e.dst, weight: e.weight }; });
      out('<h3>Entity Relationship Graph</h3>' + renderTable([
        ["Source", "src"], ["Target", "dst"], ["Weight", "weight"]
      ], rows));
    });
  }
  function renderAnalysisAI() {
    out('<h3>AI Generated Analysis</h3>' +
      '<div class="iie-analysis-card">Ask about any uploaded evidence, not only Excel: PDFs, DOCX/PPTX, screenshots/images via OCR, emails/EML, HTML, TXT/logs, CSV/XLSX, archives, entities, messages, technical indicators and transactions. Direct financial questions are answered from structured data first. Smart/Deep can use either your local assistant or Gemini 3.1 Flash-Lite.</div>' +
      '<div class="iie-ai-answer" id="analysisAIOut">No AI output yet.</div>' +
      '<div class="iie-ai-composer" id="analysisAIComposer">' +
        '<div class="iie-field iie-ai-tools">' +
          '<label class="iie-ai-control"><span>Provider</span><select id="analysisAIProvider" class="iie-select"><option value="local">Local AI</option><option value="gemini">Gemini 3.1 Flash-Lite</option></select></label>' +
          '<label class="iie-ai-control"><span>Depth</span>' +
          '<select id="analysisAIMode" class="iie-select"><option value="standard">Standard</option><option value="smart">Smart</option><option value="deep">Deep</option></select>' +
          '</label>' +
          '<button class="iie-btn primary" id="analysisAIBtn">Generate</button>' +
        '</div>' +
        '<textarea id="analysisAIQuery" class="iie-input iie-ai-query" rows="3" placeholder="Ask about any uploaded evidence, PDF, screenshot, email, chat, URL, account, entity or transaction...">Summarize all uploaded evidence and important entities.</textarea>' +
        '<div class="iie-ai-provider-status" id="analysisAIProviderStatus">Checking provider configuration…</div>' +
        '<div class="iie-muted">Ctrl+Enter generates. Standard depth stays deterministic and does not call either provider. Selecting Gemini sends only the retrieved text context for this question to Google.</div>' +
      '</div>' +
      '<h4>Export AI/Report Analysis</h4><div class="iie-export-links">' + exportLinks("investigation") + '</div>');
    var btn = $("#analysisAIBtn");
    var queryBox = $("#analysisAIQuery");
    var providerSelect = $("#analysisAIProvider");
    var providerStatus = $("#analysisAIProviderStatus");
    var providerState = {};

    function updateProviderStatus() {
      var provider = (providerSelect && providerSelect.value) || "local";
      var info = providerState[provider];
      if (!providerStatus) return;
      if (!info) {
        providerStatus.textContent = provider === "gemini"
          ? "Gemini requires GEMINI_API_KEY in .env.local or your host environment."
          : "Local AI uses the assistant configured at 127.0.0.1:5003.";
        providerStatus.className = "iie-ai-provider-status";
        return;
      }
      providerStatus.textContent = info.available
        ? "Ready: " + info.detail
        : "Not ready: " + info.detail;
      providerStatus.className = "iie-ai-provider-status " + (info.available ? "is-ready" : "is-warning");
    }

    fetch(API + "/api/ai/status").then(function (r) { return r.json(); }).then(function (d) {
      providerState = d.providers || {};
      if (providerSelect && (d.default_provider === "local" || d.default_provider === "gemini")) {
        providerSelect.value = d.default_provider;
      }
      updateProviderStatus();
    }).catch(updateProviderStatus);
    if (providerSelect) providerSelect.addEventListener("change", updateProviderStatus);

    function runAI() {
      var q = (queryBox && queryBox.value || "").trim();
      var mode = $("#analysisAIMode").value;
      var provider = (providerSelect && providerSelect.value) || "local";
      var box = $("#analysisAIOut");
      if (!q) { if (queryBox) queryBox.focus(); return; }
      if (btn) { btn.disabled = true; btn.textContent = "Generating..."; }
      box.textContent = "Generating " + mode.toUpperCase() + " analysis with " +
        (provider === "gemini" ? "Gemini 3.1 Flash-Lite" : "Local AI") + "...";
      fetch(API + "/api/ai/" + state.caseUid + "/ask", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, mode: mode, provider: provider })
      }).then(function (r) { return r.json(); }).then(function (d) {
        var prefix = d.warning ? "Note: " + d.warning + "\n\n" : "";
        var providerLine = d.provider
          ? "Provider: " + d.provider + " · Mode: " + (d.mode || mode) + "\n\n"
          : "";
        box.textContent = prefix + providerLine + (d.answer || d.error || "");
      }).catch(function () {
        box.textContent = "AI request failed.";
      }).finally(function () {
        if (btn) { btn.disabled = false; btn.textContent = "Generate"; }
        if (queryBox) queryBox.focus();
      });
    }
    if (btn) btn.addEventListener("click", runAI);
    if (queryBox) queryBox.addEventListener("keydown", function (ev) {
      if (ev.ctrlKey && ev.key === "Enter") { ev.preventDefault(); runAI(); }
    });
  }
  function renderAnalysisExport() {
    out('<h3>Export AI Generated Analysis / Report</h3>' +
      '<div class="iie-analysis-card">Download the current investigation analysis in Markdown, JSON, PDF, or DOCX. These exports include evidence, entities, transactions, money trail, messages, social profiles, technical indicators, timeline, similar evidence and leads.</div>' +
      '<div class="iie-export-links">' + exportLinks("investigation") + '</div>');
  }

  /* ---- guided mode ---- */
  function initGuided() {
    fetch(API + "/api/workspace/settings/guided_hidden").then(function (r) { return r.json(); })
      .then(function (d) { if (d.value === "1") { var g = $("#guided"); if (g) g.style.display = "none"; } }).catch(function () {});
  }
  function hideGuided() {
    var g = $("#guided"); if (g) g.style.display = "none";
    fetch(API + "/api/workspace/settings/guided_hidden", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: "1" })
    }).catch(function () {});
  }

  /* ---- boot ---- */
  function loadCases(cb) {
    fetch(API + "/api/cases").then(function (r) { return r.json(); }).then(cb).catch(function () { cb([]); });
  }
  function refreshOverview() {
    fetchJson(API + "/api/dashboard/summary", {}).then(function (d) {
      $("#sActive").textContent = d.active_investigations || 0;
      $("#sEvidence").textContent = d.evidence_count || 0;
      $("#sTasks").textContent = d.pending_tasks || 0;
      $("#sStorage").textContent = fmtBytes(d.storage_bytes || 0);
    });
  }

  function populateCases(selectUid) {
    var sel = $("#casePicker");
    loadCases(function (cases) {
      sel.innerHTML = '<option value="">Select investigation...</option>';
      cases.forEach(function (c) {
        var o = el("option"); o.value = c.uid; o.textContent = c.title; sel.appendChild(o);
      });
      if (selectUid) {
        sel.value = selectUid;
        restore(selectUid, function () { selectTab(state.tab || "overview"); });
      } else {
        setCloseButtonEnabled(false);
      }
    });
  }

  function closeInvestigation() {
    if (!state.caseUid) { window.alert("Select an investigation first."); return; }
    var sel = $("#casePicker");
    var title = sel && sel.options && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex].textContent : state.caseUid;
    var msg = "Close this investigation permanently?\n\n" +
      "Investigation: " + title + "\n\n" +
      "This will remove uploaded evidence, extracted entities, transactions, messages, search index, AI chat, workspace state, timeline, relationships, reports data and local evidence files for this investigation.\n\n" +
      "This cannot be undone.";
    if (!window.confirm(msg)) return;
    var typed = window.prompt("Type CLOSE to permanently clear and close this investigation:");
    if (String(typed || "").trim().toUpperCase() !== "CLOSE") return;
    var btn = $("#caseCloseBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Closing..."; }
    fetch(API + "/api/cases/" + encodeURIComponent(state.caseUid) + "/close", { method: "DELETE" })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) {
          window.alert((res.d && res.d.error) || "Could not close investigation.");
          setCloseButtonEnabled(true);
          return;
        }
        resetWorkspaceDisplay("Investigation closed and all local case data cleared.");
        populateCases(null);
        refreshOverview();
      })
      .catch(function () {
        window.alert("Could not close investigation - is the engine still running?");
        setCloseButtonEnabled(true);
      })
      .finally(function () {
        if (btn) btn.textContent = "Close Investigation";
      });
  }

  function bindCasePicker() {
    var sel = $("#casePicker");
    var urlCase = new URLSearchParams(window.location.search).get("case");
    populateCases(urlCase || null);
    sel.addEventListener("change", function () {
      if (!sel.value) { resetWorkspaceDisplay("Select an investigation or create a new one."); return; }
      restore(sel.value, function () { selectTab(state.tab || "overview"); });
    });
    var cb = $("#caseCloseBtn");
    if (cb) cb.addEventListener("click", closeInvestigation);
    var nb = $("#caseNewBtn");
    if (nb) nb.addEventListener("click", function () {
      var title = window.prompt("Investigation title:");
      if (!title) return;
      fetch(API + "/api/cases", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title })
      }).then(function (r) { return r.json(); }).then(function (c) {
        refreshOverview();
        populateCases(c && c.uid);
      }).catch(function () {});
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    TABS.forEach(function (t) {
      var b = document.getElementById("tab-" + t);
      if (b) b.addEventListener("click", function () { selectTab(t); });
    });
    $all(".iie-atab").forEach(function (b) {
      b.addEventListener("click", function () { loadAnalysis(b.getAttribute("data-analysis-tab") || "summary"); });
    });
    var ar = $("#analysisRefresh"); if (ar) ar.addEventListener("click", function () { loadAnalysis(state.analysisTab || "summary"); });
    var bs = $("#searchBtn"); if (bs) bs.addEventListener("click", doSearch);
    var rb = $("#reportBtn"); if (rb) rb.addEventListener("click", loadReport);
    var rk = $("#reportKind"); if (rk) rk.addEventListener("change", loadReport);
    var eu = $("#evUploadBtn"); if (eu) eu.addEventListener("click", uploadEvidence);
    var er = $("#evReprocessAllBtn"); if (er) er.addEventListener("click", reprocessAllEvidence);
    var evInput = $("#evUpload"); if (evInput) evInput.addEventListener("change", updateSelectedEvidenceFile);
    var evList = $("#pane-evidence .iie-list");
    if (evList) evList.addEventListener("click", function (e) {
      var reTarget = e.target && e.target.closest ? e.target.closest(".iie-ev-reprocess") : null;
      if (reTarget) {
        e.preventDefault();
        e.stopPropagation();
        reprocessOneEvidence(reTarget.getAttribute("data-evidence-id"), reTarget.getAttribute("data-evidence-name"));
        return;
      }
      var target = e.target && e.target.closest ? e.target.closest(".iie-ev-remove") : null;
      if (!target) return;
      e.preventDefault();
      e.stopPropagation();
      deleteEvidence(target.getAttribute("data-evidence-id"), target.getAttribute("data-evidence-name"));
    });
    var si = $("#searchInput");
    if (si) si.addEventListener("keydown", function (e) { if (e.key === "Enter") doSearch(); });
    var gh = $("#guidedHide"); if (gh) gh.addEventListener("click", hideGuided);
    bindCasePicker();
    initGuided();
    refreshOverview();
  });
})();
