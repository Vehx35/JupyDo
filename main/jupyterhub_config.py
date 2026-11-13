# Configuration file for JupyterHub
import asyncio
import html
import json
import os
from docker.types import DeviceRequest
import re
import secrets
import sys
from dockerspawner import DockerSpawner
import nativeauthenticator

sys.path.insert(0, '/opt/jupydo')
import docker
import jupydo_builder  # builds the vendorized JupyDo version of the chosen image
import jupydo_ui  # notifications, JupyterLab branding and notification bar
import jupydo_images_lib  # environments saved with Commit


def jupydo_linux_user(spawner):
    """Linux account name inside the container: the hub username, made valid."""
    name = re.sub(r'[^a-z0-9_-]', '_', spawner.user.name.lower())
    if not re.match(r'^[a-z_]', name):
        name = 'u' + name
    return name[:32]


def jupydo_uid(spawner):
    """Unique, stable UID per hub user: traceable from the host too."""
    return 10000 + spawner.user.id

def copy_examples_once(spawner, folder):
    """Copy /opt/jupydo/<folder> (example notebooks) into the user's shared folder,
    once: if the user deletes or edits them, they are not overwritten."""
    uid = jupydo_uid(spawner)
    target = f"/srv/jh_shared/{spawner.user.name}_shared/{folder}"
    if not os.path.exists(target) and os.path.isdir(f'/opt/jupydo/{folder}'):
        shutil.copytree(f'/opt/jupydo/{folder}', target)
        for root, dirs, files in os.walk(target):
            os.chown(root, uid, 100)
            for name in files:
                os.chown(os.path.join(root, name), uid, 100)


def create_shared_dir(spawner):
    ## This hook is called when a user starts their server.
    ## It creates a shared directory for the user and sets the appropriate permissions.
    username = spawner.user.name
    uid = jupydo_uid(spawner)
    hub_shared_dir = f"/srv/jh_shared/{username}_shared"
    os.makedirs(hub_shared_dir, exist_ok=True)
    os.chown(hub_shared_dir, uid, 100)  # owned by the user's own UID
    os.chmod(hub_shared_dir, 0o775) # Set permissions to rwxrwxr-x

    # Job queue examples (Python and R notebooks), copied once into the user's
    # folder: if the user deletes or edits them, they are not overwritten.
    copy_examples_once(spawner, "jobqueue_example")
    # Self-test notebook: checks that an environment works (adapts to what it has)
    copy_examples_once(spawner, "jupydo_selftest")

    # UID -> user map, readable from the host (JupyDo data folder): who is UID 10007?
    map_file = '/etc/jupyterhub/users_uid_map.tsv'
    line = f"{uid}\t{username}\t{jupydo_linux_user(spawner)}\n"
    existing = open(map_file).read() if os.path.exists(map_file) else "uid\thub_user\tlinux_user\n"
    if line not in existing:
        with open(map_file, 'w') as f:
            f.write(existing + line)


