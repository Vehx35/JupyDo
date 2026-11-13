#!/usr/bin/env python3
"""
JupyDo "transfer" service (JupyterHub managed service, for all users, in the
Services menu): upload and download of large files to and from the user's own
shared folder (/sharedFolder in JupyterLab, /srv/jh_shared/<user>_shared here).

Why not JupyterLab's drag and drop: it sends files as base64 JSON in 1 MB pieces,
one request at a time, and cannot resume. Here:
- Upload: binary pieces (JUPYDO_TRANSFER_CHUNK_MB, default 64), written to disk as
  they arrive (never kept in memory). The partial file lives in
  <shared>/.jupydo-uploads/; choosing the same file again resumes where it stopped,
  also after a closed browser or a hub restart. When complete it is moved into place
  and given to the user (same UID as their files).
- Download: files are streamed from disk with HTTP Range support, so the browser
  can resume an interrupted download; folders are streamed as an uncompressed .tar
  (no wait for compression, no resume).
A user only ever sees their own shared folder. Works with the environment stopped:
the service reads and writes the folder from the hub container.
Python 3.9 compatible.
"""
import asyncio
import hashlib
import json
import logging
import os
import queue
import tarfile
import threading
import time
from email.utils import formatdate
from urllib.parse import quote, urlparse

from jupyterhub.services.auth import HubOAuthCallbackHandler, HubOAuthenticated
from jupyterhub.utils import url_path_join
from tornado import ioloop, iostream, web

import jupydo_images_lib as lib

log = logging.getLogger("transfer")
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jupydo_transfer.html")
CHUNK = int(float(os.environ.get("JUPYDO_TRANSFER_CHUNK_MB", "64")) * 1024 * 1024)
PART_DAYS = float(os.environ.get("JUPYDO_TRANSFER_PART_DAYS", "7"))  # unfinished uploads kept this long
PART_DIR = ".jupydo-uploads"
READ_BLOCK = 4 * 1024 * 1024
GID = 100  # the users group, as for every file in the shared folders

_active = set()  # upload ids being written right now (one writer per upload)
_active_lock = threading.Lock()


def user_root(user):
    return os.path.realpath(os.path.join(lib.SHARED_ROOT, user + "_shared"))


def resolve(user, rel):
    """Absolute path of rel inside the user's shared folder; symlinks pointing
    outside it are refused."""
    root = user_root(user)
    path = os.path.realpath(os.path.join(root, (rel or "").lstrip("/")))
    if path != root and not path.startswith(root + os.sep):
        raise web.HTTPError(403, "outside your shared folder")
    return path


def give_to_user(user, path):
    uid = lib.user_uid(user)
    if uid is not None:
        try:
            os.chown(path, uid, GID)
        except OSError:
            pass


def valid_name(name):
    return bool(name) and name not in (".", "..") and "/" not in name and "\0" not in name \
        and name != PART_DIR


class Base(HubOAuthenticated, web.RequestHandler):
    def check_xsrf_cookie(self):
        pass  # JSON/binary API: cross-origin requests with these bodies are blocked by the browser

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


class ListHandler(Base):
    @web.authenticated
    def get(self):
        rel = self.get_argument("path", "")
        path = resolve(self.user, rel)
        if not os.path.isdir(path):
            raise web.HTTPError(404, "folder not found")
        entries = []
        with os.scandir(path) as it:
            for e in it:
                if e.name == PART_DIR:
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                entries.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size, "mtime": st.st_mtime})
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        root = user_root(self.user)
        rel = "" if path == root else os.path.relpath(path, root)
        self.finish({"path": rel, "entries": entries, "chunk": CHUNK})


def _upload_paths(user, upload_id):
    d = os.path.join(user_root(user), PART_DIR)
    return d, os.path.join(d, upload_id + ".part"), os.path.join(d, upload_id + ".json")


