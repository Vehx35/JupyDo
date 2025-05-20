# Configuration file for JupyterHub
import os
from dockerspawner import DockerSpawner
import nativeauthenticator

# Define the custom spawner class
class DemoFormSpawner(DockerSpawner):
    def _options_form_default(self):
        return """
        <div class="form-group">
            <label for="stack" class="form-label">Select your desired stack:</label>
            <select name="stack" class="form-select" size="1">
                <option value="">-- Select a stack --</option>
                <option value="jupyter/r-notebook">R</option>
                <option value="jupyter/tensorflow-notebook">Tensorflow</option>
                <option value="jupyter/datascience-notebook">Datascience</option>
                <option value="jupyter/all-spark-notebook">Spark</option>
            </select>
        </div>
        <div class="form-group" style="margin-top:10px;">
            <label for="custom_image" class="form-label">Or add image and name:</label>
            <input type="text" name="custom_image" class="form-control" placeholder="e.g. myrepo/myimage:tag">
        </div>
        <br>
        """

    def options_from_form(self, formdata):
        options = {}
        selected_stack = formdata.get('stack', [''])[0]
        custom_image = formdata.get('custom_image', [''])[0].strip()
        if custom_image:
            self.image = custom_image
            options['stack'] = custom_image
        elif selected_stack:
            self.image = selected_stack
            options['stack'] = selected_stack
        else:
            raise ValueError("You must select a stack or provide a custom image.")
        return options

    def start(self):
        if 'stack' in self.user_options:
            self.image = self.user_options['stack']
        return super().start()

c.JupyterHub.template_paths = [f"{os.path.dirname(nativeauthenticator.__file__)}/templates/"]

c.DockerSpawner.extra_create_kwargs = {'user': 'root'}
c.DockerSpawner.environment = {
  'GRANT_SUDO': '1',
  'UID': '0', # workaround https://github.com/jupyter/docker-stacks/pull/420
}

# Add post-start command to set permissions
c.DockerSpawner.post_start_cmd = "sh -c 'sudo chmod 777 /home/jovyan/work/shared'"

# Basic JupyterHub configuration
c = get_config()  # noqa: F821
c.JupyterHub.bind_url = 'http://:8000'
c.JupyterHub.hub_ip = '0.0.0.0'

# Use the custom spawner
c.JupyterHub.spawner_class = DemoFormSpawner

# Increase the spawner start timeout (default is 60 seconds)
c.Spawner.start_timeout = 120  # Set to 120 seconds or any desired value

# DockerSpawner configuration
c.DockerSpawner.network_name = 'jupyterhub_network'
c.JupyterHub.hub_connect_ip = 'jupyterhub'  # Use the container name of the JupyterHub instance

# Notebook directory and volumes
notebook_dir = '/home/jovyan/work'
c.DockerSpawner.notebook_dir = notebook_dir
c.DockerSpawner.volumes = {
    'jupyterhub-user-{username}': notebook_dir,
    '$USER_HOME/JupyDo/jh_shared/{username}_shared': '/home/jovyan/work/shared'
}

# Enable named servers (still in the tests)
# c.JupyterHub.allow_named_servers = True  # Allow users to create multiple named servers

# Debugging
c.JupyterHub.log_level = 'DEBUG'

# =============================================================================
#                           Authenticator Configuration
# =============================================================================

# Authenticate users with Native Authenticator
c.JupyterHub.authenticator_class = 'nativeauthenticator.NativeAuthenticator'
c.NativeAuthenticator.open_signup = False
c.NativeAuthenticator.create_system_users = True

# Allowed admins
admin = 'limo'  # Replace with your admin username
c.Authenticator.admin_users = {admin}
c.Authenticator.allow_all = True

# Failed login attempts
# Set the maximum number of failed login attempts before locking out the user
#Default value is 0 (which means no limit on failed login attempts)
c.NativeAuthenticator.allowed_failed_logins = 0

# Set timer between failed login attempts
c.NativeAuthenticator.seconds_before_next_try = 0




