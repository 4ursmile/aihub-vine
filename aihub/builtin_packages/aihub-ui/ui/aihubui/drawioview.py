"""Canvas preview of draw.io diagrams: .drawio / .dio -> ONE self-contained HTML page with an inline SVG per diagram page (stdlib only).

Reads plain and compressed (base64 + raw deflate + URI-encoded) pages, places vertices (groups, containers, swimlanes, relative geometry),
draws the common shapes (rect, rounded, ellipse, rhombus, triangle, hexagon, cylinder, document, note, cloud, actor, image, ...),
routes edges (straight, waypoints, orthogonal / elbow, ports, arrows) and shows labels (HTML labels pass a strict tag / style allowlist).
Unknown stencils (mxgraph.*) fall back to a box with their label. `page(path)` returns HTML or raises ValueError."""
import base64
import binascii
import html as H
import math
import os
import re
import xml.etree.ElementTree as ET
import zlib
from html.parser import HTMLParser
from urllib.parse import unquote

EXT = (".drawio", ".dio")
MAX_FILE = 20 * 1024 * 1024
MAX_XML = 64 * 1024 * 1024
MAX_PAGES = 60
MAX_CELLS = 30000
MAX_IMG = 4 * 1024 * 1024

PAGE = ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>%s</title><style>"
        ":root{color-scheme:light dark}body{margin:0;font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:#f4f4f6;color:#1d1d1f}"
        "@media(prefers-color-scheme:dark){body{background:#1c1c1e;color:#eee}.tabs button{border-color:#4a4a4e!important}}"
        ".tabs{position:sticky;top:0;display:flex;gap:4px;padding:8px;background:inherit;z-index:2;overflow:auto}"
        ".tabs button{font:inherit;padding:3px 12px;border:1px solid #c8c8cc;border-radius:6px;background:transparent;color:inherit;cursor:pointer;white-space:nowrap}"
        ".tabs .on{background:#0a84ff;border-color:#0a84ff;color:#fff}.pg{padding:8px 12px 20px}.board{display:inline-block;max-width:100%%;box-shadow:0 1px 8px #0003;border-radius:4px;overflow:hidden;cursor:zoom-in}"
        ".board svg{display:block;max-width:100%%;height:auto}.full .board{max-width:none;cursor:zoom-out}.full .board svg{max-width:none}.wrap{overflow:auto;text-align:center}"
        ".note{color:#888;font-size:12px;margin:6px 2px}.lb{box-sizing:border-box;display:flex;width:100%%;height:100%%;overflow:visible}"
        ".lb>div{max-width:100%%;overflow-wrap:break-word}.lb p{margin:0}.lb ul,.lb ol{margin:0;padding-left:1.4em;text-align:left}.lb table{border-collapse:collapse}.lb td,.lb th{border:1px solid currentColor;padding:1px 4px}"
        "</style></head><body>%s%s</body></html>")
JS = ("<script>(function(){var b=document.querySelectorAll('.tabs button');b.forEach(function(t){t.onclick=function(){b.forEach(function(x){x.classList.toggle('on',x===t)});"
      "document.querySelectorAll('.pg').forEach(function(p){p.hidden=p.dataset.i!==t.dataset.i})}});"
      "document.querySelectorAll('.board').forEach(function(d){d.onclick=function(){d.closest('.pg').classList.toggle('full')}})})()</script>")


def error_page(msg):
    return PAGE % ("Preview", "", "<p style='padding:40px;text-align:center;color:#888'>%s</p>" % H.escape(msg), "")


# ---------------------------------------------------------------- reading

def _parse_xml(text):
    if re.search(r"<!(DOCTYPE|ENTITY)", text[:4096] if len(text) > 4096 else text, re.I) or "<!ENTITY" in text:
        raise ValueError("this diagram declares XML entities, which the preview does not read")
    return ET.fromstring(text)


def _inflate(text):
    """A compressed <diagram> body -> the mxGraphModel XML text."""
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", text), validate=False)
    except (binascii.Error, ValueError):
        raise ValueError("could not decode a compressed diagram page")
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(raw, MAX_XML)
    except zlib.error:
        raise ValueError("could not inflate a compressed diagram page")
    if d.unconsumed_tail:
        raise ValueError("a diagram page is too large to preview")
    s = out.decode("utf-8", "replace")
    return unquote(s) if s.lstrip().startswith("%") else s


def _pages(root):
    """-> [(name, mxGraphModel element)]"""
    if root.tag == "mxGraphModel":
        return [("Page-1", root)]
    if root.tag != "mxfile":
        raise ValueError("this is not a draw.io file (root element <%s>)" % root.tag[:40])
    out = []
    for i, d in enumerate(root.findall("diagram")):
        if len(out) >= MAX_PAGES:
            break
        name = d.get("name") or "Page-%d" % (i + 1)
        m = d.find("mxGraphModel")
        if m is None:
            body = (d.text or "").strip()
            if not body:
                continue
            m = _parse_xml(body if body.startswith("<") else _inflate(body))
            if m.tag != "mxGraphModel":
                m = m.find(".//mxGraphModel")
                if m is None:
                    continue
        out.append((name, m))
    if not out:
        raise ValueError("this draw.io file has no diagram pages")
    return out


# ---------------------------------------------------------------- styles

_COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,24}|rgba?\(\s*[\d.]+%?\s*,\s*[\d.]+%?\s*,\s*[\d.]+%?\s*(,\s*[\d.]+\s*)?\))$")


def _style(s):
    out = {}
    for i, part in enumerate((s or "").split(";")):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
        elif i == 0:
            out["_name"] = part
    return out


def _color(v, default):
    if v is None or v == "" or v == "default":
        return default
    m = re.match(r"light-dark\(\s*([^,]+),", v)               # newer draw.io: light-dark(a, b) -> the light one
    if m:
        v = m.group(1).strip()
    if v == "none":
        return "none"
    return v if _COLOR.match(v) else default


