const state = {
  filename: "",
  content: "",
  contentType: "",
  contentEncoding: "text",
  reports: [],
  result: null,
  gmailState: "",
  gmailPoll: null,
  gmailAuthUrl: "",
  gmailConfigured: false,
  gmailTokenCached: false,
  reloadToken: "",
};

const $ = (id) => document.getElementById(id);

const today = new Date().toISOString().slice(0, 10);
$("asOf").value = today;
$("planDate").value = today;
$("taxYear").value = new Date().getFullYear();

$("pdfFile").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  state.filename = file.name;
  state.content = arrayBufferToBase64(await file.arrayBuffer());
  state.contentType = file.type || "application/pdf";
  state.contentEncoding = "base64";
  state.reports = [
    {
      filename: state.filename,
      content: state.content,
      contentType: state.contentType,
      contentEncoding: state.contentEncoding,
    },
  ];
  $("fileName").textContent = file.name;
  clearError();
});

$("analyzeBtn").addEventListener("click", analyze);
$("planBtn").addEventListener("click", runPlan);
$("gmailConnectBtn").addEventListener("click", connectGmail);
$("gmailFetchBtn").addEventListener("click", startGmailImport);
$("gmailCancelBtn").addEventListener("click", cancelGmailImport);
$("gmailSetupBtn").addEventListener("click", saveGmailConfig);
loadGmailConfig();
watchDevReload();

async function analyze() {
  if (!state.content && !state.reports.length) {
    showError("Choose a Trading 212 PDF statement first.");
    return false;
  }

  setBusy("analyzeBtn", true);
  clearError();
  try {
    const result = await postJson("/api/analyze", payloadBase());
    state.result = result;
    renderResult(result);
    return true;
  } catch (error) {
    showError(error.message);
    return false;
  } finally {
    setBusy("analyzeBtn", false);
  }
}

async function runPlan() {
  if (!state.result) {
    showError("Analyze a report before planning a sale.");
    return;
  }

  setBusy("planBtn", true);
  clearError();
  try {
    const result = await postJson("/api/plan", {
      ...payloadBase(),
      instrumentKey: $("planInstrument").value,
      quantity: $("planQuantity").value,
      pricePerShareCzk: $("planPrice").value,
      saleDate: $("planDate").value,
    });
    renderPlan(result);
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy("planBtn", false);
  }
}

async function connectGmail() {
  if (!state.gmailConfigured) {
    renderGmailStatus({ status: "error", message: "Save Gmail setup first." });
    return;
  }

  clearError();
  clearTimeout(state.gmailPoll);
  setGmailBusy(true);
  renderGmailStatus({ status: "starting", message: "Preparing Google sign-in." });
  const popup = window.open("", "_blank");

  try {
    const result = await postJson("/api/email/gmail/connect", {});
    state.gmailState = result.state;
    state.gmailAuthUrl = result.authUrl;
    renderGmailStatus(result);

    if (result.authUrl && popup) {
      popup.opener = null;
      popup.location.href = result.authUrl;
    } else if (result.authUrl) {
      renderGmailStatus({
        ...result,
        message: "Popup blocked. Open the Google sign-in link below.",
        authUrl: result.authUrl,
      });
    }
    pollGmailStatus();
  } catch (error) {
    if (popup && !popup.closed) popup.close();
    setGmailBusy(false);
    renderGmailStatus({ status: "error", message: error.message, error: error.message });
    showError(error.message);
  }
}

async function startGmailImport() {
  if (!state.gmailConfigured) {
    renderGmailStatus({ status: "error", message: "Save Gmail setup first." });
    return;
  }
  if (!state.gmailTokenCached) {
    renderGmailStatus({ status: "error", message: "Connect Gmail first, then fetch reports." });
    return;
  }

  clearError();
  clearTimeout(state.gmailPoll);
  setGmailBusy(true);
  renderGmailStatus({ status: "starting", message: "Starting report fetch." });
  $("gmailFiles").classList.add("hidden");
  $("gmailFiles").innerHTML = "";

  try {
    const result = await postJson("/api/email/gmail/start", {
      query: $("gmailQuery").value,
      maxMessages: Number($("gmailMaxMessages").value || 500),
      output: $("gmailOutput").value,
      reparseCachedPdfs: $("gmailReparseCachedPdfs").checked,
    });
    state.gmailState = result.state;
    state.gmailAuthUrl = "";
    renderGmailStatus(result);
    pollGmailStatus();
  } catch (error) {
    setGmailBusy(false);
    renderGmailStatus({ status: "error", message: error.message, error: error.message });
    showError(error.message);
  }
}

