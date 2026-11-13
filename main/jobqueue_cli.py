#!/usr/bin/env python3
"""
JupyDo job queue - command line client (standard library only).

Used by the R kernel integration (Rprofile.site) and usable from any terminal
in the container (JupyterLab, RStudio, VS Code):

    python3 /opt/jobqueue/jobqueue_cli.py submit analysis.R --time 2h --cpus 4 --mem 16
    python3 /opt/jobqueue/jobqueue_cli.py list
    python3 /opt/jobqueue/jobqueue_cli.py log <id> [n_lines]
    python3 /opt/jobqueue/jobqueue_cli.py cancel <id>

The script runs in a NEW container (same image, same files): nothing from the
current session is available, only what the job writes to files is kept.
"""
import argparse
import json
import os
import re
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.request

JOB_ROOT = os.environ.get("JOBQUEUE_DIR", "/sharedFolder/.jobs")
QUEUE_URL = os.environ.get("JOBQUEUE_URL", "http://jupyterhub:8000/services/jobqueue").rstrip("/")
INTERPRETERS = {".py": "python3", ".r": "Rscript", ".sh": "bash"}


def call(method, path, payload=None):
    token = os.environ.get("JUPYTERHUB_API_TOKEN")
    if not token:
        raise RuntimeError("JUPYTERHUB_API_TOKEN not set: run this inside a JupyDo server")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        QUEUE_URL + path, data=data, method=method,
        headers={"Authorization": "token " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read())["error"]
        except Exception:
            msg = e.reason
        raise RuntimeError("Job queue: {} (HTTP {})".format(msg, e.code)) from None
    except urllib.error.URLError as e:
        raise RuntimeError("Job queue unreachable at {}: {}".format(QUEUE_URL, e.reason)) from None


def parse_time(text):
    """'90' or '90m' -> 90, '2h' -> 120, '1h30m' -> 90 (minutes)."""
    text = text.strip().lower()
    if text.isdigit():
        return int(text)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", text)
    if not m or not any(m.groups()):
        raise ValueError("invalid time '{}': use e.g. 90m, 2h, 1h30m".format(text))
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    return "{}h{:02d}m".format(h, rem // 60) if h else "{}m{:02d}s".format(rem // 60, rem % 60)


def submit(script_path, interpreter, time_str, cpus, mem, gpus, name, context="this session"):
    ext = os.path.splitext(script_path)[1]
    if interpreter is None:
        interpreter = INTERPRETERS.get(ext.lower())
        if interpreter is None:
            raise ValueError("unknown script type '{}': use --interpreter".format(ext))
    if not os.path.isabs(interpreter):
        found = shutil.which(interpreter)
        if not found:
            raise ValueError("interpreter '{}' not found".format(interpreter))
        interpreter = found
    walltime = parse_time(time_str)

    job_id = secrets.token_hex(6)
    job_dir = os.path.join(JOB_ROOT, job_id)
    os.makedirs(job_dir)
    script = os.path.join(job_dir, "script" + ext)
    log_path = os.path.join(job_dir, "output.log")
    shutil.copyfile(script_path, script)

    workdir = os.getcwd()
    home = os.path.dirname(JOB_ROOT)
    if not workdir.startswith(home):
        print("WARNING: current directory {} is not under {}: it may not exist in the "
              "job container.".format(workdir, home))
    try:
        job = call("POST", "/jobs", {
            "id": job_id, "name": name or job_id,
            "server_name": os.environ.get("JUPYTERHUB_SERVER_NAME", ""),
            "python": interpreter, "script": script, "workdir": workdir, "log": log_path,
            "cpus": cpus, "mem_gb": mem, "gpus": gpus, "walltime_min": walltime,
        })
    except RuntimeError:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    print("Job {} submitted: {:g} CPU, {:g} GB, {} GPU, time limit {}.".format(
        job["id"], cpus, mem, gpus, fmt_duration(walltime * 60)))
    print("It runs in a new container: variables from {} are NOT available,".format(context))
    print("and only what the job writes to files is kept.")
    print("Output: {}".format(log_path))


def list_jobs():
    res = call("GET", "/jobs")
    p = res["pool"]
    print("Pool: {:g}/{:g} CPU free, {:g}/{:g} GB free, {}/{} GPU free | {} running, {} pending".format(
        p["free_cpus"], p["cpus"], round(p["free_mem_gb"], 1), round(p["mem_gb"], 1),
        p["free_gpus"], p["gpus"], p["running"], p["pending"]))
    if not res["jobs"]:
        print("No jobs.")
        return
    now = time.time()
    fmt = "{:<13} {:<16} {:<10} {:>5} {:>6} {:>4} {:>8} {:>10}  {}"
    print(fmt.format("ID", "NAME", "STATE", "CPU", "MEM", "GPU", "LIMIT", "ELAPSED", "INFO"))
    for j in res["jobs"]:
        if j["state"] == "pending":
            elapsed = now - j["submitted_at"]
            info = "queue pos {} (priority {})".format(j["position"], j["priority"])
        elif j["state"] == "running":
            elapsed = now - j["started_at"]
            info = "GPU " + ",".join(j["gpu_ids"]) if j["gpu_ids"] else ""
        else:
            elapsed = (j["ended_at"] or now) - (j["started_at"] or j["submitted_at"])
            info = "exit {}".format(j["exit_code"]) if j["exit_code"] is not None else j.get("error", "")
        print(fmt.format(j["id"], j["name"][:16], j["state"], "{:g}".format(j["cpus"]),
                         "{:g}G".format(j["mem_gb"]), j["gpus"], fmt_duration(j["walltime_s"]),
                         fmt_duration(elapsed), info))


def show_log(job_id, n=50):
    path = os.path.join(JOB_ROOT, job_id, "output.log")
    if not os.path.exists(path):
        print("No output yet (the job may still be pending).")
        return
    with open(path, errors="replace") as f:
        print("".join(f.readlines()[-n:]), end="")


def cancel(job_id):
    res = call("DELETE", "/jobs/" + job_id)
    print("Job {}: cancellation requested (state: {}).".format(job_id, res["state"]))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jobqueue_cli.py", description="JupyDo job queue client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit", help="submit a script as a batch job")
    s.add_argument("script", help="script file (.py, .R, .sh)")
    s.add_argument("--time", "-t", default="1h", help="expected runtime, e.g. 30m, 2h, 1h30m")
    s.add_argument("--cpus", "-c", type=float, default=1, help="CPU cores")
    s.add_argument("--mem", "-m", type=float, default=4, help="memory in GB")
    s.add_argument("--gpus", "-g", type=int, default=0, help="number of GPUs")
    s.add_argument("--name", "-n", default=None, help="job name")
    s.add_argument("--interpreter", default=None, help="override the interpreter (e.g. /usr/bin/Rscript)")
    s.add_argument("--context", default="this session", help=argparse.SUPPRESS)

    sub.add_parser("list", help="list your jobs and the pool status")
    lg = sub.add_parser("log", help="show a job's output")
    lg.add_argument("id")
    lg.add_argument("n", nargs="?", type=int, default=50)
    c = sub.add_parser("cancel", help="cancel a pending or running job")
    c.add_argument("id")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "submit":
            submit(args.script, args.interpreter, args.time, args.cpus, args.mem,
                   args.gpus, args.name, args.context)
        elif args.cmd == "list":
            list_jobs()
        elif args.cmd == "log":
            show_log(args.id, args.n)
        elif args.cmd == "cancel":
            cancel(args.id)
    except (RuntimeError, ValueError, OSError) as e:
        print("Error: {}".format(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
