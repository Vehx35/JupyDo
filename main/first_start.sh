#!/bin/bash

# first_start.sh - Initialization script

echo "Initialization script started."

# Check if the script is run as root
if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root. Please use sudo."
    exit 1
fi
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

    echo "Creating directories..."
    mkdir -p $USER_HOME/JupyDo
    mkdir -p $USER_HOME/JupyDo/jupyterhub_data

    if test -d $USER_HOME/JupyDo/jupyterhub_data; then
        echo "Directory $USER_HOME/JupyDo/jupyterhub_data successfully created."
    else
        echo "Directory $USER_HOME/JupyDo/jupyterhub_data could not be created. Error. Exiting script."
        exit 1
    fi

    # Copying configuration files
    echo "Copying configuration files..."
    cp -r ./jupyterhub_config.py $USER_HOME/JupyDo/jupyterhub_data/
    if test -f $USER_HOME/JupyDo/jupyterhub_data/jupyterhub_config.py; then
        echo "Configuration file jupyterhub_config.py successfully copied."
    else
        echo "Configuration file jupyterhub_config.py could not be copied. Error. Exiting script."
        exit 1
    fi

    # Launching Compose
    echo "Launching Docker Compose..."
    docker compose up -d --build

    echo "JupyterHub server started."
else
    echo "You can start the JupyterHub server later using 'docker-compose up -d --build'."
fi


