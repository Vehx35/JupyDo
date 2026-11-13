#!/usr/bin/env python3
"""JupyDo network connection log: the "netlog" service of compose.yaml (the hub image, with
the host's network and the NET_ADMIN capability, started only if the administrator enabled
it in first_start.sh). Only root can read its files. Nothing is installed on the host.

Every new connection from a container (users' environments, queue jobs, any other) to
outside the Docker networks is written with the time, the user, the container, its image,
the internal address and port, the destination, and the public port it leaves from (the
NAT translation): enough to tell who made a connection seen from outside, e.g. "this
server's address connected to X at 14:32:10, from port 61022". The container is looked up
when the connection starts, so the line stays valid even if the container is later deleted.
Container starts, stops and deletions are written too, with their address.

One file per day, <out>/YYYY-MM-DD.log (gzip-compressed the next day), deleted after
--retention-days. Uses conntrack (vendored in the hub image) and the Docker socket.
Usage: jupydo-netlog.py --out /path/to/JupyDo/logs/network --retention-days 365
"""
import argparse
import datetime
import gzip
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import threading
import time

LOCK = threading.Lock()
CONTAINERS = {}   # IP -> {"name", "image", "user"}
NETWORKS = []     # Docker subnets (connections inside them are not written)
_last_refresh = 0.0


_docker = None


def docker_client():
    global _docker
    if _docker is None:
        import docker   # the hub image has it (dockerspawner)
        _docker = docker.from_env()
    return _docker


def user_of(name, labels):
    """The JupyDo user of a container: label jupydo.user (queue jobs), else its name:
    jupyter-<user>--<environment> or jupyter-<user> (users' environments)."""
    if labels.get("jupydo.user"):
        return labels["jupydo.user"]
    m = re.match(r"^jupyter-(.+?)(--.*)?$", name)
    return m.group(1) if m else "-"


def refresh(force=False):
    """Addresses of the running containers and the Docker subnets, from docker inspect
    (at most every 2 seconds, unless forced)."""
    global _last_refresh, NETWORKS
    if not force and time.time() - _last_refresh < 2:
        return
    _last_refresh = time.time()
    found, nets = {}, []
    try:
        client = docker_client()
        for c in client.containers.list():
            name = c.attrs.get("Name", "").lstrip("/")
            labels = (c.attrs.get("Config") or {}).get("Labels") or {}
            info = {"name": name, "image": (c.attrs.get("Config") or {}).get("Image", ""),
                    "user": user_of(name, labels)}
            for net in ((c.attrs.get("NetworkSettings") or {}).get("Networks") or {}).values():
                if net.get("IPAddress"):
                    found[net["IPAddress"]] = info
        for n in client.networks.list():
            for cfg in (n.attrs.get("IPAM") or {}).get("Config") or []:
                try:
                    nets.append(ipaddress.ip_network(cfg["Subnet"], strict=False))
                except (KeyError, ValueError):
                    pass
    except Exception as e:  # noqa: BLE001 - Docker restarting: keep what we knew
        print(f"jupydo-netlog: cannot read Docker ({e})", file=sys.stderr)
    with LOCK:
        CONTAINERS.update(found)   # keep old entries: a container may have just stopped
        NETWORKS = nets or NETWORKS


def in_docker(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in NETWORKS)


# [1700000000.123456]	    [NEW] tcp      6 120 SYN_SENT src=172.18.0.7 dst=1.2.3.4 sport=51234
#   dport=443 [UNREPLIED] src=1.2.3.4 dst=192.168.1.10 sport=443 dport=61022
EVENT = re.compile(r"^\[(?P<ts>\d+\.\d+)\]\s+\[NEW\]\s+(?:ipv[46]\s+\d+\s+)?(?P<proto>\w+)\s+\d+\s+(?P<rest>.*)$")


