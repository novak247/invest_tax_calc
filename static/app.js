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
  plannerMode: "single",
  quotes: {},
  multiRowCounter: 0,
};

const $ = (id) => document.getElementById(id);

const today = new Date().toISOString().slice(0, 10);
$("asOf").value = today;
$("planDate").value = today;
$("multiPlanDate").value = today;
$("targetPlanDate").value = today;
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
$("multiPlanBtn").addEventListener("click", runMultiPlan);
$("targetPlanBtn").addEventListener("click", runTargetPlan);
$("addPlanRowBtn").addEventListener("click", () => addMultiPlanRow());
$("plannerRefreshBtn").addEventListener("click", refreshPlannerPrices);
$("planInstrument").addEventListener("change", syncSinglePlannerInstrument);
$("planPriceSource").addEventListener("change", applyPlannerQuotes);
$("multiPlanRows").addEventListener("click", handleMultiPlanClick);
$("multiPlanRows").addEventListener("input", updateMultiRowProceeds);
$("multiPlanRows").addEventListener("change", updateMultiRowProceeds);
document.querySelectorAll("[data-planner-mode]").forEach((button) => {
  button.addEventListener("click", () => switchPlannerMode(button.dataset.plannerMode));
});
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
    const result = await postJson("/api/plan/batch", {
      ...payloadBase(),
      saleDate: $("planDate").value,
      rows: [
        {
          instrumentKey: $("planInstrument").value,
          quantity: $("planQuantity").value,
          pricePerShareCzk: $("planPrice").value,
          priceSource: $("planPriceSource").value,
        },
      ],
    });
    renderPlan(result);
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy("planBtn", false);
  }
}

async function runMultiPlan() {
  if (!state.result) {
    showError("Analyze a report before planning a sale.");
    return;
  }

  const rows = [...document.querySelectorAll(".editable-plan-row")].map((row) => ({
    instrumentKey: row.querySelector(".multi-instrument").value,
    quantity: row.querySelector(".multi-qty").value,
    pricePerShareCzk: row.querySelector(".multi-price").value,
    priceSource: row.querySelector(".multi-source").value,
  }));
  setBusy("multiPlanBtn", true);
  clearError();
  try {
    renderPlan(
      await postJson("/api/plan/batch", {
        ...payloadBase(),
        saleDate: $("multiPlanDate").value,
        rows,
      })
    );
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy("multiPlanBtn", false);
  }
}

async function runTargetPlan() {
  if (!state.result) {
    showError("Analyze a report before optimizing sales.");
    return;
  }

  const selected = [...document.querySelectorAll(".target-candidate-check:checked")];
  if (!selected.length) {
    showError("Choose at least one target optimizer candidate.");
    return;
  }
  const quotes = Object.fromEntries(
    selected.map((checkbox) => {
      const row = checkbox.closest("tr");
      return [
        checkbox.value,
        { pricePerShareCzk: row.querySelector(".target-candidate-price").value },
      ];
    })
  );
  setBusy("targetPlanBtn", true);
  clearError();
  try {
    renderPlan(
      await postJson("/api/plan/target-proceeds", {
        ...payloadBase(),
        saleDate: $("targetPlanDate").value,
        targetProceedsCzk: $("targetProceeds").value,
        optimizationMode: $("targetMode").value,
        candidateInstrumentKeys: selected.map((checkbox) => checkbox.value),
        quotes,
      })
    );
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy("targetPlanBtn", false);
  }
}

async function refreshPlannerPrices() {
  if (!state.result) {
    showError("Analyze a report before refreshing prices.");
    return;
  }
  const refs = plannerPriceRefs();
  if (!refs.length) {
    showError("Choose at least one instrument to refresh.");
    return;
  }

  setBusy("plannerRefreshBtn", true);
  clearError();
  try {
    const result = await postJson("/api/prices/quote", {
      instruments: refs,
      asOf: plannerSaleDate(),
    });
    state.quotes = { ...state.quotes, ...(result.quotes || {}) };
    applyPlannerQuotes();
    const fetched = Object.values(result.quotes || {});
    const details = fetched.length
      ? fetched.map((quote) => `${quote.provider} @ ${quote.asOf}`).join("; ")
      : "";
    setPlannerPriceStatus(
      [...(result.warnings || []), details].filter(Boolean).join(" ") ||
        "Prices refreshed."
    );
  } catch (error) {
    setPlannerPriceStatus(error.message, true);
    showError(error.message);
  } finally {
    setBusy("plannerRefreshBtn", false);
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
  state.quotes = {};
  const select = $("planInstrument");
  select.innerHTML = holdingOptions(holdings);

  if (holdings.length) {
    $("planQuantity").value = holdings[0].quantity;
    $("planResult").classList.add("hidden");
  }
  $("multiPlanRows").innerHTML = "";
  state.multiRowCounter = 0;
  if (holdings.length) addMultiPlanRow();
  renderTargetCandidates(holdings);
  setPlannerPriceStatus("Prices stay local until you explicitly refresh them.");
}

function switchPlannerMode(mode) {
  state.plannerMode = mode;
  document.querySelectorAll("[data-planner-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.plannerMode === mode);
  });
  $("singlePlannerPane").classList.toggle("hidden", mode !== "single");
  $("multiPlannerPane").classList.toggle("hidden", mode !== "multi");
  $("targetPlannerPane").classList.toggle("hidden", mode !== "target");
}