class UploadStartHandler(Base):
    """Start or resume an upload: same user, destination, size and modification time
    of the file = same upload, so it continues from the bytes already received."""
    @web.authenticated
    def post(self):
        try:
            body = json.loads(self.request.body or b"{}")
            name, size = str(body.get("name") or ""), int(body.get("size"))
            mtime = int(body.get("lastModified") or 0)
        except (ValueError, TypeError):
            raise web.HTTPError(400, "invalid request")
        if not valid_name(name) or size < 0:
            raise web.HTTPError(400, "invalid file name")
        folder = resolve(self.user, body.get("path", ""))
        if not os.path.isdir(folder):
            raise web.HTTPError(404, "folder not found")
        dest = os.path.join(folder, name)
        if os.path.lexists(dest):
            raise web.HTTPError(409, "'%s' already exists in this folder: rename or delete it first" % name)
        upload_id = hashlib.sha256(("%s\0%s\0%d\0%d" % (self.user, dest, size, mtime)).encode()).hexdigest()[:32]
        d, part, meta = _upload_paths(self.user, upload_id)
        if not os.path.isdir(d):
            os.makedirs(d)
            give_to_user(self.user, d)
        if not os.path.exists(meta):
            with open(meta, "w") as f:
                json.dump({"dest": dest, "size": size, "started": time.time()}, f)
            open(part, "ab").close()
            give_to_user(self.user, meta)
            give_to_user(self.user, part)
            log.info("upload start: %s -> %s (%d bytes)", self.user, dest, size)
        offset = os.path.getsize(part)
        if offset:
            log.info("upload resume: %s -> %s at %d of %d bytes", self.user, dest, offset, size)
        self.finish({"id": upload_id, "offset": offset, "size": size, "chunk": CHUNK})


@web.stream_request_body
class UploadChunkHandler(Base):
    """PUT one piece at ?offset=N (must be the bytes already on disk), written as it
    arrives. DELETE cancels the upload."""
    def prepare(self):
        self.f = None
        if self.request.method != "PUT":
            return
        if not self.current_user:
            raise web.HTTPError(403, "not logged in: reload the page")
        self.request.connection.set_max_body_size(CHUNK + 1024 * 1024)
        self.upload_id = self.path_args[0]
        _, self.part, meta = _upload_paths(self.user, self.upload_id)
        if not os.path.exists(meta):
            raise web.HTTPError(404, "upload not found: choose the file again")
        with open(meta) as f:
            self.meta = json.load(f)
        with _active_lock:
            if self.upload_id in _active:
                raise web.HTTPError(409, "this upload is already running (another tab?)")
            _active.add(self.upload_id)
        self.locked = True
        current = os.path.getsize(self.part)
        try:
            offset = int(self.get_argument("offset", "-1"))
        except ValueError:
            offset = -1
        if offset != current:
            raise web.HTTPError(409, "offset mismatch: resume from %d" % current)
        self.f = open(self.part, "ab")

    def data_received(self, chunk):
        if self.f is not None:
            if self.f.tell() + len(chunk) > self.meta["size"]:
                raise web.HTTPError(400, "more data than the file size")
            self.f.write(chunk)

    def put(self, upload_id):
        self.f.close()
        self.f = None
        received = os.path.getsize(self.part)
        if received < self.meta["size"]:
            self.finish({"offset": received, "done": False})
            return
        dest = self.meta["dest"]
        if os.path.lexists(dest):
            raise web.HTTPError(409, "'%s' appeared meanwhile: the upload is kept, rename it and retry"
                                % os.path.basename(dest))
        os.rename(self.part, dest)
        os.remove(_upload_paths(self.user, upload_id)[2])
        give_to_user(self.user, dest)
        log.info("upload done: %s -> %s (%d bytes)", self.user, dest, received)
        self.finish({"offset": received, "done": True})

    @web.authenticated
    def delete(self, upload_id):
        _, part, meta = _upload_paths(self.user, upload_id)
        for p in (part, meta):
            if os.path.exists(p):
                os.remove(p)
        log.info("upload cancelled: %s (%s)", self.user, upload_id)
        self.finish({"cancelled": True})

    def on_finish(self):
        self._release()

    def on_connection_close(self):
        self._release()  # the piece received so far stays on disk: the upload resumes from there

    def _release(self):
        if self.f is not None:
            self.f.close()
            self.f = None
        if getattr(self, "locked", False):
            with _active_lock:
                _active.discard(self.upload_id)
            self.locked = False


