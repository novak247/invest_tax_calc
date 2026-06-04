const state = {
  filename: "",
  content: "",
  result: null,
  gmailState: "",
  gmailPoll: null,
  gmailAuthUrl: "",
  reloadToken: "",
};

const $ = (id) => document.getElementById(id);

const today = new Date().toISOString().slice(0, 10);
$("asOf").value = today;
$("planDate").value = today;
$("taxYear").value = new Date().getFullYear();

$("csvFile").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  state.filename = file.name;
  state.content = await file.text();
  $("fileName").textContent = file.name;
  clearError();
});

$("analyzeBtn").addEventListener("click", analyze);
$("planBtn").addEventListener("click", runPlan);
$("gmailBtn").addEventListener("click", startGmailImport);
watchDevReload();

async function analyze() {
  if (!state.content) {
    showError("Choose a Trading 212 CSV file first.");
    return;
  }

  setBusy("analyzeBtn", true);
  clearError();
  try {
    const result = await postJson("/api/analyze", payloadBase());
    state.result = result;
    renderResult(result);
  } catch (error) {
    showError(error.message);
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

async function startGmailImport() {
  clearError();
  clearTimeout(state.gmailPoll);
  setBusy("gmailBtn", true);
  renderGmailStatus({ status: "starting", message: "Preparing Google sign-in." });
  $("gmailFiles").classList.add("hidden");
  $("gmailFiles").innerHTML = "";
  const popup = window.open("", "_blank");

  try {
    const result = await postJson("/api/email/gmail/start", {
      query: $("gmailQuery").value,
      clientId: $("gmailClientId").value,
      clientSecret: $("gmailClientSecret").value,
      maxMessages: Number($("gmailMaxMessages").value || 200),
      output: $("gmailOutput").value,
    });
    state.gmailState = result.state;
    state.gmailAuthUrl = result.authUrl;
    renderGmailStatus(result);

    if (popup) {
      popup.opener = null;
      popup.location.href = result.authUrl;
    } else {
      renderGmailStatus({
        ...result,
        message: "Popup blocked. Open the Google sign-in link below.",
        authUrl: result.authUrl,
      });
    }
    pollGmailStatus();
  } catch (error) {
    if (popup && !popup.closed) popup.close();
    setBusy("gmailBtn", false);
    renderGmailStatus({ status: "error", message: error.message, error: error.message });
    showError(error.message);
  }
}

async function pollGmailStatus() {
  if (!state.gmailState) return;

  try {
    const result = await getJson(`/api/email/gmail/status?state=${encodeURIComponent(state.gmailState)}`);
    renderGmailStatus(result);

    if (result.status === "done") {
      setBusy("gmailBtn", false);
      renderGmailFiles(result.files || []);
      await loadFirstGmailCsv(result);
      return;
    }

    if (result.status === "error") {
      setBusy("gmailBtn", false);
      showError(result.error || result.message || "Gmail import failed.");
      return;
    }
  } catch (error) {
    renderGmailStatus({ status: "waiting", message: "Waiting for the local Gmail callback." });
  }

  state.gmailPoll = setTimeout(pollGmailStatus, 1200);
}

async function loadFirstGmailCsv(result) {
  const csv = (result.csvFiles || []).find((file) => file.content && !file.tooLarge);
  if (!csv) {
    if ((result.csvFiles || []).some((file) => file.tooLarge)) {
      showError("Gmail downloaded a CSV, but it is too large to load automatically.");
    }
    return;
  }

  state.filename = csv.filename;
  state.content = csv.content;
  $("fileName").textContent = `Gmail: ${csv.filename}`;
  clearError();
  await analyze();
}

function payloadBase() {
  return {
    filename: state.filename,
    content: state.content,
    taxYear: Number($("taxYear").value),
    asOf: $("asOf").value,
    defaultCurrency: $("defaultCurrency").value,
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
}

function renderGmailStatus(result) {
  const box = $("gmailStatus");
  const status = statusLabel(result.status);
  const link = result.authUrl || state.gmailAuthUrl;
  box.innerHTML = `
    <strong>${escapeHtml(status)}</strong>
    <span>${escapeHtml(result.message || "")}</span>
    ${
      result.status === "waiting" && link
        ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">Open Google sign-in</a>`
        : ""
    }
  `;
  box.classList.remove("hidden");
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
          <span>${escapeHtml(file.saved ? "saved" : "duplicate")}</span>
        </div>
      `
    )
    .join("");
  box.classList.remove("hidden");
}

function statusLabel(status) {
  const labels = {
    starting: "Starting",
    waiting: "Waiting",
    importing: "Importing",
    done: "Done",
    error: "Error",
  };
  return labels[status] || "Gmail";
}

function setBusy(id, busy) {
  const button = $(id);
  button.disabled = busy;
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

  setTimeout(watchDevReload, 1000);
}
