/* Support Desk: agent dashboard.
 *
 * Vanilla JS single-page app talking to the FastAPI backend with the HttpOnly
 * session cookie (same origin, so no tokens are handled in JS).
 *
 * Safety rule: every piece of ticket/customer data goes into the DOM as text
 * (textContent / text nodes via `h()`), never as HTML.
 */
"use strict";

// ====================================================================== constants

const POLL_MS = 30_000;
const DELIVERY_POLL_MS = 3_000;
const DELIVERY_WATCH_MAX_MS = 120_000;
const PAGE_SIZE = 50;

const FILTERS = {
  all: [],
  new: ["new"],
  open: ["open", "in_progress"],
  pending: ["pending"],
  resolved: ["resolved", "closed"],
};
const STATUS_LABELS = {
  new: "New", open: "Open", in_progress: "In progress",
  pending: "Pending", resolved: "Resolved", closed: "Closed",
};
const PRIORITY_LABELS = { low: "Low", medium: "Medium", high: "High", urgent: "Urgent" };
const PRIORITY_ICONS = { urgent: "bi-fire", high: "bi-exclamation-circle" };

// ====================================================================== state

/** localStorage that never throws (private mode, blocked storage...). */
const store = {
  get(key, fallback) {
    try { return localStorage.getItem("tts." + key) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("tts." + key, value); } catch { /* ignore */ }
  },
};

const state = {
  me: null,
  users: [],
  filter: store.get("filter", "all"),
  assigned: store.get("assigned", ""),
  sort: store.get("sort", "-last_activity_at"),
  q: "",
  tickets: [],
  total: 0,
  knownIds: new Set(),
  selectedId: null,
  detail: null,
  drafts: new Map(),          // ticketId -> { body, mode, status }
  autoRefresh: store.get("autoRefresh", "1") === "1",
  listSeq: 0,                 // guards against out-of-order list responses
  detailSeq: 0,               // ... and detail responses
  lastRefresh: 0,
  pollTimer: null,
  deliveryTimer: null,
  loginMode: "login",         // or "setup" (first admin registration)
  adminMode: false,
  adminUsers: [],
  editingUser: null,          // user being edited in the user modal (null = adding)
  resetUser: null,
  // Rendered into the page by the server, so it is correct before any API call.
  siteName: document.querySelector('meta[name="site-name"]')?.content || "Support Desk",
};

// ====================================================================== utilities

const $ = (id) => document.getElementById(id);

/** Build an element. Children that aren't Nodes become text nodes (never HTML). */
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

const icon = (name, cls = "") => h("i", { class: `bi ${name} ${cls}`.trim(), "aria-hidden": "true" });

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
function relTime(iso) {
  if (!iso) return "";
  const diff = (new Date(iso) - Date.now()) / 1000;
  const abs = Math.abs(diff);
  if (abs < 45) return diff > 0 ? "in a moment" : "just now";
  const units = [["minute", 60], ["hour", 3600], ["day", 86400], ["week", 604800]];
  let [unit, secs] = units[0];
  for (const u of units) if (abs >= u[1]) [unit, secs] = u;
  if (abs >= 30 * 86400) return new Date(iso).toLocaleDateString();
  return rtf.format(Math.round(diff / secs), unit);
}
const fullTime = (iso) => (iso ? new Date(iso).toLocaleString() : "");
const timeEl = (iso, cls = "when") => h("time", { class: cls, datetime: iso, title: fullTime(iso) }, relTime(iso));

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function statusBadge(status) {
  return h("span", { class: `badge st st-${status}` }, STATUS_LABELS[status] ?? status);
}
function priorityBadge(priority) {
  return h("span", { class: `badge pr pr-${priority}` },
    PRIORITY_ICONS[priority] ? icon(PRIORITY_ICONS[priority], "me-1") : null,
    PRIORITY_LABELS[priority] ?? priority);
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

// ====================================================================== API

class ApiError extends Error {
  constructor(status, body) {
    super(body?.error || (status === 0 ? "Network error: is the server reachable?" : `HTTP ${status}`));
    this.status = status;
    this.body = body;
  }
  /** Human-readable message including per-field validation details. */
  get detail() {
    const d = this.body?.details;
    if (Array.isArray(d) && d.length) {
      return d.map((x) => (x.field ? `${x.field}: ${x.message}` : x.message)).join("; ");
    }
    if (this.status === 429 && d?.retry_after) {
      return `${this.message} (about ${Math.ceil(d.retry_after / 60)} min)`;
    }
    return this.message;
  }
}

async function api(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, null);
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) {
    const err = new ApiError(res.status, data);
    // Session expired or revoked (e.g. password changed elsewhere): back to login.
    if (res.status === 401 && !path.startsWith("/auth/")) {
      toast("Your session has ended. Please sign in again.", "warning");
      showLogin();
    }
    throw err;
  }
  return data;
}

// ====================================================================== toasts

function toast(message, variant = "success") {
  const iconName = { success: "bi-check-circle", danger: "bi-x-octagon", warning: "bi-exclamation-triangle", info: "bi-info-circle" }[variant];
  const el = h("div", {
    class: `toast align-items-center text-bg-${variant} border-0`,
    role: variant === "danger" ? "alert" : "status",
    "aria-live": variant === "danger" ? "assertive" : "polite",
    "aria-atomic": "true",
  },
    h("div", { class: "d-flex" },
      h("div", { class: "toast-body" }, icon(iconName, "me-2"), message),
      h("button", { type: "button", class: "btn-close btn-close-white me-2 m-auto", "data-bs-dismiss": "toast", "aria-label": "Close" })));
  $("toasts").append(el);
  const t = bootstrap.Toast.getOrCreateInstance(el, { delay: variant === "danger" ? 8000 : 4000 });
  el.addEventListener("hidden.bs.toast", () => el.remove());
  t.show();
}

// ====================================================================== views

function showView(id) {
  for (const v of ["boot-view", "login-view", "app-view"]) $(v).classList.toggle("d-none", v !== id);
}

function showLogin() {
  hideAdminView();
  stopPolling();
  stopDeliveryWatch();
  state.me = null;
  state.selectedId = null;
  state.detail = null;
  state.drafts.clear();
  showView("login-view");
  $("login-password").value = "";
  $("login-email").focus();
}

const isAdmin = () => state.me?.role === "admin";

