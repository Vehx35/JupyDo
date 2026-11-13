#!/usr/bin/env python3
"""
JupyDo job queue - a JupyterHub managed service.

Users submit batch jobs from JupyterLab (the %%job magic). Each job runs in a
fresh Docker container cloned from the user's running server: same image
(pinned by image ID), same volumes, same devices/security options, same user.
The container dies when the job ends; nothing stays in memory.

Scheduling:
  * priority  = HRRN: (waited + walltime) / walltime  -> short jobs go first,
                long jobs age up and never starve
  * backfill  = if the top job does not fit, later jobs may start only if they
                fit now AND finish (by walltime) before the top job could start
  * walltime  = a job running past its declared walltime is stopped
                (SIGTERM, then SIGKILL after a grace period) only if someone
                is waiting in the queue

Resources come from a dedicated batch pool (CPUs, memory, GPU IDs), configured
via environment variables (see CONFIG below). Python 3.9 compatible.
"""
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import sys
import threading
import time
from urllib.parse import urlparse

import docker
from docker.types import DeviceRequest
from tornado import httpclient, ioloop, web
from tornado.escape import json_decode

log = logging.getLogger("jobqueue")

# ----------------------------------------------------------------------------
# CONFIG (environment variables, set in jupyterhub_config.py)
# ----------------------------------------------------------------------------
def detect_gpus():
    """NVIDIA GPUs of the host, as the indices nvidia-smi and Docker use (PCI bus order).
    The driver lists them in /proc/driver/nvidia/gpus, visible in containers too."""
    try:
        buses = sorted(os.listdir("/proc/driver/nvidia/gpus"))
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []
    return [str(i) for i in range(len(buses))]


# "auto" (default) = every GPU of the host; or a list of GPU indices, e.g. "2,3"
_gpus = os.environ.get("JOBQ_GPUS", "auto").strip()
POOL_GPUS = detect_gpus() if _gpus in ("", "auto") else [g.strip() for g in _gpus.split(",") if g.strip()]


def _host_mem_gb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    return float("inf")

def _pool_size(name, host, reserve):
    """"auto" (default) = the whole host minus a small reserve for the system;
    or a number, used as an upper limit (never more than the real host)."""
    value = os.environ.get(name, "auto").strip()
    if value in ("", "auto"):
        return max(host - reserve, 1.0)
    return min(float(value), host)


SYSTEM_RESERVE_CPUS = float(os.environ.get("JOBQ_SYSTEM_RESERVE_CPUS", "2"))
SYSTEM_RESERVE_MEM_GB = float(os.environ.get("JOBQ_SYSTEM_RESERVE_MEM_GB", "4"))
POOL_CPUS = _pool_size("JOBQ_CPUS", float(os.cpu_count() or 1), SYSTEM_RESERVE_CPUS)
POOL_MEM_GB = _pool_size("JOBQ_MEM_GB", _host_mem_gb(), SYSTEM_RESERVE_MEM_GB)
# The limits of the interactive environments running now are kept for them: jobs
# get what is left (updated at every scheduler tick, see update_interactive)
INTERACTIVE = {"cpus": 0.0, "mem_gb": 0.0, "count": 0}
MAX_WALLTIME_MIN = int(os.environ.get("JOBQ_MAX_WALLTIME_MIN", str(24 * 60)))
KILL_GRACE_S = int(os.environ.get("JOBQ_KILL_GRACE_S", "60"))
TICK_S = float(os.environ.get("JOBQ_TICK_S", "5"))
STATE_FILE = os.environ.get("JOBQ_STATE_FILE", "/etc/jupyterhub/jobqueue_state.json")
# finished jobs kept in the list per user: 0 = all (only an admin deletes them)
HISTORY_PER_USER = int(os.environ.get("JOBQ_HISTORY_PER_USER", "0"))
USAGE_FILE = os.environ.get("JOBQ_USAGE_FILE", "/etc/jupyterhub/jobqueue_usage.json")
FAIRSHARE_HALFLIFE_S = float(os.environ.get("JOBQ_FAIRSHARE_HALFLIFE_DAYS", "7")) * 86400
HUB_API_URL = os.environ.get("JUPYTERHUB_API_URL", "http://127.0.0.1:8081/hub/api").rstrip("/")

