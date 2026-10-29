// Share a file of a chat (or a folder with index.html) to an AI Hub Space.
import { ask, toast } from "./ui.js";

const KINDS = [["auto", "Detect from the file"], ["slides", "Slides"], ["report", "Report"], ["doc", "Document"], ["dashboard", "Dashboard"], ["html", "Web page"], ["pdf", "PDF"], ["sheet", "Spreadsheet"]];
const slug = (s) => s.toLowerCase().replace(/\.[a-z0-9]+$/, "").replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+|-+$/g, "").slice(0, 64);

// path = file inside the chat folder, or a folder (a site; index.html or its one page), or with `content` a chat block saved as
// shared/<path>. Local files a page loads (data, css, js, images) are bundled into it by the server. -> the space, or null when cancelled.
export async function shareToSpace(post, chatId, path, { bundle = false, signedIn = true, content = null } = {}) {
  if (!signedIn) { toast("Sign in to your hub first (Settings > Hub account)"); return null; }
  const last = path.replace(/\/index\.html$/i, "").split("/").pop() || "site";
  const v = await ask({ title: bundle ? "Share this folder to a Space" : "Share to a Space", text: bundle ? "The folder is published as a web page (index.html, or its only page). Data files the pages load are bundled in." : content != null ? "This block is saved as shared/" + path + " in the chat folder and published." : path,
    submit: "Share", fields: [
      { name: "title", label: "Title", value: last.replace(/[-_]+/g, " "), max: 200, required: true },
      { name: "name", label: "Space name", value: slug(last), help: "Lower case letters, digits and . _ - . Sharing the same name again publishes a new version.", max: 64 },
      { name: "kind", label: "Kind", type: "select", value: bundle ? "html" : "auto", options: KINDS.map(([value, label]) => ({ value, label })) },
      { name: "visibility", label: "Who can open it", type: "select", value: "private", options: [{ value: "private", label: "Only me (share with people or groups later)" }, { value: "public", label: "Anyone with the link" }] }] });
  if (!v) return null;
  toast("Uploading…");
  const where = content != null ? { content, filename: path } : { path: bundle ? path.replace(/\/?index\.html$/i, "") || "." : path };
  const r = await post("spaces/publish", { id: chatId, ...where, title: v.title.trim(), name: v.name.trim(), kind: v.kind, visibility: v.visibility });
  const n = (r.bundled || []).length, sk = (r.skipped || []).length;
  toast((r.created ? "Shared as " : "Updated ") + (r.space.ref || "a Space") + (n ? " · " + n + " file" + (n > 1 ? "s" : "") + " bundled in" : "") + (sk ? " · " + sk + " too big, left out" : ""));
  return r;
}
