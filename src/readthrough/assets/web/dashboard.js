let DATA = /*__READTHROUGH_DATA__*/null;
const LIVE = /*__READTHROUGH_LIVE__*/null;
const LANES = {ready:"Ready", approved:"Approved", waiting:"Waiting on author", blocked:"Blocked", draft:"Draft", mine:"Mine"};
const AUTHORS = {internal:"Internal", external:"External", bot:"Bots"};
// CI states, in order of urgency. Pills group them; badges and the table show the exact state.
const CI = {
  failing: {group: "failing", icon: "✗", label: "failing (required)"},
  "not-run": {group: "pending", icon: "∅", label: "required checks haven't run"},
  "failing-optional": {group: "failing", icon: "!", label: "optional check failing"},
  "failing-main": {group: "failing", icon: "✗", label: "failing, but also on main"},
  pending: {group: "pending", icon: "…", label: "pending"},
  passing: {group: "passing", icon: "✓", label: "passing"},
  unknown: {group: "pending", icon: "?", label: "CI couldn't be read"},
  none: {group: "none", icon: "–", label: "no CI"},
};
const CI_GROUPS = {passing: "CI passing", failing: "Failing", pending: "Pending", none: "No CI"};
const ciState = p => p.ci_state || "none";
const ciRank = p => Object.keys(CI).indexOf(ciState(p));


const UNDERSTOOD = ["understood","reviewed","commented","approved","changes-requested"];
const REVIEWED = ["reviewed","commented","approved","changes-requested"];
const STORE = "readthrough:" + (DATA.slug || DATA.repo);
const FORGE = DATA.forge || {name: "github", label: "GitHub", pr_noun: "pull request", url: ""};
const SLUG = DATA.slug || `${FORGE.name}/${DATA.repo}`;
const GROUPS = {lanes: LANES, authors: AUTHORS, cis: CI_GROUPS};
const HASH = new URLSearchParams(location.hash.slice(1).split("?")[1] || "");
const fromHash = (key, all) => { const v = HASH.get(key); const keep = v ? v.split(",").filter(k => k in all) : []; return new Set(keep.length ? keep : Object.keys(all)); };
const state = {
  tab: location.hash.slice(1).split("?")[0] || "map", lanes: fromHash("lanes", LANES), authors: fromHash("authors", AUTHORS),
  cis: fromHash("ci", CI_GROUPS), greenFirst: localStorage.getItem("readthrough:green-first") !== "0",
  q: HASH.get("q") || "", comp: HASH.get("comp"), sel: null, dtab: "overview", srcOnly: true, overlapOnly: true,
  drafts: JSON.parse(sessionStorage.getItem(STORE + ":drafts") || "{}"),
};
const saveDrafts = () => sessionStorage.setItem(STORE + ":drafts", JSON.stringify(state.drafts));
const study = {};  // pr -> {entries, summary, engine, pending}
let byNum, comps, LINKS, progress;
const $ = id => document.getElementById(id);
const icon = (name, cls = "ico") => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${LUCIDE[name] || ""}</svg>`;
// The mark is a page being read: a highlight band steps down the lines, and two lines carry a note.
const LOGO = `<svg class="logo" viewBox="0 0 32 32" aria-hidden="true">
  <rect x="5.5" y="3.5" width="21" height="25" rx="3" fill="none" stroke="currentColor" stroke-opacity=".35"/>
  <g class="sweep"><rect x="7.5" y="7.25" width="17" height="5.5" rx="2.75" fill="currentColor" fill-opacity=".16"/></g>
  <g stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-opacity=".45"><line x1="10" y1="10" x2="21" y2="10"/><line x1="10" y1="14.5" x2="18" y2="14.5"/><line x1="10" y1="19" x2="21" y2="19"/><line x1="10" y1="23.5" x2="16" y2="23.5"/></g>
  <circle class="blip" cx="22.5" cy="14.5" r="1.8" fill="var(--ready)"/><circle class="blip b2" cx="20.5" cy="23.5" r="1.8" fill="var(--blocked)"/></svg>`;
// The tab icon is the same page, with a badge counting PRs that are waiting on you.
function favicon(count){
  const acc = css("--acc") || "#0969da", bad = css("--bad") || "#d1242f";
  const badge = count ? `<circle cx="47" cy="17" r="17" fill="${bad}"/><text x="47" y="24" text-anchor="middle" font-family="Segoe UI,Arial" font-weight="700" font-size="${count > 9 ? 18 : 22}" fill="#fff">${count > 99 ? "99+" : count}</text>` : "";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect x="8" y="4" width="46" height="56" rx="7" fill="${acc}"/><rect x="14" y="24" width="34" height="12" rx="6" fill="#fff" fill-opacity=".3"/><g stroke="#fff" stroke-width="4.5" stroke-linecap="round" stroke-opacity=".85"><path d="M19 17h24"/><path d="M19 30h18"/><path d="M19 43h24"/><path d="M19 52h12"/></g>${badge}</svg>`;
  $("favicon").href = "data:image/svg+xml," + encodeURIComponent(svg);
}
const EMPTY = msg => `<div class="empty-state"><svg viewBox="0 0 120 120" aria-hidden="true"><rect x="22" y="10" width="76" height="100" rx="9" fill="none" stroke="var(--line)" stroke-width="2"/><rect x="30" y="38" width="60" height="18" rx="9" fill="var(--acc)" fill-opacity=".12"/><g stroke="var(--line)" stroke-width="3" stroke-linecap="round" stroke-dasharray="4 5"><path d="M38 30h44"/><path d="M38 47h32"/><path d="M38 64h44"/><path d="M38 81h24"/></g><circle cx="38" cy="47" r="3.5" fill="var(--acc)"/><path d="M84 92 l6 6 m0 -6 l-6 6" stroke="var(--mut)" stroke-width="2" stroke-linecap="round"/></svg><div>${msg}</div></div>`;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const laneColor = l => `var(--${l})`;
const lines = p => p.additions + p.deletions;
const authorGroup = p => p.author_group || (p.author_kind === "first-time" ? "external" : (p.author_kind || "external"));
const isFirst = p => p.author_kind === "first-time";
const isExt = p => authorGroup(p) === "external";
const kindBadge = k => (PROFILE.author_kinds?.[k]?.badge) || k || "";

let PROFILE, NON_SOURCE = [];
const layerColor = id => {
  const layers = PROFILE.layers, layer = layers.find(l => l.id === id);
  if (layer?.color) return layer.color;
  if (layer && layer.source === false) return "var(--support)";
  const i = layers.filter(l => l.source !== false).findIndex(l => l.id === id);
  return `var(--l${Math.max(0, i) % 6})`;
};
function setData(d){
  DATA = d;
  PROFILE = DATA.profile;
  NON_SOURCE = (PROFILE.overlap_non_source || []).map(p => new RegExp(p));
  byNum = Object.fromEntries(DATA.prs.map(p => [p.number, p]));
  comps = Object.fromEntries(DATA.components.map(c => [c.id, c]));
  LINKS = DATA.links || {};
  loadProgress();
}
// Live mode: the ledger is the only source of truth. Snapshot: this browser's checkmarks plus the ledger.
function loadProgress(){
  progress = LIVE ? {} : JSON.parse(localStorage.getItem(STORE) || "{}");
  for (const [num, e] of Object.entries(DATA.ledger || {})) {
    const p = progress[num] ||= {};
    if (UNDERSTOOD.includes(e.status)) p.understood = true;
    if (REVIEWED.includes(e.status)) p.reviewed = true;
    p.head = e.head || p.head; p.note = e.note; p.status = e.status;
  }
}
setData(DATA);
if (state.comp && !comps[state.comp]) state.comp = null;
const save = () => { if (!LIVE) localStorage.setItem(STORE, JSON.stringify(progress)); };
const stale = n => progress[n]?.head && byNum[n] && progress[n].head !== byNum[n].head;

// ---- theme
const THEMES = ["auto", "light", "dark"];
let themePref = localStorage.getItem("readthrough:theme") || "auto";
const darkQuery = matchMedia("(prefers-color-scheme: dark)");
function applyTheme(){
  const theme = themePref === "auto" ? (darkQuery.matches ? "dark" : "light") : themePref;
  document.documentElement.dataset.theme = theme;
  $("theme").innerHTML = icon({auto: "sun-moon", light: "sun", dark: "moon"}[themePref]);
  $("theme").title = {auto: "Theme: follows your OS", light: "Theme: light", dark: "Theme: dark"}[themePref] + ". Click to change.";
  $("theme").setAttribute("aria-label", $("theme").title);
  favicon(needsYou());
  if (cy) styleGraph();
}
darkQuery.addEventListener("change", applyTheme);

