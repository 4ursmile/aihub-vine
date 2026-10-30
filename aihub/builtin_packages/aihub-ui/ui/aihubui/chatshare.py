"""Export, share and import a chat. stdlib only.

A chat leaves as ONE bundle (`format = "aihub-chat"`): title, settings, the stored history rows and, if asked, the small text files of its
working folder. Two carriers hold the same bundle:
  * `.aihubchat.json` - the bundle itself;
  * `.html`           - a readable transcript page that carries the bundle in `<script type="application/json" id="aihub-chat">`.
The page is what goes to the hub: a Space of kind `html`/`report` (so the hub's own viewer, visibility, sharing and view settings apply),
and importing reads the bundle back out of it. Imported text is DATA: it is never executed, and files are written only inside the new chat's folder.
"""
import base64
import html
import json
import os
import re
import time

FORMAT = "aihub-chat"
VERSION = 1
TAG = "aihub-chat"
MAX_BYTES = 40 * 1024 * 1024                    # whole bundle, as text
FILE_MAX = 512 * 1024                           # one working-folder file
FILES_MAX = 4 * 1024 * 1024                     # all of them together
FILES_COUNT = 200
TEXT_EXT = {".md", ".txt", ".py", ".js", ".ts", ".json", ".html", ".css", ".csv", ".svg", ".yml", ".yaml", ".toml", ".sh", ".sql", ".xml", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h", ".cpp"}
KEEP = ("model", "effort", "mode", "engine")
REF = re.compile(r"([^@\s/#?]+@[a-z0-9][a-z0-9._-]{0,63})", re.I)
BLOCK = re.compile(r'<script[^>]*\bid="aihub-chat"[^>]*>(.*?)</script>', re.S | re.I)


def build(s, rows, folder=None, tree=None):
    """Bundle of chat S (a store session) with its history ROWS (dicts). FOLDER + TREE = include the small text files of the working folder."""
    b = {"format": FORMAT, "version": VERSION, "exported": int(time.time()), "title": s.get("title") or "Chat",
         "settings": {k: s.get(k) or "" for k in KEEP}, "soul": s.get("soul"), "rows": rows, "files": []}
    if folder and tree:
        total = 0
        for rel in tree:
            ext = os.path.splitext(rel)[1].lower()
            full = os.path.join(folder, rel)
            if ext not in TEXT_EXT or len(b["files"]) >= FILES_COUNT or ".git" in rel.split("/") or os.path.islink(full):
                continue
            try:
                if os.path.getsize(full) > FILE_MAX or total + os.path.getsize(full) > FILES_MAX:
                    continue
                with open(full, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            total += len(data)
            b["files"].append({"path": rel, "b64": base64.b64encode(data).decode("ascii")})
    return b


def to_json(b):
    return json.dumps(b, ensure_ascii=False, indent=1)


def _texts(row):
    """(role, text) of a stored row, or None: only what a reader of the page wants to see."""
    k = row.get("k")
    if k == "user":
        return "You", str(row.get("text", ""))
    if k == "msg":
        bl = [x for x in row.get("blocks") or [] if isinstance(x, dict)]
        t = "\n".join(x.get("text", "") for x in bl if x.get("type") == "text")
        tools = sorted({str(x.get("name")) for x in bl if x.get("type") == "tool"})
        if t or tools:
            return "Assistant", t + ("\n\n[tools: %s]" % ", ".join(tools) if tools else "")
    return None


def to_html(b):
    """Readable, script-free page of the bundle (all text escaped) that also carries the bundle for a later import."""
    items = []
    for row in b.get("rows") or []:
        if isinstance(row, dict):
            r = _texts(row)
            if r:
                who, text = r
                items.append('<section class="%s"><h2>%s</h2><div>%s</div></section>' % ("me" if who == "You" else "ai", who, html.escape(text)))
    payload = json.dumps(b, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    nf = len(b.get("files") or [])
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s</title><style>:root{color-scheme:light dark;--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--card:#f5f5f7;--acc:#d97757}"
            "@media(prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#f5f5f7;--mut:#98989d;--card:#2c2c2e}}"
            "body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,system-ui,sans-serif}main{max-width:760px;margin:0 auto;padding:32px 20px 64px}"
            "h1{font-size:24px;margin:0 0 4px}p.m{color:var(--mut);margin:0 0 24px;font-size:14px}section{margin:0 0 16px;padding:14px 16px;border-radius:14px;background:var(--card)}"
            "section.me{border-left:3px solid var(--acc)}h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 6px}"
            "section div{white-space:pre-wrap;word-wrap:break-word}</style></head><body><main><h1>%s</h1><p class=\"m\">%d messages%s &middot; "
            "shared from AI Hub UI &middot; import it with Chats &rarr; Import</p>%s</main>"
            "<script type=\"application/json\" id=\"aihub-chat\">%s</script></body></html>"
            % (html.escape(b.get("title") or "Chat"), html.escape(b.get("title") or "Chat"), len(items), (", %d files" % nf) if nf else "", "\n".join(items), payload))


def parse(data):
    """Bundle from a pasted/uploaded `.json` or the shared `.html` page. ValueError with a plain message when it is neither."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    if len(data) > MAX_BYTES:
        raise ValueError("too big to be a chat (over 40 MB)")
    t = data.strip()
    if not t.startswith("{"):
        m = BLOCK.search(t)
        if not m:
            raise ValueError("this is not an exported chat (no chat data found)")
        t = m.group(1).strip()
    try:
        b = json.loads(t)
    except ValueError:
        raise ValueError("the chat data is damaged")
    if not isinstance(b, dict) or b.get("format") != FORMAT:
        raise ValueError("this is not an exported chat")
    if int(b.get("version") or 0) > VERSION:
        raise ValueError("made by a newer AI Hub UI: update it first")
    rows = [r for r in b.get("rows") or [] if isinstance(r, dict)]
    files = [f for f in b.get("files") or [] if isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("b64"), str)]
    return {"title": str(b.get("title") or "Imported chat")[:120], "settings": b.get("settings") if isinstance(b.get("settings"), dict) else {},
            "soul": b.get("soul") if isinstance(b.get("soul"), str) else None, "rows": rows, "files": files[:FILES_COUNT]}


def ref_of(text):
    """`author@space` from a pasted hub link (.../#/s/author@space?..) or the bare reference, else None."""
    t = (text or "").strip()
    if not t or "\n" in t or len(t) > 400:
        return None
    if t.lower().startswith("http"):
        if "#/s/" not in t:
            return None
        from urllib.parse import unquote
        t = unquote(t.split("#/s/")[-1].split("?")[0])
    m = REF.fullmatch(t)
    return m.group(1) if m else None