async function cancelGmailImport() {
  if (!state.gmailState) return;
  $("gmailCancelBtn").disabled = true;
  try {
    const result = await postJson("/api/email/gmail/cancel", { state: state.gmailState });
    renderGmailStatus(result);
  } catch (error) {
    renderGmailStatus({ status: "error", message: error.message, error: error.message });
  }
}

async function loadGmailConfig() {
  try {
    const result = await getJson("/api/email/gmail/config");
    renderGmailConfig(result);
  } catch (error) {
    renderGmailConfig({ configured: false, message: error.message, tokenCached: false });
  }
}

async function saveGmailConfig() {
  clearError();
  setBusy("gmailSetupBtn", true);
  try {
    const result = await postJson("/api/email/gmail/config", {
      credentialsJson: $("gmailCredentials").value,
    });
    $("gmailCredentials").value = "";
    renderGmailConfig(result);
    renderGmailStatus({ status: "done", message: "Gmail setup saved locally." });
  } catch (error) {
    renderGmailStatus({ status: "error", message: error.message });
    showError(error.message);
  } finally {
    setBusy("gmailSetupBtn", false);
  }
}

async function pollGmailStatus() {
  if (!state.gmailState) return;

  try {
    const result = await getJson(`/api/email/gmail/status?state=${encodeURIComponent(state.gmailState)}`);
    renderGmailStatus(result);

    if (result.status === "done") {
      state.gmailTokenCached = true;
      setGmailBusy(false);
      await loadGmailConfig();
      if (result.purpose === "import") {
        renderGmailFiles(result.files || []);
        await loadGmailPdfs(result);
      }
      return;
    }

    if (result.status === "canceled") {
      setGmailBusy(false);
      return;
    }

    if (result.status === "error") {
      setGmailBusy(false);
      showError(result.error || result.message || "Gmail import failed.");
      return;
    }
  } catch (error) {
    renderGmailStatus({ status: "waiting", message: "Waiting for the local Gmail callback." });
  }

  state.gmailPoll = setTimeout(pollGmailStatus, 1200);
}

async function loadGmailPdfs(result) {
  const pdfs = (result.pdfFiles || []).filter((file) => !file.tooLarge && (file.path || file.contentBase64));
  if (!pdfs.length) {
    state.filename = "";
    state.content = "";
    state.contentType = "";
    state.contentEncoding = "text";
    state.reports = [];
    if (Number(result.skipped || 0) > 0 && Number(result.saved || 0) === 0) {
      clearError();
      $("fileName").textContent = "Gmail: no new PDF statements";
      return;
    }
    if ((result.pdfFiles || []).some((file) => file.tooLarge)) {
      showError("Gmail downloaded a PDF, but it is too large to load automatically.");
      return;
    }
    showError("Gmail did not find a Trading 212 PDF statement attachment.");
    return;
  }

  const first = pdfs[0];
  state.filename = pdfs.length === 1 ? first.filename : `${pdfs.length} Gmail PDF statements`;
  state.content = first.contentBase64 || "";
  state.contentType = first.contentType || "application/pdf";
  state.contentEncoding = first.contentBase64 ? "base64" : "path";
  state.reports = pdfs.map((pdf) => ({
    filename: pdf.filename,
    content: pdf.contentBase64 || "",
    path: pdf.path || "",
    contentType: pdf.contentType || "application/pdf",
    contentEncoding: pdf.contentBase64 ? "base64" : "path",
  }));
  $("fileName").textContent = `Gmail: ${state.filename}`;
  clearError();
  renderGmailStatus({
    ...result,
    status: "analyzing",
    message: `Analyzing ${pdfs.length} PDF statement${pdfs.length === 1 ? "" : "s"}.`,
  });
  const analyzed = await analyze();
  renderGmailStatus({
    ...result,
    status: analyzed ? "done" : "error",
    message: analyzed
      ? `Analysis complete. Loaded ${pdfs.length} PDF statement${pdfs.length === 1 ? "" : "s"}.`
      : "Analysis failed. Check the error below.",
  });
}