# Define the custom spawner class
class JupyDoSpawner(DockerSpawner):

    JUPYDO_IMAGES = {
        "jupyter/r-notebook": "R Studio & Notebook",
        "jupyter/tensorflow-notebook": "TensorFlow",
        "jupyter/datascience-notebook": "Data Science (Python, R, Julia)",
        "jupyter/all-spark-notebook": "Apache Spark",
    }

    async def get_options_form(self):
        # The user already has a container: no form, it is started as it is.
        # The form appears only for new users or after the container was removed.
        if await self.get_object():
            return ""
        # An environment with a GPU request starts with the choices made when it was
        # requested (they cannot be changed); to change them, delete the environment
        if jupydo_ui.gpu_status(self.user.name, self.name) in ("pending", "approved"):
            return ""

        current = self.user_options.get("image", "")
        try:  # environments saved by users with Commit (shared with everyone)
            saved = await asyncio.get_running_loop().run_in_executor(None, jupydo_images_lib.saved_images)
        except Exception:
            self.log.exception("Cannot list saved images")
            saved = []
        saved_refs = {i["image"] for i in saved}
        is_custom = bool(current) and current not in self.JUPYDO_IMAGES and current not in saved_refs
        custom_display = "block" if is_custom else "none"
        custom_value = html.escape(current) if is_custom else ""

        options_html = ""

        for image, label in self.JUPYDO_IMAGES.items():
            selected = " selected" if image == current else ""
            options_html += f'<option value="{image}"{selected}>{label}</option>\n'

        is_saved = current in saved_refs
        if saved:  # one entry: user and environment are chosen below (with search)
            selected = " selected" if is_saved else ""
            options_html += f'<option value="__saved__"{selected}>Saved environments (Commit)...</option>\n'
        saved_display = "block" if is_saved else "none"
        saved_json = json.dumps([{k: i[k] for k in ("image", "saved_by", "name", "from", "size_gb", "saved_at")}
                                 for i in saved]).replace("</", "<\\/")

        selected = " selected" if is_custom else ""
        options_html += f'<option value="__custom__"{selected}>Custom Image</option>\n'
        apptainer_checked = " checked" if "apptainer" in (self.user_options.get("modules") or []) else ""
        rstudio_checked = " checked" if "rstudio" in (self.user_options.get("modules") or []) else ""
        vscode_checked = " checked" if "vscode" in (self.user_options.get("modules") or []) else ""
        desktop_checked = " checked" if "desktop" in (self.user_options.get("modules") or []) else ""
        igv_checked = " checked" if "igv" in (self.user_options.get("modules") or []) else ""
        cpu_options = "".join(
            f'<option value="{v:g}">{v:g} CPU{" (default)" if v == DEFAULT_CPUS else ""}</option>'
            for v in resource_choices()["cpus"])
        mem_options = "".join(
            f'<option value="{v:g}">{v:g} GB RAM{" (default)" if v == DEFAULT_MEM_GB else ""}</option>'
            for v in resource_choices()["mem_gb"])

        return f"""
        <div class="form-group">
            <label for="stack">Select your desired environment:</label>

            <select
                name="stack"
                id="stack"
                class="form-select"
                onchange="toggleCustomImage(this.value)"
            >
                {options_html}
            </select>
        </div>

        <div
            id="custom-image-group"
            class="form-group"
            style="margin-top:15px; display:{custom_display};"
        >
            <label for="custom_image">Docker image:</label>

            <input
                type="text"
                name="custom_image"
                id="custom_image"
                class="form-control"
                placeholder="myrepo/myimage:tag"
                value="{custom_value}"
            >
        </div>
""" + (ASSISTANT_FORM if ASSISTANT_ENABLED else "") + f"""

        <div id="saved-group" class="form-group" style="margin-top:15px; display:{saved_display};">
            <label for="saved-search">Saved environment:</label>
            <input type="text" id="saved-search" class="form-control"
                   placeholder="Search a user or an environment..." autocomplete="off">
            <div class="row g-2" style="margin-top:6px;">
                <div class="col-md-4">
                    <select id="saved-user" class="form-select" size="6" aria-label="user"></select>
                </div>
                <div class="col-md-8">
                    <select id="saved-env" name="saved_image" class="form-select" size="6"
                            aria-label="saved environment"></select>
                </div>
            </div>
            <div id="saved-empty" class="form-text" style="display:none;">Nothing matches the search.</div>
        </div>

        <div class="form-group" style="margin-top:15px;">
            <label>Resources:</label>
            <div class="form-text">Every environment starts with {DEFAULT_CPUS:g} CPU and {DEFAULT_MEM_GB:g} GB RAM.
                Need more? Choose here: the environment starts anyway, the admins get a request and,
                once they approve it, the extra resources are applied without restarting.</div>
            <div class="row g-2" style="margin-top:4px;">
                <div class="col"><select name="req_cpus" class="form-select" aria-label="CPU">{cpu_options}</select></div>
                <div class="col"><select name="req_mem_gb" class="form-select" aria-label="RAM">{mem_options}</select></div>
            </div>
            <input type="text" name="req_note" class="form-control" style="margin-top:6px;"
                   placeholder="Why (optional, for the admins): e.g. STAR index of the human genome">
        </div>

        <div class="form-group" style="margin-top:15px;">
            <label>GPU:</label>
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="req_gpu" id="req_gpu" value="1">
                <label class="form-check-label" for="req_gpu">
                    Request a GPU. The environment does not start until an admin approves it and
                    assigns a GPU: you will get a notification, then start it from the home page.
                </label>
            </div>
            <input type="text" name="req_gpu_note" class="form-control" style="margin-top:6px;"
                   placeholder="When and what for (optional, for the admins): e.g. this week, model training">
        </div>

        <div class="form-group" style="margin-top:15px;">
            <label>Optional modules:</label>
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="module_apptainer"
                       id="module_apptainer" value="1"{apptainer_checked}>
                <label class="form-check-label" for="module_apptainer">
                    Apptainer (run Apptainer/Singularity containers inside this environment)
                </label>
            </div>
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="module_rstudio"
                       id="module_rstudio" value="1"{rstudio_checked}>
                <label class="form-check-label" for="module_rstudio">
                    RStudio (opens from the JupyterLab launcher, with this environment's R)
                </label>
            </div>
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="module_vscode"
                       id="module_vscode" value="1"{vscode_checked}>
                <label class="form-check-label" for="module_vscode">
                    VS Code (opens from the JupyterLab launcher, with the Python and R extensions)
                </label>
            </div>
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="module_desktop"
                       id="module_desktop" value="1"{desktop_checked}>
                <label class="form-check-label" for="module_desktop">
                    Desktop (a Linux desktop in the browser, for programs with a graphical interface)
                </label>
            </div>
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="module_igv"
                       id="module_igv" value="1"{igv_checked}>
                <label class="form-check-label" for="module_igv">
                    IGV (genome browser in a JupyterLab tab, reads BAM/VCF files of your shared folder)
                </label>
            </div>
        </div>

        <script>
            const JUPYDO_SAVED = {saved_json};
            const JUPYDO_CURRENT = {json.dumps(current)};
            function toggleCustomImage(value) {{
                const group = document.getElementById("custom-image-group");
                const input = document.getElementById("custom_image");

                if (value === "__custom__") {{
                    group.style.display = "block";
                    input.required = true;
                }} else {{
                    group.style.display = "none";
                    input.required = false;
                    input.value = "";
                }}
                const saved = document.getElementById("saved-group");
                if (saved) {{
                    saved.style.display = value === "__saved__" ? "block" : "none";
                    document.getElementById("saved-env").required = value === "__saved__";
                }}
            }}
        </script>
        """ + SAVED_PICKER_JS

    def options_from_form(self, formdata):
        selected_stack = formdata.get("stack", [""])[0].strip()
        custom_image = formdata.get("custom_image", [""])[0].strip()

        if selected_stack == "__custom__":
            if not custom_image:
                raise ValueError("Please provide a Docker image.")
            image = custom_image

        elif selected_stack == "__saved__":
            image = formdata.get("saved_image", [""])[0].strip()
            if not image:
                raise ValueError("Please choose a saved environment.")

        elif selected_stack:
            image = selected_stack

        else:
            raise ValueError("Please select an environment.")

        modules = [m for m in ("apptainer", "rstudio", "vscode", "desktop", "igv") if formdata.get(f"module_{m}")]
        options = {"image": image, "modules": modules}
        if formdata.get("req_gpu"):
            options["gpu_request"] = {"note": formdata.get("req_gpu_note", [""])[0].strip()[:500]}
        # more CPU/RAM than the defaults: asked to the admins once the environment exists
        try:
            cpus = float(formdata.get("req_cpus", [DEFAULT_CPUS])[0])
            mem_gb = float(formdata.get("req_mem_gb", [DEFAULT_MEM_GB])[0])
        except (TypeError, ValueError):
            cpus, mem_gb = DEFAULT_CPUS, DEFAULT_MEM_GB
        if cpus > DEFAULT_CPUS or mem_gb > DEFAULT_MEM_GB:
            options["resource_request"] = {"cpus": max(cpus, DEFAULT_CPUS), "mem_gb": max(mem_gb, DEFAULT_MEM_GB),
                                           "note": formdata.get("req_note", [""])[0].strip()[:500]}
        return options

    async def start(self):
        """Start the chosen image only if it is JupyDo-ready; otherwise build its
        vendorized version first (progress shown on the spawn page)."""
        if not self.name:
            # JupyDo has no default (unnamed) server: every environment has a name
            raise RuntimeError("Every environment needs a name: go back to the home page "
                               "and add one with a name.")
        status = jupydo_ui.account_status(self.user.name)
        if status and status["expired"] and not self.user.admin:
            raise RuntimeError(f"Your JupyDo account expired on {status['until']}: "
                               "ask an administrator to renew it.")
        ui_host_dir = write_environment_branding(self)  # server name + logo in JupyterLab
        obj = await self.get_object()
        if obj:
            # Existing container: restart it exactly as it is, no check, no rebuild.
            self._build_events, self._build_done = [], True
            original = self.user_options
            self.user_options = dict(original, image=obj["Config"]["Image"])
            try:
                return await super().start()
            finally:
                self.user_options = original

        # GPU: an environment created with "Request a GPU" starts only once an admin
        # approved it and chose the GPU; then it gets that GPU only
        saved = jupydo_ui.gpu_request_options(self.user.name, self.name)
        if saved is not None:  # a GPU request is open: its saved choices, as they were
            self.user_options = saved
        wants_gpu = bool(self.user_options.get("gpu_request"))
        gpu = jupydo_ui.gpu_for(self.user.name, self.name) if wants_gpu else None
        if wants_gpu and gpu is None:
            # an HTTPError, so that JupyterHub shows the message (not "Unhandled error")
            if jupydo_ui.gpu_status(self.user.name, self.name) == "rejected":
                raise _web.HTTPError(409, f"The GPU request of environment '{self.name}' was rejected: start it "
                                          "without the GPU box ticked, or ask the admins.")
            jupydo_ui.add_gpu_request(self.user.name, self.name, self.user_options["gpu_request"].get("note", ""),
                                      jupydo_admins, options=dict(self.user_options))  # once per environment
            raise _web.HTTPError(409, f"Environment '{self.name}' waits for an admin to approve its GPU request. "
                                      "When it is approved the environment starts by itself and you get a "
                                      "notification: then open it from the home page.")

        self._build_events, self._build_done = [], False
        requested = await self.check_allowed(self.user_options.get("image") or self.image)
        last = {"progress": 1}

        def emit(message, progress=None):
            if progress is not None:
                last["progress"] = progress
            self._build_events.append({"progress": last["progress"], "message": message})

        try:
            await self.pull_any_platform(requested, emit)
            modules = self.user_options.get("modules") or []
            final = await asyncio.get_running_loop().run_in_executor(
                None, jupydo_builder.ensure_jupydo_image, requested, emit, modules)
            # Docker options the image's modules need (from its label: also images saved
            # with Commit from an environment with a module). Only for this new container.
            module_options = await asyncio.get_running_loop().run_in_executor(
                None, jupydo_builder.module_host_config, final)
        finally:
            self._build_done = True
        if not hasattr(self, "_base_host_config"):
            self._base_host_config = dict(self.extra_host_config)
        self.extra_host_config = dict(self._base_host_config, **module_options)
        if gpu is not None:  # the GPU an admin assigned to this environment
            self.extra_host_config["device_requests"] = [
                DeviceRequest(driver="nvidia", device_ids=[gpu], capabilities=[["gpu"]])]
            self.environment = dict(self.environment, NVIDIA_VISIBLE_DEVICES=gpu)
        # Apptainer tutorial, copied once into the user's folder with the first
        # environment that has the Apptainer module
        if module_options:
            copy_examples_once(self, "apptainer_example")

        original = self.user_options
        self.user_options = dict(original, image=final)
        # New container: also mount this environment's JupyterLab branding (custom.css + logo)
        self.volumes = dict(self.volumes, **{
            ui_host_dir: {'bind': '/home/jovyan/.jupyter/custom', 'mode': 'ro'}})
        try:
            result = await super().start()
        finally:
            self.user_options = original
        # more CPU/RAM asked in the form: the environment runs with the defaults, the
        # admins get the request (applied live from the environments page)
        request = original.get("resource_request")
        if request:
            jupydo_ui.add_resource_request(self.user.name, self.name, request["cpus"], request["mem_gb"],
                                           request.get("note", ""), jupydo_admins)
        return result

    async def pull_any_platform(self, image, emit):
        """Pull the image as it is published. If it has no variant for this machine's
        architecture (e.g. an arm64-only image on an amd64 server), pull the variant it
        does have, the other architecture: it then runs under QEMU emulation."""
        try:
            await self.pull_image(image)
            return
        except Exception as e:
            if "no matching manifest" not in str(e):
                raise
            first_error = e
        host = (await self.docker("info")).get("Architecture", "x86_64")
        other = "linux/amd64" if host in ("aarch64", "arm64") else "linux/arm64"
        repo, tag = docker.utils.parse_repository_tag(image)
        try:
            await self.docker("pull", repo, tag=tag or "latest", platform=other)
        except docker.errors.APIError:
            raise first_error  # no variant for either architecture
        emit(f"{image} exists only for {other}: it will run under emulation (much slower).", 2)
        self.log.info("Pulled %s for %s (emulated)", image, other)

    async def delete_forever(self):
        """Deleting an environment from the home page also removes its container
        (the user's files stay: the work volume is shared by all environments)."""
        jupydo_ui.withdraw_gpu_request(self.user.name, self.name)  # its GPU request goes with it
        obj = await self.get_object()
        if obj:
            self.log.info("Removing container %s of deleted environment", self.object_name)
            self.object_id = obj["Id"]
            await self.remove_object()

    async def progress(self):
        sent = 0
        while True:
            events = getattr(self, "_build_events", [])
            while sent < len(events):
                yield events[sent]
                sent += 1
            if getattr(self, "_build_done", True):
                break
            await asyncio.sleep(1)

