"""Canvas preview of Office files: .docx, .xlsx, .pptx -> ONE self-contained HTML page (stdlib only, no network).

`render(path)` returns (html, title) or raises ValueError. The page is served like any chat file (sandboxed, opaque origin), so images are
data URIs and the only script is the tiny slide scaler. Limits keep a huge file from hanging the UI."""
import base64
import html as H
import io
import mimetypes
import os
import re
import threading
import zipfile
import xml.etree.ElementTree as ET

EXT = (".docx", ".xlsx", ".pptx")
MAX_FILE = 80 * 1024 * 1024
MAX_UNZIPPED = 400 * 1024 * 1024
MAX_SLIDES = 300
MAX_ROWS = 2000
MAX_COLS = 60
MAX_IMG = 8 * 1024 * 1024
MAX_IMG_TOTAL = 48 * 1024 * 1024

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
RN = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
EMU = 9525.0                                   # EMU per CSS px at 96 dpi
SCHEME = {"tx1": "dk1", "bg1": "lt1", "tx2": "dk2", "bg2": "lt2"}
IMG_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml", "image/bmp")

PAGE = ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>%s</title><style>"
        ":root{color-scheme:light dark}body{margin:0;font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;background:#f4f4f6;color:#1d1d1f}"
        "@media(prefers-color-scheme:dark){body{background:#1c1c1e;color:#eee}.doc,.sheet table{background:#2a2a2d!important;color:#eee}td,th{border-color:#4a4a4e!important}}"
        "%s</style></head><body>%s%s</body></html>")


def _zip(path):
    if os.path.getsize(path) > MAX_FILE:
        raise ValueError("file too large to preview (max %d MB)" % (MAX_FILE >> 20))
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError("not a valid Office file")
    if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
        z.close()
        raise ValueError("file too large to preview")
    return z


def _xml(z, name):
    try:
        return ET.fromstring(z.read(name))
    except (KeyError, ET.ParseError):
        raise ValueError("damaged Office file (%s)" % name)


def _rels(z, part):
    d, f = os.path.split(part)
    name = "%s/_rels/%s.rels" % (d, f)
    if name not in z.namelist():
        return {}
    out = {}
    for r in ET.fromstring(z.read(name)):
        t = r.get("Target") or ""
        if r.get("TargetMode") == "External":
            continue
        out[r.get("Id")] = t.lstrip("/") if t.startswith("/") else os.path.normpath(os.path.join(d, t)).replace("\\", "/")
    return out


def _img(z, name, cache, budget):
    if not name or name not in z.namelist():
        return ""
    if name in cache:
        return cache[name]
    size = z.getinfo(name).file_size
    mime = mimetypes.guess_type(name)[0] or ""
    if mime not in IMG_TYPES or size > MAX_IMG or budget[0] + size > MAX_IMG_TOTAL:
        return ""
    budget[0] += size
    cache[name] = "data:%s;base64,%s" % (mime, base64.b64encode(z.read(name)).decode())
    return cache[name]


# ---------------------------------------------------------------- docx

def docx(path):
    with _zip(path) as z:
        return _docx(z)


