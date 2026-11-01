#!/usr/bin/env python3
"""Runs the Spark node's steps inside a Python with pyspark (or under spark-submit). argv[1] = the job file written by spark_job.py;
the outcome goes to job["result_file"] as {ok, result, steps, error}. Never prints a secret."""
import glob
import json
import os
import sys
import traceback

import _steps


def write(job, **k):
    with open(job["result_file"], "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False, default=str)


def hadoop_aws(pyspark):
    """The hadoop-aws package that matches the Hadoop bundled with this pyspark (its jars folder names the version)."""
    jars = glob.glob(os.path.join(os.path.dirname(pyspark.__file__), "jars", "hadoop-client-api-*.jar"))
    if not jars:
        return ""
    ver = os.path.basename(jars[0])[len("hadoop-client-api-"):-len(".jar")]
    return "org.apache.hadoop:hadoop-aws:%s" % ver


def plain(v, cap):
    """A step result that JSON can hold: DataFrames become their first CAP rows."""
    if hasattr(v, "limit") and hasattr(v, "collect") and hasattr(v, "columns"):
        return [r.asDict(recursive=True) for r in v.limit(cap).collect()]
    if isinstance(v, dict):
        return {str(k): plain(x, cap) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x, cap) for x in v]
    return v


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        job = json.load(f)
    try:
        import pyspark
        from pyspark.sql import SparkSession
    except ImportError:
        write(job, ok=False, error="pyspark is not installed for %s. Press Install in the node's Environment box, or run: %s -m pip install pyspark" % (sys.executable, sys.executable))
        return
    try:
        import aihub_vault as vault
        buckets = vault.buckets()
    except Exception as e:                                           # no grant / nothing granted: the code can still run without buckets
        vault, buckets = None, {}
        _steps.say("aihub_vault: %s" % e)
    b = SparkSession.builder
    if job["mode"] != "spark-submit":
        b = b.master(job["master"]).appName(job["app_name"])
        pk = job["packages"]
        if pk == "auto":
            pk = hadoop_aws(pyspark) if buckets else ""
        if pk:
            b = b.config("spark.jars.packages", pk)
        for k, v in job["conf"].items():
            b = b.config(k, v)
    _steps.say("starting Spark (%s)%s" % (job["master"] if job["mode"] != "spark-submit" else "spark-submit", ", buckets: " + ", ".join(sorted(buckets)) if buckets else ""))
    try:
        spark = b.getOrCreate()
    except Exception as e:
        txt = str(e)
        if "java" in txt.lower() or "JAVA_HOME" in txt or "gateway" in txt.lower():
            txt += ("\nSpark needs Java (17 works with every pyspark). The node's Environment box shows the command for this system, "
                    "e.g. brew install openjdk@17 (macOS) or sudo apt install openjdk-17-jre-headless (Linux); then set JAVA_HOME.")
        write(job, ok=False, error="could not start Spark: " + txt[-1500:])
        return
    try:
        hc = spark.sparkContext._jsc.hadoopConfiguration()               # per-bucket s3a settings, set here so no key is ever on a command line
        for bk in buckets.values():
            for k, v in bk.s3a_conf(prefix="").items():
                hc.set(k, str(v))
    except Exception as e:
        _steps.say("could not set the bucket settings: %s" % e)

    def s3a(name, path=""):
        """s3a://<bucket>/<path> of a granted bucket, by its entry name or bucket name."""
        bk = buckets.get(name) or next((x for x in buckets.values() if x.bucket == name), None)
        if not bk:
            raise KeyError("no bucket %r granted to this node (have: %s)" % (name, ", ".join(sorted(buckets)) or "none"))
        return bk.uri(path)
    ns = {"__name__": "__aihub_spark__", "spark": spark, "sc": spark.sparkContext, "vault": vault, "buckets": buckets, "s3a": s3a, "data": job["data"],
          "inputs": job["inputs"], "text": job["text"], "workdir": job["workdir"], "result": {}, "log": _steps.say}
    try:
        steps = _steps.run(_steps.split(job["code"]), ns, "<spark node>")
        res = plain(ns.get("result"), job.get("max_rows") or 1000)
        json.dumps(res, default=str)
        write(job, ok=True, result=res if isinstance(res, dict) else {"result": res}, steps=steps)
    except _steps.StepFailed as e:
        write(job, ok=False, error=str(e))
    except Exception:
        write(job, ok=False, error="the Spark job failed:\n" + traceback.format_exc(limit=3)[-1200:])
    finally:
        try:
            spark.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
