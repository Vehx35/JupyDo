#!/bin/bash

# first_start.sh - Initialization script

# Ensure script runs as root
if [ "$EUID" -ne 0 ]; then
  exec sudo "$0" "$@"
fi

# Detect real user (even if run with sudo)
REAL_USER=${SUDO_USER:-$USER}
REAL_HOME=$(eval echo "~$REAL_USER")

echo "Detected user: $REAL_USER"
echo "Home directory: $REAL_HOME"

# Check if docker is installed
if ! command -v docker &> /dev/null; then
    echo "Docker is not installed. Please install Docker first."
    exit 1
fi

# Check if docker-compose is installed
if ! command -v docker compose &> /dev/null; then
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

if [ "$start_jupyterhub" == "y" ]; then
    echo "Starting JupyterHub server..."

# --- Scelta del percorso JupyDo con autocomplete TAB ---
while true; do
    read -e -i "$REAL_HOME" -p "Enter the parent folder where to create JupyDo (TAB for autocomplete): " JUPYDO_PATH

    # Se l'utente preme invio senza scrivere nulla → default
    if [ -z "$JUPYDO_PATH" ]; then
        JUPYDO_PATH="$REAL_HOME"
        break
    fi

    # Espansione della tilde (~)
    if [[ "$JUPYDO_PATH" == ~* ]]; then
        JUPYDO_PATH=$(eval echo "$JUPYDO_PATH")
        break
    fi

    # Se è percorso assoluto (/qualcosa) → accetta così com'è
    if [[ "$JUPYDO_PATH" == /* ]]; then
        break
    fi

    # Se è relativo (es. Documents, ./pippo) → aggancia a $REAL_HOME
    JUPYDO_PATH="$REAL_HOME/$JUPYDO_PATH"
    break
done

# Normalizzazione percorso (rimuove slash finale)
JUPYDO_PATH="${JUPYDO_PATH%/}"
JUPYDO_FULL_PATH="$JUPYDO_PATH/JupyDo"

echo "Creating directories under: $JUPYDO_FULL_PATH"
mkdir -p "$JUPYDO_FULL_PATH/jupyterhub_data"

if test -d "$JUPYDO_FULL_PATH/jupyterhub_data"; then
    echo "Directory $JUPYDO_FULL_PATH/jupyterhub_data successfully created."
else
    echo "Directory $JUPYDO_FULL_PATH/jupyterhub_data could not be created. Error. Exiting script."
    exit 1
fi

    echo "Enter the username for the initial admin user (default: limo):"
    read ADMIN_USER
    if [ -z "$ADMIN_USER" ]; then
        ADMIN_USER="limo"
    fi

    # Copying configuration files with path replacement and admin replacement
    echo "Copying configuration files..."
    sed "s|/srv/JupyDo|$JUPYDO_FULL_PATH|g; s|admin = 'limo'|admin = '$ADMIN_USER'|g" ./jupyterhub_config.py > "$JUPYDO_FULL_PATH/jupyterhub_data/jupyterhub_config.py"

    if test -f "$JUPYDO_FULL_PATH/jupyterhub_data/jupyterhub_config.py"; then
        echo "Configuration file jupyterhub_config.py successfully copied and updated."
    else
        echo "Configuration file jupyterhub_config.py could not be copied. Error. Exiting script."
        exit 1
    fi

    # Update compose.yaml with the chosen path (backup and restore at the end)
    cp compose.yaml compose.yaml.bak
    sed "s|/srv/JupyDo/jupyterhub_data|$JUPYDO_FULL_PATH/jupyterhub_data|g" compose.yaml.bak > compose.yaml

    # Launching Compose
    echo "Launching Docker Compose..."
    docker compose up -d --build

    # Restore original compose.yaml
    mv compose.yaml.bak compose.yaml
    rm -f compose.yaml.bak

    echo "JupyterHub server started."
else
    echo "You can start the JupyterHub server later using 'docker compose up -d --build'."
fi
