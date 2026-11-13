"""
JupyDo %%job magic: run a notebook cell as an independent batch job.

    %load_ext jobqueue_magic

    %%job --time 2h --cpus 8 --mem 32 --gpus 1 --name training
    import numpy as np
    ...
    np.save("results.npy", result)     # save what you need: nothing else survives

    %jobs                 list your jobs and queue status
    %job_log <id>         show the job output (stdout + stderr)
    %job_cancel <id>      cancel a pending or running job

The cell runs in a NEW container, made from a snapshot of this environment taken
when you submit the job (same software, including what you installed, and the
same /sharedFolder), with plain Python: variables defined in this notebook are
not available, and IPython syntax (!commands, %magics) does not work inside the job.
"""
import json
import os
import re
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.request

from IPython.core.magic import Magics, cell_magic, line_magic, magics_class
from IPython.core.magic_arguments import argument, magic_arguments, parse_argstring

JOB_ROOT = os.environ.get("JOBQUEUE_DIR", "/sharedFolder/.jobs")
QUEUE_URL = os.environ.get("JOBQUEUE_URL", "http://jupyterhub:8000/services/jobqueue").rstrip("/")


def _call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        QUEUE_URL + path, data=data, method=method,
        headers={"Authorization": "token " + os.environ["JUPYTERHUB_API_TOKEN"],
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


def _parse_time(text):
    """'90' or '90m' -> 90, '2h' -> 120, '1h30m' -> 90 (minutes)."""
    text = text.strip().lower()
    if text.isdigit():
        return int(text)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", text)
    if not m or not any(m.groups()):
        raise ValueError("invalid time '{}': use e.g. 90m, 2h, 1h30m".format(text))
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    return "{}h{:02d}m".format(h, rem // 60) if h else "{}m{:02d}s".format(rem // 60, rem % 60)


@magics_class
class JobQueueMagics(Magics):

    @magic_arguments()
    @argument("--time", "-t", default="1h", help="Expected runtime (walltime), e.g. 30m, 2h, 1h30m. "
              "If exceeded while others are waiting, the job is killed.")
    @argument("--cpus", "-c", type=float, default=1, help="CPU cores")
    @argument("--mem", "-m", type=float, default=4, help="Memory in GB")
    @argument("--gpus", "-g", type=int, default=0, help="Number of GPUs")
    @argument("--name", "-n", default=None, help="Job name")
    @cell_magic
    def job(self, line, cell):
        """Submit this cell as an independent batch job."""
        args = parse_argstring(self.job, line)
        walltime = _parse_time(args.time)

        job_id = secrets.token_hex(6)
        job_dir = os.path.join(JOB_ROOT, job_id)
        os.makedirs(job_dir)
        script = os.path.join(job_dir, "script.py")
        log_path = os.path.join(job_dir, "output.log")
        with open(script, "w") as f:
            f.write(cell)

        workdir = os.getcwd()
        if not workdir.startswith("/sharedFolder"):
            print("WARNING: current directory {} is not under /sharedFolder: it may not "
                  "exist in the job container.".format(workdir))

        try:
            job = _call("POST", "/jobs", {
                "id": job_id, "name": args.name or job_id,
                "server_name": os.environ.get("JUPYTERHUB_SERVER_NAME", ""),
                "python": sys.executable, "script": script, "workdir": workdir, "log": log_path,
                "cpus": args.cpus, "mem_gb": args.mem, "gpus": args.gpus, "walltime_min": walltime,
            })
        except RuntimeError:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        print("Job {} submitted: {} CPU, {} GB, {} GPU, time limit {}.".format(
            job["id"], args.cpus, args.mem, args.gpus, _fmt_duration(walltime * 60)))
        print("It runs in a new container: variables from this notebook are NOT available,")
        print("and only what the job writes to files is kept.")
        print("Output: {}   |   status: %jobs   |   log: %job_log {}".format(log_path, job["id"]))

    @line_magic
    def jobs(self, line):
        """List your jobs and the batch pool status."""
        res = _call("GET", "/jobs")
        p = res["pool"]
        print("Pool: {:g}/{:g} CPU free, {:g}/{:g} GB free, {}/{} GPU free | {} running, {} pending".format(
            p["free_cpus"], p["cpus"], p["free_mem_gb"], p["mem_gb"], p["free_gpus"], p["gpus"],
            p["running"], p["pending"]))
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
                             "{:g}G".format(j["mem_gb"]), j["gpus"], _fmt_duration(j["walltime_s"]),
                             _fmt_duration(elapsed), info))

    @line_magic
    def job_log(self, line):
        """Show the output of a job: %job_log <id> [n_lines]"""
        parts = line.split()
        if not parts:
            print("Usage: %job_log <id> [n_lines]")
            return
        n = int(parts[1]) if len(parts) > 1 else 50
        path = os.path.join(JOB_ROOT, parts[0], "output.log")
        if not os.path.exists(path):
            print("No output yet (the job may still be pending).")
            return
        with open(path, errors="replace") as f:
            print("".join(f.readlines()[-n:]), end="")

    @line_magic
    def job_cancel(self, line):
        """Cancel a job: %job_cancel <id>"""
        job_id = line.strip()
        res = _call("DELETE", "/jobs/" + job_id)
        print("Job {}: cancellation requested (state: {}).".format(job_id, res["state"]))


def load_ipython_extension(ipython):
    ipython.register_magics(JobQueueMagics)