function syncSinglePlannerInstrument() {
  const holding = state.result?.holdings?.find(
    (item) => item.instrumentKey === $("planInstrument").value
  );
  if (holding) $("planQuantity").value = holding.quantity;
  applyPlannerQuotes();
}

function holdingOptions(holdings, selected = "") {
  return holdings
    .map(
      (holding) =>
        `<option value="${escapeHtml(holding.instrumentKey)}" ${
          holding.instrumentKey === selected ? "selected" : ""
        }>${escapeHtml(instrumentLabel(holding, false))}</option>`
    )
    .join("");
}

function addMultiPlanRow() {
  const holdings = state.result?.holdings || [];
  if (!holdings.length) return;
  state.multiRowCounter += 1;
  const holding = holdings[(state.multiRowCounter - 1) % holdings.length];
  $("multiPlanRows").insertAdjacentHTML(
    "beforeend",
    `
      <div class="editable-plan-row" data-plan-row="${state.multiRowCounter}">
        <label>Instrument<select class="multi-instrument">${holdingOptions(holdings, holding.instrumentKey)}</select></label>
        <label>Qty<input class="multi-qty" type="number" min="0" step="0.000001" value="${escapeHtml(holding.quantity)}" /></label>
        <label>Source<select class="multi-source"><option value="manual">Manual</option><option value="fetch">Fetched / fallback</option></select></label>
        <label>Price CZK<input class="multi-price" type="number" min="0" step="0.01" /></label>
        <span class="row-proceeds">CZK 0</span>
        <button type="button" class="remove-plan-row">REMOVE</button>
      </div>
    `
  );
}

function handleMultiPlanClick(event) {
  const remove = event.target.closest(".remove-plan-row");
  if (!remove) return;
  remove.closest(".editable-plan-row").remove();
}

function updateMultiRowProceeds(event) {
  const row = event.target.closest(".editable-plan-row");
  if (!row) return;
  if (event.target.classList.contains("multi-instrument")) {
    const holding = state.result.holdings.find(
      (item) => item.instrumentKey === event.target.value
    );
    if (holding) row.querySelector(".multi-qty").value = holding.quantity;
  }
  if (
    event.target.classList.contains("multi-instrument") ||
    event.target.classList.contains("multi-source")
  ) {
    applyQuoteToMultiRow(row);
  }
  const proceeds =
    Number(row.querySelector(".multi-qty").value || 0) *
    Number(row.querySelector(".multi-price").value || 0);
  row.querySelector(".row-proceeds").textContent = czk(proceeds);
}

function renderTargetCandidates(holdings) {
  $("targetCandidates").innerHTML = holdings.length
    ? `
      <table>
        <thead><tr><th>Use</th><th>Instrument</th><th>Available</th><th>Tax-free now</th><th>Price/share CZK</th></tr></thead>
        <tbody>
          ${holdings
            .map(
              (holding) => `
                <tr>
                  <td><input class="target-candidate-check" type="checkbox" value="${escapeHtml(holding.instrumentKey)}" checked /></td>
                  <td>${instrumentLabel(holding)}</td>
                  <td class="num">${qty(holding.quantity)}</td>
                  <td class="num">${qty(holding.taxFreeQuantityNow)}</td>
                  <td><input class="target-candidate-price" data-instrument-key="${escapeHtml(holding.instrumentKey)}" type="number" min="0" step="0.01" /></td>
                </tr>
              `
            )
            .join("")}
        </tbody>
      </table>
    `
    : "<p>No open holdings are available.</p>";
}

