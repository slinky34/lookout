"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ui = {
  tab: localGet("tab") || "all",
  search: "",
  onlyUpdates: localGet("onlyUpdates") === "1",
  addKind: "repos",
  status: null,
  fastUntil: 0,
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
function shortDigest(d) { return d ? d.replace("sha256:", "").slice(0, 12) : ""; }

// ---------- model helpers ----------
const STATUS_LABEL = { update: "Update", ok: "Up to date", error: "Problem", skipped: "Skipped", pending: "Checking", none: "No releases" };
const pill = (st) => `<span class="pill ${esc(st)}">${STATUS_LABEL[st] || esc(st)}</span>`;
const link = (url, text) => url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text)}</a>` : esc(text);
const tagOf = (ref) => { const last = ref.split("/").pop(); return last.includes(":") ? last.split(":").pop() : "latest"; };

function containerInfo(img) {
  if (img.status === "error" || img.status === "skipped") return esc(img.reason || "");
  const bits = [];
  if (img.digest_update) bits.push(`New image for <code>:${esc(tagOf(img.ref))}</code>`);
  if (img.newest_tag) bits.push(`Newer version <code>${esc(img.newest_tag)}</code>`);
  return bits.join(" · ") || `<span class="muted">Matches the registry</span>`;
}
// How each container is run: Portainer stack (total control), compose outside
// Portainer (limited), or a standalone container.
function managers(img) {
  const seen = new Map();
  for (const m of img.members || []) {
    const g = m.manager || { type: m.project ? "compose" : "container", stack: m.project, control: null };
    const key = `${g.type}:${g.stack || ""}`;
    if (!seen.has(key)) seen.set(key, { ...g, names: [] });
    seen.get(key).names.push(m.name);
  }
  return [...seen.values()];
}
function managerBadges(img) {
  const list = managers(img);
  if (!list.length) return `<span class="muted">—</span>`;
  return list.map((g) => {
    const tip = esc(g.why || "");
    if (g.type === "stack") return `<span class="mgr total" title="Portainer stack · total control">Stack · ${esc(g.stack)}<em>Total</em></span>`;
    if (g.type === "swarm") return `<span class="mgr limited" title="${tip}">Swarm · ${esc(g.stack)}</span>`;
    if (g.type === "compose") return `<span class="mgr limited" title="${tip || "Compose project"}">Compose · ${esc(g.stack)}<em>${g.control === "limited" ? "Limited" : "Compose"}</em></span>`;
    return `<span class="mgr standalone" title="${tip}">Standalone</span>`;
  }).join("");
}
function updateAction(img, host) {
  const s = ui.status;
  if (img.status !== "update") return "";
  const p = s.portainer || {};
  if (!img.digest_update && img.newest_tag) return `<span class="hint-inline">Change the tag to <code>${esc(img.newest_tag)}</code> to update</span>`;
  if (!p.connected || !p.allow_updates) return "";
  if (!img.can_update) return `<span class="hint-inline" title="${esc((managers(img)[0] || {}).why || "")}">Update outside Portainer</span>`;
  return `<button class="btn tiny primary" data-update="${esc(host.id)}" data-ref="${esc(img.ref)}">Update</button>`;
}
function imageInfo(i) {
  if (i.status === "error") return esc(i.error);
  if (i.status === "pending") return `<span class="muted">Getting the current image…</span>`;
  const bits = [];
  if (i.latest_digest && i.installed_digest !== i.latest_digest) bits.push(`New image for <code>:${esc(tagOf(i.ref))}</code>`);
  if (i.newest_tag) bits.push(`Newer version <code>${esc(i.newest_tag)}</code>`);
  return bits.join(" · ") || `<span class="muted">No change since you added it (${esc(shortDigest(i.installed_digest))})</span>`;
}
function repoInfo(r) {
  if (r.status === "error") return esc(r.error);
  if (r.status === "pending") return `<span class="muted">Looking up releases…</span>`;
  if (r.status === "none") return `<span class="muted">This repo hasn't published a release</span>`;
  if (r.status === "update") return `<code>${esc(r.installed)}</code> → <code>${esc(r.latest)}</code>`;
  return `<code>${esc(r.latest)}</code>${r.published_at ? ` <span class="muted">· released ${esc(new Date(r.published_at).toLocaleDateString())}</span>` : ""}`;
}