// ---- markdown, always sanitized (PR text is written by strangers)
DOMPurify.addHook("afterSanitizeAttributes", node => {
  if (node.tagName === "A") { node.setAttribute("target", "_blank"); node.setAttribute("rel", "noopener noreferrer"); }
});
const PURIFY = {FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select", "iframe"], FORBID_ATTR: ["style"], ALLOW_DATA_ATTR: false};
const safeHtml = html => DOMPurify.sanitize(html || "", PURIFY);
const md = text => safeHtml(marked.parse(text || "", {gfm: true, breaks: false}));

function toast(msg){ const t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("on"), 2200); }
function copy(text, msg){ navigator.clipboard.writeText(text).then(() => toast(msg || "Copied")); }
const deepPrompt = n => `[readthrough:${SLUG}#${n}] Use the readthrough skill to run a deep review session for ${DATA.repo}#${n}. The study brief is at ${(LINKS.briefs_dir || "").replace(/\\/g, "/")}/pr-${n}.md`;
const reviewUrl = n => `readthrough://review/${SLUG}/${n}`;
// A session's editor link comes from the agent that owns it (Copilot sessions open in VS Code's agent window;
// Claude Code sessions have none), so the page never guesses one.
const vscodeUrl = n => {
  const s = (byNum[n].sessions || []).find(s => s.editor_url);
  if (s) return s.editor_url;
  return `vscode://agents/new?prompt=${encodeURIComponent(deepPrompt(n))}` + (LINKS.clone_uri ? `&workspace=${encodeURIComponent(LINKS.clone_uri)}` : "");
};
const ago = ts => { const h = (Date.now() - ts) / 3.6e6; return h < 1 ? `${Math.max(1, Math.round(h * 60))} min ago` : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} d ago`; };
const agentLabel = s => s.agent_label || "Copilot";
const sessDot = p => {
  const all = p.sessions || (p.session?.exists ? [p.session] : []);
  if (!all.length) return "";
  const live = all.filter(s => s.active).length, latest = Math.max(...all.map(s => s.updated || 0));
  const labels = [...new Set(all.map(agentLabel))].join(" and ");
  const what = all.length > 1 ? `${all.length} ${labels} sessions` : `A ${labels} review session`;
  return live ? `<span class="sess live" title="${what}; ${live} open right now"></span>`
              : `<span class="sess" title="${what}, last active ${ago(latest * 1000)}"></span>`;
};
// `active` is true, false, or null: null means the agent leaves no trace of whether the session is open.
const activity = s => s.active ? "<b>open now</b>" : s.active === false ? ago(s.updated * 1000) : `last written ${ago(s.updated * 1000)}`;
function sessionList(n){
  const all = byNum[n].sessions || [];
  if (!all.length) return `<div class="hint">No review session yet. Starting one keeps it for this PR, so you can come back to it. Sessions started from VS Code, Claude Code, or a copied prompt show up here too.</div>`;
  return `<div class="sessions">${all.map(s => `<div class="srow">${icon(s.where === "VS Code" ? "square-code" : "terminal", "ico sm")}
      <span class="sname" title="${esc(s.id)}">${esc(s.name || s.id.slice(0, 8))}</span>
      <span class="muted">${esc(agentLabel(s))} · ${esc(s.where)} · ${activity(s)}</span>
      ${LIVE ? `<button class="btn small" data-review="${n}" data-session="${s.id}" title="Resume this session in a new terminal">terminal</button>` : ""}
      ${s.editor_url ? `<a class="btn small" href="${esc(s.editor_url)}" title="Open this session in VS Code's agent window">VS Code</a>` : ""}</div>`).join("")}</div>`;
}
const whoBadge = p => {
  const k = p.author_kind || "external";
  const cls = isFirst(p) ? "first" : {internal: "int", external: "ext", bot: "bot"}[authorGroup(p)];
  return `<span class="who ${cls}" title="${esc(FORGE.label)} association: ${esc(p.author_association || "?")}">${esc(kindBadge(k))}</span>`;
};
const scoreBadge = p => {
  const s = p.study;
  if (!s || !(s.assessed || s.predictions || s.teachback)) return "";
  const q = s.assessed ? `quiz ${s.correct}/${s.assessed}` : "studying";
  return `<span class="score" title="Study log: ${s.correct} correct, ${s.partial} partial, ${s.incorrect} incorrect; ${s.predictions} predictions; teach-back ${s.teachback || "not yet"}">${q}</span>`;
};
function reviewButton(n, cls = "btn small"){
  const resume = byNum[n].session && byNum[n].session.exists;
  const label = resume ? "resume" : "review", title = `${resume ? "Resume" : "Start"} the deep-review session for #${n} in a terminal`;
  if (LIVE) return `<button class="${cls}" data-review="${n}" title="${title}">${label}</button>`;
  if (LINKS.protocol) return `<a class="${cls}" href="${reviewUrl(n)}" title="${title}">${label}</a>`;
  return `<button class="${cls}" data-copy="readthrough review ${n}" title="Copies the command. Run 'readthrough serve' (or 'readthrough install protocol') to make this one click.">${label}</button>`;
}
async function api(path, body, method = "POST"){
  const opts = method === "GET" ? {cache: "no-store"} : {method, headers: {"Content-Type": "application/json", "X-Readthrough-Token": LIVE.token}, body: JSON.stringify(body || {})};
  const resp = await fetch((LIVE.base || "") + path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || resp.statusText);
  return data;
}

// ---- filtering
function visible(p){
  if (!state.lanes.has(p.lane) || !state.authors.has(authorGroup(p)) || !state.cis.has(CI[ciState(p)].group)) return false;
  if (state.comp && !(state.comp in p.by_comp)) return false;
  const q = state.q.trim().toLowerCase();
  if (!q) return true;
  return q.split(/\s+/).every(t => {
    if (t.startsWith("@")) return p.author.toLowerCase().includes(t.slice(1));
    if (t.startsWith("#")) return String(p.number).startsWith(t.slice(1));
    if (t.startsWith("comp:")) return Object.keys(p.by_comp).some(c => c.startsWith(t.slice(5)));
    if (t.startsWith("flag:")) return [...p.flags, ...p.smells].some(f => f.id.startsWith(t.slice(5)));
    if (t.startsWith("is:")) return (p.author_kind || "").startsWith(t.slice(3)) || authorGroup(p).startsWith(t.slice(3));
    if (t.startsWith("ci:")) { const v = t.slice(3); return v === "main" ? ciState(p) === "failing-main" : ciState(p).startsWith(v) || CI[ciState(p)].group.startsWith(v); }
    return (p.title + " " + p.author + " " + p.files.map(f => f.path).join(" ")).toLowerCase().includes(t);
  });
}

