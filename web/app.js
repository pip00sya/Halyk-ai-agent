"use strict";

const appState = {
  status: null,
  run: null,
  rows: [],
  lastRunStatus: null,
  pollTimer: null,
  lastFocused: null,
};

const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Ошибка HTTP ${response.status}`);
  return payload;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message, type = "success") {
  const region = $("#toast-region");
  const toast = document.createElement("div");
  toast.className = `toast ${type === "error" ? "error" : ""}`;
  toast.textContent = message;
  region.append(toast);
  window.setTimeout(() => toast.remove(), 4200);
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 4 }).format(numeric);
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" }).format(date);
}

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function animateMetric(selector, target, formatter, duration = 760) {
  const element = $(selector);
  if (!element) return;
  if (target === null || target === undefined || !Number.isFinite(Number(target))) {
    element.textContent = "—";
    return;
  }

  const numericTarget = Number(target);
  const render = formatter || ((value) => String(Math.round(value)));
  if (prefersReducedMotion() || element.dataset.motionValue === String(numericTarget)) {
    element.textContent = render(numericTarget);
    return;
  }

  element.dataset.motionValue = String(numericTarget);
  const startedAt = performance.now();
  const tick = (now) => {
    const progress = Math.min(1, (now - startedAt) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    element.textContent = render(numericTarget * eased);
    if (progress < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function setupMotion() {
  document.documentElement.classList.add("app-ready", "motion-ready");
  const targets = [
    $("#overview .page-heading"),
    $("#overview .overview-grid"),
    $("#overview .readiness-card"),
    $("#workspace"),
    $("#results"),
    $("#documents"),
  ].filter(Boolean);

  targets.forEach((target) => target.classList.add("reveal-on-scroll"));
  if (prefersReducedMotion() || !("IntersectionObserver" in window)) {
    targets.forEach((target) => target.classList.add("is-visible"));
    return;
  }

  const revealObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add("is-visible");
      revealObserver.unobserve(entry.target);
    });
  }, { rootMargin: "0px 0px -8%", threshold: 0.08 });
  targets.forEach((target) => revealObserver.observe(target));
}

function elapsedSince(value) {
  if (!value) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds} сек`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} мин ${seconds % 60} сек`;
}

function selectedMode() {
  return $("input[name='mode']:checked")?.value || "auto";
}

function updateModeHelp() {
  const mode = selectedMode();
  const help = $("#mode-help");
  const copy = {
    auto: "Auto использует fact pack только при точном SHA‑256 совпадении public‑датасета; иначе включает LLM.",
    public: "Public разрешён только для отпечатанного открытого набора и не требует LLM‑ключа.",
    llm: "Private · LLM читает новые документы собственным planner и vision‑OCR. OPENAI_API_KEY берётся только из окружения сервера.",
  };
  help.textContent = copy[mode];
  if (mode === "llm" && appState.status && !appState.status.openai_api_key_configured) {
    help.textContent += " Сейчас OPENAI_API_KEY не найден.";
    help.classList.add("warning-text");
  } else {
    help.classList.remove("warning-text");
  }
}

function renderQuality(quality, preflight) {
  const explicitScore = Number.isFinite(Number(quality?.readiness_score)) ? Number(quality.readiness_score) : null;
  const preflightChecks = preflight?.checks || [];
  const derivedScore = preflightChecks.length
    ? Math.max(0, 100 - preflightChecks.filter((item) => item.status === "fail").length * 25 - preflightChecks.filter((item) => item.status === "warning").length * 5)
    : null;
  const score = explicitScore ?? derivedScore;
  const status = preflight?.status || quality?.status || "pending";
  const labels = { pass: "Готово", warning: "Нужно внимание", fail: "Есть блокеры", pending: "Нет отчёта" };
  const scoreNode = $("#readiness-score");
  scoreNode.textContent = score === null ? "—" : String(Math.round(score));
  $("#readiness-state").textContent = labels[status] || status;
  $("#readiness-state").className = `readiness-state ${status}`;
  $("#readiness-progress").style.transform = `scaleX(${score === null ? 0 : Math.max(0, Math.min(1, score / 100))})`;

  const checks = quality?.checks || preflight?.checks || [];
  const ordered = [...checks].sort((a, b) => {
    const rank = { fail: 0, warning: 1, pass: 2 };
    return (rank[a.status] ?? 3) - (rank[b.status] ?? 3) || Number(Boolean(b.hard_gate)) - Number(Boolean(a.hard_gate));
  }).slice(0, 6);
  const gateList = $("#readiness-gates");
  gateList.replaceChildren();
  if (!ordered.length) {
    gateList.innerHTML = `<div class="gate-row pending"><span></span><div><strong>Ожидается запуск</strong><small>После анализа здесь появятся hard gates.</small></div><b>WAIT</b></div>`;
  } else {
    ordered.forEach((check) => {
      const row = document.createElement("div");
      row.className = `gate-row ${check.status}`;
      const code = check.status === "pass" ? "PASS" : check.status === "warning" ? "REVIEW" : "BLOCK";
      row.innerHTML = `<span aria-hidden="true"></span><div><strong>${escapeHtml(check.title)}</strong><small>${escapeHtml(check.detail)}</small></div><b>${code}</b>`;
      gateList.append(row);
    });
  }

  const hash = quality?.submission_sha256;
  $("#readiness-hash").textContent = hash ? `${hash.slice(0, 12)}…${hash.slice(-8)}` : "—";
  $("#readiness-hash").title = hash || "";
  const boundary = quality?.checks?.find((item) => item.id === "boundary_sensitivity");
  const riskyCells = boundary?.metrics?.cells?.length;
  $("#readiness-risk").textContent = Number.isFinite(riskyCells) ? `${riskyCells} из ${quality?.cells || "—"}` : "—";
  $("#readiness-route").textContent = preflight?.resolved_route || quality?.checks?.find((item) => item.id === "private_route_proof")?.metrics?.planning_mode || "—";
}

function renderDatasetAudit(preflight) {
  const checks = preflight?.checks || [];
  const byId = Object.fromEntries(checks.map((item) => [item.id, item]));
  const template = byId.template_contract?.metrics || {};
  const ledger = byId.ledger_integrity?.metrics || {};
  const documents = byId.document_archive?.metrics || {};
  const profile = byId.dataset_profile?.metrics || {};
  const authority = byId.authority_resolution?.metrics || {};

  $("#audit-scenarios").textContent = template.scenarios ?? "—";
  $("#audit-cells").textContent = template.cells ?? "—";
  $("#audit-txns").textContent = ledger.transactions == null ? "—" : new Intl.NumberFormat("ru-RU").format(ledger.transactions);
  $("#audit-docs").textContent = documents.documents ?? "—";
  $("#audit-pages").textContent = documents.pages == null ? "—" : new Intl.NumberFormat("ru-RU").format(documents.pages);
  $("#audit-ocr").textContent = documents.low_text_pages ?? "—";

  const route = preflight?.resolved_route;
  const badge = $("#dataset-route-badge");
  badge.textContent = route ? `${route.toUpperCase()} ROUTE` : "NO PREFLIGHT";
  badge.classList.toggle("private", route === "llm");

  const riskLine = $("#dataset-risk-line");
  if (!checks.length) {
    riskLine.innerHTML = "<span>Запустите Preflight — агент покажет валюты, пропуски, конфликты версий и готовность OCR.</span>";
    return;
  }
  const currencies = Object.entries(profile.currencies || {}).map(([code, count]) => `${code}: ${count}`).join(" · ");
  const conflicts = authority.conflicts?.length || 0;
  const missing = profile.missing_amounts || 0;
  const nonstandard = profile.nonstandard_accounts?.join(", ");
  const items = [
    currencies && `Валюты ${currencies}`,
    `${conflicts} конфликтов версий`,
    `${missing} операций без суммы`,
    nonstandard && `нестандартный счёт ${nonstandard}`,
  ].filter(Boolean);
  riskLine.replaceChildren(...items.map((text) => {
    const item = document.createElement("span");
    item.textContent = text;
    return item;
  }));
}

function renderStatus(status) {
  appState.status = status;
  appState.privateDataset = status.private_dataset || null;
  const report = status.report || {};
  const score = status.public_score;

  $("#data-dir").value ||= status.default_dataset || "";
  $("#team").value ||= status.metadata?.team && !String(status.metadata.team).startsWith("CHANGE_ME") ? status.metadata.team : "";
  $("#contact-email").value ||= status.metadata?.contact_email && !String(status.metadata.contact_email).startsWith("CHANGE_ME") ? status.metadata.contact_email : "";
  animateMetric("#metric-docs", report.documents, (value) => String(Math.round(value)));
  animateMetric("#metric-cells", report.covenant_cells, (value) => String(Math.round(value)));
  animateMetric("#metric-txns", report.transactions, (value) => new Intl.NumberFormat("ru-RU").format(Math.round(value)));
  animateMetric("#metric-score", score ? score.score * 100 : null, (value) => `${Math.round(value)}%`);
  animateMetric("#hero-score", score?.score, (value) => value.toFixed(2), 900);
  $("#hero-mode").textContent = report.planning_mode ? report.planning_mode.replaceAll("-", " ") : "Агент готов к работе";
  const lastRun = $("#last-run");
  lastRun.textContent = report.status === "ok" ? "RUN OK" : report.status === "attention_required" ? "ACTION REQUIRED" : "NO RUN";
  lastRun.classList.toggle("attention", report.status === "attention_required");
  renderQuality(status.quality, status.preflight);
  renderDatasetAudit(status.preflight);
  updateModeHelp();
  renderDocuments(status.documents || []);
  const privatePreset = $("#private-dataset-preset");
  privatePreset.disabled = !appState.privateDataset;
  privatePreset.title = appState.privateDataset ? appState.privateDataset : "Папка agentic-bank-hidden не найдена";
  $$('[data-dataset-preset]').forEach((button) => {
    const expected = button.dataset.datasetPreset === "private" ? appState.privateDataset : status.default_dataset;
    button.classList.toggle("active", Boolean(expected) && $("#data-dir").value === expected);
  });
}

function renderDocuments(documents) {
  const grid = $("#document-grid");
  grid.replaceChildren();
  documents.forEach((item, index) => {
    const card = window.document.createElement("article");
    card.className = "document-card";
    card.innerHTML = `
      <span class="document-number">0${index + 1}</span>
      <h3>${escapeHtml(item.title)}</h3>
      <p>${escapeHtml(item.description)}</p>
      <button type="button" data-document="${escapeHtml(item.id)}">
        Открыть
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m13 5 7 7-7 7-1.4-1.4 4.6-4.6H4v-2h12.2l-4.6-4.6L13 5Z"/></svg>
      </button>`;
    grid.append(card);
  });
}

function renderRun(run) {
  const previous = appState.lastRunStatus;
  appState.run = run;
  appState.lastRunStatus = run.status;
  const pill = $("#run-status-pill");
  const panel = $(".run-panel");
  const labels = { idle: "Готов", running: "В работе", success: "Готово", error: "Ошибка" };
  pill.textContent = labels[run.status] || run.status;
  pill.className = `status-pill ${run.status}`;
  panel.classList.toggle("running", run.status === "running");
  $("#run-message").textContent = run.message || "Агент готов к запуску";
  $("#run-timing").textContent = run.status === "running"
    ? `Выполняется ${elapsedSince(run.started_at)}`
    : run.finished_at ? `Завершено ${formatDate(run.finished_at)}` : "Выберите режим и укажите данные.";
  $("#run-logs").textContent = run.logs?.length ? run.logs.join("\n") : "Журнал пока пуст.";

  const steps = $$("#run-steps li");
  steps.forEach((step) => step.classList.remove("active", "complete"));
  steps[0].classList.add("complete");
  if (run.status === "running") {
    steps[1].classList.add("active");
    steps[1].querySelector("small").textContent = "Агент выполняет pipeline";
  } else if (run.status === "success") {
    steps.forEach((step) => step.classList.add("complete"));
    steps[1].querySelector("small").textContent = "Документы обработаны";
    steps[2].querySelector("small").textContent = "Решения проверены";
    steps[3].querySelector("small").textContent = "submission.json создан";
  } else if (run.status === "error") {
    steps[1].classList.add("active");
    steps[1].querySelector("small").textContent = "Смотрите технический журнал";
  }

  const runButton = $("#run-button");
  runButton.disabled = run.status === "running";
  runButton.querySelector("span").textContent = run.status === "running" ? "Агент работает…" : "Запустить агента";

  if (previous === "running" && run.status === "success") {
    showToast("Анализ завершён. submission.json готов к проверке.");
    refreshStatusAndResults();
  }
  if (previous === "running" && run.status === "error") {
    showToast(run.message || "Анализ завершился с ошибкой", "error");
  }
}

function renderResults(payload) {
  appState.rows = payload.rows || [];
  const summary = payload.summary || {};
  $("#summary-total").textContent = summary.total ?? 0;
  $("#summary-compliant").textContent = summary.compliant ?? 0;
  $("#summary-breach").textContent = summary.breach ?? 0;
  $("#summary-evidence").textContent = summary.evidence ?? 0;
  filterResults();
}

function filterResults() {
  const query = $("#result-search").value.trim().toLocaleLowerCase("ru");
  const status = $("#status-filter").value;
  const rows = appState.rows.filter((row) => {
    const haystack = `${row.scenario_id} ${row.clause} ${row.evidence_txn_id || ""}`.toLocaleLowerCase("ru");
    return (!query || haystack.includes(query)) && (status === "all" || row.status === status);
  });
  const body = $("#results-body");
  body.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = '<td colspan="6" class="empty-cell">Подходящих решений не найдено.</td>';
    body.append(tr);
  } else {
    rows.forEach((row) => {
      const index = appState.rows.indexOf(row);
      const tr = document.createElement("tr");
      tr.dataset.detail = String(index);
      const verdictClass = row.status === "COMPLIANT" ? "compliant" : "breach";
      const verdictLabel = row.status === "COMPLIANT" ? "СОБЛЮДЁН" : "НАРУШЕН";
      tr.innerHTML = `
        <td class="scenario-cell">${escapeHtml(row.scenario_id)}</td>
        <td class="clause-cell">§ ${escapeHtml(row.clause)}</td>
        <td><span class="verdict ${verdictClass}">${verdictLabel}</span></td>
        <td class="actual-cell">${escapeHtml(formatNumber(row.actual))}</td>
        <td class="evidence-cell">${escapeHtml(row.evidence_txn_id || "—")}</td>
        <td><button class="row-detail" type="button" data-row="${index}" aria-label="Подробнее: ${escapeHtml(row.scenario_id)}, пункт ${escapeHtml(row.clause)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7-1.4-1.4 5.6-5.6-5.6-5.6L9 5Z"/></svg></button></td>`;
      body.append(tr);
    });
  }
  $("#result-caption").textContent = `Показано ${rows.length} из ${appState.rows.length}. Каждая строка открывает расчёт, источники и происхождение фактов.`;
}

function openRowDetail(index) {
  const row = appState.rows[index];
  if (!row) return;
  appState.lastFocused = document.activeElement;
  $("#drawer-kicker").textContent = `${row.scenario_id} / COVENANT ${row.clause}`;
  $("#drawer-title").textContent = "Детали решения";
  const verdictClass = row.status === "COMPLIANT" ? "compliant" : "breach";
  const verdictLabel = row.status === "COMPLIANT" ? "СОБЛЮДЁН" : "НАРУШЕН";
  const sources = row.source_documents?.length
    ? row.source_documents.map((source) => `<li>${escapeHtml(source)}</li>`).join("")
    : "<li>Источники не записаны в trace.</li>";
  const facts = Object.entries(row.facts || {}).length
    ? Object.entries(row.facts).map(([name, fact]) => `<li><span>${escapeHtml(name)}</span><strong>${escapeHtml(formatNumber(fact?.value))}</strong></li>`).join("")
    : "<li><span>Факты</span><strong>—</strong></li>";
  const counterfactuals = row.evidence_counterfactuals?.length
    ? row.evidence_counterfactuals.map((item) => `<li><span>${escapeHtml(item.txn_id)}</span><strong>${item.flips_verdict ? "вердикт меняется" : "не меняется"} · ${escapeHtml(item.status_without_transaction)}</strong></li>`).join("")
    : "<li><span>Контрфакт</span><strong>не требуется</strong></li>";
  const overrideNote = row.status_override_applied
    ? `<section class="detail-block"><h3>Применённое исключение</h3><p>${escapeHtml(row.status_override?.reason || "Статус изменён документированным условием.")}</p></section>`
    : "";
  $("#drawer-content").innerHTML = `
    <div class="detail-hero">
      <div class="detail-hero-head"><span>Итоговый вердикт</span><span class="verdict ${verdictClass}">${verdictLabel}</span></div>
      <span class="actual">${escapeHtml(formatNumber(row.actual))}</span>
      <small>рассчитанное значение ковенанта</small>
    </div>
    <div class="detail-grid">
      <div class="detail-tile"><span>Условие</span><strong>${escapeHtml(row.operator || "—")} ${escapeHtml(formatNumber(row.threshold))}</strong></div>
      <div class="detail-tile"><span>Raw actual</span><strong>${escapeHtml(formatNumber(row.raw_actual))}</strong></div>
      <div class="detail-tile"><span>Транзакция‑улика</span><strong>${escapeHtml(row.evidence_txn_id || "не требуется")}</strong></div>
      <div class="detail-tile"><span>Запас до порога</span><strong>${escapeHtml(formatNumber(row.signed_margin))}</strong></div>
    </div>
    <section class="detail-block"><h3>Обоснование</h3><p>${escapeHtml(row.rationale || "Обоснование отсутствует в техническом следе.")}</p></section>
    ${overrideNote}
    <section class="detail-block"><h3>Контрфактическая проверка</h3><ul class="fact-list">${counterfactuals}</ul></section>
    <section class="detail-block"><h3>Факты расчёта</h3><ul class="fact-list">${facts}</ul></section>
    <section class="detail-block"><h3>Документы‑источники</h3><ul class="source-list">${sources}</ul></section>`;
  $("#drawer-backdrop").hidden = false;
  document.body.classList.add("modal-open");
  const drawer = $("#detail-drawer");
  drawer.setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => drawer.classList.add("open"));
  $("#drawer-close").focus();
}

function closeDrawer() {
  const drawer = $("#detail-drawer");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  window.setTimeout(() => { $("#drawer-backdrop").hidden = true; }, 220);
  document.body.classList.remove("modal-open");
  appState.lastFocused?.focus?.();
}

async function openDocument(id) {
  appState.lastFocused = document.activeElement;
  try {
    const documentPayload = await api(`/api/document?id=${encodeURIComponent(id)}`);
    $("#document-title").textContent = documentPayload.title;
    $("#document-content").textContent = documentPayload.content;
    $("#document-modal").hidden = false;
    document.body.classList.add("modal-open");
    $("#document-close").focus();
  } catch (error) {
    showToast(error.message, "error");
  }
}

function closeDocument() {
  $("#document-modal").hidden = true;
  document.body.classList.remove("modal-open");
  appState.lastFocused?.focus?.();
}

async function refreshStatusAndResults() {
  try {
    const [status, results] = await Promise.all([api("/api/status"), api("/api/results")]);
    renderStatus(status);
    renderResults(results);
  } catch (error) {
    showToast(`Не удалось обновить данные: ${error.message}`, "error");
  }
}

async function pollRun() {
  window.clearTimeout(appState.pollTimer);
  try {
    const run = await api("/api/run");
    renderRun(run);
    appState.pollTimer = window.setTimeout(pollRun, run.status === "running" ? 1000 : 5000);
  } catch (error) {
    appState.pollTimer = window.setTimeout(pollRun, 7000);
  }
}

async function startRun(event) {
  event.preventDefault();
  const payload = {
    data_dir: $("#data-dir").value.trim(),
    team: $("#team").value.trim(),
    contact_email: $("#contact-email").value.trim(),
    mode: selectedMode(),
    model: $("#model").value.trim(),
    reasoning_effort: $("#reasoning").value,
    review: $("#review").checked,
  };
  if (payload.mode === "llm" && appState.status && !appState.status.openai_api_key_configured) {
    showToast("OPENAI_API_KEY не найден в окружении сервера.", "error");
    return;
  }
  localStorage.setItem("halyk-dashboard-form", JSON.stringify({
    data_dir: payload.data_dir,
    team: payload.team,
    contact_email: payload.contact_email,
    model: payload.model,
    reasoning_effort: payload.reasoning_effort,
    review: payload.review,
  }));
  const runButton = $("#run-button");
  runButton.disabled = true;
  runButton.querySelector("span").textContent = "Запуск…";
  try {
    renderRun(await api("/api/run", { method: "POST", body: JSON.stringify(payload) }));
    showToast("Агент запущен. Статус обновляется автоматически.");
    location.hash = "workspace";
  } catch (error) {
    runButton.disabled = false;
    runButton.querySelector("span").textContent = "Запустить агента";
    showToast(error.message, "error");
  }
}

async function validateSubmission() {
  const dataDir = $("#data-dir").value.trim() || appState.status?.default_dataset;
  try {
    const result = await api("/api/validate", { method: "POST", body: JSON.stringify({ data_dir: dataDir }) });
    showToast(result.message || "Submission валиден");
  } catch (error) {
    showToast(`Валидация не пройдена: ${error.message}`, "error");
  }
}

async function runPreflight() {
  const button = $("#preflight-button");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Проверяю…";
  try {
    const report = await api("/api/preflight", {
      method: "POST",
      body: JSON.stringify({
        data_dir: $("#data-dir").value.trim(),
        team: $("#team").value.trim(),
        contact_email: $("#contact-email").value.trim(),
        mode: selectedMode(),
      }),
    });
    renderQuality(null, report);
    renderDatasetAudit(report);
    const templateMetrics = report.checks?.find((item) => item.id === "template_contract")?.metrics || {};
    const ledgerMetrics = report.checks?.find((item) => item.id === "ledger_integrity")?.metrics || {};
    const documentMetrics = report.checks?.find((item) => item.id === "document_archive")?.metrics || {};
    animateMetric("#metric-docs", documentMetrics.documents, (value) => String(Math.round(value)));
    animateMetric("#metric-cells", templateMetrics.cells, (value) => String(Math.round(value)));
    animateMetric("#metric-txns", ledgerMetrics.transactions, (value) => new Intl.NumberFormat("ru-RU").format(Math.round(value)));
    if (appState.status) appState.status.preflight = report;
    const failed = report.checks?.filter((item) => item.status === "fail").length || 0;
    showToast(failed ? `Preflight: найдено блокеров — ${failed}` : "Preflight пройден: hard gates готовы", failed ? "error" : "success");
  } catch (error) {
    showToast(`Preflight не выполнен: ${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function restoreForm() {
  try {
    const stored = JSON.parse(localStorage.getItem("halyk-dashboard-form") || "null");
    if (!stored) return;
    if (stored.data_dir) $("#data-dir").value = stored.data_dir;
    if (stored.team) $("#team").value = stored.team;
    if (stored.contact_email) $("#contact-email").value = stored.contact_email;
    if (stored.model) $("#model").value = stored.model;
    if (stored.reasoning_effort) $("#reasoning").value = stored.reasoning_effort;
    if (typeof stored.review === "boolean") $("#review").checked = stored.review;
  } catch {
    localStorage.removeItem("halyk-dashboard-form");
  }
}

function bindEvents() {
  $("#run-form").addEventListener("submit", startRun);
  $("#preflight-button").addEventListener("click", runPreflight);
  $$('[data-dataset-preset]').forEach((button) => button.addEventListener("click", () => {
    const preset = button.dataset.datasetPreset;
    const path = preset === "private" ? appState.privateDataset : appState.status?.default_dataset;
    if (!path) {
      showToast("Папка приватного датасета не найдена рядом с проектом.", "error");
      return;
    }
    $("#data-dir").value = path;
    const nextMode = preset === "private" ? "llm" : "public";
    const modeInput = $(`input[name="mode"][value="${nextMode}"]`);
    if (modeInput) {
      modeInput.checked = true;
      updateModeHelp();
    }
    $$('[data-dataset-preset]').forEach((item) => item.classList.toggle("active", item === button));
    showToast(preset === "private" ? "Выбран private dataset. Запустите Preflight." : "Выбран public benchmark.");
  }));
  $("#validate-button").addEventListener("click", validateSubmission);
  $("#validate-top").addEventListener("click", validateSubmission);
  $("#validate-inline").addEventListener("click", validateSubmission);
  $("#refresh-results").addEventListener("click", refreshStatusAndResults);
  $("#result-search").addEventListener("input", filterResults);
  $("#status-filter").addEventListener("change", filterResults);
  $$("input[name='mode']").forEach((input) => input.addEventListener("change", updateModeHelp));
  $("#results-body").addEventListener("click", (event) => {
    const button = event.target.closest("[data-row]");
    if (button) openRowDetail(Number(button.dataset.row));
  });
  $("#document-grid").addEventListener("click", (event) => {
    const button = event.target.closest("[data-document]");
    if (button) openDocument(button.dataset.document);
  });
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  $("#document-close").addEventListener("click", closeDocument);
  $("#document-modal").addEventListener("click", (event) => {
    if (event.target === $("#document-modal")) closeDocument();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("#document-modal").hidden) closeDocument();
    else if ($("#detail-drawer").classList.contains("open")) closeDrawer();
  });

  const menu = $("#mobile-menu");
  const mobileNav = $("#mobile-nav");
  menu.addEventListener("click", () => {
    const open = mobileNav.hidden;
    mobileNav.hidden = !open;
    menu.setAttribute("aria-expanded", String(open));
    menu.classList.toggle("open", open);
  });
  $$(".nav-link").forEach((link) => link.addEventListener("click", () => {
    mobileNav.hidden = true;
    menu.setAttribute("aria-expanded", "false");
    menu.classList.remove("open");
  }));

  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    $$(".nav-link").forEach((link) => link.classList.toggle("active", link.dataset.section === visible.target.id));
  }, { rootMargin: "-20% 0px -65%", threshold: [0.05, 0.25] });
  ["overview", "architecture", "workspace", "results", "documents"].forEach((id) => {
    const section = document.getElementById(id);
    if (section) observer.observe(section);
  });
}

async function init() {
  restoreForm();
  bindEvents();
  await refreshStatusAndResults();
  setupMotion();
  await pollRun();
}

init();