# Internal API for other hub services (e.g. the Genome Index Service), which
# submit containers of their own instead of cloning a user's server.
INTERNAL_TOKEN = os.environ.get("JOBQ_INTERNAL_TOKEN", "")
INTERNAL_IMAGE_PREFIX = os.environ.get("JOBQ_INTERNAL_IMAGE_PREFIX", "")
INTERNAL_BIND_ROOT = os.path.normpath(os.environ.get("JOBQ_INTERNAL_BIND_ROOT", "/nonexistent"))
LABEL = "jupydo.jobqueue"
JOB_ID_RE = re.compile(r"^[a-z0-9]{8,32}$")
ACTIVE = ("pending", "running")

jobs = {}                 # job_id -> job dict
usage = {"updated": None, "users": {}}  # user -> {"cpu": CPU-hours, "gpu": GPU-hours}, decayed
lock = threading.RLock()  # protects `jobs`


# ----------------------------------------------------------------------------
# State persistence
# ----------------------------------------------------------------------------
def load_state():
    global jobs, usage
    try:
        with open(STATE_FILE) as f:
            jobs = json.load(f)
        log.info("Loaded %d jobs from %s", len(jobs), STATE_FILE)
    except FileNotFoundError:
        jobs = {}
    try:
        with open(USAGE_FILE) as f:
            usage = json.load(f)
    except FileNotFoundError:
        usage = {"updated": None, "users": {}}


def save_state():
    for path, data in ((STATE_FILE, jobs), (USAGE_FILE, usage)):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)


def trim_history():
    """Keep only the last HISTORY_PER_USER finished jobs per user (0: keep them all)."""
    if HISTORY_PER_USER <= 0:
        return
    by_user = {}
    for j in jobs.values():
        if j["state"] not in ACTIVE:
            by_user.setdefault(j["user"], []).append(j)
    for finished in by_user.values():
        finished.sort(key=lambda j: j.get("ended_at") or 0)
        for j in finished[:-HISTORY_PER_USER]:
            del jobs[j["id"]]


# ----------------------------------------------------------------------------
# Scheduling helpers
# ----------------------------------------------------------------------------
def accrue_usage(now):
    """Decay everyone's past usage (half-life), then charge running jobs for the
    time since the last update, in allocated CPU-hours and GPU-hours."""
    last = usage.get("updated") or now
    dt = max(0.0, now - last)
    if dt > 0:
        decay = 0.5 ** (dt / FAIRSHARE_HALFLIFE_S)
        for u in usage["users"].values():
            u["cpu"] *= decay
            u["gpu"] *= decay
        hours = dt / 3600
        for j in jobs.values():
            if j["state"] == "running":
                u = usage["users"].setdefault(j["user"], {"cpu": 0.0, "gpu": 0.0})
                u["cpu"] += j["cpus"] * hours
                u["gpu"] += j["gpus"] * hours
        # forget users whose usage has decayed to nothing
        usage["users"] = {k: v for k, v in usage["users"].items() if v["cpu"] + v["gpu"] > 1e-6}
    usage["updated"] = now


def fairshare(user, resource):
    """2^(-N * user_usage / total_usage) over the N users who used this resource:
    1 = no recent usage, 0.5 = average usage, 0.25 = twice the average."""
    values = [u[resource] for u in list(usage["users"].values()) if u[resource] > 0]
    total = sum(values)
    if total <= 0:
        return 1.0
    mine = usage["users"].get(user, {}).get(resource, 0.0)
    return 2 ** (-len(values) * mine / total)