function applyIdentity() {
  $("me-name").textContent = state.me.full_name;
  $("me-detail").textContent = `${state.me.email} · ${state.me.role}`;
  $("admin-link").classList.toggle("d-none", !isAdmin() || state.adminMode);
  for (const el of document.querySelectorAll(".admin-only")) el.classList.toggle("d-none", !isAdmin());
}

async function showApp() {
  showView("app-view");
  applyIdentity();
  $("auto-refresh").checked = state.autoRefresh;
  $("assigned-filter").value = state.assigned;
  $("sort").value = state.sort;
  renderTabs();
  await loadUsers();
  await refreshList();
  startPolling();
  routeFromHash();
}

// ====================================================================== login

function setLoginMode(mode) {
  state.loginMode = mode;
  const setup = mode === "setup";
  $("login-title").textContent = setup ? "Create the first admin account" : "Sign in to the agent dashboard";
  $("login-name-group").classList.toggle("d-none", !setup);
  $("login-password-hint").classList.toggle("d-none", !setup);
  $("login-password").autocomplete = setup ? "new-password" : "current-password";
  $("login-submit").textContent = setup ? "Create admin & sign in" : "Sign in";
  $("login-mode-toggle").textContent = setup ? "Back to sign in" : "First-time setup: create the admin account";
  $("login-error").classList.add("d-none");
}

async function onLoginSubmit(event) {
  event.preventDefault();
  const email = $("login-email").value.trim();
  const password = $("login-password").value;
  const errorBox = $("login-error");
  const button = $("login-submit");
  errorBox.classList.add("d-none");
  if (!email || !password) {
    errorBox.textContent = "Enter your email and password.";
    errorBox.classList.remove("d-none");
    return;
  }
  button.disabled = true;
  try {
    if (state.loginMode === "setup") {
      await api("/auth/register", {
        method: "POST",
        body: { email, password, full_name: $("login-name").value.trim() || email },
      });
    }
    const res = await api("/auth/login", { method: "POST", body: { email, password } });
    state.me = res.user;
    setLoginMode("login");
    await showApp();
  } catch (err) {
    let msg = err instanceof ApiError ? err.detail : String(err);
    if (state.loginMode === "setup" && err.status === 401) {
      msg = "Setup is already complete: ask an admin to create your account, then sign in.";
    }
    errorBox.textContent = msg;
    errorBox.classList.remove("d-none");
  } finally {
    button.disabled = false;
  }
}

async function logout() {
  try { await api("/auth/logout", { method: "POST" }); } catch { /* already gone */ }
  history.replaceState(null, "", location.pathname);
  showLogin();
}

// ====================================================================== users

async function loadUsers() {
  try {
    state.users = await api("/api/users");
  } catch (err) {
    if (err.status !== 401) toast(`Could not load agents: ${err.detail}`, "danger");
  }
}

// ====================================================================== ticket list

function listParams({ statuses = FILTERS[state.filter], limit, offset = 0 } = {}) {
  const p = new URLSearchParams();
  statuses.forEach((s) => p.append("status", s));
  if (state.assigned) p.set("assigned_to", state.assigned);
  if (state.q) p.set("q", state.q);
  p.set("sort", state.sort);
  p.set("limit", String(limit ?? PAGE_SIZE));
  p.set("offset", String(offset));
  return p;
}

/**
 * Reload the list (keeping as many rows as are currently shown) plus the tab counts.
 * `quiet`: background poll, so announce new arrivals instead of just redrawing.
 */
