<div align="center">
    <img src="https://i.imgur.com/HPr8Jax.jpeg" alt="JupyDo Logo" title="JupyDo Logo" width="200">
    <h1>JupyDo</h1>
    <h3>Automated, Reproducible Bioinformatics Infrastructure</h3>

[![License](https://img.shields.io/badge/License-BSD_3--Clause-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Work_In_Progress-orange)]()
[![Docker](https://img.shields.io/badge/Container-Docker-2496ED?logo=docker)]()
[![Builder bundles](https://zenodo.org/badge/DOI/10.5281/zenodo.24498197.svg)](https://doi.org/10.5281/zenodo.24498197)

</div>

---

**JupyDo** is a multi-user **JupyterHub** server for research groups. Every user works in
their own environments, made from **any Docker image** (a BioContainers tool, a Docker Hub
image, a lab's own image): JupyDo turns the image into a complete JupyterLab environment,
with the same tools always available, and records exactly what was used, so an analysis
can be repeated, shared or moved to another computer. It runs on **amd64** and **arm64**
servers, and everything it needs to build environments can be installed **offline**.

Developed by the **Reproducible Bioinformatics Lab**, University of Turin.

## Contents

- [Features](#features)
  - [Environments from any Docker image](#environments-from-any-docker-image)
  - [Optional modules](#optional-modules)
  - [Resources: CPU, memory, GPU](#resources-cpu-memory-gpu)
  - [Batch job queue](#batch-job-queue)
  - [Save, download and share environments](#save-download-and-share-environments)
  - [Files and disks](#files-and-disks)
  - [Services for users](#services-for-users)
  - [Services for administrators](#services-for-administrators)
  - [Accounts and login](#accounts-and-login)
  - [Notifications and branding](#notifications-and-branding)
  - [Audit logs (optional)](#audit-logs-optional)
  - [Reproducibility and offline installation](#reproducibility-and-offline-installation)
- [Requirements](#requirements)
- [Installation](#installation)
- [Updating, and trying a branch](#updating-and-trying-a-branch)
- [Administration tools](#administration-tools)
- [Repository layout](#repository-layout)
- [Related repositories](#related-repositories)
- [Licenses of the images](#licenses-of-the-images)
- [License](#license)
- [Authors](#authors--acknowledgments)

## Features

### Environments from any Docker image

- **Any image as a starting point.** In the environment form users pick one of the ready
  stacks, one of the environments saved by other users, or type **any image name**
  ("Custom Image"). The image does not need to contain Jupyter.
- **The JupyDo builder.** The first time an image is used, JupyDo builds its JupyDo version
  (`jupydo/<name>:<tag>`) on top of it, with the vendored JupyDo stack: its own Python with
  JupyterLab, kernels for the Python and R found in the image, the job queue client,
  the notifications bar and the self-test. The image's own software is not touched.
  The builder recognises the image's distribution and architecture and uses only the
  matching bundles; an image is "ready" only if it was built by the current builder
  version and its stack check passes, otherwise it is rebuilt automatically.
- **Supported bases:** Ubuntu 16.04, 18.04, 20.04, 22.04, 24.04, 26.04 and Debian 12, 13, on
  amd64 and arm64; minimal BioContainers images (BusyBox + conda, no package manager) are
  first converted onto a Debian 12 base, keeping their software.
- **Both architectures.** Images of the other architecture also run, through QEMU
  emulation (slower), installed by `first_start.sh`, with `sudo` working inside emulated
  environments.
- **Several environments per user** (named servers): 2 by default, more if an
  administrator allows it for that user.
- **A personal account in every environment.** Each user has a unique, stable Linux
  UID (10000 + hub user id) and their own account name, so processes and files can be
  traced to the person, also from the host (`users_uid_map.tsv` in the hub state folder
  maps UIDs to users). Users have `sudo` inside their environments.
- **JupyDo Assistant** ("Not sure what Docker is? Ask our assistant", in the form): users
  describe their analysis in plain words, in any language, and a small AI that runs on
  the server's CPU (no GPU, no external service) suggests 1-3 images from a catalog of
  about 15,000 bioinformatics images (BioContainers, StaPH-B, trusted organizations,
  Docker Hub). "Use this image" fills in the form. The model is started only while
  someone uses the assistant. See [JupyDo-Assistant](https://github.com/ReproducibleBioinformatics/JupyDo-Assistant).
- **"Use the image as it is"** (a check box in the form): no JupyDo build, the image starts
  with its own entrypoint and command, for special images prepared by hand. The image must
  start a JupyterHub single-user server by itself; the shared folder, CPU/RAM limits and GPU
  work as usual, the JupyDo additions (modules, cell log, notifications bar, `%%job`,
  self-test) are not there.
- **Self-test and examples.** Every user gets in their folder a self-test notebook that
  checks an environment (it adapts to what the environment contains) and example
  notebooks for the job queue (Python, R, GPU) and, with the Apptainer module, an
  Apptainer tutorial. The self-test is also a terminal command, `jupydo-selftest`.

### Optional modules

Chosen with check boxes when creating an environment, each from its own offline bundle:

| Module | What it adds |
|---|---|
| **RStudio** | RStudio Server, opened from the JupyterLab launcher, with the environment's R (one launcher per R version found), opening in the user's folder |
| **VS Code** | VS Code in the browser (code-server), from the JupyterLab launcher |
| **Desktop** | a Linux desktop in the browser, for programs with a graphical interface |
| **IGV** | the IGV genome browser in a JupyterLab tab, reading BAM/VCF files of the user's folder |
| **Apptainer** | Apptainer/Singularity inside the environment, to run Apptainer containers (with a dedicated AppArmor profile and seccomp filter) |

### Resources: CPU, memory, GPU

- Every new environment starts with **4 CPUs and 16 GB of memory** (`DEFAULT_CPUS`,
  `DEFAULT_MEM_GB` in `jupyterhub_config.py`).
- **More resources:** users can ask for more CPUs or memory, from the form or later from
  the hub home page, with a note; administrators approve or reject the request and the
  user is notified.
- **GPU requests:** an environment created with "Request a GPU" starts only once an
  administrator approves it and chooses which GPU; it then gets that GPU only.
  Interactive environments without an approved GPU see no GPU.
- Administrators change CPU and memory limits of any environment **live**, from the
  environments service.

### Batch job queue

Heavy work goes through a queue instead of the interactive environments:

- **From a notebook:** the `%%job` magic runs a cell as an independent job
  (`%%job --time 2h --cpus 8 --mem 32 --gpus 1 --name training`); `%jobs`, `%job_log`,
  `%job_cancel` follow it. **From R** the same through the R integration, and **from any
  terminal** (JupyterLab, RStudio, VS Code) with `jobqueue_cli.py submit|list|log|cancel`.
- Each job runs in a **new container made from a snapshot of the environment** at
  submission (same software, including what the user installed, same shared folder),
  then disappears.
- **Scheduling:** highest response ratio first (short jobs go first, long jobs age up
  and never starve), backfill of jobs that fit and end before the first in line can
  start, walltime enforced only when someone is waiting.
- **A dedicated pool:** the whole machine minus a system reserve, minus the limits of
  the running interactive environments (CPUs, memory, GPUs; configurable).
- **Admin page** (Services → jobqueue): the pool, every job of every user, live logs,
  kill or cancel any job.

### Save, download and share environments

From the **images** service (Services menu), for the user's own environments:

- **Commit:** the environment, as it is now (including software installed with `sudo`),
  becomes a saved image (`jupydo-saved/<user>-<name>:<n>`, nothing is overwritten),
  offered to **every user** when creating an environment ("Saved environments", with a
  search by user and name).
- **Download:** the environment becomes one zip in the user's folder, with the image
  and start scripts to run it **anywhere, without JupyDo**:
  - `run.sh` (Linux, macOS) and `run.cmd` (Windows), with Docker;
  - `start_apptainer.sh` (Linux, WSL), with **Apptainer or Singularity and no root**,
    e.g. on an HPC cluster: it converts the image once and opens JupyterLab on port 8888,
    with the token link shown in a clearly visible box;
  - a README explaining each.
- **Size limit:** above it (50 GB by default), saving or downloading becomes a request
  that an administrator approves.

### Files and disks

- **One shared folder per user** (`/sharedFolder` in every environment and job, on the
  host `JupyDo/jh_shared/<user>_shared`), the same in all their environments; JupyterLab
  opens there, while the file browser shows the real full paths from `/`. The user's
  home holds only settings and packages.
- **Genome indexes** built by the Genome Index Service appear read-only in
  `/sharedFolder/genome_indexes`.
- **Disk usage and limits:** usage of every user measured every hour, with a default and
  per-user limits; users over their limit get an urgent notification until they are
  back under it. `first_start.sh` checks whether the filesystem can enforce disk quotas
  (xfs or ext4 project quotas).
- **Extra disks:** an administrator can attach any disk or folder to a user's shared
  folder; it appears at once in their running environments and jobs (see
  [Administration tools](#administration-tools)).

### Services for users

In the hub's **Services** menu, each opened inside a hub page:

| Service | What it does |
|---|---|
| **images** | commit and download of the user's own environments (see above) |
| **transfer** | fast, **resumable** upload and download of large files in the user's folder (binary pieces, resume after a closed browser or a hub restart, folders as `.tar`); works with the environment stopped |
| **bookings** | a shared calendar to agree on who uses what and when (e.g. a GPU); administrators create the bookable resources |
| **genome-index** | the [Genome Index Service](https://github.com/ReproducibleBioinformatics/Index_builderGUI): builds genome indexes for aligners, through the job queue |
| **assistant** | the JupyDo Assistant (also opened from the environment form) |

### Services for administrators

| Service | What it does |
|---|---|
| **environments** | every environment with its CPU/memory limits (changed live), reset of a stopped environment, disk usage and limits of each user, how many environments each user may have, and approval of resource, GPU, commit and download requests |
| **notifications** | messages to one user, several users or everyone, with a level (info, warning, urgent), shown on the hub home and at the top of JupyterLab; the history shows who has read them |
| **jobqueue** | the job queue admin page |
| **Authorize Users** | the hub page where new accounts are authorized, with an expiry date for each account |

### Accounts and login

- **JupyDo accounts** (NativeAuthenticator): users sign up with an email address and an
  administrator authorizes them. Passwords are checked against common ones (minimum
  length 7). Users change their password from the hub.
- **Two-factor authentication** with an authenticator app, optional or **mandatory** for
  everyone (asked in `first_start.sh`).
- **Or GitHub login:** only members of a chosen GitHub organization.
- **Account validity:** administrators set until when each account is valid; users are
  warned every day during the last week, then they cannot log in and their running
  environments are stopped (containers and data are kept). Administrators never expire.
- **Terms of use:** each installation provides its PDF (`branding/terms.pdf`); no
  environment starts before the user accepts the current version, and every acceptance
  is recorded (append-only).
- **Several administrators** (`JUPYDO_ADMIN`, comma separated), so nobody is locked out.

### Notifications and branding

- **Live notifications:** administrators' messages, and automatic ones (disk limit,
  account expiry, request results), on the hub home and as a bar at the top of
  JupyterLab, updated within 30 seconds by the JupyDo JupyterLab extension.
- **Branding:** the server's name (asked in `first_start.sh`) and logo
  (`branding/logo.png`) in the hub and in JupyterLab's top bar
  ("`<server> · <environment>`"); the hub and the services follow the hub's light/dark theme.

### Audit logs (optional)

Each is **asked explicitly in `first_start.sh`** (off unless the answer is yes) and every
file is **readable only by root**. Users must be informed (e.g. in the terms of use).

| Log | What is recorded | Where |
|---|---|---|
| **Command log** | every program started by JupyDo users (terminal commands, `!commands` and subprocesses of notebooks, `sudo` with its arguments), from the host's audit system (auditd) | `JupyDo/logs/commands/<user>.log` |
| **Cell log** | every cell users run in JupyterLab (notebooks and consoles, **any language**): time, user, environment, notebook, kernel, cell and the code itself; the user is taken from the environment's token, never from what the environment says | `JupyDo/logs/cells/<user>.log` |
| **Network log** | every connection from environments and jobs to outside: time, user, container, image, destination and the **public port** after the NAT, plus container starts, stops and deletions; enough to tell who made a connection reported from outside, even if the container was deleted later. It runs in its own container (the hub image, host network): **nothing is installed on the host**. Kept for the number of days chosen at installation, older days compressed | `JupyDo/logs/network/YYYY-MM-DD.log` |

The cell log applies to environments created after it is enabled. Users have `sudo`
inside their environments, so the logs written on the host (commands, network) are the
reliable ones.

### Reproducibility and offline installation

- **The builder's bundles** (Python, R, RStudio, VS Code, desktop, Apptainer, IGV, kernel
  libraries, build tools, base images; for every supported distribution and both
  architectures) are made once by the [JupyDo builder](https://github.com/ReproducibleBioinformatics/Jupydo_Builder)
  and **published on Zenodo**, each version with its DOI (current:
  [10.5281/zenodo.24498197](https://doi.org/10.5281/zenodo.24498197)). `first_start.sh` downloads
  the chosen version and checks every file against its SHA-256: no copy by hand, and
  every installation of the same version builds the same environments.
- **The hub image is built only from vendored files** (base image pinned by digest,
  Python wheels, Node.js, the proxy, conntrack, the sources of the Genome Index and of
  the Assistant at a fixed commit), with **no network during the build**. GitHub builds
  it from the files on Zenodo for amd64 and arm64 and publishes it on
  `ghcr.io/vehx35/jupydo-hub`; `first_start.sh` downloads it ready-made, or builds it
  locally from the same files (`build-hub.sh`) if it cannot. The exact versions are
  written inside the image (`/opt/jupydo-vendor/`).
- **Environments are pinned:** saved images keep their history, downloads carry the
  exact image, and the builder version is written in every image's labels.

## Requirements

- A **Linux** server (Ubuntu or Debian recommended), amd64 or arm64.
- **Docker Engine** with **Docker Compose**.
- Standard tools found on any Linux: `curl`, `sha256sum`, `tar`, `python3`.
- Installed by `first_start.sh` when needed: **QEMU** emulation (for images of the other
  architecture: always QEMU 10.2.3, from a pinned image, registered at every boot) and, only if the command log
  is enabled, **auditd**. On systems with AppArmor, a profile for the Apptainer module.
- Disk: about 16 GB for the builder's bundles, plus the users' data and images.
- NVIDIA GPUs (optional) need the NVIDIA Container Toolkit.

## Installation

```bash
git clone https://github.com/Vehx35/JupyDo.git
cd JupyDo/main
sudo ./first_start.sh
```

`first_start.sh` (run it again at any time: every step checks first) does, in order:

1. checks Docker and Compose; installs QEMU emulation and its boot-time service;
2. creates the Docker network `jupyterhub_network`;
3. asks:
   - the parent folder of the users' data (`JupyDo/`) and the folder of the **hub
     state** (database, secrets), which should be on a local disk (it can move an
     existing state safely);
   - the administrators, the server's name;
   - the login method (JupyDo accounts with optional or mandatory 2FA, or a GitHub
     organization);
   - the **command log**, the **cell log** and the **network log** (and how many days
     to keep it), each separately;
4. checks whether disk quotas can be enforced on the data folder;
5. downloads the **builder and its bundles from Zenodo** (checked, resumable, not
   downloaded again if that version is already installed);
6. downloads the **JupyDo Assistant**'s model (from its GitHub release, checked) and its
   model server image;
7. loads the AppArmor profile of the Apptainer module, if AppArmor is active;
8. runs a read-only disk health check (`jupydo-doctor.sh`);
9. downloads the **hub image** from GitHub (or builds it from the vendored files) and
   starts JupyDo.

Then open `http://<server>:8000`. `branding/` holds the logo and the terms of use of the
installation: replace them with your own.

## Updating, and trying a branch

- **Update:** `git pull`, then `sudo ./first_start.sh` (or `docker compose pull &&
  docker compose up -d` for the hub image only). Restarting the hub stops the running
  environments: users start them again from the hub home (containers and data are kept).
- **Try a branch before merging:** GitHub builds a hub image for every branch,
  `ghcr.io/vehx35/jupydo-hub:<branch>` (`latest` is only `main`). Set
  `JUPYDO_HUB_TAG=<branch>` in `.env` (or in the shell) and `docker compose up -d`.
- **Build the hub image here, offline:** `./build-hub.sh [vendor folder]` (default
  `$JUPYDO_PATH/builder/vendor`).

## Administration tools

All in `main/`, run as root:

| Tool | What it does |
|---|---|
| `first_start.sh` | installation and reconfiguration (see above) |
| `build-hub.sh` | builds the hub image from the vendored files, without network |
| `jupydo-doctor.sh` | read-only health check of the disks JupyDo depends on: free space and inodes of the disk that really holds each folder (also on mergerfs pools), a real write test, mergerfs policy, duplicated mounts |
| `utils/jupydo-disk.sh attach\|detach\|list` | attaches an extra disk or folder to a user's shared folder (`/sharedFolder/<name>`), visible at once in running environments and jobs, optionally read-only |
| `jupydo-migrate-work.sh` | one-time move of the old per-user `work` volumes of earlier JupyDo versions into the users' shared folders (the volumes are not deleted) |
| `jupydo-cmdlog.py`, `jupydo-netlog.py` | the command and network logs (installed and started by `first_start.sh`) |

## Repository layout

```
main/
  first_start.sh            installation
  compose.yaml              the hub (and the optional network log container)
  Dockerfile.jupyterhub     the hub image, from vendored files only
  build-hub.sh              builds it offline
  jupyterhub_config.py      the hub: spawner, form, services, accounts, logs
  jupydo_builder.py         makes any image a JupyDo environment (uses the builder)
  jupydo_ui.py              notifications, branding, accounts, terms, requests
  jupydo_environments.py    admin service: environments, disks, requests
  jupydo_notifications.py   admin service: notifications
  jupydo_images*.py         commit and download of environments
  jupydo_transfer.py        resumable upload/download
  jupydo_bookings.py        shared calendar
  jobqueue_*.py, Rprofile.site, ipython_kernel_config.py   the job queue
  jupydo_celllog*.py        cell log;  jupydo-netlog.py  network log;  jupydo-cmdlog.py  command log
  templates/                hub pages (home, sign-up, terms, Services menu...)
  labextension/             JupyterLab extension (notifications bar)
  jupydo_selftest/, jobqueue_example/, apptainer_example/   notebooks copied to users
  branding/                 logo and terms of use of the installation
  utils/                    jupydo-disk.sh, QEMU helper
.github/workflows/hub-image.yml   builds the hub image from Zenodo's files, on ghcr.io
```

## Related repositories

| Repository | Role |
|---|---|
| [Jupydo_Builder](https://github.com/ReproducibleBioinformatics/Jupydo_Builder) | the builder: modules, vendoring scripts, tests; its bundles are published on Zenodo |
| [JupyDo-Assistant](https://github.com/ReproducibleBioinformatics/JupyDo-Assistant) | the assistant and its image catalog (rebuilt every 3 months) |
| [Index_builderGUI](https://github.com/ReproducibleBioinformatics/Index_builderGUI) | the Genome Index Service |
| [llama.cpp fork](https://github.com/ReproducibleBioinformatics/llama.cpp) | the model server of the assistant, built for amd64 and arm64 |

## Licenses of the images

The images users choose, and the software inside them, are made by third parties, each
with its own license; some (e.g. 10x Genomics Cell Ranger) require accepting the vendor's
license. **Users are responsible for checking and respecting the license of every image
and software they use.** The bundles JupyDo adds keep their own licenses (listed with
the builder).

## License

This project is licensed under the **BSD 3-Clause License**. See the [LICENSE](LICENSE) file for details.

## Authors & Acknowledgments
* **Francesco Maria Antonio Micocci** - *University of Turin*
* **Luca Alessandri** - *University of Turin*
* Developed by the **Reproducible Bioinformatics Lab**, University of Turin.

---

> **Disclaimer:** This project is in active development. Features and configurations are subject to change.
