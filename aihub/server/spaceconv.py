"""Turn an uploaded file into something the browser can show without any plugin. Standard library only.

kinds: doc (.docx .md .txt), slides (.pptx, or an .html deck of <section class="slide">), pdf, sheet (.xlsx .csv .tsv),
       report / dashboard / html (a single .html file or a .zip / .tar.gz site with an index.html).
Everything we generate ourselves is built from escaped text. Untrusted HTML (reports, decks) is never injected into the hub page:
it runs in a sandboxed iframe (see routers_spaces)."""
import base64
import html
import io
import json
import mimetypes
import os
import re
import shutil
import tarfile
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from ..core import archive
from . import markdown, sheet

KINDS = ("doc", "slides", "report", "pdf", "sheet", "dashboard", "html")
HTML_KINDS = ("report", "dashboard", "html")
MAX_SLIDES = 500
MAX_SHEET_ROWS = 5000
MAX_SHEET_COLS = 100
MAX_IMG = 8 * 1024 * 1024
MAX_DECK_IMG = 48 * 1024 * 1024
SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TRANSITIONS = ("auto", "none", "fade", "slide", "zoom", "flip")


class SpaceError(ValueError):
    pass


def slugify(name):
    s = re.sub(r"[^a-z0-9._-]+", "-", os.path.splitext(os.path.basename(name or ""))[0].lower()).strip("-._")
    return s[:64] or "space"


def title_of(name):
    return re.sub(r"[-_.]+", " ", os.path.splitext(os.path.basename(name or ""))[0]).strip().title()[:120]


def sniff(filename, head):
    """-> (family, default kind). family: pptx docx xlsx csv md txt html pdf site."""
    n = (filename or "").lower()
    if n.endswith(".pdf") or head[:5] == b"%PDF-":
        return "pdf", "pdf"
    if n.endswith((".zip", ".tar.gz", ".tgz")):
        return "site", "html"
    if head[:2] == b"PK":
        if n.endswith(".pptx"):
            return "pptx", "slides"
        if n.endswith(".docx"):
            return "docx", "doc"
        if n.endswith((".xlsx", ".xlsm")):
            return "xlsx", "sheet"
        return "site", "html"
    if n.endswith((".md", ".markdown")):
        return "md", "doc"
    if n.endswith(".txt"):
        return "txt", "doc"
    if n.endswith((".csv", ".tsv")):
        return "csv", "sheet"
    if n.endswith((".html", ".htm")):
        return "html", "html"
    raise SpaceError("unsupported file type (use .pptx .docx .xlsx .csv .md .txt .pdf .html or a .zip / .tar.gz site with index.html)")


# ---------------------------------------------------------------- entry point
def build(dest, src, filename, hint=None):
    """Read `src` (a file) and write what the hub serves into folder `dest`. -> {"kind", "family"}.
    Layout: dest/orig/<name> (the file as sent), plus deck.json | doc.html | sheet.json | site/ for the viewer."""
    with open(src, "rb") as f:
        head = f.read(8)
    family, kind = sniff(filename, head)
    if hint and hint != "auto":
        if hint not in KINDS:
            raise SpaceError("kind must be one of: " + ", ".join(KINDS))
        ok = {"slides": ("pptx", "html"), "doc": ("docx", "md", "txt", "html"), "sheet": ("xlsx", "csv"), "pdf": ("pdf",)}
        if hint in ok and family not in ok[hint]:
            raise SpaceError("a %s file cannot be a %s space" % (family, hint))
        if hint in HTML_KINDS and family not in ("html", "site"):
            raise SpaceError("a %s file cannot be a %s space" % (family, hint))
        kind = hint
    os.makedirs(os.path.join(dest, "orig"), exist_ok=True)
    safe = re.sub(r"[^\w.\- ]+", "_", os.path.basename(filename or "file"))[:120] or "file"
    shutil.copyfile(src, os.path.join(dest, "orig", safe))
    try:
        if family == "pptx":
            _write(dest, "deck.json", pptx_deck(src))
        elif family == "docx":
            _write(dest, "doc.html", docx_html(src), raw=True)
        elif family in ("md", "txt"):
            text = _text(src)
            _write(dest, "doc.html", markdown.render(text) if family == "md" else "<pre>%s</pre>" % html.escape(text), raw=True)
        elif family in ("xlsx", "csv"):
            _write(dest, "sheet.json", sheets(src, filename))
        elif family == "html":
            text = _text(src)
            deck = html_deck(text) if kind in ("slides", "html") else None
            if deck and kind in ("slides", "html"):
                kind = "slides"
                _write(dest, "deck.json", deck)
            elif kind == "slides":
                raise SpaceError('no slides found: wrap each slide in <section class="slide">')
            else:
                os.makedirs(os.path.join(dest, "site"), exist_ok=True)
                shutil.copyfile(src, os.path.join(dest, "site", "index.html"))
        elif family == "site":
            _site(dest, os.path.join(dest, "orig", safe))
            if kind == "slides":
                raise SpaceError("a slide deck is one .pptx or one .html file with <section class=\"slide\"> parts")
    except (zipfile.BadZipFile, tarfile.TarError, ET.ParseError, KeyError) as e:
        shutil.rmtree(dest, ignore_errors=True)
        raise SpaceError("could not read the file: %s" % (e.__class__.__name__))
    return {"kind": kind, "family": family}


