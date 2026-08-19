// Work Dashboard — renders whatever /api/overview hands back.
//
// Every section arrives as {ok, items, error, needs_auth}, so a provider being
// down or disconnected only ever costs you that one card.

const DEMO = new URLSearchParams(location.search).has("demo");
const REFRESH_MS = 60_000;
const qs = DEMO ? "?demo" : "";

const el = (id) => document.getElementById(id);
let refreshTimer = null;
let tickTimer = null;
let providers = {};

// --------------------------------------------------------------------------- //
// Formatting
// --------------------------------------------------------------------------- //
const MINUTE = 60_000, HOUR = 60 * MINUTE, DAY = 24 * HOUR;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function clock(date) {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function ago(iso) {
  if (!iso) return "";
  const then = new Date(iso), diff = Date.now() - then.getTime();
  if (diff < MINUTE) return "just now";
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)}m ago`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)}h ago`;
  if (diff < 2 * DAY) return "yesterday";
  if (diff < 7 * DAY) return `${Math.floor(diff / DAY)}d ago`;
  return then.toLocaleDateString([], { day: "numeric", month: "short" });
}

// "in 25 min" / "in progress · 20 min left" / "tomorrow at 09:00"
function whenLabel(event) {
  const now = Date.now();
  const start = new Date(event.start).getTime();
  const end = event.end ? new Date(event.end).getTime() : start + HOUR;

  if (now >= start && now < end) {
    return { text: `in progress · ${Math.round((end - now) / MINUTE)} min left`, live: true };
  }
  const until = start - now;
  if (event.all_day) return { text: until < DAY ? "today · all day" : "all day", live: false };
  if (until < MINUTE) return { text: "starting now", live: true };
  if (until < HOUR) return { text: `in ${Math.round(until / MINUTE)} min`, live: false };

  const startDate = new Date(start);
  const midnight = new Date(); midnight.setHours(24, 0, 0, 0);
  if (start < midnight.getTime()) return { text: `today at ${clock(startDate)}`, live: false };
  if (start < midnight.getTime() + DAY) return { text: `tomorrow at ${clock(startDate)}`, live: false };
  return {
    text: startDate.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })
          + ` at ${clock(startDate)}`,
    live: false,
  };
}

const LABELS = { google: "Google Workspace", salesforce: "Salesforce" };

function priorityClass(item) {
  if (item.closed) return "done";
  const p = (item.priority || "").toLowerCase();
  if (p === "high" || p === "critical" || p === "urgent") return "hot";
  if (p === "medium") return "warm";
  return "";
}

// --------------------------------------------------------------------------- //
// Section rendering
// --------------------------------------------------------------------------- //
// Not being connected yet isn't an error — say so quietly, and only offer the
// "Connect" link once an OAuth client actually exists for that provider.
function notConnected(provider) {
  const link = providers[provider]?.configured
    ? `<a class="fix" href="/auth/${provider}/start">Connect ${LABELS[provider]} →</a>`
    : "";
  return `<p class="empty">Not connected to ${LABELS[provider]} yet.${link}</p>`;
}

function providerFor(name) {
  return name === "cases" ? "salesforce" : "google";
}

function renderSection(name, section, emptyText, rowsHtml) {
  const target = el(name);
  const counter = el(`${name}Count`);

  if (!section) {
    target.innerHTML = `<p class="empty">No data.</p>`;
    counter.textContent = "";
    return;
  }
  if (!section.ok) {
    counter.textContent = "";
    target.innerHTML = section.needs_auth
      ? notConnected(providerFor(name))
      : `<p class="error">${esc(section.error || "Something went wrong.")}</p>`;
    return;
  }
  counter.textContent = section.items.length ? `${section.items.length}` : "";
  target.innerHTML = section.items.length
    ? `<ul class="list">${section.items.map(rowsHtml).join("")}</ul>`
    : `<p class="empty">${esc(emptyText)}</p>`;
}

function emailRow(mail) {
  return `<li><a class="row ${mail.unread ? "unread" : ""}" href="${esc(mail.url)}" target="_blank" rel="noreferrer">
    <div class="line1">
      <span class="who">${esc(mail.from)}</span>
      <span class="when">${esc(ago(mail.at))}</span>
    </div>
    <div class="title">${mail.starred ? "★ " : ""}${esc(mail.subject)}</div>
    <div class="snippet">${esc(mail.snippet)}</div>
  </a></li>`;
}

function caseRow(item) {
  const pill = priorityClass(item);
  return `<li><a class="row" href="${esc(item.url)}" target="_blank" rel="noreferrer">
    <div class="line1">
      <span class="who">#${esc(item.number)}</span>
      ${item.status ? `<span class="pill ${pill}">${esc(item.status)}</span>` : ""}
      <span class="when">${esc(ago(item.at))}</span>
    </div>
    <div class="title">${esc(item.subject)}</div>
    <div class="snippet">${esc([item.account, item.contact, item.priority && `${item.priority} priority`]
      .filter(Boolean).join(" · "))}</div>
  </a></li>`;
}

