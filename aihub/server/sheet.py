"""Read a spreadsheet upload (CSV/TSV or simple .xlsx) into rows of strings. Stdlib only."""
import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET

MAX_BYTES = 2 * 1024 * 1024
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _col(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    if sum(i.file_size for i in z.infolist()) > 20 * 1024 * 1024:
        raise ValueError("spreadsheet too large")
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    if not sheets:
        raise ValueError("no worksheet found")
    rows = []
    for row in ET.fromstring(z.read(sheets[0])).iter("{%s}row" % NS["m"]):
        cells = []
        for c in row.findall("m:c", NS):
            idx = _col(c.get("r", "A1"))
            while len(cells) < idx:
                cells.append("")
            t = c.get("t")
            if t == "inlineStr":
                v = "".join(x.text or "" for x in c.iter("{%s}t" % NS["m"]))
            else:
                e = c.find("m:v", NS)
                v = (e.text or "") if e is not None else ""
                if t == "s" and v.isdigit() and int(v) < len(shared):
                    v = shared[int(v)]
            cells.append(v.strip())
        rows.append(cells)
    return rows


def parse(data, filename=""):
    if len(data) > MAX_BYTES:
        raise ValueError("file too large (max 2 MB)")
    if data[:2] == b"PK" or filename.lower().endswith(".xlsx"):
        try:
            return _xlsx(data)
        except (zipfile.BadZipFile, ET.ParseError, KeyError):
            raise ValueError("could not read the .xlsx file")
    text = data.decode("utf-8-sig", "replace")
    first = text.split("\n", 1)[0]
    delim = max([",", ";", "\t"], key=first.count) if any(d in first for d in ",;\t") else ","
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]


def safe_cell(v):
    """Neutralise spreadsheet formula injection when echoing user-supplied text back in a sheet."""
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