def _text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _write(dest, name, data, raw=False):
    with open(os.path.join(dest, name), "w", encoding="utf-8") as f:
        f.write(data if raw else json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def _site(dest, orig):
    tmp = os.path.join(dest, "_x")
    arc = os.path.join(dest, "_a.zip" if zipfile.is_zipfile(orig) else "_a.tgz")
    shutil.copyfile(orig, arc)
    try:
        root = archive.safe_extract(arc, tmp)
    except ValueError as e:
        raise SpaceError(str(e))
    finally:
        try:
            os.remove(arc)
        except OSError:
            pass
    if not os.path.isfile(os.path.join(root, "index.html")):
        raise SpaceError("the archive needs an index.html at its top")
    shutil.move(root, os.path.join(dest, "site"))
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- sheets
def sheets(path, filename):
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] == b"PK" or filename.lower().endswith((".xlsx", ".xlsm")):
        parts = sheet.parse_all(data)
    else:
        parts = [("Sheet1", sheet.parse(data, filename))]
    out = []
    for name, rows in parts[:50]:
        cut = len(rows) > MAX_SHEET_ROWS
        out.append({"name": name, "rows": [r[:MAX_SHEET_COLS] for r in rows[:MAX_SHEET_ROWS]], "total": len(rows), "cut": cut})
    return {"sheets": out}


# ---------------------------------------------------------------- docx
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_html(path):
    z = zipfile.ZipFile(path)
    if sum(i.file_size for i in z.infolist()) > 200 * 1024 * 1024:
        raise SpaceError("document too large")
    root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    out, in_list = [], False

    def runs(p):
        s = []
        for r in p.iter(W + "r"):
            t = "".join((x.text or "") if x.tag == W + "t" else "\t" if x.tag == W + "tab" else "<br>" if x.tag == W + "br" else ""
                        for x in r if x.tag in (W + "t", W + "tab", W + "br"))
            if not t:
                continue
            t = t.replace("<br>", "\0")
            t = html.escape(t).replace("\0", "<br>")
            pr = r.find(W + "rPr")
            if pr is not None:
                if pr.find(W + "b") is not None:
                    t = "<b>%s</b>" % t
                if pr.find(W + "i") is not None:
                    t = "<i>%s</i>" % t
                if pr.find(W + "u") is not None:
                    t = "<u>%s</u>" % t
            s.append(t)
        return "".join(s)

    def para(p):
        nonlocal in_list
        txt = runs(p)
        ppr = p.find(W + "pPr")
        style = ""
        listed = False
        if ppr is not None:
            st = ppr.find(W + "pStyle")
            style = (st.get(W + "val") if st is not None else "") or ""
            listed = ppr.find(W + "numPr") is not None
        if listed and txt:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % txt)
            return
        if in_list:
            out.append("</ul>")
            in_list = False
        if not txt.strip():
            return
        m = re.match(r"(?i)heading\s*(\d)", style)
        if m:
            n = min(6, max(1, int(m.group(1))))
            out.append("<h%d>%s</h%d>" % (n, txt, n))
        elif style.lower() == "title":
            out.append("<h1>%s</h1>" % txt)
        elif style.lower() == "subtitle":
            out.append("<p class='sub'>%s</p>" % txt)
        else:
            out.append("<p>%s</p>" % txt)

    for el in list(body) if body is not None else []:
        if el.tag == W + "p":
            para(el)
        elif el.tag == W + "tbl":
            if in_list:
                out.append("</ul>")
                in_list = False
            rows = []
            for tr in el.iter(W + "tr"):
                rows.append("<tr>%s</tr>" % "".join("<td>%s</td>" % " ".join(runs(p) for p in tc.iter(W + "p")) for tc in tr.findall(W + "tc")))
            out.append("<div class='tscroll'><table>%s</table></div>" % "".join(rows))
    if in_list:
        out.append("</ul>")
    return "".join(out) or "<p></p>"


