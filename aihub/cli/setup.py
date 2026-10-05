"""Setup profiles (type="setup"): declarative, reversible machine onboarding steps.

Step kinds (in aihub.toml as [[setup.steps]]):
  env         {name, value}                       -> managed block in shell rc files
  json_merge  {path, data}                        -> deep-merge into a JSON file (backup kept)
  file        {path, content | source, mode?}     -> write a file (backup kept)
  block       {path, id?, content}                -> managed text block in any text file
  command     {run, undo?}                        -> run shell command (always confirmed)
Variables: ${HOME} ${PKG} ${HUB}
"""
import json
import os
import shutil
import subprocess
import time

from . import integrations, paths

KINDS = ("env", "json_merge", "file", "block", "command")


def _x(s, ctx):
    if not isinstance(s, str):
        return s
    for k, v in ctx.items():
        s = s.replace("${%s}" % k, v)
    return s


def _xd(o, ctx):
    if isinstance(o, dict):
        return {k: _xd(v, ctx) for k, v in o.items()}
    if isinstance(o, list):
        return [_xd(v, ctx) for v in o]
    return _x(o, ctx)


def _path(p, ctx):
    return os.path.expanduser(_x(p, ctx))


def _backup(path, pkg):
    if not os.path.exists(path):
        return None
    d = paths.ensure("backups", pkg)
    b = os.path.join(d, "%d-%s" % (time.time() * 1000, os.path.basename(path)))
    shutil.copy2(path, b)
    return b


def _deep_merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _deep_merge(a[k], v)
        elif isinstance(v, list) and isinstance(a.get(k), list):
            a[k] = a[k] + [x for x in v if x not in a[k]]
        else:
            a[k] = v
    return a


def describe(step, ctx):
    k = step["kind"]
    if k == "env": return "set env %s" % step["name"]
    if k == "json_merge": return "merge settings into %s" % _path(step["path"], ctx)
    if k == "file": return "write file %s" % _path(step["path"], ctx)
    if k == "block": return "add managed block to %s" % _path(step["path"], ctx)
    return "run: %s" % _x(step["run"], ctx)


def apply(pkg, steps, ctx, confirm):
    """Apply steps; returns revert records. `confirm(text)->bool` is asked for every step."""
    reverts = []
    try:
        for i, s in enumerate(steps):
            if s["kind"] not in KINDS:
                raise ValueError("unknown setup step kind: %s" % s["kind"])
            if not confirm(describe(s, ctx)):
                print("  skipped")
                continue
            k = s["kind"]
            if k == "env":
                line = 'export %s=%s' % (s["name"], json.dumps(_x(str(s["value"]), ctx)))
                for rc in (".zshrc", ".bashrc"):
                    p = os.path.join(os.path.expanduser("~"), rc)
                    if os.path.exists(p) or rc == ".zshrc":
                        reverts.append(integrations.add_block(p, "env:%s:%s" % (pkg, s["name"]), line))
            elif k == "json_merge":
                p = _path(s["path"], ctx)
                b = _backup(p, pkg)
                cur = integrations._read_json(p)
                integrations._write_json(p, _deep_merge(cur, _xd(s["data"], ctx)))
                reverts.append({"op": "restore", "path": p, "backup": b or p + ".aihub-none"})
            elif k == "file":
                p = _path(s["path"], ctx)
                b = _backup(p, pkg)
                content = _x(s.get("content", ""), ctx)
                if s.get("source"):
                    content = open(os.path.join(ctx["PKG"], s["source"])).read()
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w").write(content)
                if s.get("mode"):
                    os.chmod(p, int(str(s["mode"]), 8))
                reverts.append({"op": "restore", "path": p, "backup": b or p + ".aihub-none"})
            elif k == "block":
                reverts.append(integrations.add_block(_path(s["path"], ctx), "setup:%s:%s" % (pkg, s.get("id", i)),
                                                      _x(s["content"], ctx)))
            elif k == "command":
                subprocess.check_call(_x(s["run"], ctx), shell=True)
                if s.get("undo"):
                    reverts.append({"op": "command", "run": _x(s["undo"], ctx)})
    except Exception:
        for r in reversed(reverts):  # roll back partial application
            revert(r)
        raise
    return reverts


def revert(rec):
    if rec.get("op") == "command":
        subprocess.call(rec["run"], shell=True)
    else:
        integrations.revert(rec)