def _num(v, default):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _shape(st):
    s = st.get("shape") or st.get("_name") or ""
    if s.startswith("mxgraph.flowchart."):
        s = {"decision": "rhombus", "database": "cylinder", "document": "document", "terminator": "terminator", "start_1": "ellipse",
             "start_2": "ellipse", "on-page_reference": "ellipse", "process": "process", "data": "parallelogram", "predefined_process": "process",
             "manual_input": "manualInput", "delay": "delay", "extract_or_measurement": "triangleN", "merge_or_storage": "triangleS",
             "multi-document": "document", "off-page_reference": "offPage", "preparation": "hexagon", "or": "ellipse", "summing_function": "ellipse"
             }.get(s[18:], s)
    return s


# ---------------------------------------------------------------- HTML labels (strict allowlist)

_TAGS = {"b", "strong", "i", "em", "u", "s", "strike", "sub", "sup", "br", "div", "p", "span", "font", "ul", "ol", "li", "h1", "h2", "h3", "h4",
         "h5", "h6", "table", "tbody", "thead", "tr", "td", "th", "hr", "pre", "code", "img", "a", "blockquote", "small", "big"}
_VOID = {"br", "hr", "img"}
_DROP = {"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript", "textarea", "select", "head", "title"}
_CSS = {"color", "background-color", "background", "font-size", "font-weight", "font-style", "font-family", "text-decoration", "text-align",
        "line-height", "margin", "margin-top", "margin-bottom", "margin-left", "margin-right", "padding", "padding-left", "padding-right",
        "padding-top", "padding-bottom", "border", "border-top", "border-bottom", "border-left", "border-right", "width", "height",
        "vertical-align", "white-space", "letter-spacing", "border-collapse", "list-style", "display"}
_CSSVAL = re.compile(r"^[#\w\s.,%()'\"/-]{0,120}$")
_DATAIMG = re.compile(r"^data:image/(png|jpe?g|gif|webp|svg\+xml|bmp);base64,[A-Za-z0-9+/=\s]+$")


def _css(style):
    out = []
    for decl in (style or "").split(";"):
        if ":" not in decl:
            continue
        k, v = decl.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k in _CSS and _CSSVAL.match(v) and not re.search(r"url|expression|javascript|@|\\", v, re.I):
            out.append("%s:%s" % (k, v))
    return ";".join(out)


