"""Scheduled prompts. <bot home>/cron.json: [{"name": "morning", "when": "08:30", "prompt": "..."}].
`when`: "HH:MM" (daily, local time) or "every 30m" / "every 2h". Run with `aihub run aihub-bot -- --cron` (keeps running)."""
import json
import os
import re
import time

from . import config


def load(extra=()):
    jobs = list(extra)
    try:
        with open(os.path.join(config.HOME, "cron.json"), encoding="utf-8") as f:
            jobs += json.load(f)
    except (OSError, ValueError):
        pass
    return [j for j in jobs if isinstance(j, dict) and j.get("when") and j.get("prompt")]


def due(job, now, last):
    """True when `job` should fire at `now` (epoch seconds) given when it last ran (0 = never)."""
    w = str(job["when"]).strip().lower()
    m = re.match(r"^every\s+(\d+)\s*([mh])$", w)
    if m:
        return now - last >= int(m.group(1)) * (60 if m.group(2) == "m" else 3600)
    m = re.match(r"^(\d{1,2}):(\d{2})$", w)
    if m:
        t = time.localtime(now)
        today = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
        return now >= today and last < today
    return False


def loop(jobs, run, tick=20):
    last = {j["name"]: time.time() if str(j["when"]).startswith("every") else 0 for j in jobs}
    print("cron: %d job(s): %s" % (len(jobs), ", ".join("%s @ %s" % (j["name"], j["when"]) for j in jobs)))
    while True:
        now = time.time()
        for j in jobs:
            if due(j, now, last[j["name"]]):
                last[j["name"]] = now
                try:
                    print("[%s] %s" % (j["name"], run(j["prompt"])))
                except Exception as e:
                    print("[%s] failed: %s" % (j["name"], e))
        time.sleep(tick)