def parse(line):
    """(time, proto, src, sport, dst, dport, public_port) of a NEW conntrack event, or None."""
    m = EVENT.match(line.strip())
    if not m:
        return None
    rest = m.group("rest")
    src = re.findall(r"\bsrc=(\S+)", rest)
    dst = re.findall(r"\bdst=(\S+)", rest)
    sport = re.findall(r"\bsport=(\d+)", rest)
    dport = re.findall(r"\bdport=(\d+)", rest)
    if not src or not dst:
        return None
    # the reply tuple's destination port is the public port after the NAT (masquerade)
    public = dport[1] if len(dport) > 1 else ""
    return (float(m.group("ts")), m.group("proto"), src[0], sport[0] if sport else "",
            dst[0], dport[0] if dport else "", public)


class Writer:
    def __init__(self, out, retention_days):
        self.out, self.retention = out, retention_days
        self.day, self.f = None, None
        os.makedirs(out, exist_ok=True)
        os.chmod(out, 0o700)

    def write(self, when, text):
        stamp = datetime.datetime.fromtimestamp(when).astimezone()
        day = stamp.strftime("%Y-%m-%d")
        with LOCK:
            if day != self.day:
                self._rotate(day)
            self.f.write(f"{stamp.isoformat(timespec='seconds')}  {text}\n")
            self.f.flush()

    def _rotate(self, day):
        if self.f:
            self.f.close()
        self.day = day
        fd = os.open(os.path.join(self.out, day + ".log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self.f = os.fdopen(fd, "a", encoding="utf-8")
        cutoff = datetime.date.today() - datetime.timedelta(days=self.retention)
        for name in os.listdir(self.out):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\.log(\.gz)?$", name)
            if not m or m.group(1) == day:
                continue
            path = os.path.join(self.out, name)
            if datetime.date.fromisoformat(m.group(1)) < cutoff:
                os.remove(path)
            elif not m.group(2):   # an older day: compress it
                with open(path, "rb") as src, gzip.open(path + ".gz", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(path + ".gz", 0o600)
                os.remove(path)


def container_events(writer):
    """Starts, stops and deletions of containers, with their address (docker events)."""
    while True:
        try:
            for e in docker_client().events(decode=True, filters={"type": "container"}):
                action = e.get("Action") or e.get("status") or ""
                if action not in ("start", "die", "destroy"):
                    continue
                attrs = (e.get("Actor") or {}).get("Attributes") or {}
                name = attrs.get("name", "")
                if action == "start":
                    refresh(force=True)
                with LOCK:
                    ips = [ip for ip, c in CONTAINERS.items() if c["name"] == name]
                writer.write(e.get("time", time.time()),
                             f"CONTAINER {action} user={user_of(name, attrs)} container={name} "
                             f"image={attrs.get('image', '')} ip={','.join(ips) or '-'}")
        except Exception as e:  # noqa: BLE001 - Docker restarting: listen again
            print(f"jupydo-netlog: Docker events stopped ({e})", file=sys.stderr)
        time.sleep(5)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--retention-days", type=int, default=365)
    a = ap.parse_args()
    writer = Writer(a.out, a.retention_days)
    refresh(force=True)
    threading.Thread(target=container_events, args=(writer,), daemon=True).start()
    writer.write(time.time(), f"NETLOG started (retention {a.retention_days} days)")
    p = subprocess.Popen(["conntrack", "-E", "-e", "NEW", "-o", "timestamp,extended"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in p.stdout:
        ev = parse(line)
        if not ev:
            continue
        when, proto, src, sport, dst, dport, public = ev
        if in_docker(dst):
            continue   # between containers or to the hub: not a connection to outside
        with LOCK:
            c = CONTAINERS.get(src)
        if c is None:
            refresh()
            with LOCK:
                c = CONTAINERS.get(src)
        if c is None and not in_docker(src):
            continue   # the host's own connections
        c = c or {"name": "?", "image": "?", "user": "?"}
        writer.write(when, f"user={c['user']} container={c['name']} image={c['image']} "
                           f"{src}:{sport} -> {dst}:{dport} {proto} public_port={public or '-'}")
    sys.exit("jupydo-netlog: conntrack stopped: " + (p.stderr.read() or "").strip())


if __name__ == "__main__":
    main()