function collectUpdates(s) {
  const out = [];
  for (const h of s.hosts) for (const img of h.images) if (img.status === "update") {
    const action = updateAction(img, h);
    out.push({ kind: "Container", name: img.ref, what: containerInfo(img), url: img.url,
      where: `${img.containers.join(", ")}${s.hosts.length > 1 ? ` on ${h.name}` : ""}`,
      badges: managerBadges(img), action,
      hint: action ? "" : "Pull the new image and recreate the container to update." });
  }
  for (const i of s.images) if (i.status === "update") {
    out.push({ kind: "Image", name: i.label || i.ref, what: imageInfo(i), url: i.url,
      where: i.label ? i.ref : "", ack: ["images", i.id] });
  }
  for (const r of s.repos) if (r.status === "update") {
    out.push({ kind: "GitHub", name: r.label || r.repo, what: repoInfo(r), url: r.url,
      where: r.label ? r.repo : "", ack: ["repos", r.id] });
  }
  return out;
}

// Everything counted in the Problems tile, with where it is and what to do.
function fixFor(msg) {
  const m = (msg || "").toLowerCase();
  if (m.includes("socket not found")) return `Add <code>/var/run/docker.sock:/var/run/docker.sock:ro</code> to Lookout's volumes, or ignore this if Portainer covers this server.`;
  if (m.includes("connection refused") || m.includes("no answer") || m.includes("can't reach docker")) return "Check the machine is on and the socket proxy container is running.";
  if (m.includes("needs a login") || m.includes("private images need credentials")) return "This image is private. Add a registry login in config.yaml, or remove it from the watch list.";
  if (m.includes("not found on")) return "The registry doesn't have this image or tag. It may have been renamed or removed.";
  if (m.includes("rate limit")) return `Add a token in <a href="#" data-open-settings>Settings</a>, or check less often.`;
  if (m.includes("rejected the access token") || m.includes("portainer")) return `Check the Portainer address and token in <a href="#" data-open-settings>Settings</a>.`;
  if (m.includes("doesn't exist or is private")) return "Check the spelling, or add a GitHub token for private repos.";
  return "";
}
function collectProblems(s) {
  const out = [];
  if (s.portainer && s.portainer.error) {
    out.push({ kind: "Portainer", where: "Portainer connection", msg: s.portainer.error, fix: fixFor("portainer") });
  }
  for (const h of s.hosts) {
    const check = `<button class="btn tiny" data-check="${esc(h.id)}">Check again</button>`;
    if (h.ok === false) {
      out.push({ kind: "Host", where: h.name, sub: h.source === "portainer" ? "Portainer environment" : (h.url || "local Docker"),
        msg: h.error, fix: fixFor(h.error), actions: check });
    }
    for (const i of h.images) if (i.status === "error") {
      out.push({ kind: "Container", where: i.ref, sub: `${i.containers.join(", ")} on ${h.name}`, msg: i.reason, fix: fixFor(i.reason), actions: check });
    }
  }
  for (const i of s.images) if (i.status === "error") {
    out.push({ kind: "Image", where: i.label || i.ref, sub: i.label ? i.ref : "", msg: i.error, fix: fixFor(i.error),
      actions: `<button class="btn tiny" data-check="${esc(i.id)}">Check again</button><button class="btn tiny danger" data-remove="images:${esc(i.id)}" data-name="${esc(i.label || i.ref)}">Remove</button>` });
  }
  for (const r of s.repos) if (r.status === "error") {
    out.push({ kind: "GitHub", where: r.label || r.repo, sub: r.label ? r.repo : "", msg: r.error, fix: fixFor(r.error),
      actions: `<button class="btn tiny" data-check="${esc(r.id)}">Check again</button><button class="btn tiny danger" data-remove="repos:${esc(r.id)}" data-name="${esc(r.label || r.repo)}">Remove</button>` });
  }
  return out;
}

