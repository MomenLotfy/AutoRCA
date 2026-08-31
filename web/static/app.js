// ============================================================================
// AutoRCA Investigation Console — frontend
//
// All rendering is driven by the API. The UI is a presentation layer only.
//
// API endpoints consumed:
//   POST /api/v1/investigations        (create)
//   GET  /api/v1/investigations        (list)
//   GET  /api/v1/investigations/{id}   (full detail)
//   GET  /api/v1/investigations/{id}/evidence
//   GET  /api/v1/investigations/{id}/timeline
//   GET  /api/v1/investigations/{id}/graph
//   GET  /api/v1/investigations/{id}/correlation
//   GET  /api/v1/investigations/{id}/remediation
//   GET  /api/v1/investigations/{id}/fingerprint
//   GET  /api/v1/investigations/{id}/hypothesis
//   GET  /api/health                   (health)
//
// Legacy compatibility: /api/analyze is still served by the backend for the
// older workflow. The Investigation Console prefers the v1 surface.
// ============================================================================

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const text = (id, value) => { const el = $(id); if (el) el.textContent = value ?? "—"; };
const showStatus = (msg, kind) => {
  const s = $("#status"); s.hidden = false; s.className = `status ${kind || ""}`; s.textContent = msg;
};
const hideStatus = () => { const s = $("#status"); s.hidden = true; s.textContent = ""; };
const setHtml = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };

const SEVERITY_BUCKETS = {
  critical: ["critical"],
  high: ["high", "severe"],
  medium: ["medium", "moderate", "warning"],
  low: ["low", "info"],
};

function severityClass(sev) {
  const s = (sev || "unknown").toLowerCase();
  for (const [cls, list] of Object.entries(SEVERITY_BUCKETS)) {
    if (list.includes(s)) return cls;
  }
  return "unknown";
}

function pct(value) {
  if (value == null) return "—";
  const n = Math.round(Number(value) * 100);
  return Number.isFinite(n) ? `${n}%` : "—";
}

