# JupyDo cell log: Jupyter Server configuration, read-only in every user environment
# (in the folder named by JUPYTER_CONFIG_PATH, as jupyter_server_config.py). It only
# makes jupydo_celllog_server.py importable: the connection class is set on the server's
# command line by the hub (c.Spawner.args), which takes precedence over any user config.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
