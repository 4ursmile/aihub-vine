// Export a chat to a file, share it on the hub (as a Space), and import one: from a file, a pasted link or text, or the chats shared on the hub.
import { html, useState } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { ask, toast, Modal, Seg } from "./ui.js";

const save = (name, text, type) => {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
};
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+|-+$/g, "").slice(0, 64);

// One dialog for both ways out. get/post = the page's api helpers. -> the shared space, or null.
export async function sendChat({ get, post, id, title, signedIn }) {
  const v = await ask({ title: "Share or export this chat", text: "The conversation (and, if you tick it, the small text files of its working folder) is packed. Whoever imports it gets a new chat that knows the conversation.", submit: "Continue", fields: [
    { name: "how", label: "What to do", type: "select", value: "file", options: [{ value: "file", label: "Save as a file (.aihubchat.json)" }, { value: "page", label: "Save as a readable web page (.html, can be imported too)" }, { value: "hub", label: "Share on the hub as a Space" }] },
    { name: "files", label: "Working folder files", type: "select", value: "0", options: [{ value: "0", label: "Conversation only" }, { value: "1", label: "Include small text files (code, notes, up to 4 MB)" }] }] });
  if (!v) return null;
  if (v.how !== "hub") {
    const r = await get("sessions/export?id=" + id + "&format=" + (v.how === "page" ? "html" : "json") + "&files=" + v.files);
    save(r.filename, r.text, v.how === "page" ? "text/html" : "application/json");
    toast("Saved " + r.filename + " · " + r.messages + " rows" + (r.files ? " · " + r.files + " files" : ""));
    return null;
  }
  if (!signedIn) { toast("Sign in to your hub first (Settings > Hub account)"); return null; }
  const s = await ask({ title: "Share on the hub", text: "Published as a Space. Anyone who can open it can import it as a new chat; you can change the page kind, viewer size and who sees it later with the Space's own settings.", submit: "Share", fields: [
    { name: "title", label: "Title", value: title, max: 200, required: true },
    { name: "name", label: "Space name", value: slug(title), max: 64, help: "Sharing the same name again publishes a new version." },
    { name: "kind", label: "Page kind", type: "select", value: "report", options: [{ value: "report", label: "Report (reading page)" }, { value: "dashboard", label: "Dashboard" }, { value: "html", label: "Web page" }] },
    { name: "view", label: "Viewer size", type: "select", value: "fit", options: [{ value: "canvas", label: "Canvas (inside the page)" }, { value: "fit", label: "Fit (fills the area)" }, { value: "full", label: "Full window" }] },
    { name: "visibility", label: "Who can open it", type: "select", value: "private", options: [{ value: "private", label: "Only me (share with people or groups later)" }, { value: "public", label: "Anyone with the link" }] }] });
  if (!s) return null;
  toast("Uploading…");
  const r = await post("sessions/share", { id, title: s.title.trim(), name: s.name.trim(), kind: s.kind, view: s.view, visibility: s.visibility, files: v.files === "1" });
  if (r.url) { try { await navigator.clipboard.writeText(r.url); } catch (e) { /* no clipboard */ } }
  toast((r.created ? "Shared as " : "Updated ") + (r.space.ref || "a Space") + (r.url ? " · link copied" : ""));
  return r;
}

// Three ways in: a hub link (or author@name), pasted plain text (the exported .json or .html), or a file (picked or dropped).
// Chats shared on the hub are found in Search with @hub:.
export function ImportDialog({ get, post, close, done, extra, initial }) {
  const [tab, setTab] = useState("link"), [link, setLink] = useState(initial || ""), [text, setText] = useState(""), [busy, setBusy] = useState(false), [err, setErr] = useState(""), [over, setOver] = useState(false), [fname, setFname] = useState("");
  const go = async (body) => {
    setBusy(true); setErr("");
    try { const r = await post("sessions/import", { ...body, extra }); toast("Imported " + r.messages + " messages" + (r.files ? " and " + r.files + " files" : "")); done(r.session); }
    catch (e) { setErr(e.message); setBusy(false); }
  };
  const file = (f) => {
    if (!f) return; setFname(f.name);
    if (f.size > 40 * 1024 * 1024) return setErr("That file is over 40 MB.");
    const rd = new FileReader(); rd.onload = () => go({ text: String(rd.result) }); rd.onerror = () => setErr("Could not read the file."); rd.readAsText(f);
  };
  const drop = (e) => { e.preventDefault(); setOver(false); file(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]); };
  const pasteLink = async () => { try { const t = await navigator.clipboard.readText(); if (t) setLink(t.trim()); } catch (e) { setErr("The browser did not allow reading the clipboard; paste with Ctrl/Cmd+V."); } };
  return html`<${Modal} close=${close} wide=${true}><h1>Import a chat</h1><p class="sub">Makes a NEW chat from a shared one. Nothing in it is run; the conversation is only read.</p>
    <${Seg} items=${[{ id: "link", label: "Paste a link", icon: "link" }, { id: "text", label: "Paste text", icon: "note" }, { id: "file", label: "Upload a file", icon: "upload" }]} value=${tab} onChange=${(t) => { setTab(t); setErr(""); }}/>
    <div style="margin-top:14px">
      ${tab === "link" && html`<div class="field"><label>Hub link or author@name</label><div class="row"><input class="grow" autofocus value=${link} onInput=${(e) => setLink(e.target.value)} onKeyDown=${(e) => { if (e.key === "Enter" && link.trim() && !busy) go({ link: link.trim() }); }} placeholder="https://hub/#/s/alice@design-review"/><button class="btn" title="Paste from the clipboard" onClick=${pasteLink}>${Ic("clip", 14)} Paste</button></div></div>
        <div class="row"><span class="mute small grow">Tip: in Search, type <code>@hub:</code> to find chats shared on the hub.</span><button class="btn acc" disabled=${busy || !link.trim()} onClick=${() => go({ link: link.trim() })}>${busy ? "Importing…" : "Import"}</button></div>`}
      ${tab === "text" && html`<div class="field"><label>The exported chat: .aihubchat.json content or the shared .html page source</label><textarea rows="9" autofocus value=${text} onInput=${(e) => setText(e.target.value)} placeholder='{"format": "aihub-chat", ...}' style="width:100%;font:12px var(--mono)"></textarea></div>
        <div class="row"><span class="mute small grow">${text ? Math.round(text.length / 1024) + " KB" : ""}</span><button class="btn acc" disabled=${busy || !text.trim()} onClick=${() => go({ text })}>${busy ? "Importing…" : "Import"}</button></div>`}
      ${tab === "file" && html`<label class=${"imp-drop" + (over ? " over" : "")} onDragOver=${(e) => { e.preventDefault(); setOver(true); }} onDragLeave=${() => setOver(false)} onDrop=${drop}>
        ${Ic("upload", 22)}<b>${busy ? "Importing " + fname + "…" : fname || "Drop a file here or click to choose"}</b><span class="mute small">An .aihubchat.json or a shared .html page, up to 40 MB</span>
        <input type="file" accept=".json,.html,.htm,application/json,text/html" onChange=${(e) => file(e.target.files && e.target.files[0])} disabled=${busy} hidden/></label>`}
    </div>
    ${err && html`<p class="err" role="alert" style="color:var(--bad,#c0392b);margin-top:10px">${err}</p>`}
    <div class="row" style="margin-top:14px"><span class="grow"></span><button class="btn" onClick=${close}>Close</button></div><//>`;
}
