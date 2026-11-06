"use strict";
// MediaExplorer frontend — v{{VERSION}}

const $ = s => document.querySelector(s);
/* Which folder row was clicked last, so a row folds only on a repeat click -- see
   renderTreeNode. Anything selected outside the tree forgets it (v1.1.1). */
let lastTreeKey = null;

/* ---------- icons (v0.1.244) ----------
   Inline SVG in place of emoji: emoji are drawn by the OS font, so the same glyph
   looked different on Windows, Android and Linux (and was a blank box where the font
   lacked it). These take the text colour (currentColor), so they follow the theme. */
const _svg = (body, extra = "") =>
  `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" ` +
  `stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"${extra}>${body}</svg>`;
const ICON = {
  folder: _svg('<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l2 2h8.8A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/>'),
  zip: _svg('<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l2 2h8.8A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/><path d="M14 8v2m0 2v2m0 2v1" stroke-width="2.2"/>'),
  film: _svg('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5v14M17 5v14M3 9.5h4M3 14.5h4M17 9.5h4M17 14.5h4"/>'),
  play: _svg('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9.2v5.6l4.6-2.8z" fill="currentColor"/>'),
  warn: _svg('<path d="M12 4 2.8 19.5h18.4z"/><path d="M12 10v4.2"/><circle cx="12" cy="17" r=".6" fill="currentColor"/>'),
  pin: _svg('<path d="M9 4h6l-1 5 3.5 3.5H6.5L10 9z"/><path d="M12 12.5V20"/>'),
  sun: _svg('<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>'),
  moon: _svg('<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>'),
  star: _svg('<path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"/>'),
  starOn: _svg('<path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z" fill="currentColor"/>'),
  x: _svg('<path d="M6 6l12 12M18 6 6 18"/>'),
};
const api = (p, opt) => fetch(p, opt).then(r => {
  // A locked library answers 401 once the password cookie lapses (or after the
  // password is changed): go back to the login screen instead of spraying red
  // "error 401" chips over a page that is about to be replaced anyway.
  if (r.status === 401) {
    if (!api._bounced) {
      api._bounced = true;
      location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    }
    throw new Error(401);
  }
  if (!r.ok) throw new Error(r.status); return r.json(); });

/* ---------- client telemetry ----------
   "I clicked a folder and nothing happened" leaves no trace: the page state and
   the failed request both vanish. Every navigation event goes into
   window.__mxlog (grab it from the console) and, when it matters, to POST
   /api/log -- the server keeps a ring buffer and writes it to docker logs;
   GET /api/log reads it back. Errors and warnings always ship; the chatty
   per-request events need ?log=1 on the page URL (remembered in localStorage,
   mx.log=0 forgets it). */
const LOG_VERBOSE = new URLSearchParams(location.search).get("log") === "1"
  || localStorage.getItem("mx.log") === "1";
if (new URLSearchParams(location.search).get("log") === "1") localStorage.setItem("mx.log", "1");
function clog(ev, data) {
  try {
    const rec = Object.assign({ ev, ms: Math.round(performance.now()), kind: state.kind,
      loc: state.loc, parent: state.parent, zip: state.zipId, nav: navToken,
      // which build made this claim: a report from an old tab used to be indistinguishable
      // from a current one, and that cost several rounds of chasing a fixed bug
      b: ((document.querySelector("#version") || {}).textContent || "").trim() }, data || {});
    (window.__mxlog = window.__mxlog || []).push(rec);
    if (window.__mxlog.length > 300) window.__mxlog.shift();
    if (!LOG_VERBOSE && !/^(err|warn)$/.test(ev)) return;
    fetch("/api/log", { method: "POST", keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(rec) }).catch(() => {});
  } catch (e) { /* logging must never break the view */ }
}

/* ---------- stale tab guard ----------
   Static assets are cached by version, so a tab opened before a deploy keeps running
   the OLD client against the NEW server -- which is how requests the current code never
   makes turn into 422s that nothing on the page can explain. The navbar's version is the
   build this page is running; /api/config is the build the server is serving. */
const BUILD = (($("#version") || {}).textContent || "").trim().replace(/^v/, "");

function checkBuild() {
  if (!BUILD) return;
  api("/api/config").then(c => {
    if (c && c.version && c.version !== BUILD) showStaleBuild(c.version);
  }).catch(() => {});
}
function showStaleBuild(live) {
  if ($("#stalebar")) return;
  const bar = div("stalebar");
  bar.id = "stalebar";
  bar.appendChild(span("", `This tab is running v${BUILD} — v${live} is live.`));
  const b = document.createElement("button");
  b.textContent = "Reload";
  b.onclick = () => location.reload();
  bar.appendChild(b);
  document.body.appendChild(bar);
}

const state = {
  loc: null,          // current location id
  parent: null,       // current folder node id (null = location root)
  kind: "loc",        // loc | dir | zip
  zipId: null,
  sort: localStorage.getItem("mx.sort") || "name",
  size: +(localStorage.getItem("mx.size") || 220),
  theme: localStorage.getItem("mx.theme") || "",
  items: [],          // current grid items
  navList: [],        // images available for viewer navigation
  treeOpen: {},       // "loc:id" -> true
  painted: null,      // {kind, loc, parent, zipId} of the view the grid is showing
  // ids this page has watched a thumbnail event for. The rows on disk stay "pending"
  // until a whole zip pack is written, so a re-list mid-build handed back pending
  // flags for entries that were already on screen: the badge counted down, jumped
  // back up (18 -> 119 on a 120-entry zip, no scan involved), then froze. An id in
  // here is done whatever a later listing says.
  built: new Set(),
  builtKey: "",
  ctxTarget: null,
  pinned: false,       // the folder on screen has its own saved view
};

/* ---------- per-folder view pins ----------
   The pin beside the slider remembers a folder's tile size, sort order and video
   frame animation. It lives server-side, keyed by path, so it survives a rescan, a
   pack purge and any other browser. An unpinned folder follows the normal global
   values (localStorage); a pin never writes those, so leaving a pinned folder takes
   the next one back to your defaults. The pin is sticky while you are inside the
   folder: moving the slider or flipping a switch there updates the pin too, because
   a pin you have to re-press is worse than one that remembers what you last did. */
let PREFS = new Map();       // "loc:rel" -> {size, sort, anim}
let curRel = "";             // rel of the folder on screen ('' = the location root)
let animOverride = null;     // set by a pin; null = follow the global switch
let prefTimer = 0;
function prefKey(loc, rel) { return loc + ":" + rel; }
function prefOf(loc, rel) { return PREFS.get(prefKey(loc, rel)) || null; }
async function loadPrefs() {
  try {
    const d = await api("/api/prefs");
    PREFS = new Map((d.items || []).map(p => [prefKey(p.loc, p.rel), p]));
  } catch (_) { PREFS = new Map(); }
}
function viewApply() {                 // slider, sort label and tile width together
  const s = $("#size-slider");
  if (s) s.value = state.size;
  $("#grid").style.setProperty("--tw", state.size + "px");
  const fg = $("#film-grid");
  if (fg) fg.style.setProperty("--tw", state.size + "px");
  const b = $("#btn-sort");
  if (b) b.textContent = state.sort === "name" ? "Name" : "Date";
}
function pinPaint() {
  const b = $("#btn-pin");
  if (!b) return;
  b.classList.toggle("pinned", !!state.pinned);
  b.title = (state.pinned
    ? "This folder remembers its own size, sort and animation — click to forget"
    : "Remember this folder's size, sort and animation") + "  (P)";
}
/* Adopt the view a folder wants as soon as its listing arrives. Returns true when
   the sort changed, because the server already ordered the rows: the caller then
   re-lists once so the first paint is in the right order rather than re-sorting in
   the browser and flickering. */
function viewAdopt(rel) {
  curRel = rel || "";
  const pref = prefOf(state.loc, curRel);
  state.pinned = !!pref;
  pinPaint();
  const memSize = +(localStorage.getItem("mx.size") || 220);
  const memSort = localStorage.getItem("mx.sort") || "name";
  state.size = (pref && pref.size) ? +pref.size : memSize;
  const sort = (pref && pref.sort) ? pref.sort : memSort;
  const sortChanged = sort !== state.sort;
  state.sort = sort;
  animOverride = (pref && pref.anim !== null && pref.anim !== undefined) ? !!pref.anim : null;
  // A pin is a VISIT, not a new default: it must not write the globals, or walking
  // through a folder pinned at 420 would leave every later folder at 420 instead of
  // the zoom the user actually set by hand (owner, 2026-11-02). The globals are
  // written only where a human moves something -- the slider, the sort button and
  // the frame switch all do that themselves.
  viewApply();
  applyFramesPref();
  return sortChanged;
}
/* A pin press or slider drag that lands while a folder is still loading would land
   on the *previous* folder: state.parent already points at the new one while the
   grid (and so curRel) still describes the old one. Ignore those instead of filing
   the change against the wrong path -- that is how the root got pinned when a folder
   was clicked and the pin pressed in the same tick. */
function navPending() {
  const p = state.painted;
  if (!p) return true;
  if (state.kind === "zip") return !(p.kind === "zip" && p.zipId === state.zipId);
  const kind = state.parent == null ? "loc" : "dir";
  return !(p.kind === kind && p.parent === state.parent && p.loc === state.loc);
}
function prefSave(now) {
  if (!state.pinned || !state.loc || navPending()) return;
  clearTimeout(prefTimer);
  const body = { loc: state.loc, rel: curRel, size: state.size, sort: state.sort,
                 anim: framesOn() ? 1 : 0 };
  // Update the in-memory pin FIRST: the sort toggle re-lists immediately, and the
  // re-list reads PREFS to decide the order -- reading the old row here made the
  // new sort snap back to the pinned one until the response landed.
  PREFS.set(prefKey(body.loc, body.rel),
            { loc: body.loc, rel: body.rel, size: body.size, sort: body.sort, anim: !!body.anim });
  const send = () => api("/api/pref", { method: "PUT",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .catch(() => {});
  if (now) send(); else prefTimer = setTimeout(send, 400);   // a drag is many inputs
}
async function prefToggle() {
  if (!state.loc || navPending()) return;
  if (!state.pinned) {
    state.pinned = true;
    pinPaint();
    prefSave(true);                        // remember exactly what is on screen now
    return;
  }
  state.pinned = false;                    // forget: back to the globals on next entry
  clearTimeout(prefTimer);                 // a drag still in flight must not re-create the pin
  pinPaint();
  PREFS.delete(prefKey(state.loc, curRel));
  try {
    await api(`/api/pref?loc=${state.loc}&rel=${encodeURIComponent(curRel)}`, { method: "DELETE" });
  } catch (_) {}
}

/* ---------- theme ---------- */
function applyTheme() {
  let t = state.theme;
  if (!t) t = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.dataset.theme = t;
  $("#btn-theme").innerHTML = t === "light" ? ICON.sun : ICON.moon;
}

/* ---------- tree ---------- */
async function buildTreeRoots() {
  const cfg = await api("/api/config");
  state.locations = cfg.locations;
  $("#app-name").textContent = cfg.name;
  // Only a locked library has something to sign out of.
  const out = $("#btn-logout");
  if (out) {
    out.hidden = !(cfg.auth && cfg.auth.enabled);
    out.onclick = () => { location.href = "/logout"; };
  }
  libPending = cfg.pending;
  updateBadge(null, libPending);
  const tree = $("#tree");
  tree.innerHTML = "";
  for (const loc of cfg.locations) {
    const n = div("tnode loc-name",
      `<span class="tw">▸</span><span class="lbl" title="${esc(loc.name)}">${ICON.folder}${esc(loc.name)}</span>`);
    n.dataset.key = `l${loc.id}`;
    n.onclick = e => {          // the caret expands the location; the row just opens it
      e.stopPropagation();
      state.loc = loc.id;
      openLocation(loc.id);
    };
    n.oncontextmenu = e => showCtx(e, { loc: loc.id, parent: null, name: loc.name });
    const caret = n.querySelector(".tw");
    if (caret) caret.onclick = e => { e.stopPropagation(); toggleNode(n, null); };
    tree.appendChild(n);
    const kids = div("tkids"); kids.hidden = true; kids._owner = n;
    tree.appendChild(kids);
    n._kids = kids;
  }
}

async function ensureNodeOpen(el, id) {
  const kids = el._kids;
  if (!kids) return false;
  kids.hidden = false;
  el.classList.add("open");
  if (kids.dataset.loaded) return true;
  try {
    const rows = await api(`/api/tree?loc=${state.loc}` + (id == null ? "" : `&parent=${id}`));
    for (const r of rows) renderTreeNode(kids, r);
    kids.dataset.loaded = 1;
    return true;
  } catch (e) {
    // a failed fetch must not leave the folder permanently "loaded and empty":
    // the index can be mid-rebuild, so let the next click try again
    kids.dataset.loaded = "";
    return false;
  }
}

async function toggleNode(el, id) {
  const kids = el._kids;
  if (!kids) return;
  if (!kids.hidden) { kids.hidden = true; el.classList.remove("open"); return; }
  await ensureNodeOpen(el, id);
}

/* Expand every level below a folder, from the tree's own context menu.
   One request per folder, four at a time: a 161-subfolder folder takes about a
   second. Bounded on purpose -- this is a browsing aid, not a way to render a
   library's whole tree into the sidebar. */
const EXPAND_ALL_MAX = 400;          // folders to open before stopping
const EXPAND_ALL_ROWS = 6000;        // ...or rows in the sidebar, whichever comes first
const EXPAND_ALL_MS = 30000;         // ...or wall-clock (safety net for a slow disk)

async function ctxExpandAll() {
  const t = state.ctxTarget;
  $("#ctxmenu").hidden = true;
  if (!t) return;
  const key = t.nodeId ? "n" + t.nodeId : "l" + t.loc;
  const start = document.querySelector(`#tree [data-key="${key}"]`);
  if (!start) return;
  const t0 = Date.now();
  let opened = 0, cut = false;
  const queue = [[start, t.nodeId || null]];
  while (queue.length) {
    const batch = queue.splice(0, 4);
    // a folder can add a hundred rows at once, so the row count is the limit that
    // actually protects the sidebar -- folders alone let a wide tree run away
    const rows = document.querySelectorAll("#tree .tnode, #tree .znode").length;
    if (rows > EXPAND_ALL_ROWS) cut = true;
    await Promise.all(batch.map(async ([el, id]) => {
      if (cut || opened >= EXPAND_ALL_MAX || Date.now() - t0 > EXPAND_ALL_MS) { cut = true; return; }
      opened += 1;
      if (!(await ensureNodeOpen(el, id))) return;
      for (const kid of el._kids.children) {
        if (kid.classList.contains("tnode")) queue.push([kid, +kid.dataset.key.slice(1)]);
      }
    }));
  }
  if (cut) {
    const rows = document.querySelectorAll("#tree .tnode, #tree .znode").length;
    setBuildNote(`Tree expanded ${opened} folders (${rows} rows) and stopped at the ` +
                 `sidebar limit — expand a subfolder to go deeper.`);
    setTimeout(() => setBuildNote(null), 15000);
  }
}

function renderTreeNode(parent, r) {
  const isZip = r.kind === "zip";
  // a zip keeps the same gutter and indent as a subfolder -- it IS a folder as
  // far as browsing goes -- and only the glyph/colour say "archive"
  const n = div(isZip ? "znode" : "tnode",
    `<span class="tw">${isZip ? "" : "▸"}</span>`
    + `<span class="lbl" title="${esc(r.name)}">${isZip ? ICON.zip : ICON.folder}${esc(r.name)}</span>`);
  n.dataset.key = "n" + r.id;
  // Clicking a folder opens it on the RIGHT *and* opens ONE level here, so walking a
  // list of folders with the mouse or ↓ shows each folder's subfolders and zips as you
  // reach them. One level only: the old handler expanded the subtree and dumped a whole
  // folder into the sidebar. The caret (▸) still closes what this opened.
  n.onclick = async e => {
    e.stopPropagation();
    selectTreeNode(n);
    if (isZip) { lastTreeKey = "n" + r.id; openZip(r.id, r.name); return; }
    // The row is a toggle only on a repeat click: "open, look at a picture inside it,
    // come back to the folder" must not fold the subfolders away (owner, v1.1.1). So the
    // fold needs the SAME row clicked twice with nothing else selected in between --
    // anything picked in the grid, in the crumbs, or another row clears the memory.
    const key = "n" + r.id, wasOpen = n._kids && !n._kids.hidden;
    const again = lastTreeKey === key;
    lastTreeKey = key;
    await openFolder(r.id);
    // Closing happens *after* openFolder so the reveal walk (which expands ancestors)
    // cannot re-open the folder the user just closed.
    if (again && wasOpen) {
      n._kids.hidden = true; n.classList.remove("open");
      lastTreeKey = null;                       // a third click opens it again
      return;
    }
    // One level, not the subtree: walking down a list of folders is the point, and
    // the caret (▸) still closes what this opened.
    if (n._kids && n._kids.hidden) await ensureNodeOpen(n, r.id);
  };
  // Zips get the same menu as folders do, minus what does not apply (owner, v1.0.1):
  // right-clicking one in the tree used to fall through to the browser's own menu.
  n.oncontextmenu = e => showCtx(e, { loc: state.loc, parent: r.id, name: r.name,
                                     kind: isZip ? "zip" : "dir", nodeId: r.id });
  if (!isZip) {
    const caret = n.querySelector(".tw");
    if (caret) caret.onclick = e => { e.stopPropagation(); toggleNode(n, r.id); };
  }
  const kids = div("tkids"); kids.hidden = true; kids._owner = n;
  n._kids = kids;
  parent.appendChild(n); parent.appendChild(kids);
}

/* ---------- views ----------
   Every navigation carries a token. A hash-driven walk (f4 -> f762 -> f763) can be
   several round-trips long; if the user clicks mid-walk, the click takes a newer
   token and the walk's remaining segments are refused *before* they paint, so the
   URL, the grid and the crumbs can never end up describing different folders. */
let navToken = 0;
function newNav() { return ++navToken; }

// Re-paint the current view in place (sort change, scan finished). It is a
// *refresh*, not a new intent: it borrows the current token so it can never
// outbid a click that is still fetching, and it republishes the URL after
// painting so the address bar can never describe a folder the grid has left.
async function rerender() {
  const tok = navToken;
  const painted = state.kind === "zip" ? await renderZipEntries(tok) : await renderChildren(tok);
  if (painted) syncHash();
}

function paintedIs(kind, id) {
  const p = state.painted;
  if (!p) return false;
  if (kind === "loc") return p.kind === "loc" && p.loc === id;
  if (kind === "zip") return p.kind === "zip" && p.zipId === id;
  return p.kind === "dir" && p.parent === id && p.loc === state.loc;
}

async function openLocation(locId, tok) {
  tok = tok || newNav();
  if (state.loc === locId && state.parent == null && state.kind === "loc"
      && paintedIs("loc", locId)) return;
  if (tok !== navToken) return;                     // superseded before we started
  state.loc = locId; state.parent = null; state.kind = "loc"; state.zipId = null;
  if (await renderChildren(tok)) syncHash();
}
async function openFolder(id, tok) {
  tok = tok || newNav();
  if (state.kind === "dir" && state.parent === id && paintedIs("dir", id)) return;  // already there
  if (tok !== navToken) return;
  clog("click", { to: "folder", id });
  state.parent = id; state.kind = "dir"; state.zipId = null;
  if (await renderChildren(tok)) syncHash();
}
async function openZip(id, name, tok) {
  tok = tok || newNav();
  if (state.kind === "zip" && state.zipId === id && paintedIs("zip", id)) return;
  if (tok !== navToken) return;
  state.zipId = id; state.kind = "zip";
  if (await renderZipEntries(tok)) syncHash();
}

/* ---------- URL breadcrumb routing (browser Back works) ----------
   hash mirrors the breadcrumb chain: #/l{locId}[/f{folderId}...] [/z{zipId}] [/i{imgId}|/v{videoId}]
   e.g. #/l2/f9/f14          -> NewMedia > Archive2020 > Scans
        #/l2/f9/z12          -> ... > Trip.zip
        #/l2/f9/f14/i310     -> ... > Scans, image 310 open full-screen
        #/l2/f9/v88          -> ... > video 88 playing in-app
   You can Back, deep-link, reload-in-place, or delete trailing segments. */
let hashGuard = false;
let hashHold = false;      // true while a cross-zip hop paints its grid: it publishes /i itself
let pathChain = [];        // [{id, kind, name}] folder/zip ids from loc root, no loc itself
function recordChain(path) {
  // name is carried for the scroll-hop pill: stepping back out of the first child of a
  // folder targets the folder itself, which is only a path entry by the time we need it.
  pathChain = path.slice(1).map(p => ({ id: p.id, kind: p.kind || "f", name: p.name }));
}
function buildHash() {
  let h = "#/l" + state.loc;
  for (const s of pathChain) h += "/" + s.kind + s.id;
  return h;
}
function syncHash() {
  if (hashHold) return;            // mid-hop: the hop publishes #/l…/zNEW/iID in one step
  const h = buildHash();
  if (location.hash !== h) { hashGuard = true; location.hash = h; clog("hash", { to: h }); }
}
async function navigateFromHash() {
  const m = /^#\/l(\d+)((?:\/[fz]\d+)*)(?:\/([iv])(\d+))?$/.exec(location.hash);
  if (!m) return;
  historyNav = true;                 // a history step, not a click: keep scroll offsets
  try {
    const tok = newNav();                  // one token for the whole walk; a click outbids it
    const chain = "#/l" + m[1] + m[2];
    const ovKind = m[3];                        // "i" viewer | "v" player | null
    const itemId = m[4] ? +m[4] : null;
    const overlayOpen = !$("#viewer").hidden || !$("#player").hidden;

    // Back out of (or forward into) an overlay without re-fetching the grid
    if (itemId == null && overlayOpen && chain === buildHash()) {
      hideViewer(); hidePlayer(); return;
    }
    if (itemId != null && overlayOpen && chain === buildHash()) {
      const it = state.items.find(x => x.kind === (ovKind === "v" ? "video" : "image")
                                     && x.id === itemId);
      if (it) { ovKind === "v" ? openPlayer(it) : openViewer(it); return; }
    }

    hideViewer(); hidePlayer(); closeFilm();
    const locId = +m[1];
    // ONE paint per history step. This used to render the location root and then
    // walk the chain through every intermediate folder, painting each: going Back
    // from #/l1/f4/f9/z12 flashed the root's thumbnails, then f4's, before it
    // settled on f9. The destination is enough -- the listing returns the whole
    // breadcrumb path, so the intermediate levels never have to be fetched at all.
    const segs = (m[2].match(/\/([fz])(\d+)/g) || []).map(t => /\/([fz])(\d+)/.exec(t));
    const last = segs.length ? segs[segs.length - 1] : null;
    state.loc = locId;
    if (!last) {
      state.parent = null; state.kind = "loc"; state.zipId = null;
      if (await renderChildren(tok)) syncHash();
    } else if (last[1] === "f") {
      state.parent = +last[2]; state.kind = "dir"; state.zipId = null;
      if (await renderChildren(tok)) syncHash();
    } else {
      state.zipId = +last[2]; state.kind = "zip";
      if (await renderZipEntries(tok)) syncHash();
    }
    if (itemId != null) {
      const it = state.items.find(x => x.kind === (ovKind === "v" ? "video" : "image")
                                     && x.id === itemId);
      if (it) {
        // deep link: put the grid at the current history step, then push the overlay step
        history.replaceState(null, "", location.pathname + chain);
        ovKind === "v" ? openPlayer(it) : openViewer(it);
      }
    }
  } finally { historyNav = false; }
}
window.addEventListener("hashchange", () => {
  if (hashGuard) { hashGuard = false; return; }
  navigateFromHash();
});

/* ---------- background thumb building ----------
   Listings return immediately with pending flags; packs are written by worker
   threads. Poll while anything is pending and patch only the tiles that gained
   a thumb, so scroll position and hover state survive. */
let childPollTimer = 0;

function pendingCount(items) {
  return (items || []).reduce(
    (n, i) => n + ((i.pending && !state.built.has(i.id)) ? 1 : 0), 0);
}

// one scope per view: walking into another folder/zip starts a clean slate
function freshView(kind, zipId) {
  const p = state.painted;
  return !p || p.kind !== kind || p.loc !== state.loc ||
         p.parent !== state.parent || (p.zipId || null) !== (zipId || null);
}

function builtScope() {
  const key = [state.kind, state.loc, state.parent, state.zipId].join(":");
  if (key !== state.builtKey) { state.builtKey = key; state.built.clear(); }
}

function showBuildNote(n) {
  const g = $("#grid");
  if (!g) return;
  // the note is a class, not an id: looking it up by "#buildnote" never matched,
  // so each poll appended another copy (three stacked "Building N…" lines)
  const all = g.querySelectorAll(".buildnote");
  if (!n) { all.forEach(e => e.remove()); return; }
  const el = all[0];
  if (!el) {
    const note = div("buildnote");
    note.textContent = `Building ${n} thumbnail${n === 1 ? "" : "s"}…`;
    g.insertBefore(note, g.firstChild);
    return;
  }
  el.textContent = `Building ${n} thumbnail${n === 1 ? "" : "s"}…`;
  for (const extra of [...all].slice(1)) extra.remove();   // collapse any strays
}

function patchTiles(items) {
  const g = $("#grid");
  if (!g) return 0;
  let filled = 0;
  // the bar counts pictures the server owed this folder; the fallback poll delivers
  // them too, so it has to advance here and not only on the SSE stream
  for (const it of items) if (it.thumb) lbNoteThumb(it.id);
  for (const it of items) {
    const cell = g.querySelector(`.cell[data-id="${it.id}"]`);
    if (!cell || cell.querySelector("img") || !it.thumb) continue;
    cell.replaceWith(tile(it));          // rebuild just this tile
    filled++;
  }
  // A tile that already HAS a picture is the re-encode case, and the only live update
  // used to be the SSE stream. When the stream is not arriving (a buffering proxy, a
  // dropped connection, a second browser) the grid sat on the old bytes until the
  // folder was reopened -- the owner's "re-encode does nothing until I change folder".
  // Every thumb URL carries ?v=<stamp> and the stamp changes when the pack is
  // rewritten, so comparing stamps is enough to know which tiles to re-point.
  for (const it of items) {
    if (!it.thumb) continue;
    const cell = g.querySelector(`.cell[data-id="${it.id}"]`);
    const im = cell && cell.querySelector("img");
    if (!im || !im.parentElement) continue;
    const src = im.getAttribute("src") || "";
    if (cell.dataset.thumbsrc === it.thumb) continue;
    if (src.startsWith("data:") || src === it.thumb) {   // already the newest bytes
      cell.dataset.thumbsrc = it.thumb;
      continue;
    }
    cell.dataset.thumbsrc = it.thumb;                    // the pack was rewritten
    im.src = it.thumb;
  }
  return filled;
}

const BUILD_POLL_LIMIT = 90;        // ~2 min of polling, then stop nagging

/* ---------- live thumbnails (server-sent events) ----------
   The server streams each thumbnail as it is encoded, so tiles fill in while
   the folder is still building and no "building…" notice is needed. If the
   stream is unavailable we fall back to the 1.2s poll. */
let liveOk = false;

function thumbRetry(im, it, tries) {
  // The row said "ready" but the pack could not serve a byte (a rebuild landed
  // underneath it). The server re-pends and rebuilds; the page just has to stop
  // showing a broken-image icon and give the tile back to the poll.
  const n = tries || 0;
  const alreadyDead = () => im.complete && im.naturalWidth === 0;
  im.onerror = () => {
    if (n < 2) {
      setTimeout(() => {
        im.src = it.thumb + (it.thumb.includes("?") ? "&" : "?") + "r=" + Date.now();
        thumbRetry(im, it, n + 1);
      }, 700 * (n + 1));
      return;
    }
    clog("warn", { thumb: it.id, name: it.name, gave_up: true });
    const ph = div("ph", it.kind === "video" ? ICON.film : "?");
    if (im.parentElement) im.replaceWith(ph);
  };
  // a src set before the handler existed can already have failed silently
  if (alreadyDead()) setTimeout(() => im.onerror && im.onerror(new Event("error")), 0);
  return im;
}

function applyLiveThumb(ev) {
  const g = $("#grid");
  if (!g) return;
  lbNoteThumb(ev.id);          // the bar above the grid is counting these arrivals
  const cell = g.querySelector(`.cell[data-id="${ev.id}"]`);
  if (!cell) return;
  // A tile that ALREADY has a picture is the re-encode case -- a Scan or "Re-encode
  // thumbnails" replaces pictures that are on screen. Returning early here (what this
  // used to do) meant the server sent every new thumbnail and the grid ignored all of
  // them: the folder looked frozen for the whole scan, then every tile changed at
  // once when it finished. Swap the bytes in place instead.
  const swapIn = el => {
    el.src = "data:image/jpeg;base64," + ev.d;
    el.classList.remove("fresh");
    void el.offsetWidth;                 // restart the fade if it is already animating
    el.classList.add("fresh");
    setTimeout(() => el.classList.remove("fresh"), 700);
    cell.dataset.live = "1";
  };
  const isMosaic = cell.dataset.kind === "zip" || cell.dataset.kind === "dir";
  const isVideo = cell.dataset.kind === "video";
  if (isMosaic) {          // zip/folder tile: one composed JPEG, fills in whole
    let zb = cell.querySelector(".zipbox");
    const cur = zb && (zb.querySelector("img.tile") || zb.querySelector(".samples img"));
    if (cur) {                              // rebuild of a tile already on screen
      swapIn(cur);
      cur.className = "full tile";
      const samp = zb.querySelector(".samples");
      if (samp) samp.remove();
      return;
    }
    if (!zb) {                                  // a folder still showing its glyph
      zb = div("zipbox");
      const glyph = cell.querySelector(".videomark");
      if (glyph) glyph.replaceWith(zb); else cell.querySelector(".thumbbox").appendChild(zb);
    }
    const s = zb.querySelector(".samples, .zpending");
    if (s) s.remove();
    const t = img("data:image/jpeg;base64," + ev.d, cell.title || "");
    t.className = "full tile"; zb.appendChild(t);
    cell.dataset.live = "1";
    return;
  }
  const have = cell.querySelector("img");            // already has its picture
  if (have) { swapIn(have); if (isVideo) autoRearm(cell, ev.frames); return; }  // ...but it may be the old one
  const box = cell.querySelector(".thumbbox");
  const ph = box && box.querySelector(".ph");
  if (!box) return;
  // must use the .full class: the box is a padding-top:75% spacer, so an
  // in-flow <img> lands below that padding and only its top sliver shows
  const im = img("data:image/jpeg;base64," + ev.d, cell.title || "");
  if (ph) ph.replaceWith(im); else box.appendChild(im);
  cell.dataset.live = "1";
  if (isVideo) autoRearm(cell, ev.frames);   // the event carries this clip's frame list
  // No badge to restore: a finished filmstrip already says "video" better than a
  // clapper glyph did (owner's call, v0.1.191). The ⚠ on a zip video is added by
  // the tile builder and is unaffected.
}

/* The Scan badge counts what is still pending in this folder (or zip). A tile
   gaining its picture is exactly one item leaving that count, so the number now
   falls as the scan works through the folder instead of sitting still and dropping
   to zero when the whole thing finishes. Only the view's own items are touched: an
   event for a folder you are not looking at must not edit what is on screen. */
// What the badge should say while a scan runs.
//  - one folder or one zip: how many thumbnails are left (total is exact once known)
//  - a subtree walk: how many folders/zips/tiles are left, straight from the server,
//    because a walk keeps discovering work and a thumbnail count cannot be monotone
function scanBadgeLeft(st) {
  if (!scanWork || !st) return null;
  if (scanWork.deep) {
    const total = st.thumb_total || 0;
    if (!total || st.phase === "discover") return null;
    return Math.max(0, total - (st.thumb_done || 0));
  }
  if (!(scanWork.total > 0) || st.phase === "discover") return null;
  return Math.max(0, scanWork.total - scanWork.seen);
}

function markItemBuilt(ev) {
  if (ev && ev.id != null) state.built.add(ev.id);
  if (scanWork) {
    // a scan of a location root has no parent id, so its thumbnails arrive without
    // one; without this the number sat on its total for the whole root scan
    const mine = scanWork.deep ? true          // a subtree walk owns every event
               : scanWork.zip ? ev.z === scanWork.zip
               : scanWork.parent != null ? ev.p === scanWork.parent
               : ev.p === undefined && ev.k === "node" && ev.z === undefined;
    // one event per thumbnail is the contract, but a doubled job (or a re-publish) must
    // not push the countdown past the work that exists
    if (mine && ev.id != null) {
      if (!scanWork.seenIds.has(ev.id)) { scanWork.seenIds.add(ev.id); scanWork.seen += 1; }
    } else if (mine) scanWork.seen += 1;
  }
  const inZip = state.kind === "zip";
  if ((ev.k === "entry") !== inZip) return;
  const it = (state.items || []).find(x => x.id === ev.id);
  if (it && it.pending) {
    it.pending = false;
    if (!it.thumb) it.thumb = "live";      // this tile has a picture either way now
  }
  // the countdown moves on every thumbnail, in view or not; the early return above
  // used to leave the badge showing the total until the next one-second poll
  const left = scanWork ? scanBadgeLeft(lastStatus) : null;
  if (left != null) {
    updateBadge(left, libPending, scanWork.deep ? "folders and zips left in this scan"
                                                : "thumbnails left in this scan");
  } else if (!scanWork) {
    // only fall back to the pending count when no scan owns the badge: while one runs
    // this used to flash 0 between "…" and the first real number
    updateBadge(pendingCount(state.items), libPending, null);
  }
  // The folder-in-view bar counts on the same events as the badge. It only moved on
  // the poll tick before, so on a cold 171-photo folder the badge ran 168 -> 0 while
  // the bar beside it sat on "building: 0/171" and then vanished (measured, 0.1.237).
  if (viewJob && viewJob.key === viewKey()) noteViewBuild(missingThumbs(state.items), false);
}

function startEvents() {
  if (!window.EventSource) return;
  const es = new EventSource("/api/events");
  es.onopen = () => { liveOk = true; showBuildNote(0); };   // tiles talk for themselves
  es.onmessage = e => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.t === "thumb") { applyLiveThumb(ev); markItemBuilt(ev); }
    else if (ev.t === "dur") applyDur(ev);
    else if (ev.t === "done") onBuildDone(ev);
  };
  es.onerror = () => {
    liveOk = false;                    // reconnect is automatic; poll meanwhile
    trackBuild();
  };
}

