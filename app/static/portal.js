/* Customer portal: sign in with an emailed link, open and follow tickets.
 *
 * Talks to /portal/api with its own HttpOnly cookie (scoped to /portal). Every
 * piece of ticket text goes into the DOM as text via h(), never as HTML.
 */
"use strict";

const siteName = document.querySelector('meta[name="site-name"]')?.content || "Support";
const state = { me: null, tickets: [], draft: new Map() };
const MAX_FILES = Number(document.querySelector('meta[name="portal-max-files"]')?.content) || 5;
const NEXT_KEY = "tts.portal.next"; // where to go after signing in (survives the email round-trip)
const remember = (hash) => { try { localStorage.setItem(NEXT_KEY, hash); } catch { /* storage blocked */ } };
function takeRemembered() {
  try {
    const next = localStorage.getItem(NEXT_KEY);
    localStorage.removeItem(NEXT_KEY);
    return /^#\/(tickets\/\d+|new)$/.test(next || "") ? next : "#/";
  } catch { return "#/"; }
}

// ====================================================================== helpers

const $ = (id) => document.getElementById(id);

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
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
  const diff = (new Date(iso) - Date.now()) / 1000, abs = Math.abs(diff);
  if (abs < 45) return "just now";
  if (abs >= 30 * 86400) return new Date(iso).toLocaleDateString();
  const units = [["minute", 60], ["hour", 3600], ["day", 86400], ["week", 604800]];
  let [unit, secs] = units[0];
  for (const u of units) if (abs >= u[1]) [unit, secs] = u;
  return rtf.format(Math.round(diff / secs), unit);
}
const timeEl = (iso) => h("time", { datetime: iso, title: new Date(iso).toLocaleString() }, relTime(iso));
const formatBytes = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const statusBadge = (t) => h("span", { class: `badge status status-${t.status}` }, t.status_label);

function toast(message, variant = "success") {
  const el = h("div", { class: `toast align-items-center text-bg-${variant} border-0`, role: variant === "danger" ? "alert" : "status" },
    h("div", { class: "d-flex" }, h("div", { class: "toast-body" }, message),
      h("button", { type: "button", class: "btn-close btn-close-white me-2 m-auto", "data-bs-dismiss": "toast", "aria-label": "Close" })));
  $("toasts").append(el);
  el.addEventListener("hidden.bs.toast", () => el.remove());
  bootstrap.Toast.getOrCreateInstance(el, { delay: variant === "danger" ? 8000 : 4000 }).show();
}

// ====================================================================== API

class ApiError extends Error {
  constructor(status, body) {
    const details = Array.isArray(body?.details) ? body.details.map((d) => d.message).join("; ") : "";
    super(details || body?.error || (status === 0 ? "Can't reach the server. Check your connection." : `Error ${status}`));
    this.status = status;
  }
}

