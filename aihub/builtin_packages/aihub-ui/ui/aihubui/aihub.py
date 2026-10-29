"""Everything that touches aihub or Claude Code state. The CLI is the only writer of packages, logins and runs: this module calls
`aihub --json ...` (arguments as a list, never a shell; secrets on stdin) and reads Claude's settings without ever returning a secret."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import sys

HOME = lambda: os.environ.get("AIHUB_HOME") or os.path.join(os.path.expanduser("~"), ".aihub")
CLAUDE_HOME = lambda: os.path.join(os.path.expanduser("~"), ".claude")
SECRET_KEY = re.compile(r"(token|secret|passw|credential|api[_-]?key|auth|private|cookie|bearer)", re.I)
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class CliError(Exception):
    def __init__(self, msg, doc=None):
        super().__init__(msg)
        self.doc = doc


def command():
    """argv prefix that runs the aihub CLI: env AIHUB_CLI, `aihub` on PATH, ~/.aihub/bin/aihub, else this interpreter with -m aihub.cli."""
    e = os.environ.get("AIHUB_CLI")
    if e:
        return e.split()
    w = shutil.which("aihub")
    if w:
        return [w]
    for n in ("aihub", "aihub.cmd"):
        p = os.path.join(HOME(), "bin", n)
        if os.path.isfile(p):
            return [p]
    return [sys.executable, "-m", "aihub.cli"]


def run(args, stdin=None, timeout=600):
    """-> the JSON document of `aihub --json ARGS`. Raises CliError (message = the CLI's own error) when ok is false."""
    try:
        p = subprocess.run(command() + ["--json"] + list(args), input=stdin, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, AIHUB_JSON="1", AIHUB_NO_UPDATE="1"))
    except subprocess.TimeoutExpired:
        raise CliError("aihub %s timed out" % " ".join(args[:2]))
    except OSError as e:
        raise CliError("cannot start the aihub CLI: %s" % e)
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        raise CliError((p.stderr or p.stdout or "aihub gave no answer").strip()[-400:])
    if not doc.get("ok"):
        raise CliError((doc.get("error") or "failed") + ((" (" + doc["hint"] + ")") if doc.get("hint") else ""), doc)
    return doc


