"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ui = {
  route: "overview",
  search: "",
  envFilter: localGet("envFilter") || "all",
  drawerKey: null,
  status: null,
  fastUntil: 0,
  addKind: "repos",
};

function localGet(k) { try { return localStorage.getItem("lookout." + k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem("lookout." + k, v); } catch { /* private mode */ } }

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 2600);
}

// ---------- time ----------
function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function until(ts) {
  const s = ts - Date.now() / 1000;
  if (s <= 30) return "soon";
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  const total = Math.round(s / 60), h = Math.floor(total / 60), m = total % 60;
  return m ? `in ${h} h ${m} min` : `in ${h} h`;
}
const dateOf = (iso) => { try { return iso ? new Date(iso).toLocaleDateString() : ""; } catch { return ""; } };
const shortDigest = (d) => d ? d.replace("sha256:", "").slice(0, 12) : "";

// ---------- small renderers ----------
const FLAG_LABEL = { security: "Security", breaking: "Breaking", major: "Major" };
const FLAG_LONG = { security: "Security fixes", breaking: "Breaking changes", major: "Major version" };
const flagChips = (flags) => (flags || []).length ? `<span class="flags">${flags.map((f) => `<span class="flag ${f}">${FLAG_LABEL[f] || esc(f)}</span>`).join("")}</span>` : "";
// Release notes are markdown. Escape everything, then bring back a few safe pieces:
// headings, bold, bullets and http(s) links.
function mdLite(text) {
  return esc(text || "").replace(/\r/g, "").split("\n").map((line) => {
    let l = line;
    const h = l.match(/^\s*#{1,6}\s+(.*)$/);
    if (h) l = `<strong>${h[1]}</strong>`;
    l = l.replace(/^\s*[-*]\s+/, "• ");
    l = l.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    l = l.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    l = l.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">link</a>');
    return l;
  }).join("\n").replace(/\n{3,}/g, "\n\n");
}
const tagOf = (ref) => { const last = ref.split("/").pop(); return last.includes(":") ? last.split(":").pop() : "latest"; };
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function containerInfo(img) {
  if (img.status === "error" || img.status === "skipped") return esc(img.reason || "");
  const bits = [];
  if (img.digest_update) bits.push(`New image for <code>:${esc(tagOf(img.ref))}</code>`);
  if (img.newest_tag) bits.push(`Newer version <code>${esc(img.newest_tag)}</code>`);
  return bits.join(" · ") || `<span class="muted">Up to date</span>`;
}
function imageInfo(i) {
  if (i.status === "error") return esc(i.error);
  if (i.status === "pending") return `<span class="muted">Getting the current image…</span>`;
  const bits = [];
  if (i.latest_digest && i.installed_digest !== i.latest_digest) bits.push(`New image for <code>:${esc(tagOf(i.ref))}</code>`);
  if (i.newest_tag) bits.push(`Newer version <code>${esc(i.newest_tag)}</code>`);
  return bits.join(" · ") || `<span class="muted">No change since you added it</span>`;
}
function repoInfo(r) {
  if (r.status === "error") return esc(r.error);
  if (r.status === "pending") return `<span class="muted">Looking up releases…</span>`;
  if (r.status === "none") return `<span class="muted">No releases published</span>`;
  if (r.status === "update") return `<code>${esc(r.installed)}</code> → <code>${esc(r.latest)}</code>`;
  return `<code>${esc(r.latest)}</code> <span class="muted">· ${esc(dateOf(r.published_at))}</span>`;
}
// "from → to" when the release-notes lookup found the versions.
function versionText(it) {
  const c = it.changes || {};
  if (c.from && c.to && c.from !== c.to) return `<code>${esc(c.from)}</code> → <code>${esc(c.to)}</code>`;
  return it.what;
}

// How each container runs: Portainer stack (total control), compose outside
// Portainer (limited), or standalone.
function managers(img) {
  const seen = new Map();
  for (const m of img.members || []) {
    const g = m.manager || { type: m.project ? "compose" : "container", stack: m.project, control: null };
    const key = `${g.type}:${g.stack || ""}`;
    if (!seen.has(key)) seen.set(key, { ...g, names: [], dir: m.compose_dir, files: m.compose_files });
    seen.get(key).names.push(m.name);
  }
  return [...seen.values()];
}
function mgrBadge(g) {
  if (g.type === "stack") return `<span class="mgr total" title="Portainer stack · total control">Stack · ${esc(g.stack)}<em>Total</em></span>`;
  if (g.type === "swarm") return `<span class="mgr limited" title="${esc(g.why || "")}">Swarm · ${esc(g.stack)}</span>`;
  if (g.type === "compose") return `<span class="mgr limited" title="${esc(g.why || "Compose project")}">Compose · ${esc(g.stack)}<em>${g.control === "limited" ? "Limited" : "Compose"}</em></span>`;
  return `<span class="mgr standalone" title="${esc(g.why || "")}">Standalone</span>`;
}
const managerBadges = (img) => managers(img).map(mgrBadge).join("") || `<span class="muted">—</span>`;

function updateAction(img, host) {
  if (img.status !== "update") return "";
  const p = (ui.status && ui.status.portainer) || {};
  if (!img.digest_update && img.newest_tag) return `<span class="hint-inline">Change the tag to update</span>`;
  if (!p.connected || !p.allow_updates) return "";
  if (!img.can_update) return `<span class="hint-inline">Update outside Portainer</span>`;
  // With a newer tag also out, the button only re-pulls the tag in use: say so.
  const label = img.newest_tag ? `Re-pull :${esc(tagOf(img.ref))}` : "Update";
  return `<button class="btn tiny primary" data-update="${esc(host.id)}" data-ref="${esc(img.ref)}">${label}</button>`;
}

// ---------- one model for everything listed ----------
function allItems(s) {
  const out = [];
  const multi = s.hosts.length > 1;
  for (const h of s.hosts) for (const img of h.images) {
    out.push({
      key: `c|${h.id}|${img.ref}`, kind: "container", host: h, raw: img,
      name: img.ref, sub: img.containers.join(", ") + (multi ? ` · ${h.name}` : ""),
      status: img.status, flags: img.status === "update" ? ((img.changes || {}).flags || []) : [],
      changes: img.status === "update" ? img.changes : null,
      what: containerInfo(img), mg: managerBadges(img), action: updateAction(img, h), url: img.url,
      text: [img.ref, ...img.containers, h.name].join(" ").toLowerCase(),
    });
  }
  for (const i of s.images) {
    out.push({
      key: `i|${i.id}`, kind: "image", raw: i, name: i.label || i.ref, sub: i.label ? i.ref : "Watched image",
      status: i.status, flags: i.status === "update" ? ((i.changes || {}).flags || []) : [],
      changes: i.status === "update" ? i.changes : null,
      what: imageInfo(i), mg: "", url: i.url,
      action: i.status === "update" ? `<button class="btn tiny" data-ack="images:${esc(i.id)}">I've updated</button>` : "",
      text: [i.ref, i.label || ""].join(" ").toLowerCase(),
    });
  }
  for (const r of s.repos) {
    out.push({
      key: `r|${r.id}`, kind: "repo", raw: r, name: r.label || r.repo, sub: r.label ? r.repo : "GitHub releases",
      status: r.status, flags: r.status === "update" ? ((r.changes || {}).flags || []) : [],
      changes: r.status === "update" ? r.changes : null,
      what: repoInfo(r), mg: "", url: r.url || `https://github.com/${r.repo}`,
      action: r.status === "update" ? `<button class="btn tiny" data-ack="repos:${esc(r.id)}">I've updated</button>` : "",
      text: [r.repo, r.label || ""].join(" ").toLowerCase(),
    });
  }
  return out;
}
const RANK = { security: 0, breaking: 1, major: 2 };
const importance = (it) => Math.min(...(it.flags.length ? it.flags.map((f) => RANK[f] ?? 3) : [3]));
const matches = (it) => !ui.search || it.text.includes(ui.search.toLowerCase());

function itemRow(it, { showMg = true } = {}) {
  return `<li class="item" tabindex="0" data-open="${esc(it.key)}">
    <span class="state ${esc(it.status)}" title="${esc(it.status)}"></span>
    <div class="main"><div class="title">${esc(it.name)}</div><div class="sub">${esc(it.sub)}</div></div>
    <div class="what"><span>${it.status === "update" ? versionText(it) : it.what}</span>${flagChips(it.flags)}</div>
    ${showMg ? `<div class="mg">${it.mg}</div>` : ""}
    <div class="act">${it.action || ""}</div>
  </li>`;
}

// ---------- problems ----------
function fixFor(msg) {
  const m = (msg || "").toLowerCase();
  if (m.includes("socket not found")) return `Add <code>/var/run/docker.sock:/var/run/docker.sock:ro</code> to Lookout's volumes, or ignore this if Portainer already covers this server.`;
  if (m.includes("connection refused") || m.includes("no answer") || m.includes("can't reach docker")) return "Check the machine is on and the socket proxy container is running.";
  if (m.includes("environment is down")) return "Portainer can't reach this environment. Check the machine and its Portainer agent.";
  if (m.includes("needs a login") || m.includes("private images need credentials")) return "This image is private. Add a registry login in config.yaml, or remove it from the watch list.";
  if (m.includes("not found on")) return "The registry doesn't have this image or tag. It may have been renamed or removed.";
  if (m.includes("rate limit")) return `Add a GitHub token in <a href="#" data-open-settings>Settings</a>, or check less often.`;
  if (m.includes("rejected the access token") || m.includes("portainer")) return `Check the Portainer address and token in <a href="#" data-open-settings>Settings</a>.`;
  if (m.includes("doesn't exist or is private")) return "Check the spelling, or add a GitHub token for private repos.";
  return "";
}
function collectProblems(s) {
  const out = [];
  if (s.portainer && s.portainer.error) out.push({ where: "Portainer connection", sub: "", msg: s.portainer.error, fix: fixFor("portainer") });
  for (const h of s.hosts) {
    const check = `<button class="btn tiny" data-check="${esc(h.id)}">Check again</button>`;
    if (h.ok === false) out.push({ where: h.name, sub: h.source === "portainer" ? "Portainer environment" : (h.url || "local Docker"), msg: h.error, fix: fixFor(h.error), actions: check });
    for (const i of h.images) if (i.status === "error") out.push({ where: i.ref, sub: `${i.containers.join(", ")} · ${h.name}`, msg: i.reason, fix: fixFor(i.reason), actions: check });
  }
  for (const i of s.images) if (i.status === "error") out.push({ where: i.label || i.ref, sub: "Watched image", msg: i.error, fix: fixFor(i.error),
    actions: `<button class="btn tiny" data-check="${esc(i.id)}">Check again</button>` });
  for (const r of s.repos) if (r.status === "error") out.push({ where: r.label || r.repo, sub: "GitHub", msg: r.error, fix: fixFor(r.error),
    actions: `<button class="btn tiny" data-check="${esc(r.id)}">Check again</button>` });
  return out;
}

// ---------- routing ----------
function route() { ui.route = location.hash.replace(/^#\/?/, "") || "overview"; }
function go(r) { location.hash = "#/" + r; }

// ---------- render ----------
function render() {
  const s = ui.status;
  if (!s) return;
  renderChrome(s);
  const items = allItems(s);
  const r = ui.route;
  let title = "Overview", sub = "", html = "";
  if (r === "problems") { title = "Problems"; html = pageProblems(s); }
  else if (r === "images") { title = "Images"; sub = "Images for services Lookout can't see directly"; html = pageWatch(items.filter((i) => i.kind === "image"), "images"); }
  else if (r === "repos") { title = "GitHub releases"; sub = "Apps released on GitHub"; html = pageWatch(items.filter((i) => i.kind === "repo"), "repos"); }
  else if (r.startsWith("env/")) {
    const h = s.hosts.find((x) => x.id === r.slice(4));
    if (h) {
      title = h.name;
      sub = (h.source === "portainer" ? "Portainer environment" : (h.url || "Local Docker") + (h.portainer_env ? ` · Portainer “${h.portainer_env}”` : ""))
        + ` · checked ${ago(h.checked_at)}`;
      html = pageEnv(h, items.filter((i) => i.host && i.host.id === h.id));
    } else { html = `<div class="list empty">That environment isn't here any more.</div>`; }
  } else { html = pageOverview(s, items); }
  $("#page-title").textContent = title;
  $("#page-sub").textContent = sub || (s.checking ? "Checking for updates…" : `Checked ${ago(s.last_run)} · next ${until(s.next_run)} · every ${s.interval}`);
  const mobileFocused = document.activeElement && document.activeElement.classList.contains("mobile-search");
  $("#page").innerHTML = `<input class="mobile-search" type="search" placeholder="Search…" value="${esc(ui.search)}" aria-label="Search">` + html;
  if (mobileFocused) { const m = $(".mobile-search"); m.focus(); m.setSelectionRange(m.value.length, m.value.length); }
  if (ui.drawerKey) renderDrawer();
}

const ICONS = {
  home: `<svg class="ico" viewBox="0 0 24 24"><path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/></svg>`,
  warn: `<svg class="ico" viewBox="0 0 24 24"><path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.01"/></svg>`,
  box: `<svg class="ico" viewBox="0 0 24 24"><path d="M21 8l-9-5-9 5 9 5 9-5zM3 8v8l9 5 9-5V8M12 13v8"/></svg>`,
  gh: `<svg class="ico" viewBox="0 0 24 24"><circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="9" r="2.5"/><path d="M6 8.5v7M18 11.5c0 3-3 4-9.5 5"/></svg>`,
};

function renderChrome(s) {
  const btn = $("#btn-check");
  btn.classList.toggle("spin", s.checking);
  btn.disabled = s.checking;
  $("#btn-check span").textContent = s.checking ? "Checking…" : "Check now";
  $("#run-status").textContent = s.checking ? "Checking…" : `Checked ${ago(s.last_run)}`;
  $("#version").textContent = `Lookout ${s.version}`;
  $("#notify-status").innerHTML = s.notifiers.length ? `Notifying: ${esc(s.notifiers.join(", "))}` : `<a href="#" data-open-settings>Notifications off</a>`;

  const c = s.counts;
  const items = allItems(s);
  const navItem = (r, icon, name, count, cls = "") => `<a class="nav-item" href="#/${r}" ${ui.route === r ? 'aria-current="page"' : ""}>${icon}<span class="name">${esc(name)}</span>${count !== "" ? `<span class="count ${cls}">${count}</span>` : ""}</a>`;
  let nav = navItem("overview", ICONS.home, "Overview", c.updates || "", c.updates ? "hot" : "");
  if (c.errors) nav += navItem("problems", ICONS.warn, "Problems", c.errors, "bad");
  nav += `<div class="nav-label">Environments</div>`;
  for (const h of s.hosts) {
    const n = items.filter((i) => i.host && i.host.id === h.id && i.status === "update").length;
    const dot = `<span class="dot ${h.ok === false ? "bad" : h.ok == null ? "off" : ""}"></span>`;
    nav += navItem(`env/${h.id}`, dot, h.name, n || "", n ? "hot" : "");
  }
  if (!s.portainer.connected) nav += `<a class="nav-item muted" href="#" data-open-settings><span class="dot off"></span><span class="name">Connect Portainer…</span></a>`;
  nav += `<div class="nav-label">Watch list</div>`;
  const ni = items.filter((i) => i.kind === "image"), nr = items.filter((i) => i.kind === "repo");
  const upd = (l) => l.filter((i) => i.status === "update").length;
  nav += navItem("images", ICONS.box, "Images", upd(ni) || ni.length, upd(ni) ? "hot" : "");
  nav += navItem("repos", ICONS.gh, "GitHub", upd(nr) || nr.length, upd(nr) ? "hot" : "");
  $("#nav").innerHTML = nav;

  $("#banner").innerHTML = s.notifiers.length ? "" : `
    <div class="banner"><div><strong>Notifications aren't set up.</strong>
      <span class="muted">Updates show here, but nothing is sent until you add Discord, ntfy or Gotify.</span></div>
      <button class="btn" data-open-settings>Set up notifications</button></div>`;
}

function pageOverview(s, items) {
  const ups = items.filter((i) => i.status === "update" && matches(i));
  const important = ups.filter((i) => i.flags.length).sort((a, b) => importance(a) - importance(b));
  const others = ups.filter((i) => !i.flags.length)
    .sort((a, b) => (a.host?.name || "~").localeCompare(b.host?.name || "~") || a.name.localeCompare(b.name));
  const sec = ups.filter((i) => i.flags.includes("security")).length;
  const c = s.counts;
  let html = `<div class="stats">
    <button type="button" class="stat ${c.updates ? "hot" : ""}" data-jump="${important.length ? "important" : "others"}">
      <div class="label">Updates available</div><div class="value">${c.updates}</div></button>
    <button type="button" class="stat ${sec ? "sec" : important.length ? "hot" : ""}" data-jump="important">
      <div class="label">Need attention</div><div class="value">${important.length}</div></button>
    <a class="stat ${c.errors ? "bad" : ""}" href="#/problems" style="text-decoration:none"><div class="label">Problems</div><div class="value">${c.errors}</div></a>
    <div class="stat"><div class="label">Watching</div><div class="value">${c.watching}</div></div>
  </div>`;

  if (!ups.length) {
    return html + (ui.search ? `<div class="list empty">No updates match “${esc(ui.search)}”.</div>`
      : `<div class="list empty-hero"><div class="big">✓</div><h3>Everything's up to date</h3>
        <p class="muted">Lookout checks every ${esc(s.interval)}${s.notifiers.length ? " and will let you know" : ""}.</p></div>`);
  }
  if (important.length) {
    html += `<section class="section" id="important"><div class="section-head"><h2>Needs attention</h2>
      <p class="muted small">Security fixes, breaking changes and major versions, found in the release notes</p></div>
      <div class="important">${important.map(importantCard).join("")}</div></section>`;
  }
  if (others.length) {
    html += `<section class="section" id="others"><div class="section-head"><h2>${important.length ? "Other updates" : "Updates"}</h2>
      <p class="muted small">Click any update for details and release notes</p></div>
      <div class="list"><ul class="items">${others.map((i) => itemRow(i)).join("")}</ul></div></section>`;
  }
  return html;
}

function importantCard(it) {
  const why = it.changes && it.changes.why
    ? Object.entries(it.changes.why).filter(([k]) => k !== "major").flatMap(([, v]) => v).slice(0, 3) : [];
  const n = it.changes && it.changes.releases ? it.changes.releases.length : 0;
  return `<div class="icard ${it.flags[0] || "major"}" tabindex="0" data-open="${esc(it.key)}">
    <div class="top"><div><div class="name">${esc(it.name)}</div><div class="where">${esc(it.sub)}</div></div>${flagChips(it.flags)}</div>
    <div>${versionText(it)}</div>
    ${why.length ? `<ul class="why">${why.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    <div class="foot"><span class="muted small">${n ? `${plural(n, "release")} · details →` : "Details →"}</span>${it.action || ""}</div>
  </div>`;
}

function pageEnv(h, items) {
  const counts = {
    all: items.length,
    update: items.filter((i) => i.status === "update").length,
    ok: items.filter((i) => i.status === "ok").length,
    skipped: items.filter((i) => i.status === "skipped").length,
    error: items.filter((i) => i.status === "error").length,
  };
  const f = ui.envFilter !== "all" && counts[ui.envFilter] ? ui.envFilter : "all";
  const chip = (k, label) => counts[k] || k === "all" ? `<button class="chip" data-filter="${k}" aria-pressed="${f === k}">${label} ${counts[k]}</button>` : "";
  const shown = items.filter((i) => (f === "all" || i.status === f) && matches(i))
    .sort((a, b) => (a.status === "update" ? 0 : 1) - (b.status === "update" ? 0 : 1) || importance(a) - importance(b) || a.name.localeCompare(b.name));
  return `<div class="toolbar">
      <div class="chips">${chip("all", "All")}${chip("update", "Updates")}${chip("ok", "Up to date")}${chip("skipped", "Skipped")}${chip("error", "Problems")}</div>
      <div class="row gap"><button class="btn tiny" data-check="${esc(h.id)}">Check this environment</button>
        ${h.removable ? `<button class="btn tiny danger" data-remove="hosts:${esc(h.id)}" data-name="${esc(h.name)}">Remove</button>` : ""}</div>
    </div>
    <div class="list">
      ${h.ok === false ? `<div class="list-error">${esc(h.error)}${fixFor(h.error) ? `<div class="small">${fixFor(h.error)}</div>` : ""}</div>` : ""}
      ${shown.length ? `<ul class="items">${shown.map((i) => itemRow(i)).join("")}</ul>`
        : `<div class="empty">${items.length ? "Nothing matches." : h.ok === false ? "Can't see this environment's containers right now." : h.checked_at ? "No running containers here." : "Waiting for the first check…"}</div>`}
      ${h.ignored.length ? `<div class="foot-note">Ignored by label or settings: ${esc(h.ignored.join(", "))}</div>` : ""}
    </div>`;
}

function pageWatch(items, kind) {
  const shown = items.filter(matches).sort((a, b) => (a.status === "update" ? 0 : 1) - (b.status === "update" ? 0 : 1) || a.name.localeCompare(b.name));
  const what = kind === "repos" ? "GitHub repos" : "images";
  return `<div class="toolbar"><div></div><button class="btn tiny primary" data-add="${kind}">Add ${what}</button></div>
    <div class="list">${shown.length ? `<ul class="items">${shown.map((i) => itemRow(i, { showMg: false })).join("")}</ul>`
      : `<div class="empty">${items.length ? "Nothing matches." : kind === "repos" ? "Add apps that publish releases on GitHub to hear about new versions." : "Add images for services running somewhere Lookout can't see."}
        ${items.length ? "" : `<br><button class="btn" data-add="${kind}">Add ${what}</button>`}</div>`}</div>`;
}

function pageProblems(s) {
  const probs = collectProblems(s);
  if (!probs.length) return `<div class="list empty-hero"><div class="big">✓</div><h3>No problems</h3><p class="muted">Every check worked last time.</p></div>`;
  return `<div class="list"><ul class="items">${probs.map((p) => `
    <li class="item" style="cursor:default">
      <span class="state error"></span>
      <div class="main"><div class="title">${esc(p.where)}</div><div class="sub">${esc(p.sub)}</div></div>
      <div class="what" style="grid-column: span 2"><span>${esc(p.msg)}</span>${p.fix ? `<span class="sub">${p.fix}</span>` : ""}</div>
      <div class="act">${p.actions || ""}</div>
    </li>`).join("")}</ul></div>`;
}

// ---------- details drawer ----------
function openDrawer(key) {
  ui.drawerKey = key;
  renderDrawer();
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawer-scrim").classList.add("show");
}
function closeDrawer() {
  ui.drawerKey = null;
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawer-scrim").classList.remove("show");
}
function renderDrawer() {
  const it = allItems(ui.status).find((i) => i.key === ui.drawerKey);
  if (!it) { closeDrawer(); return; }
  $("#drawer-kind").textContent = { container: "Container image", image: "Watched image", repo: "GitHub releases" }[it.kind];
  $("#drawer-title").textContent = it.name;
  const c = it.changes || {};
  const raw = it.raw;
  let html = "";

  if (it.status === "update") {
    html += `<div class="dsec"><h4>Update</h4><div class="version">${versionText(it)}</div>
      ${c.from && c.to && c.from !== c.to && it.kind !== "repo" ? `<div class="small muted" style="margin-top:4px">${it.what}</div>` : ""}</div>`;
  } else {
    html += `<div class="dsec"><h4>Status</h4><div>${it.what}</div></div>`;
  }

  for (const f of it.flags) {
    const lines = (c.why && c.why[f]) || [];
    html += `<div class="callout ${f}"><strong>${FLAG_LONG[f]}</strong>${lines.length ? `<ul>${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}</div>`;
  }

  const acts = [];
  if (it.action && it.action.includes("<button")) acts.push(it.action.replace("btn tiny", "btn"));
  else if (it.action) acts.push(it.action);
  if (c.url) acts.push(`<a class="btn" href="${esc(c.url)}" target="_blank" rel="noopener">All releases ↗</a>`);
  if (it.url && it.kind !== "repo") acts.push(`<a class="btn" href="${esc(it.url)}" target="_blank" rel="noopener">Image page ↗</a>`);
  if (it.kind === "repo") acts.push(`<a class="btn" href="https://github.com/${esc(raw.repo)}" target="_blank" rel="noopener">Repository ↗</a>`);
  if (it.kind !== "container") acts.push(`<button class="btn danger" data-remove="${it.kind === "repo" ? "repos" : "images"}:${esc(raw.id)}" data-name="${esc(it.name)}">Remove</button>`);
  if (acts.length) html += `<div class="drawer-actions">${acts.join("")}</div>`;

  if (it.status === "update") {
    if (c.releases && c.releases.length) {
      html += `<div class="dsec"><h4>Release notes${c.repo ? ` · ${esc(c.repo)}` : ""}</h4>${c.releases.map((r, n) => `
        <details class="rel" ${n === 0 ? "open" : ""}>
          <summary><strong>${esc(r.name || r.tag)}</strong>${flagChips(r.flags)}<span class="muted small">${esc(dateOf(r.published_at))}</span>
            ${r.url ? `<a class="small" href="${esc(r.url)}" target="_blank" rel="noopener" style="margin-left:auto">Open ↗</a>` : ""}</summary>
          <div class="notes">${r.notes ? mdLite(r.notes) : "No notes."}</div>
        </details>`).join("")}</div>`;
    } else if (c.note || c.error) {
      html += `<div class="callout info">${esc(c.note || "Couldn't load release notes: " + c.error)}</div>`;
    } else if (!it.changes) {
      html += `<div class="callout info">Release notes appear after the next check.</div>`;
    } else {
      html += `<div class="callout info">No release notes were found between these versions.</div>`;
    }
  }

  if (it.kind === "container") {
    const mg = managers(raw);
    html += `<div class="dsec"><h4>Where it runs</h4><dl class="facts">
      <dt>Environment</dt><dd>${esc(it.host.name)}</dd>
      <dt>Containers</dt><dd>${esc(raw.containers.join(", "))}</dd>
      <dt>Managed by</dt><dd>${mg.map(mgrBadge).join(" ") || "—"}</dd>
      ${mg.filter((g) => g.files || g.dir).map((g) => `<dt>Compose file</dt><dd class="mono">${esc(g.files || g.dir)}</dd>`).join("")}
      ${raw.local_digest ? `<dt>Running</dt><dd class="mono">${esc(shortDigest(raw.local_digest))}</dd>` : ""}
      ${raw.remote_digest && raw.remote_digest !== raw.local_digest ? `<dt>Available</dt><dd class="mono">${esc(shortDigest(raw.remote_digest))}</dd>` : ""}
    </dl></div>`;
    for (const g of mg) {
      if (g.why) html += `<div class="callout info">${esc(g.why)}</div>`;
      if (g.type === "compose" && g.control === "limited") html += moveToPortainer(g);
    }
    if (it.status === "update" && raw.newest_tag) {
      html += `<div class="callout info">To move to <code>${esc(raw.newest_tag)}</code>, change the image tag in the stack or compose file and redeploy.</div>`;
    }
  } else if (it.kind === "image") {
    html += `<div class="dsec"><h4>Details</h4><dl class="facts">
      <dt>Image</dt><dd class="mono">${esc(raw.ref)}</dd>
      <dt>You run</dt><dd class="mono">${esc(shortDigest(raw.installed_digest))}</dd>
      <dt>Available</dt><dd class="mono">${esc(shortDigest(raw.latest_digest))}</dd>
      <dt>Checked</dt><dd>${ago(raw.checked_at)}</dd></dl>
      <p class="small muted">After you update the service, click <strong>I've updated</strong> so Lookout knows what you run now.</p></div>`;
  } else {
    html += `<div class="dsec"><h4>Details</h4><dl class="facts">
      <dt>You run</dt><dd>${esc(raw.installed || "—")}</dd><dt>Latest</dt><dd>${esc(raw.latest || "—")}</dd>
      <dt>Pre-releases</dt><dd>${raw.include_prereleases ? "Included" : "Ignored"}</dd><dt>Checked</dt><dd>${ago(raw.checked_at)}</dd></dl></div>`;
  }
  $("#drawer-body").innerHTML = html;
}

function moveToPortainer(g) {
  const where = g.files || g.dir;
  return `<details class="howto"><summary>Move “${esc(g.stack)}” into Portainer (keeps your data)</summary>
    <ol class="howto-list">
      <li>Open the compose file${where ? `: <code>${esc(where)}</code>` : " in the folder you ran <code>docker compose</code> from"}. Copy its contents, and any <code>.env</code> values next to it.</li>
      <li>Change relative paths like <code>./config:/config</code> to full paths on that machine, like <code>${esc(g.dir || "/path/to/folder")}/config:/config</code>. Portainer keeps stacks in its own folder, so relative paths would point somewhere new and empty.</li>
      <li>On that machine, in that folder, run <code>docker compose down</code>. <strong>Don't</strong> add <code>-v</code>: that deletes volumes.</li>
      <li>In Portainer: <strong>Stacks → Add stack</strong>. Name it exactly <code>${esc(g.stack)}</code> so its named volumes (<code>${esc(g.stack)}_…</code>) are reused. Paste the file, add the <code>.env</code> values under Environment variables, and deploy.</li>
      <li>Back in Lookout, <strong>Check now</strong>. It should show <em>Stack · ${esc(g.stack)} TOTAL</em>, and Update will work.</li>
    </ol></details>`;
}

// ---------- data loop ----------
async function refresh() {
  try {
    ui.status = await api("GET", "/api/status");
    render();
  } catch (e) {
    $("#run-status").textContent = "Can't reach Lookout";
    $("#page-sub").textContent = e.message;
  }
  const fast = ui.status?.checking || Date.now() < ui.fastUntil;
  clearTimeout(refresh.timer);
  refresh.timer = setTimeout(refresh, fast ? 2000 : 30000);
}
function soon() { ui.fastUntil = Date.now() + 20000; setTimeout(refresh, 600); }

async function checkNow(only) {
  try {
    await api("POST", "/api/check", only ? { only } : {});
    toast(only ? "Checking…" : "Checking everything…");
    soon();
  } catch (e) { toast(e.message); }
}

// ---------- add dialog ----------
function openAdd(kind = "repos") {
  setAddKind(kind);
  $("#host-test").textContent = "";
  $("#dlg-add").showModal();
  setTimeout(() => $(`[data-pane="${kind}"] textarea, [data-pane="${kind}"] input`)?.focus(), 30);
}
function setAddKind(kind) {
  ui.addKind = kind;
  for (const b of $$("#add-kind button")) b.setAttribute("aria-pressed", String(b.dataset.kind === kind));
  for (const p of $$(".add-pane")) p.hidden = p.dataset.pane !== kind;
  $("#btn-add-submit").textContent = kind === "hosts" ? "Add host" : "Add";
  $("#add-msg").textContent = "";
}
function addMsg(text, ok) { const m = $("#add-msg"); m.innerHTML = text; m.className = "small " + (ok ? "msg-ok" : "msg-bad"); }
async function submitAdd() {
  const btn = $("#btn-add-submit");
  btn.disabled = true;
  try {
    if (ui.addKind === "hosts") {
      addMsg("Connecting…", true);
      const r = await api("POST", "/api/hosts", { name: $("#add-host-name").value, url: $("#add-host-url").value });
      $("#add-host-name").value = $("#add-host-url").value = "";
      $("#dlg-add").close();
      toast(`Added ${r.host.name}. Checking its containers…`);
      go(`env/${r.host.id}`);
    } else {
      const isRepos = ui.addKind === "repos";
      const body = isRepos
        ? { repos: $("#add-repos").value, label: $("#add-repos-label").value, include_prereleases: $("#add-repos-pre").checked }
        : { images: $("#add-images").value, label: $("#add-images-label").value };
      const r = await api("POST", `/api/${ui.addKind}`, body);
      if (!r.added.length && !r.skipped.length) { addMsg("Paste at least one line.", false); return; }
      const ta = $(isRepos ? "#add-repos" : "#add-images");
      if (r.skipped.length) {
        addMsg(`Added ${r.added.length}. Not added:` + r.skipped.map((x) => `<br>• <code>${esc(x.value)}</code>: ${esc(x.reason)}`).join(""), r.added.length > 0);
        ta.value = r.skipped.map((x) => x.value).join("\n");
      } else {
        ta.value = "";
        $("#dlg-add").close();
        toast(`Added ${r.added.length}. Checking now…`);
        go(isRepos ? "repos" : "images");
      }
      if (r.added.length) $(isRepos ? "#add-repos-label" : "#add-images-label").value = "";
    }
    soon();
  } catch (e) {
    addMsg(esc(e.message), false);
  } finally {
    btn.disabled = false;
  }
}
async function testHost() {
  const out = $("#host-test");
  out.className = "small";
  out.textContent = "Connecting…";
  try {
    const r = await api("POST", "/api/hosts/test", { url: $("#add-host-url").value });
    out.className = "small msg-ok";
    out.textContent = `Connected to ${r.url}: ${plural(r.containers, "running container")}.`;
  } catch (e) { out.className = "small msg-bad"; out.textContent = e.message; }
}

// ---------- settings ----------
async function openSettings() {
  const msg = $("#settings-msg");
  msg.textContent = "";
  $("#notify-test").textContent = "";
  $("#portainer-test").textContent = "";
  try { fillSettings(await api("GET", "/api/settings")); }
  catch (e) { msg.textContent = e.message; msg.className = "small msg-bad"; }
  $("#dlg-settings").showModal();
}
function fillSettings(s) {
  for (const [k, v] of Object.entries(s)) {
    const el = $(`#s-${k}`);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v;
    else if (el.hasAttribute("data-secret")) {
      el.value = "";
      el.placeholder = v.set ? `Saved${v.hint ? ` (${v.hint})` : ""}. Leave blank to keep` : (el.dataset.ph ||= el.placeholder);
      renderClear(el, v.set);
    } else el.value = v;
  }
}
function renderClear(el, isSet) {
  let b = el.parentElement.querySelector(".clear-secret");
  if (!isSet) { b?.remove(); return; }
  if (!b) {
    b = document.createElement("button");
    b.type = "button";
    b.className = "btn tiny ghost clear-secret";
    b.textContent = "Remove saved value";
    b.style.alignSelf = "flex-start";
    b.addEventListener("click", () => { el.dataset.clear = "1"; el.value = ""; el.placeholder = "Will be removed when you save"; b.remove(); });
    el.after(b);
  }
}
function readSettings() {
  const out = {};
  for (const el of $$("#form-settings [id^='s-']")) {
    const k = el.id.slice(2);
    if (el.type === "checkbox") out[k] = el.checked;
    else if (el.hasAttribute("data-secret")) out[k] = el.dataset.clear === "1" ? null : el.value.trim();
    else out[k] = el.value.trim();
  }
  return out;
}
async function saveSettings(close = true) {
  const msg = $("#settings-msg");
  try {
    const s = await api("PUT", "/api/settings", readSettings());
    for (const el of $$("#form-settings [data-secret]")) delete el.dataset.clear;
    fillSettings(s);
    if (close) { $("#dlg-settings").close(); toast("Settings saved"); }
    soon();
    return true;
  } catch (e) { msg.textContent = e.message; msg.className = "small msg-bad"; return false; }
}
async function testNotify() {
  const out = $("#notify-test");
  out.className = "small";
  out.textContent = "Saving and sending…";
  if (!(await saveSettings(false))) { out.textContent = ""; return; }
  try {
    const r = await api("POST", "/api/test-notify");
    const bad = r.results.filter((x) => !x.ok);
    out.className = "small " + (bad.length ? "msg-bad" : "msg-ok");
    out.textContent = r.results.map((x) => x.ok ? `${x.name}: sent ✓` : `${x.name}: ${x.error}`).join(" · ");
  } catch (e) { out.className = "small msg-bad"; out.textContent = e.message; }
}
async function testPortainer() {
  const out = $("#portainer-test");
  out.className = "small";
  out.textContent = "Saving and connecting…";
  if (!(await saveSettings(false))) { out.textContent = ""; return; }
  try {
    const r = await api("POST", "/api/portainer/test");
    out.innerHTML = `<span class="msg-ok">Connected: ${plural(r.environments, "Docker environment")}, ${plural(r.stacks, "stack")}, ${plural(r.containers, "container")}.</span>`
      + ((r.details || []).length ? `<ul class="steps">${r.details.map((d) => d.error
        ? `<li class="msg-bad">${esc(d.name)}: ${esc(d.error)}</li>`
        : `<li>${esc(d.name)}: ${plural(d.containers, "container")}</li>`).join("")}</ul>` : "");
  } catch (e) { out.className = "small msg-bad"; out.textContent = e.message; }
}

// ---------- update through Portainer ----------
const upd = { host: null, ref: null };
async function openUpdate(host, ref) {
  upd.host = host; upd.ref = ref;
  const body = $("#update-body"), btn = $("#btn-update-go");
  $("#update-title").textContent = `Update ${ref}`;
  body.innerHTML = `<p class="muted">Asking Portainer what runs this image…</p>`;
  btn.disabled = true; btn.hidden = false;
  $("#dlg-update").showModal();
  try {
    const r = await api("POST", `/api/hosts/${encodeURIComponent(host)}/update`, { ref });
    const p = r.plan;
    body.innerHTML = (p.steps.length ? `<p>Portainer will:</p>
        <ul class="steps">${p.steps.map((s) => `<li>${esc(s.text)}${s.self ? ` <span class="flag major">restarts Lookout</span>` : ""}</li>`).join("")}</ul>
        <p class="muted small">The newest images are pulled and the containers restart, so they'll be down for a moment. In a stack, other services restart only if their image also changed. Settings, volumes and the stack file stay as they are.</p>`
      : `<p>Nothing here can be updated through Portainer.</p>`)
      + (p.skipped.length ? `<p class="small"><strong>Not included:</strong></p><ul class="steps small">${p.skipped.map((s) => `<li>${esc(s.name)}: ${esc(s.why)}</li>`).join("")}</ul>` : "");
    btn.disabled = !p.steps.length;
    btn.hidden = !p.steps.length;
  } catch (e) {
    body.innerHTML = `<p class="msg-bad">${esc(e.message)}</p>`;
    btn.hidden = true;
  }
}
async function runUpdate() {
  const body = $("#update-body"), btn = $("#btn-update-go");
  btn.disabled = true;
  btn.textContent = "Updating…";
  try {
    const r = await api("POST", `/api/hosts/${encodeURIComponent(upd.host)}/update`, { ref: upd.ref, confirm: true });
    body.innerHTML = `<ul class="steps done">${r.results.map((x) => x.ok
      ? `<li class="msg-ok">✓ ${esc(x.target)}${x.note ? ` <span class="muted">· ${esc(x.note)}</span>` : ""}</li>`
      : `<li class="msg-bad">✕ ${esc(x.target)}: ${esc(x.error)}</li>`).join("")}</ul>
      <p class="muted small">Lookout is checking this environment again. Portainer may take a minute to finish.</p>`;
    btn.hidden = true;
    soon();
  } catch (e) {
    body.innerHTML = `<p class="msg-bad">${esc(e.message)}</p><p class="muted small">If this update included Lookout itself, it's restarting. The page will reconnect on its own.</p>`;
    soon();
  } finally { btn.textContent = "Update"; }
}

// ---------- events ----------
function openSide() { $("#side").classList.add("open"); $("#side-scrim").classList.add("show"); }
function closeSide() { $("#side").classList.remove("open"); $("#side-scrim").classList.remove("show"); }

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button, a, [data-open]");
  if (!t) return;
  if (t.matches("[data-open-settings]")) { ev.preventDefault(); closeSide(); return openSettings(); }
  if (t.dataset.add) { ev.preventDefault(); return openAdd(t.dataset.add); }
  if (t.dataset.update) return openUpdate(t.dataset.update, t.dataset.ref);
  if (t.dataset.check) return checkNow([t.dataset.check]);
  if (t.dataset.filter) { ui.envFilter = t.dataset.filter; localSet("envFilter", ui.envFilter); return render(); }
  if (t.dataset.jump) { document.getElementById(t.dataset.jump)?.scrollIntoView({ behavior: "smooth", block: "start" }); return; }
  if (t.dataset.ack) {
    const [kind, id] = t.dataset.ack.split(":");
    t.disabled = true;
    try { await api("POST", `/api/${kind}/${id}/ack`); toast("Marked as updated"); refresh(); }
    catch (e) { toast(e.message); t.disabled = false; }
    return;
  }
  if (t.dataset.remove) {
    const [kind, id] = t.dataset.remove.split(":");
    const what = kind === "hosts" ? `the host “${t.dataset.name}” and stop watching its containers` : `“${t.dataset.name}”`;
    if (!confirm(`Remove ${what}?`)) return;
    try {
      await api("DELETE", `/api/${kind}/${id}`);
      toast("Removed");
      closeDrawer();
      if (kind === "hosts") go("overview");
      refresh();
    } catch (e) { toast(e.message); }
    return;
  }
  if (t.dataset.open) return openDrawer(t.dataset.open);
  if (t.classList.contains("nav-item")) closeSide();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && ui.drawerKey && !$("dialog[open]")) closeDrawer();
  if (e.key === "Enter" && e.target.dataset && e.target.dataset.open) openDrawer(e.target.dataset.open);
  if (e.key === "/" && !e.target.closest("input, textarea, dialog")) {
    e.preventDefault();
    ($("#search").offsetParent ? $("#search") : $(".mobile-search"))?.focus();
  }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "search" || e.target.classList.contains("mobile-search")) {
    ui.search = e.target.value.trim();
    clearTimeout(document.searchTimer);
    document.searchTimer = setTimeout(render, e.target.id === "search" ? 0 : 150);
  }
});

