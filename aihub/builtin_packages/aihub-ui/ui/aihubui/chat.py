"""One chat turn = one `claude -p` process (the Claude Code harness) in the chat's working folder.

Prompt-cache friendly by construction:
  * the system prompt addition is built ONCE per chat (soul + note index, no clock, sorted) and stored; every turn passes the same bytes
  * the model, tools and MCP list stay the same inside a chat (the page warns before a change: it rewrites the cache)
  * turns continue the same Claude session (--resume), so the long prefix is read from cache, not written again
Claude's own login and settings.json (model, env, hooks, MCP, skills, agents) apply because the real CLI runs."""
import json
import os
import re
import signal
import subprocess
import threading
import time

from . import auth, freechat, store
from .aihub import HOME, CLAUDE_HOME, settings_path

MODES = {"auto": "auto", "edits": "acceptEdits", "plan": "plan", "ask": "dontAsk", "bypass": "bypassPermissions"}
EFFORT = ("low", "medium", "high", "xhigh", "max")
procs = {}
plock = threading.Lock()

CANVAS_HELP = """You run inside AI Hub UI. Answers render rich blocks from fenced code: ```sh for a shell session (lines starting with $ are commands, others output), markdown tables, ```chart for a graph
(JSON: {"type":"bar|line|pie","title":"...","labels":["a","b"],"series":[{"name":"x","data":[1,2]}]}) and ```form to ask the user for several inputs at once
(JSON: {"title":"...","fields":[{"name":"k","label":"...","type":"text|textarea|number|select|checkbox","options":["a","b"],"required":true}],"submit":"Send"}); the submitted values come back as the next user message.
Diagrams are drawn from code, so write code, not hand-made SVG: ```mermaid (flowchart, sequenceDiagram, mindmap, classDiagram, stateDiagram-v2, erDiagram, gantt, timeline, journey, gitGraph, pie),
```dot (Graphviz: graphs, networks, trees, dependency maps; `layout=neato|circo|twopi|fdp` picks another engine), ```echarts (a JSON ECharts option, no functions: graph, tree, treemap, sunburst, sankey, radar, heatmap, scatter, gauge...)
and ```map (JSON {"center":[lat,lng],"zoom":5,"markers":[{"lat":0,"lng":0,"label":"..."}],"lines":[{"points":[[lat,lng],[lat,lng]],"label":"..."}],"geojson":{...}} on OpenStreetMap). Use ```svg only for an icon, logo or illustration that code cannot express (self-contained, viewBox set, no script). Prefer ```chart for simple bar/line/pie numbers.
To ask the user to choose, use a ```ask block (JSON: {"questions":[{"header":"short tag","question":"...","multiSelect":false,"options":[{"label":"...","description":"..."}]}]}); the page shows a card and the
answers come back as the next user message. After asking, stop and wait. Do not use the AskUserQuestion tool; it cannot be answered in this mode.
Slides and documents: write ONE self-contained .html file. A deck has <meta name="aihub-kind" content="slides"> and `<section class="slide">` blocks of exactly 1280x720 CSS px (one idea each, h1/h2, short ul, img, table;
speaker notes in <aside class="notes">); a document has <meta name="aihub-kind" content="doc"> and one <article class="doc"> (h1-h3, p, ul/ol, table, img, pre). Take every color and font from CSS variables on :root
(--bg --fg --muted --accent --accent2 --surface --font-head --font-body) so a theme can replace them; use system fonts and local images only; slides and documents carry no scripts. The canvas presents the file as a slideshow or paged document
and exports PowerPoint, Word, PDF or Markdown. If the user's message carries a [Theme "Name" ...] block, use those variable values exactly.
Use them when they make the answer clearer; plain prose otherwise.
 Your working folder is where this chat works (the user may have chosen it): put every file you create there.
A side canvas shows the folder live: an .html file you write (relative assets are fine; JavaScript works in full, so reports and dashboards can be fully interactive with their data inline) appears in a live preview,
so for a page, chart, diagram or small app write one index.html instead of pasting long code in the reply. Markdown, SVG and images render too.
For a dashboard, form, table or cards you may instead answer with an OpenUI Lang block (a ```openui fenced block, language rules below): the canvas renders it with OpenUI components.
Files the user attaches are saved under attachments/. Keep answers short; do not repeat file contents you just wrote.
When the user asks you to build a workflow / work / automation (a flow of nodes that runs on a schedule, a message or a click), answer with a short explanation and ONE ```workflow block holding the JSON {"title","description","nodes":[{"id","type","label","x","y","config"}],"edges":[{"from","to"}]}; the page shows a button that saves it in Works (switched off) and opens it. Node types: start.manual, start.cron, git.pull, io.path, io.read_file, io.request, ai.claude, ai.free, flow.filter, flow.template, flow.loop, flow.endloop, flow.output, space.publish, send.teams, and package nodes written pkg:<package>/<id> (for example pkg:aihub-nodes-outlook/outlook-send for Outlook mail, when that package is installed). git.pull / io.path pass a PATH, not content: a later ai.claude reads the files itself and should end with `OUTPUT: /full/path` when it creates one."""


