"""Magic ask: an LLM fills in the settings of ONE Works node from what you describe, code included ("read yesterday's orders from
Postgres and sum them per country" -> the SQL, the driver, the output). The answer is checked against the node's own fields before the
page shows it; nothing is applied until you press Apply.

Model call: a direct API call (POST /v1/messages, same credentials as Free chat) when a key is configured, else a one-shot `claude -p`
without tools or session. Never sent: secret fields (passwords, tokens, webhook URLs), the credential sets, the vault. Never filled:
secret fields and the environment field (your machine decides those)."""
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

from . import store

MAX_OUT = 8000
VALUE_CAP = 12000
FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]+field=([A-Za-z_][A-Za-z0-9_]*)[^\n]*\n(.*?)\n[ \t]*```", re.S)
JSON_FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)\n[ \t]*```", re.S)

SYSTEM = """You set up ONE node of an AI Hub "Works" flow (a visual automation) for the user. You get the node's type, its description and
guide, every settings field (name, type, options, help, current value), what flows into it from earlier nodes (texts and {{data.x}} keys
with examples), its last run (input, output, error, log) and the Python it runs with. Do what the user asks: fill in or change the fields
so the node does it. When the last run failed, find the cause in its error and log and fix it.

Reply with exactly:
1. ONE ```json block: {"config": {"<field name>": <value>, ...}, "note": "<one or two sentences: what you set and anything the user must still do>"}
   Only fields that should change. select fields: one of the listed options, exactly. checkbox: true/false. number: a number.
2. For every field of type code (and long textarea text), leave it OUT of the JSON and put the full new value in its own fence instead:
   ```<language> field=<field name>
   ...complete text...
   ```
Rules:
- Never invent passwords, tokens or keys and never put a login into code, SQL, URLs or headers (secret fields and credential sets are
  filled by the user; code reads granted sets with `vault.credential("name")["field"]`).
- Use {{input}}, {{in.<parent>}} and {{data.key}} only for values that really come from earlier nodes (listed under "Flows in"); use the
  example values to get names, shapes and types right.
- Code must be complete and runnable (imports, no placeholders like ...), short, with brief comments only where useful, using only the
  installed packages unless the user asks for more (then say in the note what to install).
- A code.python node does NOT have to match the nodes around it: a plain script or an untyped main(args) is fine, and returning nothing
  passes the input on unchanged (a side step: log, save, call an API, check and raise). Return a dict only when later nodes need its keys.
- Prompts for AI nodes are specific: what to read, what to produce, in what shape; when a later node needs a file, the prompt asks for it
  and ends with the line `OUTPUT: /full/path`.
- If the request cannot be done with this node, change nothing and say in the note which node type would do it."""


def _short(v, n=VALUE_CAP):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "\n...[cut]"


def editable(f):
    return not f.get("secret") and f.get("type") not in ("env", "password", "model", "mode", "effort", "folder")