def priority(job, now):
    """HRRN (short and long-waiting jobs first) x fair-share on the job's main
    resource: GPU usage for GPU jobs, CPU usage otherwise."""
    waited = now - job["submitted_at"]
    hrrn = (waited + job["walltime_s"]) / job["walltime_s"]
    return hrrn * fairshare(job["user"], "gpu" if job["gpus"] > 0 else "cpu")


def expected_end(job, now):
    return max(now, job["started_at"] + job["walltime_s"])


def free_resources(running):
    cpu = POOL_CPUS - INTERACTIVE["cpus"] - sum(j["cpus"] for j in running)
    mem = POOL_MEM_GB - INTERACTIVE["mem_gb"] - sum(j["mem_gb"] for j in running)
    used = {g for j in running for g in j["gpu_ids"]}
    gpus = [g for g in POOL_GPUS if g not in used]
    return cpu, mem, gpus


def fits(job, cpu, mem, n_gpus):
    eps = 1e-9
    return job["cpus"] <= cpu + eps and job["mem_gb"] <= mem + eps and job["gpus"] <= n_gpus


def shadow_time(job, running, now, cpu, mem, n_gpus):
    """Earliest time `job` could start, assuming running jobs end at their walltime."""
    for r in sorted(running, key=lambda r: expected_end(r, now)):
        cpu += r["cpus"]
        mem += r["mem_gb"]
        n_gpus += r["gpus"]
        if fits(job, cpu, mem, n_gpus):
            return expected_end(r, now)
    return float("inf")


def queue_order(now):
    pending = [j for j in jobs.values() if j["state"] == "pending"]
    return sorted(pending, key=lambda j: (-priority(j, now), j["submitted_at"]))


# ----------------------------------------------------------------------------
# Docker side
# ----------------------------------------------------------------------------
def snapshot_user_server(dc, username, server_name):
    """Copy what matters from the user's running JupyterLab container (named servers aware)."""
    for c in dc.containers.list(filters={"status": "running"}):
        if c.labels.get(LABEL):
            continue
        cfg = c.attrs["Config"]
        env = cfg.get("Env") or []
        if ("JUPYTERHUB_USER=" + username) not in env:
            continue
        if ("JUPYTERHUB_SERVER_NAME=" + server_name) not in env:
            continue
        hc = c.attrs["HostConfig"]
        devices = [
            "{}:{}:{}".format(d["PathOnHost"], d["PathInContainer"], d.get("CgroupPermissions") or "rwm")
            for d in (hc.get("Devices") or [])
        ]
        # Drop hub credentials and GPU visibility (GPUs are assigned per job)
        keep_env = [e for e in env
                    if not e.startswith(("JUPYTERHUB_", "JPY_", "NVIDIA_VISIBLE_DEVICES="))]
        # Servers start as root and start.sh drops to NB_UID:NB_GID (jovyan).
        # The job skips start.sh, so run it directly as that user: files it
        # writes stay editable from JupyterLab.
        user = cfg.get("User") or None
        if (cfg.get("Labels") or {}).get("org.jupydo.builder"):
            user = "root"  # JupyDo image: its entrypoint creates the user's account and drops to it
        elif user in ("root", "0", "0:0"):
            env_map = dict(e.split("=", 1) for e in env if "=" in e)
            user = "{}:{}".format(env_map.get("NB_UID", "1000"), env_map.get("NB_GID", "100"))
        return {
            "image": c.attrs["Image"],          # replaced by the job's snapshot at submission
            "image_name": cfg.get("Image"),
            "container_id": c.id,
            "binds": hc.get("Binds") or [],
            "devices": devices,
            "security_opt": hc.get("SecurityOpt") or [],
            "user": user,
            "env": keep_env,
        }
    return None


# Every job runs from a snapshot (commit) of the user's environment taken when the
# job is submitted: what the user installed by hand (sudo apt, pip --user...) is
# there too. The snapshot is temporary: removed once the job is over.
SNAPSHOT_REPO = "jupydo-jobsnap"