// ---- header
function header(){
  document.title = `${PROFILE.title} Readthrough`;
  $("title").textContent = `${PROFILE.title} Readthrough`;
  $("repos").innerHTML = LIVE
    ? `<select id="repoSwitch" title="Switch repo">${LIVE.repos.map(r => `<option value="${esc(r.slug)}" ${r.slug === SLUG ? "selected" : ""}>${esc(r.name)}</option>`).join("")}<option value="__setup">+ Onboard a repo…</option></select>
       <a class="btn icon" href="/setup?repo=${encodeURIComponent(SLUG)}" title="Settings for ${esc(DATA.repo)}: account, clone, profile, authors" aria-label="Repo settings">${icon("settings")}</a>`
    : "";
  $("meta").textContent = `${DATA.repo} · ${DATA.prs.length} open · as of ${new Date(DATA.generated).toLocaleString()} · you: @${DATA.viewer}`;
  $("logo").innerHTML ||= LOGO;
  renderTabs();
  const count = fn => DATA.prs.reduce((m, p) => (m[fn(p)] = (m[fn(p)] || 0) + 1, m), {});
  const counts = {lanes: count(p => p.lane), authors: count(authorGroup), cis: count(p => CI[ciState(p)].group)};
  const ciDot = {passing: "var(--good)", failing: "var(--bad)", pending: "var(--warn)", none: "var(--draft)"};
  const dot = {lanes: laneColor, authors: k => k === "external" ? "var(--external)" : null, cis: k => ciDot[k]};
  const attr = {lanes: "lane", authors: "author", cis: "ci"};
  for (const [group, labels] of Object.entries(GROUPS)) {
    const set = state[group], all = set.size === Object.keys(labels).length;
    $(group).classList.toggle("filtered", !all);
    $(group).innerHTML = Object.entries(labels).filter(([k]) => counts[group][k]).map(([k, l]) => {
      const on = set.has(k), only = set.size === 1 && on, color = dot[group](k);
      const tip = only ? `Showing only ${l}. Click to show everything again.` : `Click: show only ${l}. Ctrl-click: ${on ? "hide" : "add"} ${l}.`
        + (k === "external" ? " Includes first-time contributors (hollow dot)." : "");
      return `<button class="lane ${on ? "inc" : "off"}" data-${attr[group]}="${k}" aria-pressed="${on}" title="${tip}">${color ? `<i style="background:${color}"></i>` : ""}${l} ${counts[group][k]}</button>`;
    }).join("");
  }
  const parts = Object.entries(GROUPS).filter(([g, labels]) => state[g].size !== Object.keys(labels).length)
    .map(([g, labels]) => [...state[g]].map(k => labels[k]).join(" + "));
  $("filtered").innerHTML = parts.length ? `<button class="clear" data-clear title="Show every lane, author, and CI state">${icon("filter-x")}Only ${esc(parts.join(" · "))} · show all</button>` : "";
  const ready = DATA.prs.filter(p => !["draft","mine"].includes(p.lane));
  const u = ready.filter(p => progress[p.number]?.understood && !stale(p.number)).length;
  const r = ready.filter(p => progress[p.number]?.reviewed && !stale(p.number)).length;
  $("progtext").textContent = `${r} reviewed · ${u} understood / ${ready.length}`;
  $("progbar").innerHTML = `<span style="width:${100*r/ready.length}%;background:var(--ready)"></span><span style="width:${100*(u-r)/ready.length}%;background:color-mix(in srgb,var(--ready) 40%,transparent)"></span>`;
  $("refresh").innerHTML = LIVE
    ? `${liveBadge()} <button class="btn icon" data-refresh title="Fetch the latest PRs now" aria-label="Refresh">${icon("refresh-cw")}${LIVE.status?.building ? "refreshing…" : ""}</button>`
    : LINKS.protocol ? `<a class="btn icon" href="readthrough://refresh/${SLUG}" title="Refresh">${icon("refresh-cw")}</a>`
    : `<button class="btn icon" data-copy="readthrough build --open" title="Copies the refresh command" aria-label="Copy refresh command">${icon("refresh-cw")}</button>`;
  syncHash();
  banners();
  favicon(needsYou());
}
function liveBadge(){
  const s = LIVE.status || {};
  if (LIVE.disconnected) return `<span class="live-badge off" title="Lost the connection to readthrough serve. Retrying."><span class="ping"></span>offline</span>`;
  if (s.error) { const e = typeof s.error === "object" ? `${s.error.message} [error ${s.error.id}]` : s.error;
    return `<span class="live-badge err" title="${esc(e)}"><span class="ping"></span>refresh failed</span>`; }
  const next = s.next_build ? ` · next check ${new Date(s.next_build * 1000).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"})}` : "";
  return `<span class="live-badge" title="Connected to readthrough serve${next}"><span class="ping"></span>live</span>`;
}
const TABS = [["map", "Architecture map", "layers"], ["plan", "Burn-down plan", "list-checks"], ["overlap", "Overlap graph", "network"], ["table", "All PRs", "table-2"]];
function renderTabs(){
  const nav = $("tabs");
  if (!nav.childElementCount) nav.innerHTML = `<span class="tab-ind"></span>` + TABS.map(([k, l, ic]) => `<button class="tab" data-tab="${k}" title="${l}">${icon(ic)}<span>${l}</span></button>`).join("");
  let on;
  nav.querySelectorAll(".tab").forEach(t => { const yes = t.dataset.tab === state.tab; t.classList.toggle("on", yes); t.setAttribute("aria-current", yes ? "page" : "false"); if (yes) on = t; });
  const ind = nav.querySelector(".tab-ind");
  if (on) requestAnimationFrame(() => { ind.style.left = on.offsetLeft + "px"; ind.style.width = on.offsetWidth + "px"; });
}
// Filters live in the URL, so a view like "only my PRs" can be bookmarked.
function syncHash(){
  const q = new URLSearchParams(), key = {lanes: "lanes", authors: "authors", cis: "ci"};
  for (const [g, labels] of Object.entries(GROUPS)) if (state[g].size !== Object.keys(labels).length) q.set(key[g], [...state[g]].join(","));
  if (state.q.trim()) q.set("q", state.q.trim());
  if (state.comp) q.set("comp", state.comp);
  const hash = "#" + state.tab + (q.size ? "?" + q.toString().replace(/%2C/g, ",") : "");
  if (location.hash !== hash) history.replaceState(null, "", hash);
}
function pick(group, key, e){
  const set = state[group], all = Object.keys(GROUPS[group]);
  if (e.ctrlKey || e.metaKey || e.shiftKey) { set.has(key) ? set.delete(key) : set.add(key); if (!set.size) all.forEach(k => set.add(k)); }
  else if (set.size === 1 && set.has(key)) all.forEach(k => set.add(k));
  else { set.clear(); set.add(key); }
  render();
}
const needsYou = () => DATA.prs.filter(p => p.flags.some(f => f.kind === "you") && !progress[p.number]?.reviewed).length;
function banners(){
  const out = [];
  const ageH = (Date.now() - new Date(DATA.generated)) / 3.6e6, fresh = DATA.freshness || {stale_live_hours: 2, stale_snapshot_hours: 26};
  if (ageH > (LIVE ? fresh.stale_live_hours : fresh.stale_snapshot_hours)) out.push(`<div class="banner warn"><b>This data is ${Math.round(ageH)} hours old.</b><span class="muted">Refresh, or check <code>readthrough doctor</code>.</span></div>`);
  const err = LIVE?.status?.error;
  if (err && typeof err === "object") out.push(`<div class="banner warn"><b>The last refresh failed</b><span>during ${esc(err.step)}: ${esc(err.message)}</span><span class="muted">Showing the last good data. ${LIVE.status.consecutive_failures > 1 ? `${LIVE.status.consecutive_failures} failures in a row. ` : ""}Retrying automatically. Error <code>${esc(err.id)}</code> in readthrough.log.</span></div>`);
  for (const w of DATA.warnings || []) out.push(`<div class="banner warn"><b>${icon("info", "ico sm")}${esc(w.message)}</b>${w.details?.length ? `<details class="muted"><summary>${w.details.length} detail${w.details.length > 1 ? "s" : ""}</summary><ul>${w.details.map(d => `<li><code>${esc(d)}</code></li>`).join("")}</ul></details>` : ""}</div>`);
  if (DATA.draft) out.push(`<div class="banner warn"><b>Onboarding preview.</b><span class="muted">This map comes from a draft profile. Edit it, then finish with <code>readthrough onboard ${esc(DATA.repo)} --accept</code>.</span></div>`);
  if (DATA.drift) out.push(`<div class="banner warn"><b>The map is drifting:</b><span>only ${Math.round(DATA.drift.coverage * 100)}% of open-PR changes land in a known component.</span><span class="muted">Unmapped: ${DATA.drift.unmapped.map(([p, n]) => `<code>${esc(p)}</code> (${n})`).join(", ")}. Update it with <code>readthrough onboard ${esc(DATA.repo)} --update</code>.</span></div>`);
  const nw = DATA.news || {};
  if (nw.since){
    const list = ns => ns.filter(n => byNum[n]).map(n => chip(byNum[n])).join(" ");
    const parts = [];
    if (nw.needs_you?.length) parts.push(`<span><b>Needs you:</b> ${list(nw.needs_you)}</span>`);
    if (nw.changed_after_review?.length) parts.push(`<span><b>Changed since your review:</b> ${list(nw.changed_after_review)}</span>`);
    if (nw.opened?.length) parts.push(`<span><b>New:</b> ${list(nw.opened)}</span>`);
    if (nw.closed?.length) parts.push(`<span class="muted">Closed or merged: ${nw.closed.map(c => `<span title="${esc(c.title)}">#${c.number}</span>`).join(", ")}</span>`);
    out.push(`<div class="banner"><span class="muted">Since you last opened this (${new Date(nw.since).toLocaleString()}):</span>${parts.join("") || '<span class="muted">nothing new.</span>'}</div>`);
  }
  $("banners").innerHTML = out.join("");
}
function chip(p, comp){
  const cls = ["chip", comp && p.primary === comp ? "pri" : "", (isFirst(p) ? "first" : isExt(p) ? "ext" : authorGroup(p) === "bot" ? "bot" : "")].join(" ");
  const pad = Math.round(5 + Math.min(12, Math.log2(1 + lines(p))));
  const who = kindBadge(p.author_kind);
  return `<span class="${cls}" data-pr="${p.number}" style="background:${laneColor(p.lane)};padding-left:${pad}px;padding-right:${pad}px" title="#${p.number} ${esc(p.title)}\n@${esc(p.author)} (${who}) · ${LANES[p.lane]} · +${p.additions}/-${p.deletions}${comp ? ` · ${p.by_comp[comp]} lines here` : ""}">${p.number}</span>`;
}
// Every signal is worked out by a rule, never by a language model; DATA.rules says which rule.
const SOURCE = {forge: ["git-pull-request", `From ${FORGE.label}`], github: ["git-pull-request", "From GitHub"], profile: ["cog", "Rule in the repo's profile"], "built-in": ["cog", "readthrough check"]};
const ruleTip = id => { const r = DATA.rules?.[id]; return r ? `${SOURCE[r.source][1]}: ${r.how}` : ""; };
const ruleIcon = id => { const r = DATA.rules?.[id]; return r ? icon(SOURCE[r.source][0], "ico sm") : ""; };
function flagsHtml(p, kinds = ["blocker","you","risk","responsibility"]){
  const fl = p.flags.filter(f => kinds.includes(f.kind)).map(f => `<span class="flag ${f.kind}" title="${esc(f.msg + "\n" + ruleTip(f.id))}">${f.id}</span>`);
  const sm = [...new Set(p.smells.filter(s => s.kind === "responsibility").map(s => s.id))].map(id => `<span class="flag responsibility" title="${esc(p.smells.find(s => s.id === id).msg + "\n" + ruleTip(id))}">${id}</span>`);
  return fl.concat(sm).join("");
}

// ---- architecture map
// Bar under each component: length = its changed lines vs. the busiest component; colors = how those lines split by lane.
function mixBar(prs, comp, total, max){
  if (!total) return "";
  const by = prs.reduce((m, p) => (m[p.lane] = (m[p.lane] || 0) + p.by_comp[comp], m), {});
  const parts = Object.keys(LANES).filter(l => by[l]);
  const tip = `${total} changed lines here (${Math.round(100 * total / max)}% of the busiest component): ` + parts.map(l => `${LANES[l]} ${by[l]}`).join(", ");
  return `<div class="mix" style="width:${100 * total / max}%" title="${tip}">${parts.map(l => `<span style="flex:${by[l]};background:${laneColor(l)}"></span>`).join("")}</div>`;
}
function renderMap(){
  const vis = DATA.prs.filter(visible);
  const max = Math.max(1, ...DATA.components.map(c => vis.reduce((s,p) => s + (p.by_comp[c.id]||0), 0)));
  let html = `<p class="legend">Each chip is a PR, colored by lane and sized by how much it changes. An outline marks the component where most of the change lives. An orange dot marks an external author, a hollow one a first-time contributor. Hover a chip to light up every component it touches; click a component to filter. The bar under a component shows how much is changing there compared with the busiest one, colored by lane. ${icon("cog", "ico sm")}Placement is by the profile's path patterns, not a language model. ${profileLine()}</p>`;
  if (!vis.length) { $("v-map").innerHTML = html + EMPTY("No PRs match these filters."); return; }
  for (const {id: layer, label, flow} of PROFILE.layers){
    const bricks = DATA.components.filter(c => c.layer === layer).map((c, i) => {
      const prs = vis.filter(p => c.id in p.by_comp).sort((a,b) => b.by_comp[c.id] - a.by_comp[c.id]);
      const total = prs.reduce((s,p) => s + p.by_comp[c.id], 0);
      const arrow = flow && i > 0 ? `<span class="arrow">→</span>` : "";
      return `${arrow}<div class="brick ${prs.length?"":"empty"} ${state.comp===c.id?"sel":""}" data-comp="${c.id}" title="${esc(c.contract?.responsibility || "")}">
        <h3>${esc(c.label)} <span>${prs.length} PR · ${total} ln</span></h3>
        <div class="chips">${prs.map(p => chip(p, c.id)).join("")}</div>
        ${mixBar(prs, c.id, total, max)}</div>`;
    }).join("");
    html += `<div class="layer"><div class="layer-label" style="border-color:${layerColor(layer)}">${label}</div><div class="bricks">${bricks}</div></div>`;
  }
  $("v-map").innerHTML = html;
}

// ---- burn-down plan
const ciBadge = p => {
  const s = ciState(p), c = CI[s], fails = (p.ci_checks?.failing || []);
  const detail = fails.length ? "\n" + fails.slice(0, 8).map(f => `✗ ${f.name}${f.required ? " (required)" : ""}${f.on_main ? " (also on main)" : ""}`).join("\n") : "";
  return `<span class="cib ${s}" title="CI: ${c.label}${detail}">${c.icon}</span>`;
};
const STATUSES = [["", "To do"], ["understood", "Understood"], ["reviewed", "Reviewed"], ["commented", "Commented"], ["approved", "Approved"], ["changes-requested", "Changes requested"], ["skipped", "Skipped"]];
const statusOf = n => { const g = progress[n]; if (!g || stale(n)) return ""; return g.status || (g.reviewed ? "reviewed" : g.understood ? "understood" : ""); };
const statusSelect = n => { const cur = statusOf(n);
  return `<select class="status s-${cur || "todo"}" data-status="${n}" aria-label="Your status for #${n}" title="What you've done with #${n}${LIVE ? ". Saved to the ledger; new commits reset it." : ". New commits reset it."}">${STATUSES.map(([v, l]) => `<option value="${v}" ${cur === v ? "selected" : ""}>${l}</option>`).join("")}</select>`; };
function prRow(n){
  const p = byNum[n]; if (!p || !visible(p)) return "";
  const g = progress[n] || {}; const st = stale(n);
  return `<div class="row ${g.reviewed && !st ? "done":""}">
    ${statusSelect(n)}
    <a href="${p.url}" target="_blank" rel="noopener">#${n}</a>
    <span class="t" data-open="${n}" title="${esc(p.title)}">${ciBadge(p)} ${esc(p.title)} <span class="muted">@${esc(p.author)}</span>${whoBadge(p)}${sessDot(p)}${scoreBadge(p)}${st?' <span class="flag blocker">changed since review</span>':""}</span>
    <span>${flagsHtml(p)}</span>
    <span class="muted">${p.minutes}m ${reviewButton(n)}</span></div>`;
}
function scatter(){
  const pts = DATA.prs.filter(visible);
  const W = 460, H = 300, P = 34;
  const maxAge = Math.max(2, ...pts.map(p => p.age_days)), maxL = Math.max(10, ...pts.map(lines));
  const x = a => P + (W-2*P) * Math.log1p(a)/Math.log1p(maxAge);
  const y = l => H-P - (H-2*P) * Math.log1p(l)/Math.log1p(maxL);
  const midX = Math.min(W-P, x(14)), midY = Math.max(P, y(200));  // clamped: with only young or small PRs the dividers fall off the plot
  return `<svg width="100%" viewBox="0 0 ${W} ${H}">
    <rect x="${P}" y="${midY}" width="${midX-P}" height="${H-P-midY}" style="fill:var(--good);opacity:.05"/>
    <rect x="${midX}" y="${midY}" width="${W-P-midX}" height="${H-P-midY}" style="fill:var(--good);opacity:.11"/>
    <rect x="${midX}" y="${P}" width="${W-P-midX}" height="${midY-P}" style="fill:var(--bad);opacity:.07"/>
    <text x="${W-P-4}" y="${H-P-6}" text-anchor="end">quick wins: small and waiting</text>
    <text x="${W-P-4}" y="${P+12}" text-anchor="end">big and old: schedule or ask to split</text>
    <text x="${P+4}" y="${P+12}">big and new</text>
    <line x1="${P}" y1="${H-P}" x2="${W-P}" y2="${H-P}" style="stroke:var(--line)"/><line x1="${P}" y1="${P}" x2="${P}" y2="${H-P}" style="stroke:var(--line)"/>
    <text x="${W/2}" y="${H-8}" text-anchor="middle">age (days, log) →</text><text x="10" y="${H/2}" transform="rotate(-90 10 ${H/2})" text-anchor="middle">lines changed (log) →</text>
    ${pts.map(p => `<circle cx="${x(p.age_days)}" cy="${y(lines(p))}" r="${progress[p.number]?.reviewed?3.5:5.5}" style="fill:${laneColor(p.lane)};stroke:${isExt(p) ? "var(--external)" : "var(--panel)"};stroke-width:${isExt(p) ? 2 : 1}" fill-opacity=".9" data-open="${p.number}" cursor="pointer"><title>#${p.number} ${esc(p.title)} (${lines(p)} ln, ${Math.round(p.age_days)}d, ${esc(kindBadge(p.author_kind))}, CI ${CI[ciState(p)].label})</title></circle>${["failing", "failing-optional"].includes(ciState(p)) ? `<text x="${x(p.age_days) + 6}" y="${y(lines(p)) - 5}" style="fill:var(--bad);font-size:11px;font-weight:700;pointer-events:none">✗</text>` : ""}`).join("")}
  </svg>`;
}
function renderPlan(){
  // "CI passing first": within each card, green PRs float up (stable, so priority order is kept otherwise).
  const order = ns => state.greenFirst ? [...ns].sort((a, b) => (ciState(byNum[a]) === "passing" ? 0 : 1) - (ciState(byNum[b]) === "passing" ? 0 : 1)) : ns;
  const sessions = DATA.sessions.map((s, i) => {
    const rows = order(s.prs).map(prRow).join(""); if (!rows) return "";
    const done = s.prs.filter(n => progress[n]?.reviewed && !stale(n)).length;
    return `<div class="card"><h2>Session ${i+1} · ${esc(s.label)} <span>~${s.minutes} min · ${done}/${s.prs.length} done</span></h2>${rows}</div>`;
  }).join("");
  const lane = (l, note) => {
    const ps = DATA.prs.filter(p => p.lane === l && visible(p)); if (!ps.length) return "";
    return `<div class="card"><h2>${LANES[l]} <span>${note}</span></h2>${order(ps.sort((a,b)=>a.age_days-b.age_days).map(p => p.number)).map(prRow).join("")}</div>`;
  };
  $("v-plan").innerHTML = `<div class="grid2"><div>
      <div class="card"><h2>Size vs. age <span>click a dot · orange ring = external author</span></h2>${scatter()}</div>
      ${lane("approved","needs a merge or a final look")}
      ${lane("waiting","changes requested; nothing to do until the author pushes")}
      ${lane("blocked","conflicts or a required check failing; nudge the author")}
      ${lane("mine","your PRs")}
      ${lane("draft","not ready yet")}
    </div><div>
      <p class="muted" style="margin-top:0">Ready PRs are packed into sessions of about an hour, each in one component so you build context instead of switching it. Requested reviews and re-reviews come first, then quick wins, then the oldest. The menu on each row is your status (to do, understood, reviewed, and so on)${LIVE ? ", saved to the ledger the review sessions also write to" : ""}. New commits reset it. A <span class="sess"></span> means a review session exists; <span class="score">quiz 2/3</span> is your study-log score; <span class="cib passing">✓</span> <span class="cib failing">✗</span> <span class="cib failing-optional">!</span> show CI.</p>
      <p style="margin-top:0"><label><input type="checkbox" id="greenFirst" ${state.greenFirst ? "checked" : ""}> CI passing first</label> <span class="muted">· a red check doesn't block review, but a green one means the PR is less likely to change under you.</span></p>
      ${sessions || `<div class="card">${EMPTY("Nothing ready matches these filters.")}</div>`}
    </div></div>`;
}

// ---- table
let sortKey = "priority", sortDir = -1;
function renderTable(){
  const cols = [["number","#"],["title","Title"],["author","Author"],["author_kind","Who"],["lane","Lane"],["ci","CI"],["age_days","Age"],["idle_days","Idle"],["churn","±"],["changed_files","Files"],["primary","Primary"],["flags","Signals"],["minutes","Est"],["priority","Prio"]];
  // CI sorts by urgency, not alphabetically: ascending puts required failures first.
  const val = (p,k) => k === "churn" ? lines(p) : k === "flags" ? p.flags.length + p.smells.length : k === "ci" ? ciRank(p) : p[k];
  const rows = DATA.prs.filter(visible).sort((a,b) => { const x = val(a,sortKey), y = val(b,sortKey); return (x > y ? 1 : x < y ? -1 : 0) * sortDir; });
  $("v-table").innerHTML = `<table><thead><tr>${cols.map(([k,l]) => `<th data-sort="${k}">${l}${sortKey===k?(sortDir>0?" ▲":" ▼"):""}</th>`).join("")}</tr></thead><tbody>
  ${rows.map(p => `<tr><td><a href="${p.url}" target="_blank" rel="noopener">${p.number}</a></td><td style="cursor:pointer" data-open="${p.number}">${esc(p.title)}${sessDot(p)}${scoreBadge(p)}</td><td>@${esc(p.author)}</td><td>${whoBadge(p)}</td>
    <td><span class="pill" style="background:${laneColor(p.lane)}">${LANES[p.lane]}</span></td><td>${ciBadge(p)} <span class="muted">${CI[ciState(p)].label}</span></td><td class="num">${Math.round(p.age_days)}d</td><td class="num">${Math.round(p.idle_days)}d</td>
    <td class="num"><span class="add">+${p.additions}</span> <span class="del">-${p.deletions}</span></td><td class="num">${p.changed_files}</td><td>${esc(comps[p.primary].label)}</td>
    <td>${flagsHtml(p, ["blocker","you","risk","responsibility","convention","author"])}</td><td class="num">${p.minutes}m</td><td class="num">${p.priority}</td></tr>`).join("")}</tbody></table>`;
}

// ---- overlap graph (Cytoscape + fcose): PRs grouped by primary component, edges = shared files
let cy = null, cySig = "";
const keepFile = f => !state.srcOnly || !NON_SOURCE.some(r => r.test(f));
function overlapEdges(){
  const vis = new Set(DATA.prs.filter(visible).map(p => p.number));
  return DATA.overlaps.filter(e => vis.has(e.a) && vis.has(e.b)).map(e => ({...e, files: e.files.filter(keepFile)})).filter(e => e.files.length);
}
function renderOverlap(){
  const edges = overlapEdges();
  const connected = new Set(edges.flatMap(e => [e.a, e.b]));
  const prs = DATA.prs.filter(visible).filter(p => !state.overlapOnly || connected.has(p.number));
  const sig = JSON.stringify([DATA.generated, prs.map(p => p.number + p.lane), edges.length, state.srcOnly, state.overlapOnly]);
  const view = $("v-overlap");
  if (!view.querySelector("#cy")) {
    view.innerHTML = `<div class="toolbar">
      <label><input type="checkbox" id="srcOnly" ${state.srcOnly?"checked":""}> source files only</label>
      <label><input type="checkbox" id="overlapOnly" ${state.overlapOnly?"checked":""}> only PRs that share files</label>
      <button class="btn small" id="fit">fit</button>
      <span>Edges join PRs that change the same files (thicker = more files). Boxes group PRs by their main component. Drag, scroll to zoom, hover for details.</span></div>
      <div id="cy"></div><div class="card" style="margin-top:14px" id="hot"></div>`;
    cySig = "";
  }
  renderHotFiles();
  if (sig === cySig && cy) { cy.resize(); return; }
  cySig = sig;
  const groups = [...new Set(prs.map(p => p.primary))];
  const elements = [
    ...groups.map(g => ({data: {id: "c:" + g, label: comps[g].label, layer: comps[g].layer}, classes: "group"})),
    ...prs.map(p => ({data: {id: "p:" + p.number, parent: "c:" + p.primary, label: String(p.number), pr: p.number, lane: p.lane, kind: p.author_kind, ext: isExt(p), first: isFirst(p), size: 34 + Math.min(30, Math.sqrt(lines(p)) * 1.2)}, classes: "pr"})),
    ...edges.filter(e => prs.some(p => p.number === e.a) && prs.some(p => p.number === e.b))
      .map(e => ({data: {id: `e:${e.a}-${e.b}`, source: "p:" + e.a, target: "p:" + e.b, weight: e.files.length, files: e.files}})),
  ];
  if (cy) cy.destroy();
  cy = cytoscape({container: $("cy"), elements, wheelSensitivity: 0.3, minZoom: 0.2, maxZoom: 3});
  styleGraph();
  cy.layout({name: "fcose", quality: "proof", animate: false, randomize: true, nodeRepulsion: 22000, idealEdgeLength: e => 140 / Math.sqrt(e.data("weight")), edgeElasticity: 0.2, nodeSeparation: 90, nestingFactor: 0.5, gravity: 0.15, gravityRangeCompound: 1.6, packComponents: true, tilingPaddingVertical: 18, tilingPaddingHorizontal: 18, padding: 20}).run();
  const tip = $("tip");
  const showTip = (evt, html) => { tip.innerHTML = html; tip.style.display = "block"; const r = evt.originalEvent; tip.style.left = (r.clientX + 14) + "px"; tip.style.top = (r.clientY + 12) + "px"; };
  cy.on("mouseover", "node.pr", evt => {
    const p = byNum[evt.target.data("pr")];
    cy.elements().addClass("faded"); evt.target.closedNeighborhood().removeClass("faded"); evt.target.ancestors().removeClass("faded");
    showTip(evt, `<b>#${p.number}</b> ${esc(p.title)}<br><span class="muted">@${esc(p.author)} (${esc(kindBadge(p.author_kind))}) · ${LANES[p.lane]} · +${p.additions}/-${p.deletions}</span>`);
  });
  cy.on("mouseover", "edge", evt => {
    const f = evt.target.data("files");
    showTip(evt, `<b>#${evt.target.source().data("pr")} ↔ #${evt.target.target().data("pr")}</b>: ${f.length} shared file${f.length > 1 ? "s" : ""}<br><span class="mono">${f.slice(0, 10).map(esc).join("<br>")}</span>${f.length > 10 ? "<br>…" : ""}`);
  });
  cy.on("mousemove", evt => { if (tip.style.display === "block") { tip.style.left = (evt.originalEvent.clientX + 14) + "px"; tip.style.top = (evt.originalEvent.clientY + 12) + "px"; } });
  cy.on("mouseout", "node.pr, edge", () => { tip.style.display = "none"; cy.elements().removeClass("faded"); });
  cy.on("tap", "node.pr", evt => openDrawer(evt.target.data("pr")));
}
function styleGraph(){
  const c = n => css(n);
  cy.style([
    {selector: "node.group", style: {"label": "data(label)", "text-valign": "top", "text-halign": "center", "font-size": 13, "font-weight": 600, "color": c("--mut"), "background-color": c("--subtle"), "background-opacity": 0.6, "border-width": 1, "border-color": c("--line"), "shape": "round-rectangle", "padding": 14, "text-margin-y": -4}},
    {selector: "node.pr", style: {"label": "data(label)", "width": "data(size)", "height": "data(size)", "font-size": 12, "font-weight": 700, "text-valign": "center", "color": c("--chip-fg"), "border-width": 2, "border-color": c("--panel"), "text-outline-width": 0}},
    ...Object.keys(LANES).map(l => ({selector: `node.pr[lane = "${l}"]`, style: {"background-color": c("--" + l)}})),
    {selector: 'node.pr[?ext]', style: {"border-color": c("--external"), "border-width": 3}},
    {selector: 'node.pr[?first]', style: {"border-style": "dashed"}},
    {selector: "edge", style: {"width": e => Math.min(10, 0.8 + e.data("weight") * 1.3), "line-color": c("--line-strong"), "opacity": e => Math.min(0.85, 0.25 + e.data("weight") * 0.15), "curve-style": "haystack"}},
    {selector: ".faded", style: {"opacity": 0.12}},
  ]);
}
function renderHotFiles(){
  const touch = {};
  DATA.prs.filter(visible).forEach(p => p.files.forEach(f => { if (keepFile(f.path)) (touch[f.path] ||= []).push(p.number); }));
  const hot = Object.entries(touch).filter(([, ns]) => ns.length >= 3).sort((a,b) => b[1].length - a[1].length).slice(0, 15);
  $("hot").innerHTML = `<h2>Hot files <span>touched by 3+ open PRs: whichever merges first forces rebases on the others</span></h2>` +
    (hot.map(([f, ns]) => `<div class="row" style="grid-template-columns:1fr auto"><code>${esc(f)}</code><span class="chips">${ns.map(n => chip(byNum[n])).join("")}</span></div>`).join("") || '<span class="muted">none</span>');
}

