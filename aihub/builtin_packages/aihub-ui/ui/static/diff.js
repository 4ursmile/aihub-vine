// Diff and Git tabs of the canvas (loaded when first opened).
// Diff: every changed file of the folder since the last message or the last commit, unified or side by side, changed words marked,
// whitespace optionally ignored, copy as a patch, restore one file. Git: read-only look at the repository (branch, status, history,
// branches, one commit and its files). The server only reads git: nothing here commits, checks out, stages or fetches.
import { html, useState, useEffect, useMemo } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { toast, guard, Seg, confirmBox } from "./ui.js";
import { useHl, langOf, colorize, splitHl, esc } from "./code.js";

const pref = (k, d) => { try { return localStorage.getItem("aihub_ui_" + k) ?? d; } catch (e) { return d; } };
const setPref = (k, v) => { try { localStorage.setItem("aihub_ui_" + k, v); } catch (e) { /* private mode */ } };
const baseName = (p) => String(p || "").split("/").pop() || p;
const dirName = (p) => { const s = String(p || ""); return s.includes("/") ? s.slice(0, s.lastIndexOf("/")) : ""; };
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? "now" : s < 5400 ? Math.round(s / 60) + "m ago" : s < 129600 ? Math.round(s / 3600) + "h ago" : s < 86400 * 60 ? Math.round(s / 86400) + "d ago" : new Date(t * 1000).toLocaleDateString(); };
const when = (t) => new Date(t * 1000).toLocaleString();
const enc = encodeURIComponent;
const copy = async (t, what) => { try { await navigator.clipboard.writeText(t); toast((what || "Text") + " copied"); } catch (e) { toast("Could not copy"); } };
const CTX = 3;