def commit_snapshot(container_id, user, job_id):
    """Snapshot of the environment for one job (the environment pauses for a moment)."""
    repo = "{}/{}".format(SNAPSHOT_REPO, re.sub(r"[^a-z0-9_.-]+", "-", user.lower()).strip("-.") or "user")
    image = docker.from_env().containers.get(container_id).commit(
        repository=repo, tag=job_id, changes=["LABEL jupydo.jobsnap={}".format(job_id)])
    log.info("Job %s: snapshot %s:%s of the environment", job_id, repo, job_id)
    return image.id


def cleanup_snapshots(dc):
    """Remove the snapshots of jobs that are over (finished, failed, cancelled, lost)."""
    active = {j["id"] for j in jobs.values() if j["state"] in ("pending", "running")}
    for image in dc.images.list(filters={"label": "jupydo.jobsnap"}):
        job_id = (image.labels or {}).get("jupydo.jobsnap")
        if job_id in active:
            continue
        try:
            dc.images.remove(image.id, force=False)
            log.info("Removed snapshot of finished job %s", job_id)
        except docker.errors.APIError:
            pass  # still used by a container being removed: next time


def update_interactive(dc):
    """CPU/RAM limits of the running interactive environments: reserved for their users.
    An environment without a limit (set so by an admin) cannot be reserved: counts 0."""
    cpus = mem = 0.0
    count = 0
    for c in dc.containers.list(filters={"status": "running"}):
        if c.labels.get(LABEL):
            continue
        if not any(e.startswith("JUPYTERHUB_USER=") for e in (c.attrs["Config"].get("Env") or [])):
            continue
        hc = c.attrs["HostConfig"]
        if hc.get("NanoCpus"):
            cpus += hc["NanoCpus"] / 1e9
        elif hc.get("CpuQuota", 0) > 0 and hc.get("CpuPeriod"):
            cpus += hc["CpuQuota"] / hc["CpuPeriod"]
        mem += (hc.get("Memory") or 0) / 1024 ** 3
        count += 1
    INTERACTIVE.update(cpus=cpus, mem_gb=mem, count=count)


def start_container(dc, job, gpu_ids):
    spec = job["spec"]
    env = list(spec["env"]) + [
        "NVIDIA_VISIBLE_DEVICES=" + (",".join(gpu_ids) if gpu_ids else "void"),
        "JOBQ_JOB_ID=" + job["id"],
    ]
    kwargs = dict(
        image=spec["image"],
        command=["/bin/sh", "-c", job["command"]],
        name=job["container_name"],
        detach=True,
        init=True,  # tini as PID 1: forwards SIGTERM to the job
        healthcheck={"test": ["NONE"]},  # image healthcheck expects JupyterLab, not running here
        labels={LABEL: "1", "jupydo.user": job["user"], "jupydo.job": job["id"]},
        environment=env,
        volumes=spec["binds"],
        working_dir=job["workdir"],
        nano_cpus=int(job["cpus"] * 1e9),
        mem_limit="{}m".format(int(job["mem_gb"] * 1024)),
    )
    if spec["devices"]:
        kwargs["devices"] = spec["devices"]
    if spec["security_opt"]:
        kwargs["security_opt"] = spec["security_opt"]
    if spec["user"]:
        kwargs["user"] = spec["user"]
    if spec.get("network_mode"):
        kwargs["network_mode"] = spec["network_mode"]
    if gpu_ids:
        kwargs["device_requests"] = [
            DeviceRequest(driver="nvidia", device_ids=list(gpu_ids), capabilities=[["gpu"]])
        ]
    dc.containers.run(**kwargs)


def finish(job, state, exit_code=None, error=None):
    job["state"] = state
    job["exit_code"] = exit_code
    job["ended_at"] = time.time()
    if error:
        job["error"] = error
    log.info("Job %s (%s) -> %s (exit %s)", job["id"], job["user"], state, exit_code)