async function refreshList({ quiet = false } = {}) {
  const seq = ++state.listSeq;
  const btn = $("refresh-btn");
  btn.classList.add("refreshing");
  try {
    const limit = Math.min(200, Math.max(PAGE_SIZE, state.tickets.length));
    const [page, counts] = await Promise.all([
      api(`/api/tickets?${listParams({ limit })}`),
      loadTabCounts(),
    ]);
    if (seq !== state.listSeq) return; // a newer refresh superseded this one

    const arrivals = page.items.filter((t) => !state.knownIds.has(t.id));
    const firstLoad = state.knownIds.size === 0;
    page.items.forEach((t) => state.knownIds.add(t.id));
    state.tickets = page.items;
    state.total = page.total;
    renderList(quiet && !firstLoad ? new Set(arrivals.map((t) => t.id)) : new Set());
    renderTabCounts(counts);

    if (quiet && !firstLoad && arrivals.length) {
      toast(`${arrivals.length} new ticket${arrivals.length > 1 ? "s" : ""} in this view`, "info");
    }
    state.lastRefresh = Date.now();
    $("last-updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    await syncOpenTicket();
  } catch (err) {
    if (err.status !== 401 && seq === state.listSeq) {
      if (quiet) $("last-updated").textContent = "Refresh failed, will retry";
      else toast(`Could not load tickets: ${err.detail}`, "danger");
    }
  } finally {
    if (seq === state.listSeq) btn.classList.remove("refreshing");
  }
}

async function loadMore() {
  const btn = $("load-more");
  btn.disabled = true;
  try {
    const page = await api(`/api/tickets?${listParams({ offset: state.tickets.length })}`);
    const seen = new Set(state.tickets.map((t) => t.id));
    state.tickets.push(...page.items.filter((t) => !seen.has(t.id)));
    page.items.forEach((t) => state.knownIds.add(t.id));
    state.total = page.total;
    renderList();
  } catch (err) {
    toast(`Could not load more: ${err.detail}`, "danger");
  } finally {
    btn.disabled = false;
  }
}

/** One cheap `limit=1` request per tab, for the badge counts. */
async function loadTabCounts() {
  const entries = await Promise.all(
    Object.entries(FILTERS).map(async ([name, statuses]) => {
      const page = await api(`/api/tickets?${listParams({ statuses, limit: 1 })}`);
      return [name, page.total];
    }),
  );
  return Object.fromEntries(entries);
}

function renderTabs() {
  for (const btn of document.querySelectorAll("#status-tabs [data-filter]")) {
    const active = btn.dataset.filter === state.filter;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  }
}

function renderTabCounts(counts) {
  for (const btn of document.querySelectorAll("#status-tabs [data-filter]")) {
    const n = counts[btn.dataset.filter];
    btn.querySelector(".tab-count").textContent = n ? String(n) : "";
  }
}

function ticketCard(t, isArrival) {
  const assignee = t.assignee
    ? h("span", { class: "t-assignee", title: `Assigned to ${t.assignee.full_name}` }, icon("bi-person", "me-1"), t.assignee.full_name)
    : h("span", { class: "t-assignee t-unassigned" }, "Unassigned");
  return h("button", {
    type: "button",
    class: `ticket-card prio-${t.priority}${t.id === state.selectedId ? " active" : ""}${isArrival ? " is-new-arrival" : ""}`,
    "aria-current": t.id === state.selectedId ? "true" : null,
    dataset: { id: t.id },
    onclick: () => selectTicket(t.id),
  },
    h("div", { class: "card-top" },
      h("span", { class: "t-id" }, `#${t.id}`),
      h("span", { class: "d-flex gap-1" }, statusBadge(t.status), priorityBadge(t.priority)),
      timeEl(t.last_activity_at, "t-time")),
    h("div", { class: "t-subject", title: t.subject }, t.subject),
    h("div", { class: "t-meta" },
      h("span", { title: t.requester_email }, icon("bi-envelope", "me-1"), t.requester_email),
      assignee));
}

function renderList(arrivals = new Set()) {
  const list = $("ticket-list");
  if (!state.tickets.length) {
    const what = state.q ? "No tickets match your search." : `No ${state.filter === "all" ? "" : state.filter + " "}tickets here.`;
    list.replaceChildren(h("div", { class: "list-empty" }, icon("bi-inbox"), what));
  } else {
    list.replaceChildren(...state.tickets.map((t) => ticketCard(t, arrivals.has(t.id))));
  }
  const shown = state.tickets.length;
  $("list-count").textContent = state.total === 0 ? "No tickets"
    : `${state.total} ticket${state.total === 1 ? "" : "s"}${shown < state.total ? ` · showing ${shown}` : ""}`;
  $("load-more").classList.toggle("d-none", shown >= state.total);
}

function highlightSelectedCard() {
  for (const card of document.querySelectorAll(".ticket-card")) {
    const on = Number(card.dataset.id) === state.selectedId;
    card.classList.toggle("active", on);
    if (on) card.setAttribute("aria-current", "true"); else card.removeAttribute("aria-current");
  }
}

/** Update one card in place after the ticket changed (no full list reload needed). */
function patchCard(summary) {
  const i = state.tickets.findIndex((t) => t.id === summary.id);
  if (i === -1) return;
  state.tickets[i] = { ...state.tickets[i], ...summary };
  const old = document.querySelector(`.ticket-card[data-id="${summary.id}"]`);
  if (old) old.replaceWith(ticketCard(state.tickets[i], false));
}

// ====================================================================== routing

function routeFromHash() {
  if (location.hash === "#/admin") {
    if (isAdmin()) return showAdminView();
    history.replaceState(null, "", location.pathname);
    toast("The admin section is only available to admins.", "warning");
  }
  hideAdminView();
  const m = location.hash.match(/^#\/tickets\/(\d+)$/);
  if (m) {
    const id = Number(m[1]);
    if (id !== state.selectedId) selectTicket(id, { updateHash: false });
  } else if (state.selectedId !== null) {
    closeTicket({ updateHash: false });
  }
}

// ====================================================================== workspace

async function selectTicket(id, { updateHash = true } = {}) {
  saveDraft();
  if (updateHash && location.hash !== `#/tickets/${id}`) location.hash = `#/tickets/${id}`;
  if (id === state.selectedId && state.detail) return;
  state.selectedId = id;
  state.detail = null;
  stopDeliveryWatch();
  highlightSelectedCard();
  document.body.classList.add("ticket-open");
  $("workspace-empty").classList.add("d-none");
  $("workspace-body").classList.add("d-none");
  $("workspace-loading").classList.remove("d-none");
  await loadDetail(id, { scroll: "bottom", fresh: true });
}

function closeTicket({ updateHash = true } = {}) {
  saveDraft();
  stopDeliveryWatch();
  state.selectedId = null;
  state.detail = null;
  document.body.classList.remove("ticket-open");
  $("workspace-body").classList.add("d-none");
  $("workspace-loading").classList.add("d-none");
  $("workspace-empty").classList.remove("d-none");
  highlightSelectedCard();
  if (updateHash) history.pushState(null, "", location.pathname + location.search);
}

/**
 * Fetch and render the selected ticket.
 * scroll: "bottom" always scrolls down, "auto" only if the agent was already at the
 * bottom (otherwise shows the "New activity" pill), "keep" leaves the position alone.
 */
async function loadDetail(id, { scroll = "auto", fresh = false } = {}) {
  const seq = ++state.detailSeq;
  const wasAtBottom = isTimelineAtBottom();
  let detail;
  try {
    detail = await api(`/api/tickets/${id}`);
  } catch (err) {
    if (seq !== state.detailSeq || id !== state.selectedId) return;
    if (err.status === 404) {
      toast(`Ticket #${id} was not found.`, "warning");
      closeTicket();
    } else if (err.status !== 401) {
      toast(`Could not load ticket: ${err.detail}`, "danger");
      if (fresh) closeTicket();
    }
    return;
  }
  if (seq !== state.detailSeq || id !== state.selectedId) return; // user moved on

  const previousCount = state.detail?.comments.length ?? 0;
  state.detail = detail;
  renderHeader(detail);
  renderTimeline(detail);
  if (fresh) loadDraft(id, detail);
  $("workspace-loading").classList.add("d-none");
  $("workspace-body").classList.remove("d-none");
  patchCard(detail);

  if (scroll === "bottom" || (scroll === "auto" && wasAtBottom)) {
    scrollTimelineToBottom(fresh ? "auto" : "smooth");
    if (fresh) requestAnimationFrame(() => scrollTimelineToBottom("smooth"));
  } else if (scroll === "auto" && detail.comments.length > previousCount) {
    $("new-activity").classList.remove("d-none");
  }

  if (detail.comments.some((c) => c.delivery_status === "pending")) startDeliveryWatch();
}

/** During polling: reload the open ticket only if the list says it changed. */
async function syncOpenTicket() {
  const d = state.detail;
  if (!d) return;
  const summary = state.tickets.find((t) => t.id === d.id);
  if (summary && (summary.updated_at !== d.updated_at || summary.last_activity_at !== d.last_activity_at)) {
    await loadDetail(d.id, { scroll: "auto" });
  }
}

function renderHeader(t) {
  $("t-subject").textContent = t.subject;
  $("t-code").textContent = t.tracking_code;
  $("t-legacy").textContent = t.legacy_ref ? `HESK ${t.legacy_ref}` : "";
  $("t-legacy").classList.toggle("d-none", !t.legacy_ref);
  $("t-requester").replaceChildren(
    t.requester_name ? h("span", {}, t.requester_name, " ") : null,
    h("a", { href: `mailto:${t.requester_email}`, class: "link-secondary" }, `<${t.requester_email}>`));
  $("t-created").replaceChildren("Opened ", timeEl(t.created_at, ""));
  $("t-status-badge").replaceChildren(statusBadge(t.status));
  $("t-priority-badge").replaceChildren(priorityBadge(t.priority));

  // Assignee: active users, plus the current assignee even if since deactivated.
  const assignees = [...state.users];
  if (t.assignee && !assignees.some((u) => u.id === t.assignee.id)) assignees.push(t.assignee);
  $("t-assignee").replaceChildren(
    h("option", { value: "" }, "Unassigned"),
    ...assignees.map((u) => h("option", { value: String(u.id) }, u.id === state.me.id ? `${u.full_name} (me)` : u.full_name)));
  $("t-assignee").value = t.assignee ? String(t.assignee.id) : "";
  $("t-assign-me").classList.toggle("d-none", t.assignee?.id === state.me.id);

  $("t-status").replaceChildren(...Object.entries(STATUS_LABELS).map(([v, label]) => h("option", { value: v }, label)));
  $("t-status").value = t.status;
  $("t-priority").replaceChildren(...Object.entries(PRIORITY_LABELS).map(([v, label]) => h("option", { value: v }, label)));
  $("t-priority").value = t.priority;

  $("reply-target").textContent = `To: ${t.requester_email}`;
  updateReplyMode();
}

// ---------------------------------------------------------------------- timeline

function attachmentChips(list) {
  if (!list?.length) return null;
  return h("div", { class: "attachments" },
    list.map((a) => h("a", {
      class: "attachment", href: a.download_url, download: a.filename,
      title: `${a.filename} (${a.content_type}, ${formatBytes(a.size_bytes)})`,
    }, icon("bi-paperclip"), h("span", { class: "name" }, a.filename),
      h("span", { class: "text-body-secondary" }, formatBytes(a.size_bytes)))));
}

function deliveryInfo(c) {
  switch (c.delivery_status) {
    case "pending":
      return h("span", { class: "delivery text-body-secondary" },
        h("span", { class: "spinner-border", role: "status", "aria-hidden": "true" }), "Sending email…");
    case "sent":
      return h("span", { class: "delivery text-success-emphasis", title: fullTime(c.delivered_at) },
        icon("bi-check2-all"), "Emailed to customer ", c.delivered_at ? relTime(c.delivered_at) : "");
    case "failed":
      if (c.next_attempt_at) {
        return h("span", { class: "delivery text-warning-emphasis", title: c.delivery_error || "" },
          icon("bi-exclamation-triangle"),
          `Delivery failed (attempt ${c.delivery_attempts}); retrying ${relTime(c.next_attempt_at)}`);
      }
      return h("span", { class: "delivery text-danger-emphasis flex-wrap" },
        icon("bi-x-octagon"),
        h("span", { title: c.delivery_error || "" }, "Not delivered."),
        h("button", {
          type: "button", class: "btn btn-sm btn-outline-danger py-0 ms-1",
          onclick: (e) => resendReply(c.id, e.currentTarget),
        }, icon("bi-arrow-repeat", "me-1"), "Resend"));
    default:
      return null;
  }
}

function messageCard(kind, { who, whoTitle, tag, time, body, attachments, foot }) {
  return h("article", { class: `msg msg-${kind}` },
    h("header", { class: "msg-head" },
      h("span", { class: "who", title: whoTitle || null }, who),
      tag,
      timeEl(time)),
    h("div", { class: "msg-body" }, body || h("em", { class: "text-body-secondary" }, "(no text)")),
    attachmentChips(attachments),
    foot ? h("footer", { class: "msg-foot" }, foot) : null);
}

function commentItem(c) {
  if (c.source === "system") {
    return h("div", { class: "msg-system" }, icon("bi-clock-history"), h("span", {}, c.body), h("span", { "aria-hidden": "true" }, "·"), timeEl(c.created_at));
  }
  const staffName = c.author?.full_name || c.author_name || c.author_email || "Agent";
  const attachments = c.attachments;
  if (c.is_internal) {
    return messageCard("note", {
      who: staffName, whoTitle: c.author?.email || c.author_email,
      tag: h("span", { class: "badge text-bg-warning" }, icon("bi-lock-fill", "me-1"),
        { email: "Internal (via email)", import: "Internal (imported)" }[c.source] ?? "Internal note"),
      time: c.created_at, body: c.body, attachments,
    });
  }
  if (c.author) {
    return messageCard("agent", {
      who: staffName, whoTitle: c.author.email,
      tag: h("span", { class: "badge text-bg-primary" }, icon("bi-reply-fill", "me-1"), c.source === "import" ? "Agent reply (imported)" : "Agent reply"),
      time: c.created_at, body: c.body, attachments, foot: deliveryInfo(c),
    });
  }
  return messageCard("customer", {
    who: c.author_name || c.author_email || "Customer", whoTitle: c.author_email,
    tag: h("span", { class: "badge text-bg-secondary" }, icon(c.source === "web" ? "bi-globe" : "bi-envelope-fill", "me-1"),
      { import: "Customer (imported)", web: "Customer (portal)" }[c.source] ?? "Customer"),
    time: c.created_at, body: c.body, attachments,
  });
}

function renderTimeline(t) {
  const original = messageCard("customer", {
    who: t.requester_name || t.requester_email, whoTitle: t.requester_email,
    tag: h("span", { class: "badge text-bg-secondary" }, icon(t.source === "email" ? "bi-envelope-open-fill" : "bi-globe", "me-1"),
      t.source === "email" ? "Original email" : t.legacy_ref ? "Original request" : "Submitted via portal"),
    time: t.created_at, body: t.description, attachments: t.attachments,
  });
  original.classList.add("msg-original");
  // Messages live in a width-capped column so customer and agent bubbles stay close
  // together on wide monitors; the timeline itself still scrolls full width.
  $("timeline").replaceChildren(h("div", { class: "timeline-inner" }, original, ...t.comments.map(commentItem)));
}

function isTimelineAtBottom() {
  const el = $("timeline");
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
}

function scrollTimelineToBottom(behavior = "smooth") {
  const el = $("timeline");
  requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior }));
  $("new-activity").classList.add("d-none");
}