def brief(spec, config, ask, field=None, context=None):
    """The user message: the node, its fields with their current values, what flows in, the last run, the request."""
    config, context = config or {}, context or {}
    L = ["# Node: %s (%s)" % (spec.get("title") or spec["type"], spec["type"]), spec.get("desc") or ""]
    if spec.get("guide"):
        L += ["", "## Guide", spec["guide"]]
    L += ["", "## Fields"]
    for f in spec.get("fields") or []:
        if f["name"] == "filter":
            continue
        cur = config.get(f["name"], f.get("default", ""))
        line = "- %s (%s%s)%s: %s" % (f["name"], f["type"], ", lang " + f["lang"] if f.get("lang") else "", " required" if f.get("required") else "", f.get("label", ""))
        if f.get("options"):
            line += " | options: " + ", ".join(repr(o) for o in f["options"])
        if f.get("help"):
            line += " | " + f["help"]
        if not editable(f):
            line += " | DO NOT SET (%s)" % ("secret, filled by the user" if f.get("secret") else "chosen by the user")
            L.append(line)
            continue
        L.append(line)
        if cur not in ("", None):
            if f["type"] in ("code", "textarea") and isinstance(cur, str) and "\n" in cur:
                L.append("  current value:\n```\n%s\n```" % _short(cur))
            else:
                L.append("  current value: %s" % _short(cur, 600))
    cred = spec.get("credential")
    if cred:
        L.append("\nLogins come from a '%s' credential set the user picks; a field typed in the node overrides the set." % cred.get("kind"))
    ins = context.get("parents") or []
    keys = context.get("keys") or []
    if ins or keys:
        L += ["", "## Flows in"]
        for p in ins[:12]:
            L.append("- {{input}} from %s (%s)%s" % (p.get("name", "?"), p.get("out", "text"), (": " + str(p["note"])[:200]) if p.get("note") else ""))
        for k in keys[:40]:
            L.append("- {{data.%s}} %s%s%s" % (k.get("name", "?"), k.get("from", ""), (": " + str(k["desc"])[:160]) if k.get("desc") else "",
                                                 (" e.g. " + str(k["example"])[:120]) if k.get("example") else ""))
    last = context.get("last") or {}
    if any(last.get(k) for k in ("input", "output", "error")):
        L += ["", "## The last run of this node"]
        for k in ("input", "output", "error", "log"):
            if last.get(k):
                L.append("%s:\n```\n%s\n```" % (k, _short(last[k], 3000)))
    env = context.get("env") or {}
    if env.get("python"):
        L.append("\nRuns with Python %s; installed: %s." % (env.get("version") or "?", ", ".join("%s %s" % (p["module"], p.get("version") or "") for p in env.get("packages") or [] if p.get("installed")) or "nothing extra"))
    if field:
        f = next((x for x in spec.get("fields") or [] if x["name"] == field), None)
        L += ["", "Change ONLY the field `%s`%s." % (field, " (%s)" % f.get("label") if f else "")]
        sel = str(context.get("selection") or "")
        if sel.strip():
            L.append("The user selected this part of it (focus on it, but return the WHOLE field):\n```\n%s\n```" % _short(sel, 4000))
    L += ["", "## Request", str(ask or "").strip()[:4000]]
    return "\n".join(L)


def parse(text):
    """The model's answer -> (config dict, note). Fenced `field=` blocks win over the JSON for the same field."""
    text = str(text or "")
    blocks = {m.group(2): m.group(3) for m in FENCE.finditer(text)}
    rest = FENCE.sub("", text)
    doc = None
    for cand in JSON_FENCE.findall(rest) + [rest.strip()]:
        try:
            v = json.loads(cand.strip())
        except ValueError:
            continue
        if isinstance(v, dict):
            doc = v
            break
    if doc is None:
        m = re.search(r"\{.*\}", rest, re.S)
        if m:
            try:
                doc = json.loads(m.group(0))
            except ValueError:
                doc = None
    if doc is None and not blocks:
        raise ValueError("the answer held no settings")
    doc = doc or {}
    cfg = doc.get("config") if isinstance(doc.get("config"), dict) else {k: v for k, v in doc.items() if k != "note"}
    cfg = dict(cfg)
    cfg.update(blocks)
    return cfg, str(doc.get("note") or "").strip()[:1000]


def clean(spec, raw, config=None, field=None):
    """Keep what the node may take. -> (changes, dropped [{field, why}])."""
    config, by, out, dropped = config or {}, {f["name"]: f for f in spec.get("fields") or []}, {}, []
    for k, v in (raw or {}).items():
        f = by.get(k)
        if not f:
            dropped.append({"field": k, "why": "not a field of this node"})
            continue
        if not editable(f):
            dropped.append({"field": k, "why": "secret or chosen by you" if f.get("secret") else "chosen by you"})
            continue
        if field and k != field:
            dropped.append({"field": k, "why": "only %s was asked" % field})
            continue
        t = f["type"]
        if t == "checkbox":
            v = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "on")
        elif t == "number":
            try:
                n = float(v)
            except (TypeError, ValueError):
                dropped.append({"field": k, "why": "not a number"})
                continue
            v = int(n) if n == int(n) else n
        else:
            v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else "" if v is None else str(v)
            if t == "select" and f.get("options"):
                hit = next((o for o in f["options"] if o == v), None) or next((o for o in f["options"] if str(o).lower() == v.strip().lower()), None)
                if hit is None:
                    dropped.append({"field": k, "why": "%r is not one of %s" % (v[:40], ", ".join(f["options"]))})
                    continue
                v = hit
            v = v[:50000]
            if t == "code":
                v = v.rstrip() + "\n"
        cur = config.get(k, f.get("default", ""))
        if v == cur or (isinstance(v, str) and isinstance(cur, str) and v.strip() == cur.strip()):
            continue
        out[k] = v
    return out, dropped


# ---------- the model

def _usage(u):
    u = u or {}
    return {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0), "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cache_write": int(u.get("cache_creation_input_tokens") or 0)}