# Basic JupyterHub configuration
c = get_config()  # noqa: F821

# Resources of a new environment, and what a user may ask for more (approved by an admin)
DEFAULT_CPUS = 4
DEFAULT_MEM_GB = 16


def resource_choices():
    """CPU and RAM values offered in the forms: from the defaults up to this machine."""
    host_cpus = os.cpu_count() or DEFAULT_CPUS
    try:
        with open('/proc/meminfo') as f:
            host_mem_gb = int(f.readline().split()[1]) / 1024 ** 2
    except (OSError, ValueError, IndexError):
        host_mem_gb = DEFAULT_MEM_GB
    cpus = [v for v in (4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256) if DEFAULT_CPUS <= v <= host_cpus]
    mem = [v for v in (16, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024) if DEFAULT_MEM_GB <= v <= host_mem_gb]
    return {"cpus": cpus or [DEFAULT_CPUS], "mem_gb": mem or [DEFAULT_MEM_GB],
            "default_cpus": DEFAULT_CPUS, "default_mem_gb": DEFAULT_MEM_GB}

# JupyDo Assistant in the spawn form: a link that opens the assistant in a panel over the
# form; when the user picks an image there, the assistant sends it here (postMessage,
# same origin only), and the form switches to "Custom Image" with that image filled in.
ASSISTANT_FORM = """
        <div class="form-group" style="margin-top:10px;">
            <a href="#" onclick="jupydoAssistant(true); return false;">
                Not sure what Docker is? Ask our assistant</a>
        </div>
        <div id="assistant-panel" style="display:none; position:fixed; inset:0; z-index:1050;
             background:rgba(0,0,0,.45);" onclick="if (event.target === this) jupydoAssistant(false);">
            <div style="position:absolute; top:4vh; left:50%; transform:translateX(-50%);
                 width:min(760px, 96vw); height:90vh; background:var(--bs-body-bg, #fff);
                 border-radius:10px; overflow:hidden; box-shadow:0 10px 40px rgba(0,0,0,.35);
                 display:flex; flex-direction:column;">
                <div style="display:flex; align-items:center; padding:6px 10px 6px 16px;
                     border-bottom:1px solid var(--bs-border-color, #ddd);">
                    <span class="small text-body-secondary">JupyDo Assistant</span>
                    <button type="button" class="btn-close ms-auto" aria-label="Close"
                            onclick="jupydoAssistant(false)"></button>
                </div>
                <iframe id="assistant-frame" title="JupyDo Assistant" style="flex:1; width:100%; border:0;"></iframe>
            </div>
        </div>
        <script>
            function jupydoAssistant(open) {
                const panel = document.getElementById("assistant-panel");
                const frame = document.getElementById("assistant-frame");
                if (open && !frame.src) frame.src = "/services/assistant/";
                if (!open) frame.removeAttribute("src");   // closing the panel ends the conversation
                panel.style.display = open ? "block" : "none";
            }
            window.addEventListener("message", function (event) {
                if (event.origin !== location.origin || !event.data || event.data.type !== "jupydo-assistant-image") return;
                const stack = document.getElementById("stack");
                stack.value = "__custom__";
                toggleCustomImage("__custom__");
                document.getElementById("custom_image").value = String(event.data.image || "");
                jupydoAssistant(false);
            });
        </script>
"""