function payloadBase() {
  return {
    filename: state.filename,
    content: state.content,
    contentType: state.contentType,
    contentEncoding: state.contentEncoding,
    reports: state.reports,
    reparseCachedPdfs: $("gmailReparseCachedPdfs").checked,
    taxYear: Number($("taxYear").value),
    asOf: $("asOf").value,
    rates: $("rates").value,
  };
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Request failed.");
  }
  return data;
}

async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Request failed.");
  }
  return data;
}

function renderResult(result) {
  $("emptyState").classList.add("hidden");
  $("dashboard").classList.remove("hidden");
  $("serverStatus").textContent = `${result.transactionCount} orders parsed`;

  renderSummary(result.summary);
  renderHoldings(result.holdings);
  renderMatches(result.matches);
  renderWarnings(result.warnings);
  setupPlanner(result.holdings);
}

function renderSummary(summary) {
  const metrics = [
    ["Gross proceeds", czk(summary.grossProceedsCzk), `Year ${summary.year}`],
    [
      "Exempt proceeds",
      czk(summary.exemptProceedsCzk),
      summary.grossLimitApplies ? "100k gross limit applies" : "Mostly from 3-year lots",
    ],
    ["Taxable proceeds", czk(summary.taxableProceedsCzk), "Before costs and fees"],
    ["Taxable gain", czk(summary.taxableGainCzk), "Loss is not carried elsewhere"],
    ["Est. tax at 15%", czk(summary.estimatedTax15Czk), "23% band depends on total tax base"],
  ];

  $("summaryStrip").innerHTML = metrics
    .map(
      ([label, value, note]) => `
        <article class="metric">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
          <small>${escapeHtml(note)}</small>
        </article>
      `
    )
    .join("");
}

function renderHoldings(holdings) {
  const body = $("holdingsBody");
  if (!holdings.length) {
    body.innerHTML = `<tr><td colspan="5">No open holdings found after matching all sells.</td></tr>`;
    return;
  }

  body.innerHTML = holdings
    .map(
      (holding) => `
        <tr>
          <td>${instrumentLabel(holding)}</td>
          <td class="num">${qty(holding.quantity)}</td>
          <td class="num">${czk(holding.averageCostCzk)}</td>
          <td class="num">${qty(holding.taxFreeQuantityNow)}</td>
          <td>${holding.nextTaxFreeDate || "All current lots pass"}</td>
        </tr>
      `
    )
    .join("");
}

function renderMatches(matches) {
  const body = $("matchesBody");
  if (!matches.length) {
    body.innerHTML = `<tr><td colspan="8">No sells found for the selected tax year.</td></tr>`;
    return;
  }

  body.innerHTML = matches
    .map(
      (match) => `
        <tr>
          <td>${instrumentLabel(match)}</td>
          <td>${match.saleDate}</td>
          <td>${match.buyDate || "Missing"}</td>
          <td class="num">${qty(match.quantity)}</td>
          <td class="num">${czk(match.grossProceedsCzk)}</td>
          <td class="num">${czk(match.costCzk)}</td>
          <td class="num">${czk(match.gainCzk)}</td>
          <td><span class="badge ${match.taxable ? "tax" : "ok"}">${escapeHtml(match.status)}</span></td>
        </tr>
      `
    )
    .join("");
}

function renderWarnings(warnings) {
  const panel = $("warningsPanel");
  if (!warnings.length) {
    panel.classList.add("hidden");
    return;
  }

  $("warningsList").innerHTML = warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("");
  panel.classList.remove("hidden");
}

function setupPlanner(holdings) {
  const select = $("planInstrument");
  select.innerHTML = holdings
    .map(
      (holding) =>
        `<option value="${escapeHtml(holding.instrumentKey)}">${instrumentLabel(holding, false)}</option>`
    )
    .join("");

  if (holdings.length) {
    $("planQuantity").value = holdings[0].quantity;
    $("planResult").classList.add("hidden");
  }
}

