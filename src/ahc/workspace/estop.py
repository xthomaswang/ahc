"""The emergency stop of a task folder: `.ahc/estop.json`, written by whoever presses it.

Pressing it (the dashboard's button, `ahc-estop`, or anyone writing the file) makes the MCP server
interrupt the command it is running and refuse every further action; the Claude Code plugin's hook
also ends the agent's turn at its next tool call. Only a release record clears it, and the server
holds the stop in memory, so deleting the file does not release it. A client with shell access could
still write a release; every engage and release is in the run log.

    ahc-estop             stop now (in the task folder, or --workspace)
    ahc-estop --status
    ahc-estop --release   the person only: asks for confirmation on a terminal
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from ahc.workspace.store import Workspace, resolve_workspace

FILE = "estop.json"


def path(ws: Workspace) -> Path:
  return ws.dir / FILE


def read(ws: Workspace) -> dict[str, Any] | None:
  try:
    data = json.loads(path(ws).read_text())
  except (OSError, ValueError):
    return None
  return data if isinstance(data, dict) else None


def _write(ws: Workspace, data: dict[str, Any]) -> dict[str, Any]:
  ws.dir.mkdir(parents=True, exist_ok=True)
  tmp = path(ws).with_suffix(".tmp")
  tmp.write_text(json.dumps(data, indent=2) + "\n")
  os.replace(tmp, path(ws))  # never a half-written file for the server to read
  return data


def engage(ws: Workspace, by: str, reason: str = "") -> dict[str, Any]:
  current = read(ws)
  if current and current.get("engaged"):
    return current  # already stopped: keep the first record
  return _write(ws, {"engaged": True, "id": uuid.uuid4().hex[:8], "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "by": by, "reason": reason})


def release(ws: Workspace, by: str) -> dict[str, Any] | None:
  current = read(ws)
  if not current or not current.get("engaged"):
    return current
  return _write(ws, {**current, "engaged": False, "released_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "released_by": by})


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(prog="ahc-estop", description="Emergency stop for an AHC task folder.")
  parser.add_argument("--workspace", default=None, help="task folder (default: AHC_WORKSPACE, else the current folder)")
  group = parser.add_mutually_exclusive_group()
  group.add_argument("--release", action="store_true", help="release it (asks the person to confirm on a terminal)")
  group.add_argument("--status", action="store_true")
  args = parser.parse_args(argv)
  ws = Workspace(resolve_workspace(args.workspace))
  if args.status:
    print(json.dumps(read(ws) or {"engaged": False}, indent=2))
    return
  if args.release:
    if not sys.stdin.isatty():
      sys.exit("ahc-estop: releasing needs the person at a terminal; run it yourself, not from an agent.")
    answer = input("Release the emergency stop? The deck must be safe; the agent has to check it again "
                   "before anything moves. Type 'release' to confirm: ")
    if answer.strip() != "release":
      sys.exit("Not released.")
    release(ws, by="terminal")
    print("Released. Nothing moves until the deck check passes again.")
    return
  data = engage(ws, by="terminal")
  print(f"EMERGENCY STOP engaged for {ws.root} at {data['at']}.")