// ---- PR panel
const briefs = {};
const drawerBody = () => document.querySelector("#drawer .dbody");
function openDrawer(n, tab){
  if (!byNum[n]) return closeDrawer();
  const changed = state.sel !== n;
  state.sel = n;
  if (tab) state.dtab = tab;
  document.body.classList.add("drawer-open");
  renderDrawer({resetScroll: changed});
  if (cy) setTimeout(() => cy.resize(), 0);
}
function closeDrawer(){
  document.body.classList.remove("drawer-open"); state.sel = null;
  sessionStorage.removeItem(STORE + ":panel");
  if (cy) setTimeout(() => cy.resize(), 0);
}
// Whether the drawer body is scrolled to (or near) its end, so new study entries keep it there, like any chat.
const nearEnd = el => !!el && el.scrollHeight - el.scrollTop - el.clientHeight < 80;
function renderDrawer({resetScroll = false, follow = false} = {}){
  const n = state.sel, p = byNum[n];
  if (!p) return closeDrawer();
  sessionStorage.setItem(STORE + ":panel", JSON.stringify({sel: n, dtab: state.dtab}));
  const body = drawerBody();
  const toEnd = state.dtab === "study" && (resetScroll || follow || nearEnd(body));
  const scroll = resetScroll || !body ? 0 : body.scrollTop;
  const active = document.activeElement?.dataset?.draft;
  const caret = active ? document.activeElement.selectionStart : null;
  const tabs = [["overview", "Overview"], ["study", "Study"], ["brief", "Brief"]];
  $("dinner").innerHTML = `<div class="dhead"><span class="close" id="close" title="Close (Esc)">✕</span>
      <div><span class="pill" style="background:${laneColor(p.lane)}">${LANES[p.lane]}</span> ${p.labels.map(l => `<span class="flag">${esc(l)}</span>`).join("")}</div>
      <h2><a href="${p.url}" target="_blank" rel="noopener">#${p.number}</a> ${esc(p.title)}</h2>
      <div class="kv"><span>by <b>@${esc(p.author)}</b>${whoBadge(p)}</span><span>opened <b>${Math.round(p.age_days)}d</b> ago</span><span>idle <b>${Math.round(p.idle_days)}d</b></span>
        <span><b class="add">+${p.additions}</b> <b class="del">-${p.deletions}</b> in <b>${p.changed_files}</b> files</span><span>CI <b>${CI[ciState(p)].label}</b></span><span>review <b>${p.review_decision}</b></span><span>est <b>${p.minutes} min</b></span></div>
      <div class="dtabs">${tabs.map(([k, l]) => `<span class="dtab ${state.dtab === k ? "on" : ""}" data-dtab="${k}">${l}${k === "study" && p.study?.assessed ? ` <span class="score">${p.study.correct}/${p.study.assessed}</span>` : ""}</span>`).join("")}</div>
    </div><div class="dbody">${state.dtab === "study" ? studyTab(n) : state.dtab === "brief" ? briefTab(n) : overviewTab(n)}</div>`;
  document.querySelectorAll("#drawer [data-draft]").forEach(el => { el.value = state.drafts[`${n}:${el.dataset.draft}`] || ""; });
  if (active) { const el = document.querySelector(`#drawer [data-draft="${CSS.escape(active)}"]`); if (el) { el.focus(); if (caret != null) el.setSelectionRange(caret, caret); } }
  const fresh = drawerBody();
  fresh.scrollTop = toEnd ? fresh.scrollHeight : scroll;
}
function ciSection(p){
  const c = p.ci_checks; if (!c) return "";
  const rows = c.failing.map(f => `<div>${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.name)}</a>` : esc(f.name)}
    ${f.required ? '<span class="flag blocker">required</span>' : '<span class="flag">optional</span>'}${f.on_main ? '<span class="flag" title="This check fails on the default branch too">also failing on main</span>' : ""}</div>`).join("");
  const summary = `${c.passed} passed · ${c.failing.length} failing · ${c.pending} pending${c.cancelled ? ` · ${c.cancelled} cancelled` : ""}` +
    (c.required_missing.length ? ` · ${c.required_missing.length} required not reported` : "") + (c.required_known ? "" : " · required checks unknown");
  return `<h4>CI ${ciBadge(p)}</h4><div class="hint" style="margin-top:0">${CI[ciState(p)].label}: ${summary}</div>${rows ? `<div class="checks">${rows}</div>` : ""}`;
}
function overviewTab(n){
  const p = byNum[n], g = progress[n] || {};
  const compRows = Object.entries(p.by_comp).sort((a,b) => b[1]-a[1]).map(([c, l]) => {
    const k = comps[c]; const contract = k.contract?.does_not_own || (k.contract?.responsibility ? "Responsibility: " + k.contract.responsibility : "");
    return `<div><b>${esc(k.label)}</b> <span class="muted">(${k.layer}, ${l} lines)</span>${contract ? `<div class="contract">${esc(contract.replace(/\*\*/g, ""))}</div>` : ""}</div>`;
  }).join("");
  const files = [...p.files].sort((a,b) => (b.add+b.del)-(a.add+a.del)).map(f => `<div><span class="mono" title="${esc(comps[f.comp].label)}">${esc(f.path)}</span><span class="mono"><span class="add">+${f.add}</span> <span class="del">-${f.del}</span></span></div>`).join("");
  const smells = p.smells.map(s => `<div class="smell"><span class="flag ${s.kind}" title="${esc(ruleTip(s.id))}">${ruleIcon(s.id)}${s.kind}</span> <a href="${p.url}${FORGE.files_tab || "/files"}" target="_blank" rel="noopener">${esc(s.path)}:${s.line}</a>: ${esc(s.msg)}<code>${esc(s.code)}</code></div>`).join("");
  const threads = p.unresolved.map(t => `<div class="smell"><b>@${esc(t.author)}</b> on <span class="muted mono">${esc(t.path)}:${t.line ?? "?"}${t.outdated ? " (outdated)" : ""}</span><div class="md desc" style="max-height:220px;margin-top:4px">${t.body_html ? safeHtml(t.body_html) : md(t.body)}</div></div>`).join("");
  const related = (p.related || []).map(r => byNum[r] ? chip(byNum[r]) : "").join("");
  const rules = p.rules.map(r => { const ins = DATA.instructions.find(i => i.name === r); return `<a href="${ins?.url}" target="_blank" rel="noopener">${esc(r)}</a>`; }).join(" · ");
  const s = p.session;
  return `<div class="actions">
      ${reviewButton(n, "btn primary").replace(/>(review|resume)</, (m, w) => `>${w === "resume" ? "Resume" : "Start"} deep review<`)}
      ${LIVE ? `<button class="btn" data-dtab="study" title="Predict, get quizzed, teach back; everything goes in this PR's study log">Study</button>` : ""}
      <a class="btn" href="${vscodeUrl(n)}" title="${byNum[n].sessions?.some(s => s.editor_url) ? "Open this PR's latest agent session in VS Code" : "Open a VS Code agent session with the prompt filled in; it'll be linked to this PR"}">Open in VS Code</a>
      <button class="btn" data-deep="${n}">Copy prompt</button>
      <button class="btn" data-copy="readthrough worktree ${n}">Copy worktree cmd</button></div>
    ${sessionList(n)}
    <div class="actions"><span class="muted">Your status</span> ${statusSelect(n)}</div>
    <h4>Signals</h4>${p.flags.map(f => `<span class="flag ${f.kind}" title="${esc(f.id + ": " + ruleTip(f.id))}">${ruleIcon(f.id)}${esc(f.msg)}</span>`).join(" ") || '<span class="muted">none</span>'}
    ${ciSection(p)}
    ${smells ? `<h4>Line heuristics (verify, not verdicts)</h4>${smells}` : ""}
    <h4>Description</h4><div class="md desc">${p.body_html ? safeHtml(p.body_html) : md(p.body || "_No description._")}</div>
    <h4>Architecture contracts touched</h4>${compRows}
    <h4>Rules that apply</h4>${rules || '<span class="muted">none</span>'}
    <h4>Related open PRs (shared files)</h4><div class="chips">${related || '<span class="muted">none; it can be reviewed on its own</span>'}</div>
    ${threads ? `<h4>Unresolved threads (${p.unresolved.length})</h4>${threads}` : ""}
    <h4>Files</h4><div class="files">${files}</div>
    ${howItWorks()}`;
}
const profileLine = () => {
  const pr = PROFILE, when = pr.onboarded ? new Date(pr.onboarded).toLocaleDateString() : null;
  const drafted = pr.drafted_by === "model" ? ", first drafted by a model (the profile's header says which)" : pr.drafted_by === "readthrough" ? ", first drafted by readthrough from the file tree" : "";
  return `The map comes from ${esc(pr.origin || "the repo's profile")}${drafted}${when ? `, accepted on ${when}` : ""}.`;
};
function howItWorks(){
  return `<div class="how"><p>${icon("cog", "ico sm")}<b>Worked out by rules.</b> Lanes, CI, and review state come from ${esc(FORGE.label)}. Components come from the profile's path patterns, signals and line heuristics from its rules (hover one to see which), and the contracts are ${PROFILE.contracts_source ? `quoted from <code>${esc(PROFILE.contracts_source)}</code> in the repo` : "written in the profile"}. The plan, estimates, and related PRs are arithmetic. ${profileLine()}</p>
    <p>${icon("sparkles", "ico sm")}<b>Written by a model:</b> only what's in the Study tab (each entry names the model), what review agents write in their sessions, and the profile itself if a model drafted it.</p></div>`;
}
function briefTab(n){
  if (!LIVE) return `<p class="muted" style="margin-top:14px">The rendered brief is part of the live dashboard (<code>readthrough serve</code>). The file is at <code>${esc(LINKS.briefs_dir)}\\pr-${n}.md</code>. <button class="btn small" data-copy="${esc(LINKS.briefs_dir + "\\pr-" + n + ".md")}">copy path</button></p>`;
  if (briefs[n] === undefined) {
    briefs[n] = null;
    api(`/api/brief/${n}`, null, "GET").then(r => { briefs[n] = r.markdown; if (state.sel === n && state.dtab === "brief") renderDrawer(); })
      .catch(err => { briefs[n] = `_Couldn't load the brief: ${err.message}_`; renderDrawer(); });
  }
  return briefs[n] === null ? `<p class="muted">Loading…</p>` : `<div class="md" style="margin-top:12px">${md(briefs[n])}</div>`;
}