// ---------------------------------------------------------------------- header edits

/** PATCH the ticket; on failure restore the control to the server's value. */
async function updateTicket(patch, control, successMsg) {
  const t = state.detail;
  if (!t) return;
  const controls = [$("t-assignee"), $("t-status"), $("t-priority"), $("t-assign-me")];
  controls.forEach((c) => (c.disabled = true));
  try {
    const summary = await api(`/api/tickets/${t.id}`, { method: "PATCH", body: patch });
    toast(successMsg(summary));
    patchCard(summary);
    await loadDetail(t.id, { scroll: "bottom" }); // shows the audit entry
    refreshList({ quiet: true });                  // tab counts / filters may change
  } catch (err) {
    if (err.status !== 401) toast(`Update failed: ${err.detail}`, "danger");
    renderHeader(state.detail);                    // revert the dropdowns
  } finally {
    controls.forEach((c) => (c.disabled = false));
    control?.focus();
  }
}

function onAssigneeChange() {
  const value = $("t-assignee").value;
  const id = value ? Number(value) : null;
  updateTicket({ assigned_to: id }, $("t-assignee"), (s) =>
    s.assignee ? `Assigned to ${s.assignee.full_name}${s.assignee.id !== state.me.id ? " (they've been emailed)" : ""}` : "Ticket unassigned");
}
function onAssignMe() {
  updateTicket({ assigned_to: state.me.id }, $("t-assignee"), () => "Assigned to you");
}
function onStatusChange() {
  updateTicket({ status: $("t-status").value }, $("t-status"), (s) => `Status set to ${STATUS_LABELS[s.status]}`);
}
function onPriorityChange() {
  updateTicket({ priority: $("t-priority").value }, $("t-priority"), (s) => `Priority set to ${PRIORITY_LABELS[s.priority]}`);
}