def knowledge_index():
    d = os.path.join(HOME(), "knowledge")
    rows = []
    try:
        for f in sorted(os.listdir(d)):
            if f.endswith(".md") and f != "soul.md":
                with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                    first = next((ln.strip("# \n") for ln in fh if ln.strip()), "")
                rows.append("- %s: %s" % (f[:-3], first[:90]))
    except OSError:
        pass
    return rows[:60]


def build_system(extra="", soul=None):
    """Frozen text for a new chat. Same inputs -> same bytes (cache hit across chats that start from the same notes).
    SOUL = this chat's own persona text instead of the soul note (None = use the note)."""
    parts = [CANVAS_HELP]
    try:
        if soul is None:
            with open(os.path.join(HOME(), "knowledge", "soul.md"), encoding="utf-8") as f:
                soul = f.read()
        soul = soul.strip()
        if soul:
            parts.append("# Soul (the user's persona note)\n" + soul[:6000])
    except OSError:
        pass
    idx = knowledge_index()
    if idx:
        parts.append("# Knowledge notes (read one with: aihub knowledge get NAME)\n" + "\n".join(idx))
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


class Run:
    """One answer turn running in the background: the events it produced so far. The turn does not depend on a page being connected;
    a page (or several, or none) follows it with `follow`, also after it left and came back."""

    def __init__(self, base):
        self.cond = threading.Condition()
        self.events = []
        self.done = False
        self.cancel = threading.Event()       # set by stop(): a turn that has not started claude yet must not start it
        self.base = base                      # history lines before this run (a returning page loads those, then replays the run)
        self.since = time.time()

    def add(self, ev):
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def finish(self):
        with self.cond:
            self.done = True
            self.cond.notify_all()


runs = {}


def active(sid):
    with plock:
        r = runs.get(sid)
    return r if r and not r.done else None


def start(sess, *a, **kw):
    """Run turn() in a thread. -> the Run, or None when this chat is already answering."""
    sid = sess["id"]
    engine, files = kw.pop("engine", "claude"), kw.pop("files", None)           # "free" = the UI's own harness (no Claude Code)
    if engine == "free":
        kw["files"] = files
    base = store.count(sid)                                         # in memory after the first call; never counted while plock is held
    with plock:
        old = runs.get(sid)
        if old and not old.done:
            return None
        run = runs[sid] = Run(base)
        stopped.discard(sid)

    def go():
        try:
            for ev in (freechat.turn if engine == "free" else turn)(sess, *a, **kw):
                run.add(ev)
        except Exception as e:                                       # the thread must always end the run
            ev = {"k": "error", "text": "%s: %s" % (type(e).__name__, e)}
            store.append(sid, ev)
            run.add(ev)
            run.add({"k": "done"})
        finally:
            run.finish()
            t = threading.Timer(120, lambda: _forget(sid, run))
            t.daemon = True
            t.start()
    threading.Thread(target=go, daemon=True).start()
    return run


def _forget(sid, run):
    with plock:
        if runs.get(sid) is run:
            runs.pop(sid, None)


def _merge(evs):
    """Consecutive text/thinking deltas as one event: what a page that comes back needs to redraw the answer so far."""
    out = []
    for e in evs:
        if out and e["k"] in ("delta", "think") and out[-1]["k"] == e["k"]:
            out[-1] = {"k": e["k"], "text": out[-1]["text"] + e["text"]}
        else:
            out.append(e)
    return out


