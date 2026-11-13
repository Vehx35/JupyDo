#!/bin/bash

# first_start.sh - Initialization script

# Ensure script runs as root
if [ "$EUID" -ne 0 ]; then
  exec sudo "$0" "$@"
fi

#Correct directory checks
if [ ! -f "compose.yaml" ]; then
    echo "ERROR: 'compose.yaml' not found"
    exit 1
elif [ ! -f "jupyterhub_config.py" ]; then
    echo "ERROR: 'jupyterhub_config.py' not found"
    exit 1
fi

# Detect real user (even if run with sudo)
REAL_USER=${SUDO_USER:-$USER}
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)

echo "Detected user: $REAL_USER"
echo "Home directory: $REAL_HOME"

# Check if docker is installed
if ! command -v docker &> /dev/null; then
    echo "Docker is not installed. Please install Docker first."
    exit 1
fi

# Check if docker-compose is installed
if ! docker compose version &> /dev/null; then
    echo "Docker Compose is not installed. Please install Docker Compose first."
    exit 1
fi

# QEMU emulation: lets JupyDo run images of the other architecture (arm64 images
# on an amd64 server and vice versa), slower. Installed once, stays after reboots.
# Checks the PACKAGE, not whether emulation is active right now: an emulation
# enabled by hand (docker run tonistiigi/binfmt ...) disappears at the next reboot.
if ! dpkg -s qemu-user-static >/dev/null 2>&1; then
    echo "Installing QEMU emulation (images of the other architecture)..."
    if command -v apt-get &> /dev/null; then
        apt-get update -qq && apt-get install -y -qq qemu-user-static binfmt-support \
            || echo "WARNING: QEMU installation failed: images of the other architecture will not run."
    else
        echo "WARNING: apt-get not found: install qemu-user-static by hand to run images of the other architecture."
    fi
else
    echo "QEMU emulation already installed. Skipping."
fi

# sudo in emulated containers: QEMU registered with the "C" (credentials) flag, at
# every boot (utils/qemu-binfmt-credentials.sh, run by a systemd service after the
# system registered the emulators)
if dpkg -s qemu-user-static >/dev/null 2>&1 && command -v systemctl &> /dev/null; then
    install -D -m 755 ./utils/qemu-binfmt-credentials.sh /usr/local/lib/jupydo/qemu-binfmt-credentials.sh
    cat > /etc/systemd/system/jupydo-qemu-credentials.service <<'EOF_UNIT'
[Unit]
Description=JupyDo: QEMU emulators with the credentials flag (sudo in emulated containers)
After=systemd-binfmt.service binfmt-support.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/lib/jupydo/qemu-binfmt-credentials.sh

[Install]
WantedBy=multi-user.target
EOF_UNIT
    systemctl daemon-reload
    systemctl enable jupydo-qemu-credentials.service >/dev/null 2>&1
    systemctl restart jupydo-qemu-credentials.service
    echo "QEMU: sudo enabled in emulated containers (flags: $(sed -n 's/^flags: //p' /proc/sys/fs/binfmt_misc/qemu-aarch64 2>/dev/null))"
fi

# Creating Docker network (only once)
if ! docker network inspect jupyterhub_network >/dev/null 2>&1; then
    echo "Creating Docker network..."
    docker network create --driver bridge jupyterhub_network
    echo "Docker network created."
else
    echo "Docker network 'jupyterhub_network' already exists. Skipping."
fi

# Launch Script
echo "Initialization script completed."
echo "You can now start the JupyterHub server using 'docker compose up'."
echo "Do you want to start the JupyterHub server now? (y/n)"
read start_jupyterhub

if [[ "$start_jupyterhub" =~ ^[Yy]$ ]]; then
    echo "Starting JupyterHub server..."

