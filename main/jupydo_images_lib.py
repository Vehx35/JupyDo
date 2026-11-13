"""
JupyDo saved images: commit and export of user environments (shared by the
"images" service, used by users, and the "environments" service, used by admins).

- Commit: the environment's container becomes a new image
  jupydo-saved/<user>-<name>:<n> (n grows, nothing is overwritten). Saved images
  are offered to every user when creating an environment.
- Export (Download): the CURRENT state of the environment (including software
  installed with sudo) is saved as one zip in /sharedFolder/exports/<file>.zip, with
  run.sh (Linux/macOS) and run.cmd (Windows) to start it with JupyterLab on any computer with Docker,
  and start_apptainer.sh to start it with Apptainer/Singularity (Linux/WSL, no root).
- Above the size limit, the operation becomes a request that an admin approves.
User files in /sharedFolder are a mount: they are never part of an image.
"""
import gzip
import zipfile
import json
import os
import re
import shlex
import threading
import time
import uuid

import docker

import jupydo_ui as ui

SAVED_REPO = "jupydo-saved"
LABEL_SAVED_BY = "org.jupydo.saved_by"
LABEL_SAVED_NAME = "org.jupydo.saved_name"
LABEL_SAVED_FROM = "org.jupydo.saved_from"
LABEL_SAVED_AT = "org.jupydo.saved_at"
SIZE_LIMIT_GB = float(os.environ.get("JUPYDO_IMAGE_LIMIT_GB", "50"))
ADMINS = [a.strip() for a in os.environ.get("JUPYDO_ADMINS", "").split(",") if a.strip()]
SERVER_NAME = os.environ.get("JUPYDO_SERVER_NAME", "JupyDo")
LOGO_FILE = os.environ.get("JUPYDO_LOGO_FILE", "/opt/genome-index/app/static/img/jupydo.png")
REQUESTS_FILE = os.path.join(ui.STATE_DIR, "image_requests.json")
SHARED_ROOT = "/srv/jh_shared"
UID_MAP = os.path.join(ui.STATE_DIR, "users_uid_map.tsv")
GB = 1024 ** 3
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,40}$")

_lock = threading.RLock()
tasks = {}  # task id -> {"id", "kind", "user", "server", "status", "log": [...], "result"}


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------
def find_env(user, server):
    """The container of one environment of a user (running or stopped)."""
    for c in docker.from_env().containers.list(all=True):
        env = c.attrs["Config"].get("Env") or []
        if c.labels.get("jupydo.jobqueue"):
            continue
        if ("JUPYTERHUB_USER=" + user) in env and ("JUPYTERHUB_SERVER_NAME=" + (server or "")) in env:
            return c
    return None


def user_environments(user):
    """Names of the user's environments ('' = default server)."""
    names = []
    for c in docker.from_env().containers.list(all=True):
        env = dict(e.split("=", 1) for e in (c.attrs["Config"].get("Env") or []) if "=" in e)
        if not c.labels.get("jupydo.jobqueue") and env.get("JUPYTERHUB_USER") == user:
            names.append(env.get("JUPYTERHUB_SERVER_NAME", ""))
    return sorted(names)


def container_size_gb(container):
    """Image + changes of the container (Docker computes it only when asked: size=1).
    Raw API call: older docker-py versions have no size argument in inspect_container."""
    api = docker.from_env().api
    response = api._get(api._url("/containers/{0}/json", container.id), params={"size": 1})
    info = api._result(response, json=True)
    return (info.get("SizeRootFs") or 0) / GB


def safe_part(text):
    return re.sub(r"[^a-z0-9_.-]+", "-", text.lower()).strip("-.") or "x"


def repo_for(user, name):
    return "{}/{}-{}".format(SAVED_REPO, safe_part(user), name)


def next_tag(repo):
    tags = []
    for image in docker.from_env().images.list(name=repo):
        for t in image.tags:
            if t.startswith(repo + ":") and t.split(":")[-1].isdigit():
                tags.append(int(t.split(":")[-1]))
    return str(max(tags) + 1 if tags else 1)


