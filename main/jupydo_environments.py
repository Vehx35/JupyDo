#!/usr/bin/env python3
"""
JupyDo "environments" service (a JupyterHub managed service).

Admin page (Services -> environments, admins only), one table with every user
environment (interactive container):
  - CPU/RAM limits, changed live; reset of a stopped environment (fresh container
    at its next start);
  - disk usage of its user (/sharedFolder, plus the old work volume if not yet migrated; shared by all their
    environments), measured every hour, with a default and per-user limits. Users
    over their limit get an automatic urgent notification (hub home and top of
    JupyterLab) that disappears when they are back under the limit.
  - requests to save/download environments above the size limit: approve or reject.
In the background it also enforces the account validity set on the Authorize
Users page: a warning every day during the last week, then at the expiry the
user is no longer authorized and their running environments are stopped
(containers and data are kept).
Notifications written by admins are in the separate "notifications" service.
Python 3.9 compatible.
"""
import datetime
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

import docker
from jupyterhub.services.auth import HubOAuthCallbackHandler, HubOAuthenticated
from jupyterhub.utils import url_path_join
from tornado import ioloop, web

import jupydo_images_lib as images
import jupydo_ui as ui

log = logging.getLogger("environments")
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "jupydo_environments.html")
SERVER_NAME = os.environ.get("JUPYDO_SERVER_NAME", "JupyDo")
LOGO_FILE = os.environ.get("JUPYDO_LOGO_FILE", "/opt/genome-index/app/static/img/jupydo.png")
DISK_INTERVAL_S = float(os.environ.get("JUPYDO_DISK_INTERVAL_S", "3600"))
UID_MAP = os.path.join(ui.STATE_DIR, "users_uid_map.tsv")
SHARED_ROOT = "/srv/jh_shared"
GB = 1024 ** 3


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------
def host_limits():
    cpus = float(os.cpu_count() or 1)
    with open("/proc/meminfo") as f:
        mem_kb = next(int(l.split()[1]) for l in f if l.startswith("MemTotal:"))
    return cpus, mem_kb / 1024 / 1024


def env_containers():
    """User environments: containers started by the spawner (not queue jobs)."""
    found = []
    for c in docker.from_env().containers.list(all=True):
        env = c.attrs["Config"].get("Env") or []
        if c.labels.get("jupydo.jobqueue") or not any(e.startswith("JUPYTERHUB_USER=") for e in env):
            continue
        found.append(c)
    return found


def env_view(c):
    env = dict(e.split("=", 1) for e in (c.attrs["Config"].get("Env") or []) if "=" in e)
    hc = c.attrs["HostConfig"]
    if hc.get("NanoCpus"):
        cpus = hc["NanoCpus"] / 1e9
    elif hc.get("CpuQuota", 0) > 0 and hc.get("CpuPeriod"):
        cpus = hc["CpuQuota"] / hc["CpuPeriod"]
    else:
        cpus = None
    mem = hc.get("Memory") or 0
    return {"name": c.name, "status": c.status,
            "user": env.get("JUPYTERHUB_USER", "?"), "server": env.get("JUPYTERHUB_SERVER_NAME", ""),
            "linux_user": env.get("NB_USER", ""), "uid": env.get("NB_UID", ""),
            "image": c.attrs["Config"].get("Image"),
            "cpus": cpus, "mem_gb": round(mem / GB, 2) if mem else None}


# ---------------------------------------------------------------------------
# Disk usage (/sharedFolder + old work volume if still there), hourly
# ---------------------------------------------------------------------------
def known_users():
    users = set()
    try:
        with open(UID_MAP) as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split("\t")
                if len(parts) >= 2:
                    users.add(parts[1])
    except FileNotFoundError:
        pass
    return users


