#!/usr/bin/env python3
"""Python function node. JSON on stdin ({config, input, inputs, workdir}), JSON on stdout.

The user's code defines ONE entry function (`main`, else `run`, else the last function defined) with a typed argument and a typed return:

    class In(BaseModel): n: int
    class Out(BaseModel): double: int
    def main(args: In) -> Out: ...

Both the argument and the return must be a pydantic model, a dict / Dict[...] / TypedDict. The node fills the argument from the data of the
nodes before it, the input text when it is a JSON object, and the optional "Arguments" field, and turns the return into JSON output
(`data` keys become {{data.key}} for the nodes after it). pydantic is only needed when the code uses it.

Multi-step (ETL): `# %% name` lines split the code into steps that run in order in one namespace (see _steps.py). The steps see
`data` (the parsed input, as above), `inputs`, `text`, `workdir`, `vault` (aihub_vault: the credential sets granted to this node) and
`result` (a dict: what it holds at the end is the output). A `main(args)` defined by the steps is still called as above, after them.

Free form (a side step in the middle of a flow): the code may also be a plain script (no function), or a function without type hints.
When it gives nothing back (no `result`, or the function returns None) the node PASSES ITS INPUT ON unchanged (text and data), so it
can log, write a file, call an API or check something between two nodes without having to match their input and output."""
import contextlib
import inspect
import io
import json
import os
import re
import sys
import traceback
import typing

import _steps

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def pip_hint(mod):
    """How to install the package that provides MOD into THIS Python (the node's environment)."""
    try:
        import aihub_data
        pip = aihub_data.PIP.get(mod, mod)
        return "Install it: %s  (or press Install in the node's Environment box)" % aihub_data.install_command([pip])
    except ImportError:
        return "Install it for this Python: %s -m pip install %s" % (sys.executable, mod)