class _Clean(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.drop = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            if tag not in _VOID:
                self.drop += 1
            return
        if self.drop or tag not in _TAGS:
            return
        a = dict(attrs)
        style = _css(a.get("style"))
        if tag == "font":
            c = _color(a.get("color"), None)
            if c and c != "none":
                style = ("color:%s;" % c) + style
            tag = "span"
        if tag == "a":
            tag = "span"
            style = "text-decoration:underline;" + style
        if tag == "img":
            src = (a.get("src") or "").strip()
            if not _DATAIMG.match(src) or len(src) > MAX_IMG:
                return
            dims = "".join(' %s="%d"' % (k, _num(a.get(k), 0)) for k in ("width", "height") if _num(a.get(k), 0) > 0)
            self.out.append('<img src="%s"%s style="%s">' % (H.escape(src), dims, H.escape(style)))
            return
        self.out.append("<%s%s>" % (tag, ' style="%s"' % H.escape(style) if style else ""))
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in _VOID or tag in _TAGS:
            self.handle_starttag(tag, attrs)
            if tag not in _VOID and self.stack and self.stack[-1] == tag:
                self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _DROP:
            self.drop = max(0, self.drop - 1)
            return
        if self.drop:
            return
        tag = {"font": "span", "a": "span"}.get(tag, tag)
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append("</%s>" % t)
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.drop:
            self.out.append(H.escape(data))

    def result(self):
        self.close()
        return "".join(self.out) + "".join("</%s>" % t for t in reversed(self.stack))


def _label_html(value, st):
    if not value:
        return ""
    if st.get("html") == "1":
        c = _Clean()
        try:
            c.feed(value)
            return c.result()
        except Exception:                      # noqa: BLE001 - a broken label never breaks the page
            return H.escape(re.sub(r"<[^>]*>", "", value))
    return H.escape(value).replace("\n", "<br>")


# ---------------------------------------------------------------- geometry

class Cell:
    __slots__ = ("id", "parent", "vertex", "edge", "st", "value", "geo", "box", "src", "tgt", "pts", "sp", "tp", "path", "attrs", "visible", "rel", "offset")

    def __init__(self):
        self.box = None
        self.path = None


def _pt(e):
    return (_num(e.get("x"), 0), _num(e.get("y"), 0))


def _cells(model):
    root = model.find("root")
    if root is None:
        return []
    out = []
    for el in root:
        if len(out) >= MAX_CELLS:
            break
        attrs = {}
        if el.tag in ("UserObject", "object"):
            attrs = dict(el.attrib)
            mc = el.find("mxCell")
            if mc is None:
                mc = ET.Element("mxCell")
            cid, value = el.get("id"), el.get("label", "")
        elif el.tag == "mxCell":
            mc, cid, value = el, el.get("id"), el.get("value", "")
        else:
            continue
        c = Cell()
        c.id, c.parent, c.attrs = cid, mc.get("parent"), attrs
        c.vertex, c.edge = mc.get("vertex") == "1", mc.get("edge") == "1"
        c.st = _style(mc.get("style"))
        c.visible = mc.get("visible") != "0"
        if attrs and "1" in (c.st.get("placeholders"), attrs.get("placeholders")):
            value = re.sub(r"%(\w+)%", lambda m: attrs.get(m.group(1), m.group(0)) if m.group(1) != "label" else m.group(0), value)
        c.value = value
        c.src, c.tgt = mc.get("source"), mc.get("target")
        g = mc.find("mxGeometry")
        c.geo = (0.0, 0.0, 0.0, 0.0)
        c.rel, c.pts, c.sp, c.tp, c.offset = False, [], None, None, None
        if g is not None:
            c.geo = (_num(g.get("x"), 0), _num(g.get("y"), 0), _num(g.get("width"), 0), _num(g.get("height"), 0))
            c.rel = g.get("relative") == "1"
            for p in g:
                if p.tag == "mxPoint":
                    a = p.get("as")
                    if a == "sourcePoint":
                        c.sp = _pt(p)
                    elif a == "targetPoint":
                        c.tp = _pt(p)
                    elif a == "offset":
                        c.offset = _pt(p)
                elif p.tag == "Array" and p.get("as") == "points":
                    c.pts = [_pt(q) for q in p.findall("mxPoint")][:400]
        out.append(c)
    return out


def _origin(c, by, seen=None):
    """Absolute (x, y) of a cell's coordinate system = the top-left of its parent vertex (layers / root are 0, 0)."""
    p = by.get(c.parent)
    if p is None or not p.vertex:
        return (0.0, 0.0)
    b = _place(p, by, seen)
    return (b[0], b[1]) if b else (0.0, 0.0)


def _place(c, by, seen=None):
    if c.box is not None:
        return c.box
    seen = seen or set()
    if c.id in seen:                           # a parent cycle: give up on nesting
        return None
    seen.add(c.id)
    x, y, w, h = c.geo
    p = by.get(c.parent)
    if c.rel and p is not None and p.vertex:
        pb = _place(p, by, seen) or (0, 0, 0, 0)
        ox, oy = c.offset or (0, 0)
        c.box = (pb[0] + x * pb[2] + ox, pb[1] + y * pb[3] + oy, w, h)
    else:
        ox, oy = _origin(c, by, seen)
        c.box = (ox + x, oy + y, w, h)
    return c.box


def _inside(box, kind, p):
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return False
    dx, dy = (p[0] - x - w / 2) / (w / 2), (p[1] - y - h / 2) / (h / 2)
    if kind == "ellipse":
        return dx * dx + dy * dy <= 1
    if kind == "rhombus":
        return abs(dx) + abs(dy) <= 1
    return abs(dx) <= 1 and abs(dy) <= 1


def _clip(box, kind, frm, toward):
    """Where the segment frm -> toward leaves the shape (frm is usually inside it: the centre, or a point on a straight run)."""
    if not _inside(box, kind, frm) or _inside(box, kind, toward):
        return frm if not _inside(box, kind, frm) else toward
    a, b = frm, toward
    for _ in range(40):
        m = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        if _inside(box, kind, m):
            a = m
        else:
            b = m
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _port(box, st, pre):
    if pre + "X" not in st or pre + "Y" not in st:
        return None, None
    fx, fy = max(0.0, min(1.0, _num(st.get(pre + "X"), 0.5))), max(0.0, min(1.0, _num(st.get(pre + "Y"), 0.5)))
    x, y, w, h = box
    p = (x + fx * w + _num(st.get(pre + "Dx"), 0), y + fy * h + _num(st.get(pre + "Dy"), 0))
    d = "h" if fx in (0.0, 1.0) and fy not in (0.0, 1.0) else "v" if fy in (0.0, 1.0) else "h" if abs(fx - .5) >= abs(fy - .5) else "v"
    return p, d


def _route(c, by, kinds):
    """-> polyline [(x, y), ...] in absolute coordinates, or None."""
    ox, oy = _origin(c, by)
    s, t = by.get(c.src), by.get(c.tgt)
    sb = s.box if s is not None and s.vertex and s.box else None
    tb = t.box if t is not None and t.vertex and t.box else None
    if t is not None and t.edge and t.path:          # edge to edge: meet its middle
        tb, mid = None, _along(t.path, 0.5)
        c.tp = (mid[0] - ox, mid[1] - oy)
    if s is not None and s.edge and s.path:
        sb, mid = None, _along(s.path, 0.5)
        c.sp = (mid[0] - ox, mid[1] - oy)
    pts = [(px + ox, py + oy) for px, py in c.pts]
    st = c.st
    sp, sd = _port(sb, st, "exit") if sb else (None, None)
    tp, td = _port(tb, st, "entry") if tb else (None, None)
    start = sp or ((sb[0] + sb[2] / 2, sb[1] + sb[3] / 2) if sb else (c.sp[0] + ox, c.sp[1] + oy) if c.sp else None)
    end = tp or ((tb[0] + tb[2] / 2, tb[1] + tb[3] / 2) if tb else (c.tp[0] + ox, c.tp[1] + oy) if c.tp else None)
    if start is None or end is None:
        return None
    es = st.get("edgeStyle", "")
    ortho = es in ("orthogonalEdgeStyle", "elbowEdgeStyle", "entityRelationEdgeStyle", "segmentEdgeStyle", "isometricEdgeStyle")
    line = [start] + pts + [end]
    if ortho:
        if not pts and sb and tb and not sp and not tp:
            line = _ortho_boxes(sb, tb, st)
        elif not pts:
            dx, dy = end[0] - start[0], end[1] - start[1]
            if es == "elbowEdgeStyle":
                guess = "v" if st.get("elbow") == "vertical" else "h"
            else:
                guess = "h" if abs(dx) >= abs(dy) else "v"
            line = _elbow(start, end, sd or guess, td or guess)
        else:
            line = _ortho_points(line, sd or ("h" if abs(line[1][0] - line[0][0]) >= abs(line[1][1] - line[0][1]) else "v"))
    if sb and not sp and len(line) >= 2:
        line[0] = _clip(sb, kinds.get(c.src), line[0], line[1])
    if tb and not tp and len(line) >= 2:
        line[-1] = _clip(tb, kinds.get(c.tgt), line[-1], line[-2])
    return line


def _elbow(a, b, d1, d2):
    if d1 == "h" and d2 == "h":
        mx = (a[0] + b[0]) / 2
        return [a, (mx, a[1]), (mx, b[1]), b]
    if d1 == "v" and d2 == "v":
        my = (a[1] + b[1]) / 2
        return [a, (a[0], my), (b[0], my), b]
    return [a, (b[0], a[1]), b] if d1 == "h" else [a, (a[0], b[1]), b]


def _ortho_boxes(sb, tb, st):
    sx0, sy0, sx1, sy1 = sb[0], sb[1], sb[0] + sb[2], sb[1] + sb[3]
    tx0, ty0, tx1, ty1 = tb[0], tb[1], tb[0] + tb[2], tb[1] + tb[3]
    sc, tc = ((sx0 + sx1) / 2, (sy0 + sy1) / 2), ((tx0 + tx1) / 2, (ty0 + ty1) / 2)
    lo, hi = max(sx0, tx0), min(sx1, tx1)
    if hi - lo > 2 and st.get("edgeStyle") != "entityRelationEdgeStyle":     # stacked: one straight vertical line
        x = (lo + hi) / 2
        return [(x, sc[1]), (x, tc[1])]
    lo, hi = max(sy0, ty0), min(sy1, ty1)
    if hi - lo > 2:
        y = (lo + hi) / 2
        return [(sc[0], y), (tc[0], y)]
    if st.get("edgeStyle") == "elbowEdgeStyle":
        d = "v" if st.get("elbow") == "vertical" else "h"
    elif st.get("edgeStyle") == "entityRelationEdgeStyle":
        d = "h"
    else:
        d = "h" if abs(tc[0] - sc[0]) >= abs(tc[1] - sc[1]) else "v"
    return _elbow(sc, tc, d, d)


def _ortho_points(line, d):
    out = [line[0]]
    for b in line[1:]:
        a = out[-1]
        if abs(a[0] - b[0]) > .5 and abs(a[1] - b[1]) > .5:
            out.append((b[0], a[1]) if d == "h" else (a[0], b[1]))
            d = "v" if d == "h" else "h"
        else:
            d = "v" if abs(a[0] - b[0]) <= .5 else "h"
            d = "h" if d == "v" else "v"
        out.append(b)
    return out


def _along(line, f):
    segs = [(a, b, math.hypot(b[0] - a[0], b[1] - a[1])) for a, b in zip(line, line[1:])]
    total = sum(s[2] for s in segs)
    if total <= 0:
        return line[0]
    d = max(0.0, min(1.0, f)) * total
    for a, b, n in segs:
        if d <= n and n > 0:
            k = d / n
            return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)
        d -= n
    return line[-1]


