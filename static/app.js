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
  holdings: [],
  quotes: {},
  multiRows: [],
  multiRowSeq: 0,
  opportunities: null,
};

const $ = (id) => document.getElementById(id);

const today = new Date().toISOString().slice(0, 10);
$("asOf").value = today;
$("planDate").value = today;
$("multiDate").value = today;
$("targetDate").value = today;
$("oppDateA").value = today;
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
$("planFetchPriceBtn").addEventListener("click", fetchSinglePrice);
$("planInstrument").addEventListener("change", updateSinglePriceMeta);
$("multiAddRowBtn").addEventListener("click", () => {
  addMultiRow();
  renderMultiRows();
});
$("multiRefreshBtn").addEventListener("click", () => refreshPrices("multiRefreshBtn"));
$("multiPlanBtn").addEventListener("click", runMultiPlan);
$("targetRefreshBtn").addEventListener("click", () => refreshPrices("targetRefreshBtn"));
$("targetPlanBtn").addEventListener("click", runTargetPlan);
$("oppCompareBtn").addEventListener("click", runOppCompare);
$("oppInstrument").addEventListener("change", syncOppCompareDefaults);
document.querySelectorAll("[data-plan-tab]").forEach((button) =>
  button.addEventListener("click", () => switchPlanTab(button.dataset.planTab))
);
$("multiRows").addEventListener("input", syncMultiRow);
$("multiRows").addEventListener("change", syncMultiRow);
$("multiRows").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-remove]");
  if (!button) return;
  state.multiRows = state.multiRows.filter((row) => String(row.id) !== button.dataset.remove);
  if (!state.multiRows.length) addMultiRow();
  renderMultiRows();
});
$("gmailConnectBtn").addEventListener("click", connectGmail);
$("gmailFetchBtn").addEventListener("click", startGmailImport);
$("gmailCancelBtn").addEventListener("click", cancelGmailImport);
$("gmailSetupBtn").addEventListener("click", saveGmailConfig);
loadGmailConfig();
loadFxRates();
watchDevReload();

async function loadFxRates() {
  // Replace the placeholder FX values with real fetched rates on startup.
  const currencies = $("rates")
    .value.split("\n")
    .map((line) => line.split("=")[0].trim().toUpperCase())
    .filter(Boolean);
  try {
    const result = await postJson("/api/fx/rates", { currencies });
    applyFetchedFxRates(result.rates);
  } catch (error) {
    console.warn(`FX rate fetch failed, keeping defaults: ${error.message}`);
  }
}

function applyFetchedFxRates(fxRates) {
  const entries = Object.entries(fxRates || {});
  if (!entries.length) return;
  const order = [];
  const table = {};
  $("rates")
    .value.split("\n")
    .forEach((line) => {
      const [currency, value] = line.split("=");
      const key = (currency || "").trim().toUpperCase();
      if (!key) return;
      if (!(key in table)) order.push(key);
      table[key] = (value || "").trim();
    });
  for (const [currency, rate] of entries) {
    const key = currency.toUpperCase();
    if (!(key in table)) order.push(key);
    table[key] = Number(rate).toFixed(3);
  }
  $("rates").value = order.map((currency) => `${currency}=${table[currency]}`).join("\n");
}

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
    otherAnnualProceedsCzk: $("otherAnnualProceeds").value,
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
  if (result.taxOpportunities) {
    state.opportunities = result.taxOpportunities;
    renderOpportunities(result.taxOpportunities);
  } else {
    refreshOpportunities();
  }
}

async function refreshOpportunities() {
  try {
    const result = await postJson("/api/tax-opportunities", payloadBase());
    state.opportunities = result;
    renderOpportunities(result);
  } catch (error) {
    state.opportunities = null;
    $("oppSummary").innerHTML = "";
    $("oppCrossed").classList.add("hidden");
    $("oppNote").textContent = "";
    $("oppCompareResult").classList.add("hidden");
    $("oppMilestones").innerHTML = `<p class="opp-empty">Tax opportunities unavailable: ${escapeHtml(
      error.message
    )}</p>`;
  }
}