async function api(path, { method = "GET", json, form } = {}) {
  let res;
  try {
    res = await fetch(`/portal/api${path}`, {
      method,
      credentials: "same-origin",
      headers: json ? { "Content-Type": "application/json" } : {},
      body: json ? JSON.stringify(json) : form,
    });
  } catch {
    throw new ApiError(0, null);
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (res.status === 401 && !path.startsWith("/verify") && !path.startsWith("/request-link")) {
    state.me = null;
    showSignin("Please sign in again.");
    throw new ApiError(401, data);
  }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

function render(...nodes) {
  // Accepts nested arrays and skips null/false, like h() does for children.
  $("view").replaceChildren(...nodes.flat(Infinity).filter((n) => n != null && n !== false));
  window.scrollTo({ top: 0 });
}

function setAccount() {
  $("account").classList.toggle("d-none", !state.me);
  $("account-email").textContent = state.me?.email ?? "";
  document.title = siteName;
}

function fileInput(id) {
  return h("div", { class: "mt-3" },
    h("label", { class: "form-label small text-body-secondary", for: id }, icon("bi-paperclip", "me-1"), `Attach files (optional, up to ${MAX_FILES})`),
    h("input", { class: "form-control form-control-sm", type: "file", id, multiple: true }));
}

function addFiles(form, input) {
  const files = [...input.files];
  if (files.length > MAX_FILES) throw new ApiError(400, { error: `You can attach up to ${MAX_FILES} files` });
  files.forEach((f) => form.append("files", f));
}

function busy(button, on, label) {
  button.disabled = on;
  if (on) {
    button.dataset.label = button.textContent;
    button.replaceChildren(h("span", { class: "spinner-border spinner-border-sm me-2", "aria-hidden": "true" }), label);
  } else {
    button.textContent = button.dataset.label || button.textContent;
  }
}

// ====================================================================== sign in

function showSignin(notice = "") {
  setAccount();
  const email = h("input", { class: "form-control", type: "email", id: "signin-email", required: true, autocomplete: "email", placeholder: "you@example.com" });
  const button = h("button", { class: "btn btn-primary w-100", type: "submit" }, "Email me a sign-in link");
  const error = h("div", { class: "alert alert-danger py-2 small d-none", role: "alert" });
  const form = h("form", { novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    error.classList.add("d-none");
    if (!email.value.trim() || !email.checkValidity()) {
      error.textContent = "Enter a valid email address.";
      error.classList.remove("d-none");
      return;
    }
    busy(button, true, "Sending…");
    try {
      await api("/request-link", { method: "POST", json: { email: email.value.trim() } });
      render(h("div", { class: "panel signin-panel text-center" },
        icon("bi-envelope-check", "display-6 text-primary"),
        h("h1", { class: "h4 mt-3" }, "Check your email"),
        h("p", { class: "text-body-secondary" }, "We sent a sign-in link to ", h("strong", {}, email.value.trim()),
          ". It works once and expires soon."),
        h("p", { class: "small text-body-secondary mb-0" }, "Nothing arrived? Check your spam folder, or ",
          h("a", { href: "#/", onclick: (ev) => { ev.preventDefault(); showSignin(); } }, "try again"), ".")));
    } catch (err) {
      error.textContent = err.message;
      error.classList.remove("d-none");
      busy(button, false);
    }
  } },
    notice ? h("div", { class: "alert alert-info py-2 small" }, notice) : null,
    error,
    h("label", { class: "form-label", for: "signin-email" }, "Your email address"),
    email,
    h("div", { class: "form-text mb-3" }, "No password needed: we'll email you a one-time link."),
    button);
  render(h("div", { class: "panel signin-panel" },
    h("h1", { class: "h4" }, "Sign in"),
    h("p", { class: "text-body-secondary" }, `Open a new request or follow your existing tickets with ${siteName}.`),
    form));
  email.focus();
}

function showVerify(token) {
  setAccount();
  const button = h("button", { class: "btn btn-primary btn-lg w-100", type: "button" }, "Continue to sign in");
  const error = h("div", { class: "alert alert-danger py-2 small d-none", role: "alert" });
  // A click is required so email scanners that pre-open links can't use up the token.
  button.addEventListener("click", async () => {
    busy(button, true, "Signing in…");
    try {
      state.me = await api("/verify", { method: "POST", json: { token } });
      history.replaceState(null, "", location.pathname + takeRemembered());
      setAccount();
      route();
    } catch (err) {
      error.textContent = err.message;
      error.classList.remove("d-none");
      busy(button, false);
      button.after(h("a", { class: "btn btn-link w-100 mt-2", href: "#/", onclick: () => history.replaceState(null, "", location.pathname) }, "Request a new link"));
    }
  });
  render(h("div", { class: "panel signin-panel text-center" },
    icon("bi-shield-check", "display-6 text-primary"),
    h("h1", { class: "h4 mt-3" }, `Sign in to ${siteName}`),
    h("p", { class: "text-body-secondary" }, "You're one click away from your tickets."),
    error, button));
}

// ====================================================================== tickets

async function showList() {
  const tickets = await api("/tickets");
  const open = tickets.filter((t) => t.is_open), closed = tickets.filter((t) => !t.is_open);
  const row = (t) => h("a", { class: "ticket-row", href: `#/tickets/${t.id}` },
    h("div", { class: "min-w-0" },
      h("div", { class: "subject", title: t.subject }, t.subject),
      h("div", { class: "meta" }, `#${t.reference} · updated `, timeEl(t.last_activity_at))),
    statusBadge(t), icon("bi-chevron-right", "chevron"));
  const section = (title, list) => list.length ? [
    h("h2", { class: "group-title" }, title, ` (${list.length})`),
    h("div", { class: "ticket-group" }, list.map(row)),
  ] : [];
  render(
    h("div", { class: "d-flex flex-wrap align-items-center gap-2" },
      h("h1", { class: "h4 mb-0 me-auto" }, state.me?.name ? `Hi ${state.me.name}` : "Your tickets"),
      h("a", { class: "btn btn-primary", href: "#/new" }, icon("bi-plus-lg", "me-1"), "New request")),
    tickets.length
      ? [...section("Open", open), ...section("Resolved", closed)]
      : h("div", { class: "panel empty mt-4" }, icon("bi-inbox"), "You don't have any tickets yet.",
          h("div", { class: "mt-3" }, h("a", { class: "btn btn-outline-primary", href: "#/new" }, "Open your first request"))));
}

function showNew() {
  const name = h("input", { class: "form-control", id: "new-name", maxlength: "200", autocomplete: "name", value: state.me?.name ?? "" });
  const subject = h("input", { class: "form-control", id: "new-subject", maxlength: "200", required: true });
  const message = h("textarea", { class: "form-control", id: "new-message", rows: "8", maxlength: "50000", required: true,
    placeholder: "Describe the problem, what you expected, and anything you've already tried." });
  const files = fileInput("new-files");
  const error = h("div", { class: "alert alert-danger py-2 small d-none", role: "alert" });
  const button = h("button", { class: "btn btn-primary px-4", type: "submit" }, "Submit request");
  const form = h("form", { class: "panel new-panel mt-3", novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    error.classList.add("d-none");
    if (!subject.value.trim() || !message.value.trim()) {
      error.textContent = "Please fill in the subject and the description.";
      error.classList.remove("d-none");
      return;
    }
    const data = new FormData();
    data.append("name", name.value.trim());
    data.append("subject", subject.value.trim());
    data.append("message", message.value.trim());
    busy(button, true, "Submitting…");
    try {
      addFiles(data, files.querySelector("input"));
      const ticket = await api("/tickets", { method: "POST", form: data });
      toast("Request received. We've emailed you a confirmation.");
      location.hash = `#/tickets/${ticket.id}`;
    } catch (err) {
      error.textContent = err.message;
      error.classList.remove("d-none");
      busy(button, false);
    }
  } },
    error,
    h("div", { class: "row g-3" },
      h("div", { class: "col-sm-6" }, h("label", { class: "form-label", for: "new-name" }, "Your name"), name),
      h("div", { class: "col-sm-6" }, h("label", { class: "form-label", for: "new-email" }, "Email"),
        h("input", { class: "form-control", id: "new-email", value: state.me.email, disabled: true }))),
    h("div", { class: "mt-3" }, h("label", { class: "form-label", for: "new-subject" }, "Subject"), subject),
    h("div", { class: "mt-3" }, h("label", { class: "form-label", for: "new-message" }, "Description"), message),
    files,
    h("div", { class: "d-flex justify-content-end gap-2 mt-4" },
      h("a", { class: "btn btn-outline-secondary", href: "#/" }, "Cancel"), button));
  render(h("a", { class: "small", href: "#/" }, icon("bi-arrow-left", "me-1"), "Your tickets"),
    h("h1", { class: "h4 mt-2" }, "New request"), form);
  subject.focus();
}