// ---------- render ----------
function render() {
  const s = ui.status;
  if (!s) return;

  // header line
  const btn = $("#btn-check");
  btn.classList.toggle("spin", s.checking);
  btn.disabled = s.checking;
  $("#btn-check span").textContent = s.checking ? "Checking…" : "Check now";
  $("#run-status").textContent = s.checking ? "Checking for updates…"
    : `Last check ${ago(s.last_run)} · next ${until(s.next_run)} · every ${s.interval}`;

  // banner
  $("#banner").innerHTML = s.notifiers.length ? "" : `
    <div class="banner"><div><strong>Notifications aren't set up.</strong>
      <span class="muted">Updates show here, but nothing gets sent until you add Discord, ntfy or Gotify.</span></div>
      <button class="btn" data-open-settings>Set up notifications</button></div>`;

  // tiles
  const c = s.counts;
  $("#tiles").innerHTML = `
    <button type="button" class="tile ${c.updates ? "hot" : ""}" ${c.updates ? `data-jump="updates-section"` : "disabled"}><div class="label">Updates available</div><div class="value">${c.updates}</div>${c.updates ? `<div class="tile-link">Show ↓</div>` : ""}</button>
    <div class="tile"><div class="label">Watching</div><div class="value">${c.watching}</div></div>
    <button type="button" class="tile ${c.errors ? "bad" : ""}" ${c.errors ? `data-jump="problems-section"` : "disabled"}><div class="label">Problems</div><div class="value">${c.errors}</div>${c.errors ? `<div class="tile-link">Show what's wrong ↓</div>` : ""}</button>
    <div class="tile"><div class="label">Notifications</div><div class="value sm">${s.notifiers.length ? esc(s.notifiers.join(" · ")) : `<span class="muted">Not set up</span>`}</div></div>`;

  // problems
  const probs = collectProblems(s);
  $("#problems-section").hidden = !probs.length;
  $("#problems").innerHTML = probs.map((p) => `
    <li class="problem">
      <span class="pill error">${esc(p.kind)}</span>
      <div class="main"><div class="title">${esc(p.where)}</div>${p.sub ? `<div class="sub">${esc(p.sub)}</div>` : ""}</div>
      <div class="info">${esc(p.msg)}${p.fix ? `<div class="sub">${p.fix}</div>` : ""}</div>
      <div class="acts">${p.actions || ""}</div>
    </li>`).join("");

  // updates
  const ups = collectUpdates(s);
  $("#updates-section").hidden = !ups.length;
  $("#updates").innerHTML = ups.map((u) => `
    <div class="card">
      <div class="card-top"><div class="name">${link(u.url, u.name)}</div><span class="kind">${u.kind}</span></div>
      <div class="what">${u.what}</div>
      ${u.where ? `<div class="where">${esc(u.where)}</div>` : ""}
      ${u.badges ? `<div class="badges">${u.badges}</div>` : ""}
      ${u.hint ? `<div class="where">${esc(u.hint)}</div>` : ""}
      ${u.action ? `<div class="acts">${u.action}</div>` : ""}
      ${u.ack ? `<div class="acts"><button class="btn tiny" data-ack="${u.ack[0]}:${u.ack[1]}">I've updated it</button></div>` : ""}
    </div>`).join("");

  // tab counts
  const nCont = s.hosts.reduce((n, h) => n + h.images.length, 0);
  const counts = { all: c.watching, containers: nCont, images: s.images.length, repos: s.repos.length };
  for (const b of $$("#tabs button")) {
    const t = b.dataset.tab;
    b.setAttribute("aria-selected", String(t === ui.tab));
    b.innerHTML = `${{ all: "All", containers: "Containers", images: "Images", repos: "GitHub" }[t]}<span class="count">${counts[t]}</span>`;
  }

  // lists
  const q = ui.search.toLowerCase();
  const keep = (status, ...text) => (!ui.onlyUpdates || status === "update" || status === "error")
    && (!q || text.join(" ").toLowerCase().includes(q));
  const show = (t) => ui.tab === "all" || ui.tab === t;
  let html = "";

  if (show("containers")) {
    for (const h of s.hosts) {
      const rows = h.images.filter((i) => keep(i.status, i.ref, ...i.containers));
      const meta = h.checked_at ? `Checked ${ago(h.checked_at)}` : "Not checked yet";
      const where = h.source === "portainer" ? "Portainer environment"
        : (h.url ? esc(h.url) : "local Docker") + (h.portainer_env ? ` · Portainer “${esc(h.portainer_env)}”` : "");
      html += `<div class="panel">
        <div class="panel-head">
          <h3>${hostIcon()} ${esc(h.name)} <span class="muted small">${where}</span></h3>
          <div class="row gap"><span class="meta">${meta} · ${h.images.length} image${h.images.length === 1 ? "" : "s"}</span>
            <button class="btn tiny" data-check="${esc(h.id)}">Check</button>
            ${h.removable ? `<button class="btn tiny danger" data-remove="hosts:${esc(h.id)}" data-name="${esc(h.name)}">Remove</button>` : ""}</div>
        </div>
        ${h.ok === false ? `<div class="panel-error">${esc(h.error)}</div>` : ""}
        ${rows.length ? `<div class="rows-head wide"><span>Status</span><span>Image · containers</span><span>Managed by</span><span>Details</span><span></span></div>
          <ul class="rows wide">${rows.map((i) => `
          <li>${pill(i.status)}
            <div class="main"><div class="title">${link(i.url, i.ref)}</div><div class="sub">${esc(i.containers.join(", "))}</div></div>
            <div class="mgrs">${managerBadges(i)}</div>
            <div class="info">${containerInfo(i)}</div>
            <div class="acts">${updateAction(i, h)}</div></li>`).join("")}</ul>`
          : `<div class="empty">${h.images.length ? "Nothing matches the filter." : h.ok === false ? "Can't see this host's containers right now." : h.checked_at ? "No running containers to watch." : "Waiting for the first check…"}</div>`}
        ${h.ignored.length ? `<div class="ignored">Ignored by label or settings: ${esc(h.ignored.join(", "))}</div>` : ""}
      </div>`;
    }
    if (!(s.portainer || {}).connected) {
      html += `<p class="muted small connect-hint">Use Portainer? <a href="#" data-open-settings>Connect it</a> to watch every environment, see which stacks Portainer fully controls, and update them from here.</p>`;
    } else if ((s.portainer || {}).error) {
      html += `<p class="small msg-bad">Portainer: ${esc(s.portainer.error)}</p>`;
    }
    if (ui.tab === "containers") html += `<p class="muted small">Services on a machine outside Portainer? <a href="#" data-add="hosts">Add a Docker host</a> to watch all of its containers.</p>`;
  }

  if (show("images")) {
    const rows = s.images.filter((i) => keep(i.status, i.ref, i.label || ""));
    html += listPanel("Images", "Images for services Lookout can't see directly.", "images", rows, s.images.length, (i) => `
      <li>${pill(i.status)}
        <div class="main"><div class="title">${link(i.url, i.label || i.ref)}</div><div class="sub">${i.label ? esc(i.ref) + " · " : ""}checked ${ago(i.checked_at)}</div></div>
        <div class="info">${imageInfo(i)}</div>
        <div class="acts">${i.status === "update" ? `<button class="btn tiny" data-ack="images:${esc(i.id)}">I've updated it</button>` : ""}
          <button class="btn tiny danger" data-remove="images:${esc(i.id)}" data-name="${esc(i.label || i.ref)}">Remove</button></div></li>`);
  }

  if (show("repos")) {
    const rows = s.repos.filter((r) => keep(r.status, r.repo, r.label || ""));
    html += listPanel("GitHub releases", "Apps released on GitHub.", "repos", rows, s.repos.length, (r) => `
      <li>${pill(r.status)}
        <div class="main"><div class="title">${link(r.url || `https://github.com/${r.repo}`, r.label || r.repo)}</div>
          <div class="sub">${r.label ? esc(r.repo) + " · " : ""}${r.include_prereleases ? "incl. pre-releases · " : ""}checked ${ago(r.checked_at)}</div></div>
        <div class="info">${repoInfo(r)}</div>
        <div class="acts">${r.status === "update" ? `<button class="btn tiny" data-ack="repos:${esc(r.id)}">I've updated it</button>` : ""}
          <button class="btn tiny danger" data-remove="repos:${esc(r.id)}" data-name="${esc(r.label || r.repo)}">Remove</button></div></li>`);
  }

  $("#lists").innerHTML = html;
  $("#foot").textContent = `Lookout ${s.version}`;
}

function listPanel(title, blurb, kind, rows, total, row) {
  return `<div class="panel">
    <div class="panel-head"><h3>${title} <span class="muted small">${esc(blurb)}</span></h3>
      <button class="btn tiny" data-add="${kind}">Add</button></div>
    ${rows.length ? `<ul class="rows">${rows.map(row).join("")}</ul>`
      : `<div class="empty">${total ? "Nothing matches the filter." : `Nothing here yet.<br><button class="btn" data-add="${kind}">Add ${kind === "repos" ? "GitHub repos" : "images"}</button>`}</div>`}
  </div>`;
}
function hostIcon() {
  return `<svg class="ico" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>`;
}

// ---------- data loop ----------
async function refresh() {
  try {
    ui.status = await api("GET", "/api/status");
    render();
  } catch (e) {
    $("#run-status").textContent = `Can't reach Lookout: ${e.message}`;
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
  $("#add-msg").textContent = "";
  $("#add-msg").className = "small";
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
function addMsg(text, ok) {
  const m = $("#add-msg");
  m.innerHTML = text;
  m.className = "small " + (ok ? "msg-ok" : "msg-bad");
}
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
    } else {
      const isRepos = ui.addKind === "repos";
      const body = isRepos
        ? { repos: $("#add-repos").value, label: $("#add-repos-label").value, include_prereleases: $("#add-repos-pre").checked }
        : { images: $("#add-images").value, label: $("#add-images-label").value };
      const r = await api("POST", `/api/${ui.addKind}`, body);
      if (!r.added.length && !r.skipped.length) { addMsg("Paste at least one line.", false); return; }
      const skipped = r.skipped.map((s) => `<br>• <code>${esc(s.value)}</code>: ${esc(s.reason)}`).join("");
      if (r.skipped.length) {
        addMsg(`Added ${r.added.length}. Not added:${skipped}`, r.added.length > 0);
        if (isRepos) $("#add-repos").value = r.skipped.map((s) => s.value).join("\n");
        else $("#add-images").value = r.skipped.map((s) => s.value).join("\n");
      } else {
        $("#dlg-add").close();
        toast(`Added ${r.added.length}. Checking now…`);
      }
      if (r.added.length) {
        $(isRepos ? "#add-repos-label" : "#add-images-label").value = "";
        if (!r.skipped.length) $(isRepos ? "#add-repos" : "#add-images").value = "";
      }
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
    out.textContent = `Connected to ${r.url}: ${r.containers} running container${r.containers === 1 ? "" : "s"}.`;
  } catch (e) {
    out.className = "small msg-bad";
    out.textContent = e.message;
  }
}