$("#btn-menu").addEventListener("click", openSide);
$("#side-scrim").addEventListener("click", closeSide);
$("#drawer-scrim").addEventListener("click", closeDrawer);
$("#btn-drawer-close").addEventListener("click", closeDrawer);
$("#btn-check").addEventListener("click", () => checkNow());
$("#btn-add").addEventListener("click", () => openAdd(ui.route === "images" ? "images" : ui.route.startsWith("env/") ? "hosts" : "repos"));
$("#btn-settings").addEventListener("click", () => { closeSide(); openSettings(); });
$("#form-add").addEventListener("submit", (e) => { e.preventDefault(); submitAdd(); });
$("#form-settings").addEventListener("submit", (e) => { e.preventDefault(); saveSettings(true); });
for (const b of $$("[data-close]")) b.addEventListener("click", () => b.closest("dialog").close());
$("#btn-test-host").addEventListener("click", testHost);
$("#btn-test-notify").addEventListener("click", testNotify);
$("#btn-test-portainer").addEventListener("click", testPortainer);
$("#btn-update-go").addEventListener("click", runUpdate);
$("#btn-copy-proxy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#proxy-snippet").textContent); toast("Copied"); }
  catch { toast("Select the text and copy it"); }
});
for (const b of $$("#add-kind button")) b.addEventListener("click", () => setAddKind(b.dataset.kind));
for (const id of ["#add-repos", "#add-images"]) $(id).addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submitAdd(); });
window.addEventListener("hashchange", () => { route(); closeDrawer(); closeSide(); render(); window.scrollTo(0, 0); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

route();
refresh();
