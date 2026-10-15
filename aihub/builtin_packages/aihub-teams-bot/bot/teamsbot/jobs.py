"""Background tasks. `/task <request>` answers at once ("task 3 started"), runs the request on a harness in a worker thread and posts ONLY the
result (or the error) back to the chat when it is done. SDK free: the Teams layer sets `hub.notify(conv, text)`.

Harness: TEAMS_TASK_HARNESS (default `claude`, i.e. `claude -p`, so the task can use its own tools; falls back to BOT_HARNESS when that command
is missing). Jobs are files in <bot home>/jobs/<id>.json (state: queued|running|done|failed|interrupted); at start, leftovers are marked interrupted."""
import json
import os
import shutil
import threading
import time

from harness import config, executors

KEEP = 50                      # finished job files kept per bot


class Jobs:
    def __init__(self, hub):
        self.hub = hub
        self.dir = os.path.join(config.HOME, "jobs")
        self.slots = threading.BoundedSemaphore(max(1, int(os.environ.get("TEAMS_TASK_MAX") or 2)))
        self.threads, self.lock = [], threading.Lock()
        self._recover()

    # ---- files
    def _file(self, jid):
        return os.path.join(self.dir, "%d.json" % jid)

    def _save(self, job):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self._file(job["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)
        os.replace(tmp, self._file(job["id"]))

    def all(self):
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for n in names:
            if n.endswith(".json"):
                try:
                    with open(os.path.join(self.dir, n), encoding="utf-8") as f:
                        out.append(json.load(f))
                except (OSError, ValueError):
                    pass
        return sorted(out, key=lambda j: j["id"])

    def _recover(self):
        for j in self.all():
            if j["state"] in ("queued", "running"):
                j.update(state="interrupted", error="the bot restarted before this finished")
                self._save(j)

    def _trim(self):
        done = [j for j in self.all() if j["state"] in ("done", "failed", "interrupted")]
        for j in done[:-KEEP]:
            try:
                os.remove(self._file(j["id"]))
            except OSError:
                pass

    # ---- running
    def harness(self):
        name = (os.environ.get("TEAMS_TASK_HARNESS") or "claude").strip().lower()
        if name in executors.KINDS and getattr(executors.KINDS[name], "binary", "") and not shutil.which(executors.KINDS[name].binary):
            name = None                                          # that CLI is not installed: use the bot's own harness
        return executors.make(self.hub.agent, name)

    def start(self, conv, sender, text):
        text = (text or "").strip()
        if not text:
            return "Usage: `/task <what you want done>`. I will confirm now and post only the result when it is finished. `/tasks` lists them."
        if not self.slots.acquire(blocking=False):
            return "I am already running the maximum number of tasks (%s). Try again when one finishes (`/tasks`)." % (os.environ.get("TEAMS_TASK_MAX") or 2)
        with self.lock:
            ids = [j["id"] for j in self.all()]
            job = {"id": (max(ids) if ids else 0) + 1, "conv": conv, "who": sender.get("name") or "", "text": text[:4000], "state": "queued",
                   "ts": time.time()}
            self._save(job)
        t = threading.Thread(target=self._work, args=(job,), daemon=True)
        self.threads.append(t)
        t.start()
        return "Task #%d started. I will post the result here when it is done." % job["id"]

    def _work(self, job):
        try:
            job["state"] = "running"
            self._save(job)
            ex = self.harness()
            with self.hub.lock:                                  # build the prompt against this chat's memory, then run without blocking chat
                self.hub.agent.memory = self.hub.store(job["conv"])
                system = self.hub.agent.system(ex.hint, job["text"]) + "\n\n" + self.hub.task_prompt() + (
                    "\n\nThis is a background task. Do the work, then reply with ONLY the final result (no progress chatter).")
            out = executors.MARK.sub("", ex.run(job["text"], system, False)).strip() or "(the task produced no output)"
            job.update(state="done", result=out[:20000], end=time.time())
            msg = "**Task #%d done**\n\n%s" % (job["id"], out)
        except Exception as e:
            job.update(state="failed", error=str(e)[:500], end=time.time())
            msg = "**Task #%d failed:** %s" % (job["id"], str(e)[:500])
        finally:
            self.slots.release()
        self._save(job)
        self._trim()
        try:
            if self.hub.notify:
                self.hub.notify(job["conv"], msg)
        except Exception as e:
            job["error"] = "could not post the result: %s" % e
            self._save(job)

    def wait(self, timeout=None):
        for t in list(self.threads):
            t.join(timeout)

    def listing(self, conv):
        mine = [j for j in self.all() if j["conv"] == conv][-8:]
        if not mine:
            return "No tasks in this chat yet. Start one with `/task <request>`."
        return "\n".join("- #%d **%s** %s" % (j["id"], j["state"], j["text"][:70].replace("\n", " ")) for j in mine)