// ---- panel resizing
(function setupGrip(){
  const root = document.documentElement, grip = $("grip");
  const clamp = w => Math.max(380, Math.min(w, Math.min(1400, innerWidth * 0.9)));
  const setW = w => root.style.setProperty("--drawer-w", clamp(w) + "px");
  setW(+localStorage.getItem("readthrough:drawer-w") || 560);
  grip.addEventListener("pointerdown", e => {
    grip.setPointerCapture(e.pointerId); grip.classList.add("on");
    const move = ev => setW(innerWidth - ev.clientX);
    const up = () => {
      grip.classList.remove("on"); grip.removeEventListener("pointermove", move); grip.removeEventListener("pointerup", up);
      localStorage.setItem("readthrough:drawer-w", parseInt(getComputedStyle(root).getPropertyValue("--drawer-w")));
      if (cy) cy.resize();
    };
    grip.addEventListener("pointermove", move); grip.addEventListener("pointerup", up);
  });
  grip.addEventListener("dblclick", () => { setW(560); localStorage.removeItem("readthrough:drawer-w"); if (cy) cy.resize(); });
})();

// ---- study tab: predictions, Socratic quiz, teach-back, evidence. All of it appends to the study log.
function loadStudy(n){
  if (!LIVE || study[n]?.loading) return;
  study[n] = {...(study[n] || {}), loading: true};
  api(`/api/study/${n}`, null, "GET").then(r => {
    study[n] = {...r, pending: study[n]?.pending || null};
    if (byNum[n]) byNum[n].study = r.summary;
    if (state.sel === n && state.dtab === "study") renderDrawer();
  }).catch(err => { study[n] = {error: err.message}; renderDrawer(); });
}
// The composer's mode follows the log: predict first (or skip), answer an open question, otherwise talk.
function studyMode(entries){
  const answered = new Set(entries.filter(e => e.kind === "answer").map(e => e.qid));
  const openQ = [...entries].reverse().find(e => e.kind === "question" && !answered.has(e.qid));
  if (openQ) return {mode: "answer", q: openQ};
  if (!entries.some(e => e.kind === "prediction")) return {mode: "predict"};
  return {mode: "chat"};
}
function watchList(p){
  const items = [];
  p.flags.filter(f => f.kind !== "info").forEach(f => items.push(`${ruleIcon(f.id)}${esc(f.msg)}`));
  const smells = {}; p.smells.forEach(sm => (smells[sm.id] ||= []).push(sm));
  Object.values(smells).forEach(list => items.push(`${ruleIcon(list[0].id)}${esc(list[0].msg)}: ${list.slice(0, 3).map(sm => `<code>${esc(sm.path.split("/").pop())}:${sm.line}</code>`).join(", ")}${list.length > 3 ? ` and ${list.length - 3} more` : ""}`));
  const prim = comps[p.primary]?.contract?.does_not_own;
  if (prim) items.push(`${icon("cog", "ico sm")}${esc(comps[p.primary].label)} doesn't own: ${esc(prim.replace(/\*\*/g, "").slice(0, 220))}`);
  const big = [...p.files].sort((a, b) => (b.add + b.del) - (a.add + a.del)).slice(0, 3);
  items.push(`${icon("cog", "ico sm")}Most changed: ${big.map(f => `<code>${esc(f.path.split("/").pop())}</code> (+${f.add}/-${f.del})`).join(", ")}`);
  if (p.related?.length) items.push(`${icon("cog", "ico sm")}Shares files with ${p.related.slice(0, 5).map(r => `#${r}`).join(", ")}`);
  return `<details class="watch"><summary>${icon("target", "ico sm")}What to look at <span class="muted">(worked out by rules, no model)</span></summary><ul>${items.map(i => `<li>${i}</li>`).join("")}</ul></details>`;
}
// Who wrote an entry, as precisely as the log knows: the model named on it, else the runtime that carried it,
// else the kind of author. Never the current configuration: switching runtimes must not relabel old entries.
const author = e => e.by === "you" ? "You" : e.by === "rules" ? `${icon("cog", "ico sm")}rules` : `${icon("sparkles", "ico sm")}${esc(e.model || e.runtime || "model")}`;
// What will answer next, from the engine: the configured model if the runtime names one, else the runtime.
const engineLabel = eng => eng.name === "rules" ? `${icon("cog", "ico sm")}rules` : `${icon("sparkles", "ico sm")}${esc(eng.model || eng.runtime || "model")}`;
function studyTab(n){
  if (!LIVE) return `<p class="muted" style="margin-top:14px">Studying needs the live dashboard: run <code>readthrough serve</code> (or <code>readthrough install server</code>). In a terminal, <code>readthrough study ${n}</code> shows the evidence collected so far.</p>`;
  const st = study[n];
  if (!st || st.loading && !st.entries) { loadStudy(n); return `<p class="muted">Loading…</p>`; }
  if (st.error) return `<p class="muted">Couldn't load the study log: ${esc(st.error)}</p>`;
  const s = st.summary, entries = st.entries, busy = st.engine.busy || !!st.pending, ai = st.engine.available;
  const p = byNum[n], dis = busy || !ai ? "disabled" : "";
  const answers = {}, assessments = {}, pinned = new Set();
  entries.forEach(e => { if (e.kind === "answer") answers[e.qid] = e; if (e.kind === "assessment") assessments[e.qid] = e; if (e.kind === "note" && e.pinned) pinned.add(e.pinned); });
  const {mode, q: openQ} = studyMode(entries);
  const pin = e => pinned.has(e.id) ? `<span class="pin on" title="Kept in the brief">${icon("pin", "ico sm")}pinned</span>`
    : `<button class="pin" data-pin="${e.id}" title="Keep this in the brief's study log">${icon("pin", "ico sm")}pin</button>`;
  const who = author;
  const qcard = q => { const a = answers[q.qid], v = assessments[q.qid];
    return `<div class="qcard ${q === openQ ? "open" : ""}"><div class="qhead"><b>${esc(q.qid)}</b><span>${esc(q.focus || "")}</span>${v ? `<span class="verdict ${v.verdict}">${v.verdict}</span>` : q === openQ ? `<span class="tag">answer below</span>` : ""}</div>
      <div class="md">${md(q.question)}</div>
      ${a ? `<div class="answer md">${md(a.text)}</div>` : ""}
      ${v ? `<div class="md">${md(v.feedback)}</div>${v.evidence?.length ? `<div class="hint">Evidence: ${v.evidence.map(x => `<code>${esc(x)}</code>`).join(" ")}</div>` : ""}` : ""}
      ${a && q.expected_points?.length ? `<details class="hint"><summary>What a good answer covers</summary><ul>${q.expected_points.map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}</div>`; };
  const timeline = entries.map(e => {
    switch (e.kind) {
      case "prediction": return e.skipped ? `<div class="event">You skipped the prediction.</div>`
        : `<div class="bubble you"><div class="who-line">You <span class="tag">prediction</span></div><div class="md">${md(e.text)}</div></div>`;
      case "chat":
        if (e.action === "brief") return `<div class="event">${icon("lightbulb", "ico sm")}You asked for a briefing.</div>`;
        return `<div class="bubble ${e.by}"><div class="who-line">${who(e)} ${pin(e)}</div><div class="md">${md(e.text)}</div></div>`;
      case "question": return qcard(e);
      case "teachback": return `<div class="bubble you"><div class="who-line">You <span class="tag">teach-back</span></div><div class="md">${md(e.text)}</div></div>`;
      case "reflection": return `<div class="bubble you"><div class="who-line">You <span class="tag">what I missed${e.qid ? ` on ${esc(e.qid)}` : ""}</span></div><div class="md">${md(e.text)}</div></div>`;
      case "teachback_assessment": return `<div class="qcard"><div class="qhead"><b>Teach-back</b><span class="verdict ${e.verdict}">${e.verdict}</span></div><div class="md">${md(e.feedback)}</div>${e.gaps?.length ? `<ul>${e.gaps.map(g => `<li>${esc(g)}</li>`).join("")}</ul>` : ""}<div class="who-line">${pin({...e, text: e.feedback})}</div></div>`;
      case "note": return `<div class="event">${icon("pin", "ico sm")}${e.pinned ? "Pinned" : e.by === "you" ? "Note" : `${esc(e.model || e.runtime || e.by)} added to the brief`}: <span class="md">${md(e.text.length > 160 ? e.text.slice(0, 160) + "…" : e.text)}</span></div>`;
      // A failure is named by the runtime, never a model: a model that never answered reached nothing.
      case "failure": return `<div class="event warn">${icon("triangle-alert", "ico sm")}${esc(e.runtime ? `The ${e.runtime} runtime` : e.assistant === "rules" ? "The rules assistant" : "The assistant")} didn't answer: <code>${esc(e.text.length > 200 ? e.text.slice(0, 200) + "…" : e.text)}</code></div>`;
      case "finding": return `<div class="event"><span class="flag ${e.severity === "blocker" ? "blocker" : "risk"}">${esc(e.severity)}</span> ${e.path ? `<code>${esc(e.path)}:${e.line ?? "?"}</code> ` : ""}${esc(e.summary)} <span class="muted">(${e.by})</span></div>`;
      default: return "";
    }
  }).join("");
  const pending = st.pending ? `<div class="bubble model" id="pending"><div class="who-line">${engineLabel(st.engine)}</div><div class="md typing" id="pending-text">${md(st.pending.text || "")}</div><div class="tools" id="pending-tools">${pendingTools(st.pending)}</div></div>` : "";
  const step = (label, done, detail) => `<span class="step ${done ? "done" : ""}">${icon(done ? "circle-check" : "circle", "ico sm")}${label}${detail ? ` <span class="muted">${detail}</span>` : ""}</span>`;
  const stepper = `<div class="stepper">${step("Predict", s.predictions || s.prediction_skipped, s.prediction_skipped && !s.predictions ? "skipped" : "")}<span class="line"></span>
    ${step("Quiz", s.assessed, s.assessed ? `${s.correct}/${s.assessed} correct` : "")}<span class="line"></span>${step("Teach back", s.teachback, s.teachback || "")}</div>`;

  const draft = mode === "answer" ? `answer:${openQ.qid}` : mode === "predict" ? "prediction" : "chat";
  const placeholder = {predict: "Before reading the diff: which components will it touch, what's the riskiest part, and what test would you want to see?",
    answer: `Your answer to ${openQ?.qid}. Think it through; it's fine to be wrong.`, chat: "Ask about the change, argue a point, or write your teach-back…"}[mode];
  // The last verdict that wasn't "correct" is what a "what I missed" note is about; a teach-back's has no qid.
  const missed = [...entries].reverse().find(e => (e.kind === "assessment" || e.kind === "teachback_assessment") && e.verdict !== "correct");
  const reflect = missed ? `<button class="btn" data-study-add="reflection" data-qid="${esc(missed.qid || "")}" title="Save what's in the box as what you missed${missed.qid ? ` on ${esc(missed.qid)}` : ""}, in your own words. It goes in the brief.">What I missed</button>` : "";
  const buttons = {
    predict: `<button class="btn primary" data-study-add="prediction">Save prediction</button><button class="btn" data-skip-predict title="Go straight to the quiz or a briefing. The log notes that you skipped.">Skip</button>`,
    answer: `<button class="btn primary" data-ask="answer" data-qid="${esc(openQ?.qid)}" ${dis}>Submit answer</button><button class="btn" data-ask="hint" data-qid="${esc(openQ?.qid)}" ${dis}>${icon("lightbulb", "ico sm")}Hint</button>`,
    chat: `<button class="btn primary" data-ask="chat" ${dis} title="Send what's in the box">Send</button><button class="btn" data-ask="quiz" ${dis} title="One question at a time; you answer here and it's graded against the diff">${icon("graduation-cap", "ico sm")}${entries.some(e => e.kind === "question") ? "Next question" : "Quiz me"}</button><button class="btn" data-ask="brief" ${dis} title="A short summary and what to watch for. The log notes that you asked.">${icon("lightbulb", "ico sm")}Brief me</button><button class="btn" data-ask="teachback" ${dis} title="What's in the box is graded as your summary of the whole PR">Teach back</button>${reflect}`,
  }[mode];
  // What answers here, from the engine and never from an old entry: the model if the runtime names one up front,
  // else the runtime (GitHub Copilot picks its model per call; each reply then names it).
  const engineNote = ai ? (st.engine.name === "rules"
      ? `Questions come from the repo's profile, not a model; nothing here is graded.`
      : `Questions and grades come from <b title="${esc(st.engine.describe || "")}">${esc(st.engine.model || `the ${st.engine.runtime} runtime`)}</b>; each reply names the model that wrote it.`) : "";
  return `${stepper}
    ${ai ? "" : `<div class="banner warn">${esc(st.engine.reason)}</div>`}
    ${mode === "predict" ? "" : watchList(p)}
    <div class="transcript">${timeline || `<p class="hint">Write your prediction first: it's the cheapest way to find out what you don't know. Then ask for a question, or for a briefing.</p>`}${pending}</div>
    <p class="hint">Everything here is appended to this PR's study log, which the brief and the terminal deep review read. ${engineNote} ${icon("sparkles", "ico sm")}marks what a model wrote; it can read the PR's code but can't change anything.</p>
    <div class="composer">
      <textarea data-draft="${esc(draft)}" placeholder="${esc(placeholder)}"></textarea>
      <div class="actions">${buttons}<span class="hint">Ctrl+Enter ${mode === "predict" ? "saves" : mode === "answer" ? "submits" : "sends"}</span></div>
    </div>`;
}
const pendingTools = pend => pend.tools.length ? "used " + [...new Set(pend.tools)].map(t => `<code>${esc(t)}</code>`).join(", ") : (pend.text ? "" : "thinking…");
async function studyPost(n, body){
  try { await api(`/api/study/${n}`, body); loadStudy(n); } catch (err) { toast(err.message); }
}
async function studyAdd(n, kind, qid){
  // A reflection is written in the chat box; the other kinds each have a box of their own.
  const key = `${n}:${kind === "reflection" ? "chat" : kind}`, text = (state.drafts[key] || "").trim();
  if (!text) return toast("Write something first");
  try { await api(`/api/study/${n}`, {kind, text, ...(qid ? {qid} : {})}); state.drafts[key] = ""; saveDrafts(); renderDrawer({follow: true}); }
  catch (err) { toast(err.message); }
}
async function studyAsk(n, action, qid){
  const key = action === "answer" ? `${n}:answer:${qid}` : `${n}:chat`;
  const text = ["chat", "answer", "teachback"].includes(action) ? (state.drafts[key] || "").trim() : "";
  if (["chat", "answer", "teachback"].includes(action) && !text) return toast("Write something first");
  try {
    await api(`/api/study/${n}/ask`, {action, text, qid});
    if (text) { state.drafts[key] = ""; saveDrafts(); }
    study[n].pending = {text: "", tools: []};
    renderDrawer({follow: true});
  } catch (err) { toast(err.message); }
}
function onStudyEvent(ev){
  const n = ev.pr, st = study[n];
  if (ev.summary && byNum[n]) byNum[n].study = ev.summary;
  if (!st || !st.entries) { if (ev.type === "entry" || ev.type === "reload") render(); return; }
  const here = state.sel === n && state.dtab === "study";
  if (ev.type === "start") { st.pending ||= {text: "", tools: []}; st.engine.busy = true; }
  else if (ev.type === "delta") {
    st.pending ||= {text: "", tools: []}; st.pending.text += ev.text;
    const el = $("pending-text");
    if (here && el) {
      const body = drawerBody(), pinned = nearEnd(body);
      el.innerHTML = md(st.pending.text); $("pending-tools").innerHTML = pendingTools(st.pending);
      if (pinned) body.scrollTop = body.scrollHeight;
      return;
    }
  } else if (ev.type === "tool") {
    st.pending ||= {text: "", tools: []}; st.pending.tools.push(ev.name);
    if (here && $("pending-tools")) { $("pending-tools").innerHTML = pendingTools(st.pending); return; }
  } else if (ev.type === "entry") {
    if (!st.entries.some(e => e.id === ev.entry.id)) st.entries.push(ev.entry);
    st.summary = ev.summary;
    if (ev.entry.kind === "chat" && ev.entry.by !== "you") st.pending = null;
    if (ev.entry.kind === "answer") { const q = st.entries.find(e => e.kind === "question" && e.qid === ev.entry.qid); if (q) loadStudy(n); }
  } else if (ev.type === "done" || ev.type === "error") {
    st.pending = null; st.engine.busy = false;
    if (ev.type === "error") toast("Study: " + ev.message);
  } else if (ev.type === "reload") { loadStudy(n); }
  if (here) renderDrawer(); else if (ev.type === "entry") render();
}