SAVED_PICKER_JS = """
<script>
(function () {  // saved environments: user -> environment, with a search on both
  const search = document.getElementById("saved-search");
  const users = document.getElementById("saved-user");
  const envs = document.getElementById("saved-env");
  const empty = document.getElementById("saved-empty");
  if (!search || typeof JUPYDO_SAVED === "undefined") return;
  const esc = t => { const d = document.createElement("div"); d.textContent = String(t); return d.innerHTML; };
  const label = s => {
    const tag = s.image.split("/").slice(1).join("/");
    const date = s.saved_at ? new Date(s.saved_at * 1000).toISOString().slice(0, 10) : "";
    return (s.name || tag) + "  -  " + tag + (s.from ? ", from '" + s.from + "'" : "") +
           ", " + s.size_gb + " GB" + (date ? ", " + date : "");
  };
  const matches = (s, q) => !q || s.saved_by.toLowerCase().includes(q) ||
    (s.name || "").toLowerCase().includes(q) || s.image.toLowerCase().includes(q);
  function renderEnvs() {
    const q = search.value.trim().toLowerCase();
    const keep = envs.value;
    const list = JUPYDO_SAVED.filter(s => s.saved_by === users.value && matches(s, q));
    envs.innerHTML = list.map(s => '<option value="' + esc(s.image) + '">' + esc(label(s)) + "</option>").join("");
    if (list.some(s => s.image === keep)) envs.value = keep; else if (list.length) envs.selectedIndex = 0;
  }
  function renderUsers() {
    const q = search.value.trim().toLowerCase();
    const keep = users.value;
    const names = [...new Set(JUPYDO_SAVED.filter(s => matches(s, q)).map(s => s.saved_by))].sort();
    users.innerHTML = names.map(n => '<option value="' + esc(n) + '">' + esc(n) + " (" +
      JUPYDO_SAVED.filter(s => s.saved_by === n && matches(s, q)).length + ")</option>").join("");
    if (names.includes(keep)) users.value = keep; else if (names.length) users.selectedIndex = 0;
    empty.style.display = names.length ? "none" : "";
    renderEnvs();
  }
  search.addEventListener("input", renderUsers);
  users.addEventListener("change", renderEnvs);
  renderUsers();
  const current = JUPYDO_SAVED.find(s => s.image === JUPYDO_CURRENT);  // the choice of last time
  if (current) { users.value = current.saved_by; renderEnvs(); envs.value = current.image; }
  const stack = document.getElementById("stack");
  if (stack) envs.required = stack.value === "__saved__";
})();
</script>
"""


def check_hub_state_dir(path='/etc/jupyterhub', min_free_gb=1.0):
    """Stop with a clear message if the hub state folder (database, cookie secret,
    queue state) is not writable, instead of failing later with HTTP 500 errors.
    The write test is what matters: on a mergerfs pool the free space reported for
    the folder is the pool's total, even when the branch holding it is full."""
    probe = os.path.join(path, '.jupydo_write_test')
    try:
        with open(probe, 'w') as f:
            f.write('ok')
            f.flush()
            os.fsync(f.fileno())
        os.remove(probe)
    except OSError as e:
        raise SystemExit(
            f"JupyDo: cannot write in the hub state folder {path} ({e.strerror}). "
            "The hub database and cookie secret live there. Check, on the host: free space "
            "and inodes of the disk that physically holds it (on mergerfs: its branch), "
            "and permissions. Run main/jupydo-doctor.sh for a full check.")
    st = os.statvfs(path)
    free_gb = st.f_bavail * st.f_frsize / 1024 ** 3
    if free_gb < min_free_gb or (st.f_files and st.f_favail < 0.02 * st.f_files):
        print(f"WARNING JupyDo: hub state folder {path} is almost full "
              f"({free_gb:.1f} GB free, {st.f_favail} inodes free)")


check_hub_state_dir()

# JupyDo templates first (home page with environments only), then NativeAuthenticator's
c.JupyterHub.template_paths = ['/opt/jupydo/templates',
                               f"{os.path.dirname(nativeauthenticator.__file__)}/templates/"]

# After login go to the environments list, instead of starting a default server
c.JupyterHub.default_url = '/hub/home'

c.DockerSpawner.extra_host_config = {
}

c.JupyterHub.bind_url = 'http://:8000'
c.JupyterHub.hub_ip = '0.0.0.0'

# Use the custom spawner
c.JupyterHub.spawner_class = JupyDoSpawner
c.DockerSpawner.allowed_images = "*"

# Increase the spawner start timeout (default is 60 seconds)
c.Spawner.start_timeout = 1800  # long: the first start of an image may build its JupyDo version

# DockerSpawner configuration
c.DockerSpawner.network_name = 'jupyterhub_network'
c.JupyterHub.hub_connect_ip = 'jupyterhub'  # Use the container name of the JupyterHub instance

# Notebook directory and volumes
shared_dir = '/sharedFolder'  # the user's files (on the host, shared by all their environments)
# The file browser starts from / so users see real, full paths; it opens in
# /sharedFolder, the only place where users can write without sudo.
c.DockerSpawner.notebook_dir = '/'
c.Spawner.args = ['--ContentsManager.preferred_dir=' + shared_dir.lstrip('/'),
                 '--LabApp.custom_css=True']  # branding: logo + names in the top bar

# Default limits of every new environment. Admins change them per environment,
# live, from Services -> jobqueue -> Environments. Heavy work goes through the queue.
c.DockerSpawner.cpu_limit = DEFAULT_CPUS
c.DockerSpawner.mem_limit = f'{DEFAULT_MEM_GB}G'

base_path = os.environ.get('JUPYDO_PATH', '/srv/JupyDo')

# %%job magic: the hub copies it to the shared folder at startup and mounts it
# read-only in every user container, so it works with any image (custom too).
import shutil
os.makedirs('/srv/jh_shared/.jobqueue', exist_ok=True)
os.chmod('/srv/jh_shared/.jobqueue', 0o755)
for _f in ('jobqueue_magic.py', 'jobqueue_cli.py', 'ipython_kernel_config.py', 'Rprofile.site'):
    shutil.copy(f'/opt/jobqueue/{_f}', '/srv/jh_shared/.jobqueue/')
    os.chmod(f'/srv/jh_shared/.jobqueue/{_f}', 0o644)  # readable by jovyan, whatever the source mode

