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

# --- SYSBOX INTEGRATION (Zero-Downtime Install) ---
if ! command -v sysbox-runc &> /dev/null; then
    echo "Sysbox not found. Preparing for zero-downtime installation..."
    
    DAEMON_JSON="/etc/docker/daemon.json"
    
    # Ensure jq is installed
    if ! command -v jq &> /dev/null; then
        apt-get update -qq && apt-get install -y jq
    fi

   # Prepare daemon.json with EXACT multi-line formatting expected by Sysbox installer
    if [ ! -s "$DAEMON_JSON" ]; then
        echo "Creating $DAEMON_JSON from scratch..."
        cat <<EOF > "$DAEMON_JSON"
{
    "bip": "172.24.0.1/16",
    "default-address-pools": [
        {
            "base": "172.31.0.0/16",
            "size": 24
        }
    ]
}
EOF
        echo "File $DAEMON_JSON created with precisely formatted Sysbox network parameters."
    else
        echo "Found existing $DAEMON_JSON. Checking contents..."
        
        # Se il file esiste ma ha una sintassi JSON rotta, jq fallisce. Lo forziamo a {}.
        if ! jq -e . "$DAEMON_JSON" >/dev/null 2>&1; then
            echo "WARNING: $DAEMON_JSON is not a valid JSON. Re-initializing it..."
            echo "{}" > "$DAEMON_JSON"
        fi

        # Ora siamo sicuri che il file contiene un JSON leggibile
        HAS_BIP=$(jq 'has("bip")' "$DAEMON_JSON" 2>/dev/null)
        HAS_POOLS=$(jq 'has("default-address-pools")' "$DAEMON_JSON" 2>/dev/null)

        if [ "$HAS_BIP" != "true" ] || [ "$HAS_POOLS" != "true" ]; then
            echo "Adding 'bip' and 'default-address-pools' to existing $DAEMON_JSON..."
            cp "$DAEMON_JSON" "${DAEMON_JSON}.bak"
            
            jq --indent 4 '. + {
                "bip": "172.24.0.1/16",
                "default-address-pools": [
                    {
                        "base": "172.31.0.0/16",
                        "size": 24
                    }
                ]
            }' "$DAEMON_JSON" > "${DAEMON_JSON}.tmp" && mv "${DAEMON_JSON}.tmp" "$DAEMON_JSON"
            
            echo "Parameters successfully merged into $DAEMON_JSON."
        else
            echo "Network parameters already present in $DAEMON_JSON. Skipping modifications."
        fi
    fi

    # Proceed with installation
    SYSBOX_VERSION="0.6.7"
    DEB_FILE="/tmp/sysbox-ce_${SYSBOX_VERSION}-0.linux_amd64.deb"

    echo "Downloading Sysbox CE v${SYSBOX_VERSION}..."
    wget -qO "$DEB_FILE" "https://downloads.nestybox.com/sysbox/releases/v${SYSBOX_VERSION}/sysbox-ce_${SYSBOX_VERSION}-0.linux_amd64.deb" || { echo "Download failed!"; exit 1; }

    echo "Installing Sysbox package..."
    # The installer will find the parameters in the file and skip the daemon restart
    apt-get install -y "$DEB_FILE"

    rm -f "$DEB_FILE"

    # Make Docker digest the new runtime without stopping containers
    echo "Reloading Docker configuration (SIGHUP)..."
    systemctl reload docker

    echo "Sysbox successfully installed and ready to use!"
else
    echo "Sysbox is already installed. Skipping this step."
fi
# ---------------------------------------------------------

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
    read -p "Enter the username for the initial admin user (default: limo): " ADMIN_USER
    if [ -z "$ADMIN_USER" ]; then
        ADMIN_USER="limo"
    fi

    echo "Creating directories under: $JUPYDO_FULL_PATH"
    mkdir -p "$JUPYDO_FULL_PATH/jupyterhub_data"
    mkdir -p "$JUPYDO_FULL_PATH/jh_shared" # Crea anche la cartella per i dati condivisi

    # 2. Popola il file .env con tutte le variabili
    echo "JUPYDO_DATA_PATH=$JUPYDO_FULL_PATH/jupyterhub_data" > .env
    echo "JUPYDO_PATH=$JUPYDO_FULL_PATH" >> .env
    echo "JUPYDO_ADMIN=$ADMIN_USER" >> .env

    # 3. Copia il config così com'è (senza sed!) nella cartella dati
    cp ./jupyterhub_config.py "$JUPYDO_FULL_PATH/jupyterhub_data/jupyterhub_config.py"

    # Launching Compose
    echo "Launching Docker Compose..."
    docker compose up -d --build


    echo "JupyterHub server started."
else
    echo "You can start the JupyterHub server later using 'docker compose up -d --build'."
fi
