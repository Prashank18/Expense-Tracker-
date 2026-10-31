const state = {
  user: null,
  csrf: "",
  categories: [],
  period: "monthly",
  view: "overview",
  dashboard: null,
  transactions: [],
  authMode: "login",
};

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const escapeHtml = (value = "") => String(value).replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));

function money(value, compact = false) {
  const amount = Number(value) || 0;
  if (compact && Math.abs(amount) >= 100000) {
    return `₹${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 1 }).format(amount / 100000)}L`;
  }
  if (compact && Math.abs(amount) >= 10000) {
    return `₹${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 1 }).format(amount / 1000)}k`;
  }
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: compact ? 0 : 2,
    maximumFractionDigits: compact ? 1 : 2,
  }).format(amount);
}

function displayDate(value, options = { day: "numeric", month: "short", year: "numeric" }) {
  if (!value) return "";
  return new Intl.DateTimeFormat("en-IN", options).format(new Date(`${value}T00:00:00`));
}

function todayIso() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toast.timeout);
  toast.timeout = setTimeout(() => element.classList.remove("show"), 3000);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (options.method && options.method !== "GET" && state.csrf) {
    headers["X-CSRF-Token"] = state.csrf;
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || "Something went wrong. Please try again.");
  if (result.csrf) state.csrf = result.csrf;
  return result;
}

function showAuth() {
  $("#auth-screen").hidden = false;
  $("#app-shell").hidden = true;
}

function showApp() {
  $("#auth-screen").hidden = true;
  $("#app-shell").hidden = false;
  $("#user-name").textContent = state.user.name;
  $("#user-avatar").textContent = state.user.name.trim().charAt(0).toUpperCase() || "L";
  $("#today-label").textContent = new Intl.DateTimeFormat("en-IN", {
    weekday: "short", day: "numeric", month: "long",
  }).format(new Date());
  updateTransactionCategories("debit");
  const categories = state.categories.filter((category) => category !== "Income");
  $("#budget-category").innerHTML = categories
    .map((category) => `<option>${escapeHtml(category)}</option>`).join("");
  renderView();
}

function updateTransactionCategories(kind) {
  const categories = kind === "credit"
    ? ["Income", ...state.categories.filter((category) => category !== "Income")]
    : state.categories.filter((category) => category !== "Income");
  $("#transaction-category").innerHTML = categories
    .map((category) => `<option>${escapeHtml(category)}</option>`).join("");
}

function setAuthMode(mode) {
  state.authMode = mode;
  const registering = mode === "register";
  $("#auth-name").closest(".field").hidden = !registering;
  $("#auth-title").textContent = registering ? "A fresh start." : "Welcome back.";
  $("#auth-kicker").textContent = registering ? "MAKE YOUR MONEY MAKE SENSE" : "YOUR PERSONAL LEDGER";
  $("#auth-subtitle").textContent = registering
    ? "Create your private space and begin with a clearer view."
    : "A clear view of your money starts here.";
  $("#auth-submit").innerHTML = registering
    ? 'Create account <span aria-hidden="true">↗</span>'
    : 'Sign in <span aria-hidden="true">↗</span>';
  $("#auth-switch-copy").textContent = registering ? "Already have an account?" : "New around here?";
  $("#auth-switch").textContent = registering ? "Sign in" : "Create an account";
  $("#auth-password").autocomplete = registering ? "new-password" : "current-password";
  $("#auth-error").textContent = "";
}

function budgetRows(budgets, compact = false) {
  if (!budgets.length) {
    return `<div class="budget-empty"><span class="empty-glyph">◉</span><p>No budgets yet. Choose a category and give your month a gentle guardrail.</p></div>`;
  }
  return budgets.map((budget) => {
    const ratio = Math.min(100, Math.round(budget.ratio * 100));
    const status = budget.ratio >= 1 ? "over" : budget.ratio >= 0.8 ? "warning" : "";
    return `<div class="budget-row">
      <div class="budget-row-top"><strong>${escapeHtml(budget.category)}</strong><span>${money(budget.spent, true)} / ${money(budget.limit, true)}</span></div>
      <div class="progress-track" aria-label="${ratio}% of budget used"><div class="progress-fill ${status}" style="width:${ratio}%"></div></div>
    </div>`;
  }).join("");
}