function missingThumbs(items) {
  // tiles that have no picture yet. Folders are excluded: their icon is final.
  return (items || []).filter(i => !i.thumb && i.kind !== "dir").length;
}

/* The same count, minus tiles this page has already been told are built. The listing
   lags the SSE event by up to a poll, so pure `!i.thumb` left the bar frozen at 0 while
   the badge counted down beside it. */
function needsPicture(items) {
  return (items || []).filter(i => !i.thumb && i.kind !== "dir" && !state.built.has(i.id)).length;
}

async function refreshItems(snapshot) {
  const p = snapshot.kind === "zip"
    ? `/api/zip/${snapshot.zipId}/entries?sort=${state.sort}`
    : `/api/children?loc=${snapshot.loc}` +
      (snapshot.parent == null ? "" : `&parent=${snapshot.parent}`) + `&sort=${state.sort}`;
  const data = await api(p);
  if (state.kind !== snapshot.kind || state.parent !== snapshot.parent ||
      state.zipId !== snapshot.zipId || state.loc !== snapshot.loc) return false;  // moved on
  // every listing of a zip is entries: keep the flag the viewer routes on (see
  // viewerSrc) even if an older server leaves it off
  if (snapshot.kind === "zip") data.items.forEach(i => i.zip_entry = true);
  state.items = data.items;
  patchTiles(data.items);
  watchBuild();                      // mosaics for this view are queued now
  return true;
}

function viewKey() {
  return state.kind === "zip" ? "z" + state.zipId
       : state.kind === "dir" ? `f${state.loc}/${state.parent}`
       : "l" + state.loc;
}

/* A view opens with N pictures still to make: that is its denominator. done grows as
   tiles land and the bar clears when the folder is complete. No "nothing landed for a
   while" timeout: a folder queued behind a library-wide backlog (2400 posters at one
   point) is still a real job, and hiding the bar there is the bug this replaces --
   trackBuild's own BUILD_POLL_LIMIT is what stops the polling for good. */
function noteViewBuild(missing, gaveUp) {
  if (gaveUp || !missing) {
    if (viewJob) {
      const total = viewJob.total;
      viewJob = null;
      if (!gaveUp && total) finishJob(`building: ${total}/${total}`);   // say it finished
      else paintProgress();
    }
    return;
  }
  const key = viewKey();
  const left = needsPicture(state.items);
  if (!viewJob || viewJob.key !== key || left > viewJob.total) {
    viewJob = { key, total: left || missing, done: 0, text: "" };
  }
  const done = Math.max(0, viewJob.total - left);
  const q = (lastStatus && lastStatus.building && lastStatus.building.queued) || 0;
  viewJob.done = done;
  viewJob.text = `building: ${done}/${viewJob.total}`;
  viewJob.title = `${done} of ${viewJob.total} pictures in this folder` +
                  (q ? ` — ${q} queued behind other work` : "");
  paintProgress();
}

function trackBuild(tries) {
  clearTimeout(childPollTimer);
  childPollTimer = 0;
  const left = pendingCount(state.items);
  const missing = missingThumbs(state.items);
  const spent = tries || 0;
  // a file that can never be thumbnailed (corrupt, unsupported) must not keep
  // the strip on screen forever: give up quietly, the scan badge still shows it
  noteViewBuild(missing, spent >= BUILD_POLL_LIMIT);
  if (!missing || spent >= BUILD_POLL_LIMIT) { showBuildNote(0); return; }
  showBuildNote(liveOk ? 0 : left);   // SSE usually finishes the tile first
  // Keep polling even while SSE is connected. The stream only carries thumbs
  // built after it subscribed, so a build that ran a moment earlier -- or one
  // this page never saw an event for -- used to leave a 🎬 placeholder until
  // the folder was opened again.
  const snapshot = { kind: state.kind, parent: state.parent, zipId: state.zipId, loc: state.loc };
  childPollTimer = setTimeout(async () => {
    try {
      if (await refreshItems(snapshot)) trackBuild(spent + 1);
    } catch { /* keep the current view; the next navigation retries */ }
  }, liveOk ? 2500 : 1200);
}

// A folder or zip build reporting for duty: re-list once so the page gains the
// tiles it never saw an event for -- and any file added since it rendered.
let doneTimer = 0;
function onBuildDone(ev) {
  const mine = (ev.k === "zip" && state.kind === "zip" && ev.id === state.zipId)
    || (ev.k !== "zip" && state.kind === "dir" && ev.id === state.parent);
  if (!mine) return;
  clearTimeout(doneTimer);
  doneTimer = setTimeout(async () => {
    const snapshot = { kind: state.kind, parent: state.parent, zipId: state.zipId, loc: state.loc };
    try { if (await refreshItems(snapshot)) trackBuild(0); } catch {}
  }, 400);
}

async function renderChildren(tok) {
  builtScope();
  // Two quick clicks race: the slower response used to land last and rewrite the
  // grid and URL with the folder the user had already left. Only the newest
  // navigation token may paint.
  if (tok !== navToken) return false;
  const url = `/api/children?loc=${state.loc}` +
    (state.parent == null ? "" : `&parent=${state.parent}`) + `&sort=${state.sort}` +
    (freshView(state.parent == null ? "loc" : "dir", null) ? "&nav=1" : "");
  clog("req", { url });
  let data;
  try {
    data = await api(url);
  } catch (e) {
    return navFailed(e, url, tok);           // was an unhandled rejection: highlight moved, pane froze
  }
  if (tok !== navToken) return false;        // superseded: do not paint or publish a URL
  if (viewAdopt(data.rel)) return renderChildren(tok);   // re-list once, in the pinned order
  clog("paint", { items: data.items.length, path: (data.path || []).map(p => p.name).join("/") });
  setCrumbs(data.path);
  recordChain(data.path);
  state.items = data.items;
  renderGrid(data.items);
  state.painted = { kind: state.parent == null ? "loc" : "dir",
                    loc: state.loc, parent: state.parent, zipId: null };
  trackBuild();
  refreshStatus();
  revealTree(data.path);
  return true;
}

let zipPollTimer = 0;
async function renderZipEntries(tok) {
  builtScope();
  if (tok !== navToken) return false;
  const zid = state.zipId;
  const url = `/api/zip/${zid}/entries?sort=${state.sort}` +
    (freshView("zip", zid) ? "&nav=1" : "");
  clog("req", { url });
  let data;
  try {
    data = await api(url);
  } catch (e) {
    return navFailed(e, url, tok);
  }
  clog("paint", { items: data.items.length, zip: zid });
  if (state.kind !== "zip" || state.zipId !== zid) return false;   // navigated away mid-fetch
  if (viewAdopt(data.rel)) return renderZipEntries(tok);
  data.items.forEach(i => i.zip_entry = true);
  setCrumbs(data.path);
  recordChain(data.path);
  state.items = data.items;
  renderGrid(data.items);
  state.painted = { kind: "zip", loc: state.loc, parent: state.parent, zipId: zid };
  trackBuild();
  refreshStatus();
  revealTree(data.path);
  return true;
}

/* A listing that fails leaves the tree highlight on the folder the user clicked
   while the pane still shows the previous folder -- the exact report "the
   highlight changed but the grid didn't". Say what happened and offer the two
   ways out. */
function navFailed(e, url, tok) {
  if (tok !== navToken) return false;
  const status = String((e && e.message) || "error");
  clog("err", { url, status });
  const g = $("#grid");
  g.innerHTML = "";
  const box = div("empty", "");
  box.textContent = status === "404"
    ? "This folder is not in the index any more — it was probably renamed or moved. "
    : `Couldn't load this folder (HTTP ${status}). `;
  const retry = document.createElement("button");
  retry.className = "btn";
  retry.textContent = "Retry";
  retry.onclick = () => { (state.kind === "zip" ? openZip(state.zipId, "", newNav())
                                                : openFolder(state.parent, newNav())); };
  const up = document.createElement("button");
  up.className = "btn";
  up.textContent = "Back to " + (state.kind === "zip" ? "folder" : "location");
  up.onclick = () => { state.kind === "zip" ? openFolder(state.parent, newNav())
                                           : openLocation(state.loc, newNav()); };
  box.appendChild(retry); box.appendChild(document.createTextNode(" ")); box.appendChild(up);
  g.appendChild(box);
  return false;
}

/* ---------- reveal current path in left tree ---------- */
let revealSeq = 0;
/* Scroll #tree-pane so a row is fully visible, clearing the sticky "Up to" bar.
   scrollIntoView({block:"nearest"}) is not enough on its own: a row parked against the
   top of the scrollport still counts as "visible" while #tree-head draws over it, so
   stepping back up a level left the newly selected folder sitting behind the bar. */
function revealRow(n) {
  if (!n || !n.isConnected) return;
  const pane = document.getElementById("tree-pane");
  if (!pane) return;
  const box = pane.getBoundingClientRect();
  const head = document.getElementById("tree-head");
  const headBottom = head ? head.getBoundingClientRect().bottom : 0;   // 0 when hidden
  const top = Math.max(box.top, headBottom) + 4;      // first usable line under the bar
  const bottom = box.bottom - 8;
  const r = n.getBoundingClientRect();
  if (r.top < top) pane.scrollTop -= top - r.top;
  else if (r.bottom > bottom) pane.scrollTop += r.bottom - bottom;
}
/* Reveal now, and again next frame: expanding a node re-renders its children, which
   moves the row after we scrolled to where it used to be. */
function revealRowSoon(n) {
  revealRow(n);
  requestAnimationFrame(() => revealRow(n));
}
async function revealTree(path) {
  const seq = ++revealSeq;
  if (!path || !path.length) return;
  for (let i = 0; i < path.length; i++) {
    if (seq !== revealSeq) return;           // superseded by newer navigation
    const step = path[i];
    const isLast = i === path.length - 1;
    const key = step.loc != null ? "l" + step.loc : "n" + step.id;
    const node = document.querySelector(`#tree [data-key="${key}"]`);
    if (!node) return;                        // tree not reachable (collapsed loc)
    selectTreeNode(node);
    // Expand only what the user needs to SEE the current folder: its ancestors, plus
    // the location root itself (that is what makes the tree navigable at all). The
    // folder being opened stays collapsed -- a folder of 160 subfolders used to dump
    // all of them into the left pane on a single click. It opens when you go one
    // level deeper, because then it is an ancestor.
    if (node._kids && node._kids.hidden && (!isLast || step.loc != null)) {
      await toggleNode(node, step.loc != null ? null : step.id);
    }
  }
  const lastKey = path[path.length - 1].loc != null
    ? "l" + path[path.length - 1].loc : "n" + path[path.length - 1].id;
  const last = document.querySelector(`#tree [data-key="${lastKey}"]`);
  if (last && seq === revealSeq) revealRowSoon(last);
}
function selectTreeNode(node) {
  document.querySelectorAll("#tree .sel").forEach(n => n.classList.remove("sel"));
  node.classList.add("sel");
}