// ---------- the diff itself
// Lines of a text without the empty piece after a final newline (that newline is reported on its own).
const linesOf = (t) => { const a = t === "" ? [] : t.split("\n"); if (a.length && a[a.length - 1] === "") a.pop(); return a; };
// Line diff -> [[op, text, ai, bi]]: op " " "-" "+", ai / bi = index in the old / new lines (-1 when not there).
// Common head and tail are cut first; the middle is an LCS, or all-removed + all-added when too big. WS compares lines ignoring whitespace.
export function lineDiff(a, b, ws) {
  const A = linesOf(a), B = linesOf(b), norm = (t) => t.trim().replace(/\s+/g, " ");
  const KA = ws ? A.map(norm) : A, KB = ws ? B.map(norm) : B;
  let s = 0; while (s < A.length && s < B.length && KA[s] === KB[s]) s++;
  let ea = A.length, eb = B.length; while (ea > s && eb > s && KA[ea - 1] === KB[eb - 1]) { ea--; eb--; }
  const out = [];
  for (let i = 0; i < s; i++) out.push([" ", B[i], i, i]);
  const n = ea - s, m = eb - s;
  if (n * m > 4e6) { for (let i = s; i < ea; i++) out.push(["-", A[i], i, -1]); for (let j = s; j < eb; j++) out.push(["+", B[j], -1, j]); }
  else {
    const w = m + 1, L = new Uint32Array((n + 1) * w);
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i * w + j] = KA[s + i] === KB[s + j] ? L[(i + 1) * w + j + 1] + 1 : Math.max(L[(i + 1) * w + j], L[i * w + j + 1]);
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (KA[s + i] === KB[s + j]) { out.push([" ", B[s + j], s + i, s + j]); i++; j++; }
      else if (L[(i + 1) * w + j] >= L[i * w + j + 1]) { out.push(["-", A[s + i], s + i, -1]); i++; }
      else { out.push(["+", B[s + j], -1, s + j]); j++; }
    }
    for (; i < n; i++) out.push(["-", A[s + i], s + i, -1]);
    for (; j < m; j++) out.push(["+", B[s + j], -1, s + j]);
  }
  for (let i = ea, j = eb; i < A.length; i++, j++) out.push([" ", B[j], i, j]);
  return out;
}
// In each run of removed lines followed by added lines, the k-th removed line is paired with the k-th added one: op index -> partner.
function pairsOf(ops) {
  const p = new Map();
  for (let i = 0; i < ops.length;) {
    if (ops[i][0] === " ") { i++; continue; }
    let d = i; while (d < ops.length && ops[d][0] === "-") d++;
    let a = d; while (a < ops.length && ops[a][0] === "+") a++;
    for (let k = 0; k < Math.min(d - i, a - d); k++) { p.set(i + k, d + k); p.set(d + k, i + k); }
    i = Math.max(a, i + 1);
  }
  return p;
}
const TOK = /\w+|\s+|[^\w\s]/g;
const grow = (r, s, e) => { if (r.length && r[r.length - 1][1] === s) r[r.length - 1][1] = e; else r.push([s, e]); };
// Changed character ranges of two paired lines, word by word: [old ranges, new ranges]; null when the lines are mostly rewritten.
export function wordRanges(a, b) {
  const x = a.match(TOK) || [], y = b.match(TOK) || [];
  if (!x.length || !y.length || x.length * y.length > 6e4) return null;
  const n = x.length, m = y.length, w = m + 1, L = new Uint16Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i * w + j] = x[i] === y[j] ? L[(i + 1) * w + j + 1] + 1 : Math.max(L[(i + 1) * w + j], L[i * w + j + 1]);
  const ra = [], rb = []; let i = 0, j = 0, pa = 0, pb = 0, same = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { if (/\S/.test(x[i])) same += x[i].length; pa += x[i].length; pb += y[j].length; i++; j++; }
    else if (L[(i + 1) * w + j] >= L[i * w + j + 1]) { grow(ra, pa, pa + x[i].length); pa += x[i++].length; }
    else { grow(rb, pb, pb + y[j].length); pb += y[j++].length; }
  }
  for (; i < n; i++) { grow(ra, pa, pa + x[i].length); pa += x[i].length; }
  for (; j < m; j++) { grow(rb, pb, pb + y[j].length); pb += y[j].length; }
  if (same < 0.3 * Math.max(a.replace(/\s/g, "").length, b.replace(/\s/g, "").length)) return null;     // mostly rewritten: marking every word is noise
  return [ra, rb];
}
// Wrap the character RANGES of a highlighted line (HTML with spans and entities) in <mark class="dw">, closed and reopened around tags so it always nests.
export function markHtml(h, R) {
  if (!R || !R.length) return h;
  const open = '<mark class="dw">';
  let out = "", pos = 0, ri = 0, inM = false, i = 0;
  while (i < h.length) {
    if (h[i] === "<") { const j = h.indexOf(">", i); const tag = h.slice(i, j < 0 ? h.length : j + 1); out += inM ? "</mark>" + tag + open : tag; i += tag.length; continue; }
    while (ri < R.length && pos >= R[ri][1]) { if (inM) { out += "</mark>"; inM = false; } ri++; }
    if (!inM && ri < R.length && pos >= R[ri][0]) { out += open; inM = true; }
    let ch = h[i];
    if (ch === "&") { const j = h.indexOf(";", i); if (j > i && j - i < 10) ch = h.slice(i, j + 1); }
    out += ch; i += ch.length; pos++;
  }
  return inM ? out + "</mark>" : out;
}
// Unified patch text of OPS (the usual @@ hunks with 3 lines of context), for the clipboard.
export function patchText(ops, path, eol) {
  const N = ops.length, oc = [0], nc = [0];
  ops.forEach((o, k) => { oc.push(oc[k] + (o[0] !== "+" ? 1 : 0)); nc.push(nc[k] + (o[0] !== "-" ? 1 : 0)); });
  const ch = ops.map((o, k) => (o[0] !== " " ? k : -1)).filter((k) => k >= 0), out = [`--- a/${path}`, `+++ b/${path}`];
  for (let h = 0; h < ch.length;) {
    const s = Math.max(0, ch[h] - CTX); let e = Math.min(N - 1, ch[h] + CTX);
    while (h + 1 < ch.length && ch[h + 1] - CTX <= e + 1) { h++; e = Math.min(N - 1, ch[h] + CTX); }
    h++;
    const ol = oc[e + 1] - oc[s], nl = nc[e + 1] - nc[s];
    out.push(`@@ -${ol ? oc[s] + 1 : oc[s]},${ol} +${nl ? nc[s] + 1 : nc[s]},${nl} @@`);
    for (let k = s; k <= e; k++) out.push(ops[k][0] + ops[k][1]);
  }
  if (eol) out.push("\\ " + eol);
  return out.join("\n") + "\n";
}

