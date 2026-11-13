#!/usr/bin/env python3
"""
JupyDo "notifications" service (JupyterHub managed service, admins only).

Admin page (Services -> notifications): write a message to one user, several
users or everyone, with a level (info / warning / urgent). Users see it:
  - on the hub home page;
  - as a bar at the top of JupyterLab, within 30 seconds, while they work.
They dismiss it from either place.
The history shows who has read each message. Dismissing is handled by the hub
itself (/hub/jupydo/notifications/<id>/read), so users need no access here.
Python 3.9 compatible.
"""
import json
import logging
import os
from urllib.parse import urlparse

from jupyterhub.services.auth import HubOAuthCallbackHandler, HubOAuthenticated
from jupyterhub.utils import url_path_join
from tornado import ioloop, web

import jupydo_ui as ui

log = logging.getLogger("notifications")
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jupydo_notifications.html")
SERVER_NAME = os.environ.get("JUPYDO_SERVER_NAME", "JupyDo")
LOGO_FILE = os.environ.get("JUPYDO_LOGO_FILE", "/opt/genome-index/app/static/img/jupydo.png")
UID_MAP = os.path.join(ui.STATE_DIR, "users_uid_map.tsv")


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


def refresh(to):
    """Update the notifications file in every environment of the recipients."""
    for user in (known_users() if to == "*" else to):
        ui.refresh_user_branding(user, SERVER_NAME, LOGO_FILE)


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


class PageHandler(Base):
    @web.authenticated
    def get(self):
        self.require_admin()
        self.finish(ui.render_page(PAGE, self.current_user))


class NotificationsHandler(Base):
    @web.authenticated
    def get(self):
        self.require_admin()
        data = ui.load_notifications()
        msgs = sorted(data["messages"], key=lambda m: m["created"], reverse=True)
        for m in msgs:
            m["read_by"] = sorted(u for u, ids in data["read"].items() if m["id"] in ids)
        self.finish({"messages": msgs, "users": sorted(known_users())})

    @web.authenticated
    def post(self):
        admin = self.require_admin()
        try:
            body = json.loads(self.request.body or b"{}")
        except ValueError:
            raise web.HTTPError(400, "invalid JSON")
        text = str(body.get("text") or "").strip()
        if not text:
            raise web.HTTPError(400, "empty message")
        to = body.get("to")
        if to != "*":
            to = [u.strip() for u in (to if isinstance(to, list) else str(to or "").split(",")) if u.strip()]
            if not to:
                raise web.HTTPError(400, "choose the recipients or everyone")
        msg = ui.add_notification(to, text, level=body.get("level", "info"), sender=admin)
        refresh(to)
        log.info("Notification %s sent by %s to %s", msg["id"], admin, to)
        self.finish(msg)


class NotificationHandler(Base):
    @web.authenticated
    def delete(self, msg_id):
        admin = self.require_admin()
        msg = next((m for m in ui.load_notifications()["messages"] if m["id"] == msg_id), None)
        ui.remove_notification(msg_id)
        if msg:
            refresh(msg["to"])
        log.info("Notification %s deleted by %s", msg_id, admin)
        self.finish({"deleted": msg_id})


def main():
    logging.basicConfig(level=logging.INFO, format="[notifications %(levelname)s] %(message)s")
    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/services/notifications/")
    j = lambda p: url_path_join(prefix, p)  # noqa: E731
    app = web.Application([
        (prefix, PageHandler),
        (j("oauth_callback"), HubOAuthCallbackHandler),
        (j("api/notifications"), NotificationsHandler),
        (j(r"api/notifications/([A-Za-z0-9_.@-]+)"), NotificationHandler),
    ], cookie_secret=os.urandom(32))
    url = urlparse(os.environ.get("JUPYTERHUB_SERVICE_URL", "http://127.0.0.1:10104"))
    app.listen(url.port, url.hostname)
    log.info("Listening on %s%s", url.geturl(), prefix)
    ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