def safe_name(n):
    if not isinstance(n, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", n):
        raise CliError("bad name")
    return n


# ---- .env of a service (the CLI's own file: NAME=value lines, mode 0600)

def env_path(rec):
    return os.path.join(rec.get("bot_home") or "", ".env")


def read_env(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def write_env(path, values):
    """Set KEY=value lines in place (comments and other lines kept); new keys are appended. Values with a newline are refused."""
    for k, v in values.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k) or "\n" in v or "\r" in v or "\0" in v:
            raise CliError("bad setting %r" % k)
    lines, seen = [], set()
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in values:
                lines[i] = "%s=%s" % (k, values[k])
                seen.add(k)
    lines += ["%s=%s" % (k, v) for k, v in values.items() if k not in seen]
    from .secretbox import write_private
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_private(path, ("\n".join(lines) + "\n").encode("utf-8"))


def env_spec(rec):
    """Settings a bot/service package declares ([[bot.env]] / [[service.env]]): [{name,hint,required,secret,default}]. Never raises."""
    try:
        with open(os.path.join(rec["path"], "aihub.toml"), "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except (OSError, KeyError):
        return []
    try:
        import tomllib
        d = tomllib.loads(raw)
        t = d.get("service") or d.get("bot") or {}
        return [{"name": str(e.get("name", "")), "hint": str(e.get("hint", "")), "required": bool(e.get("required", True)),
                 "secret": bool(e.get("secret", False)), "default": str(e.get("default", ""))} for e in t.get("env", []) if e.get("name")]
    except Exception:
        pass
    out, cur = [], None
    for ln in raw.splitlines():                       # python < 3.11: read just the env tables
        s = ln.strip()
        if re.match(r"^\[\[(bot|service)\.env\]\]$", s):
            cur = {"name": "", "hint": "", "required": True, "secret": False, "default": ""}
            out.append(cur)
        elif s.startswith("["):
            cur = None
        elif cur is not None and "=" in s and not s.startswith("#"):
            k, v = [x.strip() for x in s.split("=", 1)]
            v = re.sub(r"\s+#.*$", "", v) if not v.startswith('"') else v
            if v in ("true", "false"):
                cur[k] = v == "true"
            elif v.startswith('"') and v.endswith('"') and len(v) > 1:
                cur[k] = v[1:-1]
    return [e for e in out if e["name"]]


def services():
    """Installed bots and services with their settings (secret values are reported as set / not set, never returned)."""
    doc = run(["list"])
    runs = {}
    try:
        for r in run(["run", "list"]).get("data") or []:
            runs.setdefault(r.get("bot"), []).append(r)
    except CliError:
        pass
    out = []
    for p in doc.get("data") or []:
        comps = p.get("components") or []
        kind = next((k for k in ("service", "bot") if k + ":" + p["name"] in comps), None)
        if not kind:
            continue
        env = read_env(env_path(p))
        fields = []
        for e in env_spec(p):
            v = env.get(e["name"], "")
            fields.append(dict(e, set=bool(v), value="" if e["secret"] else v))
        out.append({"name": p["name"], "kind": kind, "version": p.get("version"), "enabled": p.get("enabled", True),
                    "env": fields, "runs": runs.get(p["name"], []), "home": p.get("bot_home")})
    return out


# ---- Claude Code settings (read for models, hooks, MCP; secrets masked)

def _json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def user_path(raw):
    """A path as a person types or pastes it: quotes (Windows "Copy as path"), ~ and %VAR% / $VAR expanded, made absolute."""
    c = str(raw or "").strip().strip("\"'").strip()
    return os.path.abspath(os.path.expanduser(os.path.expandvars(c))) if c else ""


def settings_path():
    """The Claude settings.json the UI follows: the custom file chosen in Settings (pref `claude_settings`), else ~/.claude/settings.json."""
    try:
        with open(os.path.join(HOME(), "ui", "prefs.json"), encoding="utf-8-sig") as f:
            c = str((json.load(f) or {}).get("claude_settings") or "").strip()
    except (OSError, ValueError, AttributeError):
        c = ""
    return (user_path(c), True) if c else (os.path.join(CLAUDE_HOME(), "settings.json"), False)


def settings_files():
    """Files read in order (later wins): the chosen file, plus settings.local.json next to the default one."""
    path, custom = settings_path()
    return [path] if custom else [path, os.path.join(CLAUDE_HOME(), "settings.local.json")]


def mask_env(env):
    return {k: ("set" if SECRET_KEY.search(k) and v else v) for k, v in (env or {}).items()}


def merged_settings():
    """The settings files in order (later wins), like Claude Code: `env` merged key by key, picker options and availableModels joined."""
    out = {}
    for fn in settings_files():
        d = _json(fn)
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            if k == "env" and isinstance(v, dict):
                out["env"] = dict(out.get("env") or {}, **v)
            elif k == "modelPicker" and isinstance(v, dict):
                prev = out.get("modelPicker") or {}
                out["modelPicker"] = {**prev, **v, "options": list(prev.get("options") or []) + list(v.get("options") or [])}
            elif k == "availableModels" and isinstance(v, list):
                out[k] = list(out.get(k) or []) + v
            else:
                out[k] = v
    return out


def claude_settings():
    s = merged_settings()
    return {"path": settings_path()[0], "custom": settings_path()[1], "exists": os.path.isfile(settings_path()[0]), "helper": bool(isinstance(s.get("apiKeyHelper"), str) and s.get("apiKeyHelper")), "model": s.get("model"), "effort": s.get("effortLevel"), "env": mask_env(s.get("env")),
            "available": s.get("availableModels") or [], "plugins": sorted((s.get("enabledPlugins") or {}).keys()),
            "mode": (s.get("permissions") or {}).get("defaultMode")}


def _pref(name, default=None):
    try:
        with open(os.path.join(HOME(), "ui", "prefs.json"), encoding="utf-8-sig") as f:
            return (json.load(f) or {}).get(name, default)
    except (OSError, ValueError, AttributeError):
        return default


def model_kind(o, mid):
    """chat | image | tts: an explicit `type`/`kind`/`capability` of the picker option, else guessed from the model id."""
    for k in ("type", "kind", "capability", "modality"):
        v = str(o.get(k) or "").lower() if isinstance(o, dict) else ""
        if v:
            return "image" if "image" in v else "tts" if ("tts" in v or "speech" in v or "audio" in v) else "chat"
    n = mid.lower()
    if re.search(r"image|dall-?e|imagen|flux|diffusion|midjourney", n):
        return "image"
    if re.search(r"tts|speech|voice", n):
        return "tts"
    return "chat"


def models():
    """The three tiers (ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL) plus, unless the Settings switch `model_picker` is turned off
    (default on), the `modelPicker.options` of the settings file (chat, image and tts models)."""
    s = merged_settings()
    env = s.get("env") if isinstance(s.get("env"), dict) else {}
    out = [{"id": "", "label": "Default" + ((" (%s)" % s["model"]) if s.get("model") else "")}]
    seen = {""}

    def add(i, label):
        if i and i not in seen:
            seen.add(i)
            out.append({"id": i, "label": label})
    for alias in ("opus", "sonnet", "haiku"):
        real = env.get("ANTHROPIC_DEFAULT_%s_MODEL" % alias.upper())
        add(alias, alias.capitalize() + ((" (%s)" % real) if real else ""))
    picker, kinds = {}, {}
    if _pref("model_picker", True):
        mp = s.get("modelPicker")
        opts = mp.get("options") if isinstance(mp, dict) else mp
        for o in (opts if isinstance(opts, list) else [])[:200]:
            if isinstance(o, str):
                o = {"model": o}
            mid = next((o[k] for k in ("model", "id", "value") if isinstance(o, dict) and isinstance(o.get(k), str)), None)
            if mid and re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]@-]{0,150}$", mid):
                picker[mid] = str(o.get("label") or o.get("name") or mid)[:80]
                kinds[mid] = model_kind(o, mid)
                add(mid, picker[mid])
    info = {"opus": "Most capable. Deep reasoning and hard problems.", "sonnet": "Balanced. Everyday work and coding.", "haiku": "Fastest and lightest. Quick tasks."}
    for m in out:
        base = m["id"].lower()
        m["window"] = 1_000_000 if "1m" in base else 200_000
        m["group"] = "alias" if (not base or base in info) else "picker"
        m["kind"] = kinds.get(m["id"], "chat")
        m["note"] = info.get(base) or ("Your Claude settings default." if not base else {"image": "Image generation model (model picker).", "tts": "Text-to-speech model (model picker)."}.get(m["kind"], "Model picker option in your settings."))
        m["real"] = env.get("ANTHROPIC_DEFAULT_%s_MODEL" % base.upper(), "") if base in info else (m["id"] if base else s.get("model") or "")
        m["label"] = base.capitalize() if base in info else (m["label"] if base else "Default")
    return {"models": out, "default": s.get("model") or "", "effort": s.get("effortLevel") or ""}