function short(value, max = 64) {
  if (!value) return "—";
  const s = String(value);
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(path, opts);
  let payload = null;
  try { payload = await resp.json(); } catch (e) { payload = null; }
  if (!resp.ok) {
    const msg = (payload && payload.error) || `HTTP ${resp.status}`;
    const err = new Error(msg);
    err.status = resp.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

// ============================================================================
// Investigation creation
// ============================================================================
const form = $("#investigation-form");
const submitButton = $("#submit-button");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submitButton.disabled = true;
  submitButton.querySelector("span").textContent = "Running pipeline…";
  hideStatus();
  showStatus("Collecting Git and file evidence, running deterministic pipeline…", "loading");

  const data = Object.fromEntries(new FormData(form).entries());
  data.no_diff = form.no_diff.checked;

  // Phase 2.1 — only forward an elasticsearch block when the URL is set.
  // This preserves Phase 1 behaviour when the field is empty.
  const esUrl = (data.elasticsearch_url || "").trim();
  if (esUrl) {
    const esBlock = { url: esUrl };
    const idx = (data.elasticsearch_index || "").trim();
    if (idx) esBlock.index_pattern = idx;
    const svc = (data.elasticsearch_service || "").trim();
    if (svc) esBlock.service = svc;
    const sz = parseInt(data.elasticsearch_size, 10);
    if (Number.isFinite(sz) && sz > 0) esBlock.size = sz;
    data.elasticsearch = esBlock;
  }
  delete data.elasticsearch_url;
  delete data.elasticsearch_index;
  delete data.elasticsearch_service;
  delete data.elasticsearch_size;

  // Phase 2.2 — only forward prometheus / github_changes / gitlab_changes
  // blocks when their primary identifier is set. Empty blocks must not
  // change the Phase 1 payload.
  const promUrl = (data.prometheus_url || "").trim();
  const promQuery = (data.prometheus_query || "").trim();
  if (promUrl && promQuery) {
    data.prometheus = { url: promUrl, query: promQuery };
  }
  delete data.prometheus_url;
  delete data.prometheus_query;

  const ghRepo = (data.github_repo || "").trim();
  if (ghRepo) {
    const ghBlock = { url: "https://api.github.com", resource: ghRepo };
    data.github_changes = ghBlock;
  }
  delete data.github_repo;

  const glProject = (data.gitlab_project || "").trim();
  if (glProject) {
    // Allow override via the same field; default to gitlab.com public
    // API URL when the caller didn't supply one.
    const glUrl = "https://gitlab.com";
    data.gitlab_changes = { url: glUrl, resource: glProject };
  }
  delete data.gitlab_project;

  // Phase 2.3 — Kubernetes + CI/CD integrations. Each block is only
  // forwarded when its primary identifier is set; absence preserves
  // Phase 1 behaviour exactly.
  const k8sUrl = (data.kubernetes_url || "").trim();
  const k8sNs = (data.kubernetes_namespace || "").trim();
  if (k8sUrl && k8sNs) {
    data.kubernetes = { url: k8sUrl, resource: k8sNs };
  }
  delete data.kubernetes_url;
  delete data.kubernetes_namespace;

  const ghActionsRepo = (data.github_actions_repo || "").trim();
  if (ghActionsRepo) {
    data.github_actions = {
      url: "https://api.github.com",
      resource: ghActionsRepo,
    };
  }
  delete data.github_actions_repo;

  const glCiProject = (data.gitlab_ci_project || "").trim();
  if (glCiProject) {
    data.gitlab_ci = { url: "https://gitlab.com", resource: glCiProject };
  }
  delete data.gitlab_ci_project;

  // Jenkins requires an explicit endpoint (it does not have a single
  // canonical public URL). Empty endpoint → block omitted.
  const jenkinsUrl = (data.jenkins_url || "").trim();
  const jenkinsJob = (data.jenkins_job || "").trim();
  if (jenkinsUrl && jenkinsJob) {
    data.jenkins = { url: jenkinsUrl, resource: jenkinsJob };
  }
  delete data.jenkins_url;
  delete data.jenkins_job;

  // Cross-cutting incident window / service hint, applied to every
  // integration that hasn't already supplied its own bounds.
  const incidentStart = (data.incident_start || "").trim();
  const incidentEnd = (data.incident_end || "").trim();
  const serviceHint = (data.service || "").trim();
  if (incidentStart || incidentEnd || serviceHint) {
    for (const key of [
      "elasticsearch", "prometheus", "github_changes", "gitlab_changes",
      "kubernetes", "github_actions", "gitlab_ci", "jenkins",
    ]) {
      const block = data[key];
      if (!block || typeof block !== "object") continue;
      if (incidentStart && !block.incident_start) {
        block.incident_start = incidentStart;
      }
      if (incidentEnd && !block.incident_end) {
        block.incident_end = incidentEnd;
      }
      if (serviceHint && !block.service) {
        block.service = serviceHint;
      }
    }
  }
  delete data.incident_start;
  delete data.incident_end;
  delete data.service;

  try {
    const result = await api("POST", "/api/v1/investigations", data);
    await loadInvestigations();
    openInvestigation(result.investigation_id);
    hideStatus();
  } catch (err) {
    showStatus(err.message || "Investigation failed.", "error");
  } finally {
    submitButton.disabled = false;
    submitButton.querySelector("span").textContent = "Run investigation";
  }
});

// ============================================================================
// Investigation list
// ============================================================================
async function loadInvestigations() {
  try {
    const data = await api("GET", "/api/v1/investigations");
    renderInvestigationList(data.investigations || []);
    $("#incidents-count").textContent = `${data.count || 0} incident${data.count === 1 ? "" : "s"}`;
  } catch (err) {
    setHtml("#incidents-list", `<div class="empty">Failed to load: ${err.message}</div>`);
  }
}