const GROUP_GAP_MS = 60 * 60 * 1000;

/**
 * iMessage-style conversation: the customer's messages are blue on the right,
 * support's grey on the left. Consecutive messages from the same person form a
 * group: one caption above, the bubble tail on the last one.
 */
function conversation(messages) {
  const key = (m) => (m.from_customer ? "me" : `them:${m.author_name}`);
  const same = (a, b) => a && b && key(a) === key(b) && Math.abs(new Date(b.created_at) - new Date(a.created_at)) < GROUP_GAP_MS;
  return messages.map((m, i) => {
    const start = !same(messages[i - 1], m), tail = !same(m, messages[i + 1]);
    return h("article", { class: `bubble-row ${m.from_customer ? "me mine" : "them support"}${start ? " group-start" : ""}${tail ? " tail" : ""}` },
      start ? h("div", { class: "bubble-caption" }, h("span", { class: "who" }, m.from_customer ? "You" : m.author_name), "·", timeEl(m.created_at)) : null,
      h("div", { class: "bubble", title: new Date(m.created_at).toLocaleString() },
        h("div", { class: "bubble-body" }, m.body || h("em", {}, "(no text)")),
        m.attachments.length ? h("div", { class: "bubble-files" }, m.attachments.map((a) =>
          h("a", { class: "bubble-file file-chip", href: a.download_url, download: a.filename, title: a.filename },
            icon("bi-paperclip"), h("span", { class: "name" }, a.filename), h("small", {}, formatBytes(a.size_bytes))))) : null));
  });
}