function plannerPriceRefs() {
  let keys = [];
  if (state.plannerMode === "single") {
    keys = [$("planInstrument").value];
  } else if (state.plannerMode === "multi") {
    keys = [...document.querySelectorAll(".multi-instrument")].map((select) => select.value);
  } else {
    keys = [...document.querySelectorAll(".target-candidate-check:checked")].map(
      (checkbox) => checkbox.value
    );
  }
  return [...new Set(keys)]
    .map((key) => state.result.holdings.find((holding) => holding.instrumentKey === key))
    .filter(Boolean)
    .map((holding) => ({
      instrumentKey: holding.instrumentKey,
      ticker: holding.ticker,
      isin: holding.isin,
    }));
}

function plannerSaleDate() {
  if (state.plannerMode === "multi") return $("multiPlanDate").value;
  if (state.plannerMode === "target") return $("targetPlanDate").value;
  return $("planDate").value;
}

function applyPlannerQuotes() {
  const singleQuote = state.quotes[$("planInstrument").value];
  if ($("planPriceSource").value === "fetch" && singleQuote) {
    $("planPrice").value = singleQuote.priceCzk;
  }
  document.querySelectorAll(".editable-plan-row").forEach(applyQuoteToMultiRow);
  document.querySelectorAll(".target-candidate-price").forEach((input) => {
    const quote = state.quotes[input.dataset.instrumentKey];
    if (quote) input.value = quote.priceCzk;
  });
}

function applyQuoteToMultiRow(row) {
  const key = row.querySelector(".multi-instrument").value;
  const quote = state.quotes[key];
  if (row.querySelector(".multi-source").value === "fetch" && quote) {
    row.querySelector(".multi-price").value = quote.priceCzk;
  }
  const proceeds =
    Number(row.querySelector(".multi-qty").value || 0) *
    Number(row.querySelector(".multi-price").value || 0);
  row.querySelector(".row-proceeds").textContent = czk(proceeds);
}

function setPlannerPriceStatus(message, warning = false) {
  $("plannerPriceStatus").textContent = message;
  $("plannerPriceStatus").classList.toggle("warn", warning || message.includes("No market"));
}

function renderPlan(result) {
  const box = $("planResult");
  const groups = result.lotGroups || result.plannedMatchGroups || [];
  const rows = result.rows || [];
  const targetMetric =
    result.targetProceedsCzk === null || result.targetProceedsCzk === undefined
      ? ["Planned proceeds", czk(result.estimatedProceedsCzk)]
      : [
          "Target / achieved",
          `${czk(result.targetProceedsCzk)} / ${czk(result.estimatedProceedsCzk)}`,
        ];
  const optimizer = result.optimizer
    ? `<p class="plan-reason">Optimizer: ${escapeHtml(result.optimizer.strategy)}. Evaluated ${escapeHtml(
        result.optimizer.evaluatedStrategies.join(", ")
      )}.</p>`
    : "";
  const sales = rows.length
    ? `
      <div class="plan-sales-table">
        <table>
          <thead><tr><th>Instrument</th><th>Qty</th><th>Price</th><th>Proceeds</th><th>Taxable gain</th><th>Remaining</th><th>Why / status</th></tr></thead>
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
                    <td class="num">${qty(row.remainingQuantity)}</td>
                    <td><span class="plan-reason">${escapeHtml(row.reasonSelected || row.taxStatusSummary)}</span></td>
                  </tr>
                `
              )
              .join("")}
          </tbody>
        </table>
      </div>
    `
    : "";
  const plannedGroups = groups.length
    ? `
      <div class="plan-group-table">
        <div class="plan-group-head">
          <span>Instrument / lot range</span><span>Qty</span><span>Proceeds</span><span>Cost</span><span>Gain</span><span>Status</span>
        </div>
        ${groups
          .map(
            (group) => `
              <div class="plan-group-row">
                <span>${instrumentLabel(group)} / ${escapeHtml(planDateRange(group))}</span>
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
  const warnings = (result.warnings || []).length
    ? `<ul class="plan-warning-list">${result.warnings
        .map((warning) => `<li>${escapeHtml(warning)}</li>`)
        .join("")}</ul>`
    : "";

  box.innerHTML = `
    <div class="plan-result-grid">
      <div><span>${escapeHtml(targetMetric[0])}</span><strong>${targetMetric[1]}</strong></div>
      <div><span>Taxable gain delta</span><strong>${czk(result.taxableGainDeltaCzk ?? result.deltaTaxableGainCzk)}</strong></div>
      <div><span>Tax delta at 15%</span><strong>${czk(result.estimatedTaxDelta15Czk ?? result.deltaEstimatedTax15Czk)}</strong></div>
      <div><span>After-sale gross proceeds</span><strong>${czk(result.afterSummary.grossProceedsCzk)}</strong></div>
    </div>
    ${optimizer}${sales}${plannedGroups}${warnings}
  `;
  box.classList.remove("hidden");
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
