let currentUrl = null;

async function fetchInfo() {
  const input = document.getElementById("url-input");
  const url = input.value.trim();
  if (!url) return;

  currentUrl = null;
  hideError();
  setFetchLoading(true);
  document.getElementById("video-card").classList.add("hidden");
  document.getElementById("download-section").classList.add("hidden");
  document.getElementById("progress-section").classList.add("hidden");

  try {
    const res = await fetch(`/api/info?url=${encodeURIComponent(url)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(getErrorMessage(data, "Failed to fetch video info"));
    currentUrl = url;
    displayVideoInfo(data);
  } catch (e) {
    showError(e.message);
  } finally {
    setFetchLoading(false);
  }
}

function displayVideoInfo(info) {
  document.getElementById("thumb").src = info.thumbnail;
  document.getElementById("vid-title").textContent = info.title;
  document.getElementById("vid-uploader").textContent = info.uploader;
  document.getElementById("vid-duration").textContent = info.duration;

  const select = document.getElementById("quality-select");
  select.replaceChildren();
  info.qualities.forEach((q) => {
    const option = document.createElement("option");
    option.value = q;
    option.textContent = `${q}p`;
    select.appendChild(option);
  });

  const langs = info.transcript_langs || [];
  const langSelect = document.getElementById("lang-select");
  langSelect.replaceChildren();
  if (langs.length) {
    langs.forEach((l) => {
      const option = document.createElement("option");
      option.value = l;
      option.textContent = l;
      langSelect.appendChild(option);
    });
  } else {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No captions";
    langSelect.appendChild(option);
  }
  const transcriptInput = document.getElementById("format-transcript");
  const mfoPackInput = document.getElementById("format-mfo-pack");
  [transcriptInput, mfoPackInput].forEach((input) => {
    input.disabled = langs.length === 0;
    input.closest("label").style.opacity = langs.length === 0 ? "0.4" : "";
  });
  if (!langs.length && (transcriptInput.checked || mfoPackInput.checked)) {
    document.querySelector('input[name="format"][value="mp4"]').checked = true;
  }

  document.getElementById("video-card").classList.remove("hidden");
  document.getElementById("download-section").classList.remove("hidden");
  updateFormatUI();
}

function updateFormatUI() {
  const format = document.querySelector('input[name="format"]:checked').value;
  document.getElementById("quality-wrapper").style.display =
    format === "mp4" || format === "split" ? "flex" : "none";
  document.getElementById("lang-wrapper").style.display =
    format === "transcript" || format === "mfo_pack" ? "flex" : "none";
}

async function startDownload() {
  if (!currentUrl) return;

  const format_type = document.querySelector('input[name="format"]:checked').value;
  const quality = document.getElementById("quality-select").value;
  const lang = document.getElementById("lang-select").value;
  const btn = document.getElementById("download-btn");

  btn.disabled = true;
  const progressSection = document.getElementById("progress-section");
  progressSection.classList.remove("hidden");
  document.getElementById("status-message").classList.add("hidden");
  document.getElementById("status-message").className = "status-message hidden";
  setProgress(0, "Starting…");

  try {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: currentUrl, format_type, quality, lang }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(getErrorMessage(data, "Failed to start download"));
    const { job_id } = data;
    trackProgress(job_id, btn);
  } catch (e) {
    showStatusMessage("error", e.message);
    btn.disabled = false;
  }
}

function getErrorMessage(data, fallback) {
  if (!data || !data.detail) return fallback;
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((item) => item.msg || JSON.stringify(item)).join("\n");
  }
  return JSON.stringify(data.detail);
}

function trackProgress(job_id, btn) {
  const source = new EventSource(`/api/progress/${job_id}`);

  source.onmessage = (e) => {
    const data = JSON.parse(e.data);

    if (data.error && data.status !== "error") {
      showStatusMessage("error", data.error);
      source.close();
      btn.disabled = false;
      return;
    }

    if (data.status === "downloading") {
      const pct = data.percent || 0;
      const phaseLabel = data.phase ? ` · ${data.phase}` : "";
      const label = data.speed
        ? `${pct.toFixed(1)}%${phaseLabel}  ·  ${data.speed}  ·  ETA ${data.eta}`
        : `${pct.toFixed(1)}%${phaseLabel}`;
      setProgress(pct, label);
    } else if (data.status === "processing") {
      setProgress(100, data.phase ? `Processing ${data.phase}…` : "Processing…");
    } else if (data.status === "done") {
      setProgress(100, "");
      const filenames = [data.filename, data.filename2, data.filename3].filter(Boolean);
      const msg = filenames.length > 1
        ? `Saved to ~/Downloads/youtube/\n  ${filenames.join("\n  ")}`
        : `Saved to ~/Downloads/youtube/${filenames[0]}`;
      showStatusMessage("done", msg);
      source.close();
      btn.disabled = false;
    } else if (data.status === "error") {
      showStatusMessage("error", data.error || "Download failed");
      source.close();
      btn.disabled = false;
    }
  };

  source.onerror = () => {
    showStatusMessage("error", "Lost connection to server");
    source.close();
    btn.disabled = false;
  };
}

function setProgress(percent, text) {
  document.getElementById("progress-bar").style.width = `${percent}%`;
  document.getElementById("progress-text").textContent = text;
}

function showStatusMessage(type, message) {
  const el = document.getElementById("status-message");
  el.className = `status-message ${type}`;
  el.textContent = message;
}

function setFetchLoading(loading) {
  const btn = document.getElementById("fetch-btn");
  const label = document.getElementById("fetch-label");
  btn.disabled = loading;
  label.textContent = loading ? "Loading…" : "Fetch";
}

function showError(msg) {
  const el = document.getElementById("fetch-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function hideError() {
  document.getElementById("fetch-error").classList.add("hidden");
}

document.getElementById("url-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") fetchInfo();
});

let editorialResults = null;
let reviewPacket = null;
let lastAgentReview = null;
let editorialPoll = null;
let activeLeadTab = "creator";
let manualStories = [];

function fmt(value, fallback = "Not available") {
  if (value === null || value === undefined || value === "") return fallback;
  return value;
}

function shortDate(value) {
  if (!value) return "Not available";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function showEditorialError(message) {
  const el = document.getElementById("editorial-error");
  el.textContent = message;
  el.classList.remove("hidden");
}

function hideEditorialError() {
  document.getElementById("editorial-error").classList.add("hidden");
}

async function refreshEditorialResults() {
  hideEditorialError();
  try {
    const res = await fetch("/api/editorial/results");
    const data = await res.json();
    if (!res.ok) throw new Error(getErrorMessage(data, "Failed to load editorial results"));
    editorialResults = data;
    renderScannerStatus(data.status.creator, "creator-status-card");
    renderScannerStatus(data.status.news, "news-status-card");
    renderScannerStatus(data.status.research, "research-status-card");
    renderLeadInbox(data);
    manageEditorialPolling(data.status);
  } catch (e) {
    showEditorialError(e.message);
  }
}

async function runEditorialScan(type) {
  hideEditorialError();
  try {
    const res = await fetch(`/api/editorial/scans/${type}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(getErrorMessage(data, "Failed to start scanner"));
    await refreshEditorialResults();
    manageEditorialPolling({ creator: { state: "running" }, news: { state: "running" }, research: { state: "running" } });
  } catch (e) {
    showEditorialError(e.message);
  }
}

function manageEditorialPolling(status) {
  const running = ["creator", "news", "research"].some((key) =>
    ["queued", "running"].includes(status?.[key]?.state)
  );
  if (running && !editorialPoll) {
    editorialPoll = setInterval(refreshEditorialResults, 2500);
  } else if (!running && editorialPoll) {
    clearInterval(editorialPoll);
    editorialPoll = null;
  }
}

function renderScannerStatus(status, targetId) {
  const el = document.getElementById(targetId);
  if (!status) {
    const fallbackName = targetId.includes("research")
      ? "Research Radar"
      : targetId.includes("creator")
        ? "Creator Radar"
        : "News Radar";
    el.innerHTML = `
      <div class="scanner-card-head">
        <h2>${fallbackName}</h2>
        <span class="state-pill state-idle">Unavailable</span>
      </div>
      <div class="stale-warning">Restart the local app so this scanner lane can load.</div>
      <dl class="status-grid">
        <div><dt>Last successful run</dt><dd>Not available</dd></div>
        <div><dt>Leads</dt><dd>0</dd></div>
        <div><dt>Viable</dt><dd>0</dd></div>
        <div><dt>Report timestamp</dt><dd>Not available</dd></div>
      </dl>
    `;
    return;
  }
  const stale = status?.stale || {};
  const state = status?.state || "idle";
  el.innerHTML = `
    <div class="scanner-card-head">
      <h2>${status.scanner_type === "creator" ? "Creator Radar" : status.scanner_type === "research" ? "Research Radar" : "News Radar"}</h2>
      <span class="state-pill state-${state}">${state}</span>
    </div>
    ${stale.warning ? `<div class="stale-warning">${stale.warning}</div>` : ""}
    ${status.error ? `<div class="status-message error">${escapeHtml(status.error)}</div>` : ""}
    <dl class="status-grid">
      <div><dt>Last successful run</dt><dd>${shortDate(status.last_successful_run)}</dd></div>
      <div><dt>Leads</dt><dd>${status.lead_count || 0}</dd></div>
      <div><dt>Viable</dt><dd>${status.viable_count || 0}</dd></div>
      <div><dt>Report timestamp</dt><dd>${shortDate(status.report_timestamp)}</dd></div>
    </dl>
  `;
}

function allLeads(results) {
  const creator = results?.creator?.leads || [];
  const news = results?.news?.leads || [];
  const research = results?.research?.leads || [];
  return [...creator, ...news, ...research];
}

function renderLeadInbox(results) {
  const el = document.getElementById("lead-list");
  const decisions = results.decisions || {};
  const leads = leadsForActiveTab(results);
  const assessments = assessmentMap();
  if (!leads.length) {
    el.className = "lead-list empty-state";
    el.textContent = `No ${activeLeadTab} candidates available yet.`;
    renderExcluded(results);
    return;
  }
  el.className = "lead-list";
  el.replaceChildren();
  leads.forEach((lead, index) => {
    const decision = decisions[lead.lead_id]?.decision || "";
    const assessment = assessments[lead.lead_id] || null;
    const card = document.createElement("article");
    card.className = "lead-card";
    card.innerHTML = `
      <div class="lead-meta-row">
        <span class="source-pill">${escapeHtml(lead.scanner_type || "")}</span>
        <span>${escapeHtml(lead.source_name || lead.source_category || "Unknown source")}</span>
        <span>${escapeHtml(lead.status || "")}</span>
        <span>Raw rank ${escapeHtml(String(lead.raw_scanner_rank || index + 1))}</span>
        ${lead.scanner_score !== null && lead.scanner_score !== undefined ? `<strong>${lead.scanner_score}</strong>` : ""}
        ${assessment ? `<span class="rating-pill rating-${escapeAttr(assessment.agent_rating || "Weak")}">${escapeHtml(assessment.agent_rating || "")}</span>` : ""}
        ${assessment?.editorial_rank ? `<span>Editorial rank ${escapeHtml(String(assessment.editorial_rank))}</span>` : ""}
      </div>
      <h3>${escapeHtml(lead.title || "Untitled")}</h3>
      <p>${escapeHtml(lead.likely_mfo_angle || lead.mfo_audience_fit || lead.weakness_or_rejection_reason || "")}</p>
      ${assessment ? `
        <dl class="recommendation-grid assessment-grid">
          <div><dt>Agent reason</dt><dd>${escapeHtml(assessment.concise_reason || "")}</dd></div>
          <div><dt>MFO angle</dt><dd>${escapeHtml(assessment.mfo_angle || "")}</dd></div>
          <div><dt>Evidence risk</dt><dd>${escapeHtml(assessment.evidence_risk || "")}</dd></div>
          <div><dt>Archive warning</dt><dd>${escapeHtml(assessment.archive_overlap_warning || "")}</dd></div>
          <div><dt>Ranking difference</dt><dd>${escapeHtml(assessment.why_editorial_ranking_differs || "")}</dd></div>
          <div><dt>Agent action</dt><dd>${escapeHtml(assessment.recommended_action || "")}</dd></div>
        </dl>
      ` : `<div class="empty-state compact">No agent assessment imported for this candidate.</div>`}
      <div class="lead-facts">
        <span>Published: ${shortDate(lead.published_at)}</span>
        <span>Discovered: ${shortDate(lead.discovered_at)}</span>
        <span>Overlap: ${escapeHtml(lead.archive_overlap?.risk || lead.archive_overlap || "none")}</span>
      </div>
      <div class="lead-actions">
        <a class="btn btn-secondary" href="${escapeAttr(lead.source_url || "#")}" target="_blank" rel="noreferrer">Open</a>
        <button class="btn ${decision === "commission" ? "btn-primary" : "btn-secondary"}" onclick="saveLeadDecision('${escapeAttr(lead.lead_id)}', 'commission')">Commission</button>
        <button class="btn ${decision === "hold" ? "btn-primary" : "btn-secondary"}" onclick="saveLeadDecision('${escapeAttr(lead.lead_id)}', 'hold')">Hold</button>
        <button class="btn ${decision === "reject" ? "btn-primary" : "btn-secondary"}" onclick="saveLeadDecision('${escapeAttr(lead.lead_id)}', 'reject')">Reject</button>
      </div>
    `;
    el.appendChild(card);
  });
  renderExcluded(results);
}

function leadsForActiveTab(results) {
  if (reviewPacket?.packet?.review_candidates?.[activeLeadTab]) {
    return reviewPacket.packet.review_candidates[activeLeadTab] || [];
  }
  const all = activeLeadTab === "manual" ? manualStoryLeadFallbacks() : (results?.[activeLeadTab]?.leads || []);
  return all.slice(0, 10).map((lead, index) => ({ ...lead, raw_scanner_rank: lead.raw_scanner_rank || index + 1 }));
}

function manualStoryLeadFallbacks() {
  return manualStories.map((story, index) => ({
    lead_id: `manual:${index + 1}`,
    scanner_type: "manual",
    source_name: "Editor supplied",
    title: story.text,
    source_url: story.url || "",
    status: "manual",
    raw_scanner_rank: index + 1,
    likely_mfo_angle: "Editor-supplied lead. Verify before commissioning.",
  }));
}

function setLeadTab(tab) {
  activeLeadTab = tab;
  document.querySelectorAll(".tab-button").forEach((button) => {
    button.classList.toggle("active", button.textContent.toLowerCase() === tab);
  });
  if (editorialResults) renderLeadInbox(editorialResults);
}

function assessmentMap() {
  const map = {};
  (lastAgentReview?.reviewed_candidates || []).forEach((item) => {
    if (item?.lead_id) map[item.lead_id] = item;
  });
  return map;
}

function renderExcluded(results) {
  const excludedEl = document.getElementById("excluded-list");
  if (!excludedEl) return;
  const excluded = reviewPacket?.packet?.excluded_candidates?.[activeLeadTab]
    || (results?.[activeLeadTab]?.leads || []).filter((lead) =>
      ["already_covered", "rejected", "skipped"].includes(lead.status)
    );
  excludedEl.replaceChildren();
  if (!excluded.length) {
    excludedEl.textContent = "No excluded candidates for this lane.";
    return;
  }
  excluded.slice(0, 40).forEach((lead) => {
    const row = document.createElement("div");
    row.className = "rejected-row";
    row.innerHTML = `<strong>${escapeHtml(lead.lead_id || "")}</strong>: ${escapeHtml(lead.title || "")}<br><span>${escapeHtml(lead.weakness_or_rejection_reason || lead.status || "")}</span>`;
    excludedEl.appendChild(row);
  });
}

async function saveLeadDecision(leadId, decision) {
  try {
    const res = await fetch("/api/editorial/decisions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lead_id: leadId, decision, note: "" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(getErrorMessage(data, "Failed to save decision"));
    await refreshEditorialResults();
  } catch (e) {
    showEditorialError(e.message);
  }
}

async function prepareAgentReview() {
  hideEditorialError();
  try {
    const res = await fetch("/api/editorial/review-packet", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ manual_stories: manualStories }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(getErrorMessage(data, "Failed to prepare review packet"));
    reviewPacket = data;
    document.getElementById("packet-actions").classList.remove("hidden");
    const creatorCount = data.packet?.review_candidates?.creator?.length || 0;
    const newsCount = data.packet?.review_candidates?.news?.length || 0;
    const researchCount = data.packet?.review_candidates?.research?.length || 0;
    const manualCount = data.packet?.review_candidates?.manual?.length || 0;
    document.getElementById("packet-summary").textContent = `${creatorCount} Creator, ${newsCount} News, ${researchCount} Research and ${manualCount} Manual candidates ready.`;
    if (editorialResults) renderLeadInbox(editorialResults);
  } catch (e) {
    showEditorialError(e.message);
  }
}

function addManualStory() {
  const input = document.getElementById("manual-story-input");
  const text = input.value.trim();
  if (!text) return;
  const match = text.match(/https?:\/\/\S+/);
  manualStories.push({ text, url: match ? match[0].replace(/[).,]$/, "") : "" });
  input.value = "";
  renderManualStories();
  if (activeLeadTab === "manual" && editorialResults) renderLeadInbox(editorialResults);
}

function renderManualStories() {
  const el = document.getElementById("manual-story-list");
  el.replaceChildren();
  manualStories.forEach((story, index) => {
    const item = document.createElement("div");
    item.className = "manual-story-item";
    item.innerHTML = `<span>${escapeHtml(story.text)}</span><button class="btn btn-secondary" onclick="removeManualStory(${index})">Remove</button>`;
    el.appendChild(item);
  });
}

function removeManualStory(index) {
  manualStories.splice(index, 1);
  renderManualStories();
  if (activeLeadTab === "manual" && editorialResults) renderLeadInbox(editorialResults);
}

async function copyReviewPacket() {
  if (!reviewPacket) return;
  await navigator.clipboard.writeText(reviewPacket.markdown);
  document.getElementById("packet-summary").textContent = "Packet copied.";
}

function downloadReviewPacket(format) {
  if (!reviewPacket) return;
  const content = format === "json"
    ? JSON.stringify(reviewPacket.packet, null, 2)
    : reviewPacket.markdown;
  const blob = new Blob([content], { type: format === "json" ? "application/json" : "text/markdown" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = format === "json" ? "mfo-review-packet.json" : "mfo-review-packet.md";
  link.click();
  URL.revokeObjectURL(url);
}

function loadAgentResponseFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    document.getElementById("agent-response-input").value = reader.result;
  };
  reader.readAsText(file);
}

async function importAgentReview() {
  const errorEl = document.getElementById("agent-import-error");
  errorEl.classList.add("hidden");
  let parsed;
  try {
    parsed = JSON.parse(document.getElementById("agent-response-input").value);
  } catch (e) {
    errorEl.textContent = "The pasted response is not valid JSON.";
    errorEl.classList.remove("hidden");
    return;
  }
  const expectedIds = allPacketCandidates().map((lead) => lead.lead_id).filter(Boolean);
  if (expectedIds.length) {
    const reviewedIds = new Set((parsed.reviewed_candidates || []).map((item) => item?.lead_id));
    const missingIds = expectedIds.filter((leadId) => !reviewedIds.has(leadId));
    if (missingIds.length) {
      errorEl.textContent = `The response is missing assessments for ${missingIds.length} supplied candidate(s): ${missingIds.slice(0, 6).join(", ")}${missingIds.length > 6 ? "…" : ""}`;
      errorEl.classList.remove("hidden");
      return;
    }
  }
  try {
    const res = await fetch("/api/editorial/agent-review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ response: parsed }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(getErrorMessage(data, "Malformed supervisor response"));
    lastAgentReview = data.response;
    renderAgentReview(lastAgentReview);
  } catch (e) {
    errorEl.textContent = e.message;
    errorEl.classList.remove("hidden");
  }
}

function renderAgentReview(review) {
  const list = document.getElementById("recommendations-list");
  const assessments = assessmentMap();
  const candidates = allPacketCandidates();
  const candidateMap = Object.fromEntries(candidates.map((lead) => [lead.lead_id, lead]));
  const recommended = (review.recommended_ids || []).map((leadId) => ({
    lead: candidateMap[leadId],
    assessment: assessments[leadId],
    leadId,
  }));
  list.className = recommended.length ? "recommendations-list" : "recommendations-list empty-state";
  list.replaceChildren();
  if (!recommended.length) {
    list.textContent = "No recommended stories in imported review.";
  }
  recommended.forEach((item, index) => {
    const lead = item.lead || {};
    const rec = item.assessment || {};
    const card = document.createElement("article");
    card.className = "recommendation-card";
    card.innerHTML = `
      <div class="lead-meta-row">
        <span class="source-pill">#${escapeHtml(String(index + 1))}</span>
        <span>${escapeHtml(lead.scanner_type || rec.scanner_type || "")}</span>
        <span class="rating-pill rating-${escapeAttr(rec.agent_rating || "Possible")}">${escapeHtml(rec.agent_rating || "Possible")}</span>
        <span>Raw rank ${escapeHtml(String(lead.raw_scanner_rank || ""))}</span>
      </div>
      <h3>${escapeHtml(lead.title || item.leadId)}</h3>
      <dl class="recommendation-grid">
        <div><dt>Reason</dt><dd>${escapeHtml(rec.concise_reason || "")}</dd></div>
        <div><dt>MFO angle</dt><dd>${escapeHtml(rec.mfo_angle || lead.likely_mfo_angle || "")}</dd></div>
        <div><dt>Evidence risk</dt><dd>${escapeHtml(rec.evidence_risk || "")}</dd></div>
        <div><dt>Archive warning</dt><dd>${escapeHtml(rec.archive_overlap_warning || "")}</dd></div>
        <div><dt>Ranking difference</dt><dd>${escapeHtml(rec.why_editorial_ranking_differs || "")}</dd></div>
        <div><dt>Action</dt><dd>${escapeHtml(rec.recommended_action || "")}</dd></div>
      </dl>
      <div class="lead-actions">
        <button class="btn btn-secondary" onclick="saveLeadDecision('${escapeAttr(item.leadId)}', 'commission')">Commission</button>
        <button class="btn btn-secondary" onclick="saveLeadDecision('${escapeAttr(item.leadId)}', 'hold')">Hold</button>
        <button class="btn btn-secondary" onclick="saveLeadDecision('${escapeAttr(item.leadId)}', 'reject')">Reject</button>
      </div>
    `;
    list.appendChild(card);
  });

  if (editorialResults) renderLeadInbox(editorialResults);
}

function allPacketCandidates() {
  const grouped = reviewPacket?.packet?.review_candidates || {};
  return ["creator", "news", "research", "manual"].flatMap((key) => grouped[key] || []);
}

function formatArray(value) {
  return Array.isArray(value) ? value.join(", ") : (value || "");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

refreshEditorialResults();