/* ---------- keyboard folder navigation (↑/↓ folders, ←/→ collapse-expand) ---------- */
function visibleTreeNodes() {
  return [...document.querySelectorAll("#tree .tnode, #tree .znode")]
    .filter(n => n.offsetParent !== null);
}
function treeNodeParent(n) {
  const kids = n.closest(".tkids");
  return kids ? kids._owner || null : null;
}
async function openTreeNode(n) {
  const key = n.dataset.key;
  if (key.startsWith("l")) { await openLocation(+key.slice(1)); return; }
  const id = +key.slice(1);
  if (n.classList.contains("znode")) await openZip(id, "");
  else await openFolder(id);        // ↓ opens it in the grid; → expands it if wanted
}
async function treeStep(d) {
  const nodes = visibleTreeNodes();
  if (!nodes.length) return;
  let i = nodes.findIndex(n => n.classList.contains("sel"));
  if (i < 0) i = d > 0 ? -1 : nodes.length;
  const j = Math.min(nodes.length - 1, Math.max(0, i + d));
  if (j === i) return;
  const n = nodes[j];
  selectTreeNode(n);
  revealRowSoon(n);
  await openTreeNode(n);
  // ↓ behaves like a click: the folder you land on shows its subfolders and zips, so
  // the next ↓ walks into them instead of you having to aim at the caret
  if (n._kids && n._kids.hidden && !n.classList.contains("znode")) {
    await ensureNodeOpen(n, n.dataset.key.startsWith("l") ? null : +n.dataset.key.slice(1));
  }
  revealRowSoon(n);          // children may have just grown the branch under the row
}
function treeExpand() {
  const n = document.querySelector("#tree .sel");
  if (!n || !n._kids) return;
  if (n._kids.hidden) toggleNode(n, n.dataset.key.startsWith("l") ? null : +n.dataset.key.slice(1));
}
async function treeCollapse() {
  const n = document.querySelector("#tree .sel");
  if (!n) return;
  if (n._kids && !n._kids.hidden) { n._kids.hidden = true; return; }
  const p = treeNodeParent(n);           // already collapsed: jump to parent
  if (p) { selectTreeNode(p); revealRowSoon(p); await openTreeNode(p); revealRowSoon(p); }
}

function pnull(v) { return v == null ? "" : v; }

function setCrumbs(path) {
  const c = $("#crumbs");
  c.innerHTML = "";
  state.path = path || [];
  paintUp();
  paintFavBtn();
  path.forEach((p, i) => {
    if (i) c.appendChild(span("sep", "›"));
    const el = span("crumb", esc(p.name));
    el.onclick = () => {
      if (p.loc) openLocation(p.loc);
      else if (p.id && state.kind === "zip" && isZipCrumb(path, i)) openZip(p.id, p.name);
      else if (p.id) openFolder(p.id);
    };
    c.appendChild(el);
  });
}
function isZipCrumb(path, i) {
  return i === path.length - 1;
}

/* ---------- Up one folder (v0.1.237) ----------
   The button at the top of the folder pane goes to the parent of whatever is on
   screen: a folder's parent, or the folder that holds the zip you are in. It names
   its destination ("Up to Le Mans") so the click is never a guess, and it is
   disabled at a location's root, where there is nowhere further up to go.
   The breadcrumb chain is the source of truth -- it is what the server says the
   path is -- so this never has to walk the tree. */
function upTarget() {
  const p = state.path || [];
  return p.length >= 2 ? p[p.length - 2] : null;
}
function paintUp() {
  const b = $("#btn-up");
  if (!b) return;
  const t = upTarget();
  b.disabled = !t;
  const to = b.querySelector(".up-to");
  to.innerHTML = t ? `Up to <b>${esc(t.name)}</b>` : "Up";
  b.title = t ? `Up to ${t.name}  (Backspace or Alt+↑)` : "Already at the top";
}
function goUp() {
  const t = upTarget();
  if (!t) return;
  clog("click", { to: "up", name: t.name });
  if (t.loc) openLocation(t.loc);
  else if (t.id) openFolder(t.id);
}
$("#btn-up").onclick = goUp;
/* Picking a picture, a folder tile, a zip, a crumb or Up means "I am doing something
   else now": the next click on a folder row opens it instead of folding it. */
for (const sel of ["#grid", "#crumbs", "#btn-up"]) {
  const el = $(sel);
  if (el) el.addEventListener("click", () => { lastTreeKey = null; }, true);
}

/* ---------- favourites menu (v0.1.244) ----------
   The star in the top bar opens a short list of saved folders. The first line adds
   (or removes) whatever is on screen -- a folder, a zip or a location root -- and
   every saved entry has an x on its right that removes it without navigating. The
   list lives on the server, keyed by path, so it is the same in every browser and
   survives a re-index (an entry whose folder has gone shows greyed, x still there). */
let favs = [];
async function loadFavs() {
  try { favs = (await api("/api/favs")).items || []; } catch (_) { favs = []; }
  paintFavBtn();
  return favs;
}
function favHere() {
  // what "this folder" means for the add/remove line
  if (state.kind === "zip" && state.zipId) return { node: state.zipId };
  if (state.kind === "dir" && state.parent != null) return { node: state.parent };
  return { loc: state.loc };
}
function favMatch(f, here) {
  return here.node ? f.node === here.node : (f.kind === "loc" && f.loc === here.loc);
}
function paintFavBtn() {
  const b = $("#btn-favs");
  if (!b) return;
  const on = favs.some(f => favMatch(f, favHere()));
  b.innerHTML = on ? ICON.starOn : ICON.star;
  b.classList.toggle("on", on);
  b.title = on ? "Favourites — this folder is one" : "Favourites";
}
function hideFavs() { const m = $("#favmenu"); if (m && !m.hidden) m.hidden = true; }
async function toggleFavMenu() {
  const m = $("#favmenu");
  if (!m.hidden) { m.hidden = true; return; }
  await loadFavs();
  renderFavMenu();
  const r = $("#btn-favs").getBoundingClientRect();
  m.hidden = false;
  m.style.top = (r.bottom + 6) + "px";
  m.style.left = Math.max(8, Math.min(r.right - m.offsetWidth, innerWidth - m.offsetWidth - 8)) + "px";
}
function renderFavMenu() {
  const m = $("#favmenu");
  const here = favHere();
  const mine = favs.find(f => favMatch(f, here));
  const crumbs = state.path || [];
  const hereName = crumbs.length ? crumbs[crumbs.length - 1].name : "this folder";
  m.innerHTML = "";
  const top = div("fav-add", (mine ? ICON.starOn : ICON.star) +
    `<span>${mine ? "Remove" : "Add"} <b>${esc(hereName)}</b>${mine ? " from favourites" : ""}</span>`);
  top.onclick = async () => {
    try {
      const r = mine ? await api(`/api/favs/${mine.id}`, { method: "DELETE" })
                     : await api("/api/favs", { method: "POST", headers: { "Content-Type": "application/json" },
                                                body: JSON.stringify(here) });
      favs = r.items || [];
    } catch (_) {}
    paintFavBtn(); renderFavMenu();
  };
  m.appendChild(top);
  m.appendChild(div("fav-sep"));
  if (!favs.length) {
    m.appendChild(div("fav-empty", "No favourites yet. Open a folder and add it here."));
    return;
  }
  for (const f of favs) {
    const row = div("fav-item" + (f.missing ? " missing" : "") + (favMatch(f, here) ? " current" : ""),
      `${f.kind === "zip" ? ICON.zip : ICON.folder}` +
      `<span class="fav-txt"><span class="fav-name">${esc(f.name)}</span>` +
      (f.missing ? `<span class="fav-where">not found — moved or deleted</span>`
                 : f.where ? `<span class="fav-where">${esc(f.where)}</span>` : "") + `</span>`);
    row.title = f.missing ? `${f.rel} — not found` : (f.where ? `${f.where} / ${f.name}` : f.name);
    const x = document.createElement("button");
    x.type = "button"; x.className = "fav-x"; x.innerHTML = ICON.x;
    x.title = "Remove from favourites"; x.setAttribute("aria-label", `Remove ${f.name}`);
    x.onclick = async e => {
      e.stopPropagation();                         // remove, don't navigate
      try { favs = (await api(`/api/favs/${f.id}`, { method: "DELETE" })).items || []; } catch (_) {}
      paintFavBtn(); renderFavMenu();
    };
    row.appendChild(x);
    row.onclick = () => {
      if (f.missing) return;
      hideFavs();
      clog("click", { to: "favourite", name: f.name });
      if (f.loc !== state.loc) { state.loc = f.loc; }
      if (f.kind === "loc") openLocation(f.loc);
      else if (f.kind === "zip") openZip(f.node, f.name);
      else openFolder(f.node);
    };
    m.appendChild(row);
  }
}
$("#btn-favs").onclick = e => { e.stopPropagation(); toggleFavMenu(); };
document.addEventListener("mousedown", e => {
  const m = $("#favmenu");
  if (m && !m.hidden && !m.contains(e.target) && !$("#btn-favs").contains(e.target)) m.hidden = true;
}, true);
document.addEventListener("keydown", e => { if (e.key === "Escape") hideFavs(); });
window.addEventListener("blur", hideFavs);
loadFavs();

/* ---------- grid ---------- */
let lastViewKey = null;      // which folder/zip the grid was last painted for
let historyNav = false;      // true while a back/forward step is repainting the grid
const scrollMemo = new Map();   // view key -> the offset you left that view at

/* ---------- one-shot client diagnostic ("why does nothing animate?") ----------
   A strip can be provably present on disk while one particular browser shows a
   still poster. Nothing server-side can tell those apart, so after a folder renders
   this ships ONE record to /api/log describing the tab: which app.js actually ran,
   whether the OS asks for reduced motion, the switch state, and the tile image names
   sampled 3s apart. It posts regardless of log verbosity, so "send me the record"
   is a reload instead of a screenshot conversation. */
let diagTimer = 0;
function diagSnap() {
  return [...document.querySelectorAll("#grid .cell img")].slice(0, 5)
    .map(i => String(i.src).split("/").pop().split("?")[0]);
}
function diagReport() {
  try {
    const cells = [...document.querySelectorAll("#grid .cell")].filter(c => c.dataset.kind === "video");
    let jsbytes = -1;
    try {
      const e = performance.getEntriesByType("resource").find(r => String(r.name).indexOf("app.js") !== -1);
      jsbytes = e ? Math.round(e.encodedBodySize || 0) : -1;
    } catch (_) {}
    const rec = {
      ev: "diag", href: location.href, ua: navigator.userAgent.slice(0, 80),
      script: (document.querySelector("script[src*='app.js']") || {}).src || "",
      jsbytes: jsbytes,
      reduced: (() => { try { return matchMedia("(prefers-reduced-motion: reduce)").matches; }
                        catch (_) { return null; } })(),
      pref: (() => { try { return localStorage.getItem("mx.frames"); }
                     catch (_) { return "err"; } })(),
      animOverride: (typeof animOverride === "undefined" ? "n/a" : animOverride),
      on: (typeof framesOn === "function" ? framesOn() : "n/a"),
      pinned: !!state.pinned, hidden: !!document.hidden,
      vids: cells.length, imgs: document.querySelectorAll("#grid .cell img").length,
      autos: (typeof autoPreviews === "undefined" ? "n/a" : autoPreviews.size),
      srcs1: diagSnap(),
    };
    setTimeout(() => {
      try { rec.srcs2 = diagSnap(); } catch (_) {}
      fetch("/api/log", { method: "POST", keepalive: true,
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify(rec) }).catch(() => {});
    }, 3000);
  } catch (_) {}
}
function scheduleDiag() {
  clearTimeout(diagTimer);
  diagTimer = setTimeout(diagReport, 4000);
}

function renderGrid(items) {
  const g = $("#grid");
  // Remember where this view was left BEFORE the cells are replaced, so Back can
  // put you back at the picture you were looking at.
  if (lastViewKey && g.scrollTop > 0) scrollMemo.set(lastViewKey, g.scrollTop);
  g.innerHTML = "";
  // Moving to another folder or zip starts at its first file, never at whatever
  // offset the previous list was scrolled to. A redraw of the SAME view (a build
  // finishing, a re-list) keeps the position you were reading.
  const viewKey = `${state.loc}|${state.kind}|${state.parent || ""}|${state.zipId || ""}`;
  const moved = viewKey !== lastViewKey;
  lastViewKey = viewKey;
  // A grid swap must not kill a filmstrip that is still being built: the frames
  // land later and adopt() rebinds them to the new tile. Only drop the hover if
  // this video is no longer on screen.
  if (hover && !items.some(it => it.id === hover.nodeId)) endVideoHover(true);
  g.style.setProperty("--tw", state.size + "px");
  if (!items.length) {
    g.appendChild(div("empty", "Empty folder."));
    hideLoadbar();
    if (moved) {
    // Back/forward restores the offset you left that view at; any other move into a
    // folder or zip starts at its first file.
    g.scrollTop = historyNav ? (scrollMemo.get(viewKey) || 0) : 0;
    g.scrollLeft = 0;
  }
    return;
  }
  for (const it of items) g.appendChild(tile(it));
  startAutoPreview();
  scheduleDiag();
  if (moved) {
    // Back/forward restores the offset you left that view at; any other move into a
    // folder or zip starts at its first file.
    g.scrollTop = historyNav ? (scrollMemo.get(viewKey) || 0) : 0;
    g.scrollLeft = 0;
  }
  lbStart(items);
}

/* ---------- how much of this folder is actually here yet ----------
   TWO different waits, and mixing them is what made the old bar twitch:

   "Building"  the server has not composed this tile's picture yet (the 2x2 of a zip,
               a folder's face, a video's poster). The total is the whole folder, it
               only ever goes up, and it is the number that matters in a folder of
               300 zips. Driven by the SSE stream, so it costs nothing to watch.

   "Loading"   the pictures exist; the browser is fetching the ones on screen. The
               set is FROZEN when the folder is drawn, so scrolling can neither raise
               the total nor send the bar backwards.

   The old bar re-measured "cells within one screen of the viewport" every 350ms and
   recomputed both numbers each tick -- so every scroll step redefined what was being
   counted: the owner's "it changes total files and it moves back and forward as you
   scroll down". */
let lb = null;           // {mode, need:Set, have:Set, vis:[ids], visTotal, best, stall}
let lbTimer = 0;

function needsPicture(it) {
  // what mosaicTile()/tile() can draw with no server work: a composed thumb, or
  // sample pictures a zip/folder tile can show while its face is still being built
  if (it.thumb) return false;
  if (it.kind === "dir" || it.kind === "zip") return !(it.samples || []).length;
  return true;
}

function hideLoadbar() {
  const bar = $("#loadbar");
  if (bar) bar.hidden = true;
}

function lbStart(items) {
  hideLoadbar();
  lb = null;
  if (lbTimer) { clearInterval(lbTimer); lbTimer = 0; }
  const g = $("#grid");
  if (!g) return;
  const need = new Set();
  for (const it of items) if (needsPicture(it)) need.add(String(it.id));
  // the on-screen batch, frozen here so it cannot drift while the owner scrolls
  const vis = [];
  const h = g.clientHeight || 1;
  for (const c of g.querySelectorAll(".cell")) {
    const y = c.offsetTop - g.scrollTop;
    if (y > -h * 0.5 && y < h * 2) vis.push(c.dataset.id);
  }
  lb = { mode: need.size ? "build" : "load", need, have: new Set(),
         vis, visTotal: vis.length, best: 0, stall: 0 };
  if (lbPaint() && (lb.need.size || lb.visTotal)) lbTick();
}

/* A picture the server owed the folder has just arrived (SSE, or the fallback poll). */
function lbNoteThumb(id) {
  if (!lb || lb.mode !== "build") return;
  const k = String(id);
  if (!lb.need.has(k) || lb.have.has(k)) return;
  lb.have.add(k);
  lbPaint();
}

function lbPaint() {
  const bar = $("#loadbar"), txt = $("#loadbar-text");
  if (!bar || !lb) return null;
  if (lb.mode === "build") {
    // a build that delivers nothing for a minute is not a build we can report on: the
    // tiles below have not been asked for yet (a hidden tab never fires the lazy loads)
    // or the queue dropped the work -- either way, stop showing a number that cannot move
    if (lb.have.size) lb.stall = 0;
    else if (++lb.stall > 240) { hideLoadbar(); return null; }
    bar.hidden = false;
    bar.style.setProperty("--p", (lb.have.size / lb.need.size * 100).toFixed(1) + "%");
    txt.textContent = `Building pictures ${lb.have.size} of ${lb.need.size}`;
    if (lb.have.size < lb.need.size) return { mode: "build", done: lb.have.size, total: lb.need.size };
    // server is finished: whatever the browser still owes on screen from here
    lb.mode = "load"; lb.best = 0; lb.stall = 0;
  }
  let done = 0;
  for (const id of lb.vis) {
    const c = $(`#grid .cell[data-id="${id}"]`);
    const im = c && c.querySelector(".thumbbox img");
    if (im && im.complete && im.naturalWidth) done++;
  }
  if (done > lb.best) { lb.best = done; lb.stall = 0; } // never run backwards
  else lb.stall++;
  if (!lb.visTotal || lb.best >= lb.visTotal || lb.stall > 12) {
    // finished, or nothing has moved for ~3s (the owner scrolled away from the batch
    // that is loading): stop nagging and get out of the pictures' way
    hideLoadbar();
    return null;
  }
  bar.hidden = false;
  bar.style.setProperty("--p", (lb.best / lb.visTotal * 100).toFixed(1) + "%");
  txt.textContent = `Loading pictures ${lb.best} of ${lb.visTotal}`;
  return { mode: "load", done: lb.best, total: lb.visTotal };
}

function lbTick() {
  if (lbTimer) return;
  lbTimer = setInterval(() => {
    if (!lbPaint()) { clearInterval(lbTimer); lbTimer = 0; }   // null = nothing left to report
  }, 250);
}

/* ---------- video thumbnail hover flipbook (filmstrip frames) ----------
   Exactly ONE tile animates at a time. Start is delayed 500ms so sweeping
   the mouse across tiles triggers nothing, and late frame-extraction
   responses are dropped if the pointer already left (the old bug: every
   tile you swept past started animating forever). */
const vframes = {};      // nodeId -> [urls]  (cached until something rebuilds that clip)
/* Anything that re-encodes a clip invalidates this: the tile would otherwise keep
   flipping the two images it fetched before the strip existed, which is why "Build
   filmstrips in this folder" looked like it worked for one tile and not the rest --
   whichever tile had not been cached yet picked up its new frames. */
function forgetFrames() {
  for (const k of Object.keys(vframes)) delete vframes[k];
}
let   hover = null;      // { nodeId, imgEl, poster, over, timer, interval }

function endVideoHover(force) {
  const h = hover;
  if (!h) return;
  // patchTiles/applyLiveThumb can replace the tile under the cursor, which fires
  // mouseleave on a node that is no longer in the document. That is not the
  // pointer leaving, so keep the filmstrip armed; the next real leave ends it.
  if (!force && h.cell && h.cell.isConnected === false) return;
  clearTimeout(h.timer); clearInterval(h.interval);
  h.over = false;
  if (h.cell) h.cell.classList.remove("extracting");
  if (h.imgEl) h.imgEl.onerror = null;
  if (h.imgEl) clearFade(h.imgEl);
  if (h.imgEl && h.imgEl.isConnected) h.imgEl.src = h.poster || h.imgEl.src;
  const cell = h.cell; hover = null;
  // the pointer left: hand the tile back to the ambient preview
  if (framesOn() && cell && cell.isConnected) autoTile(cell);
}
function beginFlip(h) {
  const clearInd = () => { if (h.cell) h.cell.classList.remove("extracting"); };
  // The tile for this video may have been rebuilt since the frames were asked
  // for; bind to whatever <img> currently represents it, creating one over the
  // placeholder when the pack was built from a posterless tile.
  const adopt = () => {
    const live = document.querySelector(`#grid .cell[data-id="${h.nodeId}"]`);
    if (!live) return false;
    if (live !== h.cell) h.imgEl = live.querySelector("img");
    h.cell = live;
    if (!h.imgEl || !h.imgEl.isConnected) {
      const box = live.querySelector(".thumbbox");
      if (!box) return false;
      const first = img("", live.title || "");
      first.className = "full";
      const ph = box.querySelector(".ph");
      ph ? ph.replaceWith(first) : box.appendChild(first);
      h.imgEl = first;
    }
    return true;
  };
  const play = (urls) => {
    clearInd();
    if (urls.length < 2) return;                   // one still is not a preview
    if (!h.over || hover !== h) return;            // a newer hover owns the tile
    if (!adopt()) return;
    // a frame that 404s means the pack was rebuilt/removed under us: drop the
    // cached URL list so the next hover re-asks the server instead of looping
    // through broken images for the rest of the session
    h.imgEl.onerror = () => { delete vframes[h.nodeId]; endVideoHover(true); };
    let i = 0;
    h.imgEl.src = urls[0];
    // two images are the default, and 450ms between them reads as a blink: give a
    // short list a slower beat, and let a full filmstrip keep its pace
    const beat = urls.length <= 2 ? 1100 : 450;
    const fade = urls.length <= 2 ? 450 : 180;
    h.interval = setInterval(() => {
      i = (i + 1) % urls.length;
      if (!adopt()) { endVideoHover(true); return; }
      fadeTo(h.imgEl, urls[i], fade);
    }, beat);
  };
  if (vframes[h.nodeId]) { play(vframes[h.nodeId]); return; }
  if (h.cell) h.cell.classList.add("extracting");   // "creating animation" indicator
  api(`/api/frames/${h.nodeId}`).then(d => {
    // frames are cached server-side; play whenever the pointer is still here
    if (d.frames && d.frames.length) { vframes[h.nodeId] = d.frames; play(d.frames); }
    else clearInd();
  }).catch(() => clearInd());
}
const FRAMES_PREF = "mx.frames";
// animOverride wins while a pinned folder is on screen; it never writes the global
// switch, so leaving that folder restores whatever the switch says.
function framesOn() {
  if (animOverride !== null) return animOverride;
  try { return localStorage.getItem(FRAMES_PREF) !== "0"; } catch (_) { return true; }
}
function armVideoHover(nodeId, imgEl, poster, cell) {
  if (!framesOn()) return;                                     // preview switched off in the top bar
  autoPause(nodeId);                                            // the hover flipbook takes over
  if (hover && hover.nodeId === nodeId && hover.over) return;   // already armed/playing
  endVideoHover(true);                             // only one at a time
  const h = { nodeId, imgEl, poster, cell, over: true, timer: 0, interval: 0 };
  hover = h;
  h.timer = setTimeout(() => beginFlip(h), 500);
}

