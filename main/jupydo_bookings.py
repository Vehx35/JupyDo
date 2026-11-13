#!/usr/bin/env python3
"""
JupyDo "bookings" service (JupyterHub managed service, for all users, in the
Services menu): a shared calendar to agree on who uses what and when, e.g. a GPU.

Admins create the bookable resources (name + description: "GPU 0", "the whole
server"...). Users book time slots on them; two bookings of the same resource
cannot overlap. Users cancel their own bookings, admins any. Nothing is enforced:
it is an agreement among users (the GPU itself is given by the GPU request of an
environment, approved by an admin).

Data: bookings.json in the hub state folder. Times are UTC epoch seconds (the page
shows them in the browser's time zone).
"""
import json
import logging
import os
import threading
import time
import uuid
from urllib.parse import urlparse

from jupyterhub.services.auth import HubOAuthCallbackHandler, HubOAuthenticated
from jupyterhub.utils import url_path_join
from tornado import ioloop, web

import jupydo_ui as ui

log = logging.getLogger("bookings")
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jupydo_bookings.html")
DATA = os.path.join(ui.STATE_DIR, "bookings.json")
KEEP_DAYS = float(os.environ.get("JUPYDO_BOOKINGS_KEEP_DAYS", "90"))  # past bookings kept this long
MAX_HOURS = float(os.environ.get("JUPYDO_BOOKINGS_MAX_HOURS", str(14 * 24)))  # longest single booking
_lock = threading.Lock()


def load():
    try:
        with open(DATA) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"resources": [], "bookings": []}


def save(data):
    cutoff = time.time() - KEEP_DAYS * 86400
    data["bookings"] = [b for b in data["bookings"] if b["end"] >= cutoff]
    tmp = DATA + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, DATA)


class Base(HubOAuthenticated, web.RequestHandler):
    def check_xsrf_cookie(self):
        pass  # JSON API: cross-origin requests with a JSON body are blocked by the browser

    def write_error(self, status_code, **kwargs):
        exc = kwargs.get("exc_info", (None, None))[1]
        self.finish({"error": getattr(exc, "log_message", None) or self._reason})

    def body(self):
        try:
            return json.loads(self.request.body or b"{}")
        except ValueError:
            raise web.HTTPError(400, "invalid request")

    def require_admin(self):
        if not self.current_user.get("admin"):
            raise web.HTTPError(403, "admins only")
        return self.current_user["name"]


class PageHandler(Base):
    @web.authenticated
    def get(self):
        self.finish(ui.render_page(PAGE, self.current_user))


class StateHandler(Base):
    @web.authenticated
    def get(self):
        data = load()
        self.finish({"resources": data["resources"], "bookings": data["bookings"],
                     "me": self.current_user["name"], "admin": bool(self.current_user.get("admin")),
                     "max_hours": MAX_HOURS})


class ResourcesHandler(Base):
    """Admins: create (POST) and delete (DELETE /<id>, with its bookings) resources."""
    @web.authenticated
    def post(self, rid=None):
        admin = self.require_admin()
        body = self.body()
        name = str(body.get("name", "")).strip()[:80]
        if not name:
            raise web.HTTPError(400, "give the resource a name")
        res = {"id": uuid.uuid4().hex[:10], "name": name,
               "description": str(body.get("description", "")).strip()[:300]}
        with _lock:
            data = load()
            if any(r["name"].lower() == name.lower() for r in data["resources"]):
                raise web.HTTPError(409, "a resource with this name already exists")
            data["resources"].append(res)
            save(data)
        log.info("resource %r created by %s", name, admin)
        self.finish(res)

    @web.authenticated
    def delete(self, rid):
        admin = self.require_admin()
        with _lock:
            data = load()
            if not any(r["id"] == rid for r in data["resources"]):
                raise web.HTTPError(404, "resource not found")
            data["resources"] = [r for r in data["resources"] if r["id"] != rid]
            data["bookings"] = [b for b in data["bookings"] if b["resource"] != rid]
            save(data)
        log.info("resource %s deleted by %s", rid, admin)
        self.finish({"deleted": rid})


class BookingsHandler(Base):
    """Users: book (POST) a slot; cancel (DELETE /<id>) their own (admins: any)."""
    @web.authenticated
    def post(self, bid=None):
        user = self.current_user["name"]
        body = self.body()
        try:
            start, end = float(body["start"]), float(body["end"])
        except (KeyError, TypeError, ValueError):
            raise web.HTTPError(400, "give start and end")
        if end <= start:
            raise web.HTTPError(400, "the end must be after the start")
        if end < time.time():
            raise web.HTTPError(400, "this slot is in the past")
        if (end - start) / 3600 > MAX_HOURS:
            raise web.HTTPError(400, "a booking can last at most {:g} hours".format(MAX_HOURS))
        rid = str(body.get("resource", ""))
        with _lock:
            data = load()
            res = next((r for r in data["resources"] if r["id"] == rid), None)
            if res is None:
                raise web.HTTPError(404, "resource not found")
            clash = next((b for b in data["bookings"]
                          if b["resource"] == rid and b["start"] < end and start < b["end"]), None)
            if clash:
                raise web.HTTPError(409, "{} is already booked by {} in that time".format(res["name"], clash["user"]))
            booking = {"id": uuid.uuid4().hex[:10], "resource": rid, "user": user, "start": start, "end": end,
                       "note": str(body.get("note", "")).strip()[:200], "created": time.time()}
            data["bookings"].append(booking)
            save(data)
        log.info("%s booked %s from %s to %s", user, res["name"], time.ctime(start), time.ctime(end))
        self.finish(booking)

    @web.authenticated
    def delete(self, bid):
        user = self.current_user["name"]
        with _lock:
            data = load()
            b = next((b for b in data["bookings"] if b["id"] == bid), None)
            if b is None:
                raise web.HTTPError(404, "booking not found")
            if b["user"] != user and not self.current_user.get("admin"):
                raise web.HTTPError(403, "you can only cancel your own bookings")
            data["bookings"] = [x for x in data["bookings"] if x["id"] != bid]
            save(data)
        if b["user"] != user:  # an admin cancelled someone else's booking: tell them
            ui.add_notification([b["user"]], "Your booking from {} was cancelled by {}.".format(
                time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(b["start"])), user), level="warning", sender=user)
        log.info("booking %s (%s) cancelled by %s", bid, b["user"], user)
        self.finish({"deleted": bid})


def main():
    logging.basicConfig(level=logging.INFO, format="[bookings %(levelname)s] %(message)s")
    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/services/bookings/")
    j = lambda p: url_path_join(prefix, p)  # noqa: E731
    app = web.Application([
        (prefix, PageHandler),
        (j("oauth_callback"), HubOAuthCallbackHandler),
        (j("api/state"), StateHandler),
        (j("api/resources"), ResourcesHandler),
        (j(r"api/resources/([a-f0-9]+)"), ResourcesHandler),
        (j("api/bookings"), BookingsHandler),
        (j(r"api/bookings/([a-f0-9]+)"), BookingsHandler),
    ], cookie_secret=os.urandom(32))
    url = urlparse(os.environ.get("JUPYTERHUB_SERVICE_URL", "http://127.0.0.1:10107"))
    app.listen(url.port, url.hostname)
    log.info("Listening on %s%s", url.geturl(), prefix)
    ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