# ---------------------------------------------------------------- drawing

def _f(v):
    return ("%.2f" % v).rstrip("0").rstrip(".")


def _paint(st, fill_default="#ffffff", stroke_default="#000000"):
    fill = _color(st.get("fillColor"), fill_default)
    stroke = _color(st.get("strokeColor"), stroke_default)
    sw = max(0.0, min(40.0, _num(st.get("strokeWidth"), 1)))
    a = ['fill="%s"' % H.escape(fill), 'stroke="%s"' % H.escape(stroke), 'stroke-width="%s"' % _f(sw)]
    if st.get("dashed") == "1":
        pat = st.get("dashPattern", "")
        a.append('stroke-dasharray="%s"' % (H.escape(pat) if re.match(r"^[\d. ]{1,40}$", pat) else "%s %s" % (_f(3 * sw), _f(3 * sw))))
    for k, n in (("fillOpacity", "fill-opacity"), ("strokeOpacity", "stroke-opacity")):
        if k in st:
            a.append('%s="%s"' % (n, _f(max(0, min(100, _num(st[k], 100))) / 100)))
    return " ".join(a), fill, stroke, sw


def _poly(pts):
    return '<path d="M%s Z" %%s/>' % " L".join("%s,%s" % (_f(x), _f(y)) for x, y in pts)


def _cloud(x, y, w, h):
    p = [(.25, .25), (.4, .05), (.62, .1), (.8, .05), (.97, .3), (.95, .65), (.78, .92), (.5, .97), (.22, .92), (.03, .7), (.05, .35)]
    q = [(x + a * w, y + b * h) for a, b in p]
    d = "M%s,%s" % (_f(q[0][0]), _f(q[0][1]))
    for i in range(1, len(q) + 1):
        a, b = q[i - 1], q[i % len(q)]
        r = math.hypot(b[0] - a[0], b[1] - a[1]) * 0.6
        d += " A%s,%s 0 0 1 %s,%s" % (_f(r), _f(r), _f(b[0]), _f(b[1]))
    return '<path d="%s Z" %%s/>' % d


