// Share a file of a chat (or a folder with index.html) to an AI Hub Space.
import { ask, toast } from "./ui.js";

const KINDS = [["auto", "Detect from the file"], ["slides", "Slides"], ["report", "Report"], ["doc", "Document"], ["dashboard", "Dashboard"], ["html", "Web page"], ["pdf", "PDF"], ["sheet", "Spreadsheet"]];
const slug = (s) => s.toLowerCase().replace(/\.[a-z0-9]+$/, "").replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+|-+$/g, "").slice(0, 64);

// path = file inside the chat folder, or a folder holding index.html (a bundle). -> the space, or null when cancelled.
export async function shareToSpace(post, chatId, path, { bundle = false, signedIn = true } = {}) {
  if (!signedIn) { toast("Sign in to your hub first (Settings > Hub account)"); return null; }
  const last = path.replace(/\/index\.html$/i, "").split("/").pop() || "site";
  const v = await ask({ title: bundle ? "Share this folder to a Space" : "Share to a Space", text: bundle ? "The folder is published as a web page (it must have index.html)." : path,
    submit: "Share", fields: [
      { name: "title", label: "Title", value: last.replace(/[-_]+/g, " "), max: 200, required: true },
      { name: "name", label: "Space name", value: slug(last), help: "Lower case letters, digits and . _ - . Sharing the same name again publishes a new version.", max: 64 },
      { name: "kind", label: "Kind", type: "select", value: bundle ? "html" : "auto", options: KINDS.map(([value, label]) => ({ value, label })) },
      { name: "visibility", label: "Who can open it", type: "select", value: "private", options: [{ value: "private", label: "Only me (share with people or groups later)" }, { value: "public", label: "Anyone with the link" }] }] });
  if (!v) return null;
  toast("Uploading…");
  const r = await post("spaces/publish", { id: chatId, path: bundle ? path.replace(/\/?index\.html$/i, "") || "." : path, title: v.title.trim(), name: v.name.trim(), kind: v.kind, visibility: v.visibility });
  toast((r.created ? "Shared as " : "Updated ") + (r.space.ref || "a Space"));
  return r;
}
