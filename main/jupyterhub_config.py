# Configuration file for JupyterHub
import os, nativeauthenticator
c.JupyterHub.template_paths = [f"{os.path.dirname(nativeauthenticator.__file__)}/templates/"]
c = get_config()  # noqa: F821

# Basic JupyterHub configuration
c.JupyterHub.bind_url = 'http://:8000'
c.JupyterHub.hub_ip = '0.0.0.0'

# Possibility to enable named servers
#c.JupyterHub.allow_named_servers = True

# Authenticate users with Native Authenticator
c.JupyterHub.authenticator_class = 'nativeauthenticator.NativeAuthenticator'

# Require admin approval for users to login
c.NativeAuthenticator.open_signup = False

# Allowed admins
admin = 'limo'  # Replace with your admin username
c.Authenticator.admin_users = {admin} # For some reason, admin user is not being allowed by default

# Allow all signed-up users to login
c.Authenticator.allow_all = True
