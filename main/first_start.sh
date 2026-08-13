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
