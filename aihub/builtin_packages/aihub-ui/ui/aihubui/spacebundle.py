"""Turn a chat's HTML page into ONE self-contained page a Space can show.

A Space serves HTML with `connect-src 'none'` (no fetch/XHR at all) and only what was uploaded, so a page that loads
`data/sales.json`, `style.css` or `logo.png` from its folder breaks there. `bundle()` inlines what the page refers to:
local <script src> -> inline script, <link rel=stylesheet> -> <style> (its url()s as data URIs), src/poster/icon attributes
and CSS url()s -> data URIs, and every local data file named in a string literal (fetch("x.json"), d3.csv('a.csv')...)
goes into an embedded map that a small shim serves to fetch() and XMLHttpRequest. Only files inside `root` (the chat
folder) are read; anything outside, missing or too big is left as it was and reported. Stdlib only."""
import base64
import json
import mimetypes
import os
import re
from urllib.parse import unquote

FILE_MAX = 15 * 1024 * 1024        # one inlined file
TOTAL_MAX = 40 * 1024 * 1024       # everything inlined into one page
DATA_EXT = ("json", "geojson", "topojson", "csv", "tsv", "txt", "xml", "md", "yaml", "yml", "svg", "png", "jpg", "jpeg", "gif", "webp",
            "wasm", "parquet", "arrow", "bin", "glb", "gltf")
_REMOTE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", re.I)
_SCRIPT = re.compile(r"<script\b([^>]*?)\bsrc\s*=\s*([\"'])([^\"']+)\2([^>]*)>\s*</script\s*>", re.I)
_LINK = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = re.compile(r"(\b(?:src|poster|data-src)\s*=\s*)([\"'])([^\"']+)\2", re.I)
_URL = re.compile(r"url\(\s*([\"']?)([^\"')]+)\1\s*\)", re.I)
_LIT = re.compile(r"([\"'`])((?:\.{0,2}/)?[^\"'`\s<>(){}]{1,300}\.(?:%s))(?:[?#][^\"'`\s]*)?\1" % "|".join(DATA_EXT), re.I)

SHIM = """<script>/* aihub bundle: the page's data files, served to fetch/XHR without any network */(function(){
var F=%s;function k(u){u=String(u||"");var r=u.split(/[?#]/)[0].replace(/^\\.\\//,"");try{r=decodeURIComponent(r)}catch(e){}if(F[r])return r;
try{var p=decodeURIComponent(new URL(u,document.baseURI).pathname);for(var n in F)if(p===n||p.slice(-n.length-1)==="/"+n)return n}catch(e){}return null}
function b(n){var s=atob(F[n][1]),a=new Uint8Array(s.length);for(var i=0;i<s.length;i++)a[i]=s.charCodeAt(i);return a}
function t(n){return new TextDecoder().decode(b(n))}
var of=window.fetch;window.fetch=function(i,o){var n=k(typeof i==="string"?i:i&&i.url);if(!n)return of.apply(this,arguments);
return Promise.resolve(new Response(b(n),{status:200,headers:{"Content-Type":F[n][0]}}))};
var X=XMLHttpRequest.prototype,oo=X.open,os=X.send;X.open=function(m,u){this.__k=k(u);return oo.apply(this,arguments)};
X.send=function(){var n=this.__k,x=this;if(!n)return os.apply(this,arguments);setTimeout(function(){var rt=x.responseType,v=rt==="json"?JSON.parse(t(n)):rt==="arraybuffer"?b(n).buffer:rt==="blob"?new Blob([b(n)],{type:F[n][0]}):t(n);
var d=function(p,v){Object.defineProperty(x,p,{value:v,configurable:true})};d("readyState",4);d("status",200);d("statusText","OK");d("response",v);if(!rt||rt==="text")d("responseText",v);
d("getResponseHeader",function(h){return /content-type/i.test(h)?F[n][0]:null});x.dispatchEvent(new Event("readystatechange"));x.dispatchEvent(new Event("load"));x.dispatchEvent(new Event("loadend"))},0)};
})();</script>"""


