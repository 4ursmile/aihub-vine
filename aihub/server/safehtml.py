"""Allowlist HTML sanitizer for admin-written text (the announcement banner). Standard library only.
Anything not listed is dropped (tags are removed but their text stays; script/style content is removed). Links must be
http(s)/mailto and always open with rel=noopener; no inline styles, classes, ids or event handlers survive."""
import html
import re
from html.parser import HTMLParser

TAGS = {"a", "b", "strong", "i", "em", "u", "s", "br", "span", "code", "small", "mark"}
VOID = {"br"}
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "svg", "math", "noscript", "textarea", "title"}
SAFE_URL = re.compile(r"^(https?://|mailto:|/|#)", re.I)
MAX = 2000


class _S(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.open, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in TAGS:
            return
        if tag == "a":
            href = next((v for k, v in attrs if k == "href" and v), "")
            href = re.sub(r"[\x00-\x20]+", "", href)
            if not SAFE_URL.match(href):
                self.out.append("<a>")
            else:
                self.out.append('<a href="%s" target="_blank" rel="noopener noreferrer">' % html.escape(href, quote=True))
        else:
            self.out.append("<%s>" % tag)
        if tag not in VOID:
            self.open.append(tag)

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in TAGS or tag in VOID or tag not in self.open:
            return
        while self.open:                                   # close anything left open inside it, in order
            t = self.open.pop()
            self.out.append("</%s>" % t)
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))


def clean(text, limit=MAX):
    p = _S()
    p.feed(str(text or "")[: limit * 2])
    p.close()
    while p.open:
        p.out.append("</%s>" % p.open.pop())
    return "".join(p.out).strip()[:limit]