def _shape_svg(kind, st, box):
    x, y, w, h = box
    paint, fill, stroke, sw = _paint(st)
    if kind in ("text", "edgeLabel", "group", "tableRow"):
        if kind == "tableRow" or (st.get("fillColor") in (None, "none") and st.get("strokeColor") in (None, "none")):
            return ""
        return '<rect x="%s" y="%s" width="%s" height="%s" %s/>' % (_f(x), _f(y), _f(w), _f(h), _paint(st, "none", "none")[0])
    if kind == "image":
        return _image(st, box)
    if kind in ("ellipse", "doubleEllipse") or st.get("ellipse") == "1":
        s = '<ellipse cx="%s" cy="%s" rx="%s" ry="%s" %s/>' % (_f(x + w / 2), _f(y + h / 2), _f(w / 2), _f(h / 2), paint)
        if kind == "doubleEllipse":
            s += '<ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="none" stroke="%s" stroke-width="%s"/>' % (
                _f(x + w / 2), _f(y + h / 2), _f(max(0, w / 2 - 4)), _f(max(0, h / 2 - 4)), H.escape(stroke), _f(sw))
        return s
    if kind == "rhombus":
        return _poly([(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)]) % paint
    if kind in ("triangle", "triangleN", "triangleS"):
        d = {"triangleN": "north", "triangleS": "south"}.get(kind, st.get("direction", "east"))
        p = {"north": [(x, y + h), (x + w / 2, y), (x + w, y + h)], "south": [(x, y), (x + w, y), (x + w / 2, y + h)],
             "west": [(x + w, y), (x, y + h / 2), (x + w, y + h)]}.get(d, [(x, y), (x + w, y + h / 2), (x, y + h)])
        return _poly(p) % paint
    if kind == "hexagon":
        s = min(w, h) * .25 if "size" not in st else _num(st["size"], 0.25) * (w if _num(st["size"], 0.25) < 1 else 1)
        s = min(s, w / 2)
        return _poly([(x + s, y), (x + w - s, y), (x + w, y + h / 2), (x + w - s, y + h), (x + s, y + h), (x, y + h / 2)]) % paint
    if kind in ("parallelogram", "trapezoid", "manualInput"):
        s = w * min(.45, _num(st.get("size"), .2) if _num(st.get("size"), .2) < 1 else _num(st.get("size"), 20) / max(w, 1))
        p = {"parallelogram": [(x + s, y), (x + w, y), (x + w - s, y + h), (x, y + h)], "trapezoid": [(x + s, y), (x + w - s, y), (x + w, y + h), (x, y + h)],
             "manualInput": [(x, y + h * .25), (x + w, y), (x + w, y + h), (x, y + h)]}[kind]
        return _poly(p) % paint
    if kind == "step":
        s = min(w / 2, w * .2)
        return _poly([(x, y), (x + w - s, y), (x + w, y + h / 2), (x + w - s, y + h), (x, y + h), (x + s, y + h / 2)]) % paint
    if kind == "offPage":
        return _poly([(x, y), (x + w, y), (x + w, y + h * .6), (x + w / 2, y + h), (x, y + h * .6)]) % paint
    if kind in ("cylinder", "cylinder3", "datastore"):
        ry = min(h / 3, _num(st.get("size"), 15) if kind == "cylinder3" else min(15, h * .1))
        rx = w / 2
        body = ('<path d="M%s,%s A%s,%s 0 0 1 %s,%s L%s,%s A%s,%s 0 0 1 %s,%s Z" %s/>' % (
            _f(x), _f(y + ry), _f(rx), _f(ry), _f(x + w), _f(y + ry), _f(x + w), _f(y + h - ry), _f(rx), _f(ry), _f(x), _f(y + h - ry), paint))
        return body + '<path d="M%s,%s A%s,%s 0 0 0 %s,%s" fill="none" stroke="%s" stroke-width="%s"/>' % (
            _f(x), _f(y + ry), _f(rx), _f(ry), _f(x + w), _f(y + ry), H.escape(stroke), _f(sw))
    if kind == "document":
        s = h * .15
        return '<path d="M%s,%s H%s V%s Q%s,%s %s,%s T%s,%s Z" %s/>' % (
            _f(x), _f(y), _f(x + w), _f(y + h - s), _f(x + w * .75), _f(y + h - 2 * s), _f(x + w / 2), _f(y + h - s), _f(x), _f(y + h - s), paint)
    if kind == "note":
        s = min(_num(st.get("size"), 30), w / 2, h / 2)
        return (_poly([(x, y), (x + w - s, y), (x + w, y + s), (x + w, y + h), (x, y + h)]) % paint +
                '<path d="M%s,%s V%s H%s" fill="none" stroke="%s" stroke-width="%s"/>' % (_f(x + w - s), _f(y), _f(y + s), _f(x + w), H.escape(stroke), _f(sw)))
    if kind == "card":
        s = min(_num(st.get("size"), 30), w / 2, h / 2)
        return _poly([(x + s, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y + s)]) % paint
    if kind == "cloud":
        return _cloud(x, y, w, h) % paint
    if kind == "terminator":
        r = min(w, h) / 2
        return '<rect x="%s" y="%s" width="%s" height="%s" rx="%s" %s/>' % (_f(x), _f(y), _f(w), _f(h), _f(r), paint)
    if kind == "delay":
        r = min(w / 2, h / 2)
        return '<path d="M%s,%s H%s A%s,%s 0 0 1 %s,%s H%s Z" %s/>' % (_f(x), _f(y), _f(x + w - r), _f(r), _f(h / 2), _f(x + w - r), _f(y + h), _f(x), paint)
    if kind in ("actor", "umlActor"):
        hr = min(w, h) / 6 if kind == "umlActor" else min(w / 4, h / 4)
        cx = x + w / 2
        line = 'fill="none" stroke="%s" stroke-width="%s"' % (H.escape(stroke), _f(sw))
        if kind == "actor":
            return ('<circle cx="%s" cy="%s" r="%s" %s/>' % (_f(cx), _f(y + hr), _f(hr), paint) +
                    '<path d="M%s,%s Q%s,%s %s,%s H%s Q%s,%s %s,%s Z" %s/>' % (
                        _f(x), _f(y + h), _f(x), _f(y + 2.2 * hr), _f(cx), _f(y + 2.2 * hr), _f(cx), _f(x + w), _f(y + 2.2 * hr), _f(x + w), _f(y + h), paint))
        top = y + 2 * hr
        return ('<circle cx="%s" cy="%s" r="%s" %s/>' % (_f(cx), _f(y + hr), _f(hr), paint) +
                '<path d="M%s,%s V%s M%s,%s H%s M%s,%s L%s,%s L%s,%s" %s/>' % (
                    _f(cx), _f(top), _f(y + h * .66), _f(x), _f(top + h * .12), _f(x + w), _f(x), _f(y + h), _f(cx), _f(y + h * .66), _f(x + w), _f(y + h), line))
    if kind == "line":
        return '<path d="M%s,%s H%s" fill="none" stroke="%s" stroke-width="%s"/>' % (_f(x), _f(y + h / 2), _f(x + w), H.escape(stroke), _f(sw))
    if kind == "partialRectangle":
        s = '<rect x="%s" y="%s" width="%s" height="%s" %s/>' % (_f(x), _f(y), _f(w), _f(h), paint.replace('stroke="%s"' % H.escape(stroke), 'stroke="none"'))
        sides = [("top", (x, y, x + w, y)), ("right", (x + w, y, x + w, y + h)), ("bottom", (x, y + h, x + w, y + h)), ("left", (x, y, x, y + h))]
        for k, (a, b, c2, d) in sides:
            if st.get(k, "1") != "0":
                s += '<path d="M%s,%s L%s,%s" stroke="%s" stroke-width="%s"/>' % (_f(a), _f(b), _f(c2), _f(d), H.escape(stroke), _f(sw))
        return s
    if kind == "swimlane":
        return _swimlane(st, box)
    rx = ""
    if st.get("rounded") == "1":
        r = _num(st.get("arcSize"), 10) if st.get("absoluteArcSize") == "1" else min(w, h) * _num(st.get("arcSize"), 15) / 100
        rx = ' rx="%s"' % _f(max(0, min(r, w / 2, h / 2)))
    s = '<rect x="%s" y="%s" width="%s" height="%s"%s %s/>' % (_f(x), _f(y), _f(w), _f(h), rx, paint)
    if kind == "process":
        s += '<path d="M%s,%s V%s M%s,%s V%s" stroke="%s" stroke-width="%s"/>' % (
            _f(x + w * .1), _f(y), _f(y + h), _f(x + w * .9), _f(y), _f(y + h), H.escape(stroke), _f(sw))
    return s


