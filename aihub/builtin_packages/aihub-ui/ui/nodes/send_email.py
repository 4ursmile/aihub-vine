#!/usr/bin/env python3
"""Send email through the Outlook desktop app (classic / "legacy" Outlook) with pywin32 COM automation. Windows only.
Needs `pip install pywin32` and Outlook signed in. Action: send, save a draft, or just show the window. JSON on stdin, JSON on stdout."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fsread as F  # noqa: E402

d = json.load(sys.stdin)
cfg = d["config"]
wd = d.get("workdir") or os.path.expanduser("~")


def done(**k):
    print(json.dumps(k, ensure_ascii=False))
    sys.exit(0)


def addrs(v):
    return ";".join(a for a in re.split(r"[;,\s]+", str(v or "")) if a)


to, cc, bcc = addrs(cfg.get("to")), addrs(cfg.get("cc")), addrs(cfg.get("bcc"))
action = cfg.get("action") if cfg.get("action") in ("send", "draft", "display") else "send"
if action == "send" and not (to or cc or bcc):
    done(ok=False, error="no recipient: fill 'To'")
files = []
for line in str(cfg.get("attachments") or "").splitlines():
    p = F.pointer(line, wd) if line.strip() else ""
    if not p:
        if line.strip():
            done(ok=False, error="attachment not found: " + line.strip()[:200])
        continue
    why = F.blocked(p)
    if why:
        done(ok=False, error="refused: " + why)
    if os.path.isdir(p):
        done(ok=False, error="attach files, not folders: " + p)
    files.append(p)
subject = str(cfg.get("subject") or "").strip() or "(no subject)"
body = str(cfg.get("body") or d.get("input") or "")
if sys.platform != "win32":
    done(ok=False, error="Outlook automation (win32com) works on Windows only; this machine is " + sys.platform)
try:
    import pythoncom
    import win32com.client
except ImportError:
    done(ok=False, error="pywin32 is missing: run  python -m pip install pywin32  with the python that runs the UI service")
pythoncom.CoInitialize()
try:
    app = win32com.client.Dispatch("Outlook.Application")
    mail = app.CreateItem(0)                                           # olMailItem
    mail.To, mail.CC, mail.BCC, mail.Subject = to, cc, bcc, subject
    if cfg.get("format") == "html":
        mail.HTMLBody = body
    else:
        mail.Body = body
    for f in files:
        mail.Attachments.Add(f)
    if action == "send":
        mail.Send()
    elif action == "draft":
        mail.Save()
    else:
        mail.Display()
except Exception as e:
    done(ok=False, error="Outlook failed: %s (is classic Outlook installed and signed in? the new Outlook has no COM support)" % str(e)[:300])
finally:
    pythoncom.CoUninitialize()
done(ok=True, output="%s: %s to %s" % ({"send": "Sent", "draft": "Saved as draft", "display": "Opened"}[action], subject, to or cc or bcc or "-"),
     data={"to": to, "subject": subject, "attachments": files, "action": action}, log="%s '%s' (%d attachment(s))" % (action, subject, len(files)))