# --- Path selection with autocomplete (TAB) ---
while true; do
    read -e -i "$REAL_HOME" -p "Enter the parent folder where to create JupyDo (TAB for autocomplete): " JUPYDO_PATH

    # Default to REAL_HOME if empty
    if [ -z "$JUPYDO_PATH" ]; then
        JUPYDO_PATH="$REAL_HOME"
        break
    fi

    # Safe tilde (~) expansion
    if [[ "$JUPYDO_PATH" == ~* ]]; then
        JUPYDO_PATH="${JUPYDO_PATH/#\~/$REAL_HOME}"
        break
    fi

    # Accept absolute paths
    if [[ "$JUPYDO_PATH" == /* ]]; then
        break
    fi

    # Resolve relative paths
    JUPYDO_PATH="$REAL_HOME/$JUPYDO_PATH"
    break
done

    # Normalize path (remove trailing slash)
    JUPYDO_PATH="${JUPYDO_PATH%/}"
    JUPYDO_FULL_PATH="$JUPYDO_PATH/JupyDo"

    # --- Admin selection ---
    read -p "Enter the admin username(s), comma separated, e.g. luca,francesco (default: limo): " ADMIN_USER
    if [ -z "$ADMIN_USER" ]; then
        ADMIN_USER="limo"
    fi

    # --- Server name (shown in the hub and in JupyterLab, e.g. "Pumba · mario") ---
    read -p "Enter a name for this server (default: $(hostname)): " SERVER_NAME
    if [ -z "$SERVER_NAME" ]; then
        SERVER_NAME="$(hostname)"
    fi

    # --- Login method ---
    echo "Login method: 1) JupyDo accounts (optional 2FA with an authenticator app)  2) GitHub organization"
    read -p "Choose 1 or 2 (default: 1): " AUTH_CHOICE
    AUTH_MODE="native"
    TWO_FA_REQUIRED="no"
    if [ "$AUTH_CHOICE" != "2" ]; then
        read -p "Make two-factor authentication (authenticator app) mandatory for every user? (y/n): " TWO_FA
        if [[ "$TWO_FA" =~ ^[Yy]$ ]]; then
            TWO_FA_REQUIRED="yes"
        fi
    fi
    if [ "$AUTH_CHOICE" = "2" ]; then
        AUTH_MODE="github"
        echo "Create a GitHub OAuth App (GitHub > Settings > Developer settings > OAuth Apps)."
        read -p "Public URL of this JupyDo, as users open it (e.g. https://pumba.unito.it): " PUBLIC_URL
        echo "  Authorization callback URL to set in the GitHub app: ${PUBLIC_URL%/}/hub/oauth_callback"
        read -p "GitHub OAuth App Client ID: " GH_CLIENT_ID
        read -s -p "GitHub OAuth App Client Secret: " GH_CLIENT_SECRET; echo
        read -p "GitHub organization allowed to log in: " GH_ORG
        echo "Note: admins (asked above) must be GitHub usernames."
    fi

    # --- Hub state (database, cookie secret, queue state): on a LOCAL disk ---
    # Not on a pooled filesystem (mergerfs...): if the branch that physically holds it
    # fills up, the database becomes unwritable even when the pool shows free space.
    OLD_STATE=""
    if [ -f .env ]; then
        OLD_STATE="$(grep '^JUPYDO_DATA_PATH=' .env | cut -d= -f2-)"
    fi
    DEFAULT_STATE="${OLD_STATE:-/var/lib/jupydo/hub_state}"
    read -e -i "$DEFAULT_STATE" -p "Folder for the hub state (database, secrets), on a local disk: " STATE_PATH
    STATE_PATH="${STATE_PATH%/}"
    if [ -z "$STATE_PATH" ]; then
        STATE_PATH="$DEFAULT_STATE"
    fi

    # Moving the state: stop the hub (user servers keep running), copy everything
    # (database + journal files + cookie secret), keep the old folder as a backup.
    if [ -n "$OLD_STATE" ] && [ "$OLD_STATE" != "$STATE_PATH" ] && [ -f "$OLD_STATE/jupyterhub.sqlite" ]; then
        if [ -f "$STATE_PATH/jupyterhub.sqlite" ]; then
            echo "ERROR: both $OLD_STATE and $STATE_PATH contain a hub database. Keep one, move the other away, then run again."
            exit 1
        fi
        echo "Moving the hub state from $OLD_STATE to $STATE_PATH (hub stopped first, user servers keep running)..."
        docker stop jupyterhub >/dev/null 2>&1 || true
        mkdir -p "$STATE_PATH"
        cp -a "$OLD_STATE"/. "$STATE_PATH"/
        if [ ! -f "$STATE_PATH/jupyterhub.sqlite" ]; then
            echo "ERROR: copy failed. The old state is untouched in $OLD_STATE"
            exit 1
        fi
        if command -v python3 &> /dev/null; then
            python3 -c "import sqlite3,sys; r=sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0]; print('Database check:', r); sys.exit(r!='ok')" \
                "$STATE_PATH/jupyterhub.sqlite" || { echo "ERROR: the copied database is damaged. The old state is untouched in $OLD_STATE"; exit 1; }
        fi
        echo "Hub state moved. The old folder is kept as a backup: $OLD_STATE"
    fi

    echo "Creating directories under: $JUPYDO_FULL_PATH (users' shared data) and $STATE_PATH (hub state)"
    mkdir -p "$STATE_PATH"
    mkdir -p "$JUPYDO_FULL_PATH/jh_shared" # Crea anche la cartella per i dati condivisi

    # 2. Popola il file .env con tutte le variabili
    echo "JUPYDO_DATA_PATH=$STATE_PATH" > .env
    echo "JUPYDO_PATH=$JUPYDO_FULL_PATH" >> .env
    echo "JUPYDO_ADMIN=$ADMIN_USER" >> .env
    echo "JUPYDO_SERVER_NAME=$SERVER_NAME" >> .env
    echo "JUPYDO_AUTH=$AUTH_MODE" >> .env
    echo "JUPYDO_2FA_REQUIRED=$TWO_FA_REQUIRED" >> .env
    if [ "$AUTH_MODE" = "github" ]; then
        echo "JUPYDO_PUBLIC_URL=$PUBLIC_URL" >> .env
        echo "JUPYDO_GITHUB_CLIENT_ID=$GH_CLIENT_ID" >> .env
        echo "JUPYDO_GITHUB_CLIENT_SECRET=$GH_CLIENT_SECRET" >> .env
        echo "JUPYDO_GITHUB_ORG=$GH_ORG" >> .env
    fi
    chmod 600 .env  # it may contain the GitHub secret

    # --- Disk quotas: check whether the filesystem can enforce them ---
    FS_TYPE="$(df -T "$JUPYDO_FULL_PATH" | awk 'NR==2 {print $2}')"
    FS_OPTS="$(findmnt -no OPTIONS --target "$JUPYDO_FULL_PATH" 2>/dev/null)"
    case "$FS_TYPE" in
        xfs)
            if echo "$FS_OPTS" | grep -qE 'prjquota|pquota'; then
                echo "Disk quotas: SUPPORTED (xfs with project quotas)."
            else
                echo "Disk quotas: possible (xfs), but project quotas are off: add 'prjquota' to this filesystem's options in /etc/fstab and remount."
            fi ;;
        ext4)
            if echo "$FS_OPTS" | grep -q 'prjquota'; then
                echo "Disk quotas: SUPPORTED (ext4 with project quotas)."
            else
                echo "Disk quotas: possible (ext4), but project quotas are off (needs the 'project,quota' features and the 'prjquota' option)."
            fi ;;
        fuse.mergerfs)
            echo "Disk quotas: NOT available on a mergerfs pool: no per-user disk limits there." ;;
        *)
            echo "Disk quotas: NOT available on '$FS_TYPE': no per-user disk limits." ;;
    esac

    # --- Command audit log (who ran which program), recorded by the host ---
    read -p "Enable the command audit log on this host (auditd, users must be informed)? (y/n): " ENABLE_AUDIT
    if [[ "$ENABLE_AUDIT" =~ ^[Yy]$ ]]; then
        if command -v apt-get &> /dev/null; then
            apt-get install -y -qq auditd
            cat > /etc/audit/rules.d/jupydo.rules <<'EOF_AUDIT'
## JupyDo: every program started by JupyDo users (UID 10000-19999), including sudo with its arguments
-a always,exit -F arch=b64 -S execve -F uid>=10000 -F uid<=19999 -k jupydo_cmd
-a always,exit -F arch=b32 -S execve -F uid>=10000 -F uid<=19999 -k jupydo_cmd
EOF_AUDIT
            augenrules --load || systemctl restart auditd

            # One log file per user, in the JupyDo folder, updated every minute
            install -D -m 755 ./jupydo-cmdlog.py /usr/local/lib/jupydo/jupydo-cmdlog.py
            cat > /etc/systemd/system/jupydo-cmdlog.service <<EOF_SVC
[Unit]
Description=JupyDo: split the users' command audit log into one file per user
After=auditd.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/jupydo/jupydo-cmdlog.py --out $JUPYDO_FULL_PATH/logs/commands --uidmap $STATE_PATH/users_uid_map.tsv
EOF_SVC
            cat > /etc/systemd/system/jupydo-cmdlog.timer <<'EOF_TIMER'
[Unit]
Description=JupyDo: update the per-user command logs every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
EOF_TIMER
            systemctl daemon-reload
            systemctl enable --now jupydo-cmdlog.timer
            echo "Command log enabled: one file per user in $JUPYDO_FULL_PATH/logs/commands/ (root only),"
            echo "updated every minute. Raw audit log: sudo ausearch -k jupydo_cmd -i"
        else
            echo "WARNING: apt-get not found, install auditd by hand to enable the command audit log."
        fi
    fi

    # --- Notebook cell log (the code users run in JupyterLab): asked every time ---
    echo "Notebook cell log: the code users run in JupyterLab cells (notebooks and consoles, any language)"
    echo "is recorded, one file per user in $JUPYDO_FULL_PATH/logs/cells, readable only by root."
    read -p "Enable the notebook cell log (users must be informed)? (y/n): " ENABLE_CELLLOG
    if [[ "$ENABLE_CELLLOG" =~ ^[Yy]$ ]]; then
        echo "JUPYDO_CELL_LOG=yes" >> .env
        mkdir -p "$JUPYDO_FULL_PATH/logs/cells" && chmod 700 "$JUPYDO_FULL_PATH/logs/cells"
        echo "Cell log enabled. Environments created before now are logged once they are recreated."
    else
        echo "JUPYDO_CELL_LOG=no" >> .env
        echo "Cell log disabled."
    fi

    # --- Network connection log (who connected where): asked every time ---
    echo "Network connection log: every connection from users' environments and queue jobs to outside"
    echo "is recorded (time, user, container, destination, public port), one file per day in"
    echo "$JUPYDO_FULL_PATH/logs/network, readable only by root."
    read -p "Enable the network connection log (users must be informed)? (y/n): " ENABLE_NETLOG
    if [[ "$ENABLE_NETLOG" =~ ^[Yy]$ ]]; then
        read -p "Keep the network log for how many days? " NETLOG_DAYS
        while ! [[ "$NETLOG_DAYS" =~ ^[0-9]+$ ]] || [ "$NETLOG_DAYS" -lt 1 ]; do
            read -p "Please type a number of days (e.g. 365): " NETLOG_DAYS
        done
        echo "COMPOSE_PROFILES=netlog" >> .env
        echo "JUPYDO_NETLOG_DAYS=$NETLOG_DAYS" >> .env
        mkdir -p "$JUPYDO_FULL_PATH/logs/network" && chmod 700 "$JUPYDO_FULL_PATH/logs/network"
        echo "Network log enabled: $JUPYDO_FULL_PATH/logs/network/ (kept $NETLOG_DAYS days, root only;"
        echo "it runs in the jupydo-netlog container, started with JupyterHub)."
    else
        docker rm -f jupydo-netlog >/dev/null 2>&1 && echo "Network log stopped (the files already written are kept)."
        echo "Network log disabled."
    fi

    # --- The builder and its vendored bundles (everything needed to build users'
    # environments, also offline): from the Zenodo record below, published with
    # jupydo-builder's scripts/publish-zenodo.py. Every file is checked against the
    # record's SHA256SUMS; nothing is downloaded again if that record is already in place.
    JUPYDO_VENDOR_RECORD=""   # Zenodo record id of the builder version to install
    JUPYDO_ZENODO="https://zenodo.org"
    BUILDER_DIR="$JUPYDO_FULL_PATH/builder"
    if [ -z "$JUPYDO_VENDOR_RECORD" ]; then
        echo "WARNING: no builder version set in first_start.sh (JUPYDO_VENDOR_RECORD): $BUILDER_DIR is left as it is."
    elif [ "$(cat "$BUILDER_DIR/.zenodo-record" 2>/dev/null)" = "$JUPYDO_VENDOR_RECORD" ]; then
        echo "Builder (Zenodo record $JUPYDO_VENDOR_RECORD) already installed. Skipping."
    else
        echo "Downloading the builder and its bundles from Zenodo (record $JUPYDO_VENDOR_RECORD, about 15 GB,"
        echo "and as much again while unpacking)..."
        DL="$JUPYDO_FULL_PATH/.builder-download"
        mkdir -p "$DL"
        BASE="$JUPYDO_ZENODO/records/$JUPYDO_VENDOR_RECORD/files"
        if curl -fsSL -o "$DL/SHA256SUMS" "$BASE/SHA256SUMS?download=1"; then
            # the hub image is not needed here: it comes from ghcr.io (see below)
            grep -v ' jupydo-hub-' "$DL/SHA256SUMS" > "$DL/SHA256SUMS.builder"
            BUILDER_OK=yes
            while read -r _ f; do
                echo "  $f"
                curl -fL -C - --progress-bar -o "$DL/$f" "$BASE/$f?download=1" || { BUILDER_OK=no; break; }
            done < "$DL/SHA256SUMS.builder"
            if [ "$BUILDER_OK" = yes ] && (cd "$DL" && sha256sum --quiet -c SHA256SUMS.builder); then
                rm -rf "$BUILDER_DIR.new" && mkdir -p "$BUILDER_DIR.new/vendor"
                tar -xzf "$DL/jupydo-builder-src.tar.gz" -C "$BUILDER_DIR.new" --strip-components=1
                for t in "$DL"/vendor-*.tar; do tar -xf "$t" -C "$BUILDER_DIR.new/vendor"; done
                echo "$JUPYDO_VENDOR_RECORD" > "$BUILDER_DIR.new/.zenodo-record"
                rm -rf "$BUILDER_DIR.old"
                [ -d "$BUILDER_DIR" ] && mv "$BUILDER_DIR" "$BUILDER_DIR.old"
                mv "$BUILDER_DIR.new" "$BUILDER_DIR" && rm -rf "$BUILDER_DIR.old" "$DL"
                echo "Builder installed from Zenodo record $JUPYDO_VENDOR_RECORD."
            else
                # damaged files are deleted (downloaded again next time), complete ones are kept
                (cd "$DL" && sha256sum -c SHA256SUMS.builder 2>/dev/null | sed -n 's/: FAILED.*//p' | xargs -r rm -f)
                echo "WARNING: builder download failed or damaged: $BUILDER_DIR is left as it is (run again to resume)."
            fi
        else
            echo "WARNING: cannot reach Zenodo record $JUPYDO_VENDOR_RECORD: $BUILDER_DIR is left as it is."
        fi
    fi

    # 3. Copia il config così com'è (senza sed!) nella cartella dati
    cp ./jupyterhub_config.py "$STATE_PATH/jupyterhub_config.py"

    # AppArmor profile for environments with the Apptainer module (only they use it):
    # Docker's docker-default plus mount, pivot_root and user namespaces. The template
    # is in the builder folder; adapted to this machine's AppArmor (the userns rule
    # exists from AppArmor 4 on) and put in /etc/apparmor.d, reloaded at every boot.
    APPARMOR_TEMPLATE="$(grep '^JUPYDO_PATH=' .env | cut -d= -f2-)/builder/security/jupydo-apptainer.apparmor.in"
    if [ "$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null)" != "Y" ] || ! command -v apparmor_parser &> /dev/null; then
        echo "AppArmor not active: no profile needed for the Apptainer module."
    elif [ ! -f "$APPARMOR_TEMPLATE" ]; then
        echo "WARNING: $APPARMOR_TEMPLATE not found (builder not installed?): environments with Apptainer will not start."
    else
        if [ -f /etc/apparmor.d/abi/4.0 ]; then
            APPARMOR_ABI='abi <abi/4.0>,'; APPARMOR_USERNS='userns,'
        else
            APPARMOR_ABI=''; APPARMOR_USERNS=''   # AppArmor 3: no user-namespace ban to lift
        fi
        sed -e "s#@ABI@#${APPARMOR_ABI}#" -e "s#@USERNS@#${APPARMOR_USERNS}#" "$APPARMOR_TEMPLATE" \
            > /etc/apparmor.d/jupydo-apptainer
        if apparmor_parser -r -W /etc/apparmor.d/jupydo-apptainer; then
            echo "AppArmor profile jupydo-apptainer loaded (Apptainer module)."
        else
            echo "WARNING: AppArmor profile jupydo-apptainer not loaded: environments with Apptainer will not start."
        fi
    fi

    # JupyDo Assistant (helps users who do not know Docker choose an image): its model and
    # its llama.cpp server image, ready before the first click. Both come from the
    # JupyDo-Assistant repository (model: its GitHub release, checked against the SHA-256
    # published there; image: ghcr.io, the version named in its assistant/llm.py on main).
    # If something fails the assistant is simply off (its link is not shown).
    ASSISTANT_REPO="https://github.com/ReproducibleBioinformatics/JupyDo-Assistant"
    ASSISTANT_RELEASE="$ASSISTANT_REPO/releases/download/model-qwen2.5-1.5b-instruct-q4_k_m"
    ASSISTANT_MODEL="qwen2.5-1.5b-instruct-q4_k_m.gguf"
    ASSISTANT_DIR="$JUPYDO_FULL_PATH/jh_shared/.assistant"
    mkdir -p "$ASSISTANT_DIR"
    ASSISTANT_SHA256="$(curl -fsSL "$ASSISTANT_RELEASE/$ASSISTANT_MODEL.sha256" 2>/dev/null | cut -d' ' -f1)"
    if [ -z "$ASSISTANT_SHA256" ]; then
        echo "WARNING: cannot reach the JupyDo Assistant release: the assistant stays off."
    elif [ -f "$ASSISTANT_DIR/$ASSISTANT_MODEL" ] && \
         [ "$(sha256sum "$ASSISTANT_DIR/$ASSISTANT_MODEL" | cut -d' ' -f1)" = "$ASSISTANT_SHA256" ]; then
        echo "JupyDo Assistant model already present. Skipping."
    else
        echo "Downloading the JupyDo Assistant model (1.1 GB)..."
        if curl -fL --progress-bar -o "$ASSISTANT_DIR/$ASSISTANT_MODEL.part" "$ASSISTANT_RELEASE/$ASSISTANT_MODEL" \
           && [ "$(sha256sum "$ASSISTANT_DIR/$ASSISTANT_MODEL.part" | cut -d' ' -f1)" = "$ASSISTANT_SHA256" ]; then
            mv "$ASSISTANT_DIR/$ASSISTANT_MODEL.part" "$ASSISTANT_DIR/$ASSISTANT_MODEL"
            curl -fsSL -o "$ASSISTANT_DIR/LICENSE" "$ASSISTANT_RELEASE/LICENSE" || true
            echo "JupyDo Assistant model downloaded and checked."
        else
            rm -f "$ASSISTANT_DIR/$ASSISTANT_MODEL.part"
            echo "WARNING: JupyDo Assistant model download failed or damaged: the assistant stays off."
        fi
    fi
    ASSISTANT_IMAGE="$(curl -fsSL "$ASSISTANT_REPO/raw/main/assistant/llm.py" 2>/dev/null \
        | sed -n 's/^LLM_IMAGE = "\(.*\)"/\1/p')"
    if [ -n "$ASSISTANT_IMAGE" ]; then
        echo "Downloading the JupyDo Assistant model server ($ASSISTANT_IMAGE)..."
        docker pull -q "$ASSISTANT_IMAGE" >/dev/null \
            || echo "WARNING: cannot download $ASSISTANT_IMAGE: it will be downloaded at the first use of the assistant (slower)."
    fi

    # Launching Compose
    echo "Launching Docker Compose..."
    # Disk health check (read-only): free space and inodes per disk, mergerfs branches
    bash ./jupydo-doctor.sh || echo "WARNING: jupydo-doctor found problems (see above)."

    # The hub image: downloaded ready-made from GitHub (ghcr.io); if it cannot be
    # downloaded, built here from the vendored files of the builder (no internet needed)
    if docker compose pull jupyterhub; then
        echo "Hub image downloaded from ghcr.io."
    elif JUPYDO_PATH="$JUPYDO_FULL_PATH" bash ./build-hub.sh; then
        echo "Hub image built from the vendored files."
    else
        echo "ERROR: the hub image could not be downloaded nor built: JupyterHub was NOT updated."
        exit 1
    fi
    if docker compose up -d; then
        echo "JupyterHub server started."
    else
        echo "ERROR: docker compose failed, JupyterHub was NOT updated (see the messages above)."
        exit 1
    fi
else
    echo "You can start the JupyterHub server later using 'docker compose pull && docker compose up -d'"
    echo "(or './build-hub.sh && docker compose up -d' to build the hub image here, from the vendored files)."
fi
