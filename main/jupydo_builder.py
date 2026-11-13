"""
JupyDo image builder (hub side): make sure the image a user picked is JupyDo-ready.

Uses the jupydo-builder folder (mounted at BUILDER_DIR): Dockerfile, modules/,
lists/, scripts/ and vendor/ (the offline bundles). It does what
scripts/build-image.sh does by hand, with the same result: an image built by
hand and one built by the hub are the same and interchangeable.

Rule (strict): an image is ready only if it was built by the CURRENT builder for
its platform (label org.jupydo.builder == builder version of that platform) and
its stack check passes (/usr/local/bin/jupydo-check). Otherwise its JupyDo version
jupydo/<name>:<tag> is built FROM it (or reused, if already built from this exact
image with the current builder) and that image is started instead.
Only the bundles of the image's distribution/architecture are sent to Docker.

Optional modules (e.g. "apptainer") are added on top of the base: each one comes
from its own self-contained bundle (vendor/<module>/<platform>), the image gets
the suffix "-<module>" and the label org.jupydo.modules.
"""
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import threading

import docker

BUILDER_DIR = os.environ.get("JUPYDO_BUILDER_DIR", "/srv/jupydo-builder")
LABEL = "org.jupydo.builder"            # builder version the image was built with
LABEL_PLATFORM = "org.jupydo.platform"  # e.g. ubuntu-24.04-amd64
LABEL_BASE_ID = "org.jupydo.base_id"    # id of the image it was built from
LABEL_MODULES = "org.jupydo.modules"    # optional modules the image has, e.g. "apptainer"
LABEL_PYTHON = "org.jupydo.python"      # Python bundle it uses: python, or python-legacy (old glibc)
LABEL_KERNEL_LIBS = "org.jupydo.kernel_libs"  # kernel libraries it uses, e.g. "cp312 cp314"
CHECK = "/usr/local/bin/jupydo-check"

_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_versions = {}             # (platform key, modules, python bundle, kernel libs) -> builder version
_check_cache = {}          # image id -> (ok, output)
_locks = {}                # image id -> lock (one build per base image at a time)
_locks_guard = threading.Lock()


def builder_version(key, modules=(), python="python", kernel_libs=()):
    """Version of the stack for one platform, set of modules and Python bundle:
    computed by the builder's own script (scripts/lib/builder-version.sh), so it is
    the same as in scripts/build-image.sh."""
    modules = tuple(sorted(modules))
    kernel_libs = tuple(sorted(kernel_libs))
    if (key, modules, python, kernel_libs) not in _versions:
        if not os.path.isfile(os.path.join(BUILDER_DIR, "Dockerfile")):
            raise RuntimeError(f"JupyDo builder not found in {BUILDER_DIR}")
        arch = key.rsplit("-", 1)[-1]
        result = subprocess.run(["bash", os.path.join(BUILDER_DIR, "scripts/lib/builder-version.sh"), key, arch,
                                 *modules], capture_output=True, text=True,
                                env=dict(os.environ, PYTHON_BUNDLE=python, KERNEL_LIBS=" ".join(kernel_libs)))
        if result.returncode != 0:
            raise RuntimeError(f"no bundles for {key} {' '.join(modules)} in {BUILDER_DIR}/vendor: "
                               f"{result.stderr.strip()[-300:]}")
        _versions[(key, modules, python, kernel_libs)] = result.stdout.strip()
    return _versions[(key, modules, python, kernel_libs)]


def pick_python(arch, glibc):
    """Python bundle for an image with this glibc (scripts/lib/pick-python.sh, the same
    choice as scripts/build-image.sh): "python", or "python-legacy" for old distributions."""
    result = subprocess.run(["bash", os.path.join(BUILDER_DIR, "scripts/lib/pick-python.sh"), arch, glibc],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"no Python bundle for glibc {glibc}")
    return result.stdout.strip()


