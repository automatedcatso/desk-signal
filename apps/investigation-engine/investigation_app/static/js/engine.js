/* Investigation Intelligence Engine - Stage 1 home view.
 * Talks only to this module's same-origin API. */
(function () {
  "use strict";
  var API = window.IIE_BASE || "";

  function fmtBytes(n) {
    if (!n) return "0 B";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + u[i];
  }

  function loadSummary() {
    fetch(API + "/api/dashboard/summary").then(function (r) { return r.json(); })
      .then(function (d) {
        document.getElementById("sActive").textContent = d.active_investigations || 0;
        document.getElementById("sEvidence").textContent = d.evidence_count || 0;
        document.getElementById("sTasks").textContent = d.pending_tasks || 0;
        document.getElementById("sStorage").textContent = fmtBytes(d.storage_bytes || 0);
        renderRecent(d.recent_investigations || []);
      }).catch(function () {});
  }

  function renderRecent(items) {
    var ul = document.getElementById("recentList");
    ul.innerHTML = "";
    if (!items.length) {
      ul.innerHTML = '<li class="empty">No investigations yet.</li>';
      return;
    }
    items.forEach(function (c) {
      var li = document.createElement("li");
      li.innerHTML = '<span class="t">' + escapeHtml(c.title) + '</span>' +
        '<span class="d">' + escapeHtml(c.updated_at || "") + '</span>';
      ul.appendChild(li);
    });
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function createInvestigation() {
    var title = window.prompt("Investigation title:");
    if (!title) return;
    fetch(API + "/api/cases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title })
    }).then(function (r) { return r.json(); }).then(loadSummary).catch(function () {});
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.getElementById("btnCreate").addEventListener("click", createInvestigation);
    document.getElementById("btnRefresh").addEventListener("click", loadSummary);
    loadSummary();
  });
})();
