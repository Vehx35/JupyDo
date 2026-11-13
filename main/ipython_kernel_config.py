# Injected by JupyDo: loads the %%job magic in every IPython kernel.
import sys
if "/opt/jobqueue" not in sys.path:
    sys.path.append("/opt/jobqueue")
c.InteractiveShellApp.extensions.append("jobqueue_magic")