def image_modules(image):
    """Modules an image was built with (its label), e.g. ["apptainer"]."""
    return ((image.labels or {}).get(LABEL_MODULES) or "").split()


def _run(client, image, entrypoint, command):
    """Run a throwaway root container without network, return (exit code, output)."""
    c = client.containers.create(image, entrypoint=entrypoint, command=command,
                                 user="0", network_mode="none", labels={"org.jupydo.probe": "1"})
    try:
        c.start()
        code = c.wait(timeout=300)["StatusCode"]
        return code, c.logs().decode(errors="replace")
    finally:
        c.remove(force=True)


def _check(client, image, modules=()):
    """Strict readiness: built by the current builder for its platform and modules,
    with every requested module, and stack check passing."""
    labels = image.labels or {}
    key = labels.get(LABEL_PLATFORM)
    has = image_modules(image)
    missing = [m for m in modules if m not in has]
    if missing:
        return False, "without module(s) " + ", ".join(missing)
    try:
        current = builder_version(key, has, labels.get(LABEL_PYTHON) or "python",
                                  (labels.get(LABEL_KERNEL_LIBS) or "").split()) if key else None
    except RuntimeError:
        current = None
    if not current or labels.get(LABEL) != current:
        return False, "not built by the current JupyDo builder"
    if image.id not in _check_cache:
        try:
            code, out = _run(client, image.id, [CHECK], [])
        except docker.errors.APIError as e:
            code, out = 1, str(e)
        _check_cache[image.id] = (code == 0, out)
    return _check_cache[image.id]


def _detect_raw(client, image):
    """Output of the builder's modules/detect-image.sh run in the image."""
    with open(os.path.join(BUILDER_DIR, "modules", "detect-image.sh")) as f:
        script = f.read()
    try:
        return _run(client, image.id, ["/bin/sh", "-c"], [script])
    except docker.errors.APIError as e:
        raise RuntimeError(f"cannot inspect the image: {e}")


def _convert_minimal(client, name, image, emit):
    """A minimal image whose software is a conda environment in /usr/local, with no
    package manager (BioContainers on BusyBox): its /usr/local is moved onto the
    vendored Debian 12 base (Dockerfile.convert), as scripts/build-image.sh does.
    Returns the converted image's tag, or None if the image is not of this kind."""
    code, out = _detect_raw(client, image)
    line = next((l.split() for l in out.splitlines() if l.startswith("CONDA_ONLY ")), None)
    if code != 0 or not line or len(line) < 3:
        return None
    arch = line[2]
    with open(os.path.join(BUILDER_DIR, "lists", "convert-bases.txt")) as f:
        base = next(l.strip() for l in f if l.strip() and not l.lstrip().startswith("#"))
    key = f"{base.replace(':', '-')}-{arch}"
    bdir = os.path.join(BUILDER_DIR, "vendor", "bases", key)
    if not os.path.isfile(os.path.join(bdir, ".complete")):
        raise RuntimeError(f"minimal images with conda in /usr/local need the base bundle vendor/bases/{key} "
                           f"(scripts/make-vendor-bases.sh in the builder)")
    with open(os.path.join(bdir, "manifest.txt")) as f:
        base_tag = next(l.split()[1] for l in f if l.startswith("tag "))
    try:
        client.images.get(base_tag)
    except docker.errors.ImageNotFound:
        with open(os.path.join(bdir, "image.tar"), "rb") as f:
            client.images.load(f)
    emit(f"{name} is a minimal image (conda environment in /usr/local, no package manager): "
         f"moving its software onto {key}.", 4)
    with open(os.path.join(BUILDER_DIR, "Dockerfile.convert")) as f:
        dockerfile = f.read()
    for kv in image.attrs.get("Config", {}).get("Env") or []:
        k, _, v = kv.partition("=")
        v = v.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        dockerfile += f'ENV {k}="{v}"\n'
    dockerfile += f'LABEL org.jupydo.converted-from="{name}" org.jupydo.converted-base="{key}"\n'
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        raw = dockerfile.encode()
        info = tarfile.TarInfo("Dockerfile")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
    data.seek(0)
    repo = name.rsplit("@", 1)[0]
    tag = "jupydo-converted/" + (re.sub(r"[^a-z0-9_.-]+", "-", repo.lower()).strip("-.")[:80] or "image") + ":" + arch
    try:
        for chunk in client.api.build(fileobj=data, custom_context=True, tag=tag, rm=True, forcerm=True,
                                      decode=True, pull=False, platform=f"linux/{arch}",
                                      buildargs={"TOOL_IMAGE": name, "CONVERT_BASE": base_tag}):
            if "error" in chunk:
                raise RuntimeError(chunk["error"].strip())
    except Exception as e:
        raise RuntimeError(f"conversion of {name} failed: {e}")
    return tag


