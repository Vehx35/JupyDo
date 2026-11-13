"""
JupyDo cell log, inside each user environment (loaded by the vendorized Jupyter Server,
read-only, see jupydo_celllog_config.py). Only when the administrator enabled it in
first_start.sh (JUPYDO_CELL_LOG=yes).

Every cell the user runs from JupyterLab (notebooks and consoles, any kernel language)
reaches the kernel as an "execute_request" message through the Jupyter Server: this
replaces the server's kernel websocket connection with one that also sends the code,
with the notebook, the kernel and the environment, to the hub's "celllog" service. The
service knows the user from the server's API token and writes one file per user that
only root can read (jupydo_celllog.py). The cell always runs: a failure to log never
blocks it (messages that cannot be sent are kept and sent again, up to MAX_QUEUE).
Jupyter Server 2.x.
"""
import asyncio
import collections
import json
import os
import time

from jupyter_server.services.kernels.connection.channels import ZMQChannelsWebsocketConnection
from tornado.httpclient import AsyncHTTPClient

try:
    from jupyter_server.services.kernels.connection.base import deserialize_msg_from_ws_v1
except ImportError:  # older layouts
    from jupyter_server.base.zmqhandlers import deserialize_msg_from_ws_v1  # type: ignore

URL = os.environ.get("JUPYDO_CELLLOG_URL", "http://jupyterhub:8000/services/celllog/api/cells")
TOKEN = os.environ.get("JUPYTERHUB_API_TOKEN", "")
SERVER_NAME = os.environ.get("JUPYTERHUB_SERVER_NAME", "")
MAX_QUEUE = 5000          # cells kept while the hub cannot be reached
MAX_CODE = 200 * 1024     # longer cells are cut (the log notes it)

_queue = collections.deque(maxlen=MAX_QUEUE)
_sender = None


def execute_code(subprotocol, ws_msg):
    """(code, metadata) of an execute_request sent on the shell channel, else None."""
    if subprotocol == "v1.kernel.websocket.jupyter.org":
        channel, parts = deserialize_msg_from_ws_v1(ws_msg)
        if channel != "shell" or len(parts) < 4:
            return None
        header = json.loads(parts[0])
        if header.get("msg_type") != "execute_request":
            return None
        return json.loads(parts[3]).get("code", ""), json.loads(parts[2] or b"{}")
    msg = json.loads(ws_msg) if isinstance(ws_msg, str) else None
    if not msg or msg.get("channel", "shell") != "shell":
        return None
    if (msg.get("header") or {}).get("msg_type") != "execute_request":
        return None
    return (msg.get("content") or {}).get("code", ""), msg.get("metadata") or {}


async def _send_all():
    """Send the queued cells, oldest first; on failure wait and try again."""
    global _sender
    http = AsyncHTTPClient()
    delay = 2
    try:
        while _queue:
            try:
                await http.fetch(URL, method="POST", body=json.dumps(_queue[0]), request_timeout=15,
                                 headers={"Authorization": "token " + TOKEN, "Content-Type": "application/json"})
                _queue.popleft()
                delay = 2
            except Exception:  # noqa: BLE001 - the hub may be restarting: keep the cell, try later
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)
    finally:
        _sender = None


class CellLogConnection(ZMQChannelsWebsocketConnection):
    def handle_incoming_message(self, incoming_msg):
        try:
            found = execute_code(self.subprotocol, incoming_msg)
            if found is not None:
                asyncio.ensure_future(self._log(*found))
        except Exception as e:  # noqa: BLE001 - never block the cell
            self.log.warning("JupyDo cell log: cannot read the message (%s)", e)
        super().handle_incoming_message(incoming_msg)

    async def _log(self, code, metadata):
        global _sender
        path = ""
        try:
            sm = self.websocket_handler.settings.get("session_manager")
            if sm is not None:
                path = (await sm.get_session(kernel_id=self.kernel_id)).get("path", "")
        except Exception:  # noqa: BLE001 - a console has no notebook; a missing session is fine
            pass
        km = self.kernel_manager
        _queue.append({
            "time": time.time(),
            "environment": SERVER_NAME,
            "notebook": path,
            "kernel": getattr(km, "kernel_name", ""),
            "kernel_id": self.kernel_id,
            "cell_id": metadata.get("cellId", ""),
            "truncated": len(code) > MAX_CODE,
            "code": code[:MAX_CODE],
        })
        if _sender is None:
            _sender = asyncio.ensure_future(_send_all())