function renderPlan(result) {
  const box = $("planResult");
  const groups = result.plannedMatchGroups || [];
  const plannedGroups = groups.length
    ? `
      <div class="plan-group-table">
        <div class="plan-group-head">
          <span>Lot range</span>
          <span>Qty</span>
          <span>Proceeds</span>
          <span>Cost</span>
          <span>Gain</span>
          <span>Status</span>
        </div>
        ${groups
          .map(
            (group) => `
              <div class="plan-group-row">
                <span>${escapeHtml(planDateRange(group))}</span>
                <span class="num">${qty(group.quantity)}</span>
                <span class="num">${czk(group.grossProceedsCzk)}</span>
                <span class="num">${czk(group.costCzk)}</span>
                <span class="num">${czk(group.gainCzk)}</span>
                <span><span class="badge ${group.taxable ? "tax" : "ok"}">${escapeHtml(group.status)}</span></span>
              </div>
            `
          )
          .join("")}
      </div>
    `
    : "";

  box.innerHTML = `
    <div class="plan-result-grid">
      <div><span>Planned proceeds</span><strong>${czk(result.plannedProceedsCzk)}</strong></div>
      <div><span>Taxable gain delta</span><strong>${czk(result.deltaTaxableGainCzk)}</strong></div>
      <div><span>Tax delta at 15%</span><strong>${czk(result.deltaEstimatedTax15Czk)}</strong></div>
      <div><span>After-sale gross proceeds</span><strong>${czk(result.afterSummary.grossProceedsCzk)}</strong></div>
    </div>
    ${plannedGroups}
  `;
  box.classList.remove("hidden");
  return;

  /*
  const plannedRows = result.plannedMatches
    .map(
      (match) =>
        `<div><span>${match.buyDate || "Missing buy"} → ${match.saleDate}</span><strong>${escapeHtml(match.status)}</strong></div>`
    )
    .join("");

  box.innerHTML = `
    <div class="plan-result-grid">
      <div><span>Planned proceeds</span><strong>${czk(result.plannedProceedsCzk)}</strong></div>
      <div><span>Taxable gain delta</span><strong>${czk(result.deltaTaxableGainCzk)}</strong></div>
      <div><span>Tax delta at 15%</span><strong>${czk(result.deltaEstimatedTax15Czk)}</strong></div>
      <div><span>After-sale gross proceeds</span><strong>${czk(result.afterSummary.grossProceedsCzk)}</strong></div>
    </div>
    ${plannedRows ? `<div class="plan-result-grid" style="margin-top: 12px">${plannedRows}</div>` : ""}
  `;
  box.classList.remove("hidden");
  */
}

function planDateRange(group) {
  const start = group.buyDateStart || "Missing buy";
  const end = group.buyDateEnd || group.buyDateStart || "Missing buy";
  const count = Number(group.lotCount || 0);
  const range = start === end ? start : `${start} to ${end}`;
  return count > 1 ? `${range} (${count} lots)` : range;
}

function renderGmailConfig(result) {
  state.gmailConfigured = Boolean(result.configured);
  state.gmailTokenCached = Boolean(result.tokenCached);
  $("gmailSetup").classList.toggle("hidden", state.gmailConfigured);
  $("gmailReady").classList.toggle("hidden", !state.gmailConfigured);

  if (state.gmailConfigured) {
    $("gmailReady").innerHTML = `
      <strong>Gmail setup ready</strong>
      <span>${escapeHtml(result.clientIdPreview || "OAuth client saved")}</span>
      <span>${escapeHtml(state.gmailTokenCached ? "Saved login available" : "Browser login required")}</span>
    `;
  }

  setGmailBusy(false);
}

