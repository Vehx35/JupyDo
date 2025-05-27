#!/bin/bash

# first_start.sh - Initialization script

echo "Initialization script started."
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
# Creating Docker network
echo "Creating Docker network..."
docker network create --driver bridge jupyterhub_network
echo "Docker network created."

#Launch Script
echo "Initialization script completed."
echo "You can now start the JupyterHub server using 'docker-compose up'."
echo "Do you want to start the JupyterHub server now? (y/n)"
read start_jupyterhub
if [ "$start_jupyterhub" == "y" ]; then
    echo "Starting JupyterHub server..."

    echo "Inserisci la cartella parent dove creare JupyDo (lascia vuoto per default: /srv):"
    read JUPYDO_PATH
    if [ -z "$JUPYDO_PATH" ]; then
        JUPYDO_PATH="/srv"
    fi
    JUPYDO_FULL_PATH="$JUPYDO_PATH/JupyDo"

    echo "Creating directories..."
    mkdir -p "$JUPYDO_FULL_PATH"
    mkdir -p "$JUPYDO_FULL_PATH/jupyterhub_data"

    if test -d "$JUPYDO_FULL_PATH/jupyterhub_data"; then
        echo "Directory $JUPYDO_FULL_PATH/jupyterhub_data successfully created."
    else
        echo "Directory $JUPYDO_FULL_PATH/jupyterhub_data could not be created. Error. Exiting script."
        exit 1
    fi

    # Copying configuration files with path replacement
    echo "Copying configuration files..."
    sed "s|/srv/JupyDo|$JUPYDO_FULL_PATH|g" ./jupyterhub_config.py > "$JUPYDO_FULL_PATH/jupyterhub_data/jupyterhub_config.py"
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

    echo "JupyterHub server started. compose.yaml ripristinato."
else
    echo "You can start the JupyterHub server later using 'docker-compose up -d --build'."
fi


