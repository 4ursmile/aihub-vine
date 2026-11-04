"""`aihub work ...`: Works (the workflows of aihub-ui) from a terminal, for servers without the UI.

The engine lives in the installed aihub-ui package, so this command only hands its arguments to it (`ui/server.py --works ...`, the
same interpreter, .env and AIHUB_* environment `aihub run aihub-ui` uses; see aihubui/workscli.py there for every subcommand).
`aihub work serve --bg` keeps the scheduler running under the `aihub run` supervisor as the run `aihub-works` (restart always, log in
~/.aihub/runs/aihub-works.log, shown on the hub's Running page when signed in). Only one process fires the cron / package triggers
(the UI or the serve run), so starting both never runs a schedule twice.
`aihub work run W` (signed in, not --json) runs under a foreground `aihub run` supervisor as the run `work-W`, so every run from a terminal
is recorded on the hub's Running page too (status, live step log, exit code; Stop there ends it). --no-hub or `run_report off`: not recorded."""
import argparse
import json
import subprocess

from . import api, installer, ui

UI = "aihub-ui"
SERVE_RUN = "aihub-works"


def _ensure_ui():
    if UI in installer.bots():
        return
    if not ui.INTERACTIVE or ui.JSON:
        raise api.ApiError("the Works engine comes with the %s package: aihub install %s" % (UI, UI))
    if not ui.confirm("The Works engine comes with %s (it also runs without its page). Install it now?" % UI, True):
        raise api.ApiError("needs %s: aihub install %s" % (UI, UI))
    installer.install_many([(UI, "")], True)
    if UI not in installer.bots():
        raise api.ApiError("%s did not install; try: aihub install %s" % (UI, UI))


def _serve_bg(rest):
    """`aihub work serve --bg [--hooks PORT]`: the scheduler as a supervised background run."""
    from . import runs
    args = ["--works", "serve"] + [x for x in rest[1:] if x not in ("--bg", "--background")]
    a = argparse.Namespace(name=SERVE_RUN, fg=False, schedule=None, restart="always", no_hub=False, venv=None, python=None)
    return runs.start(UI, args, a)


def _run_recorded(rest):
    """`aihub work run W ...` in this terminal under a foreground supervisor that reports to the hub (Running page). -> exit code.
    One record per work (`work-W`), reused by its next run; a second run of the same work while one is live gets `work-W-2` and so on."""
    from . import runs
    ref = next((x for x in rest[1:] if not x.startswith("-")), "") or "work"
    base = "work " + ref
    for i in range(1, 10):
        name = base if i == 1 else "%s %d" % (base, i)
        old = runs.load(runs.rid_of(name))
        if not (old and runs.alive(old)):
            break
    a = argparse.Namespace(name=name, fg=True, schedule="", restart="no", no_hub=False, venv=None, python=None)
    return runs.start(UI, ["--works"] + rest, a)


def cli(a):
    rest = list(a.rest or [])
    if rest[:1] == ["--"]:
        rest = rest[1:]
    _ensure_ui()
    if rest[:1] == ["serve"] and any(x in rest for x in ("--bg", "--background")):
        return _serve_bg(rest)
    want_json = ui.JSON or "--json" in rest
    rest = [x for x in rest if x != "--json"]
    if rest[:1] == ["run"] and not want_json:
        no_hub = "--no-hub" in rest
        rest = [x for x in rest if x != "--no-hub"]
        from . import runs
        if not no_hub and runs.hub_on():
            code = _run_recorded(rest)
            if code:
                raise SystemExit(code)
            return 0
    cmd, shell, home, env = installer.bot_command(UI, ["--works"] + (["--json"] if want_json else []) + rest)
    if not want_json:
        code = subprocess.call(cmd, shell=shell, cwd=home, env=env)
        if code:
            raise SystemExit(code)
        return 0
    p = subprocess.run(cmd, shell=shell, cwd=home, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                       encoding="utf-8", errors="replace")
    try:
        doc = json.loads(p.stdout or "{}")
    except ValueError:
        doc = {"output": p.stdout}
    if isinstance(doc, dict) and doc.get("error"):
        raise api.ApiError(doc["error"])
    if p.returncode:
        raise api.ApiError((p.stderr or "").strip().splitlines()[-1] if (p.stderr or "").strip() else "aihub work failed (exit %s)" % p.returncode)
    ui.result(doc)
    return 0