// ---------- settings ----------
async function openSettings() {
  const msg = $("#settings-msg");
  msg.textContent = "";
  $("#notify-test").textContent = "";
  $("#portainer-test").textContent = "";
  try {
    fillSettings(await api("GET", "/api/settings"));
  } catch (e) { msg.textContent = e.message; msg.className = "small msg-bad"; }
  $("#dlg-settings").showModal();
}
function fillSettings(s) {
  for (const [k, v] of Object.entries(s)) {
    const el = $(`#s-${k}`);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v;
    else if (el.hasAttribute("data-secret")) {
      el.value = "";
      el.dataset.set = v.set ? "1" : "";
      el.placeholder = v.set ? `Saved${v.hint ? ` (${v.hint})` : ""}. Leave blank to keep, or type to replace` : (el.dataset.ph ||= el.placeholder);
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
  } catch (e) {
    msg.textContent = e.message;
    msg.className = "small msg-bad";
    return false;
  }
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
  } catch (e) {
    out.className = "small msg-bad";
    out.textContent = e.message;
  }
}

// ---------- update through Portainer ----------
const upd = { host: null, ref: null };
async function openUpdate(host, ref) {
  upd.host = host; upd.ref = ref;
  const body = $("#update-body");
  const go = $("#btn-update-go");
  $("#update-title").textContent = `Update ${ref}`;
  body.innerHTML = `<p class="muted">Asking Portainer what runs this image…</p>`;
  go.disabled = true;
  go.hidden = false;
  $("#dlg-update").showModal();
  try {
    const r = await api("POST", `/api/hosts/${encodeURIComponent(host)}/update`, { ref });
    const p = r.plan;
    body.innerHTML = (p.steps.length ? `
        <p>Portainer will:</p>
        <ul class="steps">${p.steps.map((s) => `<li>${esc(s.text)}${s.self ? ` <span class="pill update">restarts Lookout</span>` : ""}</li>`).join("")}</ul>
        <p class="muted small">The newest images are pulled and the containers restart, so they'll be down for a moment. In a stack, other services restart only if their image also changed. Settings, volumes and the stack file stay as they are.</p>`
      : `<p>Nothing here can be updated through Portainer.</p>`)
      + (p.skipped.length ? `<p class="small"><strong>Not included:</strong></p><ul class="steps small">${p.skipped.map((s) => `<li>${esc(s.name)}: ${esc(s.why)}</li>`).join("")}</ul>` : "");
    go.disabled = !p.steps.length;
    go.hidden = !p.steps.length;
  } catch (e) {
    body.innerHTML = `<p class="msg-bad">${esc(e.message)}</p>`;
    go.hidden = true;
  }
}
async function runUpdate() {
  const body = $("#update-body");
  const go = $("#btn-update-go");
  go.disabled = true;
  go.textContent = "Updating…";
  try {
    const r = await api("POST", `/api/hosts/${encodeURIComponent(upd.host)}/update`, { ref: upd.ref, confirm: true });
    body.innerHTML = `<ul class="steps done">${r.results.map((x) => x.ok
      ? `<li class="msg-ok">✓ ${esc(x.target)}${x.note ? ` · ${esc(x.note)}` : ""}</li>`
      : `<li class="msg-bad">✕ ${esc(x.target)}: ${esc(x.error)}</li>`).join("")}</ul>
      <p class="muted small">Checking again to confirm…</p>`;
    go.hidden = true;
    soon();
  } catch (e) {
    // Updating Lookout's own stack can drop the connection while it restarts.
    body.innerHTML = `<p class="msg-bad">${esc(e.message)}</p><p class="muted small">If this update included Lookout itself, it's restarting. The page will reconnect on its own.</p>`;
    soon();
  } finally {
    go.textContent = "Update";
  }
}

