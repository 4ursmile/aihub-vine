#!/usr/bin/env python3
"""Trigger: start a work for each NEW mail in an Outlook folder that matches the conditions (regex on sender, subject, transport headers, body).
Windows + classic Outlook (pywin32 COM), polled. Protocol: one JSON line on stdin ({"config": ...}), then one {"event": {"text", "data"}} line per mail on stdout."""
import datetime
import json
import os
import re
import sys
import time

cfg = json.loads(sys.stdin.readline())["config"]
HDR = "http://schemas.microsoft.com/mapi/proptag/0x007D001E"          # PR_TRANSPORT_MESSAGE_HEADERS


def say(**k):
    print(json.dumps(k, ensure_ascii=False), flush=True)


def rx(name):
    v = str(cfg.get(name) or "").strip()
    if not v:
        return None
    try:
        return re.compile(v, re.I | re.M)
    except re.error as e:
        sys.stderr.write("bad regex in %s: %s\n" % (name, e))
        sys.exit(2)


R = {k: rx(k) for k in ("from_regex", "subject_regex", "header_regex", "body_regex")}
try:
    gap = min(max(float(cfg.get("poll") or 20), 5), 3600)
except (TypeError, ValueError):
    gap = 20
if sys.platform != "win32":
    sys.stderr.write("Outlook automation works on Windows only\n")
    sys.exit(2)
import pythoncom  # noqa: E402
import win32com.client  # noqa: E402

FOLDERS = {"inbox": 6, "sent": 5, "drafts": 16, "junk": 23}


def smtp(m):
    a = str(getattr(m, "SenderEmailAddress", "") or "")
    if str(getattr(m, "SenderEmailType", "")).upper() == "EX":
        try:
            a = m.Sender.GetExchangeUser().PrimarySmtpAddress or a
        except Exception:
            pass
    return a


def header(m):
    try:
        return str(m.PropertyAccessor.GetProperty(HDR) or "")
    except Exception:
        return ""


def naive(t):
    return datetime.datetime(t.year, t.month, t.day, t.hour, t.minute, t.second)


def keep(m, folder_dir):
    out, base = [], re.sub(r"[^A-Za-z0-9._-]+", "_", str(m.EntryID))[-24:]
    d = os.path.join(folder_dir, base)
    for a in m.Attachments:
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, re.sub(r'[\\/:*?"<>|]+', "_", str(a.FileName))[:120])
            a.SaveAsFile(p)
            out.append(p)
        except Exception:
            continue
    return out


pythoncom.CoInitialize()
app = win32com.client.Dispatch("Outlook.Application")
box = app.GetNamespace("MAPI").GetDefaultFolder(FOLDERS.get(str(cfg.get("folder") or "inbox").lower(), 6))
started, seen = naive(datetime.datetime.now()), set()
save_dir = os.path.join(os.path.expanduser("~"), ".aihub", "workingspace", "outlook")
while True:
    try:
        items = box.Items
        items.Sort("[ReceivedTime]", True)
        for k, m in enumerate(items):
            if k >= 50:
                break
            if getattr(m, "Class", 0) != 43:                              # olMail only (not meeting requests, reports)
                continue
            if naive(m.ReceivedTime) < started:
                break
            if m.EntryID in seen or (cfg.get("unread_only", True) and not m.UnRead):
                continue
            seen.add(m.EntryID)
            who, subj, body, head = smtp(m), str(m.Subject or ""), str(m.Body or ""), header(m)
            sender = "%s <%s>" % (m.SenderName, who)
            if R["from_regex"] and not R["from_regex"].search(sender):
                continue
            if R["subject_regex"] and not R["subject_regex"].search(subj):
                continue
            if R["header_regex"] and not R["header_regex"].search(head):
                continue
            if R["body_regex"] and not R["body_regex"].search(body):
                continue
            files = keep(m, save_dir) if cfg.get("save_attachments") and m.Attachments.Count else []
            if cfg.get("mark_read"):
                try:
                    m.UnRead = False
                    m.Save()
                except Exception:
                    pass
            limit = 20000
            say(event={"text": "Subject: %s\nFrom: %s\n\n%s" % (subj, sender, body[:limit]),
                       "data": {"from": who, "from_name": str(m.SenderName or ""), "subject": subj, "to": str(m.To or ""), "cc": str(m.CC or ""),
                                "body": body[:limit], "received": naive(m.ReceivedTime).isoformat(), "attachments": files, "message_id": str(m.EntryID)}})
        seen = set(list(seen)[-500:])
    except Exception as e:                                                # Outlook closed or busy: keep trying
        sys.stderr.write("poll failed: %s\n" % str(e)[:200])
    time.sleep(gap)
