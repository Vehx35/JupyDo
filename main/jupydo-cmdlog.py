#!/usr/bin/env python3
"""
jupydo-cmdlog.py - one command log per JupyDo user, from the host's audit log.

auditd records every program started by JupyDo users (UID 10000-19999, rule key
jupydo_cmd, installed by first_start.sh). Run every minute by the systemd timer
jupydo-cmdlog.timer, this script reads only the new events and appends them to
    <JupyDo folder>/logs/commands/<user>.log
one line per program:  2026-10-04 13:04:11  [/sharedFolder/project]  ls -la data
The files are readable by root only (they are personal data).
Note: besides what users type in a terminal, this includes the programs started
by their notebooks and by JupyterLab itself.
"""
import argparse
import os
import re
import subprocess
import sys
import time

RECORD = re.compile(r"^type=(\w+) msg=audit\((\d+\.\d+):(\d+)\): (.*)$")
FIELD = re.compile(r'(\w+(?:\[\d+\])?)=("(?:[^"\\]|\\.)*"|\S+)')


def decode(value):
    """Audit values are "quoted text" or hex (when they contain spaces etc.)."""
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    try:
        return bytes.fromhex(value).decode("utf-8", "replace").replace("\x00", " ")
    except ValueError:
        return value


def parse(raw_text):
    """Raw ausearch output -> list of (time, uid, cwd, command), in order."""
    events = {}
    order = []
    for line in raw_text.splitlines():
        m = RECORD.match(line.strip())
        if not m:
            continue
        rtype, ts, serial, rest = m.groups()
        key = (ts, serial)
        if key not in events:
            events[key] = {"ts": float(ts)}
            order.append(key)
        ev = events[key]
        fields = dict(FIELD.findall(rest))
        if rtype == "SYSCALL":
            ev["uid"] = fields.get("uid")
        elif rtype == "CWD":
            ev["cwd"] = decode(fields.get("cwd", ""))
        elif rtype == "EXECVE":
            argc = int(fields.get("argc", "0"))
            args = []
            for i in range(argc):
                if "a%d" % i in fields:
                    args.append(decode(fields["a%d" % i]))
                else:  # long argument split in chunks a1[0], a1[1], ...
                    chunks = sorted((k for k in fields if k.startswith("a%d[" % i)),
                                    key=lambda k: int(k[k.index("[") + 1:-1]))
                    args.append("".join(decode(fields[k]) for k in chunks))
            ev["cmd"] = " ".join(a if a and not re.search(r"\s", a) else repr(a) for a in args)
        elif rtype == "PROCTITLE" and "cmd" not in ev:
            ev["cmd"] = decode(fields.get("proctitle", ""))
    out = []
    for key in order:
        ev = events[key]
        if ev.get("uid") and ev.get("cmd"):
            out.append((ev["ts"], ev["uid"], ev.get("cwd", "?"), ev["cmd"]))
    return out


def load_uid_map(path):
    users = {}
    try:
        with open(path) as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split("\t")
                if len(parts) >= 2:
                    users[parts[0]] = parts[1]
    except FileNotFoundError:
        pass
    return users


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--out", required=True, help="folder for the per-user logs")
    ap.add_argument("--uidmap", required=True, help="users_uid_map.tsv in the hub state folder")
    ap.add_argument("--checkpoint", default="/var/lib/jupydo/cmdlog.checkpoint")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.checkpoint), exist_ok=True)
    os.makedirs(args.out, exist_ok=True)
    os.chmod(args.out, 0o700)
    cmd = ["ausearch", "--input-logs", "-k", "jupydo_cmd", "--raw", "--checkpoint", args.checkpoint]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode in (10, 11, 12):  # checkpoint no longer valid (logs rotated): restart
        os.remove(args.checkpoint)
        result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 1):  # 1 = no new events
        sys.exit("ausearch failed ({}): {}".format(result.returncode, result.stderr.strip()))

    users = load_uid_map(args.uidmap)
    files = {}
    try:
        for ts, uid, cwd, command in parse(result.stdout):
            user = users.get(uid, "uid-" + uid)
            if user not in files:
                path = os.path.join(args.out, re.sub(r"[^A-Za-z0-9_.@-]", "_", user) + ".log")
                files[user] = open(path, "a")
                os.chmod(path, 0o600)
            files[user].write("{}  [{}]  {}\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), cwd, command))
    finally:
        for f in files.values():
            f.close()


if __name__ == "__main__":
    main()
