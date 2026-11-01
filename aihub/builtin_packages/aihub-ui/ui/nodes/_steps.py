"""Multi-step code for the Python and Spark nodes: `# %%` lines split the code into steps (cells, as in Jupyter / VS Code).

    # %% extract
    df = spark.read.csv(raw.uri("in/*.csv"), header=True)
    # %% transform
    out = df.groupBy("country").count()
    # %% load
    out.write.mode("overwrite").parquet(lake.uri("daily/"))
    result["rows"] = out.count()

The steps run one after the other in ONE namespace (a name made in a step is there in the next), each is timed and reported on stdout
(`step 2/3 transform: ok (0.4s)`, shown in the node log while it runs); the first one that fails stops the run with its step name and line."""
import re
import sys
import time
import traceback

CELL = re.compile(r"^[ \t]*#[ \t]*%%(.*)$")


def split(code):
    """-> [(name, source, first line)]. Code before the first marker is a step of its own ("setup") when it holds anything but comments."""
    cells, name, buf, start = [], "", [], 1
    for i, ln in enumerate(code.split("\n"), 1):
        m = CELL.match(ln)
        if m:
            if buf and (cells or name or any(x.strip() and not x.strip().startswith("#") for x in buf)):
                cells.append((name or "setup", "\n".join(buf), start))
            name, buf, start = m.group(1).strip().strip("-: ") or "step %d" % (len(cells) + 1), [], i + 1
        else:
            buf.append(ln)
    if buf or name:
        cells.append((name or ("main" if not cells else "step %d" % (len(cells) + 1)), "\n".join(buf), start))
    return cells


def has_steps(code):
    return any(CELL.match(ln) for ln in code.split("\n"))


class StepFailed(Exception):
    def __init__(self, k, total, name, detail):
        super().__init__("step %d/%d %s failed:\n%s" % (k, total, name, detail))
        self.k, self.name = k, name


def say(msg):
    out = sys.__stdout__
    out.write(msg + "\n")
    out.flush()


def run(cells, ns, filename="<code>"):
    """Run every step in NS. -> [{name, seconds}]. Raises StepFailed (with the traceback cut to the user's code)."""
    done, total = [], len(cells)
    for k, (name, src, first) in enumerate(cells, 1):
        say("step %d/%d %s: running" % (k, total, name))
        t0 = time.time()
        try:
            exec(compile("\n" * (first - 1) + src, filename, "exec"), ns)          # padding keeps the line numbers of the whole code
        except SystemExit:
            raise StepFailed(k, total, name, "the code called sys.exit")
        except SyntaxError as e:
            raise StepFailed(k, total, name, "syntax error on line %s: %s" % (e.lineno, e.msg))
        except Exception:
            tb = traceback.format_exc()
            keep = [b for b in tb.split("\n  File ") if filename in b or b.startswith("Traceback")]
            detail = ("\n  File ".join(keep) if len(keep) > 1 else tb).strip()
            tail = tb.strip().split("\n")[-1]
            raise StepFailed(k, total, name, (detail if tail in detail else detail + "\n" + tail)[-1500:])
        dt = time.time() - t0
        done.append({"name": name, "seconds": round(dt, 3)})
        say("step %d/%d %s: ok (%.1fs)" % (k, total, name, dt))
    return done