async function testPortainer() {
  const out = $("#portainer-test");
  out.className = "small";
  out.textContent = "Saving and connecting…";
  if (!(await saveSettings(false))) { out.textContent = ""; return; }
  try {
    const r = await api("POST", "/api/portainer/test");
    const bad = (r.details || []).some((d) => d.error);
    out.className = "small " + (bad ? "" : "msg-ok");
    out.innerHTML = `<span class="msg-ok">Connected: ${r.environments} Docker environment${r.environments === 1 ? "" : "s"}, ${r.stacks} stack${r.stacks === 1 ? "" : "s"}, ${r.containers} container${r.containers === 1 ? "" : "s"}.</span>`
      + ((r.details || []).length ? `<ul class="steps">${r.details.map((d) => d.error
        ? `<li class="msg-bad">${esc(d.name)}: ${esc(d.error)}</li>`
        : `<li>${esc(d.name)}: ${d.containers} container${d.containers === 1 ? "" : "s"}</li>`).join("")}</ul>` : "");
  } catch (e) {
    out.className = "small msg-bad";
    out.textContent = e.message;
  }
}

// ---------- events ----------
document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button, a");
  if (!t) return;
  if (t.matches("[data-open-settings]")) { ev.preventDefault(); return openSettings(); }
  if (t.dataset.update) return openUpdate(t.dataset.update, t.dataset.ref);
  if (t.dataset.jump) {
    const el = document.getElementById(t.dataset.jump);
    if (el) { el.scrollIntoView({ behavior: "smooth", block: "start" }); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1200); }
    return;
  }
  if (t.dataset.add) { ev.preventDefault(); return openAdd(t.dataset.add); }
  if (t.dataset.check) return checkNow([t.dataset.check]);
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
    try { await api("DELETE", `/api/${kind}/${id}`); toast("Removed"); refresh(); }
    catch (e) { toast(e.message); }
  }
});

