"""
JupyDo job queue - admin web page.

Served at /services/jobqueue/ and listed by JupyterHub in the "Services" menu,
which the hub shows only to admins. Login goes through Hub OAuth; non-admin
users get 403 here as well.

Admins can see the pool, all jobs of all users, the live log of running jobs,
and kill/cancel any job. They cannot change the queue order.
"""
import os
import sys
import time
import shutil

import docker
from jupyterhub.services.auth import HubOAuthCallbackHandler, HubOAuthenticated
from jupyterhub.utils import url_path_join
from tornado import web

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobqueue_admin.html")
sys.path.insert(0, "/opt/jupydo")  # jupydo_ui: the hub menu shared by all JupyDo pages
import jupydo_ui  # noqa: E402


class AdminBase(HubOAuthenticated, web.RequestHandler):
    def initialize(self, jq):
        self.jq = jq  # the running jobqueue_service module (shared state)

    def check_xsrf_cookie(self):
        pass  # JSON calls only (DELETE/GET), cross-origin DELETE is blocked by the browser

    def write_error(self, status_code, **kwargs):
        exc = kwargs.get("exc_info", (None, None))[1]
        self.finish({"error": getattr(exc, "log_message", None) or self._reason})

    def require_admin(self):
        user = self.current_user
        if not user or not user.get("admin"):
            raise web.HTTPError(403, "admins only")
        return user["name"]


class AdminPageHandler(AdminBase):
    @web.authenticated
    def get(self):
        self.require_admin()
        self.finish(jupydo_ui.render_page(PAGE, self.current_user))


class AdminStateHandler(AdminBase):
    @web.authenticated
    def get(self):
        self.require_admin()
        jq = self.jq
        with jq.lock:
            now = time.time()
            order = [j["id"] for j in jq.queue_order(now)]
            running = [j for j in jq.jobs.values() if j["state"] == "running"]
            cpu, mem, gpus = jq.free_resources(running)
            all_jobs = []
            for j in jq.jobs.values():
                view = {k: v for k, v in j.items() if k not in ("spec", "command")}
                view["image"] = j["spec"].get("image_name")
                if j["state"] == "pending":
                    view["priority"] = round(jq.priority(j, now), 3)
                    view["position"] = order.index(j["id"]) + 1
                all_jobs.append(view)
        self.finish({
            "now": now,
            "jobs": all_jobs,
            "pool": {"cpus": jq.POOL_CPUS, "mem_gb": jq.POOL_MEM_GB, "gpus": jq.POOL_GPUS,
                     "interactive_cpus": jq.INTERACTIVE["cpus"], "interactive_mem_gb": jq.INTERACTIVE["mem_gb"],
                     "interactive_count": jq.INTERACTIVE["count"],
                     "free_cpus": cpu, "free_mem_gb": mem, "free_gpus": gpus,
                     "pending": len(order), "running": len(running)},
        })


SHARED_ROOT = "/srv/jh_shared"


def host_log_path(job):
    """Where the job's log is on this side: /sharedFolder/... of the user's environment
    is /srv/jh_shared/<user>_shared/... here. None if it is elsewhere."""
    path = job.get("log") or ""
    if path.startswith("/sharedFolder/"):
        path = os.path.join(SHARED_ROOT, job["user"] + "_shared", path[len("/sharedFolder/"):])
    path = os.path.normpath(path)
    return path if path.startswith(SHARED_ROOT + os.sep) else None


class AdminJobHandler(AdminBase):
    @web.authenticated
    def delete(self, job_id):
        admin = self.require_admin()
        jq = self.jq
        with jq.lock:
            job = jq.jobs.get(job_id)
            if job is None:
                raise web.HTTPError(404, "job not found")
            if job["state"] not in ("pending", "running"):
                # finished job: remove it from the list, and its folder .jobs/<id> (log, script)
                folder = os.path.dirname(host_log_path(job) or "")
                del jq.jobs[job_id]
                jq.save_state()
                if folder.endswith(os.path.join(".jobs", job_id)) and os.path.isdir(folder):
                    shutil.rmtree(folder, ignore_errors=True)
                jq.log.info("Finished job %s of %s deleted by admin %s", job_id, job["user"], admin)
                self.finish({"id": job_id, "state": "deleted"})
                return
            if job["state"] == "pending":
                jq.finish(job, "cancelled", error="cancelled by admin " + admin)
            elif job["state"] == "running" and not job.get("stop_sent_at"):
                try:
                    docker.from_env().containers.get(job["container_name"]).kill(signal="SIGTERM")
                except docker.errors.APIError:
                    pass
                job["stop_sent_at"] = time.time()
                job["stop_reason"] = "killed"
                job["error"] = "killed by admin " + admin
            jq.save_state()
            jq.log.info("Job %s %s by admin %s", job_id, job["state"], admin)
            self.finish({"id": job_id, "state": job["state"]})


class AdminLogHandler(AdminBase):
    @web.authenticated
    def get(self, job_id):
        self.require_admin()
        jq = self.jq
        with jq.lock:
            job = jq.jobs.get(job_id)
            if job is None:
                raise web.HTTPError(404, "job not found")
            state, name, path = job["state"], job["container_name"], job["log"]
            host_path = host_log_path(job)
        # the log is a file in the user's folder: read it directly, running or finished
        if host_path and os.path.isfile(host_path):
            try:
                with open(host_path, "rb") as f:
                    f.seek(max(0, os.path.getsize(host_path) - 200_000))
                    lines = f.read().decode(errors="replace").splitlines()[-200:]
                self.finish({"log": "\n".join(lines) or "(empty)"})
            except OSError as e:
                self.finish({"log": "Could not read log: {}".format(e)})
            return
        if state != "running":
            self.finish({"log": "No log file found (expected: {}).".format(path)})
            return
        try:
            res = docker.from_env().containers.get(name).exec_run(["tail", "-n", "200", path])
            text = res.output.decode(errors="replace")
        except docker.errors.APIError as e:
            text = "Could not read log: {}".format(e)
        self.finish({"log": text})


def admin_routes(prefix, jq):
    kw = {"jq": jq}
    return [
        (prefix, AdminPageHandler, kw),
        (url_path_join(prefix, "oauth_callback"), HubOAuthCallbackHandler),
        (url_path_join(prefix, "admin/state"), AdminStateHandler, kw),
        (url_path_join(prefix, r"admin/jobs/([a-z0-9]+)"), AdminJobHandler, kw),
        (url_path_join(prefix, r"admin/jobs/([a-z0-9]+)/log"), AdminLogHandler, kw),
    ]