function chartLabel(label, period) {
  if (period === "daily") return new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short" }).format(new Date(`${label}T00:00:00`));
  if (period === "monthly") return new Intl.DateTimeFormat("en-IN", { month: "short" }).format(new Date(`${label}-01T00:00:00`));
  return label;
}

function smoothPath(points) {
  if (!points.length) return "";
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;
  return points.reduce((path, point, index) => {
    if (index === 0) return `M ${point.x} ${point.y}`;
    const previous = points[index - 1];
    const middleX = (previous.x + point.x) / 2;
    return `${path} Q ${middleX} ${previous.y}, ${middleX} ${(previous.y + point.y) / 2} T ${point.x} ${point.y}`;
  }, "");
}

function renderChart(chart, period) {
  const width = 700;
  const height = 220;
  const left = 43;
  const right = width - 12;
  const top = 13;
  const bottom = height - 28;
  const maxValue = Math.max(1, ...chart.flatMap((point) => [point.debit, point.credit])) * 1.18;
  const xAt = (index) => chart.length <= 1 ? (left + right) / 2 : left + (index * (right - left)) / (chart.length - 1);
  const yAt = (value) => bottom - (value / maxValue) * (bottom - top);
  const debitPoints = chart.map((point, index) => ({ x: xAt(index), y: yAt(point.debit) }));
  const creditPoints = chart.map((point, index) => ({ x: xAt(index), y: yAt(point.credit) }));
  const debitLine = smoothPath(debitPoints);
  const creditLine = smoothPath(creditPoints);
  const area = (line, points) => points.length
    ? `${line} L ${points.at(-1).x} ${bottom} L ${points[0].x} ${bottom} Z`
    : "";
  const grid = [0, 1, 2, 3].map((step) => {
    const y = top + (step * (bottom - top)) / 3;
    return `<line class="chart-gridline" x1="${left}" x2="${right}" y1="${y}" y2="${y}"></line>
      <text class="chart-axis" text-anchor="end" x="${left - 8}" y="${y + 3}">${money(maxValue * (1 - step / 3), true)}</text>`;
  }).join("");
  const labels = chart.map((point, index) => {
    const shouldShow = chart.length <= 7 || index % Math.ceil(chart.length / 6) === 0 || index === chart.length - 1;
    return shouldShow ? `<text class="chart-axis" text-anchor="middle" x="${xAt(index)}" y="${height - 7}">${escapeHtml(chartLabel(point.label, period))}</text>` : "";
  }).join("");
  const debitDots = debitPoints.map((point) => `<circle class="chart-point debit" cx="${point.x}" cy="${point.y}" r="3.7"></circle>`).join("");
  const creditDots = creditPoints.map((point) => `<circle class="chart-point credit" cx="${point.x}" cy="${point.y}" r="3.7"></circle>`).join("");
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Credit and debit trend">
    <defs>
      <linearGradient id="debit-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#26654f" stop-opacity=".15"></stop><stop offset="100%" stop-color="#26654f" stop-opacity="0"></stop></linearGradient>
      <linearGradient id="credit-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#d4a84c" stop-opacity=".13"></stop><stop offset="100%" stop-color="#d4a84c" stop-opacity="0"></stop></linearGradient>
    </defs>
    ${grid}
    <path class="chart-area-credit" d="${area(creditLine, creditPoints)}"></path>
    <path class="chart-area-debit" d="${area(debitLine, debitPoints)}"></path>
    <path class="chart-line-credit" d="${creditLine}"></path>
    <path class="chart-line-debit" d="${debitLine}"></path>
    ${creditDots}${debitDots}${labels}
  </svg>`;
}

function transactionRows(transactions, limit = 6) {
  if (!transactions.length) {
    return `<div class="empty-state"><span class="empty-glyph">↗</span><strong>Your story starts here.</strong><p>Add your first debit or credit and your ledger will start to take shape.</p><button class="button button-outline" data-open-transaction>Add a transaction</button></div>`;
  }
  return transactions.slice(0, limit).map((transaction) => `
    <div class="transaction-row">
      <span class="transaction-symbol ${transaction.kind}">${transaction.kind === "credit" ? "↙" : "↗"}</span>
      <span class="transaction-detail"><strong>${escapeHtml(transaction.payee)}</strong><small>${escapeHtml(transaction.category)} · ${displayDate(transaction.transaction_date, { day: "numeric", month: "short" })}</small></span>
      <span class="transaction-amount ${transaction.kind}">${transaction.kind === "credit" ? "+" : "−"}${money(transaction.amount)}</span>
    </div>`).join("");
}

async function renderView() {
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === state.view));
  const titles = { overview: "Overview", transactions: "Transactions", budgets: "Budgets", settings: "Preferences" };
  $("#breadcrumb-title").textContent = titles[state.view] || "Overview";
  try {
    const [dashboard, transactionResponse] = await Promise.all([
      api(`/api/dashboard?period=${state.period}`),
      api("/api/transactions"),
    ]);
    state.dashboard = dashboard;
    state.transactions = transactionResponse.transactions;
    if (state.view === "overview") renderOverview();
    else if (state.view === "transactions") renderTransactions();
    else if (state.view === "budgets") renderBudgets();
    else renderSettings();
  } catch (error) {
    $("#page-content").innerHTML = `<div class="panel"><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function renderOverview() {
  const data = state.dashboard;
  const periodNames = { daily: "Last 7 days", monthly: "Last 6 months", yearly: "Last 5 years" };
  $("#page-content").innerHTML = `
    <div class="page-heading">
      <div><p class="eyebrow">A CLEARER VIEW, ONE DAY AT A TIME</p><h1>Good ${new Date().getHours() < 12 ? "morning" : new Date().getHours() < 18 ? "afternoon" : "evening"}, ${escapeHtml(state.user.name.split(/\s+/)[0])}.</h1><p>Here’s how your money is moving this month.</p></div>
      <div class="heading-actions"><button class="button button-outline" data-open-budget>＋ Set a budget</button></div>
    </div>
    <section class="overview-grid" aria-label="Monthly totals">
      <article class="metric-card net-card"><div class="metric-top"><span>Net this month</span><span class="metric-icon">◒</span></div><strong>${money(data.totals.net)}</strong><small>Credit minus debit</small></article>
      <article class="metric-card"><div class="metric-top"><span>Money in</span><span class="metric-icon">↙</span></div><strong>${money(data.totals.credit)}</strong><small>Credits recorded this month</small></article>
      <article class="metric-card"><div class="metric-top"><span>Money out</span><span class="metric-icon coral">↗</span></div><strong>${money(data.totals.debit)}</strong><small>Debits recorded this month</small></article>
      <article class="metric-card"><div class="metric-top"><span>Budget planned</span><span class="metric-icon lilac">◉</span></div><strong>${money(data.totals.budget)}</strong><small>${data.budgets.length ? `${data.budgets.length} category ${data.budgets.length === 1 ? "limit" : "limits"} set` : "Set a limit that feels right"}</small></article>
    </section>
    <div class="dash-grid">
      <section class="panel trend-panel">
        <div class="panel-heading"><div><h2>Money over time</h2><p>${periodNames[state.period]} · credits and debits</p></div>
          <div class="period-switch" role="group" aria-label="Chart period">
            ${["daily", "monthly", "yearly"].map((period) => `<button class="${state.period === period ? "active" : ""}" data-period="${period}">${period[0].toUpperCase()}${period.slice(1)}</button>`).join("")}
          </div>
        </div>
        <div class="chart-legend"><span><i class="legend-dot"></i> Debit</span><span><i class="legend-dot credit"></i> Credit</span></div>
        <div class="chart-wrap">${renderChart(data.chart, state.period)}</div>
      </section>
      <section class="panel budget-panel">
        <div class="panel-heading"><div><h2>Budget check-in</h2><p>Your month, at a glance</p></div><button class="icon-button" aria-label="View budgets" data-view="budgets">↗</button></div>
        <div class="budget-list">${budgetRows(data.budgets, true)}</div>
        <div class="budget-panel-foot"><button class="button button-outline" data-open-budget>＋ Add category budget</button></div>
      </section>
    </div>
    <div class="lower-grid">
      <section class="panel">
        <div class="panel-heading"><div><h2>Recent activity</h2><p>Your latest ledger entries</p></div><button class="text-button" data-view="transactions">See all <span aria-hidden="true">↗</span></button></div>
        <div class="transaction-list">${transactionRows(data.transactions)}</div>
      </section>
      <section class="panel">
        <div class="panel-heading"><div><h2>Where it goes</h2><p>Debit by category this month</p></div></div>
        ${data.categories.length
          ? `<div class="category-list">${data.categories.map((item) => `<div class="category-item"><span><i class="category-mark"></i>${escapeHtml(item.category)}</span><strong>${money(item.spent)}</strong></div>`).join("")}</div>`
          : `<div class="empty-state"><p>Your spending categories will appear here as you add transactions.</p></div>`}
      </section>
    </div>`;
}

function renderTransactions() {
  const filter = $("#transaction-filter")?.value || "all";
  const records = filter === "all" ? state.transactions : state.transactions.filter((item) => item.kind === filter);
  $("#page-content").innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">EVERYTHING IN ITS PLACE</p><h1>Transactions</h1><p>A running record of the money coming in and going out.</p></div><div class="heading-actions"><button class="button button-primary" data-open-transaction>＋ Add transaction</button></div></div>
    <section class="panel table-panel">
      <div class="table-toolbar"><h2>${records.length} ${records.length === 1 ? "entry" : "entries"}</h2><select class="filter-select" id="transaction-filter" aria-label="Filter transaction type"><option value="all" ${filter === "all" ? "selected" : ""}>All activity</option><option value="debit" ${filter === "debit" ? "selected" : ""}>Debits only</option><option value="credit" ${filter === "credit" ? "selected" : ""}>Credits only</option></select></div>
      ${records.length ? `<table class="transaction-table"><thead><tr><th>Transaction</th><th>Category</th><th>Date</th><th>Type</th><th>Amount</th></tr></thead><tbody>${records.map((item) => `<tr><td><strong>${escapeHtml(item.payee)}</strong>${item.note ? `<div class="table-muted">${escapeHtml(item.note)}</div>` : ""}</td><td>${escapeHtml(item.category)}</td><td>${displayDate(item.transaction_date)}</td><td><span class="type-pill ${item.kind}">${item.kind === "credit" ? "↙ Credit" : "↗ Debit"}</span></td><td>${item.kind === "credit" ? "+" : "−"}${money(item.amount)}</td></tr>`).join("")}</tbody></table>`
        : `<div class="empty-state"><span class="empty-glyph">⇄</span><strong>No transactions in this view.</strong><p>New activity will show up here, with each debit and credit clearly marked.</p><button class="button button-outline" data-open-transaction>Add a transaction</button></div>`}
    </section>`;
}

function renderBudgets() {
  const budgets = state.dashboard.budgets;
  $("#page-content").innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">A LITTLE INTENTION GOES A LONG WAY</p><h1>Your budgets</h1><p>Set a monthly limit by category. We’ll let you know when spending reaches it.</p></div><div class="heading-actions"><button class="button button-primary" data-open-budget>＋ Set a budget</button></div></div>
    ${budgets.length
      ? `<div class="budget-page-grid">${budgets.map((budget) => {
        const ratio = Math.min(100, Math.round(budget.ratio * 100));
        const status = budget.ratio >= 1 ? "over" : budget.ratio >= .8 ? "warning" : "";
        return `<article class="budget-card"><div class="budget-card-head"><strong>${escapeHtml(budget.category)}</strong><span class="budget-card-icon">◉</span></div><div class="budget-card-numbers"><strong>${money(budget.spent)}</strong><span>of ${money(budget.limit)}</span></div><div class="progress-track"><div class="progress-fill ${status}" style="width:${ratio}%"></div></div><small>${ratio}% used this month</small></article>`;
      }).join("")}</div>`
      : `<section class="panel empty-state"><span class="empty-glyph">◉</span><strong>Give your spending a little direction.</strong><p>Choose a category and a monthly amount. You can adjust it whenever life changes.</p><button class="button button-primary" data-open-budget>Set your first budget</button></section>`}`;
}

function renderSettings() {
  const ready = Boolean(state.user.whatsapp_ready && state.user.whatsapp_phone && state.user.whatsapp_opt_in);
  $("#page-content").innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">MAKE IT FEEL LIKE YOURS</p><h1>Preferences</h1><p>Choose where your budget alerts should find you.</p></div></div>
    <div class="settings-grid">
      <section class="panel settings-panel">
        <h2>WhatsApp budget alerts</h2>
        <p>Get one message when a category crosses its monthly budget. Your number is stored with your account and is never shown publicly.</p>
        <form id="settings-form">
          <label class="field" for="whatsapp-phone">WhatsApp number <span class="optional-label">INTERNATIONAL FORMAT</span><input id="whatsapp-phone" type="tel" value="${escapeHtml(state.user.whatsapp_phone || "")}" placeholder="+14155550123" autocomplete="tel"></label>
          <label class="consent-check"><input id="whatsapp-opt-in" type="checkbox" ${state.user.whatsapp_opt_in ? "checked" : ""}><span>I agree to receive WhatsApp messages when one of my monthly category budgets is exceeded.</span></label>
          <button class="button button-primary" type="submit">Save alert settings <span>↗</span></button>
          <p class="form-error" id="settings-error" role="alert"></p>
        </form>
      </section>
      <section class="panel settings-panel">
        <h2>Delivery status</h2>
        <p>Alerts are checked as new debits are recorded and sent once per category each month.</p>
        <div class="status-box"><i class="status-dot ${ready ? "ready" : ""}"></i><span>${ready ? "WhatsApp alerts are enabled for your account." : "Add your number and opt in here. Delivery also needs the app owner to configure Twilio and an approved WhatsApp template on the server."}</span></div>
        <p class="settings-footnote">Without a connected provider, your budgets and in-app progress still work normally.</p>
      </section>
    </div>`;
  $("#settings-form").addEventListener("submit", saveSettings);
}

async function saveSettings(event) {
  event.preventDefault();
  const error = $("#settings-error");
  error.textContent = "";
  try {
    const result = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        whatsapp_phone: $("#whatsapp-phone").value.trim(),
        whatsapp_opt_in: $("#whatsapp-opt-in").checked,
      }),
    });
    state.user.whatsapp_phone = $("#whatsapp-phone").value.trim();
    state.user.whatsapp_opt_in = $("#whatsapp-opt-in").checked;
    state.user.whatsapp_ready = result.whatsapp_ready;
    toast(result.whatsapp_ready ? "WhatsApp alert settings saved." : "Number saved. Provider setup is still needed.");
    renderSettings();
  } catch (problem) {
    error.textContent = problem.message;
  }
}

async function handleAuth(event) {
  event.preventDefault();
  const error = $("#auth-error");
  const button = $("#auth-submit");
  error.textContent = "";
  button.disabled = true;
  try {
    const registering = state.authMode === "register";
    const payload = {
      email: $("#auth-email").value.trim(),
      password: $("#auth-password").value,
    };
    if (registering) payload.name = $("#auth-name").value.trim();
    const result = await api(registering ? "/api/register" : "/api/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.user = result.user;
    state.csrf = result.csrf;
    state.categories = result.categories;
    const me = await api("/api/me");
    state.user = { ...state.user, ...me.user, whatsapp_ready: me.whatsapp_ready };
    state.categories = me.categories;
    showApp();
  } catch (problem) {
    error.textContent = problem.message;
  } finally {
    button.disabled = false;
  }
}

function openModal(id) {
  const modal = $(`#${id}`);
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  const focusTarget = $("input:not([type=hidden]), select, button", modal);
  focusTarget?.focus();
}

function closeModals() {
  $$(".modal-backdrop").forEach((modal) => { modal.hidden = true; });
  document.body.style.overflow = "";
}

async function saveTransaction(event) {
  event.preventDefault();
  const error = $("#transaction-error");
  error.textContent = "";
  const payload = {
    kind: $("#transaction-kind").value,
    amount: $("#transaction-amount").value,
    category: $("#transaction-category").value,
    payee: $("#transaction-payee").value.trim(),
    note: $("#transaction-note").value.trim(),
    date: $("#transaction-date").value,
  };
  try {
    await api("/api/transactions", { method: "POST", body: JSON.stringify(payload) });
    closeModals();
    $("#transaction-form").reset();
    $("#transaction-date").value = todayIso();
    toast(payload.kind === "credit" ? "Credit added to your ledger." : "Debit added to your ledger.");
    await renderView();
  } catch (problem) {
    error.textContent = problem.message;
  }
}

async function saveBudget(event) {
  event.preventDefault();
  const error = $("#budget-error");
  error.textContent = "";
  try {
    await api("/api/budgets", {
      method: "POST",
      body: JSON.stringify({
        category: $("#budget-category").value,
        limit: $("#budget-limit").value,
      }),
    });
    closeModals();
    $("#budget-form").reset();
    toast("Monthly budget saved.");
    await renderView();
  } catch (problem) {
    error.textContent = problem.message;
  }
}

async function logout() {
  try {
    await api("/api/logout", { method: "POST", body: "{}" });
  } catch { /* An expired session is already signed out. */ }
  state.user = null;
  state.csrf = "";
  state.dashboard = null;
  state.transactions = [];
  showAuth();
  $("#auth-form").reset();
  setAuthMode("login");
}

function bindEvents() {
  $("#auth-form").addEventListener("submit", handleAuth);
  $("#auth-switch").addEventListener("click", () => setAuthMode(state.authMode === "login" ? "register" : "login"));
  $("#transaction-form").addEventListener("submit", saveTransaction);
  $("#budget-form").addEventListener("submit", saveBudget);
  $("#logout-button").addEventListener("click", logout);
  $("#transaction-date").value = todayIso();

  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-open-transaction]");
    if (opener) {
      $("#transaction-form").reset();
      $("#transaction-date").value = todayIso();
      $("#transaction-kind").value = "debit";
      updateTransactionCategories("debit");
      $$(".kind-choice").forEach((item) => item.classList.toggle("selected", item.dataset.kind === "debit"));
      $("#transaction-error").textContent = "";
      openModal("transaction-modal");
      return;
    }
    if (event.target.closest("[data-open-budget]")) {
      $("#budget-form").reset();
      $("#budget-error").textContent = "";
      openModal("budget-modal");
      return;
    }
    if (event.target.closest("[data-close-modal]")) {
      closeModals();
      return;
    }
    const period = event.target.closest("[data-period]");
    if (period) {
      state.period = period.dataset.period;
      renderView();
      return;
    }
    const view = event.target.closest("[data-view]");
    if (view) {
      state.view = view.dataset.view;
      renderView();
    }
  });

  $$(".kind-choice").forEach((button) => button.addEventListener("click", () => {
    $("#transaction-kind").value = button.dataset.kind;
    updateTransactionCategories(button.dataset.kind);
    $$(".kind-choice").forEach((choice) => choice.classList.toggle("selected", choice === button));
  }));
  document.addEventListener("change", (event) => {
    if (event.target.id === "transaction-filter") renderTransactions();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModals();
  });
  $$(".modal-backdrop").forEach((modal) => modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModals();
  }));
}

async function boot() {
  bindEvents();
  setAuthMode("login");
  try {
    const me = await api("/api/me");
    state.user = { ...me.user, whatsapp_ready: me.whatsapp_ready };
    state.csrf = me.csrf;
    state.categories = me.categories;
    showApp();
  } catch {
    showAuth();
  }
}

boot();
