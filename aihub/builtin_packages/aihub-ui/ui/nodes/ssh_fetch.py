#!/usr/bin/env python3
"""Files from SSH: copies a file or folder from a server over SSH (the system's scp, so ~/.ssh/config hosts, keys and the agent all work) to
~/.aihub/workingspace/data/ssh/HOST/PATH and hands the PATH on, never the content. A password or a private key can come from the node or an
'ssh' credential set; it is handed to ssh through SSH_ASKPASS / a 0600 temp file, never on the command line. JSON on stdin, JSON on stdout."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _remote as R  # noqa: E402

HOST = re.compile(r"^[A-Za-z0-9_.:\[\]-]{1,255}$")
USER = re.compile(r"^[A-Za-z0-9_.@\\-]{1,64}$")
BAD_PATH = re.compile(r"[\0\r\n$`;|&<>'\"(){}\\]")     # a legacy scp hands the path to the remote shell: no way to run a command through it


def pem(key):
    """A key pasted into a one-line password box loses its line breaks: put them back."""
    k = key.strip().replace("\\n", "\n")
    m = re.match(r"^(-----BEGIN [A-Z0-9 ]+-----)\s*(.+?)\s*(-----END [A-Z0-9 ]+-----)$", k, re.S)
    if m and "\n" not in m.group(2).strip():
        body = re.sub(r"\s+", "", m.group(2))
        k = "\n".join([m.group(1)] + [body[i:i + 64] for i in range(0, len(body), 64)] + [m.group(3)])
    return k + "\n"


def explain(err, host):
    e = err.strip()
    if re.search(r"Permission denied|Authentication failed|Too many authentication failures", e, re.I):
        return "the SSH login to %s failed: give a password or a private key (node or 'ssh' credential set), or load your key into ssh-agent. ssh said: %s" % (host, e[-200:])
    if "Host key verification failed" in e or "REMOTE HOST IDENTIFICATION HAS CHANGED" in e:
        return "the host key of %s changed since the last connection (or cannot be checked): check it, then fix ~/.ssh/known_hosts. ssh said: %s" % (host, e[-200:])
    if re.search(r"No such file|not a regular file|does not exist", e, re.I):
        return "not found on %s: %s" % (host, e[-200:])
    if re.search(r"Could not resolve|Connection refused|timed out|No route|Network is unreachable", e, re.I):
        return "cannot reach %s: %s" % (host, e[-200:])
    return "scp failed: " + e[-300:]


def main():
    d = json.loads(sys.stdin.read() or "{}")
    cfg = d.get("config") or {}
    host, user = str(cfg.get("host") or "").strip(), str(cfg.get("user") or "").strip()
    rpath = str(cfg.get("path") or "").strip()
    if "@" in host and not user:
        user, host = host.rsplit("@", 1)
    if not host or not HOST.match(host) or host.startswith("-"):
        R.done(ok=False, error="Set the SSH host (a name, an address, or a Host from ~/.ssh/config).")
    if user and (not USER.match(user) or user.startswith("-")):
        R.done(ok=False, error="bad SSH user name")
    if not rpath or rpath.startswith("-") or BAD_PATH.search(rpath):
        R.done(ok=False, error="Set the remote file or folder (no quotes, $, ;, |, &, <, >, `, (), {} or backslashes).")
    port = str(cfg.get("port") or "").strip()
    if port and not (port.isdigit() and 0 < int(port) < 65536):
        R.done(ok=False, error="bad SSH port")
    scp = shutil.which("scp")
    if not scp:
        R.done(ok=False, error="scp (OpenSSH client) is not installed on this machine.")
    limit = R.number(cfg, "timeout", 600, 10, 3600) - 10
    secret = str(cfg.get("password") or "")
    key = str(cfg.get("private_key") or "").strip()
    key_file = os.path.expanduser(str(cfg.get("key_file") or "").strip())
    if key_file and not os.path.isfile(key_file):
        R.done(ok=False, error="no key file at %s" % key_file)

    side = tempfile.mkdtemp(prefix="aihub-ssh-")                         # askpass / key, removed at the end (0700 folder)
    env = dict(os.environ)
    args = [scp, "-r", "-p", "-q", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15"]
    if port:
        args += ["-P", port]
    try:
        if key:
            key_file = os.path.join(side, "id")
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(pem(key))
        if key_file:
            args += ["-i", key_file, "-o", "IdentitiesOnly=yes"]
        if secret:                                                       # the password (or the key's passphrase) through SSH_ASKPASS
            if os.name == "nt":
                ap = os.path.join(side, "askpass.cmd")
                with open(ap, "w", encoding="utf-8") as f:
                    f.write('@"%s" -c "import os,sys;sys.stdout.write(os.environ[\'AIHUB_SSH_SECRET\']+chr(10))"\r\n' % sys.executable)
            else:
                ap = os.path.join(side, "askpass.sh")
                with open(ap, "w", encoding="utf-8") as f:
                    f.write('#!/bin/sh\nprintf \'%s\\n\' "$AIHUB_SSH_SECRET"\n')
                os.chmod(ap, 0o700)
            env.update(SSH_ASKPASS=ap, SSH_ASKPASS_REQUIRE="force", DISPLAY=env.get("DISPLAY") or ":0", AIHUB_SSH_SECRET=secret)
            args += ["-o", "NumberOfPasswordPrompts=1"]
        else:
            args += ["-o", "BatchMode=yes"]                              # never wait for a prompt nobody can answer
        dest = R.dest_for("ssh", (user + "@" if user else "") + host, rpath)
        tmp = R.stage(dest)
        remote = "%s%s:%s" % (user + "@" if user else "", "[%s]" % host if ":" in host and not host.startswith("[") else host, rpath)
        t0 = time.time()
        try:
            p = subprocess.run(args + ["--", remote, tmp + os.sep], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=limit)
        except subprocess.TimeoutExpired:
            R.drop(tmp)
            R.done(ok=False, error="the copy from %s took longer than the node's time limit" % host)
        if p.returncode != 0:
            R.drop(tmp)
            R.done(ok=False, error=explain(p.stderr or p.stdout or "exit %d" % p.returncode, host))
        got = [n for n in os.listdir(tmp)]
        if not got:
            R.drop(tmp)
            R.done(ok=False, error="nothing was copied from %s:%s" % (host, rpath))
        if len(got) == 1:                                                # one file or folder: it IS the copy (no extra level)
            one = tmp + ".one"
            os.replace(os.path.join(tmp, got[0]), one)
            R.drop(tmp)
            R.swap(one, dest)
            paths = [dest]
        else:                                                            # a * pattern: the folder holds every match
            R.swap(tmp, dest)
            paths = [os.path.join(dest, n) for n in sorted(got)]
    except OSError as e:
        R.done(ok=False, error=str(e))
    finally:
        shutil.rmtree(side, ignore_errors=True)
    files, size = 0, 0
    for x in paths:
        n, s = R.count(x)
        files, size = files + n, size + s
    data = {"path": paths[0], "paths": paths, "host": host, "remote": rpath, "files": files, "bytes": size, "seconds": round(time.time() - t0, 1)}
    R.done(ok=True, output="\n".join(paths), data=data,
           log="copied %d file(s), %s from %s:%s in %.1fs to %s (pointer only)" % (files, R.human(size), host, rpath, time.time() - t0, paths[0]))


if __name__ == "__main__":
    main()