def _detect(client, image):
    """What the ORIGINAL image is, and which Pythons and Rs it has (the builder's
    modules/detect-image.sh, the same as scripts/build-image.sh). Returns
    (platform key e.g. 'ubuntu-24.04-amd64', glibc, detection text)."""
    code, out = _detect_raw(client, image)
    os_line = next((l.split() for l in out.splitlines() if l.startswith("OS ")), None)
    if code != 0 or not os_line or len(os_line) < 4 or os_line[3] == "unknown":
        raise RuntimeError("cannot detect distribution/architecture (no /bin/sh, /etc/os-release or dpkg?): "
                           "only Ubuntu/Debian based images are supported")
    _, distro, version, arch = os_line[:4]
    glibc = os_line[4] if len(os_line) > 4 else "0"
    detected = "\n".join(l for l in out.splitlines() if l.startswith(("OS ", "PY ", "R "))) + "\n"
    return f"{distro}-{version}-{arch}", glibc, detected


def _kernel_libs(arch, detected):
    """Kernel libraries available for the Python versions the image has (cp3XY)."""
    tags = set()
    for line in detected.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "PY":
            tag = "cp" + parts[2].replace(".", "")
            if os.path.isfile(os.path.join(BUILDER_DIR, "vendor", "kernel-libs", arch, tag, ".complete")):
                tags.add(tag)
    return sorted(tags)


def _empty_tar_gz():
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz"):
        pass
    return data.getvalue()