# ----------------------------------------------------------------------------
# Scheduler loop
# ----------------------------------------------------------------------------
def update_running(dc, now):
    anyone_waiting = any(j["state"] == "pending" for j in jobs.values())
    for job in [j for j in jobs.values() if j["state"] == "running"]:
        try:
            c = dc.containers.get(job["container_name"])
        except docker.errors.NotFound:
            finish(job, "lost", error="container disappeared")
            continue

        if c.status in ("exited", "dead"):
            code = c.attrs["State"].get("ExitCode")
            state = job.get("stop_reason") or ("done" if code == 0 else "failed")
            finish(job, state, code)
            try:
                c.remove()
            except docker.errors.APIError:
                pass
            continue

        if job.get("stop_sent_at"):
            if now - job["stop_sent_at"] > KILL_GRACE_S:
                c.kill()  # SIGKILL
            continue

        if anyone_waiting and now > job["started_at"] + job["walltime_s"]:
            log.info("Job %s over walltime with queue waiting: stopping", job["id"])
            c.kill(signal="SIGTERM")
            job["stop_sent_at"] = now
            job["stop_reason"] = "killed"


def schedule(dc, now):
    running = [j for j in jobs.values() if j["state"] == "running"]
    cpu, mem, gpus = free_resources(running)
    shadow = None  # start time reserved for the top blocked job

    for job in queue_order(now):
        if fits(job, cpu, mem, len(gpus)) and (shadow is None or now + job["walltime_s"] <= shadow):
            gpu_ids = gpus[:job["gpus"]]
            try:
                start_container(dc, job, gpu_ids)
            except Exception as e:  # bad image, docker error, ...
                finish(job, "failed", error="could not start: {}".format(e))
                continue
            job.update(state="running", started_at=now, gpu_ids=gpu_ids)
            log.info("Job %s (%s) started, gpus=%s", job["id"], job["user"], gpu_ids)
            cpu -= job["cpus"]
            mem -= job["mem_gb"]
            gpus = gpus[len(gpu_ids):]
            running.append(job)
        elif shadow is None:
            shadow = shadow_time(job, running, now, cpu, mem, len(gpus))


# Daily backup of the hub state (this service runs inside the hub container)
HUB_STATE_DIR = os.environ.get("JOBQ_HUB_STATE_DIR", "/etc/jupyterhub")
BACKUP_DIR = os.environ.get("JOBQ_BACKUP_DIR", os.path.join(HUB_STATE_DIR, "backups"))
BACKUP_EVERY_S = float(os.environ.get("JOBQ_BACKUP_EVERY_H", "24")) * 3600
BACKUP_KEEP = int(os.environ.get("JOBQ_BACKUP_KEEP", "7"))
_last_backup = [0.0]


def backup_hub_state(now):
    """Consistent copy of the hub database (sqlite backup API: safe while the hub is
    writing, journal included), plus cookie secret, UID map and queue state.
    Runs at startup and then every BACKUP_EVERY_S; keeps the last BACKUP_KEEP.
    Same disk as the state: protects from corruption and mistakes, not from disk loss."""
    if now - _last_backup[0] < BACKUP_EVERY_S:
        return
    _last_backup[0] = now
    hub_db = os.path.join(HUB_STATE_DIR, "jupyterhub.sqlite")
    if not os.path.isfile(hub_db):
        return
    dest = os.path.join(BACKUP_DIR, time.strftime("%Y%m%d-%H%M%S"))
    try:
        os.makedirs(dest)
        os.chmod(BACKUP_DIR, 0o700)
        src = sqlite3.connect("file:{}?mode=ro".format(hub_db), uri=True)
        dst = sqlite3.connect(os.path.join(dest, "jupyterhub.sqlite"))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        for path in (os.path.join(HUB_STATE_DIR, "jupyterhub_cookie_secret"),
                     os.path.join(HUB_STATE_DIR, "users_uid_map.tsv"), STATE_FILE, USAGE_FILE):
            if os.path.isfile(path):
                shutil.copy2(path, dest)
        old = sorted(d for d in os.listdir(BACKUP_DIR) if os.path.isdir(os.path.join(BACKUP_DIR, d)))
        for d in old[:-BACKUP_KEEP]:
            shutil.rmtree(os.path.join(BACKUP_DIR, d), ignore_errors=True)
        log.info("Hub state backed up to %s", dest)
    except Exception:
        log.exception("Hub state backup FAILED (disk full? permissions?): %s", dest)