def folder_size(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_blocks * 512
            except OSError:
                pass
    return total


def measure_disk():
    """Measure every user, update the usage file, notify users over their limit."""
    try:
        volumes = {v["Name"]: (v.get("UsageData") or {}).get("Size", 0)
                   for v in (docker.from_env().df().get("Volumes") or [])}
    except docker.errors.APIError:
        log.exception("Cannot read Docker volume sizes")
        volumes = {}
    data = ui.load_disk()
    now = time.time()
    for user in known_users():
        work = max(volumes.get("jupyterhub-user-" + user, 0), 0)
        shared = folder_size(os.path.join(SHARED_ROOT, user + "_shared"))
        data["usage"][user] = {"work_gb": round(work / GB, 2), "shared_gb": round(shared / GB, 2),
                               "measured_at": now}
    ui.save_disk(data)
    for user in data["usage"]:
        update_disk_notification(user, data)
    log.info("Disk usage measured for %d users", len(data["usage"]))


def update_disk_notification(user, data):
    """One automatic notification per user over the limit, removed when back under it."""
    usage = data["usage"].get(user)
    if not usage:
        return
    used = usage["work_gb"] + usage["shared_gb"]
    limit = ui.disk_limit_for(data, user)
    msg_id = "disk-" + user
    over = used > limit
    already = any(m["id"] == msg_id for m in ui.load_notifications()["messages"])
    if over:
        ui.add_notification([user], (
            "DISK LIMIT EXCEEDED: you are using {:.0f} GB of your {:.0f} GB. "
            "Delete or move files in /sharedFolder NOW: the administrator has been notified."
        ).format(used, limit), level="danger", in_lab=True, sender="system", msg_id=msg_id)
    elif already:
        ui.remove_notification(msg_id)
    if over or already:
        ui.refresh_user_branding(user, SERVER_NAME, LOGO_FILE)


def disk_loop():
    while True:
        try:
            measure_disk()
        except Exception:
            log.exception("Disk measurement failed")
        time.sleep(DISK_INTERVAL_S)


# ---------------------------------------------------------------------------
# Account validity (dates set on the Authorize Users page)
# ---------------------------------------------------------------------------
HUB_API = os.environ.get("JUPYTERHUB_API_URL", "http://127.0.0.1:8081/hub/api").rstrip("/")
HUB_DB = os.path.join(ui.STATE_DIR, "jupyterhub.sqlite")
EXPIRY_CHECK_S = float(os.environ.get("JUPYDO_EXPIRY_CHECK_S", "600"))


def hub_api(method, path, data=None):
    headers = {"Authorization": "token " + os.environ.get("JUPYTERHUB_API_TOKEN", "")}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(HUB_API + path, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        body = response.read()
    return json.loads(body) if body else None


def stop_servers(user):
    """Stop every running environment of the user (containers and data are kept)."""
    info = hub_api("GET", "/users/" + quote(user)) or {}
    for name in (info.get("servers") or {}):
        path = ("/users/{}/servers/{}".format(quote(user), quote(name)) if name
                else "/users/{}/server".format(quote(user)))
        try:
            hub_api("DELETE", path)
            log.info("Stopped environment '%s' of %s (account expired)", name or "default", user)
        except urllib.error.HTTPError as e:
            log.warning("Cannot stop environment '%s' of %s: %s", name, user, e)


def deauthorize(user):
    """NativeAuthenticator: the user must be authorized again by an admin to log in."""
    try:
        db = sqlite3.connect(HUB_DB, timeout=30)
        with db:
            db.execute("UPDATE users_info SET is_authorized = 0 WHERE username = ?", (user,))
        db.close()
    except sqlite3.Error as e:  # e.g. GitHub login: no NativeAuthenticator table
        log.warning("Cannot unauthorize %s in the hub database: %s", user, e)


def check_expiries(today=None):
    today = today or datetime.date.today()
    for user, info in ui.load_expiry().items():
        if not info.get("until") or user in images.ADMINS:
            continue
        status = ui.account_status(user, today)
        prefix = "expiry-" + user + "-"
        warnings = [m["id"] for m in ui.load_notifications()["messages"]
                    if re.fullmatch(re.escape(prefix) + r"\d{4}-\d{2}-\d{2}", m["id"])]
        if status["expired"]:
            deauthorize(user)
            try:
                stop_servers(user)
            except (urllib.error.URLError, ValueError) as e:
                log.warning("Cannot stop the environments of %s: %s", user, e)
            ui.mark_expired(user, status["until"])
            for msg_id in warnings:
                ui.remove_notification(msg_id)
            log.info("Account of %s expired on %s: unauthorized, environments stopped", user, status["until"])
        elif status["days_left"] <= ui.EXPIRY_WARNING_DAYS:
            msg_id = prefix + today.isoformat()  # a new message every day
            if msg_id not in warnings:
                for old in warnings:
                    ui.remove_notification(old)
                left = status["days_left"]
                ui.add_notification([user], "Your JupyDo account expires on {} ({}): ask an administrator "
                                    "to renew it.".format(status["until"], "TODAY" if left == 0 else
                                                          "tomorrow" if left == 1 else "in {} days".format(left)),
                                    level="danger" if left <= 1 else "warning", sender="system", msg_id=msg_id)
        ui.refresh_user_branding(user, SERVER_NAME, LOGO_FILE)  # days left shown in JupyterLab


def expiry_loop():
    while True:
        try:
            check_expiries()
        except Exception:
            log.exception("Account expiry check failed")
        time.sleep(EXPIRY_CHECK_S)


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------
class Base(HubOAuthenticated, web.RequestHandler):
    def check_xsrf_cookie(self):
        pass  # JSON API: cross-origin POST/DELETE with JSON is blocked by the browser

    def write_error(self, status_code, **kwargs):
        exc = kwargs.get("exc_info", (None, None))[1]
        self.finish({"error": getattr(exc, "log_message", None) or self._reason})

    def require_admin(self):
        user = self.current_user
        if not user or not user.get("admin"):
            raise web.HTTPError(403, "admins only")
        return user["name"]

    def body(self):
        try:
            return json.loads(self.request.body or b"{}")
        except ValueError:
            raise web.HTTPError(400, "invalid JSON")


class PageHandler(Base):
    @web.authenticated
    def get(self):
        self.require_admin()
        self.finish(ui.render_page(PAGE, self.current_user))


class EnvironmentsHandler(Base):
    @web.authenticated
    def get(self):
        self.require_admin()
        cpus, mem = host_limits()
        disk = ui.load_disk()
        envs = sorted((env_view(c) for c in env_containers()), key=lambda e: (e["user"], e["server"]))
        for e in envs:
            u = disk["usage"].get(e["user"], {})
            used = u.get("work_gb", 0) + u.get("shared_gb", 0)
            limit = ui.disk_limit_for(disk, e["user"])
            e["disk"] = {"used_gb": round(used, 2), "work_gb": u.get("work_gb"), "shared_gb": u.get("shared_gb"),
                         "limit_gb": limit, "custom_limit": e["user"] in disk["limits"],
                         "over": used > limit, "measured_at": u.get("measured_at")}
        over = sorted({e["user"] for e in envs if e["disk"]["over"]})
        self.finish({"environments": envs, "host": {"cpus": cpus, "mem_gb": round(mem, 1)},
                     "default_disk_limit_gb": disk["default_limit_gb"], "over_disk_limit": over})


def find_env(name):
    target = next((c for c in env_containers() if c.name == name), None)
    if target is None:
        raise web.HTTPError(404, "environment not found")
    return target


def apply_limits(target, cpus, mem_gb):
    """Change CPU/RAM of an environment container, live (no restart)."""
    host_cpus, host_mem = host_limits()
    if cpus is not None and not (0 < cpus <= host_cpus):
        raise web.HTTPError(400, "CPU must be in (0, {:g}]".format(host_cpus))
    if mem_gb is not None and not (0.5 <= mem_gb <= host_mem):
        raise web.HTTPError(400, "RAM must be in [0.5, {:.0f}] GB".format(host_mem))
    kwargs = {}
    if cpus is not None:
        kwargs.update(cpu_period=100000, cpu_quota=int(cpus * 100000))
    if mem_gb is not None:
        kwargs.update(mem_limit=int(mem_gb * GB), memswap_limit=int(mem_gb * GB))
    if not kwargs:
        raise web.HTTPError(400, "nothing to change")
    try:
        docker.from_env().api.update_container(target.id, **kwargs)
    except docker.errors.APIError as e:
        raise web.HTTPError(400, "Docker refused the change: {}".format(e.explanation or e))
    target.reload()


def env_of(user, server):
    """The container of a user's environment, or None."""
    return next((c for c in env_containers()
                 if env_view(c)["user"] == user and env_view(c)["server"] == server), None)


def server_path(user, server):
    return "/users/{}/servers/{}".format(quote(user), quote(server))


class LimitsHandler(Base):
    @web.authenticated
    def post(self, name):
        admin = self.require_admin()
        body = self.body()
        try:
            cpus = float(body["cpus"]) if body.get("cpus") not in (None, "") else None
            mem_gb = float(body["mem_gb"]) if body.get("mem_gb") not in (None, "") else None
        except (ValueError, TypeError):
            raise web.HTTPError(400, "CPU and RAM must be numbers")
        target = find_env(name)
        apply_limits(target, cpus, mem_gb)
        log.info("Limits of %s set by %s: cpus=%s mem_gb=%s", name, admin, cpus, mem_gb)
        self.finish(env_view(target))


class ResetHandler(Base):
    @web.authenticated
    def post(self, name):
        admin = self.require_admin()
        target = find_env(name)
        if target.status == "running":
            raise web.HTTPError(400, "stop the environment first (hub Admin page), then reset it")
        target.remove()
        log.info("Environment %s reset by %s", name, admin)
        self.finish({"name": name, "reset": True})


class StopHandler(Base):
    """Stop any user's environment (through the hub, as the user's own stop): the
    container and the data are kept."""
    @web.authenticated
    def post(self, name):
        admin = self.require_admin()
        e = env_view(find_env(name))
        try:
            hub_api("DELETE", server_path(e["user"], e["server"]))
        except urllib.error.HTTPError as err:
            raise web.HTTPError(400, "the hub refused to stop it: {}".format(err))
        log.info("Environment %s stopped by %s", name, admin)
        self.finish({"name": name, "stopped": True})


class DeleteHandler(Base):
    """Delete any user's environment (through the hub, as the user's own delete): the
    environment and its container are removed; the user's files (/sharedFolder) stay."""
    @web.authenticated
    def post(self, name):
        admin = self.require_admin()
        e = env_view(find_env(name))
        try:
            hub_api("DELETE", server_path(e["user"], e["server"]), {"remove": True})
        except urllib.error.HTTPError as err:
            raise web.HTTPError(400, "the hub refused to delete it: {}".format(err))
        log.info("Environment %s deleted by %s", name, admin)
        self.finish({"name": name, "deleted": True})


class ResourceRequestsHandler(Base):
    """Requests for more CPU/RAM (from the environment form or the home page)."""
    @web.authenticated
    def get(self):
        self.require_admin()
        reqs = sorted(ui.load_resource_requests(), key=lambda r: r["created"], reverse=True)
        pending = [r for r in reqs if r["status"] == "pending"]
        for r in pending:
            c = env_of(r["user"], r["server"])
            r["current"] = env_view(c) if c else None
        self.finish({"requests": pending + [r for r in reqs if r["status"] != "pending"][:10]})

    @web.authenticated
    def post(self, req_id):
        admin = self.require_admin()
        approve = bool(self.body().get("approve"))
        req = next((r for r in ui.load_resource_requests() if r["id"] == req_id and r["status"] == "pending"), None)
        if req is None:
            raise web.HTTPError(404, "request not found or already decided")
        if approve:
            target = env_of(req["user"], req["server"])
            if target is None:
                raise web.HTTPError(404, "the environment does not exist any more: reject the request")
            apply_limits(target, req["cpus"], req["mem_gb"])
        ui.decide_resource_request(req_id, approve, admin)
        log.info("Resource request %s (%s/%s) %s by %s", req_id, req["user"], req["server"],
                 "applied" if approve else "rejected", admin)
        self.finish({"id": req_id, "status": "approved" if approve else "rejected"})


class GpuRequestsHandler(Base):
    """Requests for a GPU (from the environment form): the environment starts only
    once approved, with the GPU chosen here. Approved ones can be taken back."""
    @web.authenticated
    def get(self):
        self.require_admin()
        reqs = sorted(ui.load_gpu_requests(), key=lambda r: r["created"], reverse=True)
        shown = [r for r in reqs if r["status"] in ("pending", "approved")] \
            + [r for r in reqs if r["status"] not in ("pending", "approved")][:10]
        self.finish({"requests": shown, "gpus": [{"index": i, "model": m} for i, m in ui.detect_gpus()]})

    @web.authenticated
    def post(self, req_id):
        admin = self.require_admin()
        body = self.body()
        action = body.get("action")
        if action not in ("approve", "reject", "revoke"):
            raise web.HTTPError(400, "action must be approve, reject or revoke")
        gpu = str(body.get("gpu", "")).strip()
        if action == "approve" and gpu not in {i for i, _ in ui.detect_gpus()}:
            raise web.HTTPError(400, "choose one of the GPUs of this server")
        req = ui.decide_gpu_request(req_id, action, gpu if action == "approve" else None, admin)
        if req is None:
            raise web.HTTPError(404, "request not found or already decided")
        if action == "approve" and req.get("options"):
            # start the environment with the user's own choices, in the background: the
            # hub may take a while to answer, the admin does not wait for it
            def start():
                try:
                    hub_api("POST", server_path(req["user"], req["server"]), req["options"])
                    log.info("started %s/%s after the GPU approval", req["user"], req["server"])
                except Exception as e:  # noqa: BLE001 - the user can still start it from the home page
                    log.warning("could not start %s/%s after the GPU approval: %s", req["user"], req["server"], e)
            threading.Thread(target=start, daemon=True).start()
        log.info("GPU request %s (%s/%s) %s by %s%s", req_id, req["user"], req["server"], action, admin,
                 f" (GPU {gpu})" if action == "approve" else "")
        self.finish({"id": req_id, "status": req["status"], "user": req["user"], "server": req["server"]})


class ServerLimitsHandler(Base):
    """How many environments each user may have (default ui.DEFAULT_SERVER_LIMIT)."""
    @web.authenticated
    def get(self):
        self.require_admin()
        limits = ui.load_server_limits()
        users = sorted(known_users() | {env_view(c)["user"] for c in env_containers()} | set(limits))
        counts = {}
        for c in env_containers():
            u = env_view(c)["user"]
            counts[u] = counts.get(u, 0) + 1
        self.finish({"default": ui.DEFAULT_SERVER_LIMIT,
                     "users": [{"user": u, "limit": limits.get(u, ui.DEFAULT_SERVER_LIMIT),
                                "custom": u in limits, "environments": counts.get(u, 0)} for u in users]})

    @web.authenticated
    def post(self, user):
        admin = self.require_admin()
        try:
            limit = int(self.body().get("limit"))
        except (TypeError, ValueError):
            raise web.HTTPError(400, "the limit must be a whole number")
        if not 0 <= limit <= 100:
            raise web.HTTPError(400, "the limit must be between 0 (no limit) and 100")
        ui.set_server_limit(user, limit)
        log.info("Environment limit of %s set to %s by %s", user, limit, admin)
        self.finish({"user": user, "limit": limit})


class DiskHandler(Base):
    @web.authenticated
    def get(self):
        self.require_admin()
        data = ui.load_disk()
        users = []
        for user in sorted(set(data["usage"]) | known_users()):
            u = data["usage"].get(user, {})
            used = u.get("work_gb", 0) + u.get("shared_gb", 0)
            limit = ui.disk_limit_for(data, user)
            users.append({"user": user, "work_gb": u.get("work_gb"), "shared_gb": u.get("shared_gb"),
                          "used_gb": round(used, 2), "limit_gb": limit,
                          "custom_limit": user in data["limits"], "over": used > limit,
                          "measured_at": u.get("measured_at")})
        self.finish({"default_limit_gb": data["default_limit_gb"], "users": users})

    @web.authenticated
    def post(self):
        """{"default_limit_gb": N} or {"user": name, "limit_gb": N|null}; null = back to default."""
        admin = self.require_admin()
        body = self.body()
        data = ui.load_disk()
        try:
            if "default_limit_gb" in body:
                data["default_limit_gb"] = max(1.0, float(body["default_limit_gb"]))
            elif body.get("user"):
                if body.get("limit_gb") in (None, ""):
                    data["limits"].pop(body["user"], None)
                else:
                    data["limits"][body["user"]] = max(1.0, float(body["limit_gb"]))
            else:
                raise web.HTTPError(400, "nothing to change")
        except (ValueError, TypeError):
            raise web.HTTPError(400, "the limit must be a number of GB")
        ui.save_disk(data)
        for user in data["usage"]:
            update_disk_notification(user, data)
        log.info("Disk limits changed by %s: %s", admin, body)
        self.finish({"ok": True})


class MeasureHandler(Base):
    @web.authenticated
    def post(self):
        self.require_admin()
        threading.Thread(target=measure_disk, daemon=True).start()
        self.finish({"started": True})


class RequestsHandler(Base):
    """Commit/download requests above the size limit."""
    @web.authenticated
    def get(self):
        self.require_admin()
        reqs = sorted(images.load_requests(), key=lambda r: r["created"], reverse=True)
        self.finish({"requests": [r for r in reqs if r["status"] == "pending"] +
                                 [r for r in reqs if r["status"] != "pending"][:10],
                     "limit_gb": images.SIZE_LIMIT_GB})

    @web.authenticated
    def post(self, req_id):
        admin = self.require_admin()
        approve = bool(self.body().get("approve"))
        req, _task = images.decide_request(req_id, approve, admin)
        if req is None:
            raise web.HTTPError(404, "request not found or already decided")
        log.info("Request %s %s by %s", req_id, req["status"], admin)
        self.finish(req)


def main():
    logging.basicConfig(level=logging.INFO, format="[environments %(levelname)s] %(message)s")
    threading.Thread(target=disk_loop, daemon=True).start()
    threading.Thread(target=expiry_loop, daemon=True).start()
    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/services/environments/")
    j = lambda p: url_path_join(prefix, p)  # noqa: E731
    app = web.Application([
        (prefix, PageHandler),
        (j("oauth_callback"), HubOAuthCallbackHandler),
        (j("api/environments"), EnvironmentsHandler),
        (j(r"api/environments/([A-Za-z0-9_.-]+)/limits"), LimitsHandler),
        (j(r"api/environments/([A-Za-z0-9_.-]+)/reset"), ResetHandler),
        (j(r"api/environments/([A-Za-z0-9_.-]+)/stop"), StopHandler),
        (j(r"api/environments/([A-Za-z0-9_.-]+)/delete"), DeleteHandler),
        (j("api/resource-requests"), ResourceRequestsHandler),
        (j(r"api/resource-requests/([a-f0-9]+)"), ResourceRequestsHandler),
        (j("api/gpu-requests"), GpuRequestsHandler),
        (j(r"api/gpu-requests/([a-f0-9]+)"), GpuRequestsHandler),
        (j("api/server-limits"), ServerLimitsHandler),
        (j(r"api/server-limits/([A-Za-z0-9_.@-]+)"), ServerLimitsHandler),
        (j("api/disk"), DiskHandler),
        (j("api/disk/measure"), MeasureHandler),
        (j("api/requests"), RequestsHandler),
        (j(r"api/requests/([a-f0-9]+)"), RequestsHandler),
    ], cookie_secret=os.urandom(32))
    url = urlparse(os.environ.get("JUPYTERHUB_SERVICE_URL", "http://127.0.0.1:10103"))
    app.listen(url.port, url.hostname)
    log.info("Listening on %s%s", url.geturl(), prefix)
    ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
