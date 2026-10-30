"""The UI's local search engine: one SQLite file (~/.aihub/ui/search.db), FTS5 plus plain indexes, every query answered on this machine.

Collections
  chats   every user / assistant text of every chat (full text). Indexed incrementally: only the bytes appended to a history file since
          the last pass are read; a rewritten file (rewind, delete, import) is re-indexed (`touch(sid, rewrite=True)`, or the 64 bytes
          before the stored offset no longer match).
  spaces  a MIRROR of the hub Spaces this person can open: seeded in the background (newest per kind, top, mine, shared chats) and
          topped up with EVERY hub answer; a space not seen for KEEP_DAYS is dropped.
  pkgs    a mirror of the whole package catalogue (`aihub --json search`: one call returns all of it).

Queries run in tiers, most exact and cheapest first, and stop once `limit` is filled:
  exact ref (author@name) -> author + name prefix -> author -> BM25 full text (prefix tokens, column weights) -> substring scan,
  the last only while the collection is small (LIKE_MAX). A BM25 query that runs past RANK_MS is cut and answered newest-first.
The hub is asked only when the local answer is thin (`want_remote`) and at most once per query per ASK_TTL; its answer is merged in.
Without FTS5 (`OK` false) the callers use their old scans."""
import json
import os
import re
import sqlite3
import threading
import time

from . import aihub as A

LIKE_MAX = 20000            # a collection bigger than this never gets a substring scan
ASK_TTL = 120.0             # the same query goes to the hub at most once per 2 min
REFRESH = 60.0              # mirror refresh (only what changed since the last one), in the background, only while Search is used
FULL_EVERY = 3600.0         # a full re-seed: catches spaces newly SHARED with you (sharing does not move `updated`)
PAGES = 20                  # `--since` pages of 100 per refresh
KEEP_DAYS = 14
RANK_MS = 120               # BM25 over a huge match set is cut after this and answered newest-first
BODY_MAX = 20000            # characters indexed per message
TAIL = 64                   # bytes before the indexed offset that must still match (else the file was rewritten)
KINDS = ("doc", "slides", "report", "pdf", "sheet", "dashboard", "html")
TOK = re.compile(r"\w+", re.U)
REF = re.compile(r"^([A-Za-z0-9][\w.-]{0,63})@([\w.-]{0,64})$")
ID = re.compile(r"^[0-9a-f]{8}$")
FTS = "tokenize='unicode61 remove_diacritics 2', prefix='2 3'"
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files(sid TEXT PRIMARY KEY, off INTEGER, mtime INTEGER, size INTEGER, tail TEXT);
CREATE TABLE IF NOT EXISTS spans(sid TEXT, lo INTEGER, hi INTEGER);
CREATE INDEX IF NOT EXISTS spans_sid ON spans(sid);
CREATE VIRTUAL TABLE IF NOT EXISTS msg USING fts5(body, sid UNINDEXED, %(f)s);
CREATE TABLE IF NOT EXISTS space(id INTEGER PRIMARY KEY, ref TEXT UNIQUE COLLATE NOCASE, owner TEXT COLLATE NOCASE, slug TEXT COLLATE NOCASE,
  title TEXT, kind TEXT, visibility TEXT, chat INTEGER, description TEXT, tags TEXT, views INTEGER, seen REAL);