def scheduler_loop():
    dc = docker.from_env()
    ticks = 0
    while True:
        if ticks % 12 == 0:  # about once a minute
            try:
                with lock:
                    cleanup_snapshots(dc)
            except Exception:
                log.exception("Snapshot cleanup failed")
        ticks += 1
        try:
            update_interactive(dc)
        except Exception:
            log.exception("Cannot read the interactive environments")
        try:
            with lock:
                now = time.time()
                accrue_usage(now)
                update_running(dc, now)
                schedule(dc, now)
                trim_history()
                save_state()
        except Exception:
            log.exception("Scheduler tick failed")
        backup_hub_state(time.time())
        time.sleep(TICK_S)


# ----------------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------------
_token_cache = {}  # token -> (username, expires_at)


class BaseHandler(web.RequestHandler):
    def check_xsrf_cookie(self):
        pass  # token-authenticated API, no cookies

    def write_error(self, status_code, **kwargs):
        exc = kwargs.get("exc_info", (None, None))[1]
        msg = getattr(exc, "log_message", None) or self._reason
        self.finish({"error": msg})

    async def current_username(self):
        auth = self.request.headers.get("Authorization", "")
        if not auth.lower().startswith("token "):
            raise web.HTTPError(401, "missing token")
        token = auth.split(None, 1)[1].strip()
        cached = _token_cache.get(token)
        if cached and cached[1] > time.time():
            return cached[0]
        try:
            resp = await httpclient.AsyncHTTPClient().fetch(
                HUB_API_URL + "/user", headers={"Authorization": "token " + token})
        except httpclient.HTTPClientError:
            raise web.HTTPError(403, "invalid token")
        name = json_decode(resp.body)["name"]
        _token_cache[token] = (name, time.time() + 60)
        return name

    def job_view(self, job, now, order):
        view = {k: v for k, v in job.items() if k not in ("spec", "command")}
        view["image"] = job["spec"].get("image_name")
        if job["state"] == "pending":
            view["priority"] = round(priority(job, now), 3)
            view["position"] = order.index(job["id"]) + 1
        return view