// ---------------------------------------------------------------------- reply box

const replyMode = () => (document.querySelector('input[name="reply-mode"]:checked')?.value ?? "public");

function updateReplyMode() {
  const internal = replyMode() === "internal";
  $("reply-form").classList.toggle("is-internal", internal);
  $("reply-body").placeholder = internal
    ? "Internal note: only agents will see this…"
    : `Reply to ${state.detail?.requester_name || state.detail?.requester_email || "the customer"}; this will be emailed…`;
  $("reply-target").classList.toggle("invisible", internal);
}

function saveDraft() {
  if (state.selectedId === null) return;
  const body = $("reply-body").value;
  if (body.trim()) state.drafts.set(state.selectedId, { body, mode: replyMode(), status: $("reply-status").value });
  else state.drafts.delete(state.selectedId);
}

function loadDraft(id) {
  const draft = state.drafts.get(id);
  $("reply-body").value = draft?.body ?? "";
  $("reply-status").value = draft?.status ?? "";
  $(draft?.mode === "internal" ? "mode-internal" : "mode-public").checked = true;
  $("reply-body").classList.remove("is-invalid");
  updateReplyMode();
}

async function onReplySubmit(event) {
  event.preventDefault();
  const t = state.detail;
  const textarea = $("reply-body");
  const body = textarea.value.trim();
  if (!t) return;
  if (!body) {
    textarea.classList.add("is-invalid");
    textarea.focus();
    return;
  }
  const internal = replyMode() === "internal";
  const status = $("reply-status").value || undefined;
  const button = $("reply-submit");
  button.disabled = true;
  textarea.readOnly = true;
  try {
    await api(`/api/tickets/${t.id}/comments`, { method: "POST", body: { body, is_internal: internal, status } });
    textarea.value = "";
    $("reply-status").value = "";
    state.drafts.delete(t.id);
    toast(internal ? "Internal note added" : "Reply saved, emailing the customer…");
    await loadDetail(t.id, { scroll: "bottom" });
    refreshList({ quiet: true });
  } catch (err) {
    if (err.status !== 401) toast(`Could not send: ${err.detail}`, "danger"); // text is kept
  } finally {
    button.disabled = false;
    textarea.readOnly = false;
    textarea.focus();
  }
}

async function resendReply(commentId, button) {
  const t = state.detail;
  button.disabled = true;
  try {
    await api(`/api/tickets/${t.id}/comments/${commentId}/resend`, { method: "POST" });
    toast("Retrying delivery…", "info");
    await loadDetail(t.id, { scroll: "keep" });
  } catch (err) {
    toast(`Resend failed: ${err.detail}`, "danger");
    button.disabled = false;
  }
}

// ====================================================================== polling

function startPolling() {
  stopPolling();
  if (!state.autoRefresh) return;
  state.pollTimer = setInterval(() => {
    if (!document.hidden) refreshList({ quiet: true });
  }, POLL_MS);
}

function stopPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

/** While a reply is "sending", re-check the open ticket every few seconds. */
function startDeliveryWatch() {
  if (state.deliveryTimer) return;
  const started = Date.now();
  state.deliveryTimer = setInterval(async () => {
    const t = state.detail;
    const pending = t?.comments.some((c) => c.delivery_status === "pending");
    if (!t || !pending || Date.now() - started > DELIVERY_WATCH_MAX_MS) return stopDeliveryWatch();
    await loadDetail(t.id, { scroll: "keep" });
  }, DELIVERY_POLL_MS);
}

function stopDeliveryWatch() {
  clearInterval(state.deliveryTimer);
  state.deliveryTimer = null;
}

// ====================================================================== misc actions

async function onPasswordSubmit(event) {
  event.preventDefault();
  const errorBox = $("password-error");
  const current = $("pw-current").value;
  const next = $("pw-new").value;
  errorBox.classList.add("d-none");
  const fail = (msg) => { errorBox.textContent = msg; errorBox.classList.remove("d-none"); };
  if (next.length < 10) return fail("The new password must be at least 10 characters.");
  if (next !== $("pw-confirm").value) return fail("The new passwords don't match.");
  try {
    await api("/auth/change-password", { method: "POST", body: { current_password: current, new_password: next } });
    bootstrap.Modal.getInstance($("password-modal")).hide();
    event.target.reset();
    toast("Password changed. Other sessions were signed out.");
  } catch (err) {
    fail(err.detail);
  }
}

function toggleTheme() {
  const next = document.documentElement.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-bs-theme", next);
  store.set("theme", next);
}

// ====================================================================== admin: users

/** Open a modal. Bootstrap ignores show() while the same modal is still fading
 *  out (e.g. "Edit" clicked right after saving), so wait for it to finish first. */
function showModal(id) {
  const el = $(id);
  const modal = bootstrap.Modal.getOrCreateInstance(el);
  if (el.dataset.hiding) el.addEventListener("hidden.bs.modal", () => modal.show(), { once: true });
  else modal.show();
}