# Branding: server name (asked by first_start.sh) and logo, shown in the hub and in
# the JupyterLab top bar of every environment ("<server> · <environment>").
# The logo is branding/logo.png of the repository (mounted at /etc/jupydo-branding);
# logo.png in the hub state folder is still read, for installations made before
server_display_name = os.environ.get('JUPYDO_SERVER_NAME', 'JupyDo')
logo_file = next((f for f in ('/etc/jupydo-branding/logo.png', '/etc/jupyterhub/logo.png')
                  if os.path.isfile(f)), '/opt/genome-index/app/static/img/jupydo.png')
c.JupyterHub.logo_file = logo_file
c.JupyterHub.template_vars = {
    'jupydo_server_name': server_display_name,
    'jupydo_native_auth': os.environ.get('JUPYDO_AUTH', 'native') != 'github',
    'jupydo_2fa_required': os.environ.get('JUPYDO_2FA_REQUIRED', 'no') == 'yes',
    # validity of the logged-in user's account (hub home) and of everyone (Authorize Users page)
    'jupydo_account': lambda user: jupydo_ui.account_status(user.name) if user else None,
    'jupydo_expiry': lambda user: jupydo_ui.load_expiry() if user and user.admin else {},
    # unread notifications of the logged-in user, shown on the home page
    'jupydo_notifications': lambda user: jupydo_ui.notifications_for(user.name) if user else [],
    # CPU/RAM values for the "More resources" request on the home page
    'jupydo_resources': lambda user: resource_choices(),
    # the current terms of use (terms.pdf in the state folder) not accepted yet
    'jupydo_terms_pending': lambda user: bool(user) and not jupydo_ui.terms_accepted(user.name),
}

# JupyterLab settings for every environment: full path in the file browser, natural
# sort order (file2 before file10), no update/news checks and no extension manager
# (users must not update or add JupyterLab parts: the stack comes from the builder)
ui_dir = '/srv/jh_shared/.jupydo-ui'
os.makedirs(f'{ui_dir}/env', exist_ok=True)
os.chmod(ui_dir, 0o755)
os.chmod(f'{ui_dir}/env', 0o755)
with open(f'{ui_dir}/overrides.json', 'w') as _f:
    json.dump({
        '@jupyterlab/filebrowser-extension:browser': {
            'showFullPath': True, 'sortFileNamesNaturally': True},
        '@jupyterlab/apputils-extension:notification': {
            'checkForUpdates': False, 'fetchNews': 'false'},
        '@jupyterlab/extensionmanager-extension:plugin': {'enabled': False},
        # dark by default (the user can still change it; the hub follows, and back)
        '@jupyterlab/apputils-extension:themes': {'theme': 'JupyterLab Dark'},
    }, _f, indent=2)
os.chmod(f'{ui_dir}/overrides.json', 0o644)

# JupyterLab extension showing notifications at the top of JupyterLab (live, every 30 s)
shutil.copytree('/opt/jupydo/labextension/jupydo-notifications',
                f'{ui_dir}/labextension/jupydo-notifications', dirs_exist_ok=True)
for _root, _dirs, _files in os.walk(f'{ui_dir}/labextension'):
    os.chmod(_root, 0o755)
    for _name in _files:
        os.chmod(os.path.join(_root, _name), 0o644)

def write_environment_branding(spawner):
    """custom.css + logo of one environment (server logo, "<server> · <environment>",
    notification bar); returns the folder as seen by the host, to mount it."""
    folder = jupydo_ui.write_branding(spawner.user.name, spawner.name, server_display_name, logo_file)
    return f'{base_path}/jh_shared/' + os.path.relpath(folder, '/srv/jh_shared')

c.DockerSpawner.volumes = {
    # The user's shared folder. rslave: disks mounted on the host inside it later
    # (e.g. jh_shared/<user>_shared/hdd) appear in the running environments and jobs,
    # without restarting them (mount propagation, host -> container only)
    f'{base_path}/jh_shared/{{username}}_shared': {'bind': '/sharedFolder', 'mode': 'rw,rslave'},
    f'{base_path}/jh_shared/.jobqueue': {'bind': '/opt/jobqueue', 'mode': 'ro'},
    f'{base_path}/jh_shared/.jobqueue/ipython_kernel_config.py': {
        'bind': '/usr/local/etc/ipython/ipython_kernel_config.py', 'mode': 'ro'},
    f'{base_path}/jh_shared/.genome_index/indexes': {
        'bind': '/sharedFolder/genome_indexes', 'mode': 'ro'},  # Genome Index Service output
    f'{base_path}/jh_shared/.jupydo-ui/labextension/jupydo-notifications': {  # notifications bar
        'bind': '/usr/local/share/jupyter/labextensions/jupydo-notifications', 'mode': 'ro'},
    f'{base_path}/jh_shared/.jupydo-ui/overrides.json': {  # JupyterLab defaults (full path, natural sort)
        'bind': '/opt/python4Jup/share/jupyter/lab/settings/overrides.d/jupydo.json', 'mode': 'ro'},
}

c.DockerSpawner.extra_create_kwargs = {'user': 'root'}
from tornado import web as _web


def jupydo_pre_spawn(spawner):
    # no environment starts before the current terms of use are accepted
    if not jupydo_ui.terms_accepted(spawner.user.name):
        raise _web.HTTPError(403, 'Accept the terms of use first (hub home page)')
    create_shared_dir(spawner)


c.DockerSpawner.pre_spawn_hook = jupydo_pre_spawn

c.DockerSpawner.environment = {
    'GRANT_SUDO': '1',
    'NOPASSWD': 'yes',    # Allow users to execute sudo commands without a password
    'CHOWN_HOME': 'yes', # Assicura che jovyan possieda la sua home anche se avviato come root
    'NB_USER': jupydo_linux_user,  # personal account in the container (see jupydo-entrypoint)
    'NB_UID': lambda spawner: str(jupydo_uid(spawner)),
    'NB_GID': '100',
    'JOBQUEUE_URL': 'http://jupyterhub:8000/services/jobqueue',  # used by the %%job magic
    'R_PROFILE': '/opt/jobqueue/Rprofile.site',  # %%job magic for the R kernel
    'JOBQUEUE_DIR': '/sharedFolder/.jobs',  # job scripts and logs, visible to the job containers
    'NVIDIA_VISIBLE_DEVICES': 'void',  # no GPU in interactive environments: GPUs go to queue jobs
}