async function showTicket(id) {
  let t;
  try {
    t = await api(`/tickets/${id}`);
  } catch (err) {
    if (err.status === 404) {
      render(h("div", { class: "panel empty" }, icon("bi-question-circle"), "We couldn't find that ticket in your account.",
        h("div", { class: "mt-3" }, h("a", { class: "btn btn-outline-primary", href: "#/" }, "Back to your tickets"))));
      return;
    }
    throw err;
  }

  const message = h("textarea", { class: "form-control", id: "reply-message", rows: "4", maxlength: "50000",
    placeholder: t.is_open ? "Write a reply…" : "Need more help? Replying reopens this ticket." });
  message.value = state.draft.get(t.id) ?? "";
  message.addEventListener("input", () => state.draft.set(t.id, message.value));
  const files = fileInput("reply-files");
  const error = h("div", { class: "alert alert-danger py-2 small d-none", role: "alert" });
  const send = h("button", { class: "btn btn-primary px-4", type: "submit" }, icon("bi-send", "me-1"), "Send reply");
  const reply = h("form", { class: "panel reply-panel", novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    error.classList.add("d-none");
    if (!message.value.trim()) { message.focus(); return; }
    const data = new FormData();
    data.append("message", message.value.trim());
    busy(send, true, "Sending…");
    try {
      addFiles(data, files.querySelector("input"));
      await api(`/tickets/${t.id}/messages`, { method: "POST", form: data });
      state.draft.delete(t.id);
      toast("Reply sent");
      await showTicket(t.id);
      window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
    } catch (err) {
      error.textContent = err.message;
      error.classList.remove("d-none");
      busy(send, false);
    }
  } },
    error,
    h("label", { class: "form-label fw-semibold", for: "reply-message" }, "Reply"),
    message, files,
    h("div", { class: "d-flex justify-content-end mt-3" }, send));

  const solve = t.is_open ? h("button", { class: "btn btn-outline-success btn-sm", type: "button", onclick: async (e) => {
    if (!confirm("Mark this ticket as solved? You can reply later to reopen it.")) return;
    busy(e.currentTarget, true, "Saving…");
    try {
      await api(`/tickets/${t.id}/close`, { method: "POST" });
      toast("Thanks! The ticket is marked as solved.");
      await showTicket(t.id);
    } catch (err) {
      toast(err.message, "danger");
    }
  } }, icon("bi-check2-circle", "me-1"), "This is solved") : null;

  render(
    h("a", { class: "small", href: "#/" }, icon("bi-arrow-left", "me-1"), "Your tickets"),
    h("div", { class: "d-flex flex-wrap align-items-start gap-2 mt-2" },
      h("div", { class: "me-auto" },
        h("h1", { class: "h4 mb-1 text-break" }, t.subject),
        h("div", { class: "small text-body-secondary" }, `#${t.reference} · opened `, timeEl(t.created_at))),
      h("div", { class: "d-flex align-items-center gap-2" }, statusBadge(t), solve)),
    t.is_open ? null : h("div", { class: "alert alert-success py-2 small mt-3 mb-0" }, icon("bi-check-circle", "me-1"),
      "This ticket is resolved. If you still need help, just reply below and it will reopen."),
    h("div", { class: "conversation chat chat-card" }, conversation(t.messages)),
    reply);
}

// ====================================================================== routing

async function route() {
  const hash = location.hash;
  const verify = hash.match(/^#\/verify\/([\w-]+)$/);
  if (verify) return showVerify(verify[1]);
  if (!state.me) {
    if (/^#\/(tickets\/\d+|new)$/.test(hash)) remember(hash);
    try {
      state.me = await api("/me");
    } catch (err) {
      if (err.status !== 401) render(h("div", { class: "alert alert-danger" }, err.message));
      return;
    }
    setAccount();
  }
  try {
    const ticket = hash.match(/^#\/tickets\/(\d+)$/);
    if (ticket) await showTicket(Number(ticket[1]));
    else if (hash === "#/new") showNew();
    else await showList();
  } catch (err) {
    if (err.status !== 401) render(h("div", { class: "alert alert-danger" }, err.message));
  }
}

$("logout").addEventListener("click", async () => {
  try { await api("/logout", { method: "POST" }); } catch { /* already signed out */ }
  state.me = null;
  history.replaceState(null, "", location.pathname);
  showSignin("You're signed out.");
});
window.addEventListener("hashchange", route);
route();
