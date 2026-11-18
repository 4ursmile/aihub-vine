"""MongoDB for Data Studio. Runs inside sqlrunner.py (the connection's Python, `-I`), so only the standard library plus pymongo / bson.

A statement is written the way the mongo shell does it, or as plain JSON:

    db.orders.find({"status": "paid"}, {"total": 1}).sort({"total": -1}).limit(20)
    db.orders.aggregate([{"$group": {"_id": "$region", "revenue": {"$sum": "$total"}}}])
    db.orders.countDocuments({...})   db.orders.distinct("region")   show collections
    {"collection": "orders", "find": {...}, "sort": {...}, "limit": 20}      {"collection": "orders", "aggregate": [...]}

Shell extras are understood: unquoted keys, 'single quotes', trailing commas, /regex/i, ObjectId("..."), ISODate("..."), new Date("..."), NumberLong(..).
Documents come back as a table: nested objects are flattened to `a.b` columns (two levels), arrays stay JSON text."""
import json
import re
import time

WRITES = {"insertOne", "insertMany", "updateOne", "updateMany", "deleteOne", "deleteMany", "replaceOne", "drop", "createIndex"}
READS = {"find", "findOne", "aggregate", "count", "countDocuments", "estimatedDocumentCount", "distinct"}
SAMPLE = 40


class Missing(Exception):
    pass


# ---------- connection

def uri(c):
    u = str(c.get("url") or "").strip()
    if u:
        return u
    from urllib.parse import quote_plus
    host = str(c.get("host") or "").strip() or "localhost"
    if host.startswith(("mongodb://", "mongodb+srv://")):
        return host
    user, pw, port = str(c.get("user") or ""), str(c.get("password") or ""), str(c.get("port") or "").strip()
    auth = (quote_plus(user) + (":" + quote_plus(pw) if pw else "") + "@") if user else ""
    opts = str(c.get("options") or "").lstrip("?&")
    return "mongodb://%s%s%s/%s" % (auth, host, (":" + port) if port and ":" not in host else "", ("?" + opts) if opts else "")


def connect(c, db=None):
    """-> (client, database). `db` overrides the saved database (the schema picker)."""
    try:
        import pymongo
    except ImportError:
        raise Missing("pymongo")
    cl = pymongo.MongoClient(uri(c), serverSelectionTimeoutMS=8000, appname="aihub-data-studio")
    name = db or str(c.get("database") or "").strip()
    if not name:
        try:
            name = cl.get_default_database().name
        except Exception:
            raise ValueError("Set the Database field (the MongoDB database to work in).")
    return cl, cl[name]


# ---------- shell-ish statement -> python values

def _match(s, i, open_c, close_c):
    """Index of the bracket that closes the one at s[i] (strings and nesting aware), or -1."""
    depth, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch in "\"'":
            j = i + 1
            while j < n and s[j] != ch:
                j += 2 if s[j] == "\\" else 1
            i = j
        elif ch == open_c:
            depth += 1
        elif ch == close_c:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


FUNCS = {"ObjectId": "$oid", "ISODate": "$date", "Date": "$date", "UUID": "$uuid", "NumberLong": "$numberLong", "NumberInt": None, "Int32": None,
         "Long": "$numberLong", "NumberDecimal": "$numberDecimal", "Double": None, "Timestamp": None}