# ---------------------------------------------------------------- HTML deck
class _Deck(HTMLParser):
    """Collect <section class="slide"> parts (nested tags kept as written), the <style> text, and aihub-* meta."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.css, self.meta, self.slides = [], {}, []
        self.depth = 0
        self.cur = None
        self.in_style = False
        self.skip = 0

    @staticmethod
    def _is_slide(tag, attrs):
        return tag == "section" and "slide" in (dict(attrs).get("class") or "").split()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and (a.get("name") or "").startswith("aihub-"):
            self.meta[a["name"][6:]] = a.get("content") or ""
        if tag == "style" and self.cur is None:
            self.in_style = True
            return
        if self.cur is None:
            if self._is_slide(tag, attrs):
                self.cur = {"parts": [], "tr": a.get("data-transition") or "", "notes": []}
                self.cur["parts"].append(self._open(tag, attrs))
                self.depth = 1
            return
        if tag in ("link", "base", "embed"):
            return
        if tag in ("script", "iframe", "object"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "section":
            self.depth += 1
        self.cur["parts"].append(self._open(tag, attrs))

    def handle_startendtag(self, tag, attrs):
        if self.cur is not None and not self.skip and tag not in ("script", "iframe", "object", "embed", "link", "base"):
            self.cur["parts"].append(self._open(tag, attrs))

    @staticmethod
    def _open(tag, attrs):
        keep = []
        for k, v in attrs:
            if k.startswith("on") or k in ("srcdoc",):
                continue
            if k in ("href", "src", "xlink:href", "poster") and v and re.match(r"(?i)\s*(javascript|vbscript):", v):
                continue
            keep.append(' %s="%s"' % (k, html.escape(v or "", quote=True)))
        return "<%s%s>" % (tag, "".join(keep))

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
            return
        if self.cur is None:
            return
        if tag in ("link", "base", "embed"):
            return
        if tag in ("script", "iframe", "object"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        self.cur["parts"].append("</%s>" % tag)
        if tag == "section":
            self.depth -= 1
            if self.depth == 0:
                self.slides.append(self.cur)
                self.cur = None

    def handle_data(self, data):
        if self.in_style:
            self.css.append(data)
        elif self.cur is not None and not self.skip:
            self.cur["parts"].append(data)

    def handle_entityref(self, name):
        self.handle_data("&%s;" % name) if not self.in_style else None

    def handle_charref(self, name):
        self.handle_data("&#%s;" % name) if not self.in_style else None


def html_deck(text):
    """-> deck dict, or None when the page has fewer than one <section class="slide">."""
    d = _Deck()
    try:
        d.feed(text)
        d.close()
    except Exception:
        return None
    if not d.slides:
        return None
    m = re.match(r"^(\d{3,5})\s*[x×]\s*(\d{3,5})$", d.meta.get("slide-size", ""))
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1280, 720)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    slides = []
    for s in d.slides[:MAX_SLIDES]:
        body = "".join(s["parts"])
        notes = re.findall(r"(?is)<aside[^>]*class=\"[^\"]*notes[^\"]*\"[^>]*>(.*?)</aside>", body)
        body = re.sub(r"(?is)<aside[^>]*class=\"[^\"]*notes[^\"]*\"[^>]*>.*?</aside>", "", body)
        tr = (s["tr"] or d.meta.get("transition") or "").lower()
        slides.append({"html": body, "tr": tr if tr in TRANSITIONS else "", "notes": re.sub(r"<[^>]+>", " ", " ".join(notes)).strip()[:2000]})
    return {"w": w, "h": h, "css": "".join(d.css)[:200000], "slides": slides, "source": "html",
            "title": html.unescape(re.sub(r"\s+", " ", title.group(1)).strip()) if title else ""}


# ---------------------------------------------------------------- pptx
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
RN = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
EMU = 9525.0                                   # EMU per CSS px at 96 dpi
SCHEME = {"tx1": "dk1", "bg1": "lt1", "tx2": "dk2", "bg2": "lt2"}
TR_KIND = {"fade": "fade", "dissolve": "fade", "push": "slide", "cover": "slide", "pull": "slide", "wipe": "slide", "split": "slide",
           "blinds": "slide", "strips": "slide", "randomBar": "slide", "zoom": "zoom", "circle": "zoom", "diamond": "zoom", "plus": "zoom",
           "wheel": "flip", "flip": "flip", "vortex": "flip", "prism": "flip", "doors": "flip", "window": "flip", "ferris": "slide", "gallery": "slide",
           "conveyor": "slide", "pan": "slide", "glitter": "fade", "honeycomb": "fade", "flash": "fade", "ripple": "zoom", "shred": "fade", "switch": "flip", "flythrough": "zoom"}


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
        out[r.get("Id")] = os.path.normpath(os.path.join(d, t)).replace("\\", "/") if not t.startswith("/") else t.lstrip("/")
    return out


def _theme(z):
    out = {}
    names = [n for n in z.namelist() if re.match(r"ppt/theme/theme\d+\.xml$", n)]
    if not names:
        return out
    cs = ET.fromstring(z.read(sorted(names)[0])).find(".//" + A + "clrScheme")
    for c in (cs if cs is not None else []):
        k = c.tag.replace(A, "")
        ch = c[0] if len(c) else None
        if ch is not None:
            out[k] = ch.get("val") if ch.tag == A + "srgbClr" else ch.get("lastClr") or "000000"
    return out


def _color(el, theme):
    """solidFill/srgbClr|schemeClr child of `el` -> '#rrggbb' or ''."""
    sf = el.find(A + "solidFill") if el is not None else None
    if sf is None or not len(sf):
        return ""
    c = sf[0]
    if c.tag == A + "srgbClr":
        return "#" + (c.get("val") or "000000")
    if c.tag == A + "schemeClr":
        v = c.get("val") or ""
        v = SCHEME.get(v, v)
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
    """(type, idx) -> xfrm of the placeholders on a layout / master part."""
    if part in cache:
        return cache[part]
    out = {}
    if part and part in z.namelist():
        for sp in ET.fromstring(z.read(part)).iter(P + "sp"):
            ph = sp.find(".//" + P + "ph")
            xf = _xfrm(sp.find(P + "spPr")) if sp.find(P + "spPr") is not None else None
            if ph is not None and xf:
                out[(ph.get("type") or "body", ph.get("idx") or "0")] = xf
    cache[part] = out
    return out


def _para_html(p, theme, base_pt):
    algn = {"ctr": "center", "r": "right", "just": "justify"}.get((p.find(A + "pPr").get("algn") if p.find(A + "pPr") is not None else "") or "", "left")
    bullet = p.find(A + "pPr") is not None and p.find(A + "pPr").find(A + "buChar") is not None
    parts, size = [], 0
    for r in p:
        if r.tag == A + "br":
            parts.append("<br>")
        if r.tag not in (A + "r", A + "fld"):
            continue
        t = r.find(A + "t")
        if t is None or not t.text:
            continue
        pr = r.find(A + "rPr")
        st = []
        if pr is not None:
            if pr.get("sz"):
                st.append("font-size:%.1fpx" % (int(pr.get("sz")) / 100.0 * 96 / 72))
                size = max(size, int(pr.get("sz")))
            if pr.get("b") == "1":
                st.append("font-weight:700")
            if pr.get("i") == "1":
                st.append("font-style:italic")
            if pr.get("u") and pr.get("u") != "none":
                st.append("text-decoration:underline")
            col = _color(pr, theme)
            if col:
                st.append("color:" + col)
        parts.append('<span style="%s">%s</span>' % (";".join(st), html.escape(t.text)))
    inner = "".join(parts)
    if not inner:
        return '<div class="p" style="height:%.0fpx"></div>' % (base_pt * 96 / 72)
    return '<div class="p%s" style="text-align:%s">%s</div>' % (" b" if bullet else "", algn, inner)


def _shape(el, z, rels, theme, ph_map, imgs, budget):
    """-> html for one p:sp / p:pic / p:graphicFrame."""
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
        rid = blip.get(RN + "embed") if blip is not None else None
        name = rels.get(rid)
        if not name or name not in z.namelist():
            return ""
        info = z.getinfo(name)
        if info.file_size > MAX_IMG or budget[0] + info.file_size > MAX_DECK_IMG:
            return ""
        if name not in imgs:
            mime = mimetypes.guess_type(name)[0] or "image/png"
            if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml", "image/bmp"):
                return ""
            budget[0] += info.file_size
            imgs[name] = "data:%s;base64,%s" % (mime, base64.b64encode(z.read(name)).decode())
        return '<img class="sh" style="%s" src="%s" alt="">' % (";".join(st), imgs[name])
    if tag == "graphicFrame":
        tbl = el.find(".//" + A + "tbl")
        if tbl is None:
            return ""
        rows = []
        for tr in tbl.findall(A + "tr"):
            cells = []
            for tc in tr.findall(A + "tc"):
                txt = " ".join("".join(t.text or "" for t in p.iter(A + "t")) for p in tc.iter(A + "p"))
                cells.append("<td>%s</td>" % html.escape(txt))
            rows.append("<tr>%s</tr>" % "".join(cells))
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
    base = 40 if ph is not None and (ph.get("type") in ("title", "ctrTitle")) else 18
    body = "".join(_para_html(p, theme, base) for p in tx.findall(A + "p")) if tx is not None else ""
    bp = tx.find(A + "bodyPr") if tx is not None else None
    anchor = {"ctr": "center", "b": "flex-end"}.get((bp.get("anchor") if bp is not None else "") or "", "flex-start")
    st.append("justify-content:" + anchor)
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
    if bg is None:
        return ""
    return _color(bg.find(".//" + P + "bgPr"), theme) if bg.find(".//" + P + "bgPr") is not None else ""


def pptx_deck(path):
    z = zipfile.ZipFile(path)
    if sum(i.file_size for i in z.infolist()) > 400 * 1024 * 1024:
        raise SpaceError("presentation too large")
    pres = ET.fromstring(z.read("ppt/presentation.xml"))
    sz = pres.find(P + "sldSz")
    w, h = (int(sz.get("cx")) / EMU, int(sz.get("cy")) / EMU) if sz is not None else (1280.0, 720.0)
    prels = _rels(z, "ppt/presentation.xml")
    theme = _theme(z)
    order = [prels.get(s.get(RN + "id")) for s in pres.iter(P + "sldId")]
    imgs, cache, budget, slides = {}, {}, [0], []
    for part in [o for o in order if o and o in z.namelist()][:MAX_SLIDES]:
        root = ET.fromstring(z.read(part))
        rels = _rels(z, part)
        layout = next((v for v in rels.values() if "slideLayouts/" in v), "")
        lrels = _rels(z, layout) if layout else {}
        master = next((v for v in lrels.values() if "slideMasters/" in v), "")
        ph_map = dict(_placeholders(z, master, cache))
        ph_map.update(_placeholders(z, layout, cache))
        bg = _bg(root, theme)
        if not bg and layout in z.namelist():
            bg = _bg(ET.fromstring(z.read(layout)), theme)
        if not bg and master in z.namelist():
            bg = _bg(ET.fromstring(z.read(master)), theme)
        tree = root.find(".//" + P + "spTree")
        body = _walk(tree, z, rels, theme, ph_map, imgs, budget) if tree is not None else ""
        tr = ""
        for el in root.iter():
            if el.tag.endswith("}transition"):
                for ch in el:
                    tr = TR_KIND.get(ch.tag.split("}")[-1], "fade")
                    break
                break
        slides.append({"html": '<div class="bg" style="background:%s"></div>%s' % (bg or "#ffffff", body), "tr": tr, "notes": ""})
    if not slides:
        raise SpaceError("no slides found in the presentation")
    css = (".sh{position:absolute;box-sizing:border-box;overflow:hidden}.bg{position:absolute;inset:0}"
           ".tx{display:flex;flex-direction:column;padding:4px 8px;line-height:1.2;color:#222;word-break:break-word}"
           ".p.b:before{content:'\\2022\\00a0'}.tbl table{border-collapse:collapse;width:100%;font-size:14px}"
           ".tbl td{border:1px solid #bbb;padding:4px 6px}img.sh{object-fit:contain}")
    return {"w": round(w), "h": round(h), "css": css, "slides": slides, "source": "pptx", "title": ""}