def slash_commands():
    """Slash commands Claude Code knows: ~/.claude/commands/**/*.md and skills (~/.claude/skills/*/SKILL.md, plugin skills). Names + descriptions only."""
    out, seen = [], set()

    def front(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = f.read(2000)
        except OSError:
            return ""
        m = re.search(r"^description:\s*(.+)$", head, re.M)
        return m.group(1).strip().strip("\"'")[:160] if m else ""

    def add(name, desc, src):
        if re.match(r"^[A-Za-z0-9][\w:.-]{0,60}$", name) and name not in seen:
            seen.add(name)
            out.append({"name": name, "description": desc, "source": src})
    root = CLAUDE_HOME()
    cmd = os.path.join(root, "commands")
    for d, _, fs in os.walk(cmd):
        for f in fs:
            if f.endswith(".md"):
                rel = os.path.relpath(os.path.join(d, f), cmd)[:-3].replace(os.sep, ":")
                add(rel, front(os.path.join(d, f)), "command")
    sk = os.path.join(root, "skills")
    try:
        for n in sorted(os.listdir(sk)):
            p = os.path.join(sk, n, "SKILL.md")
            if os.path.isfile(p):
                add(n, front(p), "skill")
    except OSError:
        pass
    pl = os.path.join(root, "plugins")
    n = 0
    for d, dirs, fs in os.walk(pl):
        if d[len(pl):].count(os.sep) > 7:
            dirs[:] = []
        if "SKILL.md" in fs and n < 300:
            n += 1
            add(os.path.basename(d), front(os.path.join(d, "SKILL.md")), "plugin")
    return out


def claude_mcp():
    """MCP servers Claude Code knows (~/.claude.json): name -> {command,args,type,url}; env values masked."""
    d = _json(os.path.join(os.path.expanduser("~"), ".claude.json"))
    out = []
    for n, v in sorted((d.get("mcpServers") or {}).items()):
        if isinstance(v, dict):
            out.append({"name": n, "command": v.get("command", ""), "args": [str(a) for a in v.get("args", [])], "type": v.get("type", "stdio"),
                        "url": v.get("url", ""), "env": mask_env(v.get("env")), "source": "claude"})
    return out