class JobsHandler(BaseHandler):
    async def get(self):
        user = await self.current_username()
        with lock:
            now = time.time()
            order = [j["id"] for j in queue_order(now)]
            mine = [self.job_view(j, now, order) for j in jobs.values() if j["user"] == user]
            running = [j for j in jobs.values() if j["state"] == "running"]
            cpu, mem, gpus = free_resources(running)
        mine.sort(key=lambda j: j["submitted_at"], reverse=True)
        self.finish({
            "jobs": mine,
            "pool": {"cpus": POOL_CPUS, "mem_gb": POOL_MEM_GB, "gpus": len(POOL_GPUS),
                     "interactive_cpus": INTERACTIVE["cpus"], "interactive_mem_gb": INTERACTIVE["mem_gb"],
                     "free_cpus": cpu, "free_mem_gb": mem, "free_gpus": len(gpus),
                     "pending": len(order), "running": len(running)},
        })

    async def post(self):
        user = await self.current_username()
        try:
            body = json_decode(self.request.body)
            job_id = str(body["id"])
            cpus = float(body["cpus"])
            mem_gb = float(body["mem_gb"])
            gpus = int(body["gpus"])
            walltime_min = int(body["walltime_min"])
            paths = {k: str(body[k]) for k in ("python", "script", "workdir", "log")}
            name = str(body.get("name") or job_id)[:64]
            server_name = str(body.get("server_name") or "")
        except (KeyError, ValueError, TypeError):
            raise web.HTTPError(400, "malformed request")

        if not JOB_ID_RE.match(job_id):
            raise web.HTTPError(400, "invalid job id")
        if not (0 < cpus <= POOL_CPUS):
            raise web.HTTPError(400, "cpus must be in (0, {}]".format(POOL_CPUS))
        if not (0 < mem_gb <= POOL_MEM_GB):
            raise web.HTTPError(400, "mem must be in (0, {}] GB".format(POOL_MEM_GB))
        if not (0 <= gpus <= len(POOL_GPUS)):
            raise web.HTTPError(400, "gpus must be in [0, {}]".format(len(POOL_GPUS)))
        if not (0 < walltime_min <= MAX_WALLTIME_MIN):
            raise web.HTTPError(400, "time must be in (0, {}] minutes".format(MAX_WALLTIME_MIN))
        for k, p in paths.items():
            if not p.startswith("/") or "\n" in p or "\0" in p:
                raise web.HTTPError(400, "invalid path: " + k)

        if not re.match(r"^[A-Za-z0-9_.@-]{0,255}$", server_name):
            raise web.HTTPError(400, "invalid server name")
        spec = snapshot_user_server(docker.from_env(), user, server_name)
        if spec is None:
            raise web.HTTPError(409, "no running JupyterLab server found for this user/server")
        try:  # run the job on the environment as it is now (not as it was created)
            spec["image"] = await ioloop.IOLoop.current().run_in_executor(
                None, commit_snapshot, spec.pop("container_id"), user, job_id)
        except docker.errors.APIError as e:
            raise web.HTTPError(500, "could not snapshot the environment for the job: {}".format(e))

        command = "exec {} {} > {} 2>&1".format(
            shlex.quote(paths["python"]), shlex.quote(paths["script"]), shlex.quote(paths["log"]))
        job = {
            "id": job_id, "user": user, "server_name": server_name, "name": name, "state": "pending",
            "cpus": cpus, "mem_gb": mem_gb, "gpus": gpus, "gpu_ids": [],
            "walltime_s": walltime_min * 60, "submitted_at": time.time(),
            "started_at": None, "ended_at": None, "exit_code": None,
            "workdir": paths["workdir"], "log": paths["log"], "command": command,
            "container_name": "jobq-{}-{}".format(re.sub(r"[^a-zA-Z0-9_.-]", "-", user), job_id),
            "spec": spec,
        }
        with lock:
            if job_id in jobs:
                raise web.HTTPError(409, "job id already exists")
            jobs[job_id] = job
            save_state()
        log.info("Job %s submitted by %s", job_id, user)
        self.set_status(201)
        self.finish(self.job_view(job, time.time(), [job_id]))


class JobHandler(BaseHandler):
    async def delete(self, job_id):
        user = await self.current_username()
        with lock:
            job = jobs.get(job_id)
            if job is None or job["user"] != user:
                raise web.HTTPError(404, "job not found")
            if job["state"] == "pending":
                finish(job, "cancelled")
            elif job["state"] == "running" and not job.get("stop_sent_at"):
                try:
                    docker.from_env().containers.get(job["container_name"]).kill(signal="SIGTERM")
                except docker.errors.APIError:
                    pass
                job["stop_sent_at"] = time.time()
                job["stop_reason"] = "cancelled"
            save_state()
            self.finish({"id": job_id, "state": job["state"]})


class InternalBase(BaseHandler):
    """Token-authenticated API for trusted hub services only."""

    def prepare(self):
        auth = self.request.headers.get("Authorization", "")
        token = auth.split(None, 1)[1].strip() if auth.lower().startswith("token ") else ""
        if not INTERNAL_TOKEN or not hmac.compare_digest(token, INTERNAL_TOKEN):
            raise web.HTTPError(403, "invalid internal token")


