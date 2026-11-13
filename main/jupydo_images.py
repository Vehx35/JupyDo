#!/usr/bin/env python3
"""
JupyDo "images" service (JupyterHub managed service, for all users, in the
Services menu): commit and download of the user's own environments, chosen
from a drop-down list. A user can only act on their own environments. Saved images are listed for
everyone. Operations above the size limit become requests for the admins.
Python 3.9 compatible.
"""
import json
import logging
import os
from urllib.parse import urlparse

from jupyterhub.services.auth import HubOAuthCallbackHandler, HubOAuthenticated
from jupyterhub.utils import url_path_join
from tornado import ioloop, web

import jupydo_images_lib as lib

log = logging.getLogger("images")
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jupydo_images.html")


class Base(HubOAuthenticated, web.RequestHandler):
    def check_xsrf_cookie(self):
        pass  # JSON API: cross-origin POST with JSON is blocked by the browser

    def write_error(self, status_code, **kwargs):
        exc = kwargs.get("exc_info", (None, None))[1]
        self.finish({"error": getattr(exc, "log_message", None) or self._reason})

    @property
    def user(self):
        return self.current_user["name"]


class PageHandler(Base):
    @web.authenticated
    def get(self):
        self.finish(lib.ui.render_page(PAGE, self.current_user))


class StateHandler(Base):
    @web.authenticated
    def get(self):
        envs = lib.user_environments(self.user)
        server = self.get_argument("server", envs[0] if envs else "")
        container = lib.find_env(self.user, server)
        exports_dir = os.path.join(lib.SHARED_ROOT, self.user + "_shared", "exports")
        exports = sorted((f for f in os.listdir(exports_dir) if f.endswith((".zip", ".tar.gz"))), reverse=True) \
            if os.path.isdir(exports_dir) else []
        self.finish({
            "user": self.user, "server": server, "environments": envs,
            "environment": None if container is None else {
                "name": container.name, "image": container.attrs["Config"].get("Image"),
                "status": container.status},
            "limit_gb": lib.SIZE_LIMIT_GB,
            "tasks": sorted((t for t in lib.tasks.values() if t["user"] == self.user),
                            key=lambda t: t["started"], reverse=True),
            "requests": [r for r in lib.load_requests() if r["user"] == self.user][-10:][::-1],
            "saved_images": lib.saved_images(),
            "exports": exports,
        })


class ActionHandler(Base):
    @web.authenticated
    def post(self, kind):
        try:
            body = json.loads(self.request.body or b"{}")
        except ValueError:
            raise web.HTTPError(400, "invalid JSON")
        server = str(body.get("server") or "")
        name = str(body.get("name") or "").strip().lower() or None
        try:
            what, obj = lib.start("commit" if kind == "commit" else "export", self.user, server, name)
        except ValueError as e:
            raise web.HTTPError(400, str(e))
        log.info("%s of %s/%s by %s: %s", kind, self.user, server, self.user, what)
        self.finish({"type": what, what: obj})


def main():
    logging.basicConfig(level=logging.INFO, format="[images %(levelname)s] %(message)s")
    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/services/images/")
    j = lambda p: url_path_join(prefix, p)  # noqa: E731
    app = web.Application([
        (prefix, PageHandler),
        (j("oauth_callback"), HubOAuthCallbackHandler),
        (j("api/state"), StateHandler),
        (j(r"api/(commit|download)"), ActionHandler),
    ], cookie_secret=os.urandom(32))
    url = urlparse(os.environ.get("JUPYTERHUB_SERVICE_URL", "http://127.0.0.1:10105"))
    app.listen(url.port, url.hostname)
    log.info("Listening on %s%s (limit %.0f GB)", url.geturl(), prefix, lib.SIZE_LIMIT_GB)
    ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