function renderInvestigationList(items) {
  const list = $("#incidents-list");
  if (!items.length) {
    list.innerHTML = `<div class="empty">No investigations yet. Run your first analysis above.</div>`;
    return;
  }
  list.innerHTML = "";
  items.forEach((inv) => {
    const row = document.createElement("div");
    row.className = "incident-row";
    row.dataset.investigationId = inv.investigation_id;
    row.innerHTML = `
      <span class="sev ${severityClass(inv.severity)}">${(inv.severity || "UNKNOWN").toUpperCase()}</span>
      <div>
        <div class="title">${escapeHtml(inv.root_cause || "No root cause")}</div>
        <div class="sub">${escapeHtml(inv.repository_full_name || inv.repository || "")} · ${escapeHtml(inv.environment || "")} · ${escapeHtml(inv.branch || "")}</div>
      </div>
      <span class="pill">${escapeHtml(inv.failure_type_id || "—")}</span>
      <span class="pill">${escapeHtml(inv.status)}</span>
      <span class="confidence">${pct(inv.confidence)}<small>confidence</small></span>
    `;
    row.addEventListener("click", () => openInvestigation(inv.investigation_id));
    list.appendChild(row);
  });
}

// ============================================================================
// Investigation detail
// ============================================================================
let currentInvestigation = null;

async function openInvestigation(id) {
  try {
    const data = await api("GET", `/api/v1/investigations/${id}`);
    currentInvestigation = data;
    renderDetail(data);
    const detail = $("#investigation-detail");
    detail.hidden = false;
    detail.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    showStatus(err.message, "error");
  }
}

$("#close-detail").addEventListener("click", () => {
  $("#investigation-detail").hidden = true;
  currentInvestigation = null;
});

// Tabs
$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.tab;
    $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.tab === target));
  });
});

function renderDetail(data) {
  const summary = data.incident_summary || {};
  text("#detail-id", data.investigation_id);
  text("#detail-root-cause", summary.root_cause || "No root cause");
  text("#detail-confidence", pct(summary.confidence));
  text("#detail-severity", (summary.severity || "UNKNOWN").toUpperCase());
  text("#detail-failure-type", summary.failure_type_id || "—");
  text("#detail-commit-sha", short(data.commit_sha, 16));
  text("#detail-repository", data.repository_full_name || data.repository || "—");
  text("#detail-environment", (data.environment || "").toUpperCase());
  text("#detail-branch", data.branch || "—");

  renderReasoningChain(data);
  renderFingerprint(data.fingerprint);
  renderCorrelation(data.correlation);
  renderEvidenceExplorer(data);
  renderTimeline(data.timeline);
  renderGraph(data.graph);
  renderHypotheses(data);
  renderRemediation(data.remediation);
  renderRawData(data);
}

// ---------------------------------------------------------------------------
// Reasoning chain — built from real observations / evidence
// ---------------------------------------------------------------------------
function renderReasoningChain(data) {
  const chain = $("#detail-reasoning-chain");
  const steps = [];

  // 1. Configuration change from git diff
  const diffEv = (data.evidence || []).find((e) => e.source === "git_diff");
  if (diffEv) {
    const envKey = pickEnvVar(diffEv);
    steps.push({
      icon: "config",
      title: envKey ? `Configuration change removed ${envKey}` : "Configuration change detected",
      body: `${short(diffEv.type, 80)} — ${short(diffEv.raw_reference, 90)}`,
    });
  }

  // 2. Runtime error
  const errObs = (data.observations || []).find((o) => /key_error|module_not_found_error|address_in_use_error/.test(o.kind));
  if (errObs) {
    const errName = obsKindLabel(errObs.kind);
    const detail = (errObs.data && (errObs.data.missing_key || errObs.data.module || errObs.data.port || errObs.data.address)) || "";
    steps.push({
      icon: "error",
      title: errName,
      body: detail ? `${errName}: ${short(detail, 80)}` : short(errObs.raw_reference, 120),
    });
  }

  // 3. Failure stage
  const fp = data.fingerprint;
  if (fp && fp.failure_stage && fp.failure_stage !== "unknown") {
    steps.push({
      icon: "failure",
      title: `Application startup failure`,
      body: `Failure stage: ${fp.failure_stage}`,
    });
  }

  // 4. Root cause (selected)
  if (data.selected_hypothesis) {
    const sel = data.selected_hypothesis;
    steps.push({
      icon: "cause",
      title: `Root cause: ${sel.label || sel.id}`,
      body: `${sel.id} · ${sel.failure_type_id} · score ${Number(sel.score || 0).toFixed(3)}`,
    });
  }

  if (!steps.length) {
    chain.innerHTML = `<div class="empty">No reasoning chain available for this investigation.</div>`;
    return;
  }

  chain.innerHTML = steps.map((s, i) => `
    <div class="reasoning-step">
      <div class="step-icon ${s.icon}">${i + 1}</div>
      <div class="step-body">
        <strong>${escapeHtml(s.title)}</strong>
        <small>${escapeHtml(s.body)}</small>
      </div>
    </div>
  `).join("");
}