class InternalJobsHandler(InternalBase):
    def post(self):
        try:
            body = json_decode(self.request.body)
            owner = str(body["owner"])
            name = str(body.get("name") or "internal")[:64]
            image = str(body["image"])
            command = str(body["command"])
            binds = [str(b) for b in body["binds"]]
            cpus = min(float(body["cpus"]), POOL_CPUS)          # clamp to the pool
            mem_gb = min(float(body["mem_gb"]), POOL_MEM_GB)
            walltime_min = min(int(body["walltime_min"]), MAX_WALLTIME_MIN)
            log_path = str(body.get("log") or "")
            source = str(body.get("source") or "")
        except (KeyError, ValueError, TypeError):
            raise web.HTTPError(400, "malformed request")

        if not re.match(r"^[A-Za-z0-9_.@-]{1,64}$", owner) or not re.match(r"^[A-Za-z0-9_.-]{0,32}$", source):
            raise web.HTTPError(400, "invalid owner or source")
        if not INTERNAL_IMAGE_PREFIX or not image.startswith(INTERNAL_IMAGE_PREFIX):
            raise web.HTTPError(400, "image not allowed")
        for b in binds:
            src = os.path.normpath(b.split(":", 1)[0])
            if src != INTERNAL_BIND_ROOT and not src.startswith(INTERNAL_BIND_ROOT + os.sep):
                raise web.HTTPError(400, "bind not allowed: " + src)
        if cpus <= 0 or mem_gb <= 0 or walltime_min <= 0:
            raise web.HTTPError(400, "invalid resources")

        job_id = secrets.token_hex(6)
        job = {
            "id": job_id, "user": owner, "server_name": source, "name": name,
            "state": "pending", "cpus": cpus, "mem_gb": mem_gb, "gpus": 0, "gpu_ids": [],
            "walltime_s": walltime_min * 60, "submitted_at": time.time(),
            "started_at": None, "ended_at": None, "exit_code": None,
            "workdir": "/", "log": log_path, "command": command,
            "container_name": "jobq-{}-{}".format(re.sub(r"[^a-zA-Z0-9_.-]", "-", owner), job_id),
            "spec": {"image": image, "image_name": image, "binds": binds, "devices": [],
                     "security_opt": [], "user": None, "env": [], "network_mode": "none"},
        }
        with lock:
            jobs[job_id] = job
            save_state()
        log.info("Internal job %s (%s) submitted for %s", job_id, name, owner)
        self.set_status(201)
        self.finish(self.job_view(job, time.time(), [job_id]))


class InternalJobHandler(InternalBase):
    def get(self, job_id):
        with lock:
            job = jobs.get(job_id)
            if job is None:
                raise web.HTTPError(404, "job not found")
            now = time.time()
            order = [j["id"] for j in queue_order(now)]
            self.finish(self.job_view(job, now, order))


def main():
    logging.basicConfig(level=logging.INFO, format="[jobqueue %(levelname)s] %(message)s")
    load_state()
    threading.Thread(target=scheduler_loop, daemon=True).start()

    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/services/jobqueue/")
    from jobqueue_admin import admin_routes  # admin web page (Services menu, admins only)
    app = web.Application([
        (prefix + r"jobs/?", JobsHandler),
        (prefix + r"jobs/([a-z0-9]+)/?", JobHandler),
        (prefix + r"internal/jobs/?", InternalJobsHandler),
        (prefix + r"internal/jobs/([a-z0-9]+)/?", InternalJobHandler),
    ] + admin_routes(prefix, sys.modules[__name__]), cookie_secret=os.urandom(32))
    url = urlparse(os.environ.get("JUPYTERHUB_SERVICE_URL", "http://127.0.0.1:10101"))
    app.listen(url.port, url.hostname)
    log.info("Listening on %s%s | pool: %s CPUs, %s GB, GPUs %s",
             url.geturl(), prefix, POOL_CPUS, POOL_MEM_GB, POOL_GPUS or "none")
    ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