def _swimlane(st, box):
    x, y, w, h = box
    paint, fill, stroke, sw = _paint(st)
    hz = st.get("horizontal", "1") != "0"
    s = max(0.0, min(_num(st.get("startSize"), 23), h if hz else w))
    body = _color(st.get("swimlaneFillColor"), "none")
    rx = ' rx="4"' if st.get("rounded") == "1" else ""
    out = '<rect x="%s" y="%s" width="%s" height="%s"%s fill="%s" stroke="%s" stroke-width="%s"/>' % (
        _f(x), _f(y), _f(w), _f(h), rx, H.escape(body), H.escape(stroke), _f(sw))
    hb = (x, y, w, s) if hz else (x, y, s, h)
    out += '<rect x="%s" y="%s" width="%s" height="%s"%s %s/>' % (_f(hb[0]), _f(hb[1]), _f(hb[2]), _f(hb[3]), rx, paint)
    return out


def _image(st, box):
    x, y, w, h = box
    src = st.get("image", "")
    m = re.match(r"^data:image/([\w.+-]+),(.*)$", src, re.S)      # draw.io writes "data:image/png,<b64>" (';' is the style separator)
    if m and ";base64" not in m.group(1):
        src = "data:image/%s;base64,%s" % (m.group(1), m.group(2))
    if not _DATAIMG.match(src) or len(src) > MAX_IMG:
        return ('<rect x="%s" y="%s" width="%s" height="%s" fill="#f0f0f3" stroke="#b0b0b8" stroke-dasharray="4 3"/>'
                '<path d="M%s,%s L%s,%s M%s,%s L%s,%s" stroke="#c8c8d0"/>' % (_f(x), _f(y), _f(w), _f(h), _f(x), _f(y), _f(x + w), _f(y + h), _f(x + w), _f(y), _f(x), _f(y + h)))
    asp = "xMidYMid meet" if st.get("imageAspect", "1") != "0" else "none"
    return '<image x="%s" y="%s" width="%s" height="%s" preserveAspectRatio="%s" href="%s"/>' % (_f(x), _f(y), _f(w), _f(h), asp, H.escape(src))


_MARK = {"classic": ("M0,0 L10,5 L0,10 L3,5 Z", 10), "classicThin": ("M0,1 L10,5 L0,9 L3,5 Z", 10), "block": ("M0,0 L10,5 L0,10 Z", 10),
         "blockThin": ("M0,1 L10,5 L0,9 Z", 10), "open": ("M0,0 L10,5 L0,10", 10), "openThin": ("M0,1 L10,5 L0,9", 10), "async": ("M0,0 L10,5 L3,5 Z", 10),
         "oval": ("M1,5 A4,4 0 1 1 9,5 A4,4 0 1 1 1,5 Z", 5), "diamond": ("M0,5 L5,0 L10,5 L5,10 Z", 10), "diamondThin": ("M0,5 L5,2 L10,5 L5,8 Z", 10),
         "box": ("M1,1 H9 V9 H1 Z", 9), "dash": ("M5,0 L5,10", 5), "cross": ("M2,2 L8,8 M8,2 L2,8", 5), "circlePlus": ("M1,5 A4,4 0 1 1 9,5 A4,4 0 1 1 1,5 Z M5,1 V9 M1,5 H9", 9),
         "ERone": ("M6,0 V10", 10), "ERmandOne": ("M4,0 V10 M7,0 V10", 10), "ERmany": ("M10,0 L0,5 L10,10", 10), "ERoneToMany": ("M10,0 L0,5 L10,10 M0,0 V10", 10),
         "ERzeroToOne": ("M5,0 V10 M0,5 A2.5,2.5 0 1 1 0,5.01", 10), "ERzeroToMany": ("M10,0 L4,5 L10,10", 10)}
_OPEN = {"open", "openThin", "dash", "cross", "ERone", "ERmandOne", "ERmany", "ERoneToMany", "ERzeroToOne", "ERzeroToMany"}


class _Defs:
    def __init__(self, pid):
        self.pid, self.ids, self.out = pid, {}, []

    def marker(self, kind, color, filled, size):
        if not kind or kind == "none":
            return None
        kind = kind if kind in _MARK else "classic"
        key = (kind, color, filled, round(size, 1))
        if key not in self.ids:
            mid = "m%s_%d" % (self.pid, len(self.ids))
            self.ids[key] = mid
            d, ref = _MARK[kind]
            open_ = kind in _OPEN
            fill = "none" if open_ else (H.escape(color) if filled else "#ffffff")
            self.out.append('<marker id="%s" viewBox="-1 -1 12 12" refX="%s" refY="5" markerWidth="%s" markerHeight="%s" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
                            '<path d="%s" fill="%s" stroke="%s" stroke-width="1.2" stroke-linejoin="round"/></marker>' % (
                                mid, ref, _f(size * 1.2), _f(size * 1.2), d, fill, H.escape(color)))
        return self.ids[key]