CREATE INDEX IF NOT EXISTS space_owner ON space(owner, slug);
CREATE INDEX IF NOT EXISTS space_chat ON space(chat, seen);
CREATE VIRTUAL TABLE IF NOT EXISTS space_fts USING fts5(slug, title, description, tags, owner, %(f)s);
CREATE TABLE IF NOT EXISTS pkg(id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, type TEXT, description TEXT, version TEXT, tags TEXT, seen REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS pkg_fts USING fts5(name, description, tags, %(f)s);
CREATE TABLE IF NOT EXISTS asked(kind TEXT, q TEXT, at REAL, PRIMARY KEY(kind, q));
CREATE TABLE IF NOT EXISTS dirs(id INTEGER PRIMARY KEY, path TEXT, name TEXT COLLATE NOCASE, depth INTEGER, proj INTEGER);
CREATE INDEX IF NOT EXISTS dirs_name ON dirs(name);
CREATE VIRTUAL TABLE IF NOT EXISTS dir_fts USING fts5(name, path, %(f)s);
""" % {"f": FTS}

OK = True
_db, _path, _lock = None, "", threading.RLock()
_mem = threading.Lock()
_dirty, _rewrite = set(), set()
_state = {"warm": False, "warming": False, "refreshing": False}


def _hist(sid):
    from . import store
    return os.path.join(store.ui_dir("history"), sid + ".jsonl")


def _conn():
    """The one connection (guarded by _lock), for the current aihub home (another home = its own file). None without FTS5."""
    global _db, _path, OK
    want = os.path.join(A.HOME(), "ui", "search.db")
    if _db is not None and _path != want:
        with _lock:
            _db.close()
            _db = None
            _dirty.clear()
            _rewrite.clear()
            _state.update(warm=False, warming=False)
    if _db is not None or not OK:
        return _db
    from . import store
    store.ui_dir()
    try:
        c = sqlite3.connect(want, check_same_thread=False, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA cache_size=-16000")
        c.executescript(SCHEMA)
    except sqlite3.Error:
        OK = False
        return None
    _db, _path = c, want
    return c


def reset():
    """Tests: close and forget the connection (a new HOME gets its own file)."""
    global _db, OK
    with _lock:
        if _db is not None:
            _db.close()
        _db, OK = None, True
        _dirty.clear()
        _rewrite.clear()
        _state.update(warm=False, warming=False, refreshing=False, dirs_walking=False)


def match(q):
    """FTS5 query: every word as a prefix, all required. '' when there is no word."""
    return " ".join('"%s"*' % t for t in TOK.findall((q or "").lower())[:8])


def _ranked(c, sql, args, fallback):
    """SQL with ORDER BY rank, cut after RANK_MS (a word found in millions of rows) and then answered by `fallback` (newest first: cheap)."""
    end = time.monotonic() + RANK_MS / 1000.0
    c.set_progress_handler(lambda: 1 if time.monotonic() > end else 0, 2000)
    try:
        return c.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        if "interrupt" not in str(e):
            raise
    finally:
        c.set_progress_handler(None, 0)
    return c.execute(fallback, args).fetchall()


# ---- chats

def touch(sid, rewrite=False):
    """A history file changed (append) or was rewritten / removed (rewrite=True). Indexed at the next search or by the warm thread."""
    with _mem:
        _dirty.add(sid)
        if rewrite:
            _rewrite.add(sid)


def _texts(raw):
    for ln in raw.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("k") == "user":
            t = str(e.get("text", ""))
        elif e.get("k") == "msg":
            t = "\n".join(str(b.get("text", "")) for b in e.get("blocks") or [] if isinstance(b, dict) and b.get("type") == "text")
        else:
            continue
        if t.strip():
            yield t[:BODY_MAX]


def _forget(c, sid):
    for lo, hi in c.execute("SELECT lo, hi FROM spans WHERE sid=?", (sid,)).fetchall():
        c.execute("DELETE FROM msg WHERE rowid BETWEEN ? AND ?", (lo, hi))
    c.execute("DELETE FROM spans WHERE sid=?", (sid,))
    c.execute("DELETE FROM files WHERE sid=?", (sid,))


def _sync_chat(c, sid, rewrite=False):
    p = _hist(sid)
    try:
        st = os.stat(p)
    except OSError:
        _forget(c, sid)
        return
    row = c.execute("SELECT off, mtime, size, tail FROM files WHERE sid=?", (sid,)).fetchone()
    if row and not rewrite and row[1] == st.st_mtime_ns and row[2] == st.st_size:
        return
    off = row[0] if row and not rewrite else 0
    try:
        with open(p, "rb") as f:
            if off:
                if st.st_size < off:
                    off = 0
                else:
                    f.seek(max(0, off - TAIL))
                    if f.read(min(TAIL, off)).hex() != (row[3] or ""):
                        off = 0
            if not off and row:
                _forget(c, sid)
            f.seek(off)
            raw = f.read(st.st_size - off)
    except OSError:
        return
    cut = raw.rfind(b"\n") + 1                                          # a line still being written waits for the next pass
    raw, end = raw[:cut], off + cut
    lo = hi = None
    for t in _texts(raw):
        rid = c.execute("INSERT INTO msg(body, sid) VALUES(?, ?)", (t, sid)).lastrowid
        lo, hi = (rid if lo is None else lo), rid
    if lo is not None:
        last = c.execute("SELECT rowid, hi FROM spans WHERE sid=? ORDER BY hi DESC LIMIT 1", (sid,)).fetchone()
        if last and last[1] + 1 == lo:
            c.execute("UPDATE spans SET hi=? WHERE rowid=?", (hi, last[0]))
        else:
            c.execute("INSERT INTO spans VALUES(?, ?, ?)", (sid, lo, hi))
    try:
        with open(p, "rb") as f:                                        # the bytes just before `end`: checked by the next pass
            f.seek(max(0, end - TAIL))
            tail = f.read(min(TAIL, end))
    except OSError:
        tail = b""
    c.execute("INSERT OR REPLACE INTO files VALUES(?, ?, ?, ?, ?)", (sid, end, st.st_mtime_ns, st.st_size, tail.hex()))


def _one(sid, rewrite):
    with _lock:
        c = _conn()
        if c is None:
            return
        c.execute("BEGIN")
        try:
            _sync_chat(c, sid, rewrite)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")


def sync(budget=0.3):
    """Index the chats changed since the last pass, for at most BUDGET seconds. -> True when nothing is left."""
    end = time.monotonic() + budget
    while time.monotonic() < end:
        with _mem:
            if not _dirty:
                return True
            sid = _dirty.pop()
            rw = sid in _rewrite
            _rewrite.discard(sid)
        _one(sid, rw)
    return not _dirty


def warm():
    """Background: bring the whole index up to date (new, changed and deleted chats). One commit per chat, so searches never wait long."""
    if _state["warming"] or _conn() is None:
        return
    _state["warming"] = True

    def run():
        try:
            from . import store
            have = {f[:-6] for f in os.listdir(store.ui_dir("history")) if f.endswith(".jsonl") and ID.match(f[:-6])}
            with _lock:
                known = {r[0] for r in _conn().execute("SELECT sid FROM files").fetchall()}
            for sid in sorted(have) + sorted(known - have):
                _one(sid, False)
            _state["warm"] = True
        finally:
            _state["warming"] = False
    threading.Thread(target=run, name="search-warm", daemon=True).start()


def ready():
    return _state["warm"]


def indexed():
    """Chat ids already in the index (callers scan the others while the warm pass runs)."""
    with _lock:
        c = _conn()
        return {r[0] for r in c.execute("SELECT sid FROM files").fetchall()} if c else set()


def size(table):
    with _lock:
        c = _conn()
        return c.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] if c else 0


def chats(q, limit=40):
    """-> [(sid, snippet)] best first, one per chat."""
    m = match(q)
    c = _conn()
    if not m or c is None:
        return []
    sync()
    with _lock:
        rows = _ranked(c, "SELECT sid, snippet(msg, 0, '', '', '…', 16) FROM msg WHERE msg MATCH ? ORDER BY rank LIMIT ?", (m, limit * 8),
                       "SELECT sid, snippet(msg, 0, '', '', '…', 16) FROM msg WHERE msg MATCH ? ORDER BY rowid DESC LIMIT ?")
    out, seen = [], set()
    for sid, snip in rows:
        if sid not in seen:
            seen.add(sid)
            out.append((sid, " ".join(snip.split())[:160]))
            if len(out) >= limit:
                break
    return out


# ---- hub mirror (spaces, packages)

def _who(doc):
    c = doc.get("context") or {}
    return "%s|%s" % (c.get("hub") or "", c.get("username") or "")


def _meta(c, k, v=None):
    if v is None:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else ""
    c.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (k, str(v)))


def _same_person(c, doc):
    """A mirror belongs to one hub + account: another one empties it (what one person may open is not what another may)."""
    who = _who(doc)
    if who == "|":
        return
    if _meta(c, "who") != who:
        for t in ("space", "space_fts", "pkg", "pkg_fts", "asked"):
            c.execute("DELETE FROM %s WHERE 1=1" % t)
        _meta(c, "who", who)
        for k in ("spaces_at", "pkgs_at", "spaces_cursor", "spaces_full"):
            _meta(c, k, 0)


def merge_spaces(items, doc=None, now=None, cursor=False):
    """Upsert spaces from any hub answer (search, list, top, changes). -> how many. CURSOR: the answer is complete up to its newest
    `updated` (a change page, a newest-first list), so the `since` cursor may move there; a search hit never moves it."""
    now = now or time.time()
    n, newest = 0, 0.0
    with _lock:
        c = _conn()
        if c is None:
            return 0
        c.execute("BEGIN")
        try:
            if doc is not None:
                _same_person(c, doc)
            for x in items or []:
                if not isinstance(x, dict) or not x.get("ref") or "@" not in x["ref"]:
                    continue
                owner, slug = x["ref"].split("@", 1)
                tags = x.get("tags") or []
                tags = ",".join(tags) if isinstance(tags, list) else str(tags)
                row = (x["ref"], owner, slug, str(x.get("title") or ""), str(x.get("kind") or ""), str(x.get("visibility") or ""),
                       1 if "aihub-chat" in tags.split(",") else 0, str(x.get("description") or "")[:400], tags, int(x.get("views") or 0), now)
                c.execute("INSERT INTO space(ref, owner, slug, title, kind, visibility, chat, description, tags, views, seen) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                          "ON CONFLICT(ref) DO UPDATE SET title=excluded.title, kind=excluded.kind, visibility=excluded.visibility, chat=excluded.chat, "
                          "description=excluded.description, tags=excluded.tags, views=excluded.views, seen=excluded.seen", row)
                sid = c.execute("SELECT id FROM space WHERE ref=?", (x["ref"],)).fetchone()[0]
                c.execute("DELETE FROM space_fts WHERE rowid=?", (sid,))
                c.execute("INSERT INTO space_fts(rowid, slug, title, description, tags, owner) VALUES(?,?,?,?,?,?)",
                          (sid, slug.replace("-", " "), row[3], row[7], tags.replace(",", " "), owner))
                n += 1
                try:
                    newest = max(newest, float(x.get("updated") or 0))
                except (TypeError, ValueError):
                    pass
            if cursor and newest > float(_meta(c, "spaces_cursor") or 0):
                _meta(c, "spaces_cursor", newest)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return n


def drop_space(ref):
    """A mirrored space the hub no longer has (an import or open said 404)."""
    with _lock:
        c = _conn()
        r = c and c.execute("SELECT id FROM space WHERE ref=?", (ref,)).fetchone()
        if r:
            c.execute("DELETE FROM space_fts WHERE rowid=?", (r[0],))
            c.execute("DELETE FROM space WHERE id=?", (r[0],))


def merge_pkgs(items, doc=None, full=False, now=None):
    """Upsert packages; FULL (the whole catalogue) also removes the ones that are gone."""
    now = now or time.time()
    with _lock:
        c = _conn()
        if c is None:
            return 0
        c.execute("BEGIN")
        try:
            if doc is not None:
                _same_person(c, doc)
            n = 0
            for p in items or []:
                if not isinstance(p, dict) or not p.get("name"):
                    continue
                tags = p.get("tags") or []
                tags = ",".join(tags) if isinstance(tags, list) else str(tags)
                c.execute("INSERT INTO pkg(name, type, description, version, tags, seen) VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                          "type=excluded.type, description=excluded.description, version=excluded.version, tags=excluded.tags, seen=excluded.seen",
                          (p["name"], str(p.get("type") or ""), str(p.get("description") or "")[:400], str(p.get("latest_version") or p.get("version") or ""), tags, now))
                pid = c.execute("SELECT id FROM pkg WHERE name=?", (p["name"],)).fetchone()[0]
                c.execute("DELETE FROM pkg_fts WHERE rowid=?", (pid,))
                c.execute("INSERT INTO pkg_fts(rowid, name, description, tags) VALUES(?,?,?,?)", (pid, p["name"].replace("-", " ") + " " + p["name"], str(p.get("description") or "")[:400], tags.replace(",", " ")))
                n += 1
            if full:
                for (pid,) in c.execute("SELECT id FROM pkg WHERE seen<?", (now,)).fetchall():
                    c.execute("DELETE FROM pkg_fts WHERE rowid=?", (pid,))
                    c.execute("DELETE FROM pkg WHERE id=?", (pid,))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return n


def _prune(c, now):
    for (sid,) in c.execute("SELECT id FROM space WHERE seen<?", (now - KEEP_DAYS * 86400,)).fetchall():
        c.execute("DELETE FROM space_fts WHERE rowid=?", (sid,))
        c.execute("DELETE FROM space WHERE id=?", (sid,))
    c.execute("DELETE FROM asked WHERE at<?", (now - ASK_TTL,))


def _spaces_doc(args):
    try:
        return A.run(args, timeout=60)
    except Exception:
        return None


def _changes(cursor):
    """Spaces changed since CURSOR, page by page (`space list --since`, oldest first). Stops on a short page, when the cursor does not move
    (an older hub ignores --since and answers newest-first: one page, then stop) or after PAGES. -> False when the hub did not answer."""
    for _ in range(PAGES):
        doc = _spaces_doc(["space", "list", "--since", repr(cursor), "--limit", "100"])
        if doc is None:
            return False
        items = (doc.get("data") or {}).get("items") or []
        merge_spaces(items, doc, cursor=True)
        nxt = max([cursor] + [float(x.get("updated") or 0) for x in items if isinstance(x, dict) and isinstance(x.get("updated"), (int, float))])
        if len(items) < 100 or nxt <= cursor:                           # its own pages decide, not the shared cursor a parallel list moves
            return True
        cursor = nxt
    return True


def _seed():
    """One refresh of both mirrors. Usually TWO CLI calls: the package catalogue (the CLI keeps it cached, ETag) and the spaces changed since
    the last look. A full seed (newest per kind, top, mine, shared chats; four at a time) runs the first time and every FULL_EVERY."""
    import concurrent.futures as cf
    with _lock:
        c = _conn()
        cursor, full_at = float(_meta(c, "spaces_cursor") or 0), float(_meta(c, "spaces_full") or 0)
    full = not cursor or time.time() - full_at > FULL_EVERY

    def pk():
        doc = _spaces_doc(["search"])
        if doc is not None:
            merge_pkgs(doc.get("data") or [], doc, full=True)

    def one(args):
        doc = _spaces_doc(args)
        if doc is not None:
            merge_spaces((doc.get("data") or {}).get("items") or [], doc, cursor=args[1] == "list")      # newest-first lists
    calls = [["space", "list", "--kind", k, "--limit", "100"] for k in KINDS] + [["space", "top", "--limit", "100"], ["space", "list", "--mine", "--limit", "100"],
                                                                                   ["space", "search", "aihub-chat", "--limit", "100"]] if full else []
    with cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="search-seed") as ex:
        jobs = [ex.submit(pk)] + [ex.submit(one, a) for a in calls]
        if cursor:
            jobs.append(ex.submit(_changes, cursor))
        cf.wait(jobs)
    now = time.time()
    with _lock:
        c = _conn()
        _meta(c, "spaces_at", now)
        _meta(c, "pkgs_at", now)
        if full:
            _meta(c, "spaces_full", now)
        _prune(c, now)


def refresh(force=False):
    """Start a background refresh of the hub mirrors when they are older than REFRESH (or FORCE). Never waits."""
    c = _conn()
    if c is None or _state["refreshing"]:
        return False
    with _lock:
        at = float(_meta(c, "spaces_at") or 0)
    if not force and time.time() - at < REFRESH:
        return False
    _state["refreshing"] = True

    def run():
        try:
            _seed()
        finally:
            _state["refreshing"] = False
    threading.Thread(target=run, name="search-refresh", daemon=True).start()
    return True


def want_remote(kind, q, found, need=3):
    """Ask the hub? Only when the local answer is thin, the query is real and it was not asked in the last ASK_TTL."""
    q = " ".join((q or "").lower().split())
    if found >= need or len(q) < 2:
        return False
    with _lock:
        c = _conn()
        r = c and c.execute("SELECT at FROM asked WHERE kind=? AND q=?", (kind, q)).fetchone()
    return not (r and time.time() - r[0] < ASK_TTL)


def asked(kind, q):
    with _lock:
        c = _conn()
        if c is not None:
            c.execute("INSERT OR REPLACE INTO asked VALUES(?, ?, ?)", (kind, " ".join((q or "").lower().split()), time.time()))


SPACE_COLS = "s.ref, s.title, s.kind, s.visibility, s.chat, s.description"


def _space_rows(rows):
    return [{"ref": r[0], "title": r[1] or "", "kind": r[2] or "", "visibility": r[3] or "", "chat": bool(r[4]), "description": r[5] or ""} for r in rows]


def spaces(q, limit=30):
    """Local tiers over the Spaces mirror. Empty query = shared chats, newest seen first."""
    c = _conn()
    if c is None:
        return []
    q = " ".join((q or "").split())
    out, seen = [], set()

    def add(rows):
        for r in _space_rows(rows):
            if r["ref"].lower() not in seen and len(out) < limit:
                seen.add(r["ref"].lower())
                out.append(r)
    with _lock:
        if not q:
            add(c.execute("SELECT " + SPACE_COLS + " FROM space s WHERE chat=1 ORDER BY seen DESC LIMIT ?", (limit,)).fetchall())
            return out
        m, rest = REF.match(q.split(" ")[0]), " ".join(q.split(" ")[1:])
        if m:
            owner, pre = m.groups()
            add(c.execute("SELECT " + SPACE_COLS + " FROM space s WHERE owner=? AND slug=?", (owner, pre)).fetchall())                          # exact
            if pre:
                add(c.execute("SELECT " + SPACE_COLS + " FROM space s WHERE owner=? AND slug>=? AND slug<? ORDER BY views DESC LIMIT ?",
                              (owner, pre.lower(), pre.lower() + "￿", limit)).fetchall())                                                    # author + name prefix
            add(c.execute("SELECT " + SPACE_COLS + " FROM space s WHERE owner=? ORDER BY views DESC LIMIT ?", (owner, limit)).fetchall())         # author
            words = (owner + " " + pre + " " + rest).strip()
        else:
            words = q
            first = TOK.findall(q.lower())[:1]
            if first:                                                                                                                             # a word that is an author
                fm = match(" ".join(TOK.findall(q.lower())[1:]))
                add(c.execute("SELECT " + SPACE_COLS + " FROM space s WHERE owner=?" + (" AND s.id IN (SELECT rowid FROM space_fts WHERE space_fts MATCH ?)" if fm else "")
                              + " ORDER BY views DESC LIMIT ?", (first[0],) + ((fm,) if fm else ()) + (limit,)).fetchall())
        fm = match(words)
        if fm and len(out) < limit:                                                                                                               # full text, BM25
            add(_ranked(c, "SELECT " + SPACE_COLS + " FROM space_fts f JOIN space s ON s.id=f.rowid WHERE space_fts MATCH ? ORDER BY bm25(space_fts, 10, 6, 2, 3, 4) LIMIT ?",
                        (fm, limit), "SELECT " + SPACE_COLS + " FROM space_fts f JOIN space s ON s.id=f.rowid WHERE space_fts MATCH ? ORDER BY s.views DESC LIMIT ?"))
        if len(out) < limit and len(q) >= 2 and c.execute("SELECT COUNT(*) FROM space").fetchone()[0] <= LIKE_MAX:                              # substring, small sets only
            like = "%" + q.lower().replace("%", "").replace("_", "") + "%"
            add(c.execute("SELECT " + SPACE_COLS + " FROM space s WHERE lower(s.ref) LIKE ? OR lower(s.title) LIKE ? OR lower(s.description) LIKE ? ORDER BY views DESC LIMIT ?",
                          (like, like, like, limit)).fetchall())
    return out


def pkgs(q, limit=30):
    """Local tiers over the package mirror: exact name -> name prefix -> BM25 -> substring (small sets)."""
    c = _conn()
    if c is None:
        return []
    q = " ".join((q or "").split())
    out, seen = [], set()
    cols = "p.name, p.type, p.description, p.version"

    def add(rows):
        for r in rows:
            if r[0].lower() not in seen and len(out) < limit:
                seen.add(r[0].lower())
                out.append({"name": r[0], "type": r[1] or "", "description": r[2] or "", "latest_version": r[3] or ""})
    with _lock:
        if not q:
            add(c.execute("SELECT " + cols + " FROM pkg p ORDER BY name LIMIT ?", (limit,)).fetchall())
            return out
        add(c.execute("SELECT " + cols + " FROM pkg p WHERE name=?", (q,)).fetchall())
        add(c.execute("SELECT " + cols + " FROM pkg p WHERE name>=? AND name<? ORDER BY name LIMIT ?", (q.lower(), q.lower() + "￿", limit)).fetchall())
        fm = match(q)
        if fm and len(out) < limit:
            add(_ranked(c, "SELECT " + cols + " FROM pkg_fts f JOIN pkg p ON p.id=f.rowid WHERE pkg_fts MATCH ? ORDER BY bm25(pkg_fts, 10, 3, 4) LIMIT ?", (fm, limit),
                        "SELECT " + cols + " FROM pkg_fts f JOIN pkg p ON p.id=f.rowid WHERE pkg_fts MATCH ? ORDER BY p.name LIMIT ?"))
        if len(out) < limit and len(q) >= 2 and c.execute("SELECT COUNT(*) FROM pkg").fetchone()[0] <= LIKE_MAX:
            like = "%" + q.lower().replace("%", "").replace("_", "") + "%"
            add(c.execute("SELECT " + cols + " FROM pkg p WHERE lower(name) LIKE ? OR lower(description) LIKE ? ORDER BY name LIMIT ?", (like, like, limit)).fetchall())
    return out


# ---- folders under the home folder (Search `@folder:` -> a new chat working there)

DIR_MAX, DIR_DEPTH, DIR_SECONDS = 300000, 7, 90.0     # walk caps: folders, levels below home, seconds
DIRS_REFRESH = 1800.0                                  # re-walk at most every 30 min, only while folders are searched
DIR_BATCH = 5000
DIR_SKIP = {"node_modules", "Library", "Applications", "__pycache__", "venv", "site-packages", "dist", "build", "target", "Pictures", "Music", "Movies",
            "AppData", "Program Files", "$Recycle.Bin", "vendor", "bower_components", "Pods", "DerivedData"}
DIR_MARK = (".git", "aihub.toml", "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "pom.xml", "build.gradle", ".claude", "CLAUDE.md")


def _home():
    return os.path.realpath(os.path.expanduser("~"))


def short(path):
    h = _home()
    return "~" + path[len(h):] if path == h or path.startswith(h + os.sep) else path


def _walk():
    """Breadth first (shallow folders are found first and never cut by the caps). Yields (path, name, depth, is_project)."""
    root, end, n = _home(), time.monotonic() + DIR_SECONDS, 0
    level = [root]
    for depth in range(1, DIR_DEPTH + 1):
        nxt = []
        for d in level:
            if time.monotonic() > end or n >= DIR_MAX:
                return
            try:
                with os.scandir(d) as it:
                    subs = [e for e in it if not e.name.startswith(".") and e.name not in DIR_SKIP and e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            for e in subs:
                try:
                    names = set(os.listdir(e.path))
                except OSError:
                    names = set()
                proj = 1 if any(m in names for m in DIR_MARK) else 0
                n += 1
                yield e.path, e.name, depth, proj
                if not proj or depth < 3:                               # inside a project only its first levels are worth listing
                    nxt.append(e.path)
        level = nxt


def dirs_refresh(force=False):
    """Start a background re-walk when the folder index is older than DIRS_REFRESH (or empty, or FORCE). New rows are written in batches
    next to the old ones (searches dedupe), then the old generation is dropped by rowid: Search keeps answering while it runs."""
    c = _conn()
    if c is None or _state.get("dirs_walking"):
        return False
    with _lock:
        at = float(_meta(c, "dirs_at") or 0)
    if not force and time.time() - at < DIRS_REFRESH:
        return False
    _state["dirs_walking"] = True

    def run():
        try:
            with _lock:
                first = (c.execute("SELECT MAX(id) FROM dirs").fetchone()[0] or 0) + 1
            batch = []

            def flush():
                with _lock:
                    c.execute("BEGIN")
                    for p, name, depth, proj in batch:
                        rid = c.execute("INSERT INTO dirs(path, name, depth, proj) VALUES(?,?,?,?)", (p, name, depth, proj)).lastrowid
                        c.execute("INSERT INTO dir_fts(rowid, name, path) VALUES(?,?,?)", (rid, name.replace("-", " ").replace("_", " ") + " " + name, short(p)))
                    c.execute("COMMIT")
                batch.clear()
            for row in _walk():
                batch.append(row)
                if len(batch) >= DIR_BATCH:
                    flush()
            if batch:
                flush()
            with _lock:
                c.execute("BEGIN")
                c.execute("DELETE FROM dir_fts WHERE rowid<?", (first,))
                c.execute("DELETE FROM dirs WHERE id<?", (first,))
                _meta(c, "dirs_at", time.time())
                c.execute("COMMIT")
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        finally:
            _state["dirs_walking"] = False
    threading.Thread(target=run, name="search-dirs", daemon=True).start()
    return True


def _live(q):
    """A typed path (~/x, /x, ./x): that folder and its sub folders whose name starts with the last part. No index needed."""
    full, trail = os.path.abspath(os.path.expanduser(q)), q.endswith(("/", os.sep))
    base, pre = (full, "") if trail else (os.path.dirname(full), os.path.basename(full).lower())
    out = [full] if os.path.isdir(full) else []                        # the typed folder itself first
    try:
        with os.scandir(base) as it:
            for e in sorted(it, key=lambda e: e.name.lower()):
                if len(out) >= 60:
                    break
                if not e.name.startswith(".") and e.name not in DIR_SKIP and e.name.lower().startswith(pre) and e.is_dir() and e.path not in out:
                    out.append(e.path)
    except OSError:
        pass
    return out


def folders(q, limit=30):
    """-> [{path, short, name, proj}]: a typed path is listed live; words go through name exact -> name prefix (projects, shallow first)
    -> BM25 over names and paths (projects first)."""
    q = " ".join((q or "").split())
    out, seen = [], set()

    def add(rows):
        for path, name, proj in rows:
            if path not in seen and len(out) < limit and os.path.isdir(path):
                seen.add(path)
                out.append({"path": path, "short": short(path), "name": name, "proj": bool(proj)})
    if q[:1] in ("/", "~", ".") or (len(q) > 2 and q[1:3] in (":\\", ":/")):
        add([(p, os.path.basename(p) or p, 0) for p in _live(q)])
        return out
    c = _conn()
    if c is None or not q:
        return out
    with _lock:
        add(c.execute("SELECT path, name, proj FROM dirs WHERE name=? ORDER BY proj DESC, depth LIMIT ?", (q, limit)).fetchall())
        add(c.execute("SELECT path, name, proj FROM dirs WHERE name>=? AND name<? ORDER BY proj DESC, depth, length(name) LIMIT ?",
                      (q.lower(), q.lower() + "\uffff", limit)).fetchall())
        fm = match(q)
        if fm and len(out) < limit:
            rows = _ranked(c, "SELECT d.path, d.name, d.proj FROM dir_fts f JOIN dirs d ON d.id=f.rowid WHERE dir_fts MATCH ? ORDER BY bm25(dir_fts, 8, 1) LIMIT ?",
                           (fm, limit * 3), "SELECT d.path, d.name, d.proj FROM dir_fts f JOIN dirs d ON d.id=f.rowid WHERE dir_fts MATCH ? ORDER BY d.proj DESC, d.depth LIMIT ?")
            add(sorted(rows, key=lambda r: -r[2]))                      # stable: projects first, BM25 order inside
    return out


def dirs_busy():
    return bool(_state.get("dirs_walking"))
