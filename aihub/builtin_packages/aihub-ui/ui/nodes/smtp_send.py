#!/usr/bin/env python3
"""Send email (SMTP): any mail server, any system, stdlib only (smtplib / imaplib). Not Outlook: that one is the aihub-nodes-outlook package.
Action: send, draft (IMAP APPEND into the Drafts folder of the same account, flagged \\Draft) or eml (a .eml file in the run's folder, opens in
any mail app). JSON on stdin, JSON on stdout. The password arrives in config only (stored encrypted by the UI) and is never printed."""
import email.utils
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import socket
import ssl
import sys
import time
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fsread as F  # noqa: E402

d = json.load(sys.stdin)
cfg = d["config"]
wd = d.get("workdir") or os.path.expanduser("~")
ACTIONS = ("send", "draft", "eml")
MAX_ATTACH = 25_000_000


def done(**k):
    print(json.dumps(k, ensure_ascii=False))
    sys.exit(0)


def scrub(e):
    """Error text without the password (some servers echo the AUTH line back)."""
    s = str(e)[:300]
    pw = str(cfg.get("password") or "")
    return s.replace(pw, "***") if pw else s


def addrs(v):
    """'a@x.com; Name <b@y.com>, c@z' -> ['a@x.com', 'Name <b@y.com>', 'c@z'] (names with spaces survive)."""
    text = re.sub(r"[;\n]+", ",", str(v or ""))
    return [email.utils.formataddr((n, a)) for n, a in email.utils.getaddresses([text]) if a and "@" in a]


def bare(lst):
    return [email.utils.parseaddr(a)[1] for a in lst]