// The diff of two texts: summary bar (HEAD renders its buttons), then the hunks, unified or split; folded context opens on click.
export function DiffBody({ oldText, newText, path, mode = "unified", ws = false, head }) {
  const hl = useHl();
  const hlOf = useMemo(() => { const lang = langOf(hl, path); return (t) => splitHl(colorize(hl, lang, t || "")); }, [hl, path]);
  const oldL = useMemo(() => hlOf(oldText), [oldText, hlOf]), newL = useMemo(() => hlOf(newText), [newText, hlOf]);
  const ops = useMemo(() => lineDiff(oldText, newText, ws), [oldText, newText, ws]);
  const pairs = useMemo(() => pairsOf(ops), [ops]), words = useMemo(() => new Map(), [ops]);
  const [more, setMore] = useState(() => new Set());
  useEffect(() => setMore(new Set()), [ops]);
  const add = ops.reduce((n, o) => n + (o[0] === "+"), 0), del = ops.reduce((n, o) => n + (o[0] === "-"), 0);
  const ea = oldText !== "" && !oldText.endsWith("\n"), eb = newText !== "" && !newText.endsWith("\n");
  const eol = oldText && newText && ea !== eb ? (eb ? "No newline at end of file (removed)" : "Newline at end of file added") : "";
  const bar = html`<div class="dsum"><span class="tag good">+${add}</span> <span class="tag bad">−${del}</span>${eol && html`<span class="mute small">${eol}</span>`}<span class="grow"></span>${head && head({ add, del, patch: () => patchText(ops, path, eol) })}</div>`;
  if (!add && !del) return html`${bar}<div class="empty"><div>${Ic("check", 26)}<div>No changes${ws ? " (ignoring whitespace)" : ""}${eol ? " except the final newline" : ""}.</div></div></div>`;
  const cell = (i) => {
    if (i == null) return "";
    const o = ops[i], raw = o[0] === "-" ? oldL[o[2]] : newL[o[3]], h = raw ?? esc(o[1]);
    if (o[0] === " " || !pairs.has(i)) return h;
    const k = Math.min(i, pairs.get(i));
    if (!words.has(k)) words.set(k, wordRanges(ops[k][1], ops[pairs.get(k)][1]));
    const r = words.get(k);
    return r ? markHtml(h, r[o[0] === "-" ? 0 : 1]) : h;
  };
  const keep = new Set();
  ops.forEach((o, i) => { if (o[0] !== " ") for (let k = Math.max(0, i - CTX); k <= Math.min(ops.length - 1, i + CTX); k++) keep.add(k); });
  // rows: unified = one op per row; split = [left op, right op] with removed / added lines side by side
  let rows = [];
  if (mode === "split") {
    for (let i = 0; i < ops.length;) {
      if (ops[i][0] === " ") { rows.push([i, i]); i++; continue; }
      let d = i; while (d < ops.length && ops[d][0] === "-") d++;
      let a = d; while (a < ops.length && ops[a][0] === "+") a++;
      for (let k = 0; k < Math.max(d - i, a - d); k++) rows.push([k < d - i ? i + k : null, k < a - d ? d + k : null]);
      i = Math.max(a, i + 1);
    }
  } else rows = ops.map((o, i) => [i]);
  const shown = (r) => r.some((i) => i != null && (keep.has(i) || more.has(i)));
  const out = []; let gap = [];
  const flush = () => {
    if (!gap.length) return;
    const ids = gap.flat().filter((i) => i != null), g = ids.length && ops[ids[0]];
    out.push(html`<button type="button" class="dl gap" title="Show these lines" onClick=${() => setMore(new Set([...more, ...ids]))}>${Ic("down", 12)} ${gap.length} unchanged line${gap.length > 1 ? "s" : ""}${g ? html`<span class="mute"> · from line ${g[3] + 1}</span>` : ""}</button>`);
    gap = [];
  };
  const ln = (i, side) => { if (i == null) return ""; const o = ops[i]; return side === 0 ? (o[0] === "+" ? "" : o[2] + 1) : (o[0] === "-" ? "" : o[3] + 1); };
  for (const r of rows) {
    if (!shown(r)) { gap.push(r); continue; }
    flush();
    if (mode === "split") {
      const [l, rr] = r, lc = l != null && ops[l][0] === "-" ? " del" : "", rc = rr != null && ops[rr][0] === "+" ? " add" : "";
      out.push(html`<div class="dr"><span class=${"ln" + lc}>${ln(l, 0)}</span><span class=${"tx syn" + lc + (l == null ? " none" : "")} dangerouslySetInnerHTML=${{ __html: cell(l) }}></span><span class=${"ln" + rc}>${ln(rr, 1)}</span><span class=${"tx syn" + rc + (rr == null ? " none" : "")} dangerouslySetInnerHTML=${{ __html: cell(rr) }}></span></div>`);
    } else {
      const i = r[0], op = ops[i][0];
      out.push(html`<div class=${"dl " + (op === "+" ? "add" : op === "-" ? "del" : "")}><span class="ln">${ln(i, 0)}</span><span class="ln">${ln(i, 1)}</span><span class="tx syn"><span class="dm">${op} </span><span dangerouslySetInnerHTML=${{ __html: cell(i) }}></span></span></div>`);
    }
  }
  flush();
  return html`${bar}<div class=${"dlist" + (mode === "split" ? " sbs" : "")}>${out}</div>`;
}