class DownloadHandler(Base):
    """A file with Range support (resumable), or a folder as an uncompressed .tar."""
    @web.authenticated
    async def get(self):
        path = resolve(self.user, self.get_argument("path", ""))
        if os.path.isdir(path):
            await self._tar(path)
        elif os.path.isfile(path):
            await self._file(path)
        else:
            raise web.HTTPError(404, "not found")

    def _attachment(self, name):
        self.set_header("Content-Type", "application/octet-stream")
        self.set_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))

    async def _file(self, path):
        st = os.stat(path)
        size, start, end = st.st_size, 0, st.st_size - 1
        self._attachment(os.path.basename(path))
        self.set_header("Accept-Ranges", "bytes")
        self.set_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
        self.set_header("ETag", '"%x-%x"' % (st.st_size, int(st.st_mtime)))
        rng = self.request.headers.get("Range", "")
        if_range = self.request.headers.get("If-Range")
        if rng.startswith("bytes=") and "," not in rng and \
                (not if_range or if_range in (self._headers["ETag"], self._headers["Last-Modified"])):
            a, _, b = rng[6:].partition("-")
            try:
                if a:
                    start, end = int(a), (int(b) if b else size - 1)
                else:
                    start, end = max(0, size - int(b)), size - 1
            except ValueError:
                start, end = 0, size - 1
            if start >= size or start > end:
                self.set_status(416)
                self.set_header("Content-Range", "bytes */%d" % size)
                self.finish()
                return
            end = min(end, size - 1)
            self.set_status(206)
            self.set_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.set_header("Content-Length", str(end - start + 1))
        if start == 0:
            log.info("download: %s <- %s (%d bytes)", self.user, path, size)
        else:
            log.info("download resumed: %s <- %s from %d", self.user, path, start)
        loop = asyncio.get_event_loop()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                data = await loop.run_in_executor(None, f.read, min(READ_BLOCK, left))
                if not data:
                    break
                left -= len(data)
                self.write(data)
                try:
                    await self.flush()
                except iostream.StreamClosedError:
                    return
        self.finish()

    async def _tar(self, path):
        name = os.path.basename(path.rstrip(os.sep)) or self.user + "_shared"
        self._attachment(name + ".tar")
        log.info("download folder: %s <- %s (tar)", self.user, path)
        q = queue.Queue(maxsize=8)
        stop = threading.Event()

        class Writer:  # tarfile writes here, the request reads from the queue
            def write(self, data):
                while not stop.is_set():
                    try:
                        q.put(bytes(data), timeout=1)
                        return len(data)
                    except queue.Full:
                        pass
                raise IOError("download cancelled")

        def produce():
            try:
                with tarfile.open(fileobj=Writer(), mode="w|", bufsize=READ_BLOCK) as tf:
                    tf.add(path, arcname=name,
                           filter=lambda ti: None if os.path.basename(ti.name) == PART_DIR else ti)
            except Exception as e:  # noqa: BLE001 - reported in the log, the download just stops
                if not stop.is_set():
                    log.warning("tar of %s failed: %s", path, e)
            finally:
                while True:
                    try:
                        q.put(None, timeout=1)
                        break
                    except queue.Full:
                        if stop.is_set():
                            break

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, produce)
        try:
            while True:
                data = await loop.run_in_executor(None, q.get)
                if data is None:
                    break
                self.write(data)
                await self.flush()
        except iostream.StreamClosedError:
            stop.set()
            return
        self.finish()


def clean_old_parts():
    """Unfinished uploads older than PART_DAYS are deleted (only in .jupydo-uploads)."""
    limit = time.time() - PART_DAYS * 86400
    try:
        users = os.listdir(lib.SHARED_ROOT)
    except OSError:
        return
    for u in users:
        d = os.path.join(lib.SHARED_ROOT, u, PART_DIR)
        if not u.endswith("_shared") or not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            p = os.path.join(d, f)
            try:
                if os.path.getmtime(p) < limit:
                    os.remove(p)
                    log.info("removed old unfinished upload %s", p)
            except OSError:
                pass


def main():
    logging.basicConfig(level=logging.INFO, format="[transfer %(levelname)s] %(message)s")
    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/services/transfer/")
    j = lambda p: url_path_join(prefix, p)  # noqa: E731
    app = web.Application([
        (prefix, PageHandler),
        (j("oauth_callback"), HubOAuthCallbackHandler),
        (j("api/list"), ListHandler),
        (j("api/upload"), UploadStartHandler),
        (j(r"api/upload/([0-9a-f]{32})"), UploadChunkHandler),
        (j("api/download"), DownloadHandler),
    ], cookie_secret=os.urandom(32))
    url = urlparse(os.environ.get("JUPYTERHUB_SERVICE_URL", "http://127.0.0.1:10106"))
    app.listen(url.port, url.hostname, max_body_size=CHUNK + 1024 * 1024)
    log.info("Listening on %s%s (pieces of %d MB)", url.geturl(), prefix, CHUNK // (1024 * 1024))
    clean_old_parts()
    ioloop.PeriodicCallback(clean_old_parts, 3600 * 1000).start()
    ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