/* ---------- automatic frame preview (no hover needed) ----------
   With the top-bar switch on, every video tile in view flips through its own
   frames on its own timer. Only tiles that are actually on screen animate (an
   IntersectionObserver starts/stops them), the start phase is staggered so a grid
   does not pulse in lockstep, and hovering a tile pauses its autoplay so the
   hover flipbook owns it. One /api/frames call per clip per session, cached in
   `vframes` and shared with the hover path. */
const AUTO_BEAT = 3000;      // ms per image: a slow, ambient flip (Andy, Nov 2026: 1.4s felt rushed)
const AUTO_STRIP = 900;      // a full filmstrip is a motion preview: 8 frames x 3s = 24s a loop
const AUTO_MAX = 48;        // tiles animating at once, nearest the viewport first
const autoPreviews = new Map();     // nodeId -> {urls, i, timer, poster}
let autoObserver = null;

function autoImg(id, create) {
  const cell = document.querySelector(`#grid .cell[data-id="${id}"]`);
  if (!cell) return null;
  let im = cell.querySelector("img");
  if (!im && create) {
    const box = cell.querySelector(".thumbbox");
    if (!box) return null;
    im = img("", cell.title || "");
    im.className = "full";
    const ph = box.querySelector(".ph");
    ph ? ph.replaceWith(im) : box.appendChild(im);
  }
  return im;
}

/* ---------- crossfade between preview images (v0.1.237) ----------
   A hard src swap reads as a blink, most of all on the two-image default. The next
   frame loads into an overlay <img class="xfade">, fades in over the current one,
   and only THEN becomes the tile's own picture underneath, after which the overlay
   drops back to transparent. So the tile's real <img> -- the one poster restore,
   adopt() and patchTiles all read -- is always the single source of truth, and the
   overlay is purely cosmetic: if anything goes wrong it is just removed.
   A frame is only faded in once it has LOADED, so a slow frame never fades in
   half-drawn; a stale callback (a newer swap started) is ignored via a generation. */
const FADE_AMBIENT = 900;    // the 3s two-image beat: a slow dissolve
const FADE_STRIP = 320;      // filmstrip frames: short, so motion still reads as motion
function fadeTo(im, url, ms) {
  if (!im || !im.isConnected) return;
  const box = im.parentNode;
  if (!ms || !box) { im.src = url; return; }
  let top = box.querySelector(":scope > img.xfade");
  if (!top) {
    top = document.createElement("img");
    top.className = "full xfade"; top.alt = ""; top.draggable = false;
    box.appendChild(top);
  }
  const gen = top._gen = (top._gen || 0) + 1;
  top.style.transition = "none";
  top.classList.remove("on");
  top.onerror = () => { if (top._gen === gen) im.src = url; };
  top.onload = () => {
    if (top._gen !== gen) return;
    void top.offsetWidth;                         // commit opacity 0 before fading
    top.style.transition = `opacity ${ms}ms ease-in-out`;
    top.classList.add("on");
    setTimeout(() => {
      if (top._gen !== gen) return;
      if (!im.isConnected) { top.remove(); return; }   // the tile was rebuilt meanwhile
      im.src = url;
      const drop = () => {
        if (top._gen !== gen) return;
        top.style.transition = "none";
        top.classList.remove("on");
      };
      (im.decode ? im.decode() : Promise.resolve()).then(drop, drop);
    }, ms);
  };
  const abs = new URL(url, location.href).href;
  if (top.src === abs && top.complete && top.naturalWidth) top.onload();
  else top.src = url;
}
/* Back to a still picture (poster restore, switch off): the overlay must go too,
   or a half-faded frame would sit over the poster. */
function clearFade(im) {
  const top = im && im.parentNode && im.parentNode.querySelector(":scope > img.xfade");
  if (top) { top._gen = (top._gen || 0) + 1; top.remove(); }
}

function autoCell(id) { return document.querySelector(`#grid .cell[data-id="${id}"]`); }
/* How far a tile sits from the middle of the screen. The ambient preview spends its
   budget on what you are looking at, not on whatever came first in the listing. */
function autoRank(el) {
  if (!el || !el.isConnected) return Infinity;
  const r = el.getBoundingClientRect();
  return Math.hypot((r.left + r.right) / 2 - window.innerWidth / 2,
                    (r.top + r.bottom) / 2 - window.innerHeight / 2);
}
function autoRunning() {
  let n = 0;
  for (const st of autoPreviews.values()) if (st.timer) n++;
  return n;
}
/* Keep at most AUTO_MAX flipping at once. Returns false when the tile asking to start
   is no nearer the middle of the screen than what is already running: without that gate
   a busy grid thrashed, starting a tile only to retire it one step later. */
function autoTrim(keepId) {
  if (autoRunning() < AUTO_MAX) return true;            // budget left, nothing to retire
  const running = [...autoPreviews].filter(([id, st]) => st.timer && id !== keepId);
  if (!running.length) return true;
  const mine = autoRank(autoCell(keepId));
  let worst = -Infinity;
  for (const [id] of running) worst = Math.max(worst, autoRank(autoCell(id)));
  if (!(mine < worst)) return false;                   // no better placed than the incumbents
  let over = running.length + 1 - AUTO_MAX;
  running.sort((a, b) => autoRank(autoCell(b[0])) - autoRank(autoCell(a[0])));
  for (const [id] of running) {
    if (over-- <= 0) break;
    autoPause(id);
  }
  return true;
}
/* Budget freed by tiles scrolling away goes to the best-placed paused tile still on
   screen; without this a grid trimmed once would sit below the cap until a reload. */
function autoFill() {
  const vh = window.innerHeight;
  const paused = [];
  for (const [id, st] of autoPreviews) {
    if (st.timer || !st.urls || st.urls.length < 2) continue;
    const el = autoCell(id);
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > vh) continue;          // off screen: not a candidate
    paused.push([id, autoRank(el)]);
  }
  if (!paused.length) return;
  paused.sort((a, b) => a[1] - b[1]);
  for (const [id] of paused) {
    if (autoRunning() >= AUTO_MAX) break;
    autoStart(id);
  }
}

function autoStart(id) {
  const st = autoPreviews.get(id);
  if (!st || st.timer || !framesOn()) return;
  if (!st.urls || st.urls.length < 2) return;
  if (!autoTrim(id)) return;          // a better-placed tile already owns the budget
  const beat = st.urls.length > 3 ? AUTO_STRIP : AUTO_BEAT;
  st.timer = setInterval(() => {
    if (!framesOn()) { stopAutoPreview(); return; }
    const im = autoImg(id, true);
    if (!im) return;
    st.i = (st.i + 1) % st.urls.length;
    fadeTo(im, st.urls[st.i], beat === AUTO_BEAT ? FADE_AMBIENT : FADE_STRIP);
  }, beat);
}

function autoPause(id) {
  const st = autoPreviews.get(+id);
  if (st && st.timer) { clearInterval(st.timer); st.timer = 0; }
}

/* Nothing is asked here. The listing already carries every ready clip's frame list
   (`frames: []` included, so "this clip has no strip" is an ANSWER, not a question), and
   a poster that arrives later carries its list on the SSE event. Asking /api/frames per
   tile was one request per clip per view -- the traffic the owner saw in the log
   (owner, 1.4.5). A strip built later is picked up from the status counter (frames_gen). */
function autoTile(cell) {
  const id = +cell.dataset.id;
  if (!id || !cell.dataset.kind || cell.dataset.kind !== "video") return;
  if (autoPreviews.has(id)) { autoStart(id); return; }
  const urls = Array.isArray(vframes[id]) ? vframes[id] : null;
  if (!urls || urls.length < 2) return;        // unknown, or a clip with nothing to flip
  const cur = cell.querySelector("img");
  const st = {urls, i: 0, timer: 0, poster: cur ? cur.src : null};
  autoPreviews.set(id, st);
  // stagger the first step, or every tile in the grid flips together
  setTimeout(() => { if (autoPreviews.get(id) === st) autoStart(id); },
             Math.floor(Math.random() * AUTO_BEAT));
}

/* A poster just landed for a tile on screen: the event carries its frame list, so the
   preview can start with no request at all. */
function autoRearm(cell, frames) {
  if (!cell || cell.dataset.kind !== "video") return;
  if (!framesOn()) return;
  const id = +cell.dataset.id;
  if (Array.isArray(frames)) vframes[id] = frames;
  autoPreviews.delete(id);
  autoTile(cell);
}

function stopAutoPreview() {
  for (const [id, st] of autoPreviews) {
    if (st.timer) clearInterval(st.timer);
    const im = autoImg(id, false);
    if (im) clearFade(im);
    if (im && st.poster) im.src = st.poster;
  }
  autoPreviews.clear();
  if (autoObserver) { autoObserver.disconnect(); autoObserver = null; }
}

function startAutoPreview() {
  stopAutoPreview();
  if (!framesOn()) return;
  // Reduced motion is the OS hint, not the owner's instruction. Honour it only while
  // the switch is untouched; once it has been set by hand ("mx.frames" stored), that
  // explicit choice wins. The guard used to bail out silently in BOTH cases, so a
  // machine with "show animations" off in Windows showed a switch reading ON and no
  // tile animating -- indistinguishable from the preview being broken.
  let explicit = true;
  try { explicit = localStorage.getItem(FRAMES_PREF) !== null; } catch (_) { explicit = false; }
  if (!explicit) {
    try {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        console.info("[mx] ambient preview is off because this system prefers reduced motion;"
                     + " toggle the frame button to force it on");
        document.documentElement.dataset.mxReduced = "1";   // so the switch can say WHY
        applyFramesPref();
        return;
      }
    } catch (_) {}
  }
  if (!autoObserver && "IntersectionObserver" in window) {
    autoObserver = new IntersectionObserver(entries => {
      for (const e of entries) {
        const id = +e.target.dataset.id;
        if (e.isIntersecting) autoTile(e.target);
        else { autoPause(id); autoFill(); }   // off screen: free the budget for the view
      }
    }, { rootMargin: "150px" });
  }
  // Every video tile is OBSERVED, not just the first AUTO_MAX in listing order: in a
  // folder of 300 clips the old slice left tiles 49+ still for good, however far you
  // scrolled. The ceiling is now on tiles RUNNING at once (autoTrim), ranked by
  // distance from the middle of the screen, so the motion follows the viewport.
  // The nearest ones still start immediately rather than waiting to be told they are
  // visible: an IntersectionObserver that never fires (headless/embedded browsers do
  // this) would otherwise leave the preview silently dead.
  const cells = [...document.querySelectorAll('#grid .cell[data-kind="video"]')];
  const nearest = cells.map(c => [c, autoRank(c)]).sort((a, b) => a[1] - b[1]);
  for (const [c] of nearest.slice(0, AUTO_MAX)) autoTile(c);
  if (autoObserver) for (const c of cells) autoObserver.observe(c);
  // pause the whole show while the tab is in the background
  if (!window.__mxVisHooked) {
    window.__mxVisHooked = true;
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopAutoPreview(); else startAutoPreview();
    });
  }
}

function tile(it) {
  const cell = div("cell");
  cell.dataset.id = it.id;
  cell.dataset.kind = it.kind;
  const box = div("thumbbox");
  cell.appendChild(box); cell.appendChild(div("name", esc(it.name)));
  cell.title = it.name;

  if (it.kind === "image") {
    if (it.thumb) box.appendChild(thumbRetry(img(it.thumb, it.name), it));
    else if (it.bad) box.appendChild(div("ph bad", ICON.warn));      // bytes are damaged
    else box.appendChild(div("ph pending", it.pending ? "…" : "?"));
    if (it.bad) cell.title = it.name + " — " + (it.why || "cannot be read");
    cell.onclick = () => { if (performance.now() < closeGuardUntil) return; openViewer(it); };
    cell.oncontextmenu = e => showCtx(e, { loc: state.loc, kind: "image", name: it.name, item: it,
                                           nodeId: it.zip_entry || state.kind === "zip" ? null : it.id });
  } else if (it.kind === "video") {
    const dur = it.dur || durCache[it.id];
    if (dur) box.appendChild(durChip(dur));
    // the listing carries the clip's frame list: seed the cache the ambient preview
    // and the hover flipbook both read, so neither has to ask /api/frames per tile
    if (Array.isArray(it.frames)) vframes[it.id] = it.frames;
    if (it.thumb) {
      const vimg = thumbRetry(img(it.thumb, it.name), it);
      box.appendChild(vimg);
      // Only a video INSIDE a zip keeps a badge: the ⚠ warns that it cannot be
      // previewed like a loose file. Loose videos need nothing -- the filmstrip
      // animates on hover and the tile is obviously a clip.
      if (it.zip_entry) {
        const b = span("vidbadge", ICON.warn);
        b.title = "video inside a zip";
        box.appendChild(b);
      }
      cell.onmouseenter = () => { if (!it.zip_entry) armVideoHover(it.id, vimg, it.thumb, cell); };
      cell.onmouseleave = () => { if (!it.zip_entry) endVideoHover(); };
    } else {
      // no poster yet (fresh store / pack not built): still arm the hover, the
      // placeholder is what the filmstrip replaces when the frames arrive
      box.appendChild(div("ph" + (it.pending ? " pending" : ""),
                          it.zip_entry ? ICON.warn + ICON.film : ICON.film));
      cell.onmouseenter = () => { if (!it.zip_entry) armVideoHover(it.id, null, null, cell); };
      cell.onmouseleave = () => { if (!it.zip_entry) endVideoHover(); };
    }
    cell.onclick = () => it.zip_entry ? openZipVideo(it) : openPlayer(it);
    if (!it.zip_entry) cell.oncontextmenu = e => showCtx(e, { loc: state.loc, kind: "video", nodeId: it.id, name: it.name, item: it });
  } else if (it.kind === "dir") {
    if (it.thumb) box.appendChild(mosaicTile(it));
    else box.appendChild(span("videomark", ICON.folder));
    cell.onclick = () => openFolder(it.id);
    cell.oncontextmenu = e => showCtx(e, { loc: state.loc, parent: it.id, name: it.name,
                                           nodeId: it.id, kind: "dir" });
  } else if (it.kind === "zip") {
    box.appendChild(mosaicTile(it));
    if (it.has_video) box.appendChild(span("zipwarn", ICON.warn, "contains video"));
    if (it.bad) {
      // Images inside that cannot be decoded. The count matters: one bad entry in a
      // 200-image archive is a different repair job from an archive that is all damage.
      box.appendChild(span("zipbad", ICON.warn + "<b>" + it.bad + "</b>",
        it.bad === 1 ? "1 image inside this zip can't be read - a candidate to compare with the backup"
                     : it.bad + " images inside this zip can't be read - a candidate to compare with the backup"));
    }
    cell.onclick = () => openZip(it.id, it.name);
    // kind "zip", not "dir": a zip browses like a folder but the menu must not offer to
    // scan subfolders or build filmstrips in it -- zips have none (owner, v1.0.1)
    cell.oncontextmenu = e => showCtx(e, { loc: state.loc, parent: it.id, kind: "zip", nodeId: it.id, name: it.name });
  }
  return cell;
}

/* ---------- running time on video tiles (v0.1.241) ----------
   "0:39", "12:05", "1:02:15". The server stores the length the preview build
   measures anyway; clips listed before that existed are probed in the background and
   their length arrives as a "dur" event, which is applied in place. */