class _Ctx:
    def __init__(self, root):
        self.root = os.path.realpath(root)
        self.used, self.skipped, self.size = [], [], 0

    def local(self, ref, base):
        """-> absolute path of a local file a page refers to, or None (remote, outside the root, missing, too big)."""
        ref = (ref or "").strip()
        if not ref or _REMOTE.match(ref) or "{" in ref or "$" in ref:
            return None
        rel = unquote(re.split(r"[?#]", ref)[0])
        full = os.path.realpath(os.path.join(self.root if rel.startswith("/") else base, rel.lstrip("/")))
        if not (full == self.root or full.startswith(self.root + os.sep)) or not os.path.isfile(full):
            return None
        if ".git" in os.path.relpath(full, self.root).split(os.sep):
            return None
        n = os.path.getsize(full)
        if n > FILE_MAX or self.size + n > TOTAL_MAX:
            self.skipped.append(os.path.relpath(full, self.root).replace(os.sep, "/"))
            return None
        self.size += n
        r = os.path.relpath(full, self.root).replace(os.sep, "/")
        if r not in self.used:
            self.used.append(r)
        return full


def _read(p):
    with open(p, "rb") as f:
        return f.read()


def _mime(p):
    m = mimetypes.guess_type(p)[0]
    ext = p.rsplit(".", 1)[-1].lower()
    return m or {"json": "application/json", "geojson": "application/geo+json", "csv": "text/csv", "tsv": "text/tab-separated-values",
                 "md": "text/markdown", "yaml": "text/yaml", "yml": "text/yaml", "wasm": "application/wasm", "svg": "image/svg+xml"}.get(ext, "application/octet-stream")


def _data_uri(p):
    return "data:%s;base64,%s" % (_mime(p), base64.b64encode(_read(p)).decode("ascii"))


def _css(text, base, ctx):
    def one(m):
        p = ctx.local(m.group(2), base)
        return "url(%s)" % _data_uri(p) if p else m.group(0)
    return _URL.sub(one, text)


def bundle(path, root):
    """-> (html text, files inlined [paths relative to root], files skipped). `path` = an HTML file inside `root`."""
    ctx = _Ctx(root)
    base = os.path.dirname(os.path.realpath(path))
    html = _read(path).decode("utf-8", "replace")

    def script(m):
        p = ctx.local(m.group(3), base)
        if not p:
            return m.group(0)
        code = _read(p).decode("utf-8", "replace").replace("</script", "<\\/script")
        return "<script%s%s>%s</script>" % (m.group(1).rstrip() and " " + m.group(1).strip(), m.group(4).rstrip(), code)
    html = _SCRIPT.sub(script, html)

    def link(m):
        tag = m.group(0)
        href = re.search(r"\bhref\s*=\s*([\"'])([^\"']+)\1", tag, re.I)
        rel = (re.search(r"\brel\s*=\s*([\"']?)([^\"'\s>]+)", tag, re.I) or [None, "", ""])[2].lower()
        if not href:
            return tag
        p = ctx.local(href.group(2), base)
        if not p:
            return tag
        if rel == "stylesheet":
            return "<style>%s</style>" % _css(_read(p).decode("utf-8", "replace"), os.path.dirname(p), ctx).replace("</style", "<\\/style")
        if "icon" in rel or rel in ("preload", "image_src"):
            return tag.replace(href.group(0), 'href="%s"' % _data_uri(p))
        return tag
    html = _LINK.sub(link, html)

    def attr(m):
        p = ctx.local(m.group(3), base)
        return '%s"%s"' % (m.group(1), _data_uri(p)) if p else m.group(0)
    html = _ATTR.sub(attr, html)
    html = re.sub(r"(<style\b[^>]*>)(.*?)(</style>)", lambda m: m.group(1) + _css(m.group(2), base, ctx) + m.group(3), html, flags=re.I | re.S)
    html = re.sub(r"(\bstyle\s*=\s*\")([^\"]*url\([^\"]*)(\")", lambda m: m.group(1) + _css(m.group(2), base, ctx).replace('"', "'") + m.group(3), html, flags=re.I)

    files = {}
    for m in _LIT.finditer(html):                                   # data the scripts load at run time (now incl. the inlined ones)
        ref = m.group(2)
        if ref in files:
            continue
        p = ctx.local(ref, base)
        if p and p != os.path.realpath(path):
            files[re.sub(r"^\./", "", ref)] = [_mime(p), base64.b64encode(_read(p)).decode("ascii")]
    if files:
        shim = SHIM % json.dumps(files, separators=(",", ":")).replace("</", "<\\/")
        h = re.search(r"<head\b[^>]*>", html, re.I)
        html = html[:h.end()] + shim + html[h.end():] if h else shim + html
    return html, ctx.used, sorted(set(ctx.skipped))