function pickEnvVar(ev) {
  if (!ev || !ev.data) return null;
  const k = ev.data.key || ev.data.missing_key || ev.data.variable || ev.data.name;
  return k ? String(k) : null;
}

function obsKindLabel(kind) {
  return {
    key_error: "KeyError raised at runtime",
    module_not_found_error: "ModuleNotFoundError raised at runtime",
    address_in_use_error: "Port already in use",
    diff_removed_line: "Configuration line removed",
    diff_added_line: "Configuration line added",
    exit_code_nonzero: "Process exited non-zero",
    generic_log_line: "Runtime log entry",
  }[kind] || kind;
}

// ---------------------------------------------------------------------------
// Evidence explorer
// ---------------------------------------------------------------------------
function renderEvidenceExplorer(data) {
  const target = $("#evidence-explorer");
  const evidence = data.evidence || [];
  const selectedId = data.selected_hypothesis && data.selected_hypothesis.id;

  if (!evidence.length) {
    target.innerHTML = `<div class="empty">No evidence recorded.</div>`;
    return;
  }

  // Bucket evidence by relationship to the selected hypothesis
  const links = (data.selected_hypothesis && data.selected_hypothesis.links) || [];
  const supportIds = new Set(links.filter((l) => l.relation === "supports").map((l) => l.evidence_id));
  const contradictIds = new Set(links.filter((l) => l.relation === "contradicts").map((l) => l.evidence_id));

  const supporting = evidence.filter((e) => supportIds.has(e.id));
  const contradicting = evidence.filter((e) => contradictIds.has(e.id));
  const other = evidence.filter((e) => !supportIds.has(e.id) && !contradictIds.has(e.id));

  target.innerHTML = "";
  appendGroup(target, "Supporting evidence", supporting, "supporting", selectedId);
  appendGroup(target, "Contradicting evidence", contradicting, "contradicting", selectedId);
  appendGroup(target, "Other observations", other, "neutral", selectedId);
}

function appendGroup(root, label, items, klass, selectedId) {
  if (!items.length) return;
  const wrap = document.createElement("div");
  wrap.className = "evidence-group";
  wrap.innerHTML = `<h3>${escapeHtml(label)} (${items.length})</h3>`;
  items.forEach((e) => wrap.appendChild(evidenceItem(e, klass, selectedId)));
  root.appendChild(wrap);
}

function evidenceItem(e, klass, selectedId) {
  const row = document.createElement("div");
  row.className = `evidence-item ${klass}`;
  const supports = klass === "supporting" ? `Supports ${selectedId || ""}` :
                   klass === "contradicting" ? `Contradicts ${selectedId || ""}` :
                   "Adjacent evidence";
  row.innerHTML = `
    <div class="eid">${escapeHtml(e.id || "")}</div>
    <div class="ebody">
      <div class="esummary">${escapeHtml(e.type || "evidence")}</div>
      <div class="emeta">
        <span><strong>Source:</strong>${escapeHtml(e.source || "—")}</span>
        <span><strong>Reference:</strong>${escapeHtml(short(e.raw_reference, 80))}</span>
        <span><strong>Relation:</strong>${escapeHtml(supports)}</span>
      </div>
    </div>
  `;
  return row;
}

