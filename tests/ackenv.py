"""Stop acknowledgements as other servers would have written them, for tests that need a pid or a result of their own."""

import json
import subprocess
import sys
from typing import Any

from ahc.workspace import estop
from ahc.workspace.store import Workspace

ROBOT = {"id": "ot2", "model": "opentrons.ot2", "backend": "robot", "robot": "OT2-LAB"}


def dead_pid() -> int:
  """The pid of a process that has ended."""
  proc = subprocess.Popen([sys.executable, "-c", "pass"])
  proc.wait()
  return proc.pid


def put_ack(ws: Workspace, record: dict[str, Any], pid: int, halt: str, device: dict[str, Any] | None = None,
            server: str = "abc123", **extra: Any) -> dict[str, Any]:
  folder = estop.acks_dir(ws, record["id"])
  folder.mkdir(parents=True, exist_ok=True)
  ack = {"id": record["id"], "at": record["at"], "pid": pid, "server": server, "started": "2026-10-29T10:00:00",
         "seen": "2026-10-29T10:30:00", "updated": "2026-10-29T10:30:01", "device": device, "run_active": None,
         "halt": halt, "detail": None, **extra}
  (folder / f"{pid}-{server}.json").write_text(json.dumps(ack))
  return ack