def _edge_svg(c, defs):
    st, line = c.st, c.path
    stroke = _color(st.get("strokeColor"), "#000000")
    if stroke == "none" or len(line) < 2:
        return ""
    sw = max(0.0, min(40.0, _num(st.get("strokeWidth"), 1)))
    if st.get("curved") == "1" and len(line) > 2:
        d = "M%s,%s" % (_f(line[0][0]), _f(line[0][1]))
        for i in range(1, len(line) - 1):
            mx, my = (line[i][0] + line[i + 1][0]) / 2, (line[i][1] + line[i + 1][1]) / 2
            if i == len(line) - 2:
                mx, my = line[-1]
            d += " Q%s,%s %s,%s" % (_f(line[i][0]), _f(line[i][1]), _f(mx), _f(my))
    elif st.get("rounded") == "1" and len(line) > 2:
        d = _rounded(line, 10)
    else:
        d = "M" + " L".join("%s,%s" % (_f(x), _f(y)) for x, y in line)
    a = ['d="%s"' % d, 'fill="none"', 'stroke="%s"' % H.escape(stroke), 'stroke-width="%s"' % _f(sw), 'stroke-linejoin="round"']
    if st.get("dashed") == "1":
        pat = st.get("dashPattern", "")
        a.append('stroke-dasharray="%s"' % (H.escape(pat) if re.match(r"^[\d. ]{1,40}$", pat) else "%s %s" % (_f(3 * sw), _f(3 * sw))))
    if "opacity" in st:
        a.append('opacity="%s"' % _f(max(0, min(100, _num(st["opacity"], 100))) / 100))
    end = defs.marker(st.get("endArrow", "classic"), stroke, st.get("endFill", "1") != "0", _num(st.get("endSize"), 6) + sw * 1.5)
    start = defs.marker(st.get("startArrow", "none"), stroke, st.get("startFill", "1") != "0", _num(st.get("startSize"), 6) + sw * 1.5)
    if end:
        a.append('marker-end="url(#%s)"' % end)
    if start:
        a.append('marker-start="url(#%s)"' % start)
    return "<path %s/>" % " ".join(a)


def _rounded(line, r):
    d = "M%s,%s" % (_f(line[0][0]), _f(line[0][1]))
    for i in range(1, len(line) - 1):
        p, q, n = line[i - 1], line[i], line[i + 1]
        l1, l2 = math.hypot(q[0] - p[0], q[1] - p[1]), math.hypot(n[0] - q[0], n[1] - q[1])
        k = min(r, l1 / 2, l2 / 2)
        if k <= 0.1:
            d += " L%s,%s" % (_f(q[0]), _f(q[1]))
            continue
        a = (q[0] - (q[0] - p[0]) * k / l1, q[1] - (q[1] - p[1]) * k / l1)
        b = (q[0] + (n[0] - q[0]) * k / l2, q[1] + (n[1] - q[1]) * k / l2)
        d += " L%s,%s Q%s,%s %s,%s" % (_f(a[0]), _f(a[1]), _f(q[0]), _f(q[1]), _f(b[0]), _f(b[1]))
    return d + " L%s,%s" % (_f(line[-1][0]), _f(line[-1][1]))


_ALIGN = {"left": "flex-start", "center": "center", "right": "flex-end", "top": "flex-start", "middle": "center", "bottom": "flex-end"}


def _font_css(st, edge=False):
    size = max(1.0, min(200.0, _num(st.get("fontSize"), 11 if edge else 12)))
    fs = int(_num(st.get("fontStyle"), 0)) if re.match(r"^\d+$", st.get("fontStyle", "0") or "0") else 0
    fam = st.get("fontFamily", "Helvetica")
    fam = fam if re.match(r"^[\w ,.-]{1,80}$", fam) else "Helvetica"
    css = ["font-size:%spx" % _f(size), "font-family:%s,Arial,sans-serif" % fam, "color:%s" % _color(st.get("fontColor"), "#000000"), "line-height:1.2"]
    if fs & 1:
        css.append("font-weight:bold")
    if fs & 2:
        css.append("font-style:italic")
    deco = [n for b, n in ((4, "underline"), (8, "line-through")) if fs & b]
    if deco:
        css.append("text-decoration:%s" % " ".join(deco))
    return css


def _label(c, box, body, edge=False):
    """A label as a foreignObject so the browser wraps and aligns the text like draw.io does."""
    if not body:
        return ""
    st = c.st
    x, y, w, h = box
    sp = _num(st.get("spacing"), 2)
    l, r = sp + _num(st.get("spacingLeft"), 0), sp + _num(st.get("spacingRight"), 0)
    t, b = sp + _num(st.get("spacingTop"), 0), sp + _num(st.get("spacingBottom"), 0)
    lp, vp = st.get("labelPosition", "center"), st.get("verticalLabelPosition", "middle")
    if lp == "left":
        x -= w
    elif lp == "right":
        x += w
    if vp == "top":
        y -= h
    elif vp == "bottom":
        y += h
    align = st.get("align", "center")
    valign = st.get("verticalAlign", "middle")
    kind = _shape(st)
    if kind == "swimlane" and not edge:
        if st.get("horizontal", "1") != "0":
            h = max(0.0, min(_num(st.get("startSize"), 23), h))
        valign = st.get("verticalAlign", "middle")
    wrap = st.get("whiteSpace") == "wrap" and st.get("html") == "1"
    css = _font_css(st, edge) + ["text-align:%s" % (align if align in ("left", "right", "center") else "center")]
    css.append("white-space:%s" % ("normal" if wrap else "nowrap"))
    bg = _color(st.get("labelBackgroundColor"), "none")
    if bg == "none" and edge and c.edge:
        bg = "#ffffff"
    if bg != "none":
        css.append("background:%s" % bg)
    bc = _color(st.get("labelBorderColor"), "none")
    if bc != "none":
        css.append("border:1px solid %s" % bc)
    if "textOpacity" in st:
        css.append("opacity:%s" % _f(max(0, min(100, _num(st["textOpacity"], 100))) / 100))
    if edge:
        fw, fh = 600.0, 400.0
        fx, fy = x - fw / 2, y - fh / 2
        outer = "align-items:center;justify-content:center"
    else:
        fx, fy, fw, fh = x + l, y + t, max(1.0, w - l - r), max(1.0, h - t - b)
        outer = "align-items:%s;justify-content:%s" % (_ALIGN.get(valign, "center"), _ALIGN.get(align, "center"))
    rot = ""
    if kind == "swimlane" and st.get("horizontal", "1") == "0" and not edge:
        s = max(0.0, min(_num(st.get("startSize"), 23), w))
        cx, cy = box[0] + s / 2, box[1] + box[3] / 2
        fw, fh = box[3], s
        fx, fy = cx - fw / 2, cy - fh / 2
        rot = ' transform="rotate(-90 %s %s)"' % (_f(cx), _f(cy))
        outer = "align-items:center;justify-content:center"
    elif st.get("horizontal") == "0" and not edge:
        cx, cy = fx + fw / 2, fy + fh / 2
        fx, fy, fw, fh = cx - fh / 2, cy - fw / 2, fh, fw
        rot = ' transform="rotate(-90 %s %s)"' % (_f(cx), _f(cy))
    return ('<foreignObject x="%s" y="%s" width="%s" height="%s" style="overflow:visible"%s><div xmlns="http://www.w3.org/1999/xhtml" class="lb" style="%s">'
            '<div style="%s">%s</div></div></foreignObject>' % (_f(fx), _f(fy), _f(fw), _f(fh), rot, outer, H.escape(";".join(css)), body))