const ST = { added: ["A", "good", "Added"], untracked: ["U", "good", "Untracked (new, not in git yet)"], modified: ["M", "acc", "Modified"], deleted: ["D", "bad", "Deleted"], conflict: ["C", "bad", "Merge conflict"] };
const stTag = (st) => { const s = ST[st] || ST.modified; return html`<span class=${"dst " + s[1]} title=${s[2]}>${s[0]}</span>`; };
const counts = (f) => f.binary ? html`<span class="mute small">bin</span>` : f.add == null ? "" : html`<span class="dn">${f.add ? html`<b class="good">+${f.add}</b>` : ""}${f.del ? html`<b class="bad">−${f.del}</b>` : ""}</span>`;
// A list of changed files ({path, status, add, del, binary}) with one selected.
function FileList({ files, sel, onPick }) {
  return html`<div class="dfl">${files.map((f) => html`<button type="button" class=${"dfr" + (f.path === sel ? " on" : "")} title=${f.path} onClick=${() => onPick(f.path)}>${stTag(f.status)}<span class="t"><b>${baseName(f.path)}</b>${dirName(f.path) && html`<span class="mute"> ${dirName(f.path)}</span>`}</span>${counts(f)}</button>`)}</div>`;
}
// Display options shared by the Diff tab and commit files: unified / split, ignore whitespace (kept in prefs).
function useViewOpts() {
  const [mode, setM] = useState(() => pref("diffmode", "unified")), [ws, setW] = useState(() => pref("diffws", "0") === "1");
  const setMode = (v) => { setM(v); setPref("diffmode", v); }, setWs = (v) => { setW(v); setPref("diffws", v ? "1" : "0"); };
  const ctl = html`<${Seg} small value=${mode} onChange=${setMode} items=${[{ id: "unified", label: "Unified" }, { id: "split", label: "Split" }]}/>
    <button class=${"btn" + (ws ? " on" : "")} title="Ignore changes in whitespace and indentation" aria-pressed=${ws} onClick=${() => setWs(!ws)}>${Ic("filter", 13)} Whitespace</button>`;
  return { mode, ws, ctl };
}
const msg = (icon, text) => html`<div class="empty"><div>${Ic(icon, 26)}<div>${text}</div></div></div>`;