function chatRow(msg) {
  return `<li><a class="row" href="${esc(msg.url)}" target="_blank" rel="noreferrer">
    <div class="line1">
      <span class="who">${esc(msg.from)}</span>
      ${msg.direct ? "" : `<span class="pill">${esc(msg.space)}</span>`}
      <span class="when">${esc(ago(msg.at))}</span>
    </div>
    <div class="snippet">${esc(msg.text)}</div>
  </a></li>`;
}

function laterRow(event) {
  const when = whenLabel(event);
  return `<li><a class="row" href="${esc(event.url || "#")}" target="_blank" rel="noreferrer">
    <div class="line1">
      <span class="who">${esc(event.title)}</span>
      <span class="when">${esc(when.text)}</span>
    </div>
  </a></li>`;
}

function renderEvents(section) {
  const target = el("events");
  const counter = el("eventsCount");

  if (!section.ok) {
    counter.textContent = "";
    target.innerHTML = section.needs_auth
      ? notConnected("google")
      : `<p class="error">${esc(section.error)}</p>`;
    return;
  }
  if (!section.items.length) {
    counter.textContent = "";
    target.innerHTML = `<p class="empty">Nothing on the calendar for the next two weeks.</p>`;
    return;
  }

  const [next, ...later] = section.items;
  const when = whenLabel(next);
  const start = new Date(next.start);
  const end = next.end ? new Date(next.end) : null;
  const span = next.all_day ? "all day" : `${clock(start)}${end ? `–${clock(end)}` : ""}`;
  const meta = [
    span,
    next.location,
    next.attendees > 1 ? `${next.attendees} attendees` : "",
    next.organizer ? `by ${next.organizer}` : "",
    next.accepted === false ? "not accepted yet" : "",
  ].filter(Boolean);

  counter.textContent = later.length ? `+${later.length} later` : "";
  target.innerHTML = `
    <div class="countdown">${when.live ? `<span class="pill live">now</span> ` : ""}${esc(when.text)}</div>
    <div class="hero-title">${esc(next.title)}</div>
    <div class="hero-meta">${meta.map((m) => `<span>${esc(m)}</span>`).join("")}</div>
    <div class="hero-actions">
      ${next.meet_url ? `<a class="primary" href="${esc(next.meet_url)}" target="_blank" rel="noreferrer">Join meeting</a>` : ""}
      ${next.url ? `<a href="${esc(next.url)}" target="_blank" rel="noreferrer">Open in Calendar</a>` : ""}
    </div>
    ${later.length ? `<div class="later"><ul class="list">${later.map(laterRow).join("")}</ul></div>` : ""}`;
}

// --------------------------------------------------------------------------- //
// Connect banner
// --------------------------------------------------------------------------- //
function renderStatus(status) {
  el("demoBadge").hidden = !status.demo;
  providers = status.providers || {};

  const missing = Object.entries(status.providers || {})
    .filter(([, state]) => !state.connected);
  const banner = el("connect");
  if (!missing.length) { banner.classList.remove("show"); return; }

  const unconfigured = missing.filter(([, s]) => !s.configured).map(([n]) => LABELS[n]);
  const connectable = missing.filter(([, s]) => s.configured);

  el("connectText").textContent = unconfigured.length
    ? `${unconfigured.join(" and ")} ${unconfigured.length > 1 ? "have" : "has"} no OAuth client yet — see README.md, then restart.`
    : "Connect your accounts to fill the dashboard.";
  el("connectButtons").innerHTML = connectable
    .map(([name]) => `<button class="primary" data-provider="${name}">Connect ${LABELS[name]}</button>`)
    .join(" ");
  el("connectButtons").querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => { location.href = `/auth/${btn.dataset.provider}/start`; });
  });
  banner.classList.add("show");
}

// --------------------------------------------------------------------------- //
// Loading
// --------------------------------------------------------------------------- //
async function load() {
  try {
    const [overview, status] = await Promise.all([
      fetch(`/api/overview${qs}`).then((r) => r.json()),
      fetch(`/api/status${qs}`).then((r) => r.json()),
    ]);

    renderStatus(status);
    const s = overview.sections;
    renderEvents(s.events);
    renderSection("emails", s.emails, "Inbox is clear.", emailRow);
    renderSection("cases", s.cases, "No cases found.", caseRow);
    renderSection("chats", s.chats, "No recent messages.", chatRow);

    el("updated").textContent = `updated ${clock(new Date(overview.generated_at))}`;
    window.__events = s.events;   // kept so the ticker can re-render the countdown
  } catch (err) {
    el("updated").textContent = "could not reach the local server";
    console.error(err);
  }
}

function startTimers() {
  clearInterval(refreshTimer);
  clearInterval(tickTimer);
  refreshTimer = setInterval(load, REFRESH_MS);
  // Keep "in 25 min" honest between full refreshes.
  tickTimer = setInterval(() => {
    if (window.__events?.ok && window.__events.items.length) renderEvents(window.__events);
  }, 30_000);
}

el("refreshBtn").addEventListener("click", () => { load(); startTimers(); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { clearInterval(refreshTimer); clearInterval(tickTimer); }
  else { load(); startTimers(); }
});

load();
startTimers();