function fmtDur(sec) {
  sec = Math.max(0, Math.round(+sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
  const ss = String(s).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
function durChip(sec) {
  const c = span("durchip", fmtDur(sec));
  c.title = "Running time";
  return c;
}
/* Every length the page has been told, by id. A "dur" event can arrive before its
   tile exists (the listing that queued the probe is still being painted) or just
   before the tile is rebuilt from an older listing; either way the label used to be
   lost until the folder was opened again. tile() reads this as well as it.dur. */
const durCache = {};
function applyDur(ev) {
  if (ev.dur > 0) durCache[ev.id] = ev.dur;
  const it = (state.items || []).find(x => x.id === ev.id);
  if (it) it.dur = ev.dur;
  const cell = document.querySelector(`#grid .cell[data-id="${ev.id}"]`);
  const box = cell && cell.querySelector(".thumbbox");
  if (!box || !(ev.dur > 0)) return;
  const cur = box.querySelector(".durchip");
  if (cur) cur.textContent = fmtDur(ev.dur);
  else box.appendChild(durChip(ev.dur));
}

function mosaicTile(it) {
  // A zip (or folder) tile IS the pictures: the first four images fill the tile,
  // framed by one thick amber border. No folder glyph -- at tile size the icon
  // ate the space the pictures needed. Folders get the same treatment so a
  // folder of images reads as its content instead of a bare icon.
  const z = div("zipbox");
  if (it.thumb) {
    const t = thumbRetry(img(it.thumb, it.name), it); t.className = "full tile"; z.appendChild(t);
  } else if ((it.samples || []).length) {
    const s = div("samples");
    it.samples.forEach(u => s.appendChild(img(u, "")));
    // a dead sample shouldn't leave a torn quadrant: drop it, keep the rest
    s.querySelectorAll("img").forEach(im => {
      im.onerror = () => im.remove();
      if (im.complete && im.naturalWidth === 0) im.remove();
    });
    z.appendChild(s);
  } else {
    z.appendChild(div("ph zpending", it.pending ? "…" : ""));
  }
  return z;
}

/* ---------- viewer (full screen images) ---------- */
/* ---------- "I like full screen" is a remembered preference ---------- */
// v2 key: "full screen unless I said otherwise" is a new default, and the old key
// holds whatever past Escapes left behind -- they must not silently veto it once.
const FS_PREF = "mx.fullscreen2";
function fsPreferred() {
  try { const v = localStorage.getItem(FS_PREF); return v === null ? true : v === "1"; }
  catch (_) { return true; }
}
function setFsPreferred(on) { try { localStorage.setItem(FS_PREF, on ? "1" : "0"); } catch (_) {} }
let fsSelfExit = false;                 // kept for the legacy fullscreenchange handler

/* 0.1.219 — the app never calls the Fullscreen API.
   Chrome paints "To exit full screen, press Esc" over the picture every time a page asks
   for full screen, and nothing on the page can suppress it (no flag, no policy; the
   window-metrics heuristic in 0.1.217 was the wrong answer because it could not tell a
   windowed browser from a full-screen one reliably). #viewer/#player are already fixed
   overlays covering the window, so "full screen" here is just the .fs look: no caption,
   no counter, image at 100vh. Press F11 once for a chrome-free screen — the picture then
   fills it exactly the same, with no browser popup. F toggles the look, not the window. */
function goFs(el) {
  if (!el) return;
  setFsPreferred(true);                 // entering full screen is the vote
  el.classList.add("fs");
  zoomClamp(); zoomApply();
}
function toggleFs(el) {
  if (!el) return;
  const on = !el.classList.contains("fs");
  setFsPreferred(on);
  el.classList.toggle("fs", on);
  zoomClamp(); zoomApply();
}
function markFs() {
  // The .fs look is the app's own now (see goFs): a native-fullscreen event, if one ever
  // happens (double-clicking a <video> with browser controls), must not remove it.
  setTimeout(() => { zoomClamp(); zoomApply(); }, 120);
}
document.addEventListener("fullscreenchange", markFs);
document.addEventListener("webkitfullscreenchange", markFs);
document.addEventListener("fullscreenchange", () => {
  if (document.fullscreenElement || document.webkitFullscreenElement) return;
  if (fsSelfExit) { fsSelfExit = false; return; }
  // user pressed Esc/F11 while the picture was still up: they want the window back
  const open = ($("#viewer") && !$("#viewer").hidden) || ($("#player") && !$("#player").hidden);
  if (open) setFsPreferred(false);
});

function openViewer(it) {
  viewerHopped = false;
  state.navList = state.items.filter(x => x.kind === "image");
  state.navIdx = state.navList.findIndex(x => x.id === it.id);
  // the info panel remembers whether it was open last time
  paintInfoState();
  showViewerImage();
  $("#viewer").hidden = false;
  if (fsPreferred()) goFs($("#viewer"));
  // viewer is one breadcrumb step: push /i{itemId} so Back returns to the grid
  const h = buildHash() + "/i" + it.id;
  if (location.hash !== h) { hashGuard = true; location.hash = h; }
}
function viewerSrc(it) {
  if (!it || it.id == null) return "";
  // versioned so a recycled id can never resolve to a previously cached file
  const v = Math.round((it.mtime || 0) * 1000);
  // A picture opened inside a zip is an entry, whatever the item says: entry ids and
  // node ids overlap, so the node route would serve a DIFFERENT photo.
  const entry = it.zip_entry || state.kind === "zip";
  return entry ? `/api/file/entry/${it.id}?v=${v}` : `/api/file/node/${it.id}?v=${v}`;
}
function showViewerImage(keepZoom) {
  const it = state.navList[state.navIdx];
  if (!it) return;
  if (!keepZoom) zoomReset();
  const img = $("#viewer-img");
  const src = viewerSrc(it);
  if (!src) { hideViewer(); return; }                // a cell without an id: never request /undefined
  img.onload = () => { zoomClamp(); zoomApply(); };   // new aspect: re-bound the pan
  img.src = src;
  $("#viewer-caption").textContent = it.name;
  $("#viewer-counter").textContent = `${state.navIdx + 1} / ${state.navList.length}`;
  preloadAround();
  if (infoOpen) loadInfo(it);
}

/* ---------- picture info panel (I) (v0.1.244) ----------
   Camera EXIF for photos; for generated images every text block embedded in the
   file -- ComfyUI's `prompt` graph, A1111/Civitai `parameters` -- with a readable
   summary on top (prompts labelled positive/negative, sampler settings, models) and
   the raw blocks below in their own scrolling boxes. Read on demand, one request per
   picture, cached for the session; the open/closed state is remembered. */
const INFO_PREF = "mx.info";
let infoOpen = (() => { try { return localStorage.getItem(INFO_PREF) === "1"; } catch (_) { return false; } })();
let infoSeq = 0;
const infoCache = new Map();          // info URL -> response
function infoUrl(it) {
  if (it.kind === "video") return `/api/info/node/${it.id}`;   // loose clips only
  const entry = it.zip_entry || state.kind === "zip";      // same rule as viewerSrc
  return `/api/info/${entry ? "entry" : "node"}/${it.id}`;
}
function setInfoOpen(on) {
  infoOpen = !!on;
  try { localStorage.setItem(INFO_PREF, infoOpen ? "1" : "0"); } catch (_) {}
  paintInfoState();
  if (infoOpen) {
    if (!$("#player").hidden && state.playerItem) loadInfo(state.playerItem, $("#player-info"));
    else loadInfo(state.navList[state.navIdx]);
  }
  requestAnimationFrame(() => { zoomClamp(); zoomApply(); });   // the picture's box changed
}
/* One remembered open/closed state for both panels: photo viewer and video player. */
function paintInfoState() {
  $("#viewer-info").hidden = !infoOpen;
  $("#viewer").classList.toggle("info-open", infoOpen);
  $("#btn-vinfo").classList.toggle("on", infoOpen);
  $("#player-info").hidden = !infoOpen;
  $("#player").classList.toggle("info-open", infoOpen);
  $("#btn-pinfo").classList.toggle("on", infoOpen);
}
function fmtDur(sec) {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
}
const LANG = { eng: "English", fre: "French", fra: "French", ger: "German", deu: "German",
  spa: "Spanish", ita: "Italian", jpn: "Japanese", chi: "Chinese", zho: "Chinese", kor: "Korean",
  rus: "Russian", por: "Portuguese", dut: "Dutch", nld: "Dutch", swe: "Swedish", nor: "Norwegian",
  dan: "Danish", fin: "Finnish", pol: "Polish", ara: "Arabic", hin: "Hindi", tur: "Turkish" };
const CH = { 1: "mono", 2: "stereo", 6: "5.1", 8: "7.1" };
function renderVideoInfo(d) {
  const row = (k, v) => v == null || v === "" ? "" :
    `<div class="vi-row"><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`;
  const flag = (t, cls = "") => `<span class="vi-flag ${cls}">${esc(t)}</span>`;
  const flags = t => (t.default ? flag("default") : "") + (t.forced ? flag("forced") : "");
  const lang = t => t.lang ? (LANG[t.lang] || t.lang) : "";
  const p = [`<div class="vi-name">${esc(d.name || "")}</div>`];
  const top = [d.duration ? fmtDur(d.duration) : "", fmtBytes(d.size),
               d.bitrate ? `${(d.bitrate / 1000).toFixed(1)} Mb/s` : ""].filter(Boolean).join(" · ");
  if (top) p.push(`<div class="vi-dim">${esc(top)}</div>`);
  if (d.error) { p.push(`<div class="vi-dim">${esc(d.error)}</div>`); return p.join(""); }
  const ft = d.file_tags || {};
  const gps = d.gps ? `<div class="vi-row"><span>Location</span><b><a href="https://www.openstreetmap.org/?mlat=${d.gps.lat}&mlon=${d.gps.lon}#map=16/${d.gps.lat}/${d.gps.lon}" target="_blank" rel="noopener">${d.gps.lat}, ${d.gps.lon}</a></b></div>` : "";
  p.push(`<h4>File</h4>` + row("Container", d.container) +
         Object.entries(ft).map(([k, v]) => row(k, v)).join("") + gps);
  if ((d.video_tracks || []).length) {
    p.push(`<h4>Video</h4>`);
    for (const v of d.video_tracks) {
      const res = v.width && v.height ? `${v.width} × ${v.height}` : "";
      const line = [v.profile, v.depth ? `${v.depth}-bit` : "", v.fps ? `${+v.fps.toFixed(3)} fps` : "",
                    v.bitrate ? `${(v.bitrate / 1000).toFixed(1)} Mb/s` : "", v.dar ? `aspect ${v.dar}` : "",
                    v.scan ? "interlaced" : "", v.rotate ? `rotated ${v.rotate}°` : ""].filter(Boolean).join(" · ");
      p.push(`<div class="vi-track"><div class="vi-th"><b>${esc((v.codec_id || "").toUpperCase())}</b><small>${esc(res)}</small>` +
             `<span class="vi-flags">${v.hdr ? flag(v.hdr, "hdr") : flag("SDR")}</span></div>` +
             (line ? `<div class="vi-tl">${esc(line)}</div>` : "") +
             (v.codec ? `<div class="vi-tl vi-dim">${esc(v.codec)}</div>` : "") + `</div>`);
    }
  }
  if ((d.audio_tracks || []).length) {
    p.push(`<h4>Audio <small>${d.audio_tracks.length} track${d.audio_tracks.length === 1 ? "" : "s"}</small></h4>`);
    d.audio_tracks.forEach((a, i) => {
      const ch = a.layout || CH[a.channels] || (a.channels ? `${a.channels} ch` : "");
      const line = [ch, a.rate ? `${+(a.rate / 1000).toFixed(1)} kHz` : "", a.bitrate ? `${a.bitrate} kb/s` : "",
                    a.profile].filter(Boolean).join(" · ");
      p.push(`<div class="vi-track"><div class="vi-th"><b>${i + 1}. ${esc(lang(a) || "Unknown language")}</b>` +
             `<small>${esc((a.codec || "").toUpperCase())}</small><span class="vi-flags">${flags(a)}</span></div>` +
             (a.title ? `<div class="vi-tl">${esc(a.title)}</div>` : "") +
             (line ? `<div class="vi-tl vi-dim">${esc(line)}</div>` : "") + `</div>`);
    });
  }
  if ((d.sub_tracks || []).length) {
    p.push(`<h4>Subtitles <small>${d.sub_tracks.length} track${d.sub_tracks.length === 1 ? "" : "s"}</small></h4>`);
    d.sub_tracks.forEach((t, i) => {
      p.push(`<div class="vi-track"><div class="vi-th"><b>${i + 1}. ${esc(lang(t) || "Unknown language")}</b>` +
             `<small>${esc((t.codec || "").replace("hdmv_pgs_subtitle", "PGS").replace("subrip", "SRT").toUpperCase())}</small>` +
             `<span class="vi-flags">${flags(t)}</span></div>` +
             (t.title ? `<div class="vi-tl">${esc(t.title)}</div>` : "") + `</div>`);
    });
  }
  if ((d.chapters || []).length) {
    p.push(`<h4>Chapters <small>${d.chapters.length} · click to jump</small></h4>` +
           d.chapters.map((c, i) => `<div class="vi-chap" data-t="${c.start}"><span>${fmtDur(c.start)}</span>` +
                                    `<b>${esc(c.title || `Chapter ${i + 1}`)}</b></div>`).join(""));
  }
  return p.join("");
}
function fmtBytes(n) {
  if (!n && n !== 0) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0; n = +n;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}
const SETTING_LABELS = { noise_seed: "Seed", seed: "Seed", steps: "Steps", cfg: "CFG",
  sampler_name: "Sampler", scheduler: "Scheduler", denoise: "Denoise" };
function copyText(text, btn) {
  const done = () => { if (btn) { const t = btn.textContent; btn.textContent = "Copied"; setTimeout(() => btn.textContent = t, 1200); } };
  // the Clipboard API exists only on https/localhost; plain http on the LAN needs the
  // old textarea route
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => {});
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch (_) {}
  ta.remove();
}
function renderInfo(d) {
  const row = (k, v) => v == null || v === "" ? "" :
    `<div class="vi-row"><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`;
  const parts = [];
  parts.push(`<div class="vi-name">${esc(d.name || "")}</div>`);
  const dims = [d.width && d.height ? `${d.width} × ${d.height}` : "", d.format || "",
                fmtBytes(d.size)].filter(Boolean).join(" · ");
  if (dims) parts.push(`<div class="vi-dim">${esc(dims)}</div>`);
  if (d.error) parts.push(`<div class="vi-dim">${esc(d.error)}</div>`);
  const c = d.camera;
  if (c) {
    const expo = [c.aperture, c.shutter, c.iso ? `ISO ${c.iso}` : "", c.focal].filter(Boolean).join(" · ");
    const gps = c.gps ? `<a href="https://www.openstreetmap.org/?mlat=${c.gps.lat}&mlon=${c.gps.lon}#map=16/${c.gps.lat}/${c.gps.lon}" target="_blank" rel="noopener">${c.gps.lat}, ${c.gps.lon}</a>` : "";
    parts.push(`<h4>Camera</h4>` + row("Taken", c.taken) + row("Camera", c.camera) +
               row("Lens", c.lens) + row("Exposure", expo) + row("Software", c.software) +
               (gps ? `<div class="vi-row"><span>Location</span><b>${gps}</b></div>` : ""));
  }
  const s = d.summary;
  if (s) {
    parts.push(`<h4>Generation <small>${esc(s.source)}</small></h4>`);
    (s.prompts || []).forEach((p, i) => {
      parts.push(`<div class="vi-prompt vi-${esc(p.role)}"><div class="vi-ph"><span>${esc(p.role)}</span>` +
                 `<button type="button" data-copy="p${i}">Copy</button></div>` +
                 `<pre data-src="p${i}">${esc(p.text)}</pre></div>`);
    });
    (s.settings || []).forEach(st => {
      const rows = Object.entries(st).filter(([k]) => k !== "node")
        .map(([k, v]) => row(SETTING_LABELS[k] || k, v)).join("");
      if (rows) parts.push(`<div class="vi-set">${st.node ? `<div class="vi-node">${esc(st.node)}</div>` : ""}${rows}</div>`);
    });
    if ((s.models || []).length) {
      parts.push(`<div class="vi-set">` + s.models.map(m =>
        row(m.kind, m.name + (m.strength != null ? ` (${m.strength})` : ""))).join("") + `</div>`);
    }
  }
  if ((d.texts || []).length) {
    parts.push(`<h4>Embedded text <small>${d.texts.length} block${d.texts.length === 1 ? "" : "s"}</small></h4>`);
    d.texts.forEach((t, i) => {
      parts.push(`<div class="vi-raw"><div class="vi-ph"><span>${esc(t.key)}</span>` +
                 `<small>${t.chars.toLocaleString()} chars${t.json ? " · JSON" : ""}${t.cut ? " · truncated" : ""}</small>` +
                 `<button type="button" data-copy="t${i}">Copy</button></div>` +
                 `<pre data-src="t${i}">${esc(t.value)}</pre></div>`);
    });
  }
  if (!c && !s && !(d.texts || []).length && !d.error) {
    parts.push(`<div class="vi-dim vi-none">No camera data or embedded text in this file.</div>`);
  }
  return parts.join("");
}
/* Every panel render carries its own close button: the panels replace innerHTML,
   so a static button in index.html would be wiped on the first render. */
function paintPanel(box, html) {
  box.innerHTML = `<button class="vi-close" type="button" title="Close info (I)"
    aria-label="Close info">\u00d7</button>` + html;
}
async function loadInfo(it, box = $("#viewer-info")) {
  if (!it || !box) return;
  const seq = ++infoSeq;
  const url = infoUrl(it);
  let d = infoCache.get(url);
  if (!d) {
    paintPanel(box, `<div class="vi-name">${esc(it.name || "")}</div><div class="vi-dim">Reading…</div>`);
    try { d = await api(url); } catch (_) {
      if (seq === infoSeq) paintPanel(box, `<div class="vi-name">${esc(it.name || "")}</div><div class="vi-dim">Could not read this file's metadata.</div>`);
      return;
    }
    infoCache.set(url, d);
    while (infoCache.size > 60) infoCache.delete(infoCache.keys().next().value);
  }
  if (seq !== infoSeq) return;          // stepped to another picture meanwhile
  paintPanel(box, d.video ? renderVideoInfo(d) : renderInfo(d));
  box.scrollTop = 0;
  box.querySelectorAll(".vi-chap").forEach(c => {
    c.onclick = e => { e.stopPropagation(); pv.currentTime = +c.dataset.t; pShow(false); };
  });
  box.querySelectorAll("button[data-copy]").forEach(b => {
    b.onclick = e => {
      e.stopPropagation();
      const pre = box.querySelector(`pre[data-src="${b.dataset.copy}"]`);
      if (pre) copyText(pre.textContent, b);
    };
  });
}
$("#btn-vinfo").onclick = e => { e.stopPropagation(); setInfoOpen(!infoOpen); };
["#viewer-info", "#player-info"].forEach(sel => {
  $(sel).addEventListener("click", e => {
    if (!e.target.closest(".vi-close")) return;
    e.stopPropagation(); setInfoOpen(false);
  });
});
// The panel sits inside the viewer: its wheel must scroll the panel, not page the
// photos; a drag in it must select text, not pan; a double click must not close.
["wheel", "mousedown", "dblclick", "click", "auxclick"].forEach(ev =>
  $("#viewer-info").addEventListener(ev, e => e.stopPropagation(), { passive: true }));
$("#btn-vinfo").addEventListener("dblclick", e => e.stopPropagation());
$("#btn-pinfo").onclick = e => { e.stopPropagation(); setInfoOpen(!infoOpen); };
["wheel", "mousedown", "dblclick", "click", "auxclick"].forEach(ev => {
  $("#player-info").addEventListener(ev, e => e.stopPropagation(), { passive: true });
  $("#btn-pinfo").addEventListener(ev, e => e.stopPropagation(), { passive: true });
});

/* ---------- look-ahead for the photo viewer (v0.1.244) ----------
   It already asked for the neighbours, but with a throwaway `new Image()`: nothing
   kept a reference, so the browser was free to drop it before you stepped, and a zip
   picture (no cache validator) was then downloaded a second time -- measured: two of
   five steps through 12.zip re-fetched, 36-54ms instead of ~3. Now the look-ahead
   images are HELD (a small LRU) and DECODED in advance, so a 12-megapixel photo is
   ready to paint, and the one after next in the direction you are going is fetched
   too. The previous one stays warm for stepping back. */
let viewerDir = 1;
const viewerAhead = new Map();       // src -> HTMLImageElement, insertion-ordered LRU
const VIEWER_AHEAD_MAX = 6;
function preloadOne(it) {
  const src = viewerSrc(it);
  if (!src) return;
  if (viewerAhead.has(src)) {                     // refresh its place in the LRU
    const im = viewerAhead.get(src); viewerAhead.delete(src); viewerAhead.set(src, im);
    return;
  }
  const im = new Image();
  im.decoding = "async";
  im.src = src;
  if (im.decode) im.decode().catch(() => {});      // decode off the paint path
  viewerAhead.set(src, im);
  while (viewerAhead.size > VIEWER_AHEAD_MAX) viewerAhead.delete(viewerAhead.keys().next().value);
}
function preloadAround() {
  // no wrap: nothing beyond the ends of the list
  for (const d of [viewerDir, -viewerDir, 2 * viewerDir]) {
    const nx = state.navList[state.navIdx + d];
    if (nx && nx.id != null) preloadOne(nx);
  }
}
// When something a user can see goes wrong, say so on screen. A browser console
// message names no URL and is invisible to the person reporting it; this chip carries
// the status and the request, so a bug report can be one glance instead of a hunt.
function showErrChip(text) {
  let box = $("#errchips");
  if (!box) { box = div(""); box.id = "errchips"; document.body.appendChild(box); }
  const c = div("errchip");
  c.textContent = text;
  box.appendChild(c);
  setTimeout(() => c.remove(), 12000);
  while (box.children.length > 3) box.firstChild.remove();
}

// A rejected fetch() never reaches the resource-error listener below, so wrap the
// app's API helper once and report every non-ok response with its URL and status.
(() => {
  const of = window.fetch.bind(window);
  window.__mxFetch = of;                 // un-chipped fetch, for probes below
  // A thumbnail that is mid-rebuild answers 404 ONCE and re-pends itself; the tile
  // retries and gets the picture a second later. Chipping that turned a self-healing
  // second into a wall of red (owner's report, v0.1.193) -- so image 404s are logged
  // but never chipped. Everything else still is.
  const quiet = (u, st) => st === 404 && /\/api\/(thumb|framefile)\//.test(u);
  window.fetch = function (url, opts) {
    const u = typeof url === "string" ? url : (url && url.url) || "";
    return of.apply(this, arguments).then(r => {
      if (!r.ok && u && u.indexOf("/api/log") === -1) {
        clog("err", { req: String(u).slice(0, 300), status: r.status });
        if (!quiet(u, r.status)) {
          showErrChip(`${r.status} ${String(u).replace(location.origin, "")}`);
        }
      }
      return r;
    });
  };
})();

// "Failed to load resource: 422" in the console names no URL, and the server's
// access log is not reachable from here. Any failed <img>/<script> is reported with
// its URL, and the status is probed once so the log says WHICH request was rejected.
window.addEventListener("error", ev => {
  const t = ev.target;
  if (!t || t === window || !t.src) return;
  const src = String(t.src);
  if (src.startsWith("data:")) return;
  clog("err", { res: t.tagName, src: src.slice(0, 300) });
  (window.__mxFetch || fetch)(src, { cache: "reload" }).then(r => {
    if (!r.ok) clog("err", { res: t.tagName, src: src.slice(0, 300), status: r.status });
  }).catch(() => {});
}, true);

/* ---------- zoom + pan (no build step: the viewer already streams the original) ----
   + / -  zoom 1.3x per press, 0 or * fits the whole picture, / snaps to 100% (one
   picture pixel per screen pixel), arrows and drag pan while zoomed, wheel steps to the
   next picture at fit and zooms once you are in. */
const zoom = { s: 1, x: 0, y: 0 };
function zoomApply() {
  const i = $("#viewer-img");
  if (!i) return;
  i.style.transform = `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.s})`;
  $("#viewer").classList.toggle("zoomed", zoom.s > 1.001);
}
function contentSize() {
  const i = $("#viewer-img");
  const bw = i.clientWidth || 1, bh = i.clientHeight || 1;
  const nw = i.naturalWidth || bw, nh = i.naturalHeight || bh;
  const k = Math.min(bw / nw, bh / nh);          // object-fit: contain
  return { cw: nw * k * zoom.s, ch: nh * k * zoom.s, bw, bh };
}
function zoomClamp() {
  const { cw, ch, bw, bh } = contentSize();
  // you may pan until the picture's edge meets the window's edge, never past it
  const mx = Math.max(0, (cw - bw) / 2), my = Math.max(0, (ch - bh) / 2);
  zoom.x = Math.max(-mx, Math.min(mx, zoom.x));
  zoom.y = Math.max(-my, Math.min(my, zoom.y));
}
function zoomBy(f) {
  // From a 100% view (which can sit below fit) stepping out is allowed to keep shrinking;
  // starting at fit or above, the step floor stays at fit so '-' cannot walk into a
  // picture too small to see.
  const floor = zoom.s < 1 ? 0.05 : 1;
  zoom.s = Math.max(floor, Math.min(8, zoom.s * f));
  if (zoom.s > 0.96 && zoom.s < 1.04) zoom.s = 1;   // a step that lands on fit means fit
  if (zoom.s <= 1.001) { zoom.x = zoom.y = 0; }
  zoomClamp(); zoomApply();
}
function zoomReset() { zoom.s = 1; zoom.x = zoom.y = 0; zoomApply(); }
/* 100% = one picture pixel per screen pixel (owner, v1.1.2). The viewer letterboxes the
   file into its box, so scale 1 is "fit", not "actual": the factor is how much smaller
   the fitted content is than the file itself, and 1/k snaps it to 1:1. Deliberately not
   capped at the 8x the stepping keys use -- a 24MP photo in a 1000px box needs ~5x, a
   6000px scan needs more, and 100% is a promise about pixels, not a zoom step. */
function zoomActual() {
  const i = $("#viewer-img");
  if (!i || !i.naturalWidth) return;
  const bw = i.clientWidth || 1, bh = i.clientHeight || 1;
  const k = Math.min(bw / i.naturalWidth, bh / i.naturalHeight);
  if (!(k > 0)) return;
  // Below 1 is the point, not an accident: a 500x500 file in a 1400px window is enlarged
  // to fit, so 100% has to make it SMALLER -- the picture sits at its true size in the
  // middle, which is what the owner asked for and what "fit" can never show.
  zoom.s = Math.max(0.05, Math.min(40, 1 / k));
  if (Math.abs(zoom.s - 1) < 0.01) zoom.s = 1;      // the box happens to be 1:1 already
  if (zoom.s <= 1.001) zoom.x = zoom.y = 0;
  zoomClamp(); zoomApply();
}
function panBy(dx, dy) { zoom.x += dx; zoom.y += dy; zoomClamp(); zoomApply(); }

function viewerStep(d) {
  if ($("#viewer").hidden) return;
  viewerDir = d > 0 ? 1 : -1;        // which way to look ahead (see preloadAround)
  const next = state.navIdx + d;
  if (next >= 0 && next < state.navList.length) {
    state.navIdx = next;
    showViewerImage(true);           // next picture at the same zoom and position
    return;
  }
  // Past the last (or before the first) picture: keep walking into the next folder or
  // zip -- the same depth-first order the grid's scroll-hop walks.
  viewerCrossContainer(d);
}

/* ---------- walking from one container into the next (pictures only) ----------
   This runs the same hopNext/hopPrev the grid's scroll-hop runs, so the viewer and
   the grid always agree on what "next" means: depth first, and backwards lands at
   the END of the previous branch (its last picture), never at its first. */
let viewerHopped = false;          // the open viewer walked into a neighbour container
let crossing = false;              // one hop at a time: a fast wheel fires many steps

async function viewerCrossContainer(d) {
  if (crossing) return false;
  crossing = true;
  try {
    for (let k = 0; k < 8; k++) {
      const t = d > 0 ? await hopNext() : await hopPrev();
      if (!t) return false;                      // nothing that way: an end of the library
      hashHold = true;                           // paint the neighbour's grid silently...
      if (t.kind === "zip") await openZip(t.id, t.name, newNav());
      else await openFolder(t.id, newNav());
      hashHold = false;
      const landed = t.kind === "zip" ? (state.kind === "zip" && state.zipId === t.id)
                                      : (state.kind === "dir" && state.parent === t.id);
      if (!landed) return false;                 // the user clicked away mid-hop
      if ($("#viewer").hidden) return false;     // or closed the viewer
      // the picture list comes off state.items, not off our own fetch: a pinned folder
      // re-lists in the pinned order, and the viewer must follow the grid
      const imgs = state.items.filter(x => x.kind === "image");
      if (!imgs.length) continue;                // a branch of videos or folders: step over it
      const it = d > 0 ? imgs[0] : imgs[imgs.length - 1];
      state.navList = imgs;
      state.navIdx = imgs.indexOf(it);
      showViewerImage();                         // new container: zoom back to fit
      const g = $("#grid");
      if (g) g.scrollTop = d > 0 ? 0 : g.scrollHeight;   // the grid lands with the viewer
      // replace, not push: the whole wheel-through stays one history entry, so Esc
      // closes to this container's grid instead of stacking an /i trail
      viewerHopped = true;
      history.replaceState(null, "", buildHash() + "/i" + it.id);
      return true;
    }
    return false;
  } finally {
    crossing = false;
    hashHold = false;
  }
}
function closeViewer() {
  if ($("#viewer").hidden) return;
  // A viewer session that walked into another archive owns a single history entry, so
  // popping it would step back to the *previous* archive's picture and reopen that.
  // Closing lands on the grid of the archive you actually ended in instead.
  if (viewerHopped) {
    viewerHopped = false;
    history.replaceState(null, "", buildHash());
    hideViewer();
    return;
  }
  if (location.hash === buildHash()) { hideViewer(); return; }  // hash already at grid
  history.back();                                               // pop the /i entry
}
// Chromium keeps a hidden element stuck in the fullscreen top-layer (unresponsive
// clicks until F11); explicitly leave fullscreen BEFORE hiding an overlay.
function exitOverlayFs() {
  // the no-API full-screen look (browser already full screen) is ours to remove
  for (const sel of ["#viewer", "#player"]) { const e = $(sel); if (e) e.classList.remove("fs"); }
  const el = document.fullscreenElement || document.webkitFullscreenElement;
  if (!el) return;
  const v = $("#viewer"), p = $("#player");
  if (el === v || el === p || v.contains(el) || p.contains(el)) {
    fsSelfExit = true;
    const exit = document.exitFullscreen || document.webkitExitFullscreen;
    if (exit) try { exit.call(document); } catch (_) {}
  }
}
/* Back from an overlay puts the grid where the picture you were last looking at is,
   not where you started: wheeling through forty photos and landing at the top of the
   folder loses your place. */
function revealInGrid(id) {
  if (id == null) return;
  const g = $("#grid"), cell = g && g.querySelector(`.cell[data-id="${id}"]`);
  if (!cell) return;
  const gt = g.getBoundingClientRect(), ct = cell.getBoundingClientRect();
  if (ct.top >= gt.top + 4 && ct.bottom <= gt.bottom - 4) return;      // already on screen
  g.scrollTop += (ct.top - gt.top) - (gt.height - ct.height) / 2;      // centre it
  if (lastViewKey) scrollMemo.set(lastViewKey, g.scrollTop);           // keep it for Back
}
let closeGuardUntil = 0;
function hideViewer() {
  exitOverlayFs(); $("#viewer").hidden = true;
  closeGuardUntil = performance.now() + 450;   // swallow a double click's 2nd click on the grid
  const seen = state.navList && state.navList[state.navIdx];
  revealInGrid(seen ? seen.id : null);
  // removing the attribute, not src="" -- an empty src makes the browser request
  // the page itself as an image, which shows up as a failed resource for no reason
  $("#viewer-img").removeAttribute("src");
}

/* ---------- in-app video player (streams with range requests) ---------- */
/* ---------- player control bar ----------
   Replaces the native controls: the browser's own bar cannot lose its fullscreen
   button (Chromium ignores controlsList="nofullscreen") and its fixed buttons ate a
   third of the timeline. This one is play/pause, two stamps and a slider that takes
   all the rest. It fades out while a video plays and comes back on any mouse move. */
const pv = $("#player-video"), pCtl = $("#player-ctl"), pSeek = $("#p-seek");
const pTime = $("#p-time"), pDur = $("#p-dur"), pPlay = $("#p-play"), pMute = $("#p-mute");
let pSeeking = false, pHide = 0;
// Every clip opens silent. Deliberately not persisted: a reload goes back to muted,
// which is what a library of browser-triggered autoplay needs, while a session that
// has been unmuted once stays unmuted as you walk from one clip to the next.
let pMuted = true;

function pToggleMute() {
  pMuted = !pv.muted;
  pv.muted = pMuted;
  pShow(false);
}

function fmtTime(s) {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
}
function pSync() {
  const d = pv.duration || 0;
  pDur.textContent = fmtTime(d);
  if (!pSeeking) {
    pSeek.value = d ? Math.round(pv.currentTime / d * 1000) : 0;
    pTime.textContent = fmtTime(pv.currentTime);
  }
  pPlay.textContent = pv.paused ? "\u25B6" : "\u275A\u275A";
  // inline SVG, not emoji: a headless/fontless browser rendered the speaker emoji as
  // an empty box, and the button has to be recognisable on any machine
  pMute.innerHTML = pv.muted || pv.volume === 0
    ? '<svg viewBox="0 0 24 24" width="34" height="34" fill="currentColor" aria-hidden="true">' +
      '<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16.5 9.5l5 5m0-5l-5 5" stroke="currentColor" ' +
      'stroke-width="1.7" fill="none" stroke-linecap="round"/></svg>'
    : '<svg viewBox="0 0 24 24" width="34" height="34" fill="currentColor" aria-hidden="true">' +
      '<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16.4 8.6a5 5 0 0 1 0 6.8M18.8 6.2a8.4 8.4 0 0 1 0 11.6" ' +
      'stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/></svg>';
  pPlay.title = pv.paused ? "Play (Space)" : "Pause (Space)";
}
function pShow(stay) {
  if (!pCtl) return;
  pCtl.classList.remove("hide");
  clearTimeout(pHide);
  // while playing it fades after a couple of idle seconds; paused or mid-drag, it stays
  if (!stay) pHide = setTimeout(() => {
    if (!pv.paused && !pSeeking) pCtl.classList.add("hide");
  }, 2500);
}
pv.addEventListener("timeupdate", pSync);
pv.addEventListener("durationchange", pSync);
pv.addEventListener("volumechange", pSync);
pv.addEventListener("play", () => { pSync(); pShow(false); });
pv.addEventListener("pause", () => { pSync(); pShow(true); });
pv.addEventListener("click", () => { pv.paused ? pv.play().catch(() => {}) : pv.pause(); pShow(false); });
/* Scrubbing seeks on `input`, i.e. while you drag -- not on `change`.
   Waiting for `change` meant the video only moved on release, and if the release
   happened after the bar had faded (opacity 0 / pointer-events none, which cancels
   the drag) the event never arrived at all: the slider moved, nothing scrubbed, and
   `pSeeking` stayed stuck true so the thumb stopped tracking the video too. */
function pScrub(toEnd) {
  const d = pv.duration || 0;
  const t = d * pSeek.value / 1000;
  pTime.textContent = fmtTime(t);
  if (d) pv.currentTime = Math.min(d - 0.05, Math.max(0, t));
  pSeeking = !toEnd;
  pShow(!toEnd);
}
pSeek.addEventListener("pointerdown", () => { pSeeking = true; pShow(true); });
pSeek.addEventListener("input", () => pScrub(false));
pSeek.addEventListener("change", () => pScrub(true));
pSeek.addEventListener("pointerup", () => pScrub(true));
pSeek.addEventListener("pointercancel", () => pScrub(true));
// the bar must never fade out from under a drag, and must stay up while hovered
pCtl.addEventListener("mouseenter", () => pShow(true));
pCtl.addEventListener("mouseleave", () => { if (!pSeeking) pShow(false); });
$("#p-play").onclick = () => { pv.paused ? pv.play().catch(() => {}) : pv.pause(); pShow(false); };
$("#p-mute").onclick = pToggleMute;
// a click on the bar must not also reach the video's play/pause or the stage's dblclick
pCtl.addEventListener("click", e => e.stopPropagation());
pCtl.addEventListener("dblclick", e => e.stopPropagation());
pCtl.addEventListener("mousemove", () => pShow(false));
$("#player").addEventListener("mousemove", () => pShow(false));
$("#player-stage").addEventListener("dblclick", e => {
  if (pCtl.contains(e.target)) return;
  closePlayer();                       // double click = back to the thumbnails
});

function openPlayer(it, opts = {}) {
  state.playerItem = it;
  $("#player-title").textContent = it.name;
  const v = pv;
  v.src = viewerSrc(it);
  v.muted = pMuted;                 // silent unless the user unmuted earlier this session
  paintInfoState();
  $("#player").hidden = false;
  if (fsPreferred()) goFs($("#player"));
  if (infoOpen) loadInfo(it, $("#player-info"));
  // "Show info for this video" opens it paused: you came to read, not to watch
  if (!opts.paused) v.play().catch(() => {});  // autoplay may be blocked until user interacts
  pSync(); pShow(false);
  const h = buildHash() + "/v" + it.id;
  if (location.hash !== h) { hashGuard = true; location.hash = h; }
}
function closePlayer() {
  if ($("#player").hidden) return;
  if (location.hash === buildHash()) { hidePlayer(); return; }
  history.back();
}
function hidePlayer() {
  exitOverlayFs();
  revealInGrid(state.playerItem ? state.playerItem.id : null);
  const v = $("#player-video");
  v.pause(); v.removeAttribute("src"); v.load();
  clearTimeout(pHide);
  $("#player").hidden = true;
  state.playerItem = null;
}

/* ---------- filmstrip (video 16-frame slice) ---------- */
function openZipVideo(it) {
  $("#film-title").textContent = `${it.name}  (video inside zip)`;
  $("#film-grid").innerHTML = '<div class="empty">This video is stored inside the zip — ' +
    'flagged so it can be extracted into a folder. Frame previews and VLC playback are ' +
    'available for videos stored in folders.</div>';
  $("#btn-vlc").href = "#";
  $("#btn-raw").href = "#";
  $("#filmstrip").hidden = false;
}
function closeFilm() { $("#filmstrip").hidden = true; }

/* ---------- scan ---------- */
// One folder at a time: the folder you are looking at (or the location root).
// Crawling every subfolder of a large library is I/O nobody asked for, and the
// tree is lazy anyway -- each folder indexes itself when you open it.
async function startScan() {
  if (scanWork && lastStatus && lastStatus.running) {
    // the button is already showing this job's countdown: a second press used to queue a
    // second scan of the same folder/zip, which published every thumbnail twice
    pollScan();
    return;
  }
  state.built.clear();          // a scan rebuilds on purpose: nothing here is "done"
  state.builtKey = "";
  const q = new URLSearchParams({ loc: state.loc, why: "button" });
  // inside a zip there is no folder to refresh: the zip is the unit to rescan
  if (state.kind === "zip" && state.zipId) q.set("zip", state.zipId);
  else if (state.kind !== "loc" && state.parent != null) q.set("parent", state.parent);
  await api("/api/scan?" + q.toString(), { method: "POST" });
  pollScan();
}
let pollTimer = null;
function setScanBusy(on) {
  const b = $("#btn-scan");
  b.classList.toggle("busy", !!on);
  b.setAttribute("aria-busy", on ? "true" : "false");
  $("#scan-label").textContent = on ? "Scanning" : "Scan";
}
/* ---------- one progress bar, every countable job ----------
   The scan has a denominator from the start; so do the three menu jobs that run in
   loops the client can see (filmstrips, re-encode here, re-encode subfolders).
   Background thumbnail work does not (it is created as you browse), which is why
   that stays a queued/building chip instead of a lie with a percentage.
   A job the user started beats the scan while it runs: that is the one being
   watched, and the scan badge keeps counting regardless. */
let scanJob = null, manualJob = null;
/* The folder in front of you is a job too when it still has pictures to make.
   Without this the bar died the moment you changed folder mid-scan: the scan's
   counters belong to the folder it was started on, so the new folder's
   thumbnails built with nothing but a badge to show for it. */
let viewJob = null;

/* True when the running scan is working on the folder on screen, so its own
   counters are the right numbers and must not be doubled up with the view's. */
function scanCoversView() {
  const st = lastStatus;
  if (!st || !scanJob) return false;
  if (state.kind === "zip") return st.zip === state.zipId;
  if (st.zip) return false;
  if (st.loc !== state.loc) return false;
  if (st.deep) return true;                        // a subtree walk will get here
  return (st.parent ?? null) === (state.parent ?? null);
}

function paintProgress() {
  const box = $("#scan-progress"), bar = $("#scan-bar"), txt = $("#scan-text");
  if (!box) return;
  const job = manualJob || (scanCoversView() ? scanJob : (viewJob || scanJob)) || doneJob;
  box.classList.toggle("done", !!job && job === doneJob);
  if (!job) { box.classList.remove("on"); bar.style.setProperty("--pct", "0%"); txt.textContent = "";
              box.title = ""; return; }
  const pct = job.total ? Math.min(100, Math.round(100 * job.done / job.total)) : 0;
  box.classList.add("on");
  bar.style.setProperty("--pct", pct + "%");
  txt.textContent = job.text || `${job.done}/${job.total} ${job.label || ""}`.trim();
  box.title = job.title || txt.textContent;
}

function setManualJob(job) {
  manualJob = job;
  paintProgress();
}

/* ---------- every job ends by SAYING it ended (v0.1.237) ----------
   Measured on dev before this: re-encoding 171 photos takes ~1.3s, faster than the
   first poll, so the bar popped up at 100% and vanished and the job read as "did
   nothing"; the filmstrip and subfolder jobs dropped the bar mid-count (6/8, 3/5)
   because the loop that paints them stops the moment the server says "idle".
   doneJob is the lowest-priority slot: a full bar and a tick for a moment, shown only
   when nothing else is running, so a finished job never hides a live one. */
let doneJob = null, finishTimer = 0;
function finishJob(text, title) {
  doneJob = { done: 1, total: 1, text: `${text} ✓`, title: title || text };
  const mine = doneJob;
  paintProgress();
  clearTimeout(finishTimer);
  finishTimer = setTimeout(() => { if (doneJob === mine) { doneJob = null; paintProgress(); } }, 2200);
}
/* "~1m left" was printed for ANY remaining time (Math.ceil of seconds/60), so a job
   two seconds from done still promised a minute. Seconds under a minute. */
function fmtEta(sec) {
  if (!sec || sec <= 0) return "";
  if (sec < 60) return ` (~${Math.max(5, Math.round(sec / 5) * 5)}s left)`;
  return ` (~${Math.ceil(sec / 60)}m left)`;
}
/* The view a job was started from, so its "building…" strip is only drawn there --
   a note that says "here" must not follow you into another folder. */
function viewKeyNow() {
  return `${state.loc}|${state.kind}|${state.parent || ""}|${state.zipId || ""}`;
}

function pollScan() {
  clearTimeout(pollTimer);
  api("/api/scan/status").then(st => {
    setScanBusy(st.running);
    if (st.running) {
      const isAnim = st.phase === "animations";
      const den = (isAnim ? st.anim_total : st.thumb_total) || 0;
      const num = (isAnim ? st.anim_done : st.thumb_done) || 0;
      scanJob = st.phase === "discover"
        ? { done: 0, total: 0, text: "scan: refreshing folder…" }
        : isAnim
          ? (st.anim_skipped ? { done: 0, total: 0, text: `scan: animations on demand (${st.anim_skipped})` }
                             : { done: num, total: den, text: `scan: ${num}/${den} animations` })
          : { done: num, total: den, text: `scan: ${num}/${den} thumbs` };
      paintProgress();
      libPending = st.pending;
      // Count by "seen" rather than by a countdown from a total: the total is 0 while
      // the scan is still discovering, so a countdown seeded at that moment stuck at 0
      // for the whole job (200-entry zip: badge never moved).
      const total = st.work_total || 0;
      // st.loc is null for the first poll of each job; a key built from that ("fnull/-")
      // was replaced a second later and restarted the countdown from the top
      const key = st.zip ? "z" + st.zip
                : st.loc == null ? null
                : st.deep ? "d" + st.loc
                : "f" + st.loc + "/" + (st.parent ?? "-");
      if (key) {
        if (!scanWork || scanWork.key !== key) {
          if (scanWork) {
            clog("warn", { what: "badge_restart", from: scanWork.key, to: key,
                           seq: badgeTape.slice(-60) });
          }
          scanWork = { key, zip: st.zip || null, parent: st.parent ?? null,
                       total, seen: 0, seenIds: new Set(), deep: !!st.deep };
          badgeTapeReset();
        } else if (total > scanWork.total) {
          scanWork.total = total;      // the same job found more work, not a new job
        }
        lastStatus = st;
        const left = scanBadgeLeft(st);
        if (left != null) {
          updateBadge(left, libPending, scanWork.deep ? "folders and zips left in this scan"
                                                     : "thumbnails left in this scan");
        } else {
          // the job is running but has not counted its work yet: a plain 0 next to
          // "Scanning" reads as "nothing to do", and it was the first thing seen
          updateBadge("…", libPending, "working out how much is left in this scan");
        }
      }
      pollTimer = setTimeout(pollScan, 1000);
    } else {
      const hadScan = !!scanJob;
      scanJob = null;
      if (hadScan) finishJob("scan: done");
      paintProgress();          // falls back to a manual job if one is running
      const wasScanning = !!scanWork;
      if (wasScanning) {
        const climbs = badgeTapeClimbs();
        if (climbs.length) {
          clog("warn", { what: "badge_climb", climbs: climbs.slice(0, 8),
                         seq: badgeTape.slice(0, 120), n: badgeTape.length,
                         total: scanWork.total, key: scanWork.key });
        }
      }
      scanWork = null;                  // done: the badge is a pending count again
      libPending = st.pending;
      updateBadge(pendingCount(state.items), libPending);
      rerender();
      if (wasScanning) settleBadge();
    }
  }).catch(() => {
    // a failed poll (server restarting, brief 502) used to end the loop with the button
    // still saying "Scanning" forever: keep trying until a poll lands
    pollTimer = setTimeout(pollScan, 1500);
  });
}
/* The badge counts what is pending in the folder on screen, not in the library.
   A library-wide number next to a button that scans one folder read as "it is
   about to churn through everything"; the library total is in the tooltip. */
let libPending = null;      // library-wide pending count, for the badge tooltip
/* While a scan runs the badge counts DOWN through its work: the server says how many
   thumbnails the folder (or zip) it is working on needs, and every thumbnail event
   that belongs to that folder takes one off. It used to show the number of files with
   no thumbnail yet, which is 0 during a re-encode -- the case where you most want to
   see progress. */
let scanWork = null;        // {key, zip, parent, total, seen, deep} -- left = total - seen
let lastStatus = null;      // the newest /api/scan/status, for the badge helper
let lastFramesGen = null;   // media.frames_gen: a change means new filmstrips exist
let reframesTimer = 0;      // one deferred re-list per change, not one per tile

// A "counted down twice" report is invisible in sampled screenshots and gone by the time
// anyone looks, so the badge keeps its own tape: every value it showed during a scan, and a
// record shipped to /api/log when that tape shows a climb or a restart.
let badgeTape = [];
function badgeTapeReset() { badgeTape = []; }
function badgeTapeNote(v) {
  const last = badgeTape[badgeTape.length - 1];
  if (!last || last.v !== String(v)) badgeTape.push({ t: Math.round(performance.now()), v: String(v) });
  if (badgeTape.length > 400) badgeTape.shift();
}
function badgeTapeClimbs() {
  const out = [];
  for (let i = 1; i < badgeTape.length; i++) {
    const a = badgeTape[i - 1].v, b = badgeTape[i].v;
    if (/^\d+$/.test(a) && /^\d+$/.test(b) && +b > +a) out.push(`${a}->${b}`);
  }
  return out;
}

function updateBadge(here, total, label) {
  const b = $("#scan-badge");
  const n = here == null ? total : here;
  if (scanWork) badgeTapeNote(n);
  b.textContent = n;
  b.classList.toggle("zero", !n);
  // a manual rebuild is not "pending": those tiles already HAVE a thumbnail, they are
  // just being replaced -- so the loop that drives it says what it is counting
  b.title = label ? `${n} ${label}`
    : total != null && total !== n
      ? `${n} pending here · ${total} in the whole library`
      : `${n} pending in this folder`;
}
// After a scan the badge should read 0, not "how many files are here". The rows and the
// page's copy of them settle at different times: the scan reports itself idle while the
// pack write and the last thumbnails are still landing, so a single re-list 900ms later
// still saw pending flags and printed the file count. Wait for the build queues to empty,
// re-list, THEN count.
function settleBadge() {
  let tries = 0;
  const tick = async () => {
    tries += 1;
    let st = null;
    try { st = await api("/api/scan/status"); } catch (e) { st = null; }
    const b = (st && st.building) || {};
    const busy = (b.active || 0) + (b.queued || 0);
    if (st && st.running) return;                 // another scan took over
    if (busy > 0 && tries < 40) { setTimeout(tick, 1000); return; }
    await rerender();
    if (!scanWork) updateBadge(pendingCount(state.items), st ? st.pending : libPending);
  };
  setTimeout(tick, 700);
}

/* One re-list per change, never one request per tile. Both the status poll and the
   poster stream land here: a poster build changes a clip's frame list ([] -> poster + p1)
   exactly as a filmstrip build does, so the counter covers both. */
function checkFramesGen(st) {
  if (!st || typeof st.frames_gen !== "number") return;
  if (lastFramesGen === null) { lastFramesGen = st.frames_gen; return; }
  if (st.frames_gen === lastFramesGen) return;
  const b = st.building || {};
  const busy = (b.active || 0) + (b.queued || 0);
  // Work still landing: a re-list now throws away tiles the poster stream has already
  // painted, and the listing it replaces is not complete yet -- the grid came back static
  // and stayed static, because the events that would have fixed it were already spent.
  // Leave the counter changed and let the settle path ask again when everything is idle.
  if (busy || st.running) return;
  lastFramesGen = st.frames_gen;
  if (reframesTimer) return;
  reframesTimer = setTimeout(() => { reframesTimer = 0; forgetFrames(); rerender(); }, 2500);
}

async function refreshStatus() {
  const st = await api("/api/scan/status");
  checkFramesGen(st);
  // This runs after EVERY repaint (listing, sort, live thumb patch, navigation) and it
  // used to write the pending count over the badge unconditionally -- so mid-scan the
  // number fell back to "files here" and the next poll put the countdown back: the
  // "counts down twice, then shows the file number" report.
  if (st.running) {
    lastStatus = st;
    const left = scanWork ? scanBadgeLeft(st) : null;
    if (left != null) {
      updateBadge(left, st.pending, scanWork.deep ? "folders and zips left in this scan"
                                                 : "thumbnails left in this scan");
    }
  } else {
    updateBadge(pendingCount(state.items), st.pending);
  }
  if (st.building) paintBuild(st.building);
  watchBuild();
  if (st.running) pollScan();
}

/* ---------- build chip ----------
   The badge counts files in THIS folder with no thumbnail yet. This counts what the
   server is actually still building, library-wide -- folder mosaics, video posters,
   zip tiles. It exists because work used to be dropped in silence: a folder of 20+
   subfolders built the first handful and stopped, and nothing anywhere said so. A
   number above zero that never moves is now the visible version of that bug. */
let buildTimer = 0, buildSeen = -1, buildSince = 0, buildStuck = false;

function paintBuild(b) {
  const chip = $("#build-chip");
  if (!chip) return;
  const active = b.active || 0, queued = b.queued || 0;
  const total = active + queued;
  // class, not .hidden: [hidden] is display:none!important here, and the chip's slot
  // must stay reserved or the whole right-aligned row shifts when work starts
  if (!total) { chip.classList.remove("on"); buildSeen = -1; buildStuck = false; return; }
  chip.classList.add("on");
  if (total !== buildSeen) { buildSeen = total; buildSince = Date.now(); buildStuck = false; }
  else if (!buildStuck && Date.now() - buildSince > 25000) buildStuck = true;
  const parts = [];
  if (active) parts.push(active + " building");
  if (queued) parts.push(queued + " queued");
  chip.textContent = (buildStuck ? "⚠ " : "") + parts.join(" · ");
  chip.classList.toggle("stalled", buildStuck);
  const by = b.by_kind || {};
  const kinds = Object.keys(by).map(k => k + ": " + by[k]).join(", ");
  chip.title = (kinds || "background thumbnail work") +
    (buildStuck ? " — unchanged for 25s, something looks wedged" : "");
}

async function pollBuild() {
  clearTimeout(buildTimer);
  buildTimer = 0;
  let st = null;
  try { st = await api("/api/scan/status"); }
  catch (e) { return; }              // server restarting: the next listing asks again
  const b = st && st.building;
  if (!b) return;
  checkFramesGen(st);                // posters/filmstrips landing while we watch
  const wasBusy = buildSeen > 0;
  paintBuild(b);
  const total = (b.active || 0) + (b.queued || 0);
  if (total) buildTimer = setTimeout(pollBuild, 2500);
  else if (wasBusy) refreshStatus();     // settled: the badge was left on a stale number
}

function watchBuild() { if (!buildTimer) pollBuild(); }

/* ---------- context menu ---------- */
function showCtx(e, target) {
  e.preventDefault(); e.stopPropagation();
  state.ctxTarget = target;
  const m = $("#ctxmenu");
  const kind = !target ? "none" : (target.parent === null ? "folder" : target.kind);
  const noun = { video: "video", image: "picture", zip: "zip" }[kind] || "item";
  // A zip is not a folder, however much it browses like one: it has no subfolders to
  // recurse into, no filmstrips to build, and no "folder holding it" worth naming.
  const isZip = kind === "zip";
  const isFolderish = kind === "folder" || kind === "dir";
  const canBuild = kind === "video" || isFolderish;

  /* ONE grammar for every line: "<verb> <object> <scope>". Scope words are fixed, so two
     commands that act on different things can never read as synonyms: containers take
     "in this folder" / "in subfolders" / "in this zip", a single item takes "for this
     video" / "for this picture". The menu used to mix "here", "subfolders", "for this
     video" and "this zip's" (owner: "inconsistent with the language").
     A command that really acts on the holding folder says so -- "Scan the folder holding
     this video" -- because scanning a clip and re-encoding a clip are different scopes,
     and calling both "here" is what made the menu ambiguous. */

  const inf = $("#ctx-info");
  inf.hidden = !(target && target.item && (kind === "image" || kind === "video"));
  inf.textContent = `Show info for this ${noun}`;
  inf.title = kind === "video" ? "Open the video paused, with its tracks, chapters and details beside it"
                               : "Open the picture with its camera details and embedded text beside it";

  const sc = $("#ctx-scan");
  sc.textContent = isZip ? "Scan this zip"
    : isFolderish ? "Scan this folder" : `Scan the folder holding this ${noun}`;
  sc.title = isZip
    ? "Re-read this archive: pick up new entries and build the thumbnails they need"
    : isFolderish
      ? "Pick up new files here and build the thumbnails they need"
      : `Pick up new files in the folder that holds this ${noun}, and build its thumbnails`;
  /* A clip's "scan its folder" line is gone (owner, v0.1.235): the folder is right there
     in the left pane and the toolbar Scan button does the same for the folder you are
     looking at, so the line only added a second way to do somebody else's job. */
  sc.hidden = !(isFolderish || isZip);

  const sd = $("#ctx-scan-deep");
  sd.hidden = !isFolderish;
  sd.textContent = "Scan this folder and subfolders";
  sd.title = "The same, in every folder below this one — one pass, however deep";

  const sz = $("#ctx-scan-deep-zips");
  sz.hidden = !isFolderish;
  // The old wording ("Scan this folder, subfolders and zips") read as "also index the zips".
  // It actually re-reads every archive below and rebuilds every entry thumbnail in it --
  // the owner did not know it did that (v1.4.1), so the label now says "re-read".
  sz.textContent = "Scan this folder, subfolders and re-read every zip";
  sz.title = "Re-reads every zip below this one and rebuilds all of its entry thumbnails "
           + "— a 300-image archive counts as 300 thumbnails, not as one zip";

  const ex = $("#ctx-expand");
  ex.hidden = !isFolderish;
  ex.textContent = "Expand all subfolders";
  ex.title = "Open every folder below this one in the tree";

  const buildHere = $("#ctx-frames-here");
  buildHere.hidden = !isFolderish;
  buildHere.textContent = "Build filmstrips in this folder";
  buildHere.title = "Only the videos sitting directly in this folder";

  const build = $("#ctx-frames");
  build.hidden = !canBuild;
  build.textContent = kind === "video" ? "Build filmstrip for this video"
                                       : "Build filmstrips in subfolders";
  build.title = kind === "video"
    ? "Re-extract this clip's filmstrip, even if one already exists"
    : "Every video in every folder below this one";

  const rb = $("#ctx-rebuild");
  rb.hidden = !(target && (target.nodeId || target.parent === null));
  rb.textContent = isFolderish ? "Re-encode thumbnails in this folder"
    : kind === "zip" ? "Re-encode thumbnails in this zip"
      : `Re-encode thumbnails for this ${noun}`;
  rb.title = kind === "video"
    ? "Re-encode this clip's thumbnails — back to the two-image preview"
    : kind === "image" ? "Re-encode this picture's thumbnail"
      : isZip ? "Re-encode every thumbnail in this archive (use after a tile style change)"
        : "Re-encode the thumbnails that already exist (use after a tile style change)";

  const rd = $("#ctx-rebuild-deep");
  rd.hidden = !isFolderish;
  rd.textContent = deepRun ? "Stop re-encoding subfolders"
                           : "Re-encode thumbnails in subfolders";
  rd.title = "The same, in every folder below this one -- one folder at a time, stoppable";

  // A hairline opens each group, but never above the first visible line and never two in
  // a row (a clip hides the middle group entirely, which used to leave a double rule).
  let prevItem = false, prevSep = false;
  for (const el of m.children) {
    if (el.classList.contains("ctxsep")) {
      const on = prevItem && !prevSep && !el.hidden;
      el.classList.toggle("ctxsep-on", on);
      if (on) prevSep = true;
      continue;
    }
    if (!el.hidden) { prevItem = true; prevSep = false; }
  }
  m.style.left = e.clientX + "px"; m.style.top = e.clientY + "px";
  m.hidden = false;
}
$("#ctx-frames").onclick = () => ctxBuildFrames(true);
$("#ctx-frames-here").onclick = () => ctxBuildFrames(false);
$("#ctx-expand").onclick = () => ctxExpandAll();
$("#ctx-info").onclick = () => {
  const t = state.ctxTarget;
  $("#ctxmenu").hidden = true;
  if (!t || !t.item) return;
  clog("click", { to: "ctx-info", kind: t.kind, name: t.name });
  infoOpen = true;
  try { localStorage.setItem(INFO_PREF, "1"); } catch (_) {}
  if (t.kind === "video") openPlayer(t.item, { paused: true });
  else openViewer(t.item);
};
function setBuildNote(text) {
  const g = $("#grid");
  if (!g) return;
  const all = g.querySelectorAll(".buildnote");
  if (!text) { all.forEach(e => e.remove()); return; }
  const el = all[0] || g.insertBefore(div("buildnote"), g.firstChild);
  el.textContent = text;
}
async function ctxBuildFrames(deep = true) {
  const t = state.ctxTarget;
  $("#ctxmenu").hidden = true;
  if (!t || !t.nodeId) return;
  return runFrameBuild(t.nodeId, deep);
}
/* The job itself, so the keyboard can start the same build without a menu target.
   One clip answers `scope: "video"` and is always a rebuild (the endpoint forces);
   a folder is additive unless "here" narrows it. */
async function runFrameBuild(nodeId, deep) {
  const startView = viewKeyNow();
  const note = s => { if (s === null || viewKeyNow() === startView) setBuildNote(s); };
  const r = await api(`/api/build/frames/${nodeId}` + (deep ? "" : "?deep=0"), { method: "POST" });
  const n = r.scope === "folder" ? r.queued : 1;
  forgetFrames();            // "already built" still deserves a refresh of what is cached
  if (!n) { note(null); finishJob("filmstrips: already built"); rerender(); return; }
  const where = deep ? "in subfolders" : "here";
  note(r.scope === "folder" ? `Building ${n} filmstrips ${where}… 0/${n}`
                            : (r.force ? "Rebuilding filmstrip…" : "Building filmstrip…"));
  // a folder build takes minutes (one ffmpeg per clip): show real progress
  const t0 = Date.now();
  let last = { done: 0, total: n };
  while (Date.now() - t0 < 3600e3) {
    await new Promise(res => setTimeout(res, r.scope === "video" ? 1000 : 2000));
    if (r.scope === "video") {
      // Finished = the server says this clip's job is done. "Has frames" is NOT the
      // test: the clip already showed its two-image preview before the build began,
      // so that check fired on the first poll while ffmpeg was still running, and
      // the early return also skipped the refresh below -- the tile went on flipping
      // the old pair (owner's report, 0.1.239).
      const vs = await api(`/api/build/status/${nodeId}`).catch(() => ({ active: true }));
      if (vs.active) continue;
      break;
    }
    const st = await api(`/api/build/status/${nodeId}`);
    if (!st.active) { note(null); setManualJob(null); break; }
    last = { done: st.done || 0, total: st.total || n };
    const eta = fmtEta(st.eta);
    note(`Building filmstrips ${where}… ${st.done}/${st.total}` + eta);
    setManualJob({ done: st.done || 0, total: st.total || 0,
                   text: `filmstrips: ${st.done}/${st.total}`,
                   title: `Building filmstrips ${where} — ${st.done}/${st.total}` + eta });
  }
  note(null);
  setManualJob(null);
  forgetFrames();            // the strips are on disk now: stop showing the old pair
  await rerender();          // drops the ambient preview's cached list and re-reads it
  if (r.scope === "video") {
    // say what is actually there now, not what was asked for
    let got = 0;
    try { got = ((await api(`/api/frames/${nodeId}`)).frames || []).length; } catch (_) {}
    flashTile(nodeId);
    finishJob(got > 2 ? `filmstrip: ${got - 1} frames` : "filmstrip: failed",
              got > 2 ? "" : "The clip produced no filmstrip -- it may be too short or unreadable");
    return;
  }
  finishJob(`filmstrips: ${last.total}/${last.total}`);
}
function flashTile(id) {
  const c = document.querySelector(`.cell[data-id="${id}"]`);
  if (c) c.classList.add("built");
}
async function ctxScan(deep, zips) {
  const t = state.ctxTarget;
  $("#ctxmenu").hidden = true;
  if (!t) return;
  // the log should say a person picked this from the tree, not that a scan appeared
  const why = t.kind === "zip" ? "menu-zip"
    : deep && zips ? "menu-deep-zips" : deep ? "menu-deep" : "menu";
  const q = [deep ? "deep=1" : null, zips ? "zips=1" : null, `why=${why}`]
    .filter(Boolean).join("&");
  // A zip is scanned as itself (/api/scan?zip=), not through rescan: rescan maps a node
  // to the folder that holds it, which for an archive is the other thing entirely.
  const url = t.kind === "zip" ? `/api/scan?loc=${t.loc}&zip=${t.nodeId}&why=${why}`
            : t.nodeId != null ? `/api/rescan/${t.nodeId}` + (q ? "?" + q : "")
            : t.parent != null ? `/api/rescan/${t.parent}` + (q ? "?" + q : "")
            : `/api/scan?loc=${t.loc}&` + q;
  try { await api(url, { method: "POST" }); } catch (e) { return; }
  pollScan();
}

/* ---------- misc dom helpers ---------- */
function div(cls, html) { const d = document.createElement("div"); d.className = cls; d.innerHTML = html || ""; return d; }
function span(cls, html, title) { const s = document.createElement("span"); s.className = cls; s.innerHTML = html; if (title) s.title = title; return s; }
function img(src, alt) { const i = document.createElement("img"); i.className = "full"; i.src = src; i.alt = alt; i.loading = "lazy"; return i; }
function esc(s) { return s.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* ---------- events ---------- */
function applyFramesPref() {
  const on = framesOn();
  const b = $("#btn-frames");
  if (!b) return;
  b.classList.toggle("off", !on);
  const reduced = document.documentElement.dataset.mxReduced === "1";
  b.title = on
    ? (reduced ? "Video frame preview: blocked by this system's \"reduced motion\" setting — click to force it on"
               : "Video frame preview: on — hover a clip to flip its frames")
    : "Video frame preview: off — hover does nothing";
}
$("#btn-frames").onclick = () => {
  const wasOn = framesOn();
  if (state.pinned) {
    animOverride = !wasOn;               // this folder only; the global switch stays put
  } else {
    try { localStorage.setItem(FRAMES_PREF, wasOn ? "0" : "1"); } catch (_) {}
    animOverride = null;
  }
  applyFramesPref();
  prefSave(true);
  if (framesOn()) { startAutoPreview(); }          // ambient preview on
  else { endVideoHover(true); stopAutoPreview(); } // stop whatever is flipping
};
applyFramesPref();
$("#btn-theme").onclick = () => {
  const cur = document.documentElement.dataset.theme;
  state.theme = cur === "light" ? "dark" : "light";
  localStorage.setItem("mx.theme", state.theme);
  applyTheme();
};
$("#btn-pin").onclick = () => prefToggle();
$("#btn-sort").onclick = () => {
  state.sort = state.sort === "name" ? "date" : "name";
  if (!state.pinned) localStorage.setItem("mx.sort", state.sort);
  $("#btn-sort").textContent = state.sort === "name" ? "Name" : "Date";
  prefSave(true);
  rerender();
};
$("#size-slider").value = state.size;
$("#grid").style.setProperty("--tw", state.size + "px");
$("#size-slider").oninput = e => {
  state.size = +e.target.value;
  // Only an UNPINNED folder writes the global size: inside a pinned folder the change
  // belongs to that folder alone (owner, 2026-11-02), so leaving restores your own zoom.
  if (!state.pinned) localStorage.setItem("mx.size", state.size);
  $("#grid").style.setProperty("--tw", state.size + "px");
  $("#film-grid").style.setProperty("--tw", state.size + "px");
  prefSave(false);
};
$("#size-slider").onchange = () => prefSave(true);   // release: one write, not one per pixel
$("#btn-scan").onclick = startScan;
$("#ctx-scan").onclick = () => ctxScan(false);
$("#ctx-scan-deep").onclick = () => ctxScan(true);
$("#ctx-scan-deep-zips").onclick = () => ctxScan(true, true);

/* One folder at a time, by request. Nothing in this app regenerates a thumbnail
   on its own any more, so this is how a new tile style reaches the library. */
async function ctxRebuild() {
  const t = state.ctxTarget;
  $("#ctxmenu").hidden = true;
  if (!t) return;
  const id = t.nodeId || t.parent || 0;
  // Everything this loop watches is fixed NOW, from the target -- never re-read from
  // `state` while it runs. It used to list whatever was on screen on every poll, so
  // moving to another folder mid-job made it watch the wrong one: nothing pending
  // there, so it declared the job finished and dropped the bar while the server was
  // still encoding.
  const loc = state.loc, here = state.parent, startView = viewKeyNow();
  const note = s => { if (viewKeyNow() === startView) setBuildNote(s); };
  note("Rebuilding thumbnails…");
  let r;
  try { r = await api(`/api/rebuild/thumbs/${id}`, { method: "POST" }); }
  catch (e) { setBuildNote(null); return; }
  const total = r.queued || 0;      // the POST pends exactly these: a real denominator
  const single = r.scope === "video" || r.scope === "image";
  const url = r.scope === "zip" ? `/api/zip/${id}/entries`
    : r.scope === "folder" ? `/api/children?loc=${loc}` + (id ? `&parent=${id}` : "")
    : `/api/children?loc=${loc}` + (here ? `&parent=${here}` : "");
  const mine = items => single ? items.filter(i => i.id === id) : items;
  const t0 = Date.now();
  let sawPending = false;
  // poll fast while a quick job is likely to finish, then settle to once a second
  const gap = () => Date.now() - t0 < 4000 ? 350 : 1000;
  while (Date.now() - t0 < 900e3) {
    await new Promise(res => setTimeout(res, gap()));
    let items = [];
    try { items = (await api(url)).items || []; } catch { continue; }
    // Swap the pictures as they are rewritten, not only at the end: this is the poll
    // path, so a re-encode still shows its work when the event stream is unavailable.
    if (viewKeyNow() === startView) patchTiles(items);
    // folder tiles are throttled and may stay queued; they are not "work left"
    const left = mine(items).filter(i => i.pending && i.kind !== "dir").length;
    if (left) sawPending = true;
    // The POST returns before the first encode starts, so "nothing pending" in the
    // opening second means "not begun", not "finished".
    if (!left && (sawPending || Date.now() - t0 > 1500)) break;
    if (!left) continue;
    note(`Rebuilding ${left} thumbnail${left === 1 ? "" : "s"}…`);
    setManualJob({ done: Math.max(0, total - left), total,
                   text: `re-encode: ${Math.max(0, total - left)}/${total}` });
    if (viewKeyNow() === startView) updateBadge(left, libPending, "still being rebuilt in this folder");
  }
  if (viewKeyNow() === startView) setBuildNote(null);
  setManualJob(null);
  finishJob(single ? "re-encode: done" : `re-encode: ${total}/${total}`);
  forgetFrames();
  rerender();
  refreshStatus();          // hand the badge back to the ordinary pending count
}
$("#ctx-rebuild").onclick = ctxRebuild;

/* ---------- subfolder re-encode (the rollout switch) ----------
   Same work as "Re-encode thumbnails here", applied to every folder below, run
   server-side one folder at a time so a 300-folder subtree does not queue 300
   packs. It survives navigating away (the note comes back if you open the menu
   again), and a second click on the menu item stops it after the current folder. */
let deepRun = false, deepPoll = 0;
async function ctxRebuildDeep() {
  const t = state.ctxTarget;
  $("#ctxmenu").hidden = true;
  if (!t) return;
  if (deepRun) {
    setBuildNote("Stopping after this folder...");
    try { await api("/api/rebuild/cancel", { method: "POST" }); } catch (_) {}
    return;
  }
  const id = t.nodeId || t.parent || 0;
  let pv = { folders: 0, items: 0 };
  try { pv = await api(`/api/rebuild/preview/${id}`); } catch (_) { return; }
  if (pv.folders < 1) { setBuildNote("Nothing below this folder to re-encode"); setTimeout(() => setBuildNote(null), 3000); return; }
  const where = pv.folders <= 1 ? "in this folder"
    : `in this folder and the ${pv.folders - 1} below it`;
  if (!confirm(`Re-encode ${pv.items} thumbnail${pv.items === 1 ? "" : "s"} ${where}?\n\n` +
               "It works through them one at a time in the background, so you can keep " +
               "browsing. Right-click and pick \"Stop re-encoding subfolders\" to stop it.")) return;
  let r;
  try { r = await api(`/api/rebuild/deep/${id}`, { method: "POST" }); }
  catch (e) { setBuildNote(null); return; }
  if (!r.running) { setBuildNote(null); return; }
  attachDeep();
}

/* The job lives on the server, so a reload (or a second tab) re-attaches to it
   instead of offering to start another one. */
function attachDeep() {
  deepRun = true;
  clearInterval(deepPoll);
  let seen = 0;                        // folders in the job, once we have seen it run
  const tick = async () => {
    let st;
    try { st = await api("/api/rebuild/status"); } catch (_) { return; }
    if (!st.running) {
      deepRun = false; clearInterval(deepPoll);
      setBuildNote(null); setManualJob(null);
      if (seen) finishJob(st.stopped ? `re-encode: stopped` : `re-encode: ${seen}/${seen} folders`);
      forgetFrames(); rerender(); refreshStatus();
      return;
    }
    deepRun = true;
    seen = st.folders || seen;
    // name the item it is waiting on: one 87MB clip can hold a folder for minutes,
    // and a bare counter reads as "stuck"
    const slow = st.since > 45 ? ` -- ${Math.floor(st.since / 60)}m on this folder` : "";
    setManualJob({ done: st.done || 0, total: st.folders || 0,
                   text: `re-encode: ${st.done + 1}/${st.folders} folders`,
                   title: `Re-encoding ${st.current || ""}` });
    setBuildNote(`Re-encoding ${st.done + 1}/${st.folders}: ${st.current || "..."}` +
                 (st.left ? ` -- ${st.left} left${st.left_name ? ` (${st.left_name})` : ""}${slow}` : slow));
  };
  tick();
  deepPoll = setInterval(tick, 2000);
}
(async () => {
  try { const st = await api("/api/rebuild/status"); if (st.running) attachDeep(); }
  catch (_) {}
})();
$("#ctx-rebuild-deep").onclick = ctxRebuildDeep;
/* The menu used to close only on a bubbling "click", so a click on a tree row
   (whose handler stops propagation) left it on screen. Dismiss on capture-phase
   mousedown instead, plus Escape, scroll, blur and any other right-click. */
function hideCtx() {
  const m = $("#ctxmenu");
  if (m && !m.hidden) m.hidden = true;
}
document.addEventListener("mousedown", e => {
  const m = $("#ctxmenu");
  if (m && !m.hidden && !m.contains(e.target)) m.hidden = true;
}, true);
document.addEventListener("contextmenu", e => {
  const m = $("#ctxmenu");
  if (m && !m.hidden && !m.contains(e.target)) m.hidden = true;   // capture: before showCtx re-opens it
}, true);
document.addEventListener("keydown", e => { if (e.key === "Escape") hideCtx(); });

/* Single-key shortcuts for the three verbs the owner reaches for constantly.
   Guarded so they never steal typing, browser chords (Ctrl/Meta/Alt) or the
   viewer's own keys; the same actions the buttons and menu items call. */
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.repeat) return;
  // Only text ENTRY swallows the key. A focused range slider is still an INPUT,
  // and skipping every INPUT meant "click the zoom slider, then press P" did
  // nothing at all (owner's report, v0.1.192).
  const t = e.target;
  const ty = t && t.tagName === "INPUT" ? (t.type || "text").toLowerCase() : null;
  const typing = t && (t.tagName === "TEXTAREA" || t.isContentEditable ||
    (ty && /^(text|search|email|url|tel|password|number|date|time|datetime-local|month|week)$/.test(ty)));
  if (typing) return;
  const v = $("#viewer"), pl = $("#player");
  if (v && !v.hidden) return;                    // the viewer owns the keyboard
  if (pl && !pl.hidden) return;                  // ...and so does the player (F = full screen)
  if (!$("#ctxmenu").hidden) return;             // a menu is open: that is the choice on offer
  // Up one folder: Backspace (the old Explorer key) or Alt+Up (the current one).
  // Alt+Up is the only Alt chord taken; every other Alt combination stays the browser's.
  if ((e.key === "Backspace" && !e.altKey) || (e.altKey && e.key === "ArrowUp")) {
    e.preventDefault(); goUp(); return;
  }
  if (e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === "p") { e.preventDefault(); prefToggle(); }
  else if (k === "s") { e.preventDefault(); startScan(); }
  else if (k === "f") { e.preventDefault(); framesHere(); }
});
/* F: filmstrips for the folder being viewed. Additive (a plain build), and the
   location root has no node of its own, so there is nothing to aim at there. */