def relaxed(s):
    """Mongo-shell object notation -> strict (extended) JSON text."""
    out, i, n = [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch in "\"'":
            j, buf = i + 1, []
            while j < n and s[j] != ch:
                if s[j] == "\\" and j + 1 < n:
                    buf.append(s[j:j + 2])
                    j += 2
                    continue
                buf.append(s[j])
                j += 1
            body = "".join(buf)
            if ch == "'":
                body = re.sub(r'(?<!\\)"', '\\"', body).replace("\\'", "'")
            out.append('"' + body + '"')
            i = j + 1
        elif ch == "/" and (not "".join(out).rstrip() or "".join(out).rstrip()[-1] in ":[,(") :
            j = i + 1
            while j < n and s[j] != "/":
                j += 2 if s[j] == "\\" else 1
            pat, k = s[i + 1:j], j + 1
            m = re.match(r"[gimsuy]*", s[k:])
            k += m.end()
            out.append(json.dumps({"$regex": pat, "$options": re.sub(r"[^imsx]", "", m.group(0))}))
            i = k
        elif ch.isalpha() or ch in "_$":
            m = re.compile(r"[A-Za-z_$][\w$.]*").match(s, i)
            word, k = m.group(0), m.end()
            rest = s[k:].lstrip()
            if word == "new":
                i = k
            elif rest.startswith("("):
                p = k + (len(s[k:]) - len(rest))
                q = _match(s, p, "(", ")")
                if q < 0:
                    raise ValueError("Unbalanced ( after %s" % word)
                inner = relaxed(s[p + 1:q]).strip()
                key = FUNCS.get(word, "?")
                if key == "?":
                    raise ValueError("%s(...) is not supported here." % word)
                if word in ("Date", "ISODate") and not inner:
                    inner = json.dumps(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                if key is None:
                    out.append(inner.strip('"') or "0")
                elif key in ("$numberLong", "$numberDecimal"):
                    out.append('{"%s": %s}' % (key, inner if inner.startswith('"') else '"%s"' % inner))
                else:
                    out.append('{"%s": %s}' % (key, inner))
                i = q + 1
            elif rest.startswith(":") and word not in ("true", "false", "null"):
                out.append('"%s"' % word)
                i = k
            elif word in ("true", "false", "null"):
                out.append(word)
                i = k
            elif word in ("undefined", "NaN"):
                out.append("null")
                i = k
            elif word in ("Infinity",):
                out.append("1e308")
                i = k
            else:
                raise ValueError("Cannot read %r. Quote text values: \"%s\"." % (word, word))
        elif ch == ",":
            m = re.compile(r"\s*[}\]]").match(s, i + 1)
            if not m:
                out.append(",")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def loads(text):
    from bson import json_util
    t = relaxed(text.strip()) if text.strip() else "{}"
    try:
        return json_util.loads(t)
    except ValueError as e:
        raise ValueError("Cannot read this as a MongoDB document: %s" % e)


def split_args(s):
    """Top-level comma separated arguments of a call."""
    parts, depth, cur, i, n = [], 0, [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch in "\"'":
            j = i + 1
            while j < n and s[j] != ch:
                j += 2 if s[j] == "\\" else 1
            cur.append(s[i:j + 1])
            i = j + 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


HEAD = re.compile(r"\s*db\s*\.\s*(?:getCollection\(\s*([\"'])(.+?)\1\s*\)|([A-Za-z_][\w$]*))\s*\.\s*")
CALL = re.compile(r"([A-Za-z_]\w*)\s*\(")


def strip_comments(text):
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out).strip().rstrip(";").strip()


def parse(text):
    """-> {"coll", "calls": [(method, [args as text])]} or {"show": "collections"|"dbs"}. Raises ValueError with a hint."""
    t = strip_comments(text)
    if not t:
        raise ValueError("Write a statement first, e.g. db.orders.find({}).limit(10)")
    low = t.lower()
    if re.match(r"show\s+(collections|tables)$", low):
        return {"show": "collections"}
    if re.match(r"show\s+(dbs|databases)$", low):
        return {"show": "dbs"}
    if t.startswith("{"):                                          # JSON form
        spec = json.loads(relaxed(t))
        coll = spec.get("collection") or spec.get("coll")
        if not coll:
            raise ValueError('JSON form needs "collection": {"collection": "orders", "find": {...}}')
        calls = []
        if "aggregate" in spec:
            calls.append(("aggregate", [json.dumps(spec["aggregate"])]))
        elif "distinct" in spec:
            calls.append(("distinct", [json.dumps(spec["distinct"]), json.dumps(spec.get("filter") or {})]))
        elif "count" in spec:
            calls.append(("countDocuments", [json.dumps(spec["count"] if isinstance(spec["count"], dict) else {})]))
        else:
            calls.append(("find", [json.dumps(spec.get("find") or spec.get("filter") or {}), json.dumps(spec.get("projection") or {})]))
            for k in ("sort", "skip", "limit"):
                if spec.get(k) is not None:
                    calls.append((k, [json.dumps(spec[k])]))
        return {"coll": str(coll), "calls": calls}
    m = HEAD.match(t)
    if not m:
        raise ValueError("Start with db.<collection>.find(...) or db.<collection>.aggregate([...]); or `show collections`.")
    coll, i, calls = m.group(2) or m.group(3), m.end(), []
    while i < len(t):
        mm = CALL.match(t, i)
        if not mm:
            break
        p = mm.end() - 1
        q = _match(t, p, "(", ")")
        if q < 0:
            raise ValueError("Unbalanced ( in %s(...)" % mm.group(1))
        calls.append((mm.group(1), split_args(t[p + 1:q])))
        i = q + 1
        while i < len(t) and (t[i].isspace() or t[i] == "."):
            i += 1
    if not calls:
        raise ValueError("Add an operation after db.%s, e.g. .find({}) or .aggregate([...])" % coll)
    return {"coll": coll, "calls": calls}


# ---------- documents -> table

def simple(v):
    import datetime
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else str(v)
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return "\\x" + bytes(v)[:32].hex()
    if isinstance(v, (list, tuple, dict)):
        try:
            from bson import json_util
            return json_util.dumps(v, ensure_ascii=False)[:2000]
        except Exception:
            try:
                return json.dumps(v, ensure_ascii=False, default=str)[:2000]
            except Exception:
                return str(v)[:2000]
    try:
        d = v.to_decimal()                                         # Decimal128
        return float(d) if abs(d) < 1e15 and d == d else str(d)
    except Exception:
        return str(v)


def flat(doc, prefix="", depth=0):
    out = {}
    for k, v in doc.items():
        key = prefix + str(k)
        if isinstance(v, dict) and v and depth < 2 and not any(str(x).startswith("$") for x in v):
            out.update(flat(v, key + ".", depth + 1))
        else:
            out[key] = simple(v)
    return out


def table(docs):
    rows = [flat(d) if isinstance(d, dict) else {"value": simple(d)} for d in docs]
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    if "_id" in cols:
        cols.remove("_id")
        cols.insert(0, "_id")
    return cols, [[r.get(c) for c in cols] for r in rows]


def type_name(v):
    import datetime
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "double"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (datetime.datetime, datetime.date)):
        return "date"
    if isinstance(v, (list, tuple)):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__.lower().replace("objectid", "objectId")


def infer(coll, n=SAMPLE):
    """Fields of a collection from a sample: [{name, type, pk, null}] most common first (_id first)."""
    try:
        docs = list(coll.aggregate([{"$sample": {"size": n}}], maxTimeMS=8000))
    except Exception:
        docs = list(coll.find({}).limit(n))
    seen, types = {}, {}

    def walk(d, prefix, depth):
        for k, v in d.items():
            key = prefix + str(k)
            if isinstance(v, dict) and v and depth < 2 and not any(str(x).startswith("$") for x in v):
                walk(v, key + ".", depth + 1)
            else:
                seen[key] = seen.get(key, 0) + 1
                types.setdefault(key, {}).setdefault(type_name(v), 0)
                types[key][type_name(v)] += 1
    for d in docs:
        if isinstance(d, dict):
            walk(d, "", 0)
    names = sorted(seen, key=lambda k: (k != "_id", -seen[k]))
    return [{"name": k, "type": "|".join(sorted(types[k], key=lambda t: -types[k][t])[:2]), "pk": k == "_id", "null": seen[k] < len(docs)} for k in names]


# ---------- running

def _sort(spec):
    if isinstance(spec, dict):
        return [(k, int(v) if not isinstance(v, dict) else 1) for k, v in spec.items()]
    if isinstance(spec, list):
        return [tuple(x) for x in spec]
    raise ValueError("sort() takes an object like {\"total\": -1}")


def execute(db, text, limit, readonly):
    """-> {columns, rows, truncated, rowcount}. Raises ValueError for statements it cannot read or may not run."""
    p = parse(text)
    if p.get("show") == "collections":
        names = sorted(db.list_collection_names())
        return {"columns": ["collection"], "rows": [[n] for n in names], "truncated": False, "rowcount": None}
    if p.get("show") == "dbs":
        names = sorted(db.client.list_database_names())
        return {"columns": ["database"], "rows": [[n] for n in names], "truncated": False, "rowcount": None}
    coll = db[p["coll"]]
    (method, args), chain = p["calls"][0], p["calls"][1:]
    if readonly and method in WRITES:
        raise ValueError("This connection is read-only: only find / aggregate / count / distinct run.")
    if method not in READS and method not in WRITES:
        raise ValueError("%s() is not supported. Use find, findOne, aggregate, countDocuments, distinct (or the insert / update / delete family)." % method)
    vals = [loads(a) for a in args]
    if method in ("find", "findOne"):
        cur = coll.find(vals[0] if vals else {}, vals[1] if len(vals) > 1 else None, max_time_ms=60000)
        cap = limit + 1
        for name, a in chain:
            v = [loads(x) for x in a]
            if name == "sort" and v:
                cur = cur.sort(_sort(v[0]))
            elif name == "limit" and v:
                cap = min(cap, int(v[0]) or cap)
                cur = cur.limit(cap)
            elif name == "skip" and v:
                cur = cur.skip(int(v[0]))
            elif name in ("toArray", "pretty", "forEach"):
                continue
            elif name == "count":
                return {"columns": ["count"], "rows": [[coll.count_documents(vals[0] if vals else {})]], "truncated": False, "rowcount": None}
            else:
                raise ValueError(".%s() after find is not supported (sort, limit, skip, project are)." % name)
        docs = list(cur.limit(1 if method == "findOne" else cap))
    elif method == "aggregate":
        pipe = vals[0] if vals else []
        if not isinstance(pipe, list):
            raise ValueError("aggregate() takes a pipeline: an array of stages.")
        if any(isinstance(s, dict) and ({"$out", "$merge"} & set(s)) for s in pipe) and readonly:
            raise ValueError("$out / $merge write data: not allowed on a read-only connection.")
        if not any(isinstance(s, dict) and ({"$out", "$merge"} & set(s)) for s in pipe):
            pipe = pipe + [{"$limit": limit + 1}]
        docs = list(coll.aggregate(pipe, allowDiskUse=True, maxTimeMS=120000))
    elif method in ("count", "countDocuments"):
        return {"columns": ["count"], "rows": [[coll.count_documents(vals[0] if vals else {})]], "truncated": False, "rowcount": None}
    elif method == "estimatedDocumentCount":
        return {"columns": ["count"], "rows": [[coll.estimated_document_count()]], "truncated": False, "rowcount": None}
    elif method == "distinct":
        if not vals or not isinstance(vals[0], str):
            raise ValueError('distinct("field", {filter})')
        vs = coll.distinct(vals[0], vals[1] if len(vals) > 1 else {})
        return {"columns": [vals[0]], "rows": [[simple(v)] for v in vs[:limit]], "truncated": len(vs) > limit, "rowcount": None}
    else:
        n = 0
        if method == "insertOne":
            coll.insert_one(vals[0])
            n = 1
        elif method == "insertMany":
            n = len(coll.insert_many(vals[0]).inserted_ids)
        elif method in ("updateOne", "updateMany", "replaceOne"):
            r = getattr(coll, {"updateOne": "update_one", "updateMany": "update_many", "replaceOne": "replace_one"}[method])(vals[0], vals[1], *([vals[2].get("upsert")] if len(vals) > 2 and isinstance(vals[2], dict) else []))
            n = r.modified_count
        elif method in ("deleteOne", "deleteMany"):
            n = (coll.delete_one if method == "deleteOne" else coll.delete_many)(vals[0] if vals else {}).deleted_count
        elif method == "drop":
            coll.drop()
        elif method == "createIndex":
            coll.create_index(_sort(vals[0]))
        return {"columns": [], "rows": [], "truncated": False, "rowcount": n}
    cols, rows = table(docs[:limit])
    return {"columns": cols, "rows": rows, "truncated": len(docs) > limit, "rowcount": None}


def handle(act, req, c, out, ad):
    """The runner's actions for a MongoDB connection. `out` ends the process with the JSON answer."""
    t0 = time.time()
    ms = lambda: int((time.time() - t0) * 1000)
    try:
        cl, db = connect(c, req.get("schema") or None)
    except Missing:
        out(ok=False, error="The MongoDB driver (pymongo) is not installed.", missing=["pymongo"], command=ad.install_command(["pymongo"]))
    except ValueError as e:
        out(ok=False, error=str(e))
    try:
        if act == "test":
            info = cl.server_info()
            out(ok=True, dialect="mongodb", server="MongoDB " + str(info.get("version", "")), ms=ms())
        if act in ("tables", "digest"):
            try:
                schemas = sorted(cl.list_database_names())[:300]
            except Exception:
                schemas = [db.name]
            infos = {i["name"]: i.get("type", "collection") for i in db.list_collections()}
            names = sorted(infos, key=str.lower)
            if act == "tables":
                out(ok=True, schemas=schemas, schema=db.name, tables=[{"name": n, "kind": "view" if infos[n] == "view" else "collection"} for n in names if not n.startswith("system.")][:2000], more=False)
            want = str(req.get("table") or "").lower()
            if want:
                names = [n for n in names if want in n.lower()]
            max_t, max_c, budget = int(req.get("max_tables") or 60), int(req.get("max_columns") or 40), float(req.get("budget") or 20)
            tabs = []
            for n in names[:max_t]:
                if time.time() - t0 > budget:
                    break
                t = {"name": n, "kind": "view" if infos[n] == "view" else "collection", "columns": [], "fks": []}
                try:
                    cols = infer(db[n], 20)
                    t["columns"] = [{"name": x["name"], "type": x["type"], "pk": x["pk"]} for x in cols[:max_c]]
                    t["more_columns"] = max(0, len(cols) - max_c)
                    t["rows"] = db[n].estimated_document_count()
                except Exception as e:
                    t["error"] = str(e)[:120]
                tabs.append(t)
            out(ok=True, dialect="mongodb", schemas=schemas, schema=db.name, tables=tabs, total=len(names), ms=ms())
        if act == "columns":
            out(ok=True, columns=infer(db[str(req.get("table") or "")]))
        if act == "query":
            limit = max(1, min(int(req.get("limit") or 1000), 50000))
            stmts = [s for s in _statements(str(req.get("sql") or ""))]
            if not stmts:
                out(ok=False, error="Write a statement first, e.g. db.orders.find({}).limit(10)")
            res = {"columns": [], "rows": [], "truncated": False, "rowcount": None}
            for s in stmts:
                res = execute(db, s, limit, bool(req.get("readonly")))
            out(ok=True, statements=len(stmts), ms=ms(), **res)
        out(ok=False, error="unknown action")
    except SystemExit:
        raise
    except ValueError as e:
        out(ok=False, error=str(e), ms=ms())
    except Exception as e:
        out(ok=False, error=(type(e).__name__ + ": " + str(e))[:1500], ms=ms())


def _statements(text):
    """Statements split on ';' outside strings (several `db.` calls, or a JSON document)."""
    t = strip_comments(text) if text.strip() else ""
    res, cur, i, n = [], [], 0, len(t)
    while i < n:
        ch = t[i]
        if ch in "\"'":
            j = i + 1
            while j < n and t[j] != ch:
                j += 2 if t[j] == "\\" else 1
            cur.append(t[i:j + 1])
            i = j + 1
        elif ch == ";":
            res.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(ch)
            i += 1
    res.append("".join(cur))
    return [s.strip() for s in res if s.strip()]