def tls_context():
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    if cfg.get("insecure"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def num(v, dflt):
    try:
        return int(v)
    except (TypeError, ValueError):
        return dflt


to, cc, bcc = addrs(cfg.get("to")), addrs(cfg.get("cc")), addrs(cfg.get("bcc"))
action = cfg.get("action") if cfg.get("action") in ACTIONS else "send"
host = str(cfg.get("host") or "").strip()
security = cfg.get("security") if cfg.get("security") in ("ssl", "starttls", "none") else "starttls"
port = num(cfg.get("port"), 0) or {"ssl": 465, "starttls": 587, "none": 25}[security]
user = str(cfg.get("username") or "").strip()
password = str(cfg.get("password") or "")
sender = addrs(cfg.get("from")) or addrs(user)
if action == "send" and not (to or cc or bcc):
    done(ok=False, error="no recipient: fill 'To'")
if action == "send" and not host or action == "draft" and not (host or cfg.get("imap_host")):
    done(ok=False, error="fill 'SMTP server' (for example smtp.gmail.com), or choose the action 'eml' to only write a file")
if action != "eml" and not sender:
    done(ok=False, error="fill 'From' (or a username that is an e-mail address)")

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
if sum(os.path.getsize(f) for f in files) > MAX_ATTACH:
    done(ok=False, error="attachments are over 25 MB together; most mail servers refuse that")

subject = str(cfg.get("subject") or "").strip() or "(no subject)"
body = str(cfg.get("body") or d.get("input") or "")
msg = EmailMessage()
msg["Subject"] = subject
if sender:
    msg["From"] = sender[0]
if to:
    msg["To"] = ", ".join(to)
if cc:
    msg["Cc"] = ", ".join(cc)
if bcc and action != "send":                                             # a draft keeps Bcc so the mail app sends it; a sent mail never shows it
    msg["Bcc"] = ", ".join(bcc)
if cfg.get("reply_to"):
    msg["Reply-To"] = ", ".join(addrs(cfg.get("reply_to")))
msg["Date"] = email.utils.formatdate(localtime=True)
msg["Message-ID"] = email.utils.make_msgid(domain=(bare(sender)[0].split("@")[-1] if sender else None))
if action == "eml":
    msg["X-Unsent"] = "1"                                                # Outlook / Apple Mail open it as a new, editable mail
if cfg.get("format") == "html":
    msg.set_content(re.sub(r"<[^>]+>", "", body) or " ")
    msg.add_alternative(body, subtype="html")
else:
    msg.set_content(body)
for f in files:
    typ = (mimetypes.guess_type(f)[0] or "application/octet-stream").split("/", 1)
    with open(f, "rb") as fh:
        msg.add_attachment(fh.read(), maintype=typ[0], subtype=typ[1], filename=os.path.basename(f))

timeout = 60
try:
    if action == "eml":
        out_dir = os.path.join(wd, "drafts")
        os.makedirs(out_dir, exist_ok=True)
        name = (re.sub(r"[^\w.-]+", "-", subject).strip("-")[:60] or "mail") + "-" + time.strftime("%Y%m%d-%H%M%S") + ".eml"
        path = os.path.join(out_dir, name)
        with open(path, "wb") as fh:
            fh.write(bytes(msg))
        done(ok=True, output="Saved as draft file: %s\nOUTPUT: %s" % (subject, path),
             data={"to": ", ".join(to), "subject": subject, "attachments": files, "action": action, "path": path},
             log="wrote %s (%d attachment(s))" % (path, len(files)))
    if action == "draft":
        ihost = str(cfg.get("imap_host") or "").strip() or re.sub(r"^(smtp|mail|smtps)\.", "imap.", host)
        iport = num(cfg.get("imap_port"), 0) or 993
        folder = str(cfg.get("drafts_folder") or "").strip() or "Drafts"
        if iport == 143:
            im = imaplib.IMAP4(ihost, iport, timeout=timeout)
            im.starttls(ssl_context=tls_context())
        else:
            im = imaplib.IMAP4_SSL(ihost, iport, ssl_context=tls_context(), timeout=timeout)
        try:
            im.login(user or bare(sender)[0], password)
            typ, info = im.append('"%s"' % folder.replace('"', ""), r"(\Draft \Seen)", imaplib.Time2Internaldate(time.time()), bytes(msg))
            if typ != "OK":
                done(ok=False, error="the IMAP server refused to save into '%s': %s (Gmail: [Gmail]/Drafts)" % (folder, scrub(info)))
        finally:
            try:
                im.logout()
            except Exception:
                pass
        done(ok=True, output="Saved as draft: %s to %s" % (subject, ", ".join(to) or "-"),
             data={"to": ", ".join(to), "subject": subject, "attachments": files, "action": action, "folder": folder},
             log="IMAP %s:%d APPEND to %s (%d attachment(s))" % (ihost, iport, folder, len(files)))
    if security == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=timeout, context=tls_context())
    else:
        s = smtplib.SMTP(host, port, timeout=timeout)
        if security == "starttls":
            s.starttls(context=tls_context())
    try:
        if user or password:
            s.login(user or bare(sender)[0], password)
        refused = s.send_message(msg, from_addr=bare(sender)[0], to_addrs=bare(to + cc + bcc))
    finally:
        try:
            s.quit()
        except Exception:
            pass
except SystemExit:
    raise
except (smtplib.SMTPAuthenticationError, imaplib.IMAP4.error) as e:
    done(ok=False, error="login refused: %s (an account with 2-step sign-in needs an app password)" % scrub(e))
except (socket.timeout, OSError, smtplib.SMTPException, ssl.SSLError) as e:
    done(ok=False, error="mail server failed: %s" % scrub(e))
done(ok=True, output="Sent: %s to %s" % (subject, ", ".join(to or cc or bcc)),
     data={"to": ", ".join(to), "subject": subject, "attachments": files, "action": action, "refused": sorted(refused or {})},
     log="SMTP %s:%d sent '%s' to %d recipient(s), %d attachment(s)%s" % (host, port, subject, len(to + cc + bcc), len(files),
                                                                       ("; refused: " + ", ".join(refused)) if refused else ""))