// ---------------------------------------------------------------------------
// Timeline
// ---------------------------------------------------------------------------
function renderTimeline(timeline) {
  const target = $("#timeline-list");
  const status = $("#timeline-status");
  if (!timeline || !timeline.events || !timeline.events.length) {
    target.innerHTML = `<div class="empty">No timeline events.</div>`;
    status.textContent = "—";
    return;
  }
  if (timeline.has_unknown_timestamps) {
    status.textContent = "Some timestamps unavailable";
  } else {
    status.textContent = `${timeline.events.length} events`;
  }
  target.innerHTML = "";
  timeline.events.forEach((ev) => {
    const row = document.createElement("div");
    row.className = "timeline-event";
    const whenClass = ev.timestamp_known ? "" : "unknown";
    const whenText = ev.timestamp_known ? ev.timestamp : "Timestamp unavailable";
    row.innerHTML = `
      <div>
        <div class="when ${whenClass}">${escapeHtml(whenText)}</div>
        <div class="etype">${escapeHtml(ev.event_type || "")}</div>
      </div>
      <div class="ebody">
        <strong>${escapeHtml(ev.description || ev.event_type || "")}</strong>
        <div class="edesc">${escapeHtml(short(ev.raw_reference, 140) || "")}</div>
        <div class="ref">${escapeHtml(ev.source || "")}</div>
      </div>
    `;
    target.appendChild(row);
  });
}

// ---------------------------------------------------------------------------
// Graph (inline SVG renderer)
// ---------------------------------------------------------------------------
const NODE_COLOR = {
  git_change: "#ffad70",
  configuration_file: "#7dc6ff",
  missing_environment_variable: "#7de2be",
  missing_dependency: "#c69cff",
  runtime_error: "#ff7b7b",
  port_binding: "#ffd07a",
  service_startup: "#7de2be",
  container_failure: "#ff7b7b",
  application_startup_failure: "#ff7b7b",
  default: "#7dc6ff",
};

const RELATION_COLOR = {
  leads_to: "#7de2be",
  triggers: "#ffad70",
  caused_by: "#ff7b7b",
  modifies: "#7dc6ff",
  references: "#c69cff",
};