def _docx(z):
    body = _xml(z, "word/document.xml").find(W + "body")
    rels, cache, budget = _rels(z, "word/document.xml"), {}, [0]
    out, state = [], {"list": False}

    def runs(p):
        s = []
        for r in p.iter(W + "r"):
            t = ""
            for x in r:
                if x.tag == W + "t":
                    t += H.escape(x.text or "")
                elif x.tag == W + "tab":
                    t += "&emsp;"
                elif x.tag in (W + "br", W + "cr"):
                    t += "<br>"
                elif x.tag == W + "drawing":
                    b = x.find(".//" + A + "blip")
                    src = _img(z, rels.get(b.get(RN + "embed")) if b is not None else None, cache, budget)
                    if src:
                        t += "<img src='%s' alt=''>" % src
            if not t:
                continue
            pr = r.find(W + "rPr")
            if pr is not None:
                for tag, el in (("b", "b"), ("i", "i"), ("u", "u"), ("strike", "s")):
                    e = pr.find(W + tag)
                    if e is not None and e.get(W + "val") not in ("0", "false", "none"):
                        t = "<%s>%s</%s>" % (el, t, el)
                c = pr.find(W + "color")
                if c is not None and re.match(r"^[0-9A-Fa-f]{6}$", c.get(W + "val") or ""):
                    t = "<span style='color:#%s'>%s</span>" % (c.get(W + "val"), t)
            s.append(t)
        return "".join(s)

    def close():
        if state["list"]:
            out.append("</ul>")
            state["list"] = False

    def para(p):
        txt = runs(p)
        ppr = p.find(W + "pPr")
        style, listed, algn = "", False, ""
        if ppr is not None:
            st = ppr.find(W + "pStyle")
            style = (st.get(W + "val") if st is not None else "") or ""
            listed = ppr.find(W + "numPr") is not None
            jc = ppr.find(W + "jc")
            algn = {"center": "center", "right": "right", "both": "justify"}.get(jc.get(W + "val") if jc is not None else "", "")
        if listed and txt:
            if not state["list"]:
                out.append("<ul>")
                state["list"] = True
            out.append("<li>%s</li>" % txt)
            return
        close()
        if not txt.strip():
            return
        low = style.lower()
        m = re.match(r"heading\s*(\d)", low)
        tag = "h%d" % min(6, max(1, int(m.group(1)))) if m else "h1" if low == "title" else "p"
        out.append("<%s%s>%s</%s>" % (tag, " style='text-align:%s'" % algn if algn else "", txt, tag))

    for el in list(body) if body is not None else []:
        if el.tag == W + "p":
            para(el)
        elif el.tag == W + "tbl":
            close()
            rows = ["<tr>%s</tr>" % "".join("<td>%s</td>" % " ".join(runs(p) for p in tc.iter(W + "p")) for tc in tr.findall(W + "tc")) for tr in el.iter(W + "tr")]
            out.append("<div class='ts'><table>%s</table></div>" % "".join(rows))
    close()
    css = (".doc{max-width:820px;margin:24px auto;padding:48px 56px;background:#fff;color:#1d1d1f;box-shadow:0 1px 6px #0002;border-radius:4px}"
           ".doc img{max-width:100%;height:auto}.doc h1,.doc h2,.doc h3{line-height:1.25}.ts{overflow:auto}"
           "table{border-collapse:collapse;margin:12px 0}td,th{border:1px solid #c8c8cc;padding:4px 8px;vertical-align:top}")
    return PAGE % ("Document", css, "<div class='doc'>%s</div>" % ("".join(out) or "<p>(empty document)</p>"), "")


# ---------------------------------------------------------------- xlsx

