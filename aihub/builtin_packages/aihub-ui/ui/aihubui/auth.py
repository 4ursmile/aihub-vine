"""Claude Code's `apiKeyHelper` (settings.json): a command that prints an API key / token, often after an OAuth login whose token the
helper saves itself. Claude Code runs it on its own; this module adds what a headless UI needs:

  * check()  runs the helper once (the same way Claude does) to learn whether it works now. The key it prints is read only to see that it is
             not empty and is NEVER stored, logged or returned; stderr is returned with the key removed.
  * login()  runs the helper in the background with a long timeout and collects stderr lines, so a device-code or sign-in URL it prints
             reaches the page. The helper stores its own token (that is what it is for); we only remember "valid until" in memory.
  * ready()  preflight for a chat turn or a work node: fresh within the helper's TTL, else check(). A failure carries needs_login."""
import json
import os
import subprocess
import threading
import time

from .aihub import settings_files

DEFAULT_TTL = 300
_lock = threading.Lock()
state = {"valid_until": 0.0, "last_error": "", "login": None}


def _settings():
    out = {}
    for fn in settings_files():                                         # local overrides, as Claude Code merges them
        try:
            with open(fn, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                out.update({k: v for k, v in d.items() if k != "env"})
                out.setdefault("env", {}).update(d.get("env") or {})
        except (OSError, ValueError):
            pass
    return out


def helper():
    s = _settings().get("apiKeyHelper")
    return s if isinstance(s, str) and s.strip() else None


def ttl():
    try:
        return max(30, int(_settings().get("env", {}).get("CLAUDE_CODE_API_KEY_HELPER_TTL_MS", DEFAULT_TTL * 1000)) / 1000)
    except (TypeError, ValueError):
        return DEFAULT_TTL


def _clean(text, key):
    text = (text or "")[-4000:]
    return text.replace(key, "***") if key else text


def check(timeout=45):
    """-> {"ok": bool, "error": str, "needs_login": bool}. Never contains the key."""
    cmd = helper()
    if not cmd:
        return {"ok": True, "error": "", "needs_login": False}
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=os.path.expanduser("~"))
    except subprocess.TimeoutExpired:
        return _fail("the key helper took longer than %ds (it probably waits for a sign-in)" % timeout)
    key = p.stdout.strip()
    if p.returncode != 0 or not key:
        return _fail(_clean(p.stderr, key).strip() or "the key helper printed no key (exit %d)" % p.returncode)
    with _lock:
        state["valid_until"] = time.time() + ttl()
        state["last_error"] = ""
    return {"ok": True, "error": "", "needs_login": False}


def _fail(msg):
    with _lock:
        state["valid_until"] = 0.0
        state["last_error"] = msg
    return {"ok": False, "error": msg, "needs_login": True}


def ready():
    if not helper():
        return {"ok": True, "error": "", "needs_login": False}
    if time.time() < state["valid_until"]:
        return {"ok": True, "error": "", "needs_login": False}
    return check()


def status():
    cmd = helper()
    lg = state["login"]
    return {"helper": bool(cmd), "program": (cmd or "").split()[0].split("/")[-1] if cmd else "", "valid": time.time() < state["valid_until"],
            "last_error": state["last_error"], "login": {"running": lg["running"], "lines": lg["lines"][-40:], "ok": lg.get("ok")} if lg else None}


def login(timeout=300):
    """Start the helper in the background (once). Poll status()['login']."""
    cmd = helper()
    if not cmd:
        raise ValueError("no apiKeyHelper in Claude settings.json")
    with _lock:
        if state["login"] and state["login"]["running"]:
            return
        lg = state["login"] = {"running": True, "lines": [], "ok": None}

    def work():
        key = ""
        try:
            p = subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 cwd=os.path.expanduser("~"), start_new_session=True)
            timer = threading.Timer(timeout, p.kill)
            timer.start()
            out = []
            threading.Thread(target=lambda: out.append(p.stdout.read()), daemon=True).start()
            for ln in p.stderr:
                lg["lines"].append(ln.rstrip()[:400])
            p.wait()
            timer.cancel()
            time.sleep(0.2)
            key = (out[0] if out else "").strip()
            lg["ok"] = p.returncode == 0 and bool(key)
            if lg["ok"]:
                with _lock:
                    state["valid_until"] = time.time() + ttl()
                    state["last_error"] = ""
            else:
                lg["lines"].append("The helper ended with exit %s and %s key." % (p.returncode, "a" if key else "no"))
        except Exception as e:
            lg["lines"].append("could not run the helper: %s" % e)
            lg["ok"] = False
        finally:
            lg["lines"] = [_clean(x, key) for x in lg["lines"]]
            lg["running"] = False
    threading.Thread(target=work, daemon=True).start()