function renderGraph(graph) {
  const target = $("#graph-canvas");
  const status = $("#graph-status");
  if (!graph || !graph.nodes || !graph.nodes.length) {
    target.innerHTML = `<div class="empty">Graph not available.</div>`;
    status.textContent = "—";
    return;
  }
  status.textContent = `${graph.node_count} nodes · ${graph.edge_count} edges`;

  // Simple layered layout — group by node type and place in columns.
  const layers = {};
  const order = [
    "git_change", "configuration_file", "missing_environment_variable", "missing_dependency",
    "port_binding", "service_startup", "application_startup_failure", "container_failure", "runtime_error",
  ];
  graph.nodes.forEach((n) => {
    const key = order.includes(n.type) ? n.type : "other";
    if (!layers[key]) layers[key] = [];
    layers[key].push(n);
  });

  const layerKeys = [...order.filter((k) => layers[k]), ...(layers.other ? ["other"] : [])];
  const colWidth = 220;
  const rowHeight = 70;
  const padding = 30;
  const positions = {};
  layerKeys.forEach((k, ci) => {
    layers[k].forEach((n, ri) => {
      positions[n.id] = { x: padding + ci * colWidth, y: padding + ri * rowHeight };
    });
  });

  const width = padding * 2 + layerKeys.length * colWidth;
  const rows = Math.max(...layerKeys.map((k) => layers[k].length), 1);
  const height = padding * 2 + rows * rowHeight;

  let svg = `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">`;
  svg += `<defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#7de2be"/>
    </marker>
  </defs>`;

  // Edges
  (graph.edges || []).forEach((e) => {
    const src = positions[e.source_node_id];
    const dst = positions[e.target_node_id];
    if (!src || !dst) return;
    const color = RELATION_COLOR[e.relation] || "#7de2be";
    svg += `<path d="M${src.x + 80},${src.y + 28} C${(src.x + dst.x) / 2},${src.y + 28} ${(src.x + dst.x) / 2},${dst.y + 28} ${dst.x},${dst.y + 28}" stroke="${color}" stroke-width="1.5" fill="none" marker-end="url(#arrow)" />`;
    svg += `<text x="${(src.x + dst.x) / 2}" y="${(src.y + dst.y) / 2}" fill="#8ca09e" font-family="DM Mono" font-size="10" text-anchor="middle">${escapeHtml(e.relation || "")}</text>`;
  });

  // Nodes
  graph.nodes.forEach((n) => {
    const pos = positions[n.id];
    if (!pos) return;
    const color = NODE_COLOR[n.type] || NODE_COLOR.default;
    svg += `<g transform="translate(${pos.x},${pos.y})">`;
    svg += `<rect width="160" height="56" rx="10" fill="#0d1a1f" stroke="${color}" stroke-width="1.5" />`;
    svg += `<text x="12" y="22" fill="${color}" font-family="DM Mono" font-size="10" letter-spacing="1">${escapeHtml(n.type || "")}</text>`;
    svg += `<text x="12" y="42" fill="#e7f0ee" font-family="Space Grotesk" font-size="13" font-weight="600">${escapeHtml(short(n.label || n.id, 22))}</text>`;
    svg += `</g>`;
  });
  svg += `</svg>`;

  // Legend
  const types = Array.from(new Set(graph.nodes.map((n) => n.type)));
  let legend = `<div class="graph-legend">`;
  types.forEach((t) => {
    legend += `<div class="leg"><i style="background:${NODE_COLOR[t] || NODE_COLOR.default}"></i>${escapeHtml(t)}</div>`;
  });
  legend += `</div>`;

  target.innerHTML = svg + legend;
}

// ---------------------------------------------------------------------------
// Hypotheses
// ---------------------------------------------------------------------------
function renderHypotheses(data) {
  const list = $("#hypothesis-list");
  const items = data.hypotheses || [];
  if (!items.length) {
    list.innerHTML = `<div class="empty">No hypotheses.</div>`;
    return;
  }
  list.innerHTML = "";
  items.forEach((h) => {
    const card = document.createElement("div");
    card.className = `hypothesis ${h.status || "candidate"}`;
    const supports = (h.links || []).filter((l) => l.relation === "supports").length;
    const contradicts = (h.links || []).filter((l) => l.relation === "contradicts").length;
    card.innerHTML = `
      <div class="htop">
        <div>
          <div class="htitle">${escapeHtml(h.label || h.id)}</div>
          <div class="hmeta">
            <span><strong>ID:</strong>${escapeHtml(h.id)}</span>
            <span><strong>Failure type:</strong>${escapeHtml(h.failure_type_id || "")}</span>
            <span><strong>Status:</strong>${escapeHtml(h.status)}</span>
          </div>
        </div>
        <div class="hscore">${Number(h.score || 0).toFixed(2)}<small>raw score</small></div>
      </div>
      <div class="hlinks">
        <span class="sup">+ ${supports} supporting</span>
        <span class="con">− ${contradicts} contradicting</span>
      </div>
    `;
    list.appendChild(card);
  });
}