function renderOpportunities(result) {
  const milestones = result.milestones || [];
  const nextMilestone = milestones[0];
  const otherProceeds = Number(result.otherAnnualProceedsCzk || 0);
  const metrics = [
    [
      `Taxpayer gross ${result.selectedYear}`,
      czk(result.existingGrossProceedsCzk),
      otherProceeds > 0
        ? `${czk(result.loadedGrossProceedsCzk)} loaded + ${czk(otherProceeds)} other`
        : "Counted toward the 100k gross-proceeds rule",
      false,
    ],
    [
      "Remaining under 100k",
      czk(result.remainingGrossAllowanceCzk),
      result.grossLimitCrossed
        ? "Limit already crossed"
        : "Gross proceeds you can still add this year",
      result.grossLimitCrossed,
    ],
    [
      "Next tax-free date",
      nextMilestone ? nextMilestone.taxFreeDate : "—",
      nextMilestone
        ? `${nextMilestone.ticker || nextMilestone.instrumentKey}: +${qty(nextMilestone.quantity)} units`
        : "No lots are waiting on the time test",
      false,
    ],
  ];
  $("oppSummary").innerHTML = metrics
    .map(
      ([label, value, note, danger]) => `
        <article class="metric">
          <span>${escapeHtml(label)}</span>
          <strong${danger ? ' class="crossed"' : ""}>${escapeHtml(value)}</strong>
          <small>${escapeHtml(note)}</small>
        </article>
      `
    )
    .join("");

  const crossed = $("oppCrossed");
  if (result.grossLimitCrossed) {
    crossed.textContent = `100,000 CZK gross-proceeds limit crossed for ${result.selectedYear} — short-term sales this year are taxable unless time-exempt.`;
    crossed.classList.remove("hidden");
  } else {
    crossed.classList.add("hidden");
  }
  $("oppNote").textContent = `// ${result.grossLimitWarning || ""}`;

  if (!milestones.length) {
    $("oppMilestones").innerHTML = `<p class="opp-empty">${
      result.holdingsCount
        ? "No upcoming tax-free milestones — every open lot already passes the 3-year time test."
        : "No open holdings — nothing is waiting on the 3-year time test."
    }</p>`;
  } else {
    const milestoneGroups = groupMilestonesByInstrument(milestones);
    $("oppMilestones").innerHTML = `
      ${renderMilestoneSummary(milestoneGroups)}
      ${renderMilestoneDetails(milestones)}
    `;
  }

  $("oppCompareResult").classList.add("hidden");
  setupOppCompare();
}

function groupMilestonesByInstrument(milestones) {
  const groups = new Map();
  for (const item of milestones) {
    const key = item.instrumentKey || `${item.ticker || ""}:${item.isin || ""}`;
    if (!groups.has(key)) {
      groups.set(key, {
        instrumentKey: item.instrumentKey,
        ticker: item.ticker,
        isin: item.isin,
        name: item.name,
        firstDate: item.taxFreeDate,
        firstQuantity: 0,
        firstCumulativeTaxFreeQuantity: 0,
        lastDate: item.taxFreeDate,
        milestoneCount: 0,
        totalQuantity: 0,
        totalCostCzk: 0,
      });
    }
    const group = groups.get(key);
    const itemQuantity = Number(item.quantity || 0);
    const itemCost = Number(item.costCzk || 0);
    if (item.taxFreeDate === group.firstDate) {
      group.firstQuantity += itemQuantity;
      group.firstCumulativeTaxFreeQuantity = Math.max(
        group.firstCumulativeTaxFreeQuantity,
        Number(item.cumulativeTaxFreeQuantity || 0)
      );
    }
    group.lastDate = item.taxFreeDate;
    group.milestoneCount += 1;
    group.totalQuantity += itemQuantity;
    group.totalCostCzk += itemCost;
  }
  return [...groups.values()].sort((a, b) =>
    a.firstDate === b.firstDate
      ? instrumentLabel(a, false).localeCompare(instrumentLabel(b, false))
      : a.firstDate.localeCompare(b.firstDate)
  );
}