function renderGmailStatus(result) {
  const box = $("gmailStatus");
  const status = statusLabel(result.status);
  const link = result.authUrl || state.gmailAuthUrl;
  box.innerHTML = `
    <strong>${escapeHtml(status)}</strong>
    <span>${escapeHtml(result.message || "")}</span>
    ${gmailProgressHtml(result.progress)}
    ${
      result.status === "waiting" && link
        ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">Open Google sign-in</a>`
        : ""
    }
  `;
  box.classList.remove("hidden");
}

function gmailProgressHtml(progress) {
  if (!progress) return "";

  const totalMessages = Number(progress.totalMessages || 0);
  const processedMessages = Number(progress.processedMessages || 0);
  const totalAttachments = Number(progress.totalAttachments || 0);
  const saved = Number(progress.saved || 0);
  const skipped = Number(progress.skipped || 0);
  const percent = totalMessages > 0 ? Math.min(100, Math.round((processedMessages / totalMessages) * 100)) : 0;
  const label = totalMessages > 0
    ? `${processedMessages}/${totalMessages} messages`
    : statusLabel(progress.phase || "starting");

  return `
    <div class="progress-block">
      <div class="progress-meta">
        <span>${escapeHtml(label)}</span>
        <span>${escapeHtml(`${totalAttachments} attachments, ${saved} new, ${skipped} cached`)}</span>
      </div>
      <div class="progress-track" aria-label="Gmail import progress">
        <div class="progress-fill" style="width: ${percent}%"></div>
      </div>
    </div>
  `;
}

function renderGmailFiles(files) {
  const box = $("gmailFiles");
  if (!files.length) {
    box.innerHTML = "<span>No attachments matched.</span>";
    box.classList.remove("hidden");
    return;
  }

  box.innerHTML = files
    .slice(0, 8)
    .map(
      (file) => `
        <div>
          <strong>${escapeHtml(file.filename)}</strong>
          <span>${escapeHtml(`${fileKind(file)} ${file.saved ? "saved" : "cached"}`)}</span>
        </div>
      `
    )
    .join("");
  box.classList.remove("hidden");
}

function fileKind(file) {
  const name = String(file.filename || "").toLowerCase();
  const contentType = String(file.contentType || "").toLowerCase();
  if (name.endsWith(".pdf") || contentType === "application/pdf") return "pdf";
  return "file";
}

function statusLabel(status) {
  const labels = {
    starting: "Starting",
    waiting: "Waiting",
    authorizing: "Authorizing",
    canceling: "Stopping",
    canceled: "Stopped",
    listing: "Finding messages",
    downloading: "Downloading",
    importing: "Importing",
    analyzing: "Analyzing",
    done: "Done",
    error: "Error",
  };
  return labels[status] || "Gmail";
}

function setBusy(id, busy) {
  const button = $(id);
  button.disabled = busy;
}

function setGmailBusy(busy) {
  $("gmailConnectBtn").disabled = busy || !state.gmailConfigured;
  $("gmailFetchBtn").disabled = busy || !state.gmailConfigured || !state.gmailTokenCached;
  $("gmailCancelBtn").classList.toggle("hidden", !busy);
  $("gmailCancelBtn").disabled = !busy;
}

function showError(message) {
  $("errorBox").textContent = message;
  $("errorBox").classList.remove("hidden");
}

function clearError() {
  $("errorBox").classList.add("hidden");
  $("errorBox").textContent = "";
}

function instrumentLabel(item, html = true) {
  const main = item.ticker || item.name || item.instrumentKey;
  const sub = item.isin || item.instrumentKey;
  const label = `${main}${sub && sub !== main ? ` (${sub})` : ""}`;
  return html ? escapeHtml(label) : label;
}

function czk(value) {
  return new Intl.NumberFormat("cs-CZ", {
    style: "currency",
    currency: "CZK",
    maximumFractionDigits: 0,
  }).format(Number(value || 0));
}

function qty(value) {
  return new Intl.NumberFormat("cs-CZ", {
    maximumFractionDigits: 8,
  }).format(Number(value || 0));
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

async function watchDevReload() {
  try {
    const info = await getJson("/api/dev/reload-state");
    if (!info.enabled) return;

    if (state.reloadToken && state.reloadToken !== info.token) {
      window.location.reload();
      return;
    }
    state.reloadToken = info.token;
  } catch (error) {
    if (!state.reloadToken) return;
  }

  setTimeout(watchDevReload, 2000);
}