# Cell log (only if the administrator enabled it in first_start.sh: JUPYDO_CELL_LOG=yes):
# every cell users run in JupyterLab (notebooks and consoles, any language) is recorded,
# one file per user in JupyDo/logs/cells, readable only by root. The Jupyter Server of
# each environment sends the cells (jupydo_celllog_server.py, read-only); the hub service
# "celllog" writes them, under the name of the owner of the server's token. Containers
# keep the mounts they were created with: environments created before enabling it are
# logged only once they are recreated.
JUPYDO_CELL_LOG = os.environ.get('JUPYDO_CELL_LOG', 'no') == 'yes'
if JUPYDO_CELL_LOG:
    os.makedirs('/srv/jh_shared/.celllog', exist_ok=True)
    os.chmod('/srv/jh_shared/.celllog', 0o755)
    shutil.copy('/opt/jupydo/jupydo_celllog_server.py', '/srv/jh_shared/.celllog/jupydo_celllog_server.py')
    shutil.copy('/opt/jupydo/jupydo_celllog_config.py', '/srv/jh_shared/.celllog/jupyter_server_config.py')
    for _f in ('jupydo_celllog_server.py', 'jupyter_server_config.py'):
        os.chmod(f'/srv/jh_shared/.celllog/{_f}', 0o644)
    c.DockerSpawner.volumes[f'{base_path}/jh_shared/.celllog'] = {'bind': '/opt/jupydo-celllog', 'mode': 'ro'}
    c.DockerSpawner.environment.update({
        'JUPYTER_CONFIG_PATH': '/opt/jupydo-celllog',  # makes the module importable
        'JUPYDO_CELLLOG_URL': 'http://jupyterhub:8000/services/celllog/api/cells',
    })
    # on the command line: it takes precedence over any configuration file of the user
    c.Spawner.args.append('--ServerApp.kernel_websocket_connection_class='
                          'jupydo_celllog_server.CellLogConnection')

# =============================================================================
#                           Batch job queue (%%job magic)
# =============================================================================
# Managed service: runs cells as independent containers cloned from the user's
# server, with limits from a dedicated batch pool. The pool is the whole machine
# minus a small system reserve, minus the limits of the interactive environments
# running at each moment (they are kept for their users).

# Shared secret for services that submit containers to the queue (Genome Index
# Service). New at every hub start, known only to the hub's own services.
jobq_internal_token = secrets.token_hex(32)
genome_index_host_dir = f'{base_path}/jh_shared/.genome_index'  # host path of its data

# JupyDo Assistant (github.com/ReproducibleBioinformatics/JupyDo-Assistant): helps users
# who do not know Docker choose an image. Its model is downloaded by first_start.sh into
# jh_shared/.assistant; without it the assistant is off and its link is not shown.
ASSISTANT_MODEL_FILE = 'qwen2.5-1.5b-instruct-q4_k_m.gguf'
ASSISTANT_ENABLED = (os.path.isfile(f'/srv/jh_shared/.assistant/{ASSISTANT_MODEL_FILE}')
                     and os.path.isfile('/opt/assistant/assistant/service.py'))

# Admins (JUPYDO_ADMIN in .env, comma separated), also told about requests by the services
jupydo_admins = {a.strip() for a in os.environ.get('JUPYDO_ADMIN', 'limo').split(',') if a.strip()}

c.JupyterHub.services = [{
    'name': 'jobqueue',
    'oauth_no_confirm': True,  # shown inside a hub page: no confirmation page
    'url': 'http://127.0.0.1:10101',
    'command': ['python3', '/opt/jobqueue/jobqueue_service.py'],
    'environment': {
        'JOBQ_GPUS': 'auto',      # every NVIDIA GPU of the host (or a list, e.g. '2,3')
        'JOBQ_CPUS': 'auto',      # host CPUs minus the reserve below (or a maximum, e.g. '32')
        'JOBQ_MEM_GB': 'auto',    # host RAM minus the reserve below (or a maximum, e.g. '128')
        'JOBQ_SYSTEM_RESERVE_CPUS': '2',   # always left to the system and the hub
        'JOBQ_SYSTEM_RESERVE_MEM_GB': '4',
        'JOBQ_MAX_WALLTIME_MIN': '1440',
        'JOBQ_KILL_GRACE_S': '60',
        'JOBQ_INTERNAL_TOKEN': jobq_internal_token,
        'JOBQ_INTERNAL_IMAGE_PREFIX': 'gis-tool-',        # only Genome Index tool images
        'JOBQ_INTERNAL_BIND_ROOT': genome_index_host_dir,  # only its data folder
    },
}, {
    # Environments (admins only, Services menu): every user environment with its
    # CPU/RAM limits and the user's disk usage and limit.
    'name': 'environments',
    'oauth_no_confirm': True,  # shown inside a hub page: no confirmation page
    'url': 'http://127.0.0.1:10103',
    'command': ['python3', '/opt/jupydo/jupydo_environments.py'],
    'environment': {
        'JUPYDO_SERVER_NAME': server_display_name,
        'JUPYDO_LOGO_FILE': logo_file,
        'JUPYDO_DISK_INTERVAL_S': '3600',
        'JUPYDO_ADMINS': ','.join(sorted(jupydo_admins)),
        'JUPYDO_IMAGE_LIMIT_GB': '50',
    },
}, {
    # Notifications (admins only, Services menu): messages to users, shown on the hub
    # home and live at the top of JupyterLab.
    'name': 'notifications',
    'oauth_no_confirm': True,  # shown inside a hub page: no confirmation page
    'url': 'http://127.0.0.1:10104',
    'command': ['python3', '/opt/jupydo/jupydo_notifications.py'],
    'environment': {
        'JUPYDO_SERVER_NAME': server_display_name,
        'JUPYDO_LOGO_FILE': logo_file,
    },
}, {
    # Images (all users): commit and download of the user's own environments.
    'name': 'images',
    'oauth_no_confirm': True,  # shown inside a hub page: no confirmation page
    'url': 'http://127.0.0.1:10105',
    'command': ['python3', '/opt/jupydo/jupydo_images.py'],
    'environment': {
        'JUPYDO_SERVER_NAME': server_display_name,
        'JUPYDO_LOGO_FILE': logo_file,
        'JUPYDO_ADMINS': ','.join(sorted(jupydo_admins)),
        'JUPYDO_IMAGE_LIMIT_GB': '50',  # above this, commit/download need an admin's approval
    },
}, {
    # Transfer (all users): fast, resumable upload and download of large files in the
    # user's own shared folder (/sharedFolder), instead of JupyterLab's drag and drop.
    'name': 'transfer',
    'oauth_no_confirm': True,  # shown inside a hub page: no confirmation page
    'url': 'http://127.0.0.1:10106',
    'command': ['python3', '/opt/jupydo/jupydo_transfer.py'],
    'environment': {
        'JUPYDO_TRANSFER_CHUNK_MB': '64',   # size of each upload piece (one HTTP request)
        'JUPYDO_TRANSFER_PART_DAYS': '7',   # unfinished uploads are deleted after this
    },
}, {
    # Bookings (all users): a shared calendar to agree on who uses what and when
    # (e.g. a GPU). Admins create the bookable resources; nothing is enforced.
    'name': 'bookings',
    'oauth_no_confirm': True,  # shown inside a hub page: no confirmation page
    'url': 'http://127.0.0.1:10107',
    'command': ['python3', '/opt/jupydo/jupydo_bookings.py'],
    'environment': {
        'JUPYDO_BOOKINGS_MAX_HOURS': '336',  # longest single booking (14 days)
        'JUPYDO_BOOKINGS_KEEP_DAYS': '90',   # past bookings kept this long
    },
}, {
    # Genome Index Service (github.com/ReproducibleBioinformatics/Index_builderGUI).
    # Hub admins get the admin interface, everyone else the user one.
    # Indexing containers go through the job queue above.
    'name': 'genome-index',
    'oauth_no_confirm': True,  # shown inside a hub page: no confirmation page
    'url': 'http://127.0.0.1:10102',
    'cwd': '/opt/genome-index/app',
    'command': ['sh', '-c', 'umask 000 && exec waitress-serve --host=127.0.0.1 --port=10102 '
                            '--url-prefix=/services/genome-index --threads=8 main:app'],
    'environment': {
        'APP_ROLE': 'hub',
        'DATA_DIR': '/srv/jh_shared/.genome_index',
        'DATA_HOST_BIND': genome_index_host_dir,
        'SEED_TOOLS_DIR': '/opt/genome-index/tools',
        'DOCKER_HOST': 'unix:///var/run/docker.sock',
        'TOOL_IMAGE_PREFIX': 'gis-tool',
        'JOBQUEUE_URL': 'http://127.0.0.1:10101/services/jobqueue',
        'JOBQUEUE_TOKEN': jobq_internal_token,
        'INDEX_THREADS': '8',
        'INDEX_MEM_GB': '32',             # memory requested per index (clamped to the pool)
        'INDEX_WALLTIME_MIN': '1440',
        'JOB_WORKERS': '8',
    },
}]
if JUPYDO_CELL_LOG:
    c.JupyterHub.services.append({
        # Cell log (see above): receives the cells from the environments, no page.
        'name': 'celllog',
        'url': 'http://127.0.0.1:10109',
        'command': ['python3', '/opt/jupydo/jupydo_celllog.py'],
        'environment': {'JUPYDO_CELLLOG_DIR': '/srv/jupydo-logs/cells'},
    })
