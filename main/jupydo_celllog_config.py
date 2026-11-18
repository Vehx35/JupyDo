# JupyDo cell log: Jupyter Server configuration, read-only in every user environment
# (in the folder named by JUPYTER_CONFIG_PATH, as jupyter_server_config.py): it makes
# jupydo_celllog_server.py importable and uses its kernel connection, which also sends
# every cell the user runs to the hub (see that file). Set here and not on the command
# line: the command line is checked before this file runs, when the module is not yet
# importable (and PYTHONPATH would override the one some images need).
# With a Jupyter Server it does not know (e.g. an image used "as it is"), the cell log
# simply stays off: the server must start anyway.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import jupydo_celllog_server  # noqa: F401
    c.ServerApp.kernel_websocket_connection_class = "jupydo_celllog_server.CellLogConnection"  # noqa: F821
except Exception as e:  # noqa: BLE001
    print(f"JupyDo cell log: not active with this Jupyter Server ({e})", file=sys.stderr)
