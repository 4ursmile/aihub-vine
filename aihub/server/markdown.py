"""Tiny safe Markdown -> HTML. All text is escaped first; no raw HTML passes through."""
import html
import re


def _inline(s):
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)

    def link(m):
        url = html.unescape(m.group(2)).strip()
        if not re.match(r"^(https?://|/|#|mailto:)", url):
            return m.group(1)
        return '<a href="%s" rel="noopener nofollow">%s</a>' % (html.escape(url, quote=True), m.group(1))
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, s)


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_sep(line):
    return bool(re.match(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", line)) and "-" in line


def render(text: str) -> str:
    out, lines, i, in_list, para = [], text.replace("\r\n", "\n").split("\n"), 0, None, []

    def flush_para():
        if para:
            out.append("<p>%s</p>" % _inline(" ".join(x.strip() for x in para)))
            para.clear()

    def close():
        nonlocal in_list
        flush_para()
        if in_list:
            out.append("</%s>" % in_list)
            in_list = None

    while i < len(lines):
        l = lines[i]
        if l.startswith("```"):
            close(); i += 1; buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i]); i += 1
            out.append("<pre><code>%s</code></pre>" % html.escape("\n".join(buf)))
        elif re.match(r"^#{1,6} ", l):
            close(); n = len(l) - len(l.lstrip("#"))
            out.append("<h%d>%s</h%d>" % (n, _inline(l[n + 1:].strip()), n))
        elif re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", l):
            close(); out.append("<hr>")
        elif "|" in l and i + 1 < len(lines) and _is_sep(lines[i + 1]):
            close(); head = _cells(l); i += 2; rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i])); i += 1
            out.append("<div class='tscroll'><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>" % (
                "".join("<th>%s</th>" % _inline(c) for c in head),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in r) for r in rows)))
            continue
        elif l.startswith(">"):
            close(); buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i].lstrip("> ")); i += 1
            out.append("<blockquote>%s</blockquote>" % _inline(" ".join(buf)))
            continue
        elif re.match(r"^\s*[-*] ", l):
            flush_para()
            if in_list != "ul": close(); out.append("<ul>"); in_list = "ul"
            out.append("<li>%s</li>" % _inline(re.sub(r"^\s*[-*] ", "", l)))
        elif re.match(r"^\s*\d+\. ", l):
            flush_para()
            if in_list != "ol": close(); out.append("<ol>"); in_list = "ol"
            out.append("<li>%s</li>" % _inline(re.sub(r"^\s*\d+\. ", "", l)))
        elif l.strip():
            if in_list and l.startswith("  "):   # list continuation
                out[-1] = out[-1][:-5] + " " + _inline(l.strip()) + "</li>"
            else:
                if in_list: close()
                para.append(l)
        else:
            close()
        i += 1
    close()
    return "\n".join(out)