// ---- render + events
function render(){
  header();
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("on", v.id === "v-" + state.tab));
  ({map: renderMap, plan: renderPlan, overlap: renderOverlap, table: renderTable})[state.tab]();
  if (state.sel) renderDrawer();
}
document.addEventListener("click", e => {
  const t = e.target.closest("[data-tab],[data-lane],[data-author],[data-ci],[data-clear],[data-open],[data-deep],[data-copy],[data-sort],[data-review],[data-refresh],[data-dtab],[data-ask],[data-study-add],[data-skip-predict],[data-pin],.chip,.brick,#close,#fit,#theme");
  if (!t) return;
  if (t.id === "close") return closeDrawer();
  if (t.id === "theme") { themePref = THEMES[(THEMES.indexOf(themePref) + 1) % THEMES.length]; localStorage.setItem("readthrough:theme", themePref); return applyTheme(); }
  if (t.id === "fit") return cy && cy.fit(undefined, 24);
  if (t.dataset.review) return startReview(+t.dataset.review, t);
  if (t.hasAttribute("data-refresh")) return api("/api/refresh").then(() => toast("Refreshing…")).catch(err => toast(err.message));
  if (t.dataset.dtab) { state.dtab = t.dataset.dtab; return renderDrawer({resetScroll: true}); }
  if (t.dataset.ask) return studyAsk(state.sel, t.dataset.ask, t.dataset.qid);
  if (t.dataset.studyAdd) return studyAdd(state.sel, t.dataset.studyAdd, t.dataset.qid || null);
  if (t.hasAttribute("data-skip-predict")) return studyPost(state.sel, {kind: "prediction", skipped: true});
  if (t.dataset.pin) { const e = study[state.sel]?.entries.find(x => x.id === +t.dataset.pin); if (e) return studyPost(state.sel, {kind: "note", text: e.text || e.feedback, pinned: e.id}); return; }
  if (t.dataset.tab) { state.tab = t.dataset.tab; return render(); }
  if (t.dataset.lane) return pick("lanes", t.dataset.lane, e);
  if (t.dataset.author) return pick("authors", t.dataset.author, e);
  if (t.dataset.ci) return pick("cis", t.dataset.ci, e);
  if (t.hasAttribute("data-clear")) { for (const [g, labels] of Object.entries(GROUPS)) Object.keys(labels).forEach(k => state[g].add(k)); return render(); }
  if (t.dataset.deep) return copy(deepPrompt(t.dataset.deep), "Prompt copied. Paste it into any agent chat (Copilot, Claude Code, VS Code)");
  if (t.dataset.copy) return copy(t.dataset.copy);
  if (t.dataset.sort) { sortDir = sortKey === t.dataset.sort ? -sortDir : (t.dataset.sort === "ci" ? 1 : -1); sortKey = t.dataset.sort; return render(); }
  if (t.dataset.open || t.dataset.pr) return openDrawer(+(t.dataset.open || t.dataset.pr));
  if (t.dataset.comp) { state.comp = state.comp === t.dataset.comp ? null : t.dataset.comp; return render(); }
});
document.addEventListener("input", e => { const d = e.target.dataset?.draft; if (d && state.sel) { state.drafts[`${state.sel}:${d}`] = e.target.value; saveDrafts(); } });
document.addEventListener("change", e => {
  const t = e.target;
  if (t.id === "repoSwitch") { location.href = t.value === "__setup" ? "/setup" : `/r/${encodeURI(t.value)}/`; return; }
  if (t.id === "srcOnly") { state.srcOnly = t.checked; return render(); }
  if (t.id === "overlapOnly") { state.overlapOnly = t.checked; return render(); }
  if (t.id === "greenFirst") { state.greenFirst = t.checked; localStorage.setItem("readthrough:green-first", t.checked ? "1" : "0"); return render(); }
  if (!t.dataset.status) return;
  const n = t.dataset.status, status = t.value || null, g = progress[n] ||= {};
  Object.assign(g, {status, understood: UNDERSTOOD.includes(status), reviewed: REVIEWED.includes(status), head: byNum[n].head});
  if (LIVE) api(`/api/ledger/${n}`, {status}).then(entries => { DATA.ledger = entries; loadProgress(); render(); }).catch(err => toast(err.message));
  save(); render();
});
document.addEventListener("mouseover", e => {
  const c = e.target.closest(".chip[data-pr]"), map = $("v-map");
  map.classList.toggle("dim", !!c && map.contains(c));
  map.querySelectorAll(".chip.hl").forEach(x => x.classList.remove("hl"));
  if (c && map.contains(c)) map.querySelectorAll(`.chip[data-pr="${c.dataset.pr}"]`).forEach(x => x.classList.add("hl"));
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeDrawer();
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target.dataset?.draft && state.sel) {
    const d = e.target.dataset.draft;
    if (d.startsWith("answer:")) studyAsk(state.sel, "answer", d.slice(7));
    else if (d === "chat") studyAsk(state.sel, "chat");
    else studyAdd(state.sel, d);
  }
});
$("q").value = state.q;
$("q").addEventListener("input", e => { state.q = e.target.value; render(); });
new ResizeObserver(() => document.documentElement.style.setProperty("--hdr", document.querySelector("header").offsetHeight + "px")).observe(document.querySelector("header"));
addEventListener("resize", renderTabs);
async function startReview(n, button){
  button.disabled = true;
  try { const r = await api(`/api/review/${n}`, button.dataset.session ? {session: button.dataset.session} : {}); toast(r.resuming ? `Resuming the #${n} session in a new terminal` : `Starting a deep review of #${n} in a new terminal`); }
  catch (err) { toast(err.message); }
  finally { button.disabled = false; }
}
// Live mode: the server pushes changes. Re-render in place so tabs, filters, drafts, and the open panel survive.
function connectLive(){
  const es = new EventSource((LIVE.base || "") + "/api/events");
  // A restarted server has a new token; reload so the buttons keep working. Drafts survive in sessionStorage.
  es.addEventListener("hello", e => { if (JSON.parse(e.data).instance !== LIVE.instance) { es.close(); location.reload(); } });
  es.onopen = () => { if (LIVE.disconnected) { LIVE.disconnected = false; reloadData(); } header(); };
  es.onerror = () => { LIVE.disconnected = true; header(); };
  es.addEventListener("status", e => { LIVE.status = JSON.parse(e.data); header(); });
  es.addEventListener("data", () => reloadData(true));
  es.addEventListener("ledger", e => { DATA.ledger = JSON.parse(e.data); loadProgress(); render(); });
  es.addEventListener("sessions", e => { for (const [n, info] of Object.entries(JSON.parse(e.data))) if (byNum[n]) Object.assign(byNum[n], info); render(); });
  es.addEventListener("study", e => onStudyEvent(JSON.parse(e.data)));
}
async function reloadData(announce){
  const resp = await fetch((LIVE.base || "") + "/api/data", {cache: "no-store"});
  if (!resp.ok) return;
  const before = new Set(DATA.prs.map(p => p.number));
  setData(await resp.json());
  for (const k of Object.keys(briefs)) delete briefs[k];
  render();
  const added = DATA.prs.filter(p => !before.has(p.number)).length;
  if (announce) toast(added ? `Updated: ${added} new PR${added > 1 ? "s" : ""}` : "Updated");
}
applyTheme();
render();
{ const panel = JSON.parse(sessionStorage.getItem(STORE + ":panel") || "null"); if (panel && byNum[panel.sel]) openDrawer(panel.sel, panel.dtab); }
if (LIVE) connectLive();
