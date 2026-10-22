"""Upload your daily token totals to the hub, in the background, after each answer turn (Claude Code `Stop` hook).

The hook only calls `kick()`: it marks "something changed" and starts one detached worker (`aihub usage-sync --background`) unless one
is alive. The worker waits a short window (a burst of turns becomes ONE upload), reads the usage logs through the per-file parse cache
(only changed files are parsed), totals them per UTC day / tool / model and sends only the rows that differ from the last successful
upload. A row is the whole day, so the hub overwrites it: a resend or a lost answer can never double count. Only numbers and model names
leave the machine (no titles, prompts or folders). Needs a hub and a signed-in user; off with AIHUB_USAGE_SYNC=off or
`aihub config set usage_sync off`. Nothing here may raise or block the agent. Stdlib only."""
import gzip
import json
import os
import subprocess
import time

from ..core import textsafe
from . import paths

WINDOW = 8              # seconds the worker waits so quick consecutive turns are one upload
STALE = 300             # lock heartbeat age after which the worker is considered dead
BACKOFF_BASE, BACKOFF_MAX = 60, 3600
CHUNK = 2000            # rows per request
PASSES = 3              # extra passes when a turn ended while the worker was busy
TIMEOUT = 20
KEEP_DAYS = 120        # signatures older than this are forgotten (such a day is not resent anyway)
RECENT_DAYS = 2        # routine passes recompute only the last UTC days: old days cannot change, so reading every record each turn is wasted work
FULL_EVERY = 7 * 86400  # ...and one full pass a week (first run, after a login, `aihub usage-sync`) catches anything else
STATE = "usage-sync.json"


def enabled():
    v = str(os.environ.get("AIHUB_USAGE_SYNC") or paths.config().get("usage_sync") or "on").lower()
    return v not in ("off", "0", "false", "no") and paths.hub_configured() and bool(paths.load("credentials.json", {}).get("token"))


def _q(name):
    return paths.p("queue", name)


def _state():
    s = paths.load(STATE, {})
    return s if isinstance(s, dict) else {}


def _save(s):
    try:
        paths.save(STATE, s)
    except OSError:
        pass


def _acquire():
    """Become the one worker (O_EXCL). A lock whose heartbeat is stale belonged to a dead process: take it over."""
    lock = _q("usage.lock")
    try:
        paths.ensure("queue")                                      # the folder may have been removed (self-uninstall, a cleanup): no folder must not mean "never again"
    except OSError:
        return False
    for _ in range(2):
        try:
            os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) < STALE:
                    return False
                os.remove(lock)
            except OSError:
                return False
        except OSError:
            return False
    return False


def _release():
    try:
        os.remove(_q("usage.lock"))
    except OSError:
        pass


def _beat():
    try:
        os.utime(_q("usage.lock"), None)
    except OSError:
        pass


def _mark():
    """The dirty stamp: a turn ended now. One tiny file write."""
    paths.ensure("queue")
    with open(_q("usage.dirty"), "w") as f:
        f.write(str(time.time()))


def _dirty_at():
    try:
        return os.path.getmtime(_q("usage.dirty"))
    except OSError:
        return 0.0


def kick():
    """Hook hot path (end of an answer turn): mark and start the detached worker. Microseconds of work; never raises."""
    try:
        if not enabled():
            return
        _mark()
        if time.time() < _state().get("retry_at", 0) or not _acquire():
            return
        from . import telemetry
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        if telemetry.WINDOWS:
            kw["creationflags"] = 0x08000000 | 0x00000200
        else:
            kw["start_new_session"] = True
        try:
            subprocess.Popen(telemetry.self_cmd() + ["usage-sync", "--background"], **kw)
        except Exception:
            _release()
    except Exception:
        pass


