"""
JupyDo UI helpers shared by the hub config and the "environments" service.

- Notifications: messages from admins (or automatic, e.g. disk limit) to users,
  stored in the hub state folder. Users see them on the hub home page and as a
  bar at the top of JupyterLab, updated live by the jupydo-notifications
  JupyterLab extension (it reads notifications.json in each environment folder).
- Disk settings: per-user disk limits and the last measured usage.
- JupyterLab branding: custom.css + logo for each environment (server logo,
  "<server> · <environment>" in the top bar) and the notifications file.
- Account validity: until when each user is authorized (set by admins on the
  Authorize Users page); shown to the user on the hub home and in JupyterLab.
- Terms of use: the PDF each installation puts in the state folder (terms.pdf) and
  who accepted which version of it (terms_acceptances.jsonl, append-only).
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid

STATE_DIR = os.environ.get("JUPYDO_HUB_STATE_DIR", "/etc/jupyterhub")
NOTIFICATIONS_FILE = os.path.join(STATE_DIR, "notifications.json")
DISK_FILE = os.path.join(STATE_DIR, "disk_usage.json")
EXPIRY_FILE = os.path.join(STATE_DIR, "account_expiry.json")
RESOURCE_REQUESTS_FILE = os.path.join(STATE_DIR, "resource_requests.json")
GPU_REQUESTS_FILE = os.path.join(STATE_DIR, "gpu_requests.json")
SERVER_LIMITS_FILE = os.path.join(STATE_DIR, "server_limits.json")
DEFAULT_SERVER_LIMIT = 2   # environments a user may have, unless an admin changes it
EXPIRY_WARNING_DAYS = 7
BRANDING_DIR = os.environ.get("JUPYDO_BRANDING_DIR", "/etc/jupydo-branding")  # branding/ of the repository
TERMS_FILE = os.path.join(BRANDING_DIR, "terms.pdf")
TERMS_LOG = os.path.join(STATE_DIR, "terms_acceptances.jsonl")
UI_DIR = "/srv/jh_shared/.jupydo-ui"          # as seen inside the hub container
DEFAULT_DISK_LIMIT_GB = 200.0

_lock = threading.RLock()


# ---------------------------------------------------------------------------
# JSON files (atomic writes, tolerant reads)
# ---------------------------------------------------------------------------
def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def load_notifications():
    return _load(NOTIFICATIONS_FILE, {"messages": [], "read": {}})


def add_notification(to, text, level="info", in_lab=True, sender="admin", msg_id=None, link=None):
    """to = "*" (everyone) or a list of hub usernames. link: optional page the message
    points to (e.g. the environments page for a request). Returns the message."""
    with _lock:
        data = load_notifications()
        msg = {"id": msg_id or uuid.uuid4().hex[:12], "to": to, "text": text[:2000],
               "level": level if level in ("info", "warning", "danger") else "info",
               "in_lab": bool(in_lab), "from": sender, "created": time.time()}
        if link:
            msg["link"] = link
        data["messages"] = [m for m in data["messages"] if m["id"] != msg["id"]] + [msg]
        _save(NOTIFICATIONS_FILE, data)
        return msg


def remove_notification(msg_id):
    with _lock:
        data = load_notifications()
        data["messages"] = [m for m in data["messages"] if m["id"] != msg_id]
        for user, ids in data["read"].items():
            data["read"][user] = [i for i in ids if i != msg_id]
        _save(NOTIFICATIONS_FILE, data)


def mark_read(user, msg_id):
    with _lock:
        data = load_notifications()
        ids = data["read"].setdefault(user, [])
        if msg_id not in ids:
            ids.append(msg_id)
        _save(NOTIFICATIONS_FILE, data)


def notifications_for(user, unread_only=True):
    """Messages addressed to this user, newest first."""
    data = load_notifications()
    read = set(data["read"].get(user, []))
    msgs = [m for m in data["messages"]
            if (m["to"] == "*" or user in m["to"]) and not (unread_only and m["id"] in read)]
    return sorted(msgs, key=lambda m: m["created"], reverse=True)


# ---------------------------------------------------------------------------
# Requests for more CPU/RAM: the environment starts with the defaults, an admin
# applies the request later (live, from the environments page)
# ---------------------------------------------------------------------------
def load_resource_requests():
    return _load(RESOURCE_REQUESTS_FILE, [])


def add_resource_request(user, server, cpus, mem_gb, note, admins):
    """Record a request and notify the admins. A newer pending request for the same
    environment replaces the older one."""
    with _lock:
        everything = load_resource_requests()
        replaced = [r["id"] for r in everything
                    if r["status"] == "pending" and r["user"] == user and r["server"] == server]
        reqs = [r for r in everything if r["id"] not in replaced]
        req = {"id": uuid.uuid4().hex[:12], "user": user, "server": server, "cpus": cpus,
               "mem_gb": mem_gb, "note": (note or "")[:500], "status": "pending", "created": time.time()}
        reqs.append(req)
        _save(RESOURCE_REQUESTS_FILE, reqs[-500:])
    for old_id in replaced:
        remove_notification("res-" + old_id)
    if admins:
        add_notification(sorted(admins), "{} asks for {:g} CPU and {:g} GB RAM for environment '{}'{}".format(
            user, cpus, mem_gb, server, (": " + note) if note else ""), level="warning", sender=user,
            msg_id="res-" + req["id"], link="/hub/jupydo/apps/environments")
    return req


def decide_resource_request(req_id, approve, admin):
    """Mark a pending request approved/rejected, tell the user. Returns it (or None)."""
    with _lock:
        reqs = load_resource_requests()
        req = next((r for r in reqs if r["id"] == req_id and r["status"] == "pending"), None)
        if req is None:
            return None
        req.update(status="approved" if approve else "rejected", decided_by=admin, decided_at=time.time())
        _save(RESOURCE_REQUESTS_FILE, reqs)
    remove_notification("res-" + req_id)
    add_notification([req["user"]], "Your request of {:g} CPU and {:g} GB RAM for environment '{}' was {}{}".format(
        req["cpus"], req["mem_gb"], req["server"], "applied" if approve else "rejected",
        " (already active, no restart needed)." if approve else "."), level="info" if approve else "warning")
    return req


# ---------------------------------------------------------------------------
# How many environments each user may have (default DEFAULT_SERVER_LIMIT)
# ---------------------------------------------------------------------------
def load_server_limits():
    return _load(SERVER_LIMITS_FILE, {})


def server_limit_for(user):
    return int(load_server_limits().get(user, DEFAULT_SERVER_LIMIT))


def set_server_limit(user, limit):
    with _lock:
        limits = load_server_limits()
        if limit is None or int(limit) == DEFAULT_SERVER_LIMIT:
            limits.pop(user, None)
        else:
            limits[user] = int(limit)
        _save(SERVER_LIMITS_FILE, limits)


# ---------------------------------------------------------------------------
# Disk limits and usage
# ---------------------------------------------------------------------------
def load_disk():
    data = _load(DISK_FILE, {})
    data.setdefault("default_limit_gb", DEFAULT_DISK_LIMIT_GB)
    data.setdefault("limits", {})     # user -> GB
    data.setdefault("usage", {})      # user -> {"work_gb", "shared_gb", "measured_at"}
    return data


def save_disk(data):
    with _lock:
        _save(DISK_FILE, data)


def disk_limit_for(data, user):
    return float(data["limits"].get(user, data["default_limit_gb"]))


# ---------------------------------------------------------------------------
# Account validity: {user: {"until": "YYYY-MM-DD"}} or {user: {"expired_on": ...}}
# The account is valid through the whole "until" day.
# ---------------------------------------------------------------------------
def load_expiry():
    return _load(EXPIRY_FILE, {})


def set_valid_until(user, until):
    """until: "YYYY-MM-DD", or None/"" for no expiry."""
    if until:
        datetime.date.fromisoformat(until)  # raises ValueError if not a date
    with _lock:
        data = load_expiry()
        if until:
            data[user] = {"until": until}
        else:
            data.pop(user, None)
        _save(EXPIRY_FILE, data)


def mark_expired(user, until):
    with _lock:
        data = load_expiry()
        data[user] = {"expired_on": until}
        _save(EXPIRY_FILE, data)


def account_status(user, today=None):
    """{"until", "days_left", "expired"} for a user with an expiry date, else None."""
    info = load_expiry().get(user) or {}
    if not info.get("until"):
        return None
    today = today or datetime.date.today()
    left = (datetime.date.fromisoformat(info["until"]) - today).days
    return {"until": info["until"], "days_left": left, "expired": left < 0}


# ---------------------------------------------------------------------------
# JupyterLab branding + notification bar (custom.css, served by JupyterLab)
# ---------------------------------------------------------------------------
BRANDING_CSS = """/* JupyDo: server logo and environment name in the JupyterLab top bar */
#jp-MainLogo {{ background: url("logo.png") center / contain no-repeat; }}
#jp-MainLogo > * {{ visibility: hidden; }}
#jp-top-panel {{ position: relative; }}
#jp-top-panel::after {{
  content: "{label}";
  position: absolute; top: 0; left: 50%; transform: translateX(-50%);
  line-height: 28px; font-size: var(--jp-ui-font-size1); font-weight: 600;
  color: var(--jp-ui-font-color1); pointer-events: none; white-space: nowrap;
}}
"""



def _css_string(text):
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\A ")


def env_key(user, server):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{user}__{server or 'default'}")


def write_branding(user, server, server_display_name, logo_file):
    """Write custom.css + logo for one environment. Returns the folder inside the hub."""
    folder = os.path.join(UI_DIR, "env", env_key(user, server))
    os.makedirs(folder, exist_ok=True)
    os.chmod(folder, 0o755)
    if os.path.isfile(logo_file):
        shutil.copy(logo_file, os.path.join(folder, "logo.png"))
        os.chmod(os.path.join(folder, "logo.png"), 0o644)
    css = BRANDING_CSS.format(label=_css_string(f"{server_display_name} \u00b7 {server or user}"))
    _write_file(folder, "custom.css", css)
    # Unread notifications, read every 30 s by the JupyterLab extension (served by
    # JupyterLab at <base_url>/custom/notifications.json)
    msgs = [{"id": m["id"], "level": m["level"], "from": m["from"], "text": m["text"]}
            for m in notifications_for(user)]
    _write_file(folder, "notifications.json", json.dumps(msgs))
    # account validity, shown by the extension in the JupyterLab top bar
    _write_file(folder, "account.json", json.dumps(account_status(user) or {}))
    return folder


def _write_file(folder, name, content):
    tmp = os.path.join(folder, name + ".tmp")
    with open(tmp, "w") as f:
        f.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(folder, name))


def refresh_user_branding(user, server_display_name, logo_file):
    """Rewrite the files of every environment of a user (e.g. after a new message)."""
    prefix = env_key(user, "") .rsplit("__", 1)[0] + "__"
    env_dir = os.path.join(UI_DIR, "env")
    if not os.path.isdir(env_dir):
        return
    for name in os.listdir(env_dir):
        if name.startswith(prefix):
            server = name[len(prefix):]
            write_branding(user, "" if server == "default" else server, server_display_name, logo_file)


# ---------------------------------------------------------------------------
# Service pages (shown under the hub menu by the hub, see templates/jupydo_app.html)
# ---------------------------------------------------------------------------
THEME_SYNC = """<script>
(function () {  // same light/dark theme as the hub, also when it is toggled there
  const apply = () => document.documentElement.setAttribute("data-bs-theme",
    localStorage.getItem("jupyterhub-bs-theme") || "dark");  // JupyDo default: dark
  apply();
  window.addEventListener("storage", apply);
})();
</script>"""


def render_page(path, user=None):
    """Read a service page; <!--JUPYDO_NAVBAR--> becomes the theme sync (the hub menu
    is shown around the page by the hub itself)."""
    with open(path) as f:
        return f.read().replace("<!--JUPYDO_NAVBAR-->", THEME_SYNC)


# ---------------------------------------------------------------------------
# Terms of use
# Each installation puts its own PDF in branding/terms.pdf of the repository (no file =
# no terms, nothing is asked). A version is identified by the SHA-256 of the PDF:
# replacing the file is a new version, and every user must accept it again at the
# next visit of the hub home (environments cannot start before). Every acceptance
# is one line of terms_acceptances.jsonl: time (UTC), user, email, how (signup or
# login), IP address, SHA-256 of the accepted PDF. Lines are only ever added.
# ---------------------------------------------------------------------------
_terms_lock = threading.Lock()
_terms_cache = {"key": None, "sha256": None}
_terms_accepted = None  # {user: set of accepted SHA-256}, read once from TERMS_LOG


def terms_sha256():
    """SHA-256 of the current terms.pdf, or None when there are no terms."""
    try:
        st = os.stat(TERMS_FILE)
    except FileNotFoundError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    with _terms_lock:
        if _terms_cache["key"] != key:
            h = hashlib.sha256()
            with open(TERMS_FILE, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            _terms_cache.update(key=key, sha256=h.hexdigest())
        return _terms_cache["sha256"]


def _accepted():
    global _terms_accepted
    if _terms_accepted is None:
        _terms_accepted = {}
        try:
            with open(TERMS_LOG) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        _terms_accepted.setdefault(e["user"], set()).add(e["terms_sha256"])
                    except (ValueError, KeyError):
                        pass
        except FileNotFoundError:
            pass
    return _terms_accepted


def terms_accepted(user):
    """True if the user accepted the current terms (or there are no terms)."""
    sha = terms_sha256()
    if sha is None:
        return True
    with _terms_lock:
        return sha in _accepted().get(user, set())


def record_terms(user, email, how, ip):
    sha = terms_sha256()
    if sha is None:
        return
    entry = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
             "user": user, "email": email, "how": how, "ip": ip, "terms_sha256": sha}
    with _terms_lock:
        new = not os.path.exists(TERMS_LOG)
        with open(TERMS_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if new:
            os.chmod(TERMS_LOG, 0o600)
        _accepted().setdefault(user, set()).add(sha)


# ---------------------------------------------------------------------------
# Interactive GPU: an environment created with "Request a GPU" does not start
# until an admin approves it and picks the GPU; it then starts with that GPU only.
# No turns are enforced: users agree among themselves (e.g. with the Bookings
# calendar), and the job queue keeps using every GPU.
# ---------------------------------------------------------------------------
def detect_gpus():
    """NVIDIA GPUs of the host as [(index, model)], index as Docker and nvidia-smi
    use it (PCI bus order). /proc/driver/nvidia/gpus is visible in containers too."""
    base = "/proc/driver/nvidia/gpus"
    try:
        buses = sorted(os.listdir(base))
    except OSError:
        return []
    gpus = []
    for i, bus in enumerate(buses):
        model = "NVIDIA GPU"
        try:
            with open(os.path.join(base, bus, "information")) as f:
                for line in f:
                    if line.startswith("Model:"):
                        model = line.split(":", 1)[1].strip()
        except OSError:
            pass
        gpus.append((str(i), model))
    return gpus


def load_gpu_requests():
    return _load(GPU_REQUESTS_FILE, [])


def gpu_for(user, server):
    """The GPU approved for this environment (e.g. "0"), or None."""
    reqs = [r for r in load_gpu_requests() if r["user"] == user and r["server"] == server]
    last = max(reqs, key=lambda r: r["created"], default=None)
    return last["gpu"] if last and last["status"] == "approved" else None


def gpu_status(user, server):
    """Status of the latest GPU request of this environment (pending, approved,
    rejected, revoked), or None if it never asked for one."""
    reqs = [r for r in load_gpu_requests() if r["user"] == user and r["server"] == server]
    last = max(reqs, key=lambda r: r["created"], default=None)
    return last["status"] if last else None


def gpu_pending(user, server):
    return any(r["user"] == user and r["server"] == server and r["status"] == "pending"
               for r in load_gpu_requests())


def add_gpu_request(user, server, note, admins, options=None):
    """Record a GPU request (once per environment) and notify the admins. options:
    the user's choices (image, modules...), used to start the environment by itself
    when the request is approved."""
    with _lock:
        reqs = load_gpu_requests()
        if any(r["user"] == user and r["server"] == server and r["status"] == "pending" for r in reqs):
            return None
        req = {"id": uuid.uuid4().hex[:12], "user": user, "server": server, "note": (note or "")[:500],
               "status": "pending", "gpu": None, "created": time.time(),
               "options": json.loads(json.dumps(options or {}, default=str))}
        reqs.append(req)
        _save(GPU_REQUESTS_FILE, reqs[-500:])
    if admins:
        add_notification(sorted(admins), "{} asks for a GPU for environment '{}'{}".format(
            user, server, (": " + note) if note else ""), level="warning", sender=user,
            msg_id="gpu-" + req["id"], link="/hub/jupydo/apps/environments")
    return req


def gpu_request_options(user, server):
    """The choices (image, modules, GPU...) saved with the open (pending or approved)
    GPU request of this environment, or None: they are what the environment starts
    with, and cannot be changed any more (no form)."""
    reqs = [r for r in load_gpu_requests() if r["user"] == user and r["server"] == server
            and r["status"] in ("pending", "approved")]
    last = max(reqs, key=lambda r: r["created"], default=None)
    return dict(last["options"]) if last and last.get("options") else None


def withdraw_gpu_request(user, server):
    """The environment was deleted: its open GPU requests (pending or approved) are
    closed, so that a new environment with the same name starts from scratch."""
    with _lock:
        reqs = load_gpu_requests()
        gone = [r for r in reqs if r["user"] == user and r["server"] == server
                and r["status"] in ("pending", "approved")]
        for r in gone:
            r.update(status="closed (environment deleted)", decided_by=user, decided_at=time.time())
        if gone:
            _save(GPU_REQUESTS_FILE, reqs)
    for r in gone:
        remove_notification("gpu-" + r["id"])


def decide_gpu_request(req_id, action, gpu, admin):
    """action: approve (with gpu), reject, or revoke (an approved one). Tells the user."""
    with _lock:
        reqs = load_gpu_requests()
        req = next((r for r in reqs if r["id"] == req_id), None)
        if req is None or (action in ("approve", "reject") and req["status"] != "pending") \
                or (action == "revoke" and req["status"] != "approved"):
            return None
        req.update(status={"approve": "approved", "reject": "rejected", "revoke": "revoked"}[action],
                   gpu=gpu if action == "approve" else req.get("gpu"), decided_by=admin, decided_at=time.time())
        _save(GPU_REQUESTS_FILE, reqs)
    remove_notification("gpu-" + req_id)
    text = {"approve": "GPU {gpu} was approved for environment '{server}': it is starting now, "
                       "open it from the home page.",
            "reject": "Your GPU request for environment '{server}' was rejected.",
            "revoke": "GPU {gpu} was taken back from environment '{server}': the environment keeps it until "
                      "it is reset or deleted, then it starts without a GPU."}[action]
    add_notification([req["user"]], text.format(gpu=req.get("gpu"), server=req["server"]),
                     level="info" if action == "approve" else "warning")
    return req