def _svg(model, pid):
    cells = _cells(model)
    by = {c.id: c for c in cells if c.id}
    kinds = {}
    hidden = set()
    for c in cells:                                         # a hidden layer / group hides what is inside it
        p, n = c, 0
        while p is not None and n < 64:
            if not p.visible:
                hidden.add(c.id)
                break
            p, n = by.get(p.parent), n + 1
    for c in cells:
        if c.vertex:
            p = by.get(c.parent)
            if not (p is not None and p.edge):
                _place(c, by)
            kinds[c.id] = "rhombus" if _shape(c.st) == "rhombus" else "ellipse" if _shape(c.st) in ("ellipse", "doubleEllipse") or c.st.get("ellipse") == "1" else "rect"
    pending = [c for c in cells if c.edge]
    for _ in range(3):                                      # edges that end on edges need those routed first
        left = []
        for c in pending:
            t, s = by.get(c.tgt), by.get(c.src)
            if ((t is not None and t.edge and t.path is None) or (s is not None and s.edge and s.path is None)) and _ < 2:
                left.append(c)
                continue
            c.path = _route(c, by, kinds) or None
        pending = left
    for c in cells:                                         # labels sitting on an edge
        p = by.get(c.parent)
        if c.vertex and p is not None and p.edge:
            if p.path:
                pt = _along(p.path, (c.geo[0] + 1) / 2)
                ox, oy = c.offset or (0, 0)
                w, h = c.geo[2], c.geo[3]
                c.box = (pt[0] + ox - w / 2, pt[1] + c.geo[1] + oy - h / 2, w, h)
                c.rel = "edgechild"
    xs, ys = [], []
    defs, parts = _Defs(pid), []
    for c in cells:
        if c.id in hidden:
            continue
        if c.vertex and c.box:
            kind = _shape(c.st)
            body = _label_html(c.value, c.st)
            if c.rel == "edgechild":
                x, y, w, h = c.box
                parts.append(_label(c, (x + w / 2, y + h / 2, 0, 0), body, edge=True))
                xs += [x, x + w]
                ys += [y, y + h]
                continue
            x, y, w, h = c.box
            xs += [x, x + w]
            ys += [y, y + h]
            g = _shape_svg(kind, c.st, c.box) + _label(c, c.box, body)
            tr = []
            rot = _num(c.st.get("rotation"), 0)
            if rot:
                tr.append('transform="rotate(%s %s %s)"' % (_f(rot), _f(x + w / 2), _f(y + h / 2)))
            if "opacity" in c.st:
                tr.append('opacity="%s"' % _f(max(0, min(100, _num(c.st["opacity"], 100))) / 100))
            if c.st.get("shadow") == "1":
                tr.append('filter="url(#sh%s)"' % pid)
            parts.append("<g %s>%s</g>" % (" ".join(tr), g) if tr else g)
        elif c.edge and c.path:
            parts.append(_edge_svg(c, defs))
            xs += [p[0] for p in c.path]
            ys += [p[1] for p in c.path]
            body = _label_html(c.value, c.st)
            if body:
                ox, oy = c.offset or (0, 0)
                pt = _along(c.path, (c.geo[0] + 1) / 2) if c.rel else _along(c.path, 0.5)
                parts.append(_label(c, (pt[0] + ox, pt[1] + oy + (c.geo[1] if not c.rel else 0), 0, 0), body, edge=True))
    if not xs:
        return None, 0
    m = 12
    x0, y0, x1, y1 = min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m
    w, h = max(40.0, x1 - x0), max(40.0, y1 - y0)
    bg = _color(model.get("background"), "#ffffff")
    bg = "#ffffff" if bg == "none" else bg
    shadow = '<filter id="sh%s" x="-10%%" y="-10%%" width="130%%" height="130%%"><feDropShadow dx="2" dy="3" stdDeviation="1.5" flood-opacity=".25"/></filter>' % pid
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s %s %s %s" width="%s" height="%s" style="background:%s">'
           '<defs>%s%s</defs>%s</svg>' % (_f(x0), _f(y0), _f(w), _f(h), _f(w), _f(h), H.escape(bg), shadow, "".join(defs.out), "".join(parts)))
    return svg, len(cells)


# ---------------------------------------------------------------- page

def page(path):
    """-> HTML page for a .drawio / .dio file. ValueError carries a message fit to show the person."""
    if os.path.getsize(path) > MAX_FILE:
        raise ValueError("this diagram is too large to preview (over %d MB)" % (MAX_FILE // 1024 // 1024))
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", "replace").lstrip("﻿")
    try:
        root = _parse_xml(text)
        pages = _pages(root)
        tabs, panes = [], []
        for i, (name, model) in enumerate(pages):
            svg, n = _svg(model, i)
            title = H.escape(name[:120])
            tabs.append("<button data-i='%d'%s>%s</button>" % (i, " class='on'" if not i else "", title))
            inner = ("<div class='wrap'><div class='board' title='Click to toggle fit / actual size'>%s</div></div>" % svg) if svg else "<p class='note'>This page is empty.</p>"
            panes.append("<div class='pg' data-i='%d'%s>%s%s</div>" % (i, "" if not i else " hidden", inner,
                                                                    "<p class='note'>Showing the first %d shapes.</p>" % MAX_CELLS if n >= MAX_CELLS else ""))
    except (ET.ParseError, AttributeError, TypeError, KeyError, IndexError, RecursionError, zlib.error) as e:
        raise ValueError("could not read this draw.io file (%s)" % e.__class__.__name__)
    name = H.escape(os.path.basename(path))
    return PAGE % (name, "<div class='tabs'>%s</div>" % "".join(tabs) if len(tabs) > 1 else "", "".join(panes) + JS)