/** Close one modal and open another once the first has fully hidden (no stacking). */
function swapModal(fromId, toId) {
  const from = $(fromId);
  if (!from.classList.contains("show")) return showModal(toId);
  from.addEventListener("hidden.bs.modal", () => showModal(toId), { once: true });
  bootstrap.Modal.getInstance(from).hide();
}

/** Promise<boolean> confirmation dialog. `body` may contain form controls. */
function confirmDialog({ title, body, okText, variant = "danger" }) {
  return new Promise((resolve) => {
    const el = $("confirm-modal");
    const ok = $("confirm-ok");
    $("confirm-title").textContent = title;
    $("confirm-body").replaceChildren(...[body].flat());
    ok.textContent = okText;
    ok.className = `btn btn-${variant}`;
    let confirmed = false;
    const onOk = () => { confirmed = true; bootstrap.Modal.getInstance(el).hide(); };
    ok.addEventListener("click", onOk, { once: true });
    el.addEventListener("hidden.bs.modal", () => { ok.removeEventListener("click", onOk); resolve(confirmed); }, { once: true });
    showModal("confirm-modal");
  });
}

/** 16 random characters (no look-alikes such as 0/O, 1/l), grouped for dictation. */
function generatePassword() {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
  const limit = 256 - (256 % alphabet.length); // rejection sampling: no modulo bias
  const out = [];
  while (out.length < 16) {
    for (const b of crypto.getRandomValues(new Uint8Array(32))) {
      if (b < limit && out.length < 16) out.push(alphabet[b % alphabet.length]);
    }
  }
  return out.join("").match(/.{4}/g).join("-");
}

async function copyToClipboard(input) {
  try {
    await navigator.clipboard.writeText(input.value);
  } catch {
    input.select(); // clipboard API needs HTTPS (or localhost); fall back to the legacy path
    document.execCommand("copy");
  }
  toast("Copied to clipboard");
}

function showSecret(fromModalId, intro, password) {
  $("secret-intro").textContent = intro;
  $("secret-value").value = password;
  swapModal(fromModalId, "secret-modal");
}

async function showAdminView() {
  if (!state.adminMode) {
    state.adminMode = true;
    document.body.classList.add("admin-mode");
    $("admin-back").href = state.selectedId !== null ? `#/tickets/${state.selectedId}` : "#/";
    $("admin-back").classList.remove("d-none");
    $("admin-link").classList.add("d-none");
    document.querySelector(".panes").classList.add("d-none");
    $("admin-view").classList.remove("d-none");
    updateTitle();
    loadSiteSettings();
  }
  await loadAdminUsers();
}

function hideAdminView() {
  if (!state.adminMode) return;
  state.adminMode = false;
  document.body.classList.remove("admin-mode");
  $("admin-back").classList.add("d-none");
  $("admin-link").classList.toggle("d-none", !isAdmin());
  document.querySelector(".panes").classList.remove("d-none");
  $("admin-view").classList.add("d-none");
  updateTitle();
}

/** Re-read our own account (role may have been changed by another admin). */
async function refreshMe() {
  try {
    state.me = await api("/auth/me");
    applyIdentity();
  } catch { /* 401 handled by api() */ }
}

function updateTitle() {
  document.title = state.adminMode ? `Users · ${state.siteName}` : state.siteName;
}

function applySiteName(name) {
  state.siteName = name;
  $("brand-name").textContent = name;
  $("login-site-name").textContent = name;
  document.querySelector('meta[name="site-name"]')?.setAttribute("content", name);
  updateTitle();
}

async function loadSiteSettings() {
  $("site-name-input").value = state.siteName;
  try {
    const settings = await api("/api/admin/settings");
    $("site-name-input").value = settings.site_name;
    if (settings.site_name !== state.siteName) applySiteName(settings.site_name); // changed by another admin
  } catch (err) {
    if (err.status !== 401 && err.status !== 403) toast(`Could not load settings: ${err.detail}`, "danger");
  }
}

async function onSiteSubmit(event) {
  event.preventDefault();
  const input = $("site-name-input");
  const name = input.value.trim();
  showFormError("site-error", "");
  if (!name) {
    input.classList.add("is-invalid");
    return showFormError("site-error", "Enter a site name.");
  }
  const button = $("site-save");
  button.disabled = true;
  try {
    const settings = await api("/api/admin/settings", { method: "PATCH", body: { site_name: name } });
    input.value = settings.site_name;
    applySiteName(settings.site_name);
    toast("Site name updated");
  } catch (err) {
    if (err.status !== 401) showFormError("site-error", err.detail);
  } finally {
    button.disabled = false;
  }
}

async function loadAdminUsers() {
  try {
    state.adminUsers = await api("/api/admin/users");
    renderAdmin();
  } catch (err) {
    if (err.status === 403) {
      toast("You no longer have admin access.", "warning");
      await refreshMe();
      location.hash = "#/";
    } else if (err.status !== 401) {
      toast(`Could not load users: ${err.detail}`, "danger");
    }
  }
}

/** Refresh everything that shows user data after an admin change. */
async function afterUserChange() {
  await Promise.all([loadAdminUsers(), loadUsers()]);
  refreshList({ quiet: true });
  if (state.detail) renderHeader(state.detail); // assignee dropdown options
}

function renderAdmin() {
  const q = $("admin-search").value.trim().toLowerCase();
  const showInactive = $("admin-show-inactive").checked;
  const all = state.adminUsers;
  const users = all.filter((u) =>
    (showInactive || u.is_active) &&
    (!q || u.full_name.toLowerCase().includes(q) || u.email.includes(q)));

  const active = all.filter((u) => u.is_active);
  const stat = (num, label, cls = "") => h("div", { class: `stat ${cls}` }, h("div", { class: "num" }, String(num)), h("div", { class: "label" }, label));
  const flagged = all.filter((u) => u.recent_failed_logins > 0).length;
  $("admin-summary").replaceChildren(
    stat(active.filter((u) => u.role === "agent").length, "Active agents"),
    stat(active.filter((u) => u.role === "admin").length, "Active admins"),
    stat(all.length - active.length, "Inactive"),
    stat(all.reduce((n, u) => n + u.open_tickets, 0), "Open tickets assigned"),
    stat(flagged, "With failed sign-ins (1h)", flagged ? "text-warning-emphasis" : ""));

  $("admin-rows").replaceChildren(...(users.length
    ? users.map(adminRow)
    : [h("tr", {}, h("td", { colspan: "7", class: "text-center text-body-secondary py-4" }, "No users match."))]));
}