def saved_images():
    """Every saved image (visible to all users), newest first."""
    out = []
    for image in docker.from_env().images.list(filters={"label": LABEL_SAVED_BY}):
        labels = image.labels or {}
        # only real saves: copies (job snapshots, exports) inherit the labels too
        for tag in (t for t in image.tags if t.startswith(SAVED_REPO + "/")):
            out.append({"image": tag, "saved_by": labels.get(LABEL_SAVED_BY, "?"),
                        "name": labels.get(LABEL_SAVED_NAME, ""), "from": labels.get(LABEL_SAVED_FROM, ""),
                        "saved_at": float(labels.get(LABEL_SAVED_AT, 0) or 0),
                        "size_gb": round((image.attrs.get("Size") or 0) / GB, 2)})
    return sorted(out, key=lambda i: i["saved_at"], reverse=True)


def user_uid(user):
    try:
        with open(UID_MAP) as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1] == user:
                    return int(parts[0])
    except (FileNotFoundError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# Tasks (run in background threads, progress shown on the page)
# ---------------------------------------------------------------------------
def new_task(kind, user, server):
    task = {"id": uuid.uuid4().hex[:12], "kind": kind, "user": user, "server": server,
            "status": "running", "log": [], "result": None, "started": time.time()}
    with _lock:
        tasks[task["id"]] = task
    return task


def _log(task, text):
    task["log"].append(time.strftime("%H:%M:%S ") + text)


def run_task(task, fn, *args):
    def target():
        try:
            task["result"] = fn(task, *args)
            task["status"] = "done"
        except Exception as e:
            _log(task, "ERROR: {}".format(e))
            task["status"] = "failed"
        # tell the user, wherever they are (hub home + top of JupyterLab)
        what = "Saving the environment" if task["kind"] == "commit" else "The download"
        if task["status"] == "done":
            detail = task["result"].get("image") or task["result"].get("archive")
            text = "{} '{}' is finished: {}".format(what, task["server"] or "default", detail)
        else:
            text = "{} '{}' FAILED: {}".format(what, task["server"] or "default", task["log"][-1])
        ui.add_notification([task["user"]], text, level="info" if task["status"] == "done" else "danger",
                            sender="system")
        ui.refresh_user_branding(task["user"], SERVER_NAME, LOGO_FILE)
    threading.Thread(target=target, daemon=True).start()
    return task


def do_commit(task, user, server, name):
    container = find_env(user, server)
    if container is None:
        raise RuntimeError("environment not found")
    repo = repo_for(user, name)
    tag = next_tag(repo)
    _log(task, "Saving environment '{}' as {}:{} (the environment pauses for a moment)...".format(server, repo, tag))
    now = time.time()
    container.commit(repository=repo, tag=tag, author=user,
                     message="Saved from JupyDo environment {} of {}".format(server, user),
                     changes=["LABEL {}={}".format(LABEL_SAVED_BY, shlex.quote(user)),
                              "LABEL {}={}".format(LABEL_SAVED_NAME, shlex.quote(name)),
                              "LABEL {}={}".format(LABEL_SAVED_FROM, shlex.quote(server or "default")),
                              "LABEL {}={}".format(LABEL_SAVED_AT, int(now))])
    _log(task, "Saved: {}:{}. It is now in the image list when creating an environment.".format(repo, tag))
    return {"image": "{}:{}".format(repo, tag)}


RUN_SH = """#!/usr/bin/env bash
# Start this JupyDo environment with JupyterLab on Linux or macOS with Docker, without
# JupyterHub. The folder you run it from is mounted as /sharedFolder (WORKDIR=/path
# to choose another one).
# Usage: bash run.sh      then open the http://127.0.0.1:8888/lab?token=... link it prints.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${{WORKDIR:-$PWD}}"
echo "Loading the image (only the first time, it can take a while)..."
docker load -i "$HERE/image.tar"
docker run --rm -it -p 8888:8888 \\
  -e NB_USER="$(id -un | tr -c 'a-zA-Z0-9_\\n-' '_')" -e NB_UID="$(id -u)" -e NB_GID=100 \\
  -v "$WORK":/sharedFolder \\
  {image} \\
  /opt/python4Jup/bin/jupyter lab --ip=0.0.0.0 --port=8888 --no-browser \\
      --ServerApp.root_dir=/ --ContentsManager.preferred_dir=sharedFolder
"""

RUN_APPTAINER = """#!/usr/bin/env bash
# Start this JupyDo environment with JupyterLab using Apptainer (or Singularity), on
# Linux or WSL, without Docker and without root: e.g. on an HPC cluster, or on a computer
# where Docker is not available. The folder you run it from is mounted as /sharedFolder
# (WORKDIR=/path to choose another one), as with run.sh.
# Usage: bash start_apptainer.sh      then open the link it prints in the box.
#
# The first time it converts image.tar into environment.sif (once, it can take a while).
# What you install or change in your home is kept in apptainer-data/, next to this file.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(cd "${WORKDIR:-$PWD}" && pwd)"
SIF="$HERE/environment.sif"
DATA="$HERE/apptainer-data"
PORT=8888

die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "start_apptainer.sh works on Linux and WSL only (on macOS use run.sh with Docker)."
if command -v apptainer >/dev/null 2>&1; then
    RUNNER=apptainer
elif command -v singularity >/dev/null 2>&1; then
    RUNNER=singularity
else
    die "neither apptainer nor singularity is installed (https://apptainer.org/docs/admin/main/installation.html),
       or use run.sh with Docker."
fi

# 1. The environment as an Apptainer image, converted once from the Docker archive
if [ ! -f "$SIF" ]; then
    [ -f "$HERE/image.tar" ] || die "image.tar is missing in $HERE (it is needed for the first start)."
    echo "Converting image.tar into environment.sif with $RUNNER."
    echo "This happens only the first time and can take a while (several minutes for big environments)..."
    "$RUNNER" build "$SIF.partial" "docker-archive://$HERE/image.tar"
    mv "$SIF.partial" "$SIF"
    echo "Done: environment.sif (image.tar is no longer needed by this script)."
fi

# 2. Port 8888, as with run.sh
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    die "port $PORT is already in use on this computer (another JupyterLab, or run.sh?): stop it, then run this again."
fi

# 3. The image is read-only: everything that must be written goes to apptainer-data/
#    home        your home (/home/jovyan): settings, packages you install, Jupyter's files
#    rstudio     RStudio Server's state (/var/lib/rstudio-server)
#    run, tmp    /run (/var/run) and the temporary folders
mkdir -p "$DATA/home" "$DATA/rstudio" "$DATA/run" "$DATA/tmp"
TOKEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \\n')"

banner() {
    local line="=================================================================================="
    printf '\\n%s\\n' "$line"
    printf '  JupyterLab is ready. Open this link in your browser:\\n\\n'
    printf '      http://127.0.0.1:%s/lab?token=%s\\n\\n' "$PORT" "$TOKEN"
    printf '  From another computer, replace 127.0.0.1 with this computer'"'"'s name or address\\n'
    printf '  (%s). Your folder %s is /sharedFolder.\\n' "$(hostname)" "$WORK"
    printf '  Stop it with Ctrl-C.\\n'
    printf '%s\\n\\n' "$line"
}
# the box is printed once JupyterLab answers, so it is not lost among its messages
( for _ in $(seq 1 600); do
      sleep 1
      if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then sleep 2; banner; exit 0; fi
  done ) &

# --containall: nothing of this computer (environment, home, /tmp) enters the container
exec "$RUNNER" exec --containall \\
    --home "$DATA/home:/home/jovyan" \\
    --bind "$WORK:/sharedFolder" \\
    --bind "$DATA/rstudio:/var/lib/rstudio-server" \\
    --bind "$DATA/run:/run" \\
    --bind "$DATA/tmp:/tmp" \\
    --bind "$DATA/tmp:/var/tmp" \\
    --env JUPYDO_TOKEN="$TOKEN" --env USER="$(id -un)" --env LOGNAME="$(id -un)" --env SHELL=/bin/bash \\
    --pwd /sharedFolder \\
    "$SIF" /bin/sh -c '
        export LD_LIBRARY_PATH=/usr/local/lib/R/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
        exec /opt/python4Jup/bin/jupyter lab --ip=0.0.0.0 --port=8888 --no-browser \\
            --ServerApp.root_dir=/ --ContentsManager.preferred_dir=sharedFolder \\
            --IdentityProvider.token="$JUPYDO_TOKEN"'
"""

# Windows: plain cmd (no PowerShell, no execution policy, nothing to unblock).
# Double-click it, or run "run.cmd" in a terminal. Written with CRLF line endings.
RUN_CMD = """@echo off
rem Start this JupyDo environment with JupyterLab on Windows with Docker Desktop,
rem without JupyterHub. The folder containing this file is mounted as /sharedFolder
rem (set WORKDIR=C:\\some\\folder before running it to choose another one).
rem Usage: double-click run.cmd, then open the http://127.0.0.1:8888/lab?token=... link it prints.
setlocal
cd /d "%~dp0"
set "WORK=%~dp0"
set "WORK=%WORK:~0,-1%"
if defined WORKDIR set "WORK=%WORKDIR%"
where docker >nul 2>nul
if errorlevel 1 (
  echo Docker was not found: install Docker Desktop, start it, then run this again.
  pause
  exit /b 1
)
echo Loading the image (only the first time, it can take a while)...
docker load -i "%~dp0image.tar"
if errorlevel 1 (
  echo Could not load the image: is Docker Desktop running?
  pause
  exit /b 1
)
docker run --rm -it -p 8888:8888 -e NB_USER=jovyan -e NB_UID=1000 -e NB_GID=100 ^
  -v "%WORK%":/sharedFolder ^
  {image} ^
  /opt/python4Jup/bin/jupyter lab --ip=0.0.0.0 --port=8888 --no-browser ^
  --ServerApp.root_dir=/ --ContentsManager.preferred_dir=sharedFolder
pause
"""

README = """JupyDo environment export
=========================
Environment: {server} of {user}, exported from {site} on {date}.
Image: {image}

Unzip this file anywhere, then, on a computer with Docker:
    Linux / macOS:  bash run.sh
    Windows:        double-click run.cmd  (Docker Desktop must be running)
and open the link it prints (http://127.0.0.1:8888/lab?token=...).
The first start loads the image into Docker (once; image.tar can then be deleted).
Your folder is mounted as /sharedFolder: on Linux/macOS the folder you start it
from (WORKDIR=/path to choose another), on Windows the folder of run.cmd.

The image contains the whole software environment, including what was installed
by hand. Your data (/sharedFolder) is NOT inside: copy it separately.

Without Docker: Apptainer / Singularity (Linux and WSL)
-------------------------------------------------------
Where Docker is not available or you have no root (e.g. an HPC cluster), but Apptainer
or Singularity is installed:
    bash start_apptainer.sh
and open the link printed in the box (http://127.0.0.1:8888/lab?token=...).
The first start converts image.tar into environment.sif (once, it can take a while,
and needs free space in the temporary folder: set APPTAINER_TMPDIR to a bigger one if
it fails). Your folder is mounted as /sharedFolder, as above.
Limits compared with Docker:
- no root: sudo and apt install do not work. pip install and install.packages() work
  and are kept in apptainer-data/home, next to this file (apptainer-data/ holds all
  that the environment writes outside /sharedFolder; delete it to start clean);
- everything else in the image is read-only: changes there are not kept;
- it always uses port 8888: stop any other JupyterLab on it first;
- it only uses image.tar from this download (no other Singularity images).
"""


def do_export(task, user, server):
    """One zip per export in /sharedFolder/exports/: image.tar (the environment as it
    is now, compressed by the zip), run.sh, start_apptainer.sh, run.cmd and README.txt."""
    container = find_env(user, server)
    if container is None:
        raise RuntimeError("environment not found")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = "{}-{}".format(safe_part(server or "default"), stamp)
    image_ref = "jupydo-export/{}:{}".format(safe_part(user), base)
    out_dir = os.path.join(SHARED_ROOT, user + "_shared", "exports")
    os.makedirs(out_dir, exist_ok=True)
    archive = os.path.join(out_dir, base + ".zip")
    client = docker.from_env()
    _log(task, "Capturing the current state of '{}' (the environment pauses for a moment)...".format(server))
    container.commit(repository=image_ref.split(":")[0], tag=image_ref.split(":")[1])
    try:
        image = client.images.get(image_ref)
        _log(task, "Writing {} ({:.1f} GB before compression, this takes a while)...".format(
            os.path.basename(archive), (image.attrs.get("Size") or 0) / GB))
        info = dict(server=server or "default", user=user, site=SERVER_NAME,
                    date=time.strftime("%Y-%m-%d %H:%M"), image=image_ref)
        with zipfile.ZipFile(archive + ".partial", "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=1, allowZip64=True) as z:
            for name, text, mode in (("run.sh", RUN_SH.format(**info), 0o755),
                                     ("start_apptainer.sh", RUN_APPTAINER, 0o755),
                                     ("run.cmd", RUN_CMD.format(**info).replace("\n", "\r\n"), 0o644),
                                     ("README.txt", README.format(**info), 0o644)):
                entry = zipfile.ZipInfo(base + "/" + name, date_time=time.localtime()[:6])
                entry.external_attr = (0o100000 | mode) << 16   # Unix permissions (run.sh executable)
                entry.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(entry, text)
            entry = zipfile.ZipInfo(base + "/image.tar", date_time=time.localtime()[:6])
            entry.external_attr = (0o100000 | 0o644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            written = 0
            with z.open(entry, "w", force_zip64=True) as f:
                for chunk in image.save(named=True):
                    f.write(chunk)
                    written += len(chunk)
                    if written // GB != (written - len(chunk)) // GB:
                        _log(task, "  {} GB written".format(written // GB))
        os.replace(archive + ".partial", archive)
    finally:
        try:
            client.images.remove(image_ref, noprune=False)
        except docker.errors.APIError:
            pass
        if os.path.exists(archive + ".partial"):
            os.remove(archive + ".partial")
    uid = user_uid(user)
    if uid is not None:  # the file belongs to the user, who can move or delete it
        for path in (out_dir, archive):
            os.chown(path, uid, 100)
    size = os.path.getsize(archive) / GB
    _log(task, "Done: /sharedFolder/exports/{} ({:.1f} GB): unzip it, then run.sh (Linux/macOS) "
               "or run.cmd (Windows).".format(os.path.basename(archive), size))
    return {"archive": "/sharedFolder/exports/" + os.path.basename(archive), "size_gb": round(size, 2)}


# ---------------------------------------------------------------------------
# Requests above the size limit (approved by an admin)
# ---------------------------------------------------------------------------
def load_requests():
    try:
        with open(REQUESTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return []


def _save_requests(reqs):
    tmp = REQUESTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(reqs, f, indent=1)
    os.replace(tmp, REQUESTS_FILE)


def add_request(kind, user, server, name, size_gb):
    with _lock:
        reqs = load_requests()
        req = {"id": uuid.uuid4().hex[:12], "kind": kind, "user": user, "server": server, "name": name,
               "size_gb": round(size_gb, 1), "created": time.time(), "status": "pending"}
        reqs.append(req)
        _save_requests(reqs)
    if ADMINS:
        what = "save as image '{}'".format(name) if kind == "commit" else "download"
        ui.add_notification(ADMINS, "{} asks to {} environment '{}' ({:.0f} GB, above the {:.0f} GB limit): "
                            "approve or reject it in Services > environments.".format(
                                user, what, server or "default", size_gb, SIZE_LIMIT_GB),
                            level="warning", sender="system")
        for admin in ADMINS:
            ui.refresh_user_branding(admin, SERVER_NAME, LOGO_FILE)
    return req


def decide_request(req_id, approve, admin):
    """Approve (runs the operation) or reject a pending request; tells the user."""
    with _lock:
        reqs = load_requests()
        req = next((r for r in reqs if r["id"] == req_id and r["status"] == "pending"), None)
        if req is None:
            return None, None
        req["status"] = "approved" if approve else "rejected"
        req["decided_by"] = admin
        req["decided_at"] = time.time()
        _save_requests(reqs)
    task = None
    if approve:
        if req["kind"] == "commit":
            task = run_task(new_task("commit", req["user"], req["server"]), do_commit,
                            req["user"], req["server"], req["name"])
        else:
            task = run_task(new_task("export", req["user"], req["server"]), do_export,
                            req["user"], req["server"])
    what = "saving '{}'".format(req["name"]) if req["kind"] == "commit" else "the download"
    ui.add_notification([req["user"]], "Your request for {} of environment '{}' was {} by {}.{}".format(
        what, req["server"] or "default", "APPROVED" if approve else "rejected", admin,
        " It is running now." if approve else ""), level="info", sender=admin)
    ui.refresh_user_branding(req["user"], SERVER_NAME, LOGO_FILE)
    return req, task


def start(kind, user, server, name=None):
    """Start a commit/export, or turn it into a request if the environment is too big.
    Returns ("task", task) or ("request", request)."""
    container = find_env(user, server)
    if container is None:
        raise ValueError("environment not found")
    if kind == "commit" and not NAME_RE.match(name or ""):
        raise ValueError("the name must be lowercase letters, digits, '.', '_' or '-' (max 41 chars)")
    size = container_size_gb(container)
    if size > SIZE_LIMIT_GB:
        return "request", add_request(kind, user, server, name, size)
    task = new_task(kind, user, server)
    _log(task, "Environment size: {:.1f} GB (limit {:.0f} GB).".format(size, SIZE_LIMIT_GB))
    if kind == "commit":
        return "task", run_task(task, do_commit, user, server, name)
    return "task", run_task(task, do_export, user, server)
