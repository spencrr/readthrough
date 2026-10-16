const LIVE = /*__READTHROUGH_LIVE__*/null;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const icon = (name, cls = "ico") => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${LUCIDE[name] || ""}</svg>`;
const theme = localStorage.getItem("readthrough:theme") || "auto";
document.documentElement.dataset.theme = theme === "auto" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : theme;
$("logo").innerHTML = `<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><rect x="5.5" y="3.5" width="21" height="25" rx="3" fill="none" stroke="currentColor" stroke-opacity=".35"/><rect x="7.5" y="7.25" width="17" height="5.5" rx="2.75" fill="currentColor" fill-opacity=".16"/><g stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-opacity=".45"><line x1="10" y1="10" x2="21" y2="10"/><line x1="10" y1="14.5" x2="18" y2="14.5"/><line x1="10" y1="19" x2="21" y2="19"/><line x1="10" y1="23.5" x2="16" y2="23.5"/></g></svg>`;
function toast(msg){ const t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("on"), 2600); }
async function api(path, body){
  const opts = body === undefined ? {cache: "no-store"} : {method: "POST", headers: {"Content-Type": "application/json", "X-Readthrough-Token": LIVE.token}, body: JSON.stringify(body)};
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || resp.statusText);
  return data;
}
async function poll(job, onLog){
  for (;;) {
    const j = await api(`/api/setup/jobs/${job}`);
    onLog(j.log);
    if (j.status === "done") return j.result;
    if (j.status === "error") throw new Error(j.error);
    await new Promise(r => setTimeout(r, 900));
  }
}
const slug = s => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "part";
const label = s => { const n = s.split("/").pop().replace(/[_-]+/g, " "); return n.charAt(0).toUpperCase() + n.slice(1); };
const KIND_LABEL = {internal: "internal", external: "external", "first-time": "first-time", bot: "bot", unknown: "unknown"};

// ---- state
// A repo is referred to by its slug (forge/owner/name) everywhere the server is involved; the sidebar and
// headings show its plain name, with the forge named only when it isn't GitHub.
const S = {state: null, sel: new URLSearchParams(location.search).get("repo") || null, w: null};
const same = (a, b) => !!a && !!b && a.toLowerCase() === b.toLowerCase();
const isSel = r => same(S.sel, r.slug) || same(S.sel, r.name);
const forgeOf = name => (S.state?.forges || []).find(f => f.name === name) || {name, label: name, shape: "owner/name", accounts: false, pr_noun: "pull request"};
const forgeTag = r => r.forge && r.forge !== "github" ? ` <span class="sub" style="margin:0;font-weight:400">on ${esc(r.forge_label)}</span>` : "";
async function load(){
  S.state = await api(`/api/setup/state${S.sel ? `?repo=${encodeURIComponent(S.sel)}` : ""}`);
  render();
}
function render(){
  const st = S.state, repos = st.repos;
  $("back").innerHTML = repos.some(r => r.onboarded) ? `<a class="btn" href="/">${icon("map")}Dashboards</a>` : "";
  $("nav").innerHTML = repos.map(r => `<button class="ritem ${isSel(r) ? "on" : ""}" data-repo="${esc(r.slug)}"><span class="dot ${r.onboarded ? "ok" : "todo"}" title="${r.onboarded ? "Onboarded" : "Not onboarded yet"}"></span><span class="n">${esc(r.name)}${forgeTag(r)}</span>${same(st.settings.default_repo, r.cli_name) ? '<span style="font-size:11px;color:var(--mut)">default</span>' : ""}</button>`).join("")
    + `<button class="ritem ${S.sel ? "" : "on"}" data-new>${icon("plus")}<span class="n">Onboard a repo</span></button>`
    + `<div style="margin-top:14px">${globalCard()}</div>`;
  const repo = st.repo;
  if (!S.sel || !repo || !repo.onboarded) {
    renderWizard(repo);
    if (S.w.repo && !S.w.info && !S.w.tried) { S.w.tried = true; act({dataset: {act: "inspect"}}); }
  } else renderSettings(repo);
}

// ---- global settings (in the side bar)
function globalCard(){
  const st = S.state.settings, onboarded = S.state.repos.filter(r => r.onboarded);
  return `<div class="card" style="padding:10px 12px"><h2 style="font-size:13px">${icon("settings", "ico sm")}Everywhere</h2>
    <label class="f">Default repo<select id="g-default"><option value="">(none)</option>${onboarded.map(r => `<option value="${esc(r.slug)}" ${same(st.default_repo, r.cli_name) ? "selected" : ""}>${esc(r.cli_name)}</option>`).join("")}</select></label>
    <label class="f" style="margin-top:6px">Refresh every (minutes)<input type="number" id="g-refresh" min="1" max="1440" value="${st.refresh_minutes}" style="width:90px"></label>
    <div class="row"><button class="btn" data-act="save-global">Save</button></div>
    <div class="cli" style="margin-top:4px">Port <b>${st.port}</b> (change it in <code>config.toml</code>, then restart the server)</div></div>`;
}

// ---- onboarding wizard
function renderWizard(repo){
  const w = S.w ||= {repo: repo?.name || "", forge: repo?.forge || "github", account: repo?.account || "", clone: repo?.clone || "", remote: repo?.remote || "", info: null, cloneInfo: null, toml: repo?.profile_text || "", report: null, log: [], busy: false, editable: true, source: repo?.source || null};
  const accounts = S.state.accounts, forges = S.state.forges, forge = forgeOf(w.forge);
  const cli = `readthrough onboard ${w.repo || forge.shape.split(" ")[0]}${w.forge !== "github" ? ` --forge ${w.forge}` : ""}${w.account ? ` --account ${w.account}` : ""}${w.clone ? ` --clone "${w.clone}"` : ""}`;
  const step = (n, done) => `<span class="step ${done ? "done" : ""}">${done ? "✓" : n}</span>`;
  const info = w.info;
  $("main").innerHTML = `
  <div class="card"><h2>${step(1, !!info)}Which repo?</h2>
    <p class="sub">GitHub is read through the gh CLI (pick the account that can see the repo if you're signed in to several). Other forges are read through their API with a token from their own CLI or an environment variable, or anonymously for public repos.</p>
    <div class="row"><label class="f">Where<select id="w-forge">${forges.map(f => `<option value="${esc(f.name)}" ${w.forge === f.name ? "selected" : ""} title="${esc(f.summary)}">${esc(f.label)}</option>`).join("")}</select></label>
      <label class="f">Repo<input type="text" id="w-repo" placeholder="${esc(forge.shape.split(" ")[0])}" value="${esc(w.repo)}" autocomplete="off" spellcheck="false"></label>
      ${forge.accounts ? `<label class="f">Account<select id="w-account"><option value="">gh's active account</option>${accounts.map(a => `<option ${w.account === a ? "selected" : ""}>${esc(a)}</option>`).join("")}</select></label>` : ""}
      <button class="btn primary" data-act="inspect" style="align-self:flex-end">Check access</button></div>
    ${info ? `<div class="msg ok">${icon("circle-check", "ico sm")}<b>${esc(info.full_name)}</b> on ${esc(info.forge_label)} · ${info.private ? "private" : "public"} · default branch <code>${esc(info.default_branch)}</code>${info.description ? ` · ${esc(info.description)}` : ""}</div>` : ""}
    ${w.err1 ? `<div class="msg err">${esc(w.err1)}</div>` : ""}</div>

  <div class="card ${info ? "" : "dim"}"><h2>${step(2, w.cloneInfo?.ok)}Your local clone</h2>
    <p class="sub">Deep reviews and study sessions check PRs out next to it in their own worktrees; your checkout isn't touched. Optional for just the map.</p>
    <div class="row"><label class="f">Path<input type="text" class="wide" id="w-clone" placeholder="C:\\Users\\you\\src\\repo" value="${esc(w.clone)}" spellcheck="false"></label>
      <button class="btn" data-act="clone" style="align-self:flex-end">Check</button>
      ${w.cloneInfo?.remotes?.length ? `<label class="f">Remote for PRs<select id="w-remote">${w.cloneInfo.remotes.map(r => `<option value="${esc(r.name)}" ${(w.remote || w.cloneInfo.detected) === r.name ? "selected" : ""}>${esc(r.name)} · ${esc(r.url)}</option>`).join("")}</select></label>` : ""}</div>
    ${w.cloneInfo ? `<div class="msg ${w.cloneInfo.ok ? "ok" : ""}">${esc(w.cloneInfo.message)}</div>` : ""}${w.err2 ? `<div class="msg err">${esc(w.err2)}</div>` : ""}</div>

  <div class="card ${info ? "" : "dim"}"><h2>${step(3, !!w.toml)}Its architecture profile</h2>
    <p class="sub">The map, the contracts in each PR's brief, and Copilot's questions all come from this. ${w.source ? `Found one: <b>${esc(w.source.label)}</b>.` : "There isn't one yet, so draft one."}</p>
    <div class="row">${w.source ? `<button class="btn primary" data-act="draft" data-mode="source" ${w.busy ? "disabled" : ""}>Use ${w.source.kind === "shared" ? "the shared" : w.source.kind === "repo" ? "the repo's" : "the existing"} profile</button>` : ""}
      <button class="btn ${w.source ? "" : "primary"}" data-act="draft" data-mode="plain" ${w.busy ? "disabled" : ""}>${icon("layers", "ico sm")}Draft from the file tree</button>
      <button class="btn" data-act="draft" data-mode="copilot" ${w.busy ? "disabled" : ""} title="Copilot reads the code and docs and proposes a profile; each proposal is checked like Accept does">${icon("sparkles", "ico sm")}Draft with Copilot</button>
      ${w.busy ? '<span class="spin"></span>' : ""}</div>
    ${w.log.length ? `<div class="log" id="w-log">${w.log.map(l => `<div>${esc(l)}</div>`).join("")}</div>` : ""}${w.err3 ? `<div class="msg err">${esc(w.err3)}</div>` : ""}</div>

  ${w.toml ? editorCard(w, "w", {onboarding: true}) : ""}
  <div class="cli">Same thing from a terminal: <code>${esc(cli)}</code></div>`;
  const log = $("w-log"); if (log) log.scrollTop = log.scrollHeight;
}

// ---- the profile editor with its live report (shared by onboarding and settings)
function editorCard(e, key, {onboarding = false, editable = true} = {}){
  const r = e.report, ok = r?.valid && !r.problems.length;
  const share = r?.valid ? Math.round(r.share * 100) : 0;
  const report = !r ? '<p class="sub">Checking…</p>' : !r.valid ? `<div class="msg err">${esc(r.error)}</div>` : `
    <div><b>${share}%</b> of ${r.total} ${esc(r.basis)} map to a component <span class="sub">(needs ${Math.round(r.need * 100)}%)</span></div>
    <div class="meter"><span class="${share < r.need * 100 ? "low" : ""}" style="width:${share}%"></span><i style="left:${r.need * 100}%"></i></div>
    ${r.problems.length ? `<h3>Before accepting</h3><ul class="problems">${r.problems.map(p => `<li>${esc(p)}</li>`).join("")}</ul>` : `<div class="msg ok">${icon("circle-check", "ico sm")}${onboarding ? "Ready to accept." : "All checks pass."}</div>`}
    ${r.unmapped.length ? `<h3>Not mapped yet</h3><ul>${r.unmapped.map(([p, n]) => `<li><code>${esc(p)}</code> ${n} ${editable ? `<button class="btn" style="padding:0 6px;font-size:12px" data-act="add-comp" data-key="${key}" data-path="${esc(p)}" title="Add a component for this path to the profile">+ component</button>` : ""}</li>`).join("")}</ul>` : ""}
    <h3>Busiest components</h3><div style="font-size:13px">${r.busiest.map(([l, n]) => `${esc(l)} <span class="sub">${n}</span>`).join(" · ")}</div>
    <h3>Recent authors</h3>${authorsTable(r.authors, key)}`;
  return `<div class="card"><h2>${onboarding ? `<span class="step ${ok ? "done" : ""}">${ok ? "✓" : 4}</span>` : ""}${onboarding ? "Check it, edit it, accept it" : "Profile"}</h2>
    <p class="sub">${editable ? "Edit the TOML on the left; the report re-checks as you type." : `This profile comes from <b>${esc(e.source?.label)}</b>. Customize it to make a local copy you can edit; yours then takes precedence.`}
      <a href="https://github.com/spencrr/readthrough/blob/main/docs/profiles.md" target="_blank" rel="noopener">How to write a good one</a></p>
    <div class="editor"><textarea class="toml" id="${key}-toml" spellcheck="false" ${editable ? "" : "readonly"}>${esc(e.toml)}</textarea><div class="report" id="${key}-report">${report}</div></div>
    <div class="row">${editable ? `<button class="btn" data-act="save" data-key="${key}">Save draft</button>` : `<button class="btn primary" data-act="customize" data-key="${key}">Customize</button>`}
      <button class="btn" data-act="preview" data-key="${key}" ${e.previewing ? "disabled" : ""}>${icon("external-link", "ico sm")}Preview the map</button>
      ${onboarding ? `<button class="btn primary" data-act="accept" data-key="${key}" ${r?.valid ? "" : "disabled"} title="${ok ? "Finish onboarding and start the dashboard" : "Fix the items in the report first, or tick accept anyway"}">Accept and start</button>
        ${r?.valid && r.problems.length ? `<label style="font-size:13px"><input type="checkbox" id="${key}-force"> accept anyway</label>` : ""}` : ""}
      <button class="btn" data-act="upstream" data-key="${key}" title="Open the forge's new-file page for readthrough.toml so the maintainers can own this map">${icon("git-pull-request", "ico sm")}Propose upstream</button>
      ${e.previewing ? '<span class="spin"></span>' : ""}</div></div>`;
}
function authorsTable(rows, key){
  if (!rows.length) return '<p class="sub">No recent authors.</p>';
  const repo = currentRepoView(), int = new Set(repo?.internal_authors || []), ext = new Set(repo?.external_authors || []);
  const can = !!repo?.slug && S.state.repos.some(x => x.slug === repo.slug);
  const sorted = [...rows].sort((a, b) => b[3] - a[3]);
  const row = ([login, assoc, kind, n]) => {
    const forced = int.has(login) ? "internal" : ext.has(login) ? "external" : "";
    return `<tr><td title="${esc(assoc.toLowerCase())}, ${n} PR${n > 1 ? "s" : ""}">@${esc(login)} <span class="sub">${n}</span></td><td class="k-${esc(kind)}">${esc(KIND_LABEL[kind] || kind)}</td>
      <td>${can ? `<select data-override="${esc(login)}" aria-label="Override for ${esc(login)}" title="Your own override, kept in config.toml">${["", "internal", "external"].map(v => `<option value="${v}" ${forced === v ? "selected" : ""}>${v || "auto"}</option>`).join("")}</select>` : ""}</td></tr>`; };
  const counts = rows.reduce((m, r) => (m[r[2]] = (m[r[2]] || 0) + 1, m), {});
  return `<p class="sub" style="margin:0 0 4px">${Object.entries(counts).map(([k, n]) => `<span class="k-${esc(k)}">${n} ${esc(KIND_LABEL[k] || k)}</span>`).join(" · ")}. Override anyone who's classified wrong.</p>
    <table class="authors"><tbody>${sorted.slice(0, 12).map(row).join("")}</tbody></table>
    ${sorted.length > 12 ? `<details><summary class="sub">${sorted.length - 12} more</summary><table class="authors"><tbody>${sorted.slice(12).map(row).join("")}</tbody></table></details>` : ""}`;
}
const currentRepoView = () => { const want = S.sel || S.w?.info?.slug || ""; return S.state.repos.find(r => same(want, r.slug) || same(want, r.name)); };
const editorState = key => key === "w" ? S.w : S.e;
let checkTimer = null;
function scheduleCheck(key){
  clearTimeout(checkTimer);
  checkTimer = setTimeout(async () => {
    const e = editorState(key), repo = key === "w" ? e.info?.slug : S.sel;
    if (!repo) return;
    try { e.report = await api("/api/setup/check", {repo, toml: e.toml}); }
    catch (err) { e.report = {valid: false, error: err.message}; }
    const box = $(`${key}-report`);
    if (box) { const html = editorCard(e, key, {onboarding: key === "w", editable: e.editable}); const tmp = document.createElement("div"); tmp.innerHTML = html; box.innerHTML = tmp.querySelector(".report").innerHTML; refreshButtons(key, tmp); }
  }, 700);
}
function refreshButtons(key, tmp){
  const row = $(`${key}-toml`)?.closest(".card")?.querySelector(".row:last-child"), fresh = tmp.querySelector(".row:last-child");
  if (row && fresh) row.innerHTML = fresh.innerHTML;
  const step = $(`${key}-toml`)?.closest(".card")?.querySelector("h2 .step"), freshStep = tmp.querySelector("h2 .step");
  if (step && freshStep) step.outerHTML = freshStep.outerHTML;
}

// ---- settings for an onboarded repo
function renderSettings(repo){
  const e = S.e && S.e.slug === repo.slug ? S.e : (S.e = {slug: repo.slug, toml: repo.profile_text, source: repo.source, editable: repo.editable, report: null, branch: null});
  const accounts = S.state.accounts, forge = forgeOf(repo.forge);
  $("main").innerHTML = `
  <div class="card"><h2>${icon("git-pull-request")}${esc(repo.name)}${forgeTag(repo)} <span class="sub" style="margin:0;font-weight:400">onboarded ${new Date(repo.onboarded).toLocaleDateString()}</span>
    <span style="flex:1"></span><a class="btn" href="/r/${esc(repo.slug)}/">${icon("map", "ico sm")}Open dashboard</a></h2></div>
  <div class="card"><h2>${icon("terminal")}Where it comes from</h2>
    <div class="row">${forge.accounts ? `<label class="f">gh account<select id="s-account"><option value="">gh's active account</option>${accounts.map(a => `<option ${repo.account === a ? "selected" : ""}>${esc(a)}</option>`).join("")}</select></label>` : `<label class="f">Forge<input type="text" value="${esc(forge.label)}" readonly style="min-width:120px"></label>`}
      <label class="f">Local clone<input type="text" class="wide" id="s-clone" value="${esc(repo.clone)}" spellcheck="false"></label>
      <label class="f">Remote<input type="text" id="s-remote" value="${esc(repo.remote || "")}" placeholder="detected" style="min-width:120px"></label></div>
    <div class="row"><button class="btn primary" data-act="save-source">Save</button><span class="sub" style="margin:0">Data lives in <code>${esc(repo.data_dir)}</code></span></div>
    ${S.msgSource ? `<div class="msg ${S.msgSource.ok ? "ok" : "err"}">${esc(S.msgSource.text)}</div>` : ""}</div>
  <div class="card"><h2>${icon("list-checks")}Reviewing</h2>
    <div class="row"><label class="f">Session length (minutes)<input type="number" id="s-minutes" min="10" max="480" value="${repo.session_minutes}" style="width:110px"></label>
      <label class="f">Most PRs to fetch<input type="number" id="s-limit" min="1" max="500" value="${repo.limit}" style="width:110px"></label>
      <label class="f">Study model<input type="text" id="s-model" value="${esc(repo.study_model || "")}" placeholder="Copilot's default" style="min-width:200px"></label></div>
    <div class="row"><button class="btn primary" data-act="save-review">Save</button></div>
    ${S.msgReview ? `<div class="msg ${S.msgReview.ok ? "ok" : "err"}">${esc(S.msgReview.text)}</div>` : ""}</div>
  ${editorCard(e, "e", {editable: e.editable})}
  <div class="card"><h2>${icon("info")}Set from the command line only</h2>
    <p class="sub">These change what the server launches or reads, so the browser can't edit them.</p>
    <dl class="ro"><dt>agent_args</dt><dd><code>${esc(JSON.stringify(repo.agent_args))}</code></dd>
      <dt>profile override</dt><dd>${repo.profile_override ? `<code>${esc(repo.profile_override)}</code>` : '<span class="sub">none</span>'}</dd></dl>
    <div class="cli">Edit them with <code>readthrough config --edit</code>.</div></div>
  <div class="card"><h2 style="color:var(--bad)">${icon("x")}Stop tracking</h2>
    <p class="sub">Removes it from the config and stops serving it. Its data (ledger, study logs, worktrees) stays unless you also delete it.</p>
    <div class="row"><label style="font-size:13px"><input type="checkbox" id="s-purge"> also delete its data and worktrees</label><button class="btn danger" data-act="remove">Remove ${esc(repo.name)}</button></div>
    <div class="cli">Same as <code>readthrough remove ${esc(repo.cli_name)}</code> (add <code>--purge</code> to delete the data)</div></div>`;
  if (!e.report) scheduleCheck("e");
}

// ---- actions
const val = id => $(id)?.value.trim() ?? "";
async function act(t){
  const a = t.dataset.act, key = t.dataset.key, e = key ? editorState(key) : null;
  const repoOf = () => key === "w" ? S.w.info?.slug : S.sel;
  try {
    if (a === "inspect") {
      const w = S.w; w.repo = val("w-repo"); w.forge = val("w-forge") || w.forge; w.account = val("w-account"); w.err1 = null;
      t.disabled = true;
      w.info = await api("/api/setup/inspect", {repo: w.repo, forge: w.forge, account: w.account || null});
      w.repo = w.info.full_name; w.clone ||= w.info.clone; w.source = w.info.source; w.toml = w.info.profile_text || ""; w.editable = true;
      if (w.info.onboarded) { location.search = `?repo=${encodeURIComponent(w.info.slug)}`; return; }
      if (w.clone) await act({dataset: {act: "clone"}});
      if (w.toml) scheduleCheck("w");
      return render();
    }
    if (a === "clone") {
      const w = S.w; w.clone = val("w-clone") || w.clone; w.err2 = null;
      try { w.cloneInfo = await api("/api/setup/clone", {repo: w.info.slug, clone: w.clone}); w.remote = w.cloneInfo.detected || ""; }
      catch (err) { w.cloneInfo = null; w.err2 = err.message; }
      return render();
    }
    if (a === "draft") {
      const w = S.w; w.busy = true; w.err3 = null; w.log = []; w.remote = val("w-remote") || w.remote;
      render();
      const {job} = await api("/api/setup/draft", {repo: w.info.slug, account: w.account || null, clone: w.cloneInfo?.ok ? w.clone : null, remote: w.cloneInfo?.ok ? w.remote : null, mode: t.dataset.mode});
      try {
        const res = await poll(job, log => { if (log.length !== w.log.length) { w.log = log; const box = $("w-log"); if (box) { box.innerHTML = log.map(l => `<div>${esc(l)}</div>`).join(""); box.scrollTop = box.scrollHeight; } else render(); } });
        w.toml = res.toml; w.report = res.report; w.editable = true;
        S.state = await api(`/api/setup/state?repo=${encodeURIComponent(w.info.slug)}`);
      } catch (err) { w.err3 = err.message; }
      w.busy = false; return render();
    }
    if (a === "add-comp") {
      const p = t.dataset.path, layer = (e.report.layers.find(l => l.source) || e.report.layers[0]).id;
      const glob = /\.[A-Za-z0-9]+$/.test(p.split("/").pop()) ? p : `${p}/**`;
      e.toml = e.toml.replace(/\s*$/, "\n") + `\n[[components]]\nid = "${slug(p)}"\nlabel = "${label(p)}"\nlayer = "${layer}"\npaths = ["${glob}"]\n# responsibility = "What this component is for."\n`;
      $(`${key}-toml`).value = e.toml; scheduleCheck(key); return toast(`Added a component for ${p}. Move it into place and name it.`);
    }
    if (a === "save") {
      await api("/api/setup/save", {repo: repoOf(), toml: e.toml});
      return toast("Saved to your local profile");
    }
    if (a === "customize") { e.editable = true; return render(); }
    if (a === "preview") {
      e.previewing = true; render();
      const win = window.open("about:blank", "_blank");
      try { const {job} = await api("/api/setup/preview", {repo: repoOf(), toml: e.toml}); const res = await poll(job, () => {}); if (win) win.location = res.url; else location.href = res.url; }
      catch (err) { if (win) win.close(); toast(err.message); }
      e.previewing = false; return render();
    }
    if (a === "accept") {
      t.disabled = true;
      const res = await api("/api/setup/accept", {repo: repoOf(), toml: e.toml, force: !!$(`${key}-force`)?.checked});
      toast("Onboarded. Building the dashboard…"); setTimeout(() => location.href = res.url, 900); return;
    }
    if (a === "upstream") {
      const info = S.w?.info || await api("/api/setup/inspect", {repo: repoOf(), account: currentRepoView()?.account || null});
      const base = info.propose_url;
      if (!base) { await navigator.clipboard.writeText(e.toml); return toast(`Profile copied: add it to the repo as ${info.profile_path}`); }
      const full = `${base}&value=${encodeURIComponent(e.toml)}`;
      if (full.length < 7000) window.open(full, "_blank", "noopener");
      else { await navigator.clipboard.writeText(e.toml); window.open(base, "_blank", "noopener"); toast(`Profile copied: paste it into the new file on ${info.forge_label}`); }
      return;
    }
    if (a === "save-source" || a === "save-review") {
      const values = a === "save-source" ? {account: val("s-account") || null, clone: val("s-clone") || null, remote: val("s-remote") || null}
        : {session_minutes: val("s-minutes"), limit: val("s-limit"), study_model: val("s-model") || null};
      const msgKey = a === "save-source" ? "msgSource" : "msgReview";
      try { await api("/api/setup/settings", {repo: S.sel, values, version: S.state.settings.version}); S[msgKey] = {ok: true, text: "Saved. The dashboard picks it up within a few seconds."}; }
      catch (err) { S[msgKey] = {ok: false, text: err.message}; }
      S.state = await api(`/api/setup/state?repo=${encodeURIComponent(S.sel)}`); return render();
    }
    if (a === "save-global") {
      await api("/api/setup/settings", {global: {default_repo: val("g-default"), refresh_minutes: val("g-refresh")}, version: S.state.settings.version});
      toast("Saved"); return load();
    }
    if (a === "remove") {
      const purge = $("s-purge").checked;
      if (!confirm(`Stop tracking ${S.sel}${purge ? " and delete its data and worktrees" : ""}?`)) return;
      await api("/api/setup/remove", {repo: S.sel, purge, version: S.state.settings.version});
      location.search = ""; return;
    }
  } catch (err) {
    toast(err.message);
    if (a === "inspect") { S.w.err1 = err.message; S.w.info = null; render(); }
  } finally { if (t.disabled !== undefined && a !== "accept") t.disabled = false; }
}
async function setOverride(login, kind){
  const repo = currentRepoView(); if (!repo) return;
  const int = new Set(repo.internal_authors), ext = new Set(repo.external_authors);
  int.delete(login); ext.delete(login);
  if (kind === "internal") int.add(login); if (kind === "external") ext.add(login);
  try {
    await api("/api/setup/settings", {repo: repo.slug, values: {internal_authors: [...int], external_authors: [...ext]}, version: S.state.settings.version});
    S.state = await api(`/api/setup/state${S.sel ? `?repo=${encodeURIComponent(S.sel)}` : ""}`);
    toast(`@${login} is ${kind || "classified automatically"} now`);
    const key = S.sel && S.state.repo?.onboarded ? "e" : "w"; scheduleCheck(key);
  } catch (err) { toast(err.message); }
}

document.addEventListener("click", ev => {
  const t = ev.target.closest("[data-act],[data-repo],[data-new]");
  if (!t) return;
  if (t.dataset.repo) { history.pushState(null, "", `?repo=${encodeURIComponent(t.dataset.repo)}`); S.sel = t.dataset.repo; S.w = null; S.e = null; S.msgSource = S.msgReview = null; return load(); }
  if (t.hasAttribute("data-new")) { history.pushState(null, "", location.pathname); S.sel = null; S.w = null; return load(); }
  act(t);
});
document.addEventListener("input", ev => {
  const id = ev.target.id;
  if (id === "w-toml" || id === "e-toml") { const key = id[0]; editorState(key).toml = ev.target.value; scheduleCheck(key); }
  if (id === "w-repo") { S.w.repo = ev.target.value; }
  if (id === "w-clone") { S.w.clone = ev.target.value; }
});
document.addEventListener("change", ev => {
  if (ev.target.dataset.override !== undefined) return setOverride(ev.target.dataset.override, ev.target.value);
  if (ev.target.id === "w-account") S.w.account = ev.target.value;
  if (ev.target.id === "w-forge") { S.w.forge = ev.target.value; S.w.account = ""; S.w.info = null; S.w.err1 = null; render(); }
  if (ev.target.id === "w-remote") S.w.remote = ev.target.value;
});
document.addEventListener("keydown", ev => { if (ev.key === "Enter" && ev.target.id === "w-repo") act({dataset: {act: "inspect"}, disabled: false}); });
addEventListener("popstate", () => { S.sel = new URLSearchParams(location.search).get("repo"); S.w = S.e = null; load(); });
load().catch(err => { $("main").innerHTML = `<div class="msg err">${esc(err.message)}</div>`; });