$("#btn-check").addEventListener("click", () => checkNow());
$("#btn-add").addEventListener("click", () => openAdd(ui.tab === "images" ? "images" : ui.tab === "containers" ? "hosts" : "repos"));
$("#btn-settings").addEventListener("click", openSettings);
$("#form-add").addEventListener("submit", (e) => { e.preventDefault(); submitAdd(); });
$("#btn-test-host").addEventListener("click", testHost);
$("#form-settings").addEventListener("submit", (e) => { e.preventDefault(); saveSettings(true); });
for (const b of $$("[data-close]")) b.addEventListener("click", () => b.closest("dialog").close());
$("#btn-test-notify").addEventListener("click", testNotify);
$("#btn-test-portainer").addEventListener("click", testPortainer);
$("#btn-update-go").addEventListener("click", runUpdate);
$("#btn-copy-proxy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#proxy-snippet").textContent); toast("Copied"); }
  catch { toast("Select the text and copy it"); }
});
for (const b of $$("#add-kind button")) b.addEventListener("click", () => setAddKind(b.dataset.kind));
for (const b of $$("#tabs button")) b.addEventListener("click", () => { ui.tab = b.dataset.tab; localSet("tab", ui.tab); render(); });
$("#search").addEventListener("input", (e) => { ui.search = e.target.value; render(); });
$("#only-updates").checked = ui.onlyUpdates;
$("#only-updates").addEventListener("change", (e) => { ui.onlyUpdates = e.target.checked; localSet("onlyUpdates", e.target.checked ? "1" : "0"); render(); });
$("#add-repos").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submitAdd(); });
$("#add-images").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submitAdd(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

refresh();