def follow(sid):
    """Generator of a running (or just finished) turn's events from its start. Ends after `done`; returns at once when nothing ran."""
    with plock:
        run = runs.get(sid)
    if not run:
        yield {"k": "done"}
        return
    with run.cond:
        pos = len(run.events)
        first = _merge(run.events[:pos])
    for e in first:
        yield e
    while True:
        with run.cond:
            while len(run.events) == pos and not run.done:
                run.cond.wait(15)
                if len(run.events) == pos and not run.done:
                    break                                           # nothing new: let the caller send a keep-alive
            new = run.events[pos:]
            pos = len(run.events)
            end = run.done and not new
        if end:
            return
        if not new:
            yield {"k": "ping"}
        for e in new:
            yield e


stopped = set()


def _kill(p):
    """End the child and what it started: its process group (POSIX) or the whole tree (Windows, where there is no killpg)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        else:
            os.killpg(p.pid, signal.SIGTERM)
    except Exception:
        try:
            p.terminate()
        except Exception:
            pass


def stop(sid):
    if freechat.cancel(sid):
        return True
    with plock:
        p = procs.get(sid)
        r = runs.get(sid)
    if r and not r.done:                                             # also a turn that is still preparing (before claude exists)
        r.cancel.set()
        stopped.add(sid)
    if p and p.poll() is None:
        stopped.add(sid)
        _kill(p)
        return True
    return bool(r and not r.done)


def _cancelled(sid):
    with plock:
        r = runs.get(sid)
    return sid in stopped or bool(r and not r.done and r.cancel.is_set())


ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def clean_env(extra):
    """Only real environment variable names reach the child; secret box entries such as node:<work>:<node>:<field> never do."""
    return {k: str(v) for k, v in (extra or {}).items() if isinstance(k, str) and ENV_NAME.match(k) and not k.startswith("node:") and v is not None}


def busy(sid):
    with plock:
        p = procs.get(sid)
    return bool(p and p.poll() is None)


def window(model=""):
    """Context window in tokens the UI assumes: the Settings value (default 1M)."""
    return store.prefs()["window"]


def _usage(u):
    u = u or {}
    return {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
            "cache_read": u.get("cache_read_input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0)}


def _blocks(content):
    out = []
    for b in content if isinstance(content, list) else []:
        t = b.get("type")
        if t == "text" and b.get("text"):
            out.append({"type": "text", "text": b["text"]})
        elif t == "tool_use":
            out.append({"type": "tool", "id": b.get("id"), "name": b.get("name"), "input": b.get("input")})
        elif t == "thinking" and b.get("thinking"):
            out.append({"type": "thinking", "text": b["thinking"]})
    return out


def _result_text(c):
    if isinstance(c, str):
        return c
    return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text") if isinstance(c, list) else ""


_cj = {"stamp": None, "data": {}}


def _claude_json():
    """~/.claude.json (can be MBs) parsed again only when the file changed."""
    p = os.path.join(os.path.expanduser("~"), ".claude.json")
    st = os.stat(p)
    stamp = (st.st_mtime_ns, st.st_size)
    if _cj["stamp"] != stamp:
        with open(p, encoding="utf-8") as f:
            _cj["data"] = json.load(f)
        _cj["stamp"] = stamp
    return _cj["data"]


def mcp_config():
    """Path of an --mcp-config file for `claude -p`, or None. The chat runs in its own folder, so servers Claude Code keeps per
    PROJECT (~/.claude.json `projects.<dir>.mcpServers`, the default scope of `claude mcp add`) would never load here: they are
    merged in (user scope and the UI list win), so what works in a terminal also works in the chat."""
    merged = {}
    try:
        d = _claude_json()
        for proj in (d.get("projects") or {}).values():
            for n, v in ((proj or {}).get("mcpServers") or {}).items():
                if isinstance(v, dict):
                    merged[n] = v
        for n, v in (d.get("mcpServers") or {}).items():
            if isinstance(v, dict):
                merged[n] = v
    except (OSError, ValueError, AttributeError):
        pass
    for n, v in (store.ui_mcp().get("mcpServers") or {}).items():
        if isinstance(v, dict):
            merged[n] = v
    if not merged:
        return None
    path = os.path.join(store.ui_dir(), "mcp.effective.json")
    store._write(path, {"mcpServers": merged})
    return path


def turn(sess, text, model="", effort="", mode="auto", extra_env=None, mcp=None, add_dirs=None):
    """Generator of UI events for one user message. Stores them in the chat history as it goes."""
    sid = sess["id"]
    if busy(sid):
        yield {"k": "error", "text": "this chat is still answering"}
        return
    ok = auth.ready()                                               # apiKeyHelper needing a sign-in: say so instead of failing inside claude
    if not ok["ok"]:
        ev = {"k": "error", "text": "Sign-in needed: " + ok["error"], "auth": True}
        store.append(sid, ev)
        yield ev
        return
    cur = store.get_session(sid)
    if not cur:                                                     # deleted while queued: no folder, no history
        yield {"k": "done"}
        return
    if "system" not in sess:                                         # a slim row from the list: use the frozen prompt from the files
        sess = cur
    if not active(sid):
        stopped.discard(sid)                                         # (a Run from start() cleared it already; a stop since then must survive)
    if _cancelled(sid):
        stopped.discard(sid)
        yield {"k": "done", "stopped": True}
        return
    cwd = store.session_dir(sid)
    os.makedirs(cwd, exist_ok=True)
    prep = {"before": {}}

    def snap():                                                      # runs while claude boots; the prompt is sent only after it ends
        files = store.tree(sid)
        prep["before"] = {f["path"]: (f["mtime"], f["size"]) for f in files}               # to list what this turn created or changed
        try:
            store.snapshot(sid, files)                                                     # what Diff compares against
        except OSError:
            pass
    snap_t = threading.Thread(target=snap, daemon=True)
    snap_t.start()
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--permission-mode", MODES.get(mode, "auto")]
    sp, custom = settings_path()
    if custom:
        cmd += ["--settings", sp]                                    # the file chosen in Settings, merged over ~/.claude/settings.json
    cmd += ["--resume", sess["claude"]] if sess.get("started") else ["--session-id", sess["claude"]]
    if model:
        cmd += ["--model", model]
    if effort in EFFORT:
        cmd += ["--effort", effort]
    if sess.get("system"):
        sp = os.path.join(store.ui_dir("system"), sid + ".md")      # a file, not argv: the prompt can be tens of KB
        if not os.path.exists(sp) or open(sp, encoding="utf-8").read() != sess["system"]:
            with open(sp, "w", encoding="utf-8") as f:
                f.write(sess["system"])
        cmd += ["--append-system-prompt-file", sp]
    kd = os.path.join(HOME(), "knowledge")
    if os.path.isdir(kd):
        cmd += ["--add-dir", kd]
    for ad in add_dirs or ():                                        # folders a Works pointer names: Claude may read them
        if os.path.isdir(ad):
            cmd += ["--add-dir", ad]
    mcp = mcp_config() or mcp
    if mcp:
        cmd += ["--mcp-config", mcp]
    if sess.get("work"):                                             # a Works run: nobody can answer a question (auto mode's classifier decides tools)
        cmd += ["--disallowedTools", "AskUserQuestion"]
    env = dict(os.environ)
    env.update(store.prefs_env())
    env.update(clean_env(extra_env))
    env["AIHUB_UI_SESSION"] = sid
    if _cancelled(sid):
        snap_t.join()
        stopped.discard(sid)
        yield {"k": "done", "stopped": True}
        return
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except OSError as e:
        snap_t.join()
        ev = {"k": "error", "text": "cannot start claude: %s (install Claude Code and sign in with `claude`)" % e}
        store.append(sid, ev)
        yield ev
        return
    with plock:
        procs[sid] = p
    if _cancelled(sid):                                              # Stop arrived while claude was starting
        _kill(p)
    errbuf = []
    threading.Thread(target=lambda: errbuf.extend(p.stderr.readlines()[-20:]), daemon=True).start()
    snap_t.join()                                                    # the snapshot must hold the files as they were before claude sees the prompt
    before = prep["before"]
    try:
        p.stdin.write(text)
        p.stdin.close()
    except (OSError, ValueError):
        pass
    store.update_session(sid, started=True, model=model, effort=effort, mode=mode)       # (no write when nothing changed)
    ctx = None                                                       # context size: kept here, written once when the turn ends
    got_result = False
    t0 = time.time()
    try:
        for ln in p.stdout:
            try:
                m = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(m, dict):                              # "null" or a bare number on stdout (a killed child): not an event
                continue
            t = m.get("type")
            if t == "stream_event":
                e = m.get("event") or {}
                d = e.get("delta") or {}
                if e.get("type") == "content_block_delta":
                    if d.get("type") == "text_delta":
                        yield {"k": "delta", "text": d.get("text", "")}
                    elif d.get("type") == "thinking_delta":
                        yield {"k": "think", "text": d.get("thinking", "")}
            elif t == "system" and m.get("subtype") == "compact_boundary":
                c = m.get("compact_metadata") or {}
                ev = {"k": "compact", "pre": c.get("pre_tokens", 0), "post": c.get("post_tokens", 0), "trigger": c.get("trigger", "auto")}
                store.append(sid, ev)
                ctx = ev["post"]
                yield ev
                yield {"k": "ctx", "used": ev["post"]}
            elif t == "assistant":
                u = _usage((m.get("message") or {}).get("usage"))
                used = u["input"] + u["cache_read"] + u["cache_write"]
                if used:
                    ctx = used
                    yield {"k": "ctx", "used": used}
                blocks = _blocks((m.get("message") or {}).get("content"))
                if blocks:
                    ev = {"k": "msg", "blocks": blocks}
                    store.append(sid, ev)
                    yield ev
                    for b in blocks:
                        if b["type"] == "tool" and b["name"] in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                            yield {"k": "file", "path": (b.get("input") or {}).get("file_path", "")}
            elif t == "user":
                for b in (m.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        ev = {"k": "tool_result", "id": b.get("tool_use_id"), "text": _result_text(b.get("content"))[:4000], "error": bool(b.get("is_error"))}
                        store.append(sid, ev)
                        yield ev
            elif t == "result":
                got_result = True
                made = [{"path": f["path"], "size": f["size"], "new": f["path"] not in before} for f in store.tree(sid) if before.get(f["path"]) != (f["mtime"], f["size"])]
                if made:                                                  # whatever made them (Write, a shell command, a script)
                    ev = {"k": "files", "items": sorted(made, key=lambda x: (not x["new"], x["path"]))[:16]}
                    store.append(sid, ev)
                    yield ev
                u = _usage(m.get("usage"))
                u["cost"] = m.get("total_cost_usd") or 0
                store.add_usage(sid, u)
                ev = {"k": "result", "usage": u, "ms": m.get("duration_ms") or int((time.time() - t0) * 1000), "error": bool(m.get("is_error")),
                      "text": m.get("result") if m.get("is_error") else ""}
                store.append(sid, ev)
                yield ev
                if ev["error"] or m.get("subtype", "success") != "success":
                    why = (m.get("result") or "").strip() or "".join(errbuf).strip() or "claude stopped: %s" % m.get("subtype", "error")
                    err = {"k": "error", "text": why[-1200:]}      # the API's own message, not only a "failed" tag after the thinking
                    store.append(sid, err)
                    yield err
            elif t == "system" and m.get("subtype") == "api_retry":
                ev = {"k": "note", "text": "API error%s, retrying (%s/%s): %s" % (
                    " " + str(m["error_status"]) if m.get("error_status") else "", m.get("attempt", "?"), m.get("max_retries", "?"), m.get("error") or "")}
                store.append(sid, ev)
                yield ev
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill(p)
    except GeneratorExit:                                            # whoever drives turn() went away: stop the child
        stop(sid)
        raise
    finally:
        with plock:
            procs.pop(sid, None)
        if ctx is not None:
            store.update_session(sid, ctx=ctx)                       # one write per turn (a crash loses only this number)
    if not got_result and sid in stopped:
        stopped.discard(sid)                                         # the user pressed Stop: not an error
    elif not got_result:
        ev = {"k": "error", "text": ("".join(errbuf).strip() or "claude ended without an answer (exit %s)" % p.returncode)[-600:]}
        store.append(sid, ev)
        yield ev
    yield {"k": "done"}
