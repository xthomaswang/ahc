"""ahc-sim: the simulators a task needs. Only the OT-2 has one; the STARlet's is built into PyLabRobot.

    ahc-sim ot2 --setup [--dry-run]     install the base once (says what it downloads first)
    ahc-sim ot2 --setup <checkout>      register an opentrons-ot2 checkout that is already set up
    ahc-sim ot2                         run this task folder's simulator (foreground; Ctrl-C stops it)
    ahc-sim ot2 --status                base install, this task's port, whether it answers
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ahc.devices.opentrons.sim import robot_server as ot2
from ahc.workspace.store import Workspace, resolve_workspace


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(prog="ahc-sim", description="Run the device simulators AHC tasks use.")
  sub = parser.add_subparsers(dest="device", required=True)
  p = sub.add_parser("ot2", help="Opentrons' OT-2 robot-server with a virtual Smoothie")
  p.add_argument("--workspace", default=None, help="task folder (default: AHC_WORKSPACE, else the current folder)")
  p.add_argument("--setup", nargs="?", const="", default=None, metavar="CHECKOUT",
                 help="install the base once; with a path, register an existing opentrons-ot2 checkout instead")
  p.add_argument("--dry-run", action="store_true", help="with --setup: only say what would be downloaded")
  p.add_argument("--status", action="store_true", help="show the base install and this task's simulator")
  args = parser.parse_args(argv)
  try:
    if args.setup is not None:
      if args.dry_run:
        print(ot2.setup_plan())
        return
      base = ot2.setup(Path(args.setup) if args.setup else None)
      print(f"OT-2 simulator base ready: {base.robot_server} ({base.commit or 'unknown commit'}).")
      print("Start it for a task with `ahc-sim ot2` in that task's folder; the AHC server's hints give the full command.")
      return
    ws = Workspace(resolve_workspace(args.workspace))
    if args.status:
      print(json.dumps(ot2.status(ws), indent=2))
      return
    base = ot2.installed_base()
    if base is None:
      sys.exit("ahc-sim: the OT-2 simulator is not installed. " + ot2.setup_plan())
    command, env, cwd, port = ot2.prepare_instance(ws, base)
    print(f"OT-2 simulator for {ws.root} on http://{ot2.HOST}:{port} (Ctrl-C to stop)", flush=True)
    os.chdir(cwd)
    os.execve(command[0], command, env)  # the simulator replaces this process: one pid to stop
  except ot2.SimError as exc:
    sys.exit(f"ahc-sim: {exc}")


__all__ = ["main"]