function framesHere() {
  if (state.kind !== "dir" || state.parent == null) return;
  return runFrameBuild(state.parent, false);
}
window.addEventListener("blur", hideCtx);
document.addEventListener("scroll", hideCtx, true);
$("#btn-film-close").onclick = closeFilm;
$("#btn-p-close").onclick = closePlayer;
// F toggles full screen; a double click is the way back to the thumbnails
{
  let dragging = false, lx = 0, ly = 0;
  const v = $("#viewer");
  v.addEventListener("mousedown", e => {
    if (zoom.s <= 1.001 || e.button !== 0) return;
    dragging = true; lx = e.clientX; ly = e.clientY; e.preventDefault();
    v.classList.add("dragging");
  });
  window.addEventListener("mousemove", e => {
    if (!dragging) return;
    panBy(e.clientX - lx, e.clientY - ly); lx = e.clientX; ly = e.clientY;
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false; v.classList.remove("dragging");
  });
}
/* Single click closes the picture (same as double click used to). Two guards: a pan
   while zoomed is a drag, not a click, and the second click of a habitual double click
   must not fall through onto the grid and reopen whatever was under the cursor. */
let vDownX = null, vDownY = null;
$("#viewer").addEventListener("mousedown", e => { vDownX = e.clientX; vDownY = e.clientY; });
$("#viewer").addEventListener("click", e => {
  if (e.target.closest("#btn-vinfo, #viewer-info")) return;   // their own handlers
  if (vDownX !== null && Math.hypot(e.clientX - vDownX, e.clientY - vDownY) > 6) return;  // panned
  vDownX = vDownY = null;
  closeViewer();
});
$("#viewer").addEventListener("dblclick", () => closeViewer());