if ASSISTANT_ENABLED:
    c.JupyterHub.services.append({
        # JupyDo Assistant (all users): opened in a panel over the spawn form. The service
        # is small and always on; its model (llama.cpp container on jupyterhub_network)
        # is started when someone opens the chat and stopped when the last one closes it.
        'name': 'assistant',
        'oauth_no_confirm': True,  # shown inside a hub page: no confirmation page
        'url': 'http://127.0.0.1:10108',
        'command': ['python3', '/opt/assistant/assistant/service.py'],
        'environment': {
            'ASSISTANT_CATALOG': '/opt/assistant/catalog.json',
            'ASSISTANT_MODEL_DIR': f'{base_path}/jh_shared/.assistant',  # host path of the model
            'ASSISTANT_MODEL_FILE': ASSISTANT_MODEL_FILE,
            'ASSISTANT_LLM_NETWORK': 'jupyterhub_network',
            'ASSISTANT_LLM_THREADS': '4',
            'DOCKER_HOST': 'unix:///var/run/docker.sock',
        },
    })

# Dismissing a notification is handled by the hub itself (users have no access to the
# admin services). JupyterHub 5 logs that extra_handlers is deprecated: it still works.
from jupyterhub.handlers import BaseHandler as _HubHandler
from tornado import web as _web


class DismissNotificationHandler(_HubHandler):
    @_web.authenticated
    def get(self, msg_id):
        jupydo_ui.mark_read(self.current_user.name, msg_id)
        jupydo_ui.refresh_user_branding(self.current_user.name, server_display_name, logo_file)
        self.redirect(self.hub.base_url + 'home')


class ServiceAppHandler(_HubHandler):
    """A service shown under the hub menu (the Services menu links here, see
    templates/page.html): the service itself is loaded unchanged, full size."""
    @_web.authenticated
    async def get(self, name):
        if name not in {s['name'] for s in c.JupyterHub.services}:
            raise _web.HTTPError(404)
        query = self.request.query
        html = await self.render_template('jupydo_app.html', app_name=name,
                                          app_url=f'/services/{name}/' + (f'?{query}' if query else ''))
        self.finish(html)


class AccountExpiryHandler(_HubHandler):
    """Authorize Users page: save until when a user's account is valid (empty = no expiry)."""
    @_web.authenticated
    def post(self, name):
        if not self.current_user.admin:
            raise _web.HTTPError(403)
        until = self.get_body_argument('valid_until', '').strip()
        try:
            jupydo_ui.set_valid_until(name, until or None)
        except ValueError:
            raise _web.HTTPError(400, 'valid_until must be a date (YYYY-MM-DD)')
        jupydo_ui.refresh_user_branding(name, server_display_name, logo_file)
        self.redirect(self.hub.base_url + 'authorize')


class ResourceRequestHandler(_HubHandler):
    """Home page: ask the admins for more CPU/RAM for one of the user's environments.
    The environment keeps running; an admin applies the request live."""
    @_web.authenticated
    def post(self, server):
        user = self.current_user
        if not server or server not in user.orm_spawners:  # every environment, also not loaded yet
            raise _web.HTTPError(404, 'environment not found')
        try:
            cpus = float(self.get_body_argument('cpus'))
            mem_gb = float(self.get_body_argument('mem_gb'))
        except (_web.MissingArgumentError, ValueError):
            raise _web.HTTPError(400, 'CPU and RAM must be numbers')
        note = self.get_body_argument('note', '').strip()[:500]
        jupydo_ui.add_resource_request(user.name, server, cpus, mem_gb, note, jupydo_admins)
        jupydo_ui.add_notification([user.name], "Request of {:g} CPU and {:g} GB RAM for '{}' sent to the "
                                   "admins: you will get a message when they decide.".format(cpus, mem_gb, server),
                                   in_lab=False, sender="JupyDo")
        self.redirect(self.hub.base_url + 'home')


class TermsPdfHandler(_HubHandler):
    """The terms of use PDF (also before login: it is linked from the sign-up page)."""
    def get(self):
        if not jupydo_ui.terms_sha256():
            raise _web.HTTPError(404, 'no terms of use on this server')
        self.set_header('Content-Type', 'application/pdf')
        self.set_header('Content-Disposition', 'inline; filename="terms-of-use.pdf"')
        with open(jupydo_ui.TERMS_FILE, 'rb') as f:
            self.finish(f.read())


