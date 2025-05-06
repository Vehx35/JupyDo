# Configuration file for JupyterHub
import os
from dockerspawner import DockerSpawner
import nativeauthenticator

# Define the custom spawner class
class DemoFormSpawner(DockerSpawner):
    def _options_form_default(self):
        # Check if the user has already selected an image
        if self.user_options.get('image_selected', False):
            return ""  # No form is shown if the image is already selected

        # Show the form for selecting an image with JupyterHub's theme and an intuitive arrow
        return """
        <div class="form-group">
            <label for="stack" class="form-label">Select your desired stack:</label>
            <select name="stack" class="form-select" size="1">
                <option value="">-- Select an image --</option>
                <option value="jupyter/r-notebook">R</option>
                <option value="jupyter/tensorflow-notebook">Tensorflow</option>
                <option value="jupyter/datascience-notebook">Datascience</option>
                <option value="jupyter/all-spark-notebook">Spark</option>
            </select>
        </div>
        <br>
        """

    def options_from_form(self, formdata):
        options = {}
        # Get the selected stack from the dropdown
        selected_stack = formdata.get('stack', [''])[0]

        # Ensure a stack is selected
        if not selected_stack:
            raise ValueError("You must select a stack to proceed.")

        print("SPAWN: " + selected_stack + " IMAGE")
        self.image = selected_stack  # Set the selected image

        # Store the selected image in user options to avoid asking again
        options['image_selected'] = True
        options['stack'] = selected_stack
        return options

    def start(self):
        # Use the previously selected image if available
        if 'stack' in self.user_options:
            self.image = self.user_options['stack']
        return super().start()

c.JupyterHub.template_paths = [f"{os.path.dirname(nativeauthenticator.__file__)}/templates/"]

# Basic JupyterHub configuration
c = get_config()  # noqa: F821
c.JupyterHub.bind_url = 'http://:8000'
c.JupyterHub.hub_ip = '0.0.0.0'

# Use the custom spawner
c.JupyterHub.spawner_class = DemoFormSpawner

# DockerSpawner configuration
c.DockerSpawner.network_name = 'main_jupyterhub_network'
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

# Enable named servers (still in the tests)
# c.JupyterHub.allow_named_servers = True  # Allow users to create multiple named servers
