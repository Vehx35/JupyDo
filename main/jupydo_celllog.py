#!/usr/bin/env python3
"""
JupyDo "celllog" service (JupyterHub managed service, no page). Only when the
administrator enabled the cell log in first_start.sh (JUPYDO_CELL_LOG=yes).

Receives the cells users run in JupyterLab, sent by jupydo_celllog_server.py from each
environment, and appends them to one file per user, <JUPYDO_CELLLOG_DIR>/<user>.log
(on the host: JupyDo/logs/cells/, readable only by root). One JSON object per line:
time (of the hub), user, environment, notebook, kernel, cell id and code.
The user is the owner of the API token the environment sends (asked to the hub), never
a name written by the environment: a user cannot write in another user's log.
Python 3.9 compatible.
"""
import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from tornado import ioloop, web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError

log = logging.getLogger("celllog")
LOG_DIR = os.environ.get("JUPYDO_CELLLOG_DIR", "/srv/jupydo-logs/cells")
HUB_API = os.environ.get("JUPYTERHUB_API_URL", "http://127.0.0.1:8081/hub/api").rstrip("/")
TOKEN_CACHE_S = 300
_users = {}   # token -> (user name, time it was checked)


async def user_of(token):
    """The hub user who owns the token, or None (asked to the hub, cached a few minutes)."""
    hit = _users.get(token)
    if hit and time.time() - hit[1] < TOKEN_CACHE_S:
        return hit[0]
    try:
        r = await AsyncHTTPClient().fetch(HUB_API + "/user", headers={"Authorization": "token " + token},
                                          request_timeout=10)
    except HTTPClientError as e:
        if e.code in (401, 403):
            return None
        raise
    name = json.loads(r.body).get("name")
    if not isinstance(name, str) or not name:
        return None
    _users[token] = (name, time.time())
    return name


def write_line(user, entry):
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", user)
    path = os.path.join(LOG_DIR, safe + ".log")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class CellsHandler(web.RequestHandler):
    def check_xsrf_cookie(self):
        pass  # called by the environments' servers with a token, not by browsers

    async def post(self):
        auth = self.request.headers.get("Authorization", "")
        token = auth[len("token "):].strip() if auth.lower().startswith("token ") else ""
        if not token:
            raise web.HTTPError(401)
        user = await user_of(token)
        if not user:
            raise web.HTTPError(403)
        try:
            cell = json.loads(self.request.body)
        except ValueError:
            raise web.HTTPError(400, "invalid JSON")
        if not isinstance(cell, dict):
            raise web.HTTPError(400)
        sent = cell.get("time")
        entry = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "user": user,
            "environment": str(cell.get("environment", ""))[:200],
            "notebook": str(cell.get("notebook", ""))[:1000],
            "kernel": str(cell.get("kernel", ""))[:200],
            "cell_id": str(cell.get("cell_id", ""))[:200],
            "run_at": (datetime.fromtimestamp(sent).astimezone().isoformat(timespec="seconds")
                       if isinstance(sent, (int, float)) else ""),
            "truncated": bool(cell.get("truncated")),
            "code": str(cell.get("code", "")),
        }
        write_line(user, entry)
        self.set_status(204)


def main():
    logging.basicConfig(level=logging.INFO, format="[celllog %(levelname)s] %(message)s")
    os.makedirs(LOG_DIR, exist_ok=True)
    os.chmod(LOG_DIR, 0o700)
    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/services/celllog/")
    app = web.Application([(prefix + "api/cells", CellsHandler)])
    url = urlparse(os.environ.get("JUPYTERHUB_SERVICE_URL", "http://127.0.0.1:10109"))
    app.listen(url.port, url.hostname)
    log.info("Listening on %s%s, writing to %s", url.geturl(), prefix, LOG_DIR)
    ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