def totals(recs=None):
    """{(day, tool, model): row} of every local record, days in UTC (the hub's day)."""
    from . import usage
    import datetime
    recs = usage.collect(usage.TOOLS) if recs is None else recs
    out, rates, days = {}, {}, {}
    for r in recs:
        k = int(r["ts"]) // 3600
        if k not in days:
            days[k] = datetime.datetime.fromtimestamp(k * 3600, datetime.timezone.utc).strftime("%Y-%m-%d")
        key = (days[k], r["tool"], r["model"])
        a = out.get(key)
        if a is None:
            a = out[key] = {"day": key[0], "tool": key[1], "model": key[2], "requests": 0, "input": 0, "output": 0, "cache_write": 0,
                            "cache_read": 0, "cost": 0.0, "unpriced": 0}
        a["requests"] += 1
        for f in ("input", "output", "cache_write", "cache_read"):
            a[f] += r[f]
        c = usage.cost(r, rates)
        if c is None:
            a["unpriced"] += 1
        else:
            a["cost"] += c
    for a in out.values():
        a["cost"] = round(a["cost"], 6)
    return out


def session_rows(recs):
    """One summary per session, numbers only (no title, prompt or folder): {sid, day (UTC of the last request), tool, model (the costliest),
    start, end, requests, input, output, cache_write, cache_read, cost, unpriced, peak (largest context of one request), cold (cache
    rebuilt after an idle gap), compact (context fell by half), big (context over 150k), light (premium model, short reply)}. The id is a
    hash, so the hub never learns the log file's name. Same definitions as the local `aihub usage` tips."""
    import hashlib
    from . import usage
    out = []
    for sess in usage.sessions_of(recs, None, {}):
        sess["reqs"].sort(key=lambda r: r["ts"])
        notes = usage.notes_of(sess)
        light = sum(1 for r in sess["reqs"] if "opus" in r["model"].lower() and r["output"] < 300 and usage.ctx(r) < 30000)
        out.append({"sid": hashlib.sha256(("%s|%s" % (sess["tool"], sess["session"])).encode()).hexdigest()[:24], "tool": sess["tool"],
                    "model": sess["model"], "day": _utc_day(sess["end"]), "start": int(sess["start"]), "end": int(sess["end"]),
                    "requests": sess["requests"], "input": sess["input"], "output": sess["output"], "cache_write": sess["cache_write"],
                    "cache_read": sess["cache_read"], "cost": round(sess["cost"], 6), "unpriced": sess["unpriced"], "peak": sess["peak"],
                    "cold": sum(1 for n in notes if "cold" in n), "compact": sum(1 for n in notes if "compact" in n),
                    "big": sum(1 for n in notes if "big" in n), "light": light})
    return out


