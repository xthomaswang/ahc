"""Where the 3D live view is, for the dashboard: `.ahc/state/view.json`, written by the server that runs the view.

The dashboard is a separate reader (it may be the standalone ahc-dashboard or the view-only page), so it learns the
viewer's link, with its #token, from this file. The link only works on this machine (the viewer binds 127.0.0.1) and
only watches. The file names the server's pid and is ignored once that process is gone, so a crash leaves nothing
stale; a graceful shutdown removes it. `error` says why the viewer could not start (the page then says so).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ahc.workspace.store import Workspace, write_atomic


LOCAL_LINK = re.compile(r"http://(127\.0\.0\.1|localhost|\[::1\]):\d{2,5}/#token=[\w-]+")  # what Viewer3D.url looks like


def path(ws: Workspace) -> Path:
  return ws.state_dir / "view.json"


def write(ws: Workspace, url: str | None, error: str | None, real_hardware: bool) -> None:
  """Record this server's view: its link, or why there is none. Never raises: the view is a convenience."""
  try:
    ws.require_task_folder()
    ws.state_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(path(ws), json.dumps({"pid": os.getpid(), "url": url, "error": error, "real_hardware": real_hardware}))
  except Exception:  # noqa: BLE001
    pass


def remove(ws: Workspace) -> None:
  """Take this server's own record away (another server's is left alone)."""
  try:
    data = json.loads(path(ws).read_text())
    if data.get("pid") == os.getpid():
      path(ws).unlink()
  except (OSError, ValueError, AttributeError):
    pass


def read(ws: Workspace) -> dict[str, Any] | None:
  """The live view a running server of this folder serves: {"url", "error", "real_hardware"}, or None."""
  from ahc.workspace import estop

  try:
    data = json.loads(path(ws).read_text())
  except (OSError, ValueError):
    return None
  if not isinstance(data, dict) or not estop.pid_alive(data.get("pid")):
    return None
  url = data.get("url") if isinstance(data.get("url"), str) and LOCAL_LINK.fullmatch(data["url"]) else None  # this machine only
  error = data.get("error") if isinstance(data.get("error"), str) else None
  if url is None and error is None:
    return None
  return {"url": url, "error": error, "real_hardware": bool(data.get("real_hardware"))}