def _col(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _letters(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx(path):
    with _zip(path) as z:
        return _xlsx(z)


def _rows(z, name):
    """Rows of one worksheet as they are read (iterparse: a huge sheet is not parsed past what is shown)."""
    try:
        with z.open(name) as f:
            for _ev, el in ET.iterparse(f, events=("end",)):
                if el.tag == M + "row":
                    yield el
                    el.clear()
    except (KeyError, ET.ParseError):
        raise ValueError("damaged Office file (%s)" % name)


def _xlsx(z):
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in _xml(z, "xl/sharedStrings.xml").findall(M + "si"):
            shared.append("".join(t.text or "" for t in si.iter(M + "t")))
    files = sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)), key=lambda n: int(re.findall(r"\d+", n)[-1]))
    if not files:
        raise ValueError("no worksheet found")
    names = {}
    try:
        rid = {r.get("Id"): "xl/" + r.get("Target").lstrip("/").replace("xl/", "", 1) for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        for sh in ET.fromstring(z.read("xl/workbook.xml")).iter(M + "sheet"):
            names[rid.get(sh.get(RN + "id"))] = sh.get("name")
    except (KeyError, ET.ParseError, AttributeError):
        pass
    tabs, panes = [], []
    for k, f in enumerate(files):
        rows, cut, width = [], False, 0
        for row in _rows(z, f):
            if len(rows) >= MAX_ROWS:
                cut = True
                break
            cells = []
            for c in row.findall(M + "c"):
                idx = _col(c.get("r", "A1"))
                if idx >= MAX_COLS:
                    continue
                while len(cells) < idx:
                    cells.append("")
                t = c.get("t")
                if t == "inlineStr":
                    v = "".join(x.text or "" for x in c.iter(M + "t"))
                else:
                    e = c.find(M + "v")
                    v = (e.text or "") if e is not None else ""
                    if t == "s" and v.isdigit() and int(v) < len(shared):
                        v = shared[int(v)]
                    elif t == "b":
                        v = "TRUE" if v == "1" else "FALSE"
                cells.append(v)
            width = max(width, len(cells))
            rows.append(cells)
        head = "<tr><th></th>%s</tr>" % "".join("<th>%s</th>" % _letters(i) for i in range(width))
        body = "".join("<tr><th>%d</th>%s</tr>" % (n + 1, "".join("<td>%s</td>" % H.escape(c) for c in r + [""] * (width - len(r)))) for n, r in enumerate(rows))
        title = H.escape(names.get(f) or "Sheet%d" % (k + 1))
        tabs.append("<button data-i='%d'%s>%s</button>" % (k, " class='on'" if not k else "", title))
        panes.append("<div class='sheet' data-i='%d'%s><div class='ts'><table>%s%s</table></div>%s</div>" % (
            k, "" if not k else " hidden", head, body, "<p class='note'>Showing the first %d rows.</p>" % MAX_ROWS if cut else ""))
    css = (".tabs{position:sticky;top:0;display:flex;gap:4px;padding:8px;background:inherit;z-index:2;overflow:auto}.tabs button{font:inherit;padding:4px 12px;border:1px solid #c8c8cc;border-radius:6px;background:transparent;color:inherit;cursor:pointer}"
           ".tabs .on{background:#0a84ff;border-color:#0a84ff;color:#fff}.ts{overflow:auto;padding:0 8px 16px}table{border-collapse:collapse;font-size:13px;background:#fff;color:#1d1d1f}"
           "td,th{border:1px solid #d4d4d8;padding:3px 8px;white-space:pre;max-width:360px;overflow:hidden;text-overflow:ellipsis}th{background:#ececf0;color:#555;font-weight:500;position:sticky}"
           ".note{padding:0 12px;color:#888;font-size:12px}")
    js = ("<script>document.querySelectorAll('.tabs button').forEach(function(b){b.onclick=function(){document.querySelectorAll('.tabs button').forEach(function(x){x.classList.toggle('on',x===b)});"
          "document.querySelectorAll('.sheet').forEach(function(s){s.hidden=s.dataset.i!==b.dataset.i})}})</script>")
    return PAGE % ("Workbook", css, ("<div class='tabs'>%s</div>" % "".join(tabs) if len(tabs) > 1 else "") + "".join(panes), js)


# ---------------------------------------------------------------- pptx

def _theme(z):
    out = {}
    names = [n for n in z.namelist() if re.match(r"ppt/theme/theme\d+\.xml$", n)]
    if not names:
        return out
    cs = ET.fromstring(z.read(sorted(names)[0])).find(".//" + A + "clrScheme")
    for c in (cs if cs is not None else []):
        ch = c[0] if len(c) else None
        if ch is not None:
            out[c.tag.replace(A, "")] = ch.get("val") if ch.tag == A + "srgbClr" else ch.get("lastClr") or "000000"
    return out


def _color(el, theme):
    sf = el.find(A + "solidFill") if el is not None else None
    if sf is None or not len(sf):
        return ""
    c = sf[0]
    if c.tag == A + "srgbClr":
        return "#" + (c.get("val") or "000000")
    if c.tag == A + "schemeClr":
        v = SCHEME.get(c.get("val") or "", c.get("val") or "")
        return "#" + theme[v] if v in theme else ""
    if c.tag == A + "sysClr":
        return "#" + (c.get("lastClr") or "000000")
    return ""


def _xfrm(sp):
    x = sp.find(".//" + A + "xfrm")
    if x is None:
        x = sp.find(P + "xfrm")
    if x is None:
        return None
    off, ext = x.find(A + "off"), x.find(A + "ext")
    if off is None or ext is None:
        return None
    return (int(off.get("x", 0)) / EMU, int(off.get("y", 0)) / EMU, int(ext.get("cx", 0)) / EMU, int(ext.get("cy", 0)) / EMU, int(x.get("rot", 0) or 0) / 60000.0)


def _placeholders(z, part, cache):
    if part in cache:
        return cache[part]
    out = {}
    if part and part in z.namelist():
        for sp in ET.fromstring(z.read(part)).iter(P + "sp"):
            ph = sp.find(".//" + P + "ph")
            pr = sp.find(P + "spPr")
            xf = _xfrm(pr) if pr is not None else None
            if ph is not None and xf:
                out[(ph.get("type") or "body", ph.get("idx") or "0")] = xf
    cache[part] = out
    return out


def _para(p, theme, base_pt):
    ppr = p.find(A + "pPr")
    algn = {"ctr": "center", "r": "right", "just": "justify"}.get((ppr.get("algn") if ppr is not None else "") or "", "left")
    bullet = ppr is not None and ppr.find(A + "buChar") is not None
    parts = []
    for r in p:
        if r.tag == A + "br":
            parts.append("<br>")
        if r.tag not in (A + "r", A + "fld"):
            continue
        t = r.find(A + "t")
        if t is None or not t.text:
            continue
        pr, st = r.find(A + "rPr"), []
        if pr is not None:
            if pr.get("sz"):
                st.append("font-size:%.1fpx" % (int(pr.get("sz")) / 100.0 * 96 / 72))
            if pr.get("b") == "1":
                st.append("font-weight:700")
            if pr.get("i") == "1":
                st.append("font-style:italic")
            if pr.get("u") and pr.get("u") != "none":
                st.append("text-decoration:underline")
            col = _color(pr, theme)
            if col:
                st.append("color:" + col)
        parts.append('<span style="%s">%s</span>' % (";".join(st), H.escape(t.text)))
    inner = "".join(parts)
    if not inner:
        return '<div class="p" style="height:%.0fpx"></div>' % (base_pt * 96 / 72)
    return '<div class="p%s" style="text-align:%s">%s</div>' % (" b" if bullet else "", algn, inner)


def _shape(el, z, rels, theme, ph_map, imgs, budget):
    tag = el.tag.replace(P, "")
    sppr = el.find(P + "spPr")
    xf = _xfrm(el)
    ph = el.find(".//" + P + "ph")
    if xf is None and ph is not None:
        xf = ph_map.get((ph.get("type") or "body", ph.get("idx") or "0")) or ph_map.get((ph.get("type") or "body", "0"))
    if xf is None:
        return ""
    x, y, w, h, rot = xf
    st = ["left:%.1fpx" % x, "top:%.1fpx" % y, "width:%.1fpx" % w, "height:%.1fpx" % h]
    if rot:
        st.append("transform:rotate(%.1fdeg)" % rot)
    if tag == "pic":
        blip = el.find(".//" + A + "blip")
        src = _img(z, rels.get(blip.get(RN + "embed")) if blip is not None else None, imgs, budget)
        return '<img class="sh" style="%s" src="%s" alt="">' % (";".join(st), src) if src else ""
    if tag == "graphicFrame":
        tbl = el.find(".//" + A + "tbl")
        if tbl is None:
            return ""
        rows = ["<tr>%s</tr>" % "".join("<td>%s</td>" % H.escape(" ".join("".join(t.text or "" for t in p.iter(A + "t")) for p in tc.iter(A + "p"))) for tc in tr.findall(A + "tc")) for tr in tbl.findall(A + "tr")]
        return '<div class="sh tbl" style="%s"><table>%s</table></div>' % (";".join(st), "".join(rows))
    if tag != "sp":
        return ""
    fill = _color(sppr, theme) if sppr is not None else ""
    geom = sppr.find(A + "prstGeom") if sppr is not None else None
    if fill:
        st.append("background:" + fill)
    if geom is not None and geom.get("prst") == "ellipse":
        st.append("border-radius:50%")
    ln = sppr.find(A + "ln") if sppr is not None else None
    lc = _color(ln, theme) if ln is not None else ""
    if lc and ln.find(A + "noFill") is None:
        st.append("border:%.1fpx solid %s" % (max(1, int(ln.get("w", 12700)) / EMU), lc))
    tx = el.find(P + "txBody")
    base = 40 if ph is not None and ph.get("type") in ("title", "ctrTitle") else 18
    body = "".join(_para(p, theme, base) for p in tx.findall(A + "p")) if tx is not None else ""
    bp = tx.find(A + "bodyPr") if tx is not None else None
    st.append("justify-content:" + {"ctr": "center", "b": "flex-end"}.get((bp.get("anchor") if bp is not None else "") or "", "flex-start"))
    st.append("font-size:%.1fpx" % (base * 96 / 72.0))
    if not body and not fill and not lc:
        return ""
    return '<div class="sh tx" style="%s">%s</div>' % (";".join(st), body)


def _walk(tree, z, rels, theme, ph_map, imgs, budget):
    out = []
    for el in tree:
        t = el.tag.replace(P, "")
        if t in ("sp", "pic", "graphicFrame"):
            out.append(_shape(el, z, rels, theme, ph_map, imgs, budget))
        elif t == "grpSp":
            out.append(_walk(el, z, rels, theme, ph_map, imgs, budget))
    return "".join(out)


def _bg(root, theme):
    bg = root.find(".//" + P + "bg")
    pr = bg.find(".//" + P + "bgPr") if bg is not None else None
    return _color(pr, theme) if pr is not None else ""


def pptx(path):
    with _zip(path) as z:
        return _pptx(z)


def _pptx(z):
    pres = _xml(z, "ppt/presentation.xml")
    sz = pres.find(P + "sldSz")
    w, h = (int(sz.get("cx")) / EMU, int(sz.get("cy")) / EMU) if sz is not None else (1280.0, 720.0)
    prels, theme = _rels(z, "ppt/presentation.xml"), _theme(z)
    order = [prels.get(s.get(RN + "id")) for s in pres.iter(P + "sldId")]
    imgs, cache, budget, slides = {}, {}, [0], []
    for n, part in enumerate([o for o in order if o and o in z.namelist()][:MAX_SLIDES]):
        root = _xml(z, part)
        rels = _rels(z, part)
        layout = next((v for v in rels.values() if "slideLayouts/" in v), "")
        master = next((v for v in (_rels(z, layout) if layout else {}).values() if "slideMasters/" in v), "")
        ph_map = dict(_placeholders(z, master, cache))
        ph_map.update(_placeholders(z, layout, cache))
        bg = _bg(root, theme)
        for extra in (layout, master):
            if not bg and extra in z.namelist():
                bg = _bg(ET.fromstring(z.read(extra)), theme)
        tree = root.find(".//" + P + "spTree")
        body = _walk(tree, z, rels, theme, ph_map, imgs, budget) if tree is not None else ""
        slides.append("<section class='slide'><div class='lbl'>%d</div><div class='box'><div class='stage' style='width:%.0fpx;height:%.0fpx'>"
                      "<div class='bg' style='background:%s'></div>%s</div></div></section>" % (n + 1, w, h, bg or "#ffffff", body))
    if not slides:
        raise ValueError("no slides found in the presentation")
    css = ("body{padding:12px 0}.slide{margin:0 auto 18px;width:min(96vw,1100px)}.lbl{font-size:12px;color:#888;margin:0 0 4px 2px}"
           ".box{position:relative;overflow:hidden;box-shadow:0 1px 8px #0003;border-radius:4px;background:#fff}.stage{position:absolute;left:0;top:0;transform-origin:0 0;overflow:hidden}"
           ".sh{position:absolute;box-sizing:border-box;overflow:hidden}.bg{position:absolute;inset:0}"
           ".tx{display:flex;flex-direction:column;padding:4px 8px;line-height:1.2;color:#222;word-break:break-word}"
           ".p.b:before{content:'\\2022\\00a0'}.tbl table{border-collapse:collapse;width:100%;font-size:14px;color:#222;background:#fff}"
           ".tbl td{border:1px solid #bbb;padding:4px 6px}img.sh{object-fit:contain}")
    js = ("<script>(function(){var W=%d,H=%d;function fit(){document.querySelectorAll('.box').forEach(function(b){var s=b.clientWidth/W;"
          "b.style.height=(H*s)+'px';b.firstChild.style.transform='scale('+s+')'})}addEventListener('resize',fit);fit()})()</script>") % (round(w), round(h))
    return PAGE % ("Presentation", css, "".join(slides), js)


_CACHE = {}                                    # (path, mtime_ns, size) -> page; small, oldest dropped first
_CACHE_MAX = 8
_CACHE_BYTES = 24 * 1024 * 1024
_clock = threading.Lock()


def render(path):
    """-> HTML page for a .docx/.xlsx/.pptx file. ValueError carries a message fit to show the person. Rendered pages are cached by (path, mtime, size)."""
    ext = os.path.splitext(path)[1].lower()
    fn = {".docx": docx, ".xlsx": xlsx, ".pptx": pptx}.get(ext)
    if not fn:
        raise ValueError("no preview for " + ext)
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    with _clock:
        page = _CACHE.get(key)
        if page is not None:
            _CACHE[key] = _CACHE.pop(key)             # most recently used last
            return page
    page = _render(fn, path, ext)
    with _clock:
        _CACHE[key] = page
        while len(_CACHE) > _CACHE_MAX or sum(len(v) for v in _CACHE.values()) > _CACHE_BYTES:
            if len(_CACHE) <= 1:
                break
            _CACHE.pop(next(iter(_CACHE)))
    return page


def _render(fn, path, ext):
    try:
        return fn(path)
    except (ET.ParseError, KeyError, zipfile.BadZipFile, AttributeError, TypeError) as e:
        raise ValueError("could not read this %s file (%s)" % (ext[1:], e.__class__.__name__))


def error_page(msg):
    return PAGE % ("Preview", "body{padding:40px;text-align:center;color:#888}", "<p>%s</p>" % H.escape(msg), "")