function adminRow(u) {
  const self = u.id === state.me.id;
  const action = (iconName, label, onclick, variant = "btn-outline-secondary") =>
    h("button", { type: "button", class: `btn btn-sm ${variant} ms-1`, title: label, "aria-label": `${label}: ${u.full_name}`, onclick }, icon(iconName));
  return h("tr", { class: u.is_active ? "" : "inactive", dataset: { userId: u.id } },
    h("td", {},
      h("div", { class: "user-name" }, u.full_name,
        self ? h("span", { class: "badge text-bg-light border ms-2" }, "you") : null,
        h("span", { class: `badge role-${u.role} ms-1 d-sm-none` }, u.role === "admin" ? "Admin" : "Agent")),
      h("div", { class: "small text-body-secondary d-md-none text-truncate admin-email-sm", title: u.email }, u.email)),
    h("td", { class: "d-none d-md-table-cell text-break" }, u.email),
    h("td", { class: "d-none d-sm-table-cell" }, h("span", { class: `badge role-${u.role}` }, u.role === "admin" ? "Admin" : "Agent")),
    h("td", {},
      u.is_active
        ? h("span", { class: "badge text-bg-success" }, "Active")
        : h("span", { class: "badge text-bg-secondary" }, "Inactive"),
      u.recent_failed_logins
        ? h("span", { class: "badge text-bg-warning ms-1", title: "Failed sign-in attempts in the last hour" },
            icon("bi-shield-exclamation", "me-1"), `${u.recent_failed_logins} failed`)
        : null),
    h("td", { class: "d-none d-sm-table-cell text-end" }, String(u.open_tickets)),
    h("td", { class: "d-none d-lg-table-cell small" },
      u.last_login_at ? timeEl(u.last_login_at, "") : h("span", { class: "text-body-secondary" }, "Never")),
    h("td", { class: "text-end actions" },
      action("bi-pencil", "Edit", () => openUserModal(u)),
      action("bi-key", "Reset password", () => openResetModal(u)),
      u.recent_failed_logins ? action("bi-unlock", "Clear failed sign-ins", () => unlockUser(u), "btn-outline-warning") : null,
      self ? null
        : u.is_active
          ? action("bi-person-x", "Deactivate", () => confirmDeactivate(u), "btn-outline-danger")
          : action("bi-person-check", "Reactivate", () => reactivateUser(u), "btn-outline-success")));
}

function showFormError(boxId, msg) {
  const box = $(boxId);
  box.textContent = msg;
  box.classList.toggle("d-none", !msg);
}

function openUserModal(user = null) {
  state.editingUser = user;
  const self = user?.id === state.me.id;
  $("user-modal-title").textContent = user ? `Edit ${user.full_name}` : "Add user";
  $("u-name").value = user?.full_name ?? "";
  $("u-email").value = user?.email ?? "";
  $("u-role").value = user?.role ?? "agent";
  $("u-role").disabled = self;
  $("u-role-self").classList.toggle("d-none", !self);
  $("u-password-group").classList.toggle("d-none", Boolean(user));
  $("u-password").value = user ? "" : generatePassword();
  $("user-submit").textContent = user ? "Save changes" : "Create user";
  showFormError("user-error", "");
  showModal("user-modal");
}

async function onUserSubmit(event) {
  event.preventDefault();
  const user = state.editingUser;
  const name = $("u-name").value.trim();
  const email = $("u-email").value.trim().toLowerCase();
  const role = $("u-role").value;
  const password = $("u-password").value;
  if (!name) return showFormError("user-error", "Enter a name.");
  if (!$("u-email").checkValidity() || !email) return showFormError("user-error", "Enter a valid email address.");
  if (!user && password.length < 10) return showFormError("user-error", "The password must be at least 10 characters.");

  const button = $("user-submit");
  button.disabled = true;
  try {
    if (user) {
      const patch = {};
      if (name !== user.full_name) patch.full_name = name;
      if (email !== user.email) patch.email = email;
      if (role !== user.role && user.id !== state.me.id) patch.role = role;
      if (Object.keys(patch).length) {
        const updated = await api(`/api/users/${user.id}`, { method: "PATCH", body: patch });
        if (updated.id === state.me.id) { state.me = { ...state.me, ...updated }; applyIdentity(); }
        toast(`Saved ${updated.full_name}`);
      }
      bootstrap.Modal.getInstance($("user-modal")).hide();
    } else {
      const created = await api("/auth/register", { method: "POST", body: { email, full_name: name, role, password } });
      showSecret("user-modal", `${created.full_name} can now sign in as ${created.email} with this password:`, password);
    }
    await afterUserChange();
  } catch (err) {
    if (err.status !== 401) showFormError("user-error", err.detail);
  } finally {
    button.disabled = false;
  }
}

function openResetModal(user) {
  state.resetUser = user;
  const self = user.id === state.me.id;
  $("reset-modal-title").textContent = `Reset password: ${user.full_name}`;
  $("reset-intro").textContent = self
    ? "Set a new password for your own account. Your other sessions will be signed out; this one stays."
    : `${user.full_name} will be signed out everywhere and must use the new password. Failed sign-in attempts on the account are cleared too.`;
  $("reset-password").value = generatePassword();
  showFormError("reset-error", "");
  showModal("reset-modal");
}

async function onResetSubmit(event) {
  event.preventDefault();
  const user = state.resetUser;
  const password = $("reset-password").value;
  if (password.length < 10) return showFormError("reset-error", "The password must be at least 10 characters.");
  const button = $("reset-submit");
  button.disabled = true;
  try {
    await api(`/api/users/${user.id}/password`, { method: "POST", body: { password } });
    showSecret("reset-modal", `Share the new password with ${user.full_name}:`, password);
    await afterUserChange();
  } catch (err) {
    if (err.status !== 401) showFormError("reset-error", err.detail);
  } finally {
    button.disabled = false;
  }
}

async function unlockUser(user) {
  try {
    const res = await api(`/api/users/${user.id}/unlock`, { method: "POST" });
    toast(`Cleared ${res.cleared_failures} failed sign-in attempt${res.cleared_failures === 1 ? "" : "s"} for ${user.full_name}`);
    await loadAdminUsers();
  } catch (err) {
    if (err.status !== 401) toast(`Could not unlock: ${err.detail}`, "danger");
  }
}