/* Middle click = full-screen toggle. e.preventDefault() on mousedown is what
   suppresses Chrome's autoscroll cursor; without it the page starts scrolling
   under the picture. auxclick is cancelled too, so no middle-click paste. */
function middleFs(el) {
  el.addEventListener("mousedown", e => {
    if (e.button !== 1) return;
    e.preventDefault();
    toggleFs(el);
  });
  el.addEventListener("auxclick", e => { if (e.button === 1) e.preventDefault(); });
}
middleFs($("#viewer"));
middleFs($("#player"));
/* Wheel over the video = 5% of the clip per notch, down = forward (the same direction
   as the photo viewer's wheel). A mouse notch is deltaY 100; trackpads send a stream of
   tiny deltas, so they accumulate to one step per 80 units instead of racing ahead.
   The info panel stops its own wheel events, so it still scrolls by itself. */
let pWheelAcc = 0;
$("#player").addEventListener("wheel", e => {
  e.preventDefault();
  if (!pv.duration) return;
  const d = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaMode === 2 ? e.deltaY * 300 : e.deltaY;
  pWheelAcc += d;
  if (Math.abs(pWheelAcc) < 80) return;
  const step = 0.05 * pv.duration * (pWheelAcc > 0 ? 1 : -1);
  pWheelAcc = 0;
  pv.currentTime = Math.max(0, Math.min(pv.duration - 0.05, pv.currentTime + step));
  pShow(true);
}, { passive: false });