class TermsHandler(_HubHandler):
    """Accept the current terms of use (first login with GitHub, users registered
    before the terms existed, and everyone after the PDF changes)."""
    def _email(self):
        get_user = getattr(self.authenticator, 'get_user', None)  # NativeAuthenticator only
        info = get_user(self.current_user.name) if get_user else None
        return (getattr(info, 'email', '') or '') if info else ''

    @_web.authenticated
    async def get(self, error=None):
        if jupydo_ui.terms_accepted(self.current_user.name):
            return self.redirect(self.hub.base_url + 'home')
        html = await self.render_template('terms.html', email=self._email(), error=error)
        self.finish(html)

    @_web.authenticated
    async def post(self):
        email = self.get_body_argument('email', '').strip()
        if not self.get_body_argument('accept', '') or '@' not in email:
            return await self.get(error='Write your email and tick the box to accept the terms of use.')
        jupydo_ui.record_terms(self.current_user.name, email, 'login', self.request.remote_ip)
        self.redirect(self.hub.base_url + 'home')


c.JupyterHub.extra_handlers = [
    (r'/jupydo/terms\.pdf', TermsPdfHandler),
    (r'/jupydo/terms', TermsHandler),
    (r'/jupydo/resources/([^/]+)', ResourceRequestHandler),
    (r'/jupydo/expiry/([^/]+)', AccountExpiryHandler),
    (r'/jupydo/notifications/([^/]+)/read', DismissNotificationHandler),
    (r'/jupydo/apps/([^/]+)', ServiceAppHandler),
]

# Every user can open the Genome Index Service, images, transfer, bookings and the assistant
# (the admin services stay admin-only).
c.JupyterHub.load_roles = [
    # the environments service stops the environments of expired accounts
    {'name': 'jupydo-environments', 'services': ['environments'],
     'scopes': ['read:users', 'admin:servers']},
    {'name': 'user', 'scopes': ['self', 'access:services!service=genome-index',
                               'access:services!service=images', 'access:services!service=transfer',
                               'access:services!service=bookings']
     + (['access:services!service=assistant'] if ASSISTANT_ENABLED else [])},
]

# Enable named servers (still in the tests)
c.JupyterHub.allow_named_servers = True  # Allow users to create multiple named servers


# How many environments a user may have: 2 unless an admin sets another number for
# that user (environments page). JupyterHub calls this with the request's handler.
def named_server_limit(handler):
    user = handler.current_user
    return jupydo_ui.server_limit_for(user.name) if user else jupydo_ui.DEFAULT_SERVER_LIMIT


c.JupyterHub.named_server_limit_per_user = named_server_limit

# Debugging
c.JupyterHub.log_level = 'DEBUG'

# =============================================================================
#                           Authenticator Configuration
# =============================================================================

# Two options, chosen in first_start.sh (JUPYDO_AUTH in .env):
#  - native (default): JupyDo accounts, admins authorize sign-ups, optional 2FA with an
#    authenticator app (Duo Mobile, Google Authenticator...), chosen at sign-up
#  - github: log in with GitHub, only members of the organization JUPYDO_GITHUB_ORG
if os.environ.get('JUPYDO_AUTH', 'native') == 'github':
    c.JupyterHub.authenticator_class = 'oauthenticator.github.GitHubOAuthenticator'
    c.GitHubOAuthenticator.client_id = os.environ['JUPYDO_GITHUB_CLIENT_ID']
    c.GitHubOAuthenticator.client_secret = os.environ['JUPYDO_GITHUB_CLIENT_SECRET']
    c.GitHubOAuthenticator.oauth_callback_url = (
        os.environ['JUPYDO_PUBLIC_URL'].rstrip('/') + '/hub/oauth_callback')
    c.GitHubOAuthenticator.allowed_organizations = {os.environ['JUPYDO_GITHUB_ORG']}
    c.GitHubOAuthenticator.scope = ['read:org']
    c.JupyterHub.template_paths = ['/opt/jupydo/templates']  # no NativeAuthenticator pages
else:
    from nativeauthenticator import NativeAuthenticator
    from nativeauthenticator.handlers import SignUpHandler

    TOS_HTML = ('I have read and accept the <a href="/hub/jupydo/terms.pdf" target="_blank">'
                'terms of use</a> of this server')

    class JupyDoSignUpHandler(SignUpHandler):
        """Sign-up with the terms of use: the box is checked again here (not only in the
        browser) and the acceptance is recorded with the version (SHA-256) of the PDF."""
        def _set_tos(self):
            self.authenticator.tos = TOS_HTML if jupydo_ui.terms_sha256() else ''  # '' = no box

        async def get(self):
            self._set_tos()
            await super().get()

        async def post(self):
            self._set_tos()
            name = self.authenticator.normalize_username(self.get_body_argument('username', '', strip=False))
            existed = self.authenticator.user_exists(name)
            if self.authenticator.tos and not self.get_body_argument('tos_check', ''):
                raise _web.HTTPError(400, 'You must accept the terms of use to sign up')
            await super().post()
            if self.authenticator.tos and not existed and self.authenticator.user_exists(name):
                jupydo_ui.record_terms(name, self.get_body_argument('email', '').strip(),
                                       'signup', self.request.remote_ip)

    class JupyDoNativeAuthenticator(NativeAuthenticator):
        def get_handlers(self, app):
            return [(path, JupyDoSignUpHandler if handler is SignUpHandler else handler)
                    for path, handler in super().get_handlers(app)]

    c.JupyterHub.authenticator_class = JupyDoNativeAuthenticator
    c.Authenticator.allow_all = True  # every authorized JupyDo account (never with GitHub!)
    c.NativeAuthenticator.ask_email_on_signup = True  # email required at sign-up
c.NativeAuthenticator.open_signup = False
c.NativeAuthenticator.create_system_users = True
c.NativeAuthenticator.allow_2fa = True


def jupydo_post_auth(authenticator, handler, auth_model):
    """Refuse the login of expired accounts (admins never expire) and, when 2FA is
    mandatory, of NativeAuthenticator accounts without 2FA."""
    name = auth_model['name']
    if name in jupydo_admins:
        return auth_model
    status = jupydo_ui.account_status(name)
    if status and status['expired']:
        authenticator.log.warning('Login refused: account of %s expired on %s', name, status['until'])
        return None
    if os.environ.get('JUPYDO_2FA_REQUIRED', 'no') == 'yes' and hasattr(authenticator, 'get_user'):
        info = authenticator.get_user(name)
        if info is not None and not info.has_2fa:
            authenticator.log.warning('Login refused: %s has no 2FA and 2FA is mandatory', name)
            return None
    return auth_model


c.Authenticator.post_auth_hook = jupydo_post_auth

# Allowed admins
# One or more admins, comma separated in .env (JUPYDO_ADMIN=luca,francesco):
# with two admins nobody stays locked out if one loses the admin rights.
c.Authenticator.admin_users = jupydo_admins

# Failed login attempts
# Set the maximum number of failed login attempts before locking out the user
#Default value is 0 (which means no limit on failed login attempts)
c.NativeAuthenticator.allowed_failed_logins = 0

# Set timer between failed login attempts
c.NativeAuthenticator.seconds_before_next_try = 0

# Password complexity
c.NativeAuthenticator.check_common_password = True
c.NativeAuthenticator.minimum_password_length = 7