def done(**k):
    sys.stdout.write("\n" + json.dumps(k, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def fail(msg):
    done(ok=False, error=msg)


def json_obj(text):
    """A JSON object inside text (plain or in a ```json fence) -> dict, else None."""
    t = (text or "").strip()
    for cand in (t, *FENCE.findall(t)):
        try:
            v = json.loads(cand.strip())
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    return None


def is_model(tp):
    return inspect.isclass(tp) and any(c.__name__ == "BaseModel" and c.__module__.startswith("pydantic") for c in tp.__mro__)


def is_typed_dict(tp):
    return inspect.isclass(tp) and issubclass(tp, dict) and hasattr(tp, "__annotations__") and hasattr(tp, "__total__")


def is_dict(tp):
    return tp is dict or typing.get_origin(tp) is dict or is_typed_dict(tp)


def kind_of(tp):
    return "model" if is_model(tp) else "dict" if is_dict(tp) else ""


def entry(ns):
    for name in ("main", "run"):
        if inspect.isfunction(ns.get(name)):
            return ns[name]
    fns = [v for v in ns.values() if inspect.isfunction(v) and v.__module__ == "__aihub_node__" and not v.__name__.startswith("_")]
    return fns[-1] if fns else None


def fields_of(tp):
    if is_model(tp):
        return list(getattr(tp, "model_fields", None) or getattr(tp, "__fields__", {}))
    return list(getattr(tp, "__annotations__", {}))


def dump(v):
    if hasattr(v, "model_dump"):
        return v.model_dump(mode="json")
    if hasattr(v, "dict") and is_model(type(v)):
        return json.loads(v.json())
    return v


_D = {}


def main():
    d = json.load(sys.stdin)
    _D.update(d)
    cfg, wd = d.get("config") or {}, d.get("workdir") or ""
    code = str(cfg.get("code") or "")
    if not code.strip():
        fail("write a Python function first")
    if wd and os.path.isdir(wd):
        os.chdir(wd)
    sys.path.insert(0, os.getcwd())
    ns = {"__name__": "__aihub_node__", "__builtins__": __builtins__}
    sys.modules["__aihub_node__"] = type(sys)("__aihub_node__")
    sys.modules["__aihub_node__"].__dict__.update(ns)
    ns = sys.modules["__aihub_node__"].__dict__
    log = io.StringIO()
    args, text, obj = gather(d, cfg)
    if _steps.has_steps(code):
        return run_steps(code, ns, log, d, args, text, wd)
    ns.update(data=dict(args), inputs=d.get("inputs") or [], text=text, workdir=wd, result={}, log=_steps.say)     # a plain script sees the same names as steps
    try:
        import aihub_vault
        ns["vault"] = aihub_vault
    except ImportError:
        ns["vault"] = None
    try:
        with contextlib.redirect_stdout(log):
            exec(compile(code, "<python node>", "exec"), ns)
    except SystemExit:
        fail("the code called sys.exit")
    except ImportError as e:
        name = getattr(e, "name", "") or ""
        fail("%s. %s" % (e, pip_hint(name.split(".")[0]) if name else ""))
    except SyntaxError as e:
        fail("syntax error on line %s: %s" % (e.lineno, e.msg))
    except Exception:
        fail("the code failed while loading:\n" + traceback.format_exc(limit=3)[-600:])
    fn = entry(ns)
    if fn is None:                                                   # a plain script: it ran while loading; `result` (if it set one) is the output
        res = ns.get("result")
        if res in (None, {}, [], ""):
            passthrough(d, args, text, log, "script ran")
        finish(res, log, "script ran")
    call(fn, ns, log, args, text, obj)


def passthrough(d, args, text, log, what):
    """Nothing given back: hand the input on unchanged (text and data), so the node can sit between two others as a side step."""
    data = {}
    for ev in d.get("inputs") or []:
        if isinstance(ev, dict) and isinstance(ev.get("data"), dict):
            data.update(ev["data"])
    try:
        data = json.loads(json.dumps(data, default=str))
    except (TypeError, ValueError):
        data = {}
    done(ok=True, output=text, data=data, log=(log.getvalue()[-1500:].rstrip() + "\n" if log.getvalue().strip() else "") + what + "; nothing returned: the input is passed on")


def finish(res, log, what):
    res = dump(res)
    if not isinstance(res, dict):
        res = {"result": res}
    try:
        res = json.loads(json.dumps(res, default=str))
    except (TypeError, ValueError) as e:
        fail("the result is not JSON serialisable: %s" % e)
    done(ok=True, output=json.dumps(res, indent=2, ensure_ascii=False), data=res, log=log.getvalue()[-1500:] or what)


def gather(d, cfg):
    """The argument, auto parsed: data of the nodes before -> input text as a JSON object -> the Arguments field (later wins)."""
    args = {}
    for ev in d.get("inputs") or []:
        if isinstance(ev, dict) and isinstance(ev.get("data"), dict):
            args.update(ev["data"])
    text = d.get("input") or ""
    obj = json_obj(text)
    if obj:
        args.update(obj)
    raw = str(cfg.get("args") or "").strip()
    if raw:
        a = json_obj(raw)
        if a is None:
            fail("Arguments must be a JSON object, e.g. {\"n\": 3}")
        args.update(a)
    return args, text, obj


def run_steps(code, ns, log, d, args, text, wd):
    """Multi-step code: every `# %%` step in order, then main() if the steps defined one, else the `result` dict is the output."""
    try:
        import aihub_vault as vault
    except ImportError:
        vault = None
    ns.update(data=dict(args), inputs=d.get("inputs") or [], text=text, workdir=wd, result={}, vault=vault, log=_steps.say)
    try:
        with contextlib.redirect_stdout(log):
            steps = _steps.run(_steps.split(code), ns, "<python node>")
    except _steps.StepFailed as e:
        msg = str(e)
        m = __import__("re").search(r"No module named '([\w.]+)'", msg)
        fail(msg + ("\n" + pip_hint(m.group(1).split(".")[0]) if m else ""))
    fn = next((ns[n] for n in ("main", "run") if inspect.isfunction(ns.get(n))), None)
    if fn is not None:
        return call(fn, ns, log, args, text, json_obj(text))
    summary = "%d step(s): %s" % (len(steps), ", ".join("%s %.1fs" % (s["name"], s["seconds"]) for s in steps))
    res = dump(ns.get("result"))
    if res in (None, {}, [], ""):
        passthrough(d, args, text, log, summary)
    if not isinstance(res, dict):
        res = {"result": res}
    try:
        res = json.loads(json.dumps(res, default=str))
    except (TypeError, ValueError) as e:
        fail("the result is not JSON serialisable: %s" % e)
    done(ok=True, output=json.dumps(res, indent=2, ensure_ascii=False), data=dict(res, steps=steps),
         log=(log.getvalue()[-1200:] + "\n" if log.getvalue() else "") + "%d step(s): %s" % (len(steps), ", ".join("%s %.1fs" % (s["name"], s["seconds"]) for s in steps)))


def call(fn, ns, log, args, text, obj):
    try:
        hints = typing.get_type_hints(fn, ns)
    except Exception as e:
        fail("cannot read the type hints of %s: %s" % (fn.__name__, e))
    params = [p for p in inspect.signature(fn).parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    if len(params) > 1:
        fail("%s() takes %d arguments: use ONE argument typed as a pydantic model or a dict" % (fn.__name__, len(params)))
    arg_tp = hints.get(params[0].name) if params else None
    if params and arg_tp is not None and not kind_of(arg_tp):
        fail("type the argument of %s() as a pydantic BaseModel or a dict (or leave it untyped: it gets a dict), e.g. def %s(%s: In) -> Out" % (fn.__name__, fn.__name__, params[0].name))
    ret_tp = hints.get("return")                                     # untyped / None / anything: dict = output, None = pass the input on, else {"result": value}

    if params and arg_tp is None:                                   # untyped: the parsed input as a dict (plain text under `input` / `text`)
        arg = dict(args)
        if text.strip() and not obj:
            arg.setdefault("input", text)
            arg.setdefault("text", text)
    elif params:
        names = fields_of(arg_tp)
        for k in ("input", "text"):                                   # a plain-text input fills a field called input / text
            if k in names and k not in args and text.strip() and not obj:
                args[k] = text
                break
        if is_model(arg_tp) and (getattr(arg_tp, "model_config", None) or {}).get("extra") != "allow":
            args = {k: v for k, v in args.items() if k in set(names)}      # extra keys from upstream data are dropped
        try:
            if is_model(arg_tp):
                arg = arg_tp.model_validate(args) if hasattr(arg_tp, "model_validate") else arg_tp.parse_obj(args)
            else:
                arg = arg_tp(args) if is_typed_dict(arg_tp) else args
        except Exception as e:
            fail("the input does not match %s: %s" % (getattr(arg_tp, "__name__", "the argument"), str(e)[:600]))
    try:
        with contextlib.redirect_stdout(log):
            res = fn(arg) if params else fn()
            if inspect.iscoroutine(res):
                import asyncio
                res = asyncio.run(res)
    except SystemExit:
        fail("the code called sys.exit")
    except Exception:
        fail("%s() raised:\n%s" % (fn.__name__, traceback.format_exc(limit=4)[-800:]))
    res = dump(res)
    if is_model(ret_tp) and not isinstance(res, dict):
        fail("%s() must return the %s model, got %s" % (fn.__name__, ret_tp.__name__, type(res).__name__))
    if res is None:
        passthrough(_D, args, text, log, "ran %s()" % fn.__name__)
    if not isinstance(res, dict):
        res = {"result": res}
    try:
        res = json.loads(json.dumps(res, default=str))
    except (TypeError, ValueError) as e:
        fail("the result is not JSON serialisable: %s" % e)
    done(ok=True, output=json.dumps(res, indent=2, ensure_ascii=False), data=res, log=log.getvalue()[-1500:] or "ran %s()" % fn.__name__)


main()