def _direct(system, prompt, model, extra_env, max_tokens=MAX_OUT, quick=False):
    """-> (text, model) through the API, or raises LookupError when no key is configured (the caller falls back to claude -p)."""
    from . import freechat
    try:
        base, head, env = freechat.credentials(extra_env)
    except ValueError as e:
        raise LookupError(str(e))
    head = dict(head, accept="application/json")
    mid = freechat.resolve_model(model or "sonnet", env)
    body = {"model": mid, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": prompt}]}
    if quick:
        body["thinking"] = {"type": "disabled"}                     # inline suggestions must be instant: no reasoning phase eating the token budget
    req = urllib.request.Request(base + "/v1/messages", data=json.dumps(body).encode("utf-8"), headers=head, method="POST")
    secrets = [v for k, v in head.items() if k in ("x-api-key", "authorization")]
    try:
        with urllib.request.urlopen(req, timeout=240, context=freechat.tls_context(env)) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(freechat._api_error(e, secrets))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError("cannot reach %s: %s" % (base, getattr(e, "reason", e)))
    try:
        from . import hubusage
        hubusage.note("works-magic", "free", d.get("model") or mid, _usage(d.get("usage")))
    except Exception:
        pass
    return "".join(b.get("text", "") for b in d.get("content") or [] if b.get("type") == "text"), d.get("model") or mid


def _claude(system, prompt, model):
    from .aihub import CLAUDE_MISSING, claude_cmd, settings_path
    cmd = [claude_cmd(), "-p", "--output-format", "json", "--max-turns", "1", "--no-session-persistence", "--tools", "", "--append-system-prompt", system]
    sp, custom = settings_path()
    if custom:
        cmd += ["--settings", sp]
    if model:
        cmd += ["--model", model]
    env = dict(os.environ)
    env.update(store.prefs_env())
    env["AIHUB_JUDGE"] = "1"                                         # our own hook returns at once for this throwaway session
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, cwd=store.ui_dir(),
                           env=env, start_new_session=True)
    except subprocess.TimeoutExpired:
        raise RuntimeError("no answer within 5 minutes")
    except OSError as e:
        raise RuntimeError("cannot start claude: %s. %s" % (e, CLAUDE_MISSING))
    try:
        m = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError((r.stderr.strip() or "claude ended without an answer (exit %s)" % r.returncode)[-600:])
    used = next(iter(m.get("modelUsage") or {}), model)
    try:
        from . import hubusage
        hubusage.note("works-magic", "free", used, _usage(m.get("usage")))
    except Exception:
        pass
    if m.get("is_error"):
        raise RuntimeError(str(m.get("result") or m.get("subtype") or "claude failed")[:600])
    return str(m.get("result") or ""), used


def ask_model(system, prompt, model="", extra_env=None, engine="auto", max_tokens=MAX_OUT, quick=False):
    """-> (text, model, engine)."""
    if engine in ("auto", "api"):
        try:
            t, m = _direct(system, prompt, model, extra_env, max_tokens, quick)
            return t, m, "api"
        except LookupError:
            if engine == "api":
                raise RuntimeError("no API key for a direct call (Settings > Encrypted secrets: ANTHROPIC_API_KEY)")
    t, m = _claude(system, prompt, model)
    return t, m, "claude"


def magic(spec, config, ask, field=None, context=None, model="", box=None, engine="auto"):
    """-> {config: changes, note, dropped, engine, model}. Raises ValueError (bad request) / RuntimeError (the model failed)."""
    if not str(ask or "").strip():
        raise ValueError("describe what the node should do")
    if field and not any(f["name"] == field and editable(f) for f in spec.get("fields") or []):
        raise ValueError("this field cannot be filled by Magic ask")
    safe = {f["name"]: config.get(f["name"]) for f in spec.get("fields") or [] if editable(f) and f["name"] in (config or {})}
    prompt = brief(spec, safe, ask, field, context)
    try:
        from . import works
        system = SYSTEM + "\n\n" + works.FLOW_BASICS                   # how data moves between nodes: the same rules the work generator gets
    except Exception:
        system = SYSTEM
    text, used, how = ask_model(system, prompt, model, box.get_all() if box else None, engine)
    try:
        raw, note = parse(text)
    except ValueError:
        raise RuntimeError("the model answered without settings: %s" % (text.strip()[:300] or "(empty)"))
    changes, dropped = clean(spec, raw, safe, field)
    if not changes and not note:
        note = "Nothing to change."
    return {"config": changes, "note": note, "dropped": dropped, "engine": how, "model": used}
