# Configuration file for JupyterHub
import os
from dockerspawner import DockerSpawner
import nativeauthenticator

def create_shared_dir(spawner):
    ## This hook is called when a user starts their server. 
    ## It creates a shared directory for the user and sets the appropriate permissions.
    username = spawner.user.name
    hub_shared_dir = f"/srv/jh_shared/{username}_shared"
    os.makedirs(hub_shared_dir, exist_ok=True)
    os.chown(hub_shared_dir, 1000, 100)  # Set ownership to the jovyan user (uid=1000)
    os.chmod(hub_shared_dir, 0o775) # Set permissions to rwxr-xr-x


# Define the custom spawner class
class JupyDoSpawner(DockerSpawner):

    JUPYDO_IMAGES = {
        "jupyter/r-notebook": "R Studio & Notebook",
        "jupyter/tensorflow-notebook": "TensorFlow",
        "jupyter/datascience-notebook": "Data Science (Python, R, Julia)",
        "jupyter/all-spark-notebook": "Apache Spark",
    }

    async def get_options_form(self):
        if self.user_options.get("image"):
            return ""
            
        options_html = '<option value="">-- Select a predefined stack --</option>\n'

        for image, label in self.JUPYDO_IMAGES.items():
            options_html += (
                f'<option value="{image}">{label}</option>\n'
            )

        return f"""
        <div class="form-group">
            <label for="stack">Select your desired environment:</label>
            <select name="stack" class="form-select">
                {options_html}
            </select>
        </div>

        <div class="form-group" style="margin-top:15px;">
            <label for="custom_image">
                Or use a custom Docker image:
            </label>
            <input
                type="text"
                name="custom_image"
                class="form-control"
                placeholder="myrepo/myimage:tag"
            >
        </div>
        """

    def options_from_form(self, formdata):
        selected_stack = formdata.get('stack', [''])[0].strip()
        custom_image = formdata.get('custom_image', [''])[0].strip()

        if custom_image:
            image = custom_image
        elif selected_stack:
            image = selected_stack
        else:
            raise ValueError(
                "You must select an environment or provide a custom image."
            )
        return {"image": image}

# Basic JupyterHub configuration
c = get_config()  # noqa: F821

c.JupyterHub.template_paths = [f"{os.path.dirname(nativeauthenticator.__file__)}/templates/"]

c.DockerSpawner.extra_host_config = {
}

c.JupyterHub.bind_url = 'http://:8000'
c.JupyterHub.hub_ip = '0.0.0.0'

# Use the custom spawner
c.JupyterHub.spawner_class = JupyDoSpawner
c.DockerSpawner.allowed_images = {
    image: image
    for image in JupyDoSpawner.JUPYDO_IMAGES
}

# Increase the spawner start timeout (default is 60 seconds)
c.Spawner.start_timeout = 120  # Set to 120 seconds or any desired value

# DockerSpawner configuration
c.DockerSpawner.network_name = 'jupyterhub_network'
c.JupyterHub.hub_connect_ip = 'jupyterhub'  # Use the container name of the JupyterHub instance

# Notebook directory and volumes
notebook_dir = '/home/jovyan/work'
c.DockerSpawner.notebook_dir = notebook_dir

base_path = os.environ.get('JUPYDO_PATH', '/srv/JupyDo')

c.DockerSpawner.volumes = {
    'jupyterhub-user-{username}': notebook_dir,
    f'{base_path}/jh_shared/{{username}}_shared': '/home/jovyan/work/shared'
}

c.DockerSpawner.extra_create_kwargs = {'user': 'root'}
c.DockerSpawner.pre_spawn_hook = create_shared_dir

c.DockerSpawner.environment = {
    'GRANT_SUDO': '1',
    'NOPASSWD': 'yes',    # Allow users to execute sudo commands without a password
    'CHOWN_HOME': 'yes', # Assicura che jovyan possieda la sua home anche se avviato come root
    'NB_UID': '1000',
    'NB_GID': '100'
}

# Enable named servers (still in the tests)
c.JupyterHub.allow_named_servers = True  # Allow users to create multiple named servers

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
admin = os.environ.get('JUPYDO_ADMIN', 'limo')  # Replace with your admin username
c.Authenticator.admin_users = {admin}
c.Authenticator.allow_all = True

# Failed login attempts
# Set the maximum number of failed login attempts before locking out the user
#Default value is 0 (which means no limit on failed login attempts)
c.NativeAuthenticator.allowed_failed_logins = 0

# Set timer between failed login attempts
c.NativeAuthenticator.seconds_before_next_try = 0

# Password complexity
c.NativeAuthenticator.check_common_password = True
c.NativeAuthenticator.minimum_password_length = 7



