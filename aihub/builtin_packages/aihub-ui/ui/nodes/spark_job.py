#!/usr/bin/env python3
"""Spark node (launcher). JSON on stdin ({config, input, inputs, workdir}), JSON on stdout.

Runs PySpark code made of `# %%` steps (see _steps.py) in a Python that has pyspark (field Python, else $PYSPARK_PYTHON, else this one),
either as a local / client SparkSession (mode `session`: master local[*], spark://..., yarn, k8s://...) or through `spark-submit`
(mode `spark-submit`: whatever your cluster setup needs). The code runs in spark_runner.py, which builds the session, configures every
bucket of the node's S3 credential set for s3a:// (per bucket: endpoint, keys, path style) and gives the steps `spark`, `vault`,
`buckets`, `s3a()`, `data`, `inputs`, `text`, `workdir` and `result`. Secrets never go on a command line: the runner reads them with
aihub_vault from the node's grant file. Progress lines (`step 2/3 transform: ok`) and Spark's own output stream into the node log."""
import json
import os
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def done(**k):
    sys.stdout.write("\n" + json.dumps(k, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def fail(msg):
    done(ok=False, error=msg)


def conf_lines(text):
    out = {}
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        k, sep, v = ln.partition("=")
        if not sep:
            fail("Spark settings: one key=value per line (got %r)" % ln[:60])
        out[k.strip()] = v.strip()
    return out


def spark_submit(cfg):
    p = str(cfg.get("spark_submit") or "").strip()
    if p:
        return p
    home = os.environ.get("SPARK_HOME")
    for c in ([os.path.join(home, "bin", "spark-submit")] if home else []) + [shutil.which("spark-submit") or ""]:
        if c and os.path.isfile(c):
            return c
    fail("spark-submit not found: set SPARK_HOME, put spark-submit on PATH, or fill in the spark-submit field")


def main():
    d = json.load(sys.stdin)
    cfg = d.get("config") or {}
    code = str(cfg.get("code") or "")
    if not code.strip():
        fail("write the Spark code first")
    try:
        args = json.loads(cfg.get("args") or "{}") if str(cfg.get("args") or "").strip() else {}
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        fail("Arguments must be a JSON object, e.g. {\"day\": \"2026-10-31\"}")
    data = {}
    for ev in d.get("inputs") or []:
        if isinstance(ev, dict) and isinstance(ev.get("data"), dict):
            data.update(ev["data"])
    data.update(args)
    ndir = os.environ.get("AIHUB_NODE_DIR") or d.get("workdir") or os.getcwd()
    os.makedirs(ndir, exist_ok=True)
    mode = str(cfg.get("mode") or "session")
    job = {"code": code, "data": data, "inputs": d.get("inputs") or [], "text": d.get("input") or "", "workdir": d.get("workdir") or ndir,
           "master": str(cfg.get("master") or "local[*]"), "app_name": str(cfg.get("app_name") or "aihub-works"), "mode": mode,
           "conf": conf_lines(cfg.get("conf")), "packages": str(cfg.get("packages") or "auto").strip(), "max_rows": int(cfg.get("max_rows") or 1000),
           "result_file": os.path.join(ndir, "spark-result.json")}
    jf = os.path.join(ndir, "spark-job.json")
    with open(jf, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, default=str)
    try:
        os.remove(job["result_file"])
    except OSError:
        pass
    py = str(cfg.get("python") or "").strip() or os.environ.get("PYSPARK_PYTHON") or sys.executable
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env["PYTHONPATH"] = os.pathsep.join(x for x in (HERE, env.get("PYTHONPATH", "")) if x)        # _steps + aihub_vault (lib) for the runner
    if mode == "spark-submit":
        cmd = [spark_submit(cfg), "--master", job["master"], "--name", job["app_name"]]
        if job["packages"] and job["packages"] != "auto":
            cmd += ["--packages", job["packages"]]
        for k, v in job["conf"].items():                               # the node's own settings only; bucket logins are set inside the job
            cmd += ["--conf", "%s=%s" % (k, v)]
        if cfg.get("python"):
            env["PYSPARK_PYTHON"] = env["PYSPARK_DRIVER_PYTHON"] = py
        cmd += [os.path.join(HERE, "spark_runner.py"), jf]
    else:
        cmd = [py, os.path.join(HERE, "spark_runner.py"), jf]
    sys.stdout.write("$ %s\n" % " ".join(cmd))
    sys.stdout.flush()
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr, env=env, cwd=job["workdir"] if os.path.isdir(job["workdir"]) else ndir)
    except OSError as e:
        fail("cannot start %s: %s" % (cmd[0], e))

    def stop(*_):                                                      # same process group as this launcher: Stop / a timeout ends the JVM too
        try:
            p.terminate()
        except OSError:
            pass
        sys.exit(1)
    signal.signal(signal.SIGTERM, stop)
    rc = p.wait()
    try:
        with open(job["result_file"], encoding="utf-8") as f:
            res = json.load(f)
    except (OSError, ValueError):
        res = None
    if not res:
        fail("the Spark job ended (exit %s) without a result; see the log above" % rc)
    if not res.get("ok"):
        fail(res.get("error") or "the Spark job failed (exit %s)" % rc)
    out = res.get("result") or {}
    steps = res.get("steps") or []
    done(ok=True, output=json.dumps(out, indent=2, ensure_ascii=False, default=str), data=dict(out, steps=steps) if isinstance(out, dict) else {"result": out, "steps": steps},
         log="%d step(s) in %.1fs: %s" % (len(steps), time.time() - t0, ", ".join("%s %.1fs" % (s["name"], s["seconds"]) for s in steps)))


if __name__ == "__main__":
    main()
