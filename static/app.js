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
    if (!res.ok) throw new Error(data.detail || "Failed to fetch video info");
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
  transcriptInput.disabled = langs.length === 0;
  transcriptInput.closest("label").style.opacity = langs.length === 0 ? "0.4" : "";
  if (!langs.length && transcriptInput.checked) {
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
    format === "transcript" ? "flex" : "none";
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
    if (!res.ok) throw new Error(data.detail || "Failed to start download");
    const { job_id } = data;
    trackProgress(job_id, btn);
  } catch (e) {
    showStatusMessage("error", e.message);
    btn.disabled = false;
  }
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
      const msg = data.filename2
        ? `Saved to ~/Downloads/youtube/\n  ${data.filename}\n  ${data.filename2}`
        : `Saved to ~/Downloads/youtube/${data.filename}`;
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