def _utc_day(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def _sig(a):
    return [a["requests"], a["input"], a["output"], a["cache_write"], a["cache_read"], a["cost"], a["unpriced"]]


def _post(client, rows, sessions=()):
    """-> True. Raises Busy (hub asks to slow down), OSError (cannot reach it / not supported / not signed in)."""
    import urllib.error
    import urllib.request
    from . import api
    body = json.dumps(textsafe.deep({"client_id": client, "rows": rows, "sessions": list(sessions)}), separators=(",", ":")).encode()
    h = {"Content-Type": "application/json", "User-Agent": "aihub-cli",
         "Authorization": "Bearer " + str(paths.load("credentials.json", {}).get("token") or "")}
    if len(body) > 512:
        body, h["Content-Encoding"] = gzip.compress(body, 6, mtime=0), "gzip"
    rq = urllib.request.Request(paths.config()["hub"].rstrip("/") + "/api/v1/usage/sync", data=body, headers=h, method="POST")
    try:
        with api._opener.open(rq, timeout=TIMEOUT) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        if e.code in (429, 502, 503, 504):
            try:
                ra = float(e.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                ra = 0
            raise Busy(ra)
        raise OSError("hub answered %d" % e.code)      # 401 stale login, 404 old hub (no route), 400/413: wait before trying again


class Busy(Exception):
    def __init__(self, retry_after=0):
        super().__init__("hub busy")
        self.retry_after = retry_after


def push(recs=None, full=False):
    """One upload of what changed since the last success. -> number of rows sent (daily rows + session summaries). Raises like _post.
    Cheap by default: only the last RECENT_DAYS UTC days are totalled (a full pass when never done, older than FULL_EVERY, or full=True).
    A day or session whose local total got SMALLER than what was already sent (the tool deleted old logs) is not sent again: history stays
    on the hub. The state file keeps one short signature per key, and keys older than KEEP_DAYS are dropped from it, so it stays small."""
    from . import hooks
    st = _state()
    hub, user = paths.config()["hub"], paths.load("credentials.json", {}).get("username")
    sent = st.get("sent") if isinstance(st.get("sent"), dict) else {}
    ssent = st.get("ssent") if isinstance(st.get("ssent"), dict) else {}
    if st.get("hub") != hub or st.get("user") != user:
        sent, ssent, st = {}, {}, {}                               # another hub or account: everything is new there
    now = time.time()
    full = full or now - st.get("full", 0) > FULL_EVERY
    if recs is None:
        from . import usage
        recs = usage.collect(usage.TOOLS)
    if not full:
        lo = (int(now) // 86400 - RECENT_DAYS) * 86400
        recs = [r for r in recs if r["ts"] >= lo]
    client = hooks.client_id()
    todo = {}
    for k, v in totals(recs).items():
        key, sig = "|".join(k), _sig(v)
        old = sent.get(key)
        if old == sig or (old and sum(sig[1:5]) < sum(old[1:5])):
            continue
        todo[key] = v
    stodo = {}
    for v in session_rows(recs):
        sig = [v["requests"], v["input"] + v["output"] + v["cache_write"] + v["cache_read"], v["peak"], v["cost"]]
        old = ssent.get(v["sid"])
        if old == sig or (old and sig[1] < old[1]):
            continue
        stodo[v["sid"]] = (v, sig)
    keys, skeys = list(todo), list(stodo)
    n = max(len(keys), len(skeys))
    for i in range(0, max(n, 1), CHUNK):
        part, spart = keys[i:i + CHUNK], skeys[i:i + CHUNK]
        if not part and not spart:
            break
        _beat()
        _post(client, [todo[k] for k in part], [stodo[k][0] for k in spart])
        for k in part:
            sent[k] = _sig(todo[k])
        for k in spart:
            ssent[k] = stodo[k][1]
        _save({"sent": sent, "ssent": ssent, "hub": hub, "user": user, "ok": time.time(), "full": st.get("full", 0)})
    if full:
        horizon = time.strftime("%Y-%m-%d", time.gmtime(now - KEEP_DAYS * 86400))
        sent = {k: v for k, v in sent.items() if k.split("|", 1)[0] >= horizon}      # the state file must not grow for ever
        s = _state()
        s.update(full=time.time(), sent=sent, ssent=ssent if len(ssent) < 20000 else dict(list(ssent.items())[-20000:]), hub=hub, user=user)
        _save(s)
    return len(keys) + len(skeys)


def _fail(wait=0):
    s = _state()
    n = int(s.get("fails", 0)) + 1
    s.update(fails=n, retry_at=time.time() + max(wait, min(BACKOFF_BASE * 2 ** (n - 1), BACKOFF_MAX)))
    _save(s)


def run(background=False, window=WINDOW, full=False):
    """The worker. background: hold the lock taken by kick(), wait one window, then upload (again while turns keep ending). Foreground
    (`aihub usage-sync`): upload now. -> rows sent."""
    total = 0
    held = background
    try:
        paths.ensure("queue")
        if not background:
            if not _acquire():
                return 0
            held = True
        try:
            from . import hooks
            hooks.verify()                                         # we are sending usage: make sure the hooks that trigger it are still in place
        except Exception:
            pass
        for i in range(PASSES + 1):
            if not enabled():
                break
            if background and window:
                end = time.time() + window
                while time.time() < end:
                    _beat()
                    time.sleep(min(2, max(0.1, end - time.time())))
            began = time.time()
            try:
                total += push(full=full)
                s = _state()
                s.pop("fails", None)
                s.pop("retry_at", None)
                _save(s)
            except Busy as e:
                _fail(e.retry_after)
                break
            except Exception as e:
                run.error = str(e)[:200]
                _fail()
                break
            if _dirty_at() <= began:                               # no turn ended while we worked: done
                break
    except Exception:
        pass
    finally:
        if held:
            _release()
    return total


run.error = ""