// ---------- Diff tab
export function DiffView({ id, path, ver, rev, api, put, onPick, onSaved }) {
  const [base, setB] = useState(() => pref("diffbase", "turn")), [rail, setR] = useState(() => pref("diffrail", "1") === "1");
  const [d, setD] = useState(null), [list, setList] = useState(undefined), { mode, ws, ctl } = useViewOpts();
  const setBase = (v) => { setB(v); setPref("diffbase", v); }, setRail = (v) => { setR(v); setPref("diffrail", v ? "1" : "0"); };
  useEffect(() => { let on = true; setList(undefined); api(`files/changes?id=${id}&base=${base}`).then((r) => on && setList(r.files)).catch(() => on && setList(null)); return () => { on = false; }; }, [id, base, rev]);
  useEffect(() => { let on = true; setD(null); if (path) api(`files/diff?id=${id}&path=${enc(path)}&base=${base}`).then((r) => on && setD(r)).catch((e) => on && setD({ err: e.message })); return () => { on = false; }; }, [id, path, ver, base, rev]);
  const files = list || [], at = files.findIndex((f) => f.path === path);
  const step = (k) => { if (!files.length) return; const n = at < 0 ? (k > 0 ? 0 : files.length - 1) : (at + k + files.length) % files.length; onPick(files[n].path); };
  const restore = guard(async () => {
    if (!(await confirmBox({ title: "Restore " + baseName(path) + "?", text: base === "git" ? "The file goes back to its last committed version. Your current changes to it are lost." : "The file goes back to how it was before the last message. The changes since then are lost.", ok: "Restore", danger: true }))) return;
    await put("files/content", { id, path, text: d.base }); toast("Restored " + baseName(path)); onSaved && onSaved();
  });
  const head = ({ patch }) => html`<button class="btn" title="Copy these changes as a unified patch" onClick=${() => copy(patch(), "Patch")}>${Ic("copy", 13)} Patch</button>${!d.deleted && typeof d.base === "string" && html`<button class="btn" title=${base === "git" ? "Put back the committed version" : "Put back the version from before the last message"} onClick=${restore}>${Ic("undo", 13)} Restore</button>`}`;
  const bar = html`<div class="dbar"><${Seg} small value=${base} onChange=${setBase} items=${[{ id: "turn", label: "Since last message" }, { id: "git", label: "Since last commit" }]}/>${ctl}<span class="grow"></span>
    <button class="ibtn sm" title="Previous changed file" aria-label="Previous changed file" disabled=${!files.length} onClick=${() => step(-1)}>${Ic("up", 14)}</button><button class="ibtn sm" title="Next changed file" aria-label="Next changed file" disabled=${!files.length} onClick=${() => step(1)}>${Ic("down", 14)}</button>
    <button class=${"ibtn sm" + (rail ? " on" : "")} title="Show the changed files" aria-label="Show the changed files" onClick=${() => setRail(!rail)}>${Ic("list", 14)}</button></div>`;
  const none = base === "git" ? "This folder is not in a git repository with a commit." : "Nothing to compare yet: the snapshot is taken when you send a message.";
  const side = rail && html`<div class="dfiles"><div class="dfh"><b>${list === undefined ? "…" : files.length}</b> changed${list && files.length ? html`<span class="mute"> · <b class="good">+${files.reduce((n, f) => n + (f.add || 0), 0)}</b> <b class="bad">−${files.reduce((n, f) => n + (f.del || 0), 0)}</b></span>` : ""}</div>
    ${list === undefined ? html`<div class="shine small" style="padding:10px">Looking…</div>` : list === null ? html`<p class="mute small" style="padding:10px">${none}</p>` : files.length ? html`<${FileList} files=${files} sel=${path} onPick=${onPick}/>` : html`<p class="mute small" style="padding:10px">No changed files.</p>`}</div>`;
  let body;
  if (!path) body = list && files.length ? msg("layers", html`${files.length} changed file${files.length > 1 ? "s" : ""}. Pick one${rail ? " on the left" : html`: <button class="lnk" onClick=${() => setRail(true)}>show the list</button>`}.`) : msg("layers", list === null ? none : list === undefined ? "Looking for changes…" : "No changed files.");
  else if (!d) body = html`<div class="empty"><div class="shine">Comparing…</div></div>`;
  else if (d.err) body = msg("alert", d.err);
  else if (d.binary) body = msg("file", d.big ? "Too large to compare here." : "Binary file: there is no text to compare.");
  else if (d.base === null) body = msg("layers", html`${base === "git" ? "This file is not tracked in a git repository here." : "Nothing to compare yet: the snapshot is taken when you send a message (big and binary files are not kept)."}${d.git && base === "turn" ? html` <button class="lnk" onClick=${() => setBase("git")}>Compare with the last commit</button>` : d.turn && base === "git" ? html` <button class="lnk" onClick=${() => setBase("turn")}>Compare with the last message</button>` : ""}`);
  else body = html`${d.deleted && html`<div class="dnote bad">${Ic("trash", 13)} Deleted ${base === "git" ? "since the last commit" : "since the last message"}</div>`}<${DiffBody} oldText=${d.base} newText=${d.now} path=${path} mode=${mode} ws=${ws} head=${head}/>`;
  return html`<div class="diffv">${bar}<div class="dmain">${side}<div class="dpane">${body}</div></div></div>`;
}