def _build_context(key, modules=(), python="python", detected="", kernel_libs=()):
    """Tar with Dockerfile, modules and only the bundles this platform needs (same
    layout as scripts/build-image.sh: selected/apt, selected/python, selected/r,
    selected/modules/<module>, selected/apptainer.tar.gz)."""
    vendor = os.path.join(BUILDER_DIR, "vendor")
    arch = key.rsplit("-", 1)[-1]
    apt_dir = os.path.join(vendor, "apt", key)
    python_tgz = os.path.join(vendor, python, arch, "python4jup.tar.gz")
    if not os.path.isfile(os.path.join(apt_dir, ".complete")):
        supported = sorted(d for d in os.listdir(os.path.join(vendor, "apt"))
                           if os.path.isfile(os.path.join(vendor, "apt", d, ".complete")))
        raise RuntimeError(f"{key} is not supported (no apt bundle). Supported: {', '.join(supported)}")
    if not os.path.isfile(python_tgz):
        raise RuntimeError(f"no Python bundle {python} for {arch}")
    f = tempfile.TemporaryFile()
    with tarfile.open(fileobj=f, mode="w") as tar:
        tar.add(os.path.join(BUILDER_DIR, "Dockerfile"), "Dockerfile")
        tar.add(os.path.join(BUILDER_DIR, "modules"), "modules")
        tar.add(apt_dir, "selected/apt")
        tar.add(python_tgz, "selected/python/python4jup.tar.gz")
        tar.add(os.path.join(vendor, "r"), "selected/r")
        # the Pythons and Rs of the original image, and the kernel libraries for them
        data = detected.encode()
        info = tarfile.TarInfo("selected/kernels.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        libs = io.BytesIO()
        with tarfile.open(fileobj=libs, mode="w:gz") as kl:
            for tag in kernel_libs:
                base = os.path.join(vendor, "kernel-libs", key.rsplit("-", 1)[-1], tag)
                kl.add(os.path.join(base, "site"), f"{tag}/site")
                kl.add(os.path.join(base, "manifest.txt"), f"{tag}/manifest.txt")
        info = tarfile.TarInfo("selected/kernel-libs.tar.gz")
        info.size = libs.tell()
        libs.seek(0)
        tar.addfile(info, libs)
        # optional modules: their packages, and Apptainer's program for ADD
        # (an empty archive when the module is off)
        info = tarfile.TarInfo("selected/modules")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        apptainer_tgz = None
        for module in modules:
            src = os.path.join(vendor, module, key)
            if not os.path.isfile(os.path.join(src, ".complete")):
                raise RuntimeError(f"module {module} is not available for {key}")
            tar.add(os.path.join(src, "debs"), f"selected/modules/{module}/debs")
            tar.add(os.path.join(src, "packages.txt"), f"selected/modules/{module}/packages.txt")
            if os.path.isdir(os.path.join(src, "wheels")):  # Python packages for JupyDo's Python
                tar.add(os.path.join(src, "wheels"), f"selected/modules/{module}/wheels")
            if os.path.isdir(os.path.join(src, "files")):  # large files of the module (e.g. code-server)
                tar.add(os.path.join(src, "files"), f"selected/modules/{module}/files")
            if module == "apptainer":
                apptainer_tgz = os.path.join(src, "apptainer.tar.gz")
        if apptainer_tgz:
            tar.add(apptainer_tgz, "selected/apptainer.tar.gz")
        else:
            empty = _empty_tar_gz()
            info = tarfile.TarInfo("selected/apptainer.tar.gz")
            info.size = len(empty)
            tar.addfile(info, io.BytesIO(empty))
    f.seek(0)
    return f


def _target_tag(name, modules=()):
    """jupyter/r-notebook:latest -> jupydo/jupyter-r-notebook:latest (the original name and tag),
    plus "-<module>" for each module: jupydo/jupyter-r-notebook:latest-apptainer.
    Whether it must be rebuilt is decided by its labels, not by its name."""
    repo, _, tag = name.rpartition(":") if ":" in name.rsplit("/", 1)[-1] else (name, "", "latest")
    slug = re.sub(r"[^a-z0-9_.-]+", "-", repo.lower()).strip("-.")[:80] or "image"
    tag = re.sub(r"[^A-Za-z0-9_.-]+", "-", tag)[:100] or "latest"
    for module in sorted(modules):
        tag += "-" + module
    return f"jupydo/{slug}:{tag}"


def _up_to_date(client, built, image, modules=()):
    """Built from this exact version of the image, with these modules, and ready with
    the current builder."""
    return ((built.labels or {}).get(LABEL_BASE_ID) == image.id
            and sorted(image_modules(built)) == sorted(modules) and _check(client, built, modules)[0])


def ensure_jupydo_image(name, emit, modules=()):
    """Return the image to start for `name` with these optional modules, building it
    if needed.

    emit(message, progress) reports progress (0-100) to the spawn page.
    Blocking: call it in a thread.
    """
    modules = sorted(set(modules))
    client = docker.from_env()
    image = client.images.get(name)
    if "apptainer" in modules:
        # Apptainer's starter cannot run under QEMU user-mode emulation: refuse clearly
        # instead of giving an environment where Apptainer does not work.
        host = _ARCH.get(client.info().get("Architecture", ""), "")
        own = _ARCH.get(image.attrs.get("Architecture", ""), image.attrs.get("Architecture", ""))
        if host and own and own != host:
            raise RuntimeError(
                f"Apptainer does not work in emulated environments: {name} is {own}, this server is {host}. "
                f"Use an image built for {host} with the Apptainer module (it can run {own} containers as "
                f"sandbox folders: 'apptainer build --arch {own} --fix-perms --sandbox <folder> docker://...'), "
                f"or start this image without Apptainer.")
    emit(f"Checking image {name}...", 2)
    ok, out = _check(client, image, modules)
    if ok:
        emit("Image is JupyDo-ready.", 10)
        return name

    with _locks_guard:
        lock = _locks.setdefault((image.id, tuple(modules)), threading.Lock())
    with lock:  # a second user starting the same image waits for the first build
        tag = _target_tag(name, modules)
        try:
            if _up_to_date(client, client.images.get(tag), image, modules):
                emit("Using the JupyDo version already built for this image.", 10)
                return tag
            emit("The JupyDo version of this image is outdated (new builder or new base image): "
                 "rebuilding it.", 4)
        except docker.errors.ImageNotFound:
            pass

        # BusyBox + conda images (BioContainers) are converted first, then built as usual
        converted = _convert_minimal(client, name, image, emit)
        build_name = converted or name
        key, glibc, detected = _detect(client, client.images.get(build_name))
        arch = key.rsplit("-", 1)[-1]
        python = pick_python(arch, glibc)
        kernel_libs = _kernel_libs(arch, detected)
        context = _build_context(key, modules, python, detected, kernel_libs)  # clear message if not supported
        version = builder_version(key, modules, python, kernel_libs)
        with_modules = f" with {', '.join(modules)}" if modules else ""
        emit(f"Building the JupyDo version of {name}{with_modules} ({key}). This happens once per image, "
             f"it can take several minutes.", 5)
        tail = []
        try:
            for chunk in client.api.build(
                    fileobj=context, custom_context=True, tag=tag, rm=True, forcerm=True,
                    decode=True, pull=False, platform=f"linux/{arch}",
                    labels={LABEL_BASE_ID: image.id, LABEL_PLATFORM: key, LABEL_PYTHON: python,
                            LABEL_KERNEL_LIBS: " ".join(kernel_libs)},
                    buildargs={"BASE_IMAGE": build_name, "JUPYDO_BUILDER_VERSION": version,
                               "JUPYDO_MODULES": " ".join(modules)}):
                if "error" in chunk:
                    raise RuntimeError(chunk["error"].strip())
                line = (chunk.get("stream") or "").strip()
                if not line:
                    continue
                tail = (tail + [line])[-30:]
                step = re.match(r"Step (\d+)/(\d+)", line)
                pct = 5 + int(85 * int(step.group(1)) / int(step.group(2))) if step else None
                emit(line[:300], pct)
        except Exception as e:
            raise RuntimeError("JupyDo build failed for {}: {}\n{}".format(name, e, "\n".join(tail)))
        finally:
            context.close()

        ok, out = _check(client, client.images.get(tag), modules)
        if not ok:
            raise RuntimeError(f"JupyDo build of {name} finished but the check failed:\n{out}")
        emit("JupyDo version built and verified.", 92)
        return tag


def module_host_config(image_name):
    """Extra Docker options for a container of this image, from the modules it has.
    Apptainer: FUSE device, the builder's seccomp profile (Docker's default + the calls
    for unprivileged namespaces) and, where AppArmor is active, the jupydo-apptainer
    profile (loaded on the host by first_start.sh). Nothing for images without modules."""
    client = docker.from_env()
    modules = image_modules(client.images.get(image_name))
    if "apptainer" not in modules:
        return {}
    with open(os.path.join(BUILDER_DIR, "security", "apptainer-seccomp.json")) as f:
        seccomp = json.dumps(json.load(f), separators=(",", ":"))
    security = ["seccomp=" + seccomp]
    try:
        apparmor = open("/sys/module/apparmor/parameters/enabled").read().strip() == "Y"
    except OSError:
        apparmor = False
    if apparmor:
        security.append("apparmor=jupydo-apptainer")
    return {"devices": ["/dev/fuse:/dev/fuse:rwm"], "security_opt": security}