$("#viewer").addEventListener("wheel", e => {
  e.preventDefault();
  // the wheel always pages, zoomed or not: it is how Andy browses. Zoom is the
  // +/- keys only. Ctrl+wheel also zooms, for a mouse user who wants that.
  if (e.ctrlKey) zoomBy(e.deltaY > 0 ? 1 / 1.12 : 1.12);
  else viewerStep(e.deltaY > 0 ? 1 : -1);
}, { passive: false });

document.addEventListener("keydown", e => {
  if (!$("#viewer").hidden) {
    if (e.key === "Escape") closeViewer();
    else if ((e.key === "i" || e.key === "I") && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault(); setInfoOpen(!infoOpen);
    }
    else if (e.key === "f" || e.key === "F") { e.preventDefault(); toggleFs($("#viewer")); }
    else if (e.key === "+" || e.key === "=") { e.preventDefault(); zoomBy(1.3); }
    else if (e.key === "-" || e.key === "_") { e.preventDefault(); zoomBy(1 / 1.3); }
    else if (e.key === "0") { e.preventDefault(); zoomReset(); }
    else if (e.key === "/") { e.preventDefault(); zoomActual(); }   // 100%: 1 picture px = 1 screen px
    else if (e.key === "*") { e.preventDefault(); zoomReset(); }    // fit the window, both axes
    else if (zoom.s > 1.001 && (e.key === "ArrowRight" || e.key === "ArrowLeft" ||
                                e.key === "ArrowUp" || e.key === "ArrowDown")) {
      e.preventDefault();                          // zoomed: the arrows pan, not page
      const step = 80 * (e.shiftKey ? 3 : 1);
      if (e.key === "ArrowRight") panBy(-step, 0);
      else if (e.key === "ArrowLeft") panBy(step, 0);
      else if (e.key === "ArrowUp") panBy(0, step);
      else panBy(0, -step);
    }
    else if (e.key === "PageDown" || e.key === "ArrowRight") viewerStep(1);
    else if (e.key === "PageUp" || e.key === "ArrowLeft") viewerStep(-1);
  } else if (!$("#player").hidden) {
    if (e.key === "Escape") closePlayer();
    else if (e.key === "f" || e.key === "F") { e.preventDefault(); toggleFs($("#player")); }
    else if (e.key === " " || e.key === "k") { e.preventDefault(); pv.paused ? pv.play().catch(() => {}) : pv.pause(); pShow(false); }
    else if (e.key === "ArrowRight") { e.preventDefault(); pv.currentTime = Math.min(pv.duration || 0, pv.currentTime + 5); pShow(false); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); pv.currentTime = Math.max(0, pv.currentTime - 5); pShow(false); }
    else if (e.key === "m" || e.key === "M") pToggleMute();
    else if ((e.key === "i" || e.key === "I") && !e.altKey) { e.preventDefault(); setInfoOpen(!infoOpen); }
  } else if (!$("#filmstrip").hidden && e.key === "Escape") closeFilm();
  else if (e.target.tagName === "INPUT") return;   // slider etc keep native arrows
  else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();                            // never scroll the thumbnails
    treeStep(e.key === "ArrowDown" ? 1 : -1);
  }
  else if (e.key === "ArrowRight") { e.preventDefault(); treeExpand(); }
  else if (e.key === "ArrowLeft")  { e.preventDefault(); treeCollapse(); }
});

/* resizer */
{
  let dragging = false, sx = 0, sw = 0;
  const rz = $("#resizer"), pane = $("#tree-pane");
  rz.addEventListener("mousedown", e => { dragging = true; sx = e.clientX; sw = pane.offsetWidth; e.preventDefault(); });
  document.addEventListener("mousemove", e => {
    if (!dragging) return;
    pane.style.width = Math.max(140, sw + e.clientX - sx) + "px";
  });
  document.addEventListener("mouseup", () => dragging = false);
}

/* ---------- boot ---------- */
applyTheme();
startEvents();            // live tile painting while thumbs are built
viewApply();              // slider and sort label show the stored globals
$("#btn-sort").textContent = state.sort === "name" ? "Name" : "Date";
loadPrefs().then(buildTreeRoots).then(() => {
  const locs = state.locations;
  if (/^#\/l\d+/.test(location.hash)) navigateFromHash();   // deep link / reload
  else if (locs.length) openLocation(locs[0].id);
  checkBuild();                                             // never run a stale client
  setInterval(checkBuild, 60000);
});

/* ================= phones, folded screens and touch (v1.0.2) =================
   Two problems, one shell: the desktop split (tree + grid side by side) does not fit a
   phone, and a fold seam is a viewport change Chromium does not always take -- the page
   stays laid out for the folded width and the rest of the unfolded screen is bare
   canvas. The drawer is CSS; this is the plumbing, plus the touch gestures the desktop
   mouse never needed. */
const NARROW_MQ = matchMedia("(max-width: 820px), (pointer: coarse) and (max-width: 1024px)");
const narrowNow = () => NARROW_MQ.matches;

function syncTopbarH() {
  const h = $("#topbar").offsetHeight;
  if (h) document.documentElement.style.setProperty("--topbar-h", h + "px");
}

/* Nothing in CSS can rescue a stale layout viewport, so compare what the page thinks it
   has with what the screen actually is. Only when the SCREEN IS WIDER: pinch-zoom makes
   the visual viewport smaller, and rewriting the meta there would shrink the layout to
   the zoom box. */
function healViewport() {
  const vv = window.visualViewport;
  const real = Math.round((vv && vv.width) || window.innerWidth || 0);
  const laid = document.documentElement.clientWidth || 0;
  if (!real || !laid || real <= laid + 24) return;
  const m = document.querySelector('meta[name="viewport"]');
  if (!m) return;
  m.setAttribute("content", "width=" + real + ", initial-scale=1");
  setTimeout(() => {                       // back to device-width for the next fold
    m.setAttribute("content", "width=device-width, initial-scale=1");
    syncTopbarH();
  }, 400);
}

function setTree(open) {
  document.body.classList.toggle("tree-open", open);
  $("#tree-scrim").hidden = !open;
  const b = $("#btn-tree");
  b.classList.toggle("on", open);
  b.setAttribute("aria-expanded", open ? "true" : "false");
}
$("#btn-tree").onclick = () => setTree(!document.body.classList.contains("tree-open"));
$("#tree-scrim").onclick = () => setTree(false);
/* A drawer over the grid swallows the tap that navigates: close it on any tree click. */
$("#tree").addEventListener("click", () => { if (narrowNow()) setTree(false); });
addEventListener("hashchange", () => setTree(false));

let tPress = 0, suppressClick = 0;
/* Chrome Android never fires contextmenu on a plain div, so right-click was unreachable
   by touch. A long press synthesises one at the finger -- and the touchend that follows
   would also click whatever is under it (often a line of the menu that just opened), so
   the next click is swallowed. */
document.addEventListener("touchstart", e => {
  if (e.touches.length !== 1) { clearTimeout(tPress); tPress = 0; return; }
  const el = e.target.closest && e.target.closest(".cell, .tnode, .znode");
  if (!el) return;
  const t = e.touches[0], x = t.clientX, y = t.clientY;
  tPress = setTimeout(() => {
    tPress = 0;
    suppressClick = performance.now() + 350;
    el.dispatchEvent(new MouseEvent("contextmenu",
      { bubbles: true, cancelable: true, clientX: x, clientY: y }));
  }, 450);
}, { passive: true });
const endPress = () => { if (tPress) { clearTimeout(tPress); tPress = 0; } };
document.addEventListener("touchmove", endPress, { passive: true });
document.addEventListener("touchend", endPress);
document.addEventListener("touchcancel", endPress);
document.addEventListener("click", e => {
  if (performance.now() < suppressClick) { e.stopPropagation(); e.preventDefault(); }
}, true);

/* Swipe the viewer: sideways pages, down closes. Not while zoomed -- there a drag pans. */
let vSwipe = null;
$("#viewer").addEventListener("touchstart", e => {
  if (e.touches.length !== 1 || zoom.s > 1.001) { vSwipe = null; return; }
  const t = e.touches[0];
  vSwipe = { x: t.clientX, y: t.clientY, t: Date.now() };
}, { passive: true });
$("#viewer").addEventListener("touchend", e => {
  if (!vSwipe) return;
  const t = e.changedTouches[0];
  const dx = t.clientX - vSwipe.x, dy = t.clientY - vSwipe.y, dt = Date.now() - vSwipe.t;
  vSwipe = null;
  if (dt > 900) return;                      // a slow drag is a pan, not a swipe
  if (Math.abs(dx) > Math.abs(dy)) {
    if (Math.abs(dx) < 55) return;
    suppressClick = performance.now() + 350;   // a swipe is not a tap-to-close
    viewerStep(dx < 0 ? 1 : -1);
  } else if (dy > 110) {
    suppressClick = performance.now() + 350;
    closeViewer();
  }
}, { passive: true });

syncTopbarH();
addEventListener("resize", () => { syncTopbarH(); healViewport(); });
addEventListener("orientationchange", () => setTimeout(() => { syncTopbarH(); healViewport(); }, 250));
if (window.visualViewport) window.visualViewport.addEventListener("resize", healViewport);
addEventListener("pageshow", () => { syncTopbarH(); healViewport(); });

/* ================= continuous scroll across folders and zips =================
   Owner: "scrolling through zips and getting to the end — can continuing to
   scroll move to the start of the next folder or zip? Also in reverse?"
   A container is a folder or a zip. With the grid pinned at its bottom (or top)
   and the wheel still turning that way, the walk steps to the next (or previous)
   container: the current one's siblings in grid order first, then up a level and
   on from there — which is the order the tree shows. Forward lands at the TOP of
   the target; backward lands at its END, so a scroll up keeps reading upward.
   Deliberate, not twitchy: nothing moves until HOP_FIRE pixels of continued
   scrolling in one direction, the pill appears at HOP_ARM so the jump is never a
   surprise, and any reversal or a HOP_IDLE pause forgets the debt. */
const HOP_ARM = 160, HOP_FIRE = 560, HOP_IDLE = 900;
let hopAcc = 0, hopDir = 0, hopTimer = 0, hopBusy = false, hopNoMore = false;
const hopSibs = new Map();          // "loc:parent:sort" -> [{id,kind,name}]

function hopPill() {
  let el = $("#hop-hint");
  if (!el) {
    el = document.createElement("div");
    el.id = "hop-hint";
    el.hidden = true;
    el.innerHTML = '<span class="arw"></span><span class="txt"></span><i></i>';
    document.body.appendChild(el);
  }
  return el;
}

function hopReset() {
  hopAcc = 0; hopDir = 0; hopNoMore = false;
  const el = $("#hop-hint");
  if (el) { el.hidden = true; el.style.setProperty("--p", "0%"); }
  if (hopTimer) { clearTimeout(hopTimer); hopTimer = 0; }
}

/* Is the grid pinned at an edge it should be reading past? 1 bottom, -1 top, 0 no.
   `dir` is the way the user is scrolling. A list that cannot scroll is NOT an edge:
   nothing moved, so there is no "bottom" for the owner to see, and the pill appearing
   the moment a two-item folder is wheeled reads as a bug (it did, 2026-11-06). Leaving
   a short folder stays available through the breadcrumb, Backspace and Alt+←, which are
   visible affordances rather than a silent jump. The location root is not a container
   and never hops. */
function hopEdge(dir) {
  if (!($("#viewer") && $("#viewer").hidden)) return 0;
  if (!($("#player") && $("#player").hidden)) return 0;
  if (state.kind !== "dir" && state.kind !== "zip") return 0;
  const g = $("#grid");
  if (!g) return 0;
  const room = g.scrollHeight - g.clientHeight;
  if (room <= 2) return 0;                 // nothing to scroll -- no edge, no pill
  if (g.scrollTop >= room - 2) return 1;
  if (g.scrollTop <= 2) return -1;
  return 0;
}

function hopParentOf(chain) {
  const prev = chain[chain.length - 1];
  return prev && prev.kind === "f" ? prev.id : null;      // null = the location root
}

async function hopContainers(parentId) {
  const key = state.loc + ":" + (parentId == null ? 0 : parentId) + ":" + state.sort;
  if (hopSibs.has(key)) return hopSibs.get(key);
  let list = [];
  try {
    const d = await api(`/api/children?loc=${state.loc}&parent=${parentId == null ? 0 : parentId}` +
                        `&sort=${state.sort}`);
    list = (d.items || []).filter(x => x.kind === "dir" || x.kind === "zip")
                          .map(x => ({ id: x.id, kind: x.kind, name: x.name }));
  } catch (e) { list = []; }
  hopSibs.set(key, list);                    // remembered: a hop back costs nothing
  return list;
}

/* The next container in tree order, or null at the far end of the library.
   Order is a depth-first walk of the tree, which is what the sidebar shows:
     1. a folder you have just read through hands over to ITS OWN first folder or
        zip (owner: "can it automatically go to each child?") -- only the folder you
        are actually in does this, never an ancestor you merely climb out of,
        otherwise finishing the last zip in a folder would drop you straight back
        into its first one and the walk would circle that folder for ever;
     2. a leaf (a zip, or a folder with no containers inside) moves to its next
        sibling;
     3. with no siblings left, up a level and on from there. */
async function hopNext() {
  const chain = pathChain.slice();
  let cur = chain.pop();
  if (!cur) return null;                     // the location root has no neighbours
  if (cur.kind === "f") {
    const kids = await hopContainers(cur.id);
    if (kids.length) return kids[0];          // straight down, not sideways
  }
  for (let step = 0; step < 30 && cur; step++) {
    const sibs = await hopContainers(hopParentOf(chain));
    const want = cur.kind === "z" ? "zip" : "dir";
    const i = sibs.findIndex(x => x.id === cur.id && x.kind === want);
    const t = i >= 0 ? sibs[i + 1] : null;
    if (t) return t;
    cur = chain.pop();                       // out of siblings here: climb and carry on
  }
  return null;
}

/* The mirror image: the previous container in the same depth-first order -- the
   LAST container inside the previous sibling (so "back" lands at the end of the
   branch before this one), or the parent when this is the first thing in it. */
async function hopPrev() {
  const chain = pathChain.slice();
  let cur = chain.pop();
  if (!cur) return null;
  for (let step = 0; step < 30 && cur; step++) {
    const sibs = await hopContainers(hopParentOf(chain));
    const want = cur.kind === "z" ? "zip" : "dir";
    const i = sibs.findIndex(x => x.id === cur.id && x.kind === want);
    if (i > 0) {
      let t = sibs[i - 1];
      for (let d = 0; d < 12; d++) {          // down to the end of that branch
        if (t.kind === "zip") break;          // (listings here use "dir"/"zip", not "f"/"z")
        const kids = await hopContainers(t.id);
        if (!kids.length) break;
        t = kids[kids.length - 1];
      }
      return t;
    }
    cur = chain.pop();
    if (!cur) return null;
    return cur;                              // first in its folder: the folder itself
  }
  return null;
}

async function hopTarget(dir) {
  return dir > 0 ? hopNext() : hopPrev();
}

async function hopGo(t, dir) {
  hopBusy = true;
  hopReset();
  try {
    if (t.kind === "zip") await openZip(t.id, t.name, newNav());
    else await openFolder(t.id, newNav());
    const g = $("#grid");
    const land = () => { if (g) g.scrollTop = dir > 0 ? 0 : g.scrollHeight; };
    land();
    requestAnimationFrame(() => { land(); requestAnimationFrame(land); });
  } finally { hopBusy = false; }
}

async function hopFire(dir) {
  if (hopBusy) return;
  hopBusy = true;
  const t = await hopTarget(dir);
  hopBusy = false;
  if (!t) {
    // nothing that way: say so instead of silently eating the gesture
    await hopArm(dir);
    setTimeout(hopReset, 1100);
    return;
  }
  await hopGo(t, dir);
}

async function hopArm(dir) {                 // the pill, before anything moves
  const el = hopPill();
  const t = await hopTarget(dir);
  hopNoMore = !t;
  el.className = t ? (dir > 0 ? "next" : "prev") : "end";
  el.querySelector(".arw").textContent = dir > 0 ? "⇣" : "⇡";
  el.querySelector(".txt").textContent = t
    ? (dir > 0 ? "Next: " : "Back to: ") + t.name + (t.kind === "zip" ? "   (zip)" : "")
    : (dir > 0 ? "End of the library" : "Start of the library");
  el.hidden = false;
}

function hopFeed(dy) {
  if (hopBusy || !dy) return;
  const dir = dy > 0 ? 1 : -1;
  const edge = hopEdge(dir);
  if (!edge) { if (hopAcc) hopReset(); return; }
  if ((dir > 0 && edge !== 1) || (dir < 0 && edge !== -1)) {
    if (hopAcc) hopReset();
    return;
  }
  if (hopDir !== dir) { hopAcc = 0; hopDir = dir; }
  hopAcc += Math.abs(dy);
  if (hopTimer) clearTimeout(hopTimer);
  hopTimer = setTimeout(hopReset, HOP_IDLE);
  const el = hopPill();
  el.style.setProperty("--p", Math.min(100, (hopAcc / HOP_FIRE) * 100).toFixed(0) + "%");
  if (hopAcc >= HOP_FIRE) { hopFire(dir); return; }
  if (hopAcc >= HOP_ARM && el.hidden) hopArm(dir);
}

{
  const g = $("#grid");
  if (g) {
    g.addEventListener("wheel", e => {
      if (e.ctrlKey || e.metaKey) return;   // ctrl+wheel is the browser's zoom
      hopFeed(e.deltaY);
    }, { passive: true });
    let ty = 0;
    g.addEventListener("touchstart", e => { ty = e.touches[0] ? e.touches[0].clientY : 0; },
                      { passive: true });
    g.addEventListener("touchmove", e => {
      const t0 = e.touches[0];
      if (!t0) return;
      const dy = ty - t0.clientY;           // finger up reads forward
      ty = t0.clientY;
      if (Math.abs(dy) > 2) hopFeed(dy);
    }, { passive: true });
  }
  // Paging keys at an edge do the same thing: one press is already deliberate.
  document.addEventListener("keydown", e => {
    if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "")) return;
    if (e.key === "PageDown" && hopEdge(1) === 1) { e.preventDefault(); hopFire(1); }
    else if (e.key === "PageUp" && hopEdge(-1) === -1) { e.preventDefault(); hopFire(-1); }
  });
}