// ---------- Git tab
// One file of a commit, before and after.
function CommitFile({ id, api, sha, path, mode, ws }) {
  const [d, setD] = useState(null);
  useEffect(() => { let on = true; setD(null); api(`git/file?id=${id}&rev=${sha}&path=${enc(path)}`).then((r) => on && setD(r)).catch((e) => on && setD({ err: e.message })); return () => { on = false; }; }, [id, sha, path]);
  if (!d) return html`<div class="shine small" style="padding:10px">Loading…</div>`;
  if (d.err) return html`<p class="mute small" style="padding:10px">${d.err}</p>`;
  if (d.binary) return html`<p class="mute small" style="padding:10px">${d.big ? "Too large to show here." : "Binary file."}</p>`;
  return html`<${DiffBody} oldText=${d.old} newText=${d.new} path=${path} mode=${mode} ws=${ws} head=${({ patch }) => html`<button class="btn" onClick=${() => copy(patch(), "Patch")}>${Ic("copy", 13)} Patch</button>`}/>`;
}
const Refs = ({ refs }) => (refs || []).map((r) => html`<span class=${"gref" + (/^HEAD/.test(r) ? " head" : /^tag: /.test(r) ? " tag" : /\//.test(r) ? " remote" : "")}>${r.replace(/^HEAD -> /, "")}</span>`);
function CommitView({ id, api, sha, back, open }) {
  const [c, setC] = useState(null), [f, setF] = useState(null), { mode, ws, ctl } = useViewOpts();
  useEffect(() => { let on = true; setC(null); setF(null); api(`git/commit?id=${id}&rev=${sha}`).then((r) => on && setC(r.commit)).catch((e) => on && setC({ err: e.message })); return () => { on = false; }; }, [id, sha]);
  const top = html`<div class="dbar"><button class="btn" onClick=${back}>${Ic("left", 13)} History</button>${f && ctl}</div>`;
  if (!c) return html`<div class="gitv">${top}<div class="empty"><div class="shine">Loading commit…</div></div></div>`;
  if (c.err) return html`<div class="gitv">${top}${msg("alert", c.err)}</div>`;
  const [subj, ...rest] = c.message.split("\n"), body = rest.join("\n").trim(), add = c.files.reduce((n, x) => n + (x.add || 0), 0), del = c.files.reduce((n, x) => n + (x.del || 0), 0);
  return html`<div class="gitv">${top}<div class="gscroll"><div class="gcom">
      <h3>${subj}</h3>${body && html`<pre class="gmsg">${body}</pre>`}
      <div class="gmeta"><span>${Ic("user", 13)} <b>${c.author}</b> <span class="mute">${"<" + c.email + ">"}</span></span><span title=${when(c.date)}>${Ic("clock", 13)} ${when(c.date)}</span>${c.committer && c.committer !== c.author && html`<span class="mute">committed by ${c.committer}</span>`}</div>
      <div class="gmeta"><button class="gsha" title="Copy the commit id" onClick=${() => copy(c.sha, "Commit id")}>${Ic("copy", 12)} ${c.sha.slice(0, 12)}</button>${c.parents.map((p) => html`<button class="gsha mute" title="Open the parent commit" onClick=${() => open(p)}>parent ${p.slice(0, 7)}</button>`)}<${Refs} refs=${c.refs}/></div>
    </div>
    <div class="dfh"><b>${c.files.length}</b> file${c.files.length === 1 ? "" : "s"} in this folder <span class="mute"> · <b class="good">+${add}</b> <b class="bad">−${del}</b>${c.files_more ? " · list cut" : ""}</span></div>
    ${c.files.length ? c.files.map((x) => html`<div class="gfile"><button type="button" class=${"dfr" + (f === x.path ? " on" : "")} onClick=${() => setF(f === x.path ? null : x.path)}>${Ic(f === x.path ? "down" : "right", 12)}${stTag(x.status)}<span class="t"><b>${baseName(x.path)}</b>${dirName(x.path) && html`<span class="mute"> ${dirName(x.path)}</span>`}</span>${counts(x)}</button>
      ${f === x.path && html`<div class="gdiff"><${CommitFile} id=${id} api=${api} sha=${c.sha} path=${x.path} mode=${mode} ws=${ws}/></div>`}</div>`) : html`<p class="mute small" style="padding:10px">This commit changed nothing in this folder.</p>`}</div></div>`;
}
const LOG_N = 50;
export function GitView({ id, api, rev, file, onOpenDiff }) {
  const [g, setG] = useState(null), [sec, setS] = useState(() => pref("gitsec", "changes")), [ref, setRef] = useState(""), [only, setOnly] = useState(false), [q, setQ] = useState(""), [qq, setQq] = useState("");
  const [log, setLog] = useState(null), [sel, setSel] = useState(null), [tick, setTick] = useState(0);
  const setSec = (v) => { setS(v); setPref("gitsec", v); setSel(null); };
  useEffect(() => { let on = true; api("git/info?id=" + id).then((r) => on && setG(r)).catch((e) => on && setG({ err: e.message })); return () => { on = false; }; }, [id, rev, tick]);
  useEffect(() => { const t = setTimeout(() => setQq(q.trim()), 300); return () => clearTimeout(t); }, [q]);
  const logUrl = (skip) => `git/log?id=${id}&n=${LOG_N}&skip=${skip}${ref ? "&rev=" + enc(ref) : ""}${only && file ? "&path=" + enc(file) : ""}${qq ? "&q=" + enc(qq) : ""}`;
  useEffect(() => { if (sec !== "history" || !g || !g.repo) return; let on = true; setLog(null); api(logUrl(0)).then((r) => on && setLog(r)).catch((e) => on && setLog({ err: e.message })); return () => { on = false; }; }, [id, sec, ref, only, file, qq, g && g.head && g.head.sha, tick]);
  const loadMore = guard(async () => { const r = await api(logUrl(log.commits.length)); setLog({ commits: [...log.commits, ...r.commits], more: r.more }); });
  if (!g) return html`<div class="gitv"><div class="empty"><div class="shine">Reading the repository…</div></div></div>`;
  if (g.err) return html`<div class="gitv">${msg("alert", g.err)}</div>`;
  if (!g.repo) return html`<div class="gitv">${msg("branch", html`This folder is not in a git repository.<br/><span class="mute small">Ask Claude to run <code>git init</code> to start tracking it.</span>`)}</div>`;
  if (sel) return html`<${CommitView} id=${id} api=${api} sha=${sel} back=${() => setSel(null)} open=${setSel}/>`;
  const n = g.counts || {}, changed = g.status || [];
  const stOf = (x) => x.conflict ? "conflict" : x.untracked ? "untracked" : x.x === "A" ? "added" : x.x === "D" || x.y === "D" ? "deleted" : "modified";
  const where = (x) => x.conflict ? "conflict" : x.untracked ? "untracked" : [x.x !== "." && "staged", x.y !== "." && "not staged"].filter(Boolean).join(" + ");
  const header = html`<div class="ghead">
    <div class="row" style="gap:8px;flex-wrap:wrap">${Ic("branch", 15)}<b class="gbr">${g.branch || (g.detached ? "detached HEAD" : "no branch")}</b>
      ${g.upstream && html`<span class="mute small" title="Upstream branch">⇄ ${g.upstream}</span>`}${g.ahead ? html`<span class="tag" title="Commits not pushed yet">↑${g.ahead}</span>` : ""}${g.behind ? html`<span class="tag" title="Commits on the upstream not pulled yet (as of the last fetch)">↓${g.behind}</span>` : ""}
      <span class="grow"></span><button class="ibtn sm" title="Refresh" aria-label="Refresh" onClick=${() => setTick(tick + 1)}>${Ic("refresh", 14)}</button></div>
    ${g.head ? html`<div class="gline"><button class="gsha" title="Open this commit" onClick=${() => setSel(g.head.sha)}>${g.head.short}</button><span class="t">${g.head.subject || ""}</span>${g.head.date ? html`<span class="mute small">${ago(g.head.date)}</span>` : ""}</div>` : html`<div class="mute small">No commit yet.</div>`}
    <div class="gchips">${n.staged ? html`<span class="tag good">${n.staged} staged</span>` : ""}${n.unstaged ? html`<span class="tag">${n.unstaged} modified</span>` : ""}${n.untracked ? html`<span class="tag">${n.untracked} untracked</span>` : ""}${n.conflicts ? html`<span class="tag bad">${n.conflicts} conflict${n.conflicts > 1 ? "s" : ""}</span>` : ""}${!changed.length ? html`<span class="tag good">${Ic("check", 11)} clean</span>` : ""}
      ${g.stashes ? html`<span class="mute small">${g.stashes} stash${g.stashes > 1 ? "es" : ""}</span>` : ""}${g.tags ? html`<span class="mute small">${g.tags} tag${g.tags > 1 ? "s" : ""}</span>` : ""}${g.prefix && html`<span class="mute small" title=${g.top}>in ${g.prefix}/</span>`}</div>
    ${g.remotes.length ? html`<div class="mute small gline">${g.remotes.map((r) => html`<span title=${r.url}>${r.name}: <span class="mono">${r.url}</span></span>`)}</div>` : ""}</div>`;
  const tabs = html`<div class="dbar"><${Seg} small value=${sec} onChange=${setSec} items=${[{ id: "changes", label: "Changes" + (changed.length ? " " + changed.length : "") }, { id: "history", label: "History" }, { id: "branches", label: "Branches " + g.branches.length }]}/></div>`;
  let body;
  if (sec === "changes") body = changed.length ? html`<div class="dfl">${changed.map((x) => html`<button type="button" class="dfr" title=${"Show the diff since the last commit · " + where(x)} onClick=${() => onOpenDiff(x.path)}>${stTag(stOf(x))}<span class="t"><b>${baseName(x.path)}</b>${dirName(x.path) && html`<span class="mute"> ${dirName(x.path)}</span>`}</span><span class="mute small">${where(x)}</span></button>`)}${g.status_more ? html`<p class="mute small" style="padding:8px 12px">More files changed than are listed here.</p>` : ""}</div>`
    : msg("check", "Nothing changed since the last commit.");
  else if (sec === "branches") body = html`<div class="dfl">${g.branches.map((b) => html`<button type="button" class=${"dfr" + (b.current ? " on" : "")} title="Show the history of this branch" onClick=${() => { setRef(b.current ? "" : b.name); setSec("history"); }}>${Ic("branch", 13)}<span class="t"><b>${b.name}</b>${b.current ? html` <span class="tag good">current</span>` : ""}<span class="mute"> ${b.subject}</span></span><span class="mute small">${b.upstream ? b.upstream + " · " : ""}${ago(b.date)}</span></button>`)}</div>`;
  else body = html`<div class="gfilt"><input class="fq grow" placeholder="Search commit messages" value=${q} onInput=${(e) => setQ(e.target.value)}/>
      <select value=${ref} title="Branch" onChange=${(e) => setRef(e.target.value)}><option value="">${g.branch || "HEAD"}${g.branch ? " (current)" : ""}</option>${g.branches.filter((b) => !b.current).map((b) => html`<option value=${b.name}>${b.name}</option>`)}</select>
      ${file && html`<button class=${"btn" + (only ? " on" : "")} title=${"Only commits that changed " + file} aria-pressed=${only} onClick=${() => setOnly(!only)}>${Ic("file", 13)} ${baseName(file)}</button>`}</div>
    ${!log ? html`<div class="shine small" style="padding:12px">Loading history…</div>` : log.err ? html`<p class="mute small" style="padding:12px">${log.err}</p>` : !log.commits.length ? html`<p class="mute small" style="padding:12px">No commits${qq ? " match" : ""}.</p>`
      : html`<div class="glog">${log.commits.map((c) => html`<button type="button" class="gc" onClick=${() => setSel(c.sha)}><span class="gsha">${c.short}</span><span class="t"><span class="s">${c.subject}</span> <${Refs} refs=${c.refs}/></span><span class="mute small" title=${when(c.date)}>${c.author} · ${ago(c.date)}</span></button>`)}
        ${log.more && html`<button class="btn" style="margin:10px auto;display:flex" onClick=${loadMore}>Load more</button>`}</div>`}`;
  return html`<div class="gitv">${header}${tabs}<div class="gscroll">${body}</div></div>`;
}
