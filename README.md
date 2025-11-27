<div align="center">
    <img src="https://i.imgur.com/HPr8Jax.jpeg" alt="JupyDo Logo" title="JupyDo Logo" width="200">
    <h1>JupyDo</h1>
    <h3>Automated, Reproducible Bioinformatics Infrastructure</h3>

[![License](https://img.shields.io/badge/License-BSD_3--Clause-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Work_In_Progress-orange)]()
[![Docker](https://img.shields.io/badge/Container-Docker-2496ED?logo=docker)]()

</div>

---

**JupyDo** is a **JupyterHub** deployment designed to bridge the gap between interactive data analysis and reproducible research. Part of the **FAIRLab** project, it provides a fully automated, multi-user infrastructure that leverages **Docker** to deliver secure, isolated, and persistent computational environments.

JupyDo allows users to launch pre-configured bioinformatics environments or specify compatible custom Docker images, running seamlessly on both **AMD64** and **ARM** architectures.

## Key Features

### Containerized Isolation
* **Docker Integration:** Leverages standard Docker containers to create isolated workspaces for each user, preventing dependency conflicts between researchers.
* **Multi-Architecture Support:** Designed to work simultaneously on **x86_64 (AMD64)** and **ARM64** (Apple Silicon M1/M2/M3) hardware, ensuring accessibility across different server types.

### Reproducibility & Flexibility
* **Curated Stacks:** Users can choose from a list of pre-configured, frozen environments (e.g., R/Seurat, Python/TensorFlow) guaranteed to work out-of-the-box.
* **Custom Image Support:** Through the custom spawner interface, users can specify their own Docker images (must be Jupyter-compatible) to run specialized workflows.
* **Multi-Environment Interface:** Supports **JupyterLab** directly in the browser.

### Persistence & Sharing
* **Persistent User Storage:** Every user gets a dedicated persistent volume (`/home/jovyan/work`) that survives container restarts.
* **Shared Folders:** Automated setup of shared directories for collaboration between research groups.

### Easy Administration
* **Automated Setup:** Includes a `first_start.sh` wizard that handles network creation, directory structure, and configuration generation.
* **Custom Form Spawner:** A user-friendly HTML interface for selecting images and managing named servers.

## Architecture

JupyDo is built on top of:
* **JupyterHub**: The multi-user server core.
* **DockerSpawner**: For spawning user instances.
* **NativeAuthenticator**: For simple user management.

## Installation

### Prerequisites
* Linux Host (Ubuntu/Debian recommended)
* [Docker Engine](https://docs.docker.com/engine/install/) installed
* [Docker Compose](https://docs.docker.com/compose/install/) installed

### Quick Start

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/YourUsername/JupyDo.git](https://github.com/YourUsername/JupyDo.git)
    cd JupyDo
    ```

2.  **Run the Setup Wizard:**
    This script will guide you through the initial configuration (admin user, data paths, network setup).
    ```bash
    bash first_start.sh
    ```

3.  **Access the Hub:**
    Open your browser and navigate to `http://localhost:8000` (or your server's IP).

## Usage

### Starting a Server
Upon login, users are presented with a configuration form:
1.  **Select a Stack:** Choose from the list of available images.
2.  **Custom Image:** Select "Bring your own..." and enter a Docker image tag. *Note: The image must be Jupyter-compatible (contain [jupyterhub-singleuser](https://jupyterhub-dockerspawner.readthedocs.io/en/latest/docker-image.html)).*

### Managing Files
* **Private Data:** Files in `/home/jovyan/work` are private and persistent.
* **Shared Data:** Files in `/home/jovyan/work/shared` are accessible to other users (if configured).

## License

This project is licensed under the **BSD 3-Clause License**. See the [LICENSE](LICENSE) file for details.

## Authors & Acknowledgments
* **Francesco Maria Antonio Micocci** - *University of Turin*
* **Luca Alessandri** - *University of Turin*
* Developed as part of the **FAIRLab** project.

---

> **Disclaimer:** This project is currently in active development (WIP). Features and configurations are subject to change.