/** Return a user's active tickets to the Unassigned queue. Returns how many moved. */
async function unassignOpenTickets(userId) {
  const params = new URLSearchParams({ assigned_to: String(userId), limit: "200" });
  ["new", "open", "in_progress", "pending"].forEach((st) => params.append("status", st));
  const page = await api(`/api/tickets?${params}`);
  let moved = 0;
  for (const t of page.items) { // sequential: gentle on the server, each gets its audit note
    await api(`/api/tickets/${t.id}`, { method: "PATCH", body: { assigned_to: null } });
    moved += 1;
  }
  return moved;
}

async function confirmDeactivate(user) {
  const body = [h("p", {}, `${user.full_name} will be signed out immediately and won't be able to sign in until reactivated. Their replies and history are kept.`)];
  let unassign = null;
  if (user.open_tickets) {
    unassign = h("input", { type: "checkbox", class: "form-check-input", id: "confirm-unassign", checked: true });
    body.push(h("div", { class: "form-check" }, unassign,
      h("label", { class: "form-check-label", for: "confirm-unassign" },
        `Also unassign their ${user.open_tickets} open ticket${user.open_tickets === 1 ? "" : "s"} (returns them to the Unassigned queue)`)));
  }
  if (!(await confirmDialog({ title: `Deactivate ${user.full_name}?`, body, okText: "Deactivate" }))) return;
  try {
    const moved = unassign?.checked ? await unassignOpenTickets(user.id) : 0;
    await api(`/api/users/${user.id}`, { method: "PATCH", body: { is_active: false } });
    toast(`${user.full_name} deactivated${moved ? `; ${moved} ticket${moved === 1 ? "" : "s"} unassigned` : ""}`);
  } catch (err) {
    if (err.status !== 401) toast(`Could not deactivate: ${err.detail}`, "danger");
  }
  await afterUserChange();
}

async function reactivateUser(user) {
  try {
    await api(`/api/users/${user.id}`, { method: "PATCH", body: { is_active: true } });
    toast(`${user.full_name} reactivated`);
    await afterUserChange();
  } catch (err) {
    if (err.status !== 401) toast(`Could not reactivate: ${err.detail}`, "danger");
  }
}

// ====================================================================== wiring

function bindEvents() {
  $("login-form").addEventListener("submit", onLoginSubmit);
  $("login-mode-toggle").addEventListener("click", () => setLoginMode(state.loginMode === "login" ? "setup" : "login"));

  $("status-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-filter]");
    if (!btn || btn.dataset.filter === state.filter) return;
    state.filter = btn.dataset.filter;
    store.set("filter", state.filter);
    state.tickets = [];
    renderTabs();
    $("ticket-list").scrollTop = 0;
    refreshList();
  });

  const resetAndRefresh = () => { state.tickets = []; $("ticket-list").scrollTop = 0; refreshList(); };
  $("search").addEventListener("input", debounce(() => {
    state.q = $("search").value.trim();
    resetAndRefresh();
  }, 300));
  $("assigned-filter").addEventListener("change", () => {
    state.assigned = $("assigned-filter").value;
    store.set("assigned", state.assigned);
    resetAndRefresh();
  });
  $("sort").addEventListener("change", () => {
    state.sort = $("sort").value;
    store.set("sort", state.sort);
    resetAndRefresh();
  });
  $("load-more").addEventListener("click", loadMore);

  $("refresh-btn").addEventListener("click", () => refreshList());
  $("auto-refresh").addEventListener("change", () => {
    state.autoRefresh = $("auto-refresh").checked;
    store.set("autoRefresh", state.autoRefresh ? "1" : "0");
    startPolling();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.me && state.autoRefresh && Date.now() - state.lastRefresh > POLL_MS) {
      refreshList({ quiet: true });
    }
  });

  $("t-assignee").addEventListener("change", onAssigneeChange);
  $("t-assign-me").addEventListener("click", onAssignMe);
  $("t-status").addEventListener("change", onStatusChange);
  $("t-priority").addEventListener("change", onPriorityChange);
  $("back-btn").addEventListener("click", () => closeTicket());
  $("new-activity").addEventListener("click", () => scrollTimelineToBottom());
  $("timeline").addEventListener("scroll", () => {
    if (isTimelineAtBottom()) $("new-activity").classList.add("d-none");
  });

  $("reply-form").addEventListener("submit", onReplySubmit);
  for (const r of document.querySelectorAll('input[name="reply-mode"]')) r.addEventListener("change", updateReplyMode);
  $("reply-body").addEventListener("input", () => $("reply-body").classList.remove("is-invalid"));
  $("reply-body").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("reply-form").requestSubmit();
  });

  $("logout-btn").addEventListener("click", logout);
  $("theme-toggle").addEventListener("click", toggleTheme);
  $("change-password-open").addEventListener("click", () => showModal("password-modal"));
  $("password-form").addEventListener("submit", onPasswordSubmit);

  for (const el of document.querySelectorAll(".modal")) {
    el.addEventListener("hide.bs.modal", () => (el.dataset.hiding = "1"));
    el.addEventListener("hidden.bs.modal", () => delete el.dataset.hiding);
  }
  $("admin-add").addEventListener("click", () => openUserModal());
  $("site-form").addEventListener("submit", onSiteSubmit);
  $("site-name-input").addEventListener("input", () => $("site-name-input").classList.remove("is-invalid"));
  $("admin-search").addEventListener("input", renderAdmin);
  $("admin-show-inactive").addEventListener("change", renderAdmin);
  $("user-form").addEventListener("submit", onUserSubmit);
  $("u-generate").addEventListener("click", () => ($("u-password").value = generatePassword()));
  $("user-modal").addEventListener("shown.bs.modal", () => $("u-name").focus());
  $("reset-form").addEventListener("submit", onResetSubmit);
  $("reset-generate").addEventListener("click", () => ($("reset-password").value = generatePassword()));
  $("secret-copy").addEventListener("click", () => copyToClipboard($("secret-value")));
  $("secret-modal").addEventListener("hidden.bs.modal", () => ($("secret-value").value = "")); // don't leave it in the DOM

  window.addEventListener("hashchange", () => state.me && routeFromHash());
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
    if (typing || e.ctrlKey || e.metaKey || e.altKey || !state.me || state.adminMode) return;
    if (e.key === "/") { e.preventDefault(); $("search").focus(); }
    else if (e.key === "r") refreshList();
    else if (e.key === "Escape" && state.selectedId !== null) closeTicket();
  });
}

async function init() {
  bindEvents();
  setLoginMode("login");
  try {
    state.me = await api("/auth/me");
    await showApp();
  } catch {
    showLogin();
  }
}

init();