// ---------------------------------------------------------------------------
// Remediation
// ---------------------------------------------------------------------------
function renderRemediation(remediation) {
  const target = $("#remediation-content");
  if (!remediation) {
    target.innerHTML = `<div class="empty">No remediation available for this investigation.</div>`;
    return;
  }
  const steps = (remediation.steps || []).map((s) => `<li>${escapeHtml(s)}</li>`).join("");
  const validation = (remediation.validation || []).map((s) => `<li>${escapeHtml(s)}</li>`).join("");
  const rollback = (remediation.rollback || []).map((s) => `<li>${escapeHtml(s)}</li>`).join("");
  const targets = (remediation.target_symbols || []).map((s) => `<span>${escapeHtml(s)}</span>`).join("");
  target.innerHTML = `
    <div class="remediation-card">
      <div class="remediation-section">
        <h3>ACTION</h3>
        <p>${escapeHtml(remediation.action || "—")}</p>
        <p>${escapeHtml(remediation.description || "")}</p>
        <p>${remediation.environment ? `<span class="env-pill">Environment: ${escapeHtml(remediation.environment)}</span>` : ""}</p>
      </div>
      <div class="remediation-section">
        <h3>TARGET SYMBOLS</h3>
        <div class="target">${targets || "—"}</div>
      </div>
      <div class="remediation-section">
        <h3>STEPS</h3>
        <ol>${steps || "<li>—</li>"}</ol>
      </div>
      <div class="remediation-section">
        <h3>VALIDATION</h3>
        <ul>${validation || "<li>—</li>"}</ul>
      </div>
      <div class="remediation-section">
        <h3>ROLLBACK</h3>
        <ul>${rollback || "<li>—</li>"}</ul>
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Fingerprint / Correlation / Raw
// ---------------------------------------------------------------------------
function renderFingerprint(fp) {
  if (!fp) {
    text("#fp-category", "—"); text("#fp-type", "—"); text("#fp-exception", "—");
    text("#fp-service", "—"); text("#fp-stage", "—"); text("#fp-config-area", "—");
    text("#fp-change", "—"); text("#fp-runtime", "—");
    $("#fp-signature").innerHTML = "";
    return;
  }
  text("#fp-category", fp.failure_category || "—");
  text("#fp-type", fp.failure_type || "—");
  text("#fp-exception", fp.exception_type || "—");
  text("#fp-service", fp.affected_service || "—");
  text("#fp-stage", fp.failure_stage || "—");
  text("#fp-config-area", fp.configuration_area || "—");
  text("#fp-change", fp.related_change_type || "—");
  text("#fp-runtime", fp.runtime_type || "—");
  $("#fp-signature").innerHTML = (fp.signature_keys || []).map((k) => `<span class="sig">${escapeHtml(k)}</span>`).join("");
}

function renderCorrelation(correlation) {
  const target = $("#correlation-list");
  const edges = (correlation && correlation.edges) || [];
  if (!edges.length) {
    target.innerHTML = `<div class="empty">No correlations detected.</div>`;
    return;
  }
  target.innerHTML = "";
  edges.forEach((e) => {
    const row = document.createElement("div");
    row.className = "correlation-row";
    row.innerHTML = `
      <code>${escapeHtml(e.source_observation_id)}</code>
      <span class="crelation">${escapeHtml(e.relation)}</span>
      <code>${escapeHtml(e.target_observation_id)}</code>
      <small style="color:var(--muted)">${escapeHtml(short(e.rationale, 60) || "")}</small>
      <span class="cconfidence">${Number(e.confidence || 0).toFixed(2)}</span>
    `;
    target.appendChild(row);
  });
}

function renderRawData(data) {
  $("#raw-observations").textContent = JSON.stringify(data.observations || [], null, 2);
  $("#raw-fingerprint").textContent = JSON.stringify(data.fingerprint || null, null, 2);
  $("#raw-correlation").textContent = JSON.stringify(data.correlation || null, null, 2);
  $("#raw-graph").textContent = JSON.stringify(data.graph || null, null, 2);
  $("#raw-payload").textContent = JSON.stringify(data, null, 2);
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function escapeHtml(value) {
  if (value === null || value === undefined) return "—";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Initial load
loadInvestigations();