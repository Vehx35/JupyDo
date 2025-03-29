# Configuration file for JupyterHub

c = get_config()  # noqa: F821

# Basic JupyterHub configuration
c.JupyterHub.bind_url = 'http://:8000'
c.JupyterHub.hub_ip = '0.0.0.0'

# Authenticate users with Native Authenticator
c.JupyterHub.authenticator_class = 'nativeauthenticator.NativeAuthenticator'

# Require admin approval for users to login
c.NativeAuthenticator.open_signup = False

# Allowed admins
admin = 'giovanni'
c.Authenticator.admin_users = {admin}

# Allow all signed-up users to login
c.Authenticator.allow_all = True