function renderMilestoneSummary(groups) {
  return `
    <table class="milestone-summary-table">
      <thead><tr><th>Instrument</th><th>Next tax-free date</th><th>Qty on next date</th><th>Remaining milestones</th><th>Total pending qty</th><th>Cost basis CZK</th></tr></thead>
      <tbody>
        ${groups
          .map(
            (group) => `
              <tr>
                <td>${instrumentLabel(group)}</td>
                <td>${escapeHtml(group.firstDate)}</td>
                <td class="num">${qty(group.firstQuantity)}</td>
                <td>${escapeHtml(milestoneRangeLabel(group))}</td>
                <td class="num">${qty(group.totalQuantity)}</td>
                <td class="num">${czk(group.totalCostCzk)}</td>
              </tr>
            `
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function milestoneRangeLabel(group) {
  if (group.milestoneCount === 1) {
    return "1 date";
  }
  return `${group.milestoneCount} dates through ${group.lastDate}`;
}

function renderMilestoneDetails(milestones) {
  const visibleRows = milestones.slice(0, 250);
  const hiddenCount = milestones.length - visibleRows.length;
  return `
    <details class="milestone-details">
      <summary>Show exact milestone rows (${milestones.length})</summary>
      <table>
        <thead><tr><th>Tax-free date</th><th>Instrument</th><th>Qty becoming exempt</th><th>Cumulative tax-free</th><th>Cost basis CZK</th></tr></thead>
        <tbody>
          ${visibleRows
            .map(
              (item) => `
                <tr>
                  <td>${escapeHtml(item.taxFreeDate)}</td>
                  <td>${instrumentLabel(item)}</td>
                  <td class="num">${qty(item.quantity)}</td>
                  <td class="num">${qty(item.cumulativeTaxFreeQuantity)}</td>
                  <td class="num">${czk(item.costCzk)}</td>
                </tr>
              `
            )
            .join("")}
        </tbody>
      </table>
      ${
        hiddenCount > 0
          ? `<p class="opp-empty">Showing first ${visibleRows.length} rows. ${hiddenCount} later rows hidden to keep the UI responsive.</p>`
          : ""
      }
    </details>
  `;
}

function setupOppCompare() {
  const select = $("oppInstrument");
  const previous = select.value;
  select.innerHTML = state.holdings
    .map(
      (holding) =>
        `<option value="${escapeHtml(holding.instrumentKey)}">${instrumentLabel(holding, false)}</option>`
    )
    .join("");
  if (previous && state.holdings.some((holding) => holding.instrumentKey === previous)) {
    select.value = previous;
  }
  if (!$("oppDateA").value) $("oppDateA").value = today;
  syncOppCompareDefaults();
}

function syncOppCompareDefaults() {
  const holding = state.holdings.find(
    (item) => item.instrumentKey === $("oppInstrument").value
  );
  if (!holding) return;
  if (!$("oppQuantity").value) $("oppQuantity").value = holding.quantity;
  $("oppDateB").value = holding.nextTaxFreeDate || "";
}

async function runOppCompare() {
  if (!state.result) {
    showError("Analyze a report before comparing sale dates.");
    return;
  }

  setBusy("oppCompareBtn", true);
  clearError();
  try {
    const result = await postJson("/api/tax-opportunities/compare", {
      ...payloadBase(),
      instrumentKey: $("oppInstrument").value,
      quantity: $("oppQuantity").value,
      pricePerShareCzk: $("oppPrice").value,
      firstDate: $("oppDateA").value,
      secondDate: $("oppDateB").value,
    });
    renderOppCompare(result);
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy("oppCompareBtn", false);
  }
}

function renderOppCompare(result) {
  const box = $("oppCompareResult");
  const first = result.first || {};
  const second = result.second || {};
  const diff = result.comparison || {};
  const rows = [
    ["Est. proceeds", first.estimatedProceedsCzk, second.estimatedProceedsCzk, null],
    ["Exempt proceeds", first.exemptProceedsCzk, second.exemptProceedsCzk, diff.exemptProceedsDifferenceCzk],
    ["Taxable proceeds", first.taxableProceedsCzk, second.taxableProceedsCzk, diff.taxableProceedsDifferenceCzk],
    ["Taxable gain delta", first.taxableGainDeltaCzk, second.taxableGainDeltaCzk, diff.taxableGainDifferenceCzk],
    ["Est. tax at 15%", first.estimatedTaxDelta15Czk, second.estimatedTaxDelta15Czk, diff.estimatedTaxDifference15Czk],
    ["Gross proceeds after sale", first.grossProceedsAfterCzk, second.grossProceedsAfterCzk, diff.grossProceedsAfterDifferenceCzk],
  ];

  const taxSaving = -Number(diff.estimatedTaxDifference15Czk || 0);
  let note = "No estimated tax difference between the two dates.";
  if (taxSaving > 0) {
    note = `Waiting until ${second.saleDate} saves about ${czk(taxSaving)} in estimated tax.`;
  } else if (taxSaving < 0) {
    note = `Selling on ${first.saleDate} is cheaper by about ${czk(-taxSaving)} in estimated tax.`;
  }

  const warnings = [...new Set([...(first.warnings || []), ...(second.warnings || [])])];
  const warningsNote = warnings.length
    ? `<p class="plan-summary-note" style="color: var(--amber); padding: 0 14px 14px;">${warnings
        .map((warning) => escapeHtml(warning))
        .join("<br>")}</p>`
    : "";

  box.innerHTML = `
    <table class="opp-compare-table">
      <thead><tr><th></th><th>A · ${escapeHtml(first.saleDate || "")}</th><th>B · ${escapeHtml(
    second.saleDate || ""
  )}</th><th>B − A</th></tr></thead>
      <tbody>
        ${rows
          .map(
            ([label, a, b, d]) => `
              <tr>
                <td>${escapeHtml(label)}</td>
                <td class="num">${czk(a)}</td>
                <td class="num">${czk(b)}</td>
                <td class="num">${d == null ? "—" : czk(d)}</td>
              </tr>
            `
          )
          .join("")}
      </tbody>
    </table>
    <p class="opp-compare-note ${taxSaving > 0 ? "save" : ""}">${escapeHtml(note)}</p>
    ${warningsNote}
  `;
  box.classList.remove("hidden");
}

function renderSummary(summary) {
  const otherProceeds = Number(summary.otherAnnualProceedsCzk || 0);
  const metrics = [
    [
      "Gross proceeds",
      czk(summary.grossProceedsCzk),
      otherProceeds > 0
        ? `${czk(summary.loadedGrossProceedsCzk)} loaded + ${czk(otherProceeds)} other`
        : `Year ${summary.year}`,
    ],
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
  state.holdings = holdings || [];
  const select = $("planInstrument");
  select.innerHTML = holdings
    .map(
      (holding) =>
        `<option value="${escapeHtml(holding.instrumentKey)}">${instrumentLabel(holding, false)}</option>`
    )
    .join("");

  if (holdings.length) {
    $("planQuantity").value = holdings[0].quantity;
  }

  state.multiRows = [];
  addMultiRow();
  renderMultiRows();
  renderTargetInstruments();
  updateSinglePriceMeta();
  ["planResult", "multiResult", "targetResult"].forEach((id) => $(id).classList.add("hidden"));
}

function switchPlanTab(tab) {
  document
    .querySelectorAll("[data-plan-tab]")
    .forEach((button) => button.classList.toggle("active", button.dataset.planTab === tab));
  $("planTabSingle").classList.toggle("hidden", tab !== "single");
  $("planTabMulti").classList.toggle("hidden", tab !== "multi");
  $("planTabTarget").classList.toggle("hidden", tab !== "target");
}

function addMultiRow() {
  state.multiRowSeq += 1;
  state.multiRows.push({
    id: state.multiRowSeq,
    instrumentKey: state.holdings[0]?.instrumentKey || "",
    quantity: "",
    price: "",
    source: "fetch",
  });
}

function renderMultiRows() {
  const options = (selected) =>
    state.holdings
      .map(
        (holding) =>
          `<option value="${escapeHtml(holding.instrumentKey)}" ${
            holding.instrumentKey === selected ? "selected" : ""
          }>${instrumentLabel(holding)}</option>`
      )
      .join("");

  $("multiRows").innerHTML = state.multiRows
    .map((row) => {
      const quote = row.source === "fetch" ? state.quotes[row.instrumentKey] : null;
      const priceValue =
        row.source === "fetch" ? (quote?.priceCzk ? roundCzk(quote.priceCzk) : "") : row.price;
      let meta = "manual price";
      let metaClass = "";
      if (row.source === "fetch") {
        if (quote?.priceCzk) {
          meta = `${quote.provider} · ${formatAsOf(quote.asOf)}`;
          metaClass = "ok";
        } else {
          meta = "no quote — refresh px or go manual";
          metaClass = "warn";
        }
      }
      return `
        <div class="multi-row" data-row="${row.id}">
          <select data-field="instrumentKey">${options(row.instrumentKey)}</select>
          <input data-field="quantity" type="number" min="0" step="0.00000001" value="${escapeHtml(
            String(row.quantity ?? "")
          )}" />
          <input data-field="price" type="number" min="0" step="0.01" value="${escapeHtml(
            String(priceValue ?? "")
          )}" ${row.source === "fetch" ? "readonly" : ""} />
          <select data-field="source">
            <option value="fetch" ${row.source === "fetch" ? "selected" : ""}>FETCH</option>
            <option value="manual" ${row.source === "manual" ? "selected" : ""}>MANUAL</option>
          </select>
          <span class="row-meta ${metaClass}">${escapeHtml(meta)}</span>
          <button type="button" data-remove="${row.id}" title="Remove row">×</button>
        </div>
      `;
    })
    .join("");
}

function syncMultiRow(event) {
  const field = event.target.dataset?.field;
  const rowEl = event.target.closest("[data-row]");
  if (!field || !rowEl) return;
  const row = state.multiRows.find((item) => String(item.id) === rowEl.dataset.row);
  if (!row) return;
  row[field] = event.target.value;
  // Re-render only on structural changes; re-rendering on keystrokes would drop focus.
  if (event.type === "change" && (field === "source" || field === "instrumentKey")) {
    renderMultiRows();
  }
}

function renderTargetInstruments() {
  const existing = document.querySelectorAll("#targetInstruments input[data-target-key]");
  const previous = existing.length
    ? new Set(
        [...existing].filter((input) => input.checked).map((input) => input.dataset.targetKey)
      )
    : null;

  $("targetInstruments").innerHTML = state.holdings
    .map((holding) => {
      const quote = state.quotes[holding.instrumentKey];
      const hasQuote = Boolean(quote?.priceCzk);
      const checked = previous ? previous.has(holding.instrumentKey) : true;
      const px = hasQuote ? czk(Number(quote.priceCzk)) : "no px";
      return `
        <label class="${hasQuote ? "has-quote" : ""}">
          <input type="checkbox" data-target-key="${escapeHtml(holding.instrumentKey)}" ${
        checked ? "checked" : ""
      } />
          <span>${instrumentLabel(holding)}</span>
          <small>${escapeHtml(px)}</small>
        </label>
      `;
    })
    .join("");
}

async function refreshPrices(busyId) {
  if (!state.holdings.length) {
    showError("Analyze a report before fetching prices.");
    return;
  }

  setBusy(busyId, true);
  clearError();
  try {
    const result = await postJson("/api/prices/quote", {
      instruments: state.holdings.map((holding) => ({
        instrumentKey: holding.instrumentKey,
        ticker: holding.ticker,
        isin: holding.isin,
      })),
      rates: $("rates").value,
      forceRefresh: true,
    });
    state.quotes = result.quotes || {};
    applyFetchedFxRates(result.fxRates);
    renderMultiRows();
    renderTargetInstruments();
    updateSinglePriceMeta();

    const count = Object.keys(state.quotes).length;
    const warningText = (result.warnings || []).join(" · ");
    const fxText = formatFxRates(result.fxRates);
    ["multiPriceMeta", "targetPriceMeta"].forEach((id) => {
      const el = $(id);
      el.innerHTML = `<b>${count}</b> quote${count === 1 ? "" : "s"} fetched${
        fxText ? ` · FX fetched: ${escapeHtml(fxText)}` : ""
      }${warningText ? ` · ${escapeHtml(warningText)}` : ""}`;
      el.classList.remove("hidden");
    });
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy(busyId, false);
  }
}

async function fetchSinglePrice() {
  const key = $("planInstrument").value;
  const holding = state.holdings.find((item) => item.instrumentKey === key);
  if (!holding) {
    showError("Analyze a report and choose an instrument first.");
    return;
  }

  setBusy("planFetchPriceBtn", true);
  clearError();
  try {
    const result = await postJson("/api/prices/quote", {
      instruments: [
        { instrumentKey: holding.instrumentKey, ticker: holding.ticker, isin: holding.isin },
      ],
      rates: $("rates").value,
      forceRefresh: true,
    });
    state.quotes = { ...state.quotes, ...(result.quotes || {}) };
    applyFetchedFxRates(result.fxRates);
    const quote = state.quotes[key];
    if (quote?.priceCzk) {
      $("planPrice").value = roundCzk(quote.priceCzk);
      updateSinglePriceMeta();
    } else {
      showError(
        (result.warnings || []).join(" ") || "No quote available. Enter the price manually."
      );
    }
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy("planFetchPriceBtn", false);
  }
}

function formatFxRates(fxRates) {
  return Object.entries(fxRates || {})
    .map(([currency, rate]) => `${currency}=${Number(rate).toFixed(3)}`)
    .join(", ");
}

function updateSinglePriceMeta() {
  const quote = state.quotes[$("planInstrument").value];
  const el = $("planPriceMeta");
  if (!quote?.priceCzk) {
    el.classList.add("hidden");
    return;
  }
  const fxBit =
    quote.currency && quote.currency !== "CZK" && quote.fxRate
      ? ` · ${escapeHtml(quote.currency)}=${escapeHtml(Number(quote.fxRate).toFixed(3))}${
          quote.fxSource === "fetched" ? " (fetched)" : ""
        }`
      : "";
  el.innerHTML = `PX <b>${escapeHtml(roundCzk(quote.priceCzk))} CZK</b> · ${escapeHtml(
    quote.provider || ""
  )}${quote.symbol ? ` (${escapeHtml(quote.symbol)})` : ""}${fxBit} · as of <b>${escapeHtml(
    formatAsOf(quote.asOf)
  )}</b>`;
  el.classList.remove("hidden");
}

async function runMultiPlan() {
  if (!state.result) {
    showError("Analyze a report before planning.");
    return;
  }

  const rows = [];
  for (let index = 0; index < state.multiRows.length; index += 1) {
    const row = state.multiRows[index];
    const label = `Row ${index + 1}`;
    if (!row.instrumentKey) {
      showError(`${label}: choose an instrument.`);
      return;
    }
    let price = row.price;
    if (row.source === "fetch") {
      const quote = state.quotes[row.instrumentKey];
      if (!quote?.priceCzk) {
        showError(`${label}: no fetched price. Refresh prices or switch the row to manual.`);
        return;
      }
      price = quote.priceCzk;
    }
    rows.push({
      instrumentKey: row.instrumentKey,
      quantity: String(row.quantity || ""),
      pricePerShareCzk: String(price || ""),
    });
  }

  setBusy("multiPlanBtn", true);
  clearError();
  try {
    const result = await postJson("/api/plan/batch", {
      ...payloadBase(),
      saleDate: $("multiDate").value,
      rows,
    });
    renderScenario("multiResult", result, {});
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy("multiPlanBtn", false);
  }
}

async function runTargetPlan() {
  if (!state.result) {
    showError("Analyze a report before planning.");
    return;
  }

  const keys = [
    ...document.querySelectorAll("#targetInstruments input[data-target-key]:checked"),
  ].map((input) => input.dataset.targetKey);
  if (!keys.length) {
    showError("Pick at least one candidate instrument.");
    return;
  }

  const quotes = {};
  keys.forEach((key) => {
    const quote = state.quotes[key];
    if (quote?.priceCzk) {
      quotes[key] = { pricePerShareCzk: String(quote.priceCzk) };
    }
  });
  if (!Object.keys(quotes).length) {
    showError("No prices available for the selected instruments. Refresh prices first.");
    return;
  }

  setBusy("targetPlanBtn", true);
  clearError();
  try {
    const result = await postJson("/api/plan/target-proceeds", {
      ...payloadBase(),
      saleDate: $("targetDate").value,
      targetProceedsCzk: String($("targetAmount").value || ""),
      optimizationMode: $("targetMode").value,
      candidateInstrumentKeys: keys,
      quotes,
    });
    renderScenario("targetResult", result, { optimizer: true });
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy("targetPlanBtn", false);
  }
}

function renderScenario(containerId, result, opts = {}) {
  const box = $(containerId);
  const metrics = [];
  if (result.targetProceedsCzk != null) {
    metrics.push(["Target", czk(result.targetProceedsCzk)]);
  }
  metrics.push(["Est. proceeds", czk(result.estimatedProceedsCzk)]);
  if (Number(result.shortfallCzk || 0) > 0) {
    metrics.push(["Shortfall", czk(result.shortfallCzk)]);
  }
  metrics.push(["Taxable gain delta", czk(result.taxableGainDeltaCzk)]);
  metrics.push(["Tax delta at 15%", czk(result.estimatedTaxDelta15Czk)]);
  metrics.push(["After-sale gross proceeds", czk(result.afterSummary.grossProceedsCzk)]);

  const optimizerNote =
    opts.optimizer && result.optimizer
      ? `<p class="plan-summary-note">OPTIMIZER: ${escapeHtml(result.optimizer.mode)} · ${escapeHtml(
          result.optimizer.strategy
        )} · target ${result.optimizer.targetReached ? "reached" : "NOT reached"} · ${
          result.optimizer.candidateCount
        } candidate lots</p>`
      : "";

  const rows = result.rows || [];
  const rowsTable = rows.length
    ? `
      <div class="plan-rows-table">
        <table>
          <thead><tr><th>Sell</th><th>Qty</th><th>Px CZK</th><th>Proceeds</th><th>Taxable gain</th><th>Status</th><th>Remaining</th></tr></thead>
          <tbody>
            ${rows
              .map(
                (row) => `
                  <tr>
                    <td>${instrumentLabel(row)}</td>
                    <td class="num">${qty(row.quantity)}</td>
                    <td class="num">${czk(row.pricePerShareCzk)}</td>
                    <td class="num">${czk(row.estimatedProceedsCzk)}</td>
                    <td class="num">${czk(row.taxableGainCzk)}</td>
                    <td><span class="badge ${Number(row.taxableGainCzk) > 0 ? "tax" : "ok"}">${escapeHtml(
                  row.taxStatusSummary || ""
                )}</span></td>
                    <td class="num">${qty(row.remainingQuantity)}</td>
                  </tr>
                `
              )
              .join("")}
          </tbody>
        </table>
      </div>
    `
    : "";

  const groups = result.lotGroups || [];
  const groupTable = groups.length
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
                <span>${escapeHtml(`${group.ticker || group.instrumentKey} · ${planDateRange(group)}`)}</span>
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

  const warningsNote = (result.warnings || []).length
    ? `<p class="plan-summary-note" style="color: var(--amber)">${result.warnings
        .map((warning) => escapeHtml(warning))
        .join("<br>")}</p>`
    : "";

  box.innerHTML = `
    <div class="plan-result-grid">
      ${metrics
        .map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
        .join("")}
    </div>
    ${optimizerNote}
    ${rowsTable}
    ${groupTable}
    ${warningsNote}
  `;
  box.classList.remove("hidden");
}

function roundCzk(value) {
  return Number(value || 0).toFixed(2);
}

function formatAsOf(value) {
  if (!value) return "unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("cs-CZ", { dateStyle: "short", timeStyle: "short" });
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

  const planWarnings = (result.warnings || []).length
    ? `<p class="plan-summary-note" style="color: var(--amber)">${result.warnings
        .map((warning) => escapeHtml(warning))
        .join("<br>")}</p>`
    : "";

  box.innerHTML = `
    <div class="plan-result-grid">
      <div><span>Planned proceeds</span><strong>${czk(result.plannedProceedsCzk)}</strong></div>
      <div><span>Taxable gain delta</span><strong>${czk(result.deltaTaxableGainCzk)}</strong></div>
      <div><span>Tax delta at 15%</span><strong>${czk(result.deltaEstimatedTax15Czk)}</strong></div>
      <div><span>After-sale gross proceeds</span><strong>${czk(result.afterSummary.grossProceedsCzk)}</strong></div>
    </div>
    ${plannedGroups}
    ${planWarnings}
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
