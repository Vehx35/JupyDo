# Configuration file for JupyterHub
import os
from dockerspawner import DockerSpawner
import nativeauthenticator

# Define the custom spawner class
class DemoFormSpawner(DockerSpawner):
    def _options_form_default(self):
        default_stack = "jupyter/minimal-notebook"
        return """
        <label for="stack">Select your desired stack</label>
        <select name="stack" size="1">
        <option value="jupyter/r-notebook">R: </option>
        <option value="jupyter/tensorflow-notebook">Tensorflow: </option>
        <option value="jupyter/datascience-notebook">Datascience: </option>
        <option value="jupyter/all-spark-notebook">Spark: </option>
        </select>
        """.format(stack=default_stack)

    def options_from_form(self, formdata):
        options = {}
        options['stack'] = formdata['stack']
        container_image = ''.join(formdata['stack'])
        print("SPAWN: " + container_image + " IMAGE")
        self.image = container_image  # Set the selected image
        return options

c.JupyterHub.template_paths = [f"{os.path.dirname(nativeauthenticator.__file__)}/templates/"]

# Basic JupyterHub configuration
c = get_config()  # noqa: F821
c.JupyterHub.bind_url = 'http://:8000'
c.JupyterHub.hub_ip = '0.0.0.0'

# Use the custom spawner
c.JupyterHub.spawner_class = DemoFormSpawner

# DockerSpawner configuration
c.DockerSpawner.network_name = 'jupyterhub_network'
c.JupyterHub.hub_connect_ip = 'jupyterhub'  # Use the container name of the JupyterHub instance

# Notebook directory and volumes
notebook_dir = '/home/jovyan/work'
c.DockerSpawner.notebook_dir = notebook_dir
c.DockerSpawner.volumes = { 'jupyterhub-user-{username}': notebook_dir }

# Debugging
c.JupyterHub.log_level = 'DEBUG'

# Authenticate users with Native Authenticator
c.JupyterHub.authenticator_class = 'nativeauthenticator.NativeAuthenticator'
c.NativeAuthenticator.open_signup = False
c.NativeAuthenticator.create_system_users = True

# Allowed admins
admin = 'limo'  # Replace with your admin username
c.Authenticator.admin_users = {admin}
c.Authenticator.allow_all = True

# Enable named servers (commented out)
# c.JupyterHub.allow_named_servers = True  # Allow users to create multiple named servers
