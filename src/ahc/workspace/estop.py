"""The emergency stop of a task folder: `.ahc/estop.json`, written by whoever presses it.

Pressing it (the dashboard's button, `ahc-estop`, or anyone writing the file) makes the MCP server
interrupt the command it is running and refuse every further action; the Claude Code plugin's hook
also ends the agent's turn at its next tool call. Only a release record clears it, and the server
holds the stop in memory, so deleting the file does not release it.

Every reader (the server, the dashboard, ahc-estop and the plugin's hooks) reads the file the same,
fail-safe way: no file is no stop, and a JSON object whose "engaged" is false is a release record.
Anything else counts as pressed: "engaged" true in any spelling or missing, `{}`, an empty file,
`stop`, a file that is not JSON or cannot be read. Such a file is released like any other, on the
dashboard or with `ahc-estop --release`, which writes a release record over it.

Releasing is the person's: `ahc-estop --release` refuses without a terminal and when an agent runs it
(CLAUDECODE or AI_AGENT set). A client with shell access could still write a release record, and the
run log cannot tell who released the stop: it records what the release record says.

    ahc-estop             stop now (in the task folder, or --workspace); waits (--wait N) for the servers and says,
                          one line each, what they did, then whether the robot reported its run stopped
    ahc-estop --status    the same lines for a stop already pressed (--json for scripts)
    ahc-estop --release   the person only: asks for confirmation on a terminal
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from ahc.core.errors import LabError
from ahc.workspace.store import Workspace, resolve_workspace

FILE = "estop.json"
AGENT_VARS = ("CLAUDECODE", "AI_AGENT")  # set by Claude Code, and by other agents, for the commands they run
HALT_EVENTS = {"device_halted": "halted", "device_not_halted": "not_halted", "device_halt_failed": "failed"}


def path(ws: Workspace) -> Path:
  return ws.dir / FILE


def read(ws: Workspace) -> dict[str, Any] | None:
  """The stop as every reader sees it: None without a file; a release record (engaged exactly false)
  as it is; anything else as an engaged stop. A stop AHC did not write gets an id made from the file's
  bytes, so the server latches it once and a release written over it carries the same id."""
  p = path(ws)
  try:
    raw = p.read_bytes()
  except FileNotFoundError:
    return None
  except OSError as exc:  # a folder named estop.json, no permission: still a press
    return _pressed(p, b"", {}, f"cannot be read ({exc.strerror or exc})")
  try:
    data = json.loads(raw)
  except ValueError:
    return _pressed(p, raw, {}, "not JSON")
  if not isinstance(data, dict):
    return _pressed(p, raw, {}, "not a JSON object")
  engaged = data.get("engaged")
  if engaged is False or (engaged is True and data.get("id")):
    return data
  if engaged is True:
    return _pressed(p, raw, data, None)
  return _pressed(p, raw, data, '"engaged" is missing' if "engaged" not in data
                  else f'"engaged" is {json.dumps(engaged)}, not true or false')


def _pressed(p: Path, raw: bytes, data: dict[str, Any], problem: str | None) -> dict[str, Any]:
  try:
    at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime))
  except OSError:
    at = time.strftime("%Y-%m-%dT%H:%M:%S")
  record = {**data, "engaged": True, "id": data.get("id") or "file-" + hashlib.sha256(raw).hexdigest()[:8],
            "at": data.get("at") or at, "by": data.get("by") or "a file AHC did not write"}
  if problem:
    record["unreadable"] = problem
  return record


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
  """Write the release record of the engaged stop, with its id (the server releases only the stop it
  holds). None when nothing is engaged: no file, or a release record already."""
  current = read(ws)
  if current is None or current.get("engaged") is False:
    return None
  record = {k: v for k, v in current.items() if k != "unreadable"}
  return _write(ws, {**record, "engaged": False, "released_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "released_by": by})


def running_agent() -> str | None:
  """The variable that says an agent runs this process (CLAUDECODE, AI_AGENT), if one is set."""
  return next((name for name in AGENT_VARS if os.environ.get(name)), None)


# -- acknowledgements: what each server did with the stop ------------------------------------------
#
# Every AHC server that sees a stop writes its own file, .ahc/estop-acks/<stop id>/<pid>-<server>.json (atomically:
# several servers write at once, one file each, so none overwrites another), first as soon as it has latched the stop
# and again when its device answered:
#
#   id, at          the stop (its id and press time); an ack of another press is not this one's
#   pid, server     the process and the server in it (several can live in one process); started: when it began
#   seen, updated   when it saw the stop; when this file was last written
#   device          null (nothing bound), else {"id", "model", "backend", "robot": the robot's name or null}
#   run_active      true / false when the device said, null when unknown
#   halt            no_device | pending | stopped | no_run | no_stop | not_confirmed | other_run_active
#   detail          what the device or the error said

ACKS = "estop-acks"
HALT_TEXT = {
  "no_device": "no device bound: nothing to stop",
  "pending": "stopping the device's run, no answer yet",
  "stopped": "the robot reports its run stopped",
  "no_run": "no device run was active",
  "no_stop": "this device has no stop AHC can reach",
  "not_confirmed": "NOT confirmed",
  "other_run_active": "NOT stopped: another client's run replaced AHC's",
}
KEEP_ACKS_S = 7 * 86400  # acknowledgements of older stops are removed when a server writes a new one


def acks_dir(ws: Workspace, stop_id: Any) -> Path:
  return ws.dir / ACKS / (re.sub(r"[^A-Za-z0-9_.-]", "_", str(stop_id))[:64] or "_")


def write_ack(ws: Workspace, record: dict[str, Any], server: str, started: str, ack: dict[str, Any]) -> None:
  """Write (or rewrite) one server's acknowledgement of this stop, atomically. Raises OSError."""
  folder = acks_dir(ws, record.get("id"))
  folder.mkdir(parents=True, exist_ok=True)
  now = time.strftime("%Y-%m-%dT%H:%M:%S")
  data = {"id": record.get("id"), "at": record.get("at"), "pid": os.getpid(), "server": server, "started": started,
          "seen": now, **ack, "updated": now}
  target = folder / f"{os.getpid()}-{server}.json"
  try:  # keep the time it was first seen across rewrites
    data["seen"] = json.loads(target.read_text()).get("seen") or now
  except (OSError, ValueError, AttributeError):
    pass
  tmp = folder / f".{target.name}.tmp"
  tmp.write_text(json.dumps(data, indent=2) + "\n")
  os.replace(tmp, target)
  _prune_acks(folder)


def _prune_acks(keep: Path) -> None:
  try:
    for other in keep.parent.iterdir():
      if other != keep and other.is_dir() and time.time() - other.stat().st_mtime > KEEP_ACKS_S:
        shutil.rmtree(other, ignore_errors=True)
  except OSError:
    pass


def pid_alive(pid: Any) -> bool:
  if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
    return False
  try:
    os.kill(pid, 0)
  except ProcessLookupError:
    return False
  except OSError:  # PermissionError: the process exists
    return True
  return True


def acknowledgements(ws: Workspace, record: dict[str, Any]) -> list[dict[str, Any]]:
  """The servers' acknowledgements of this press, oldest first. "stale" marks one whose process is gone: what it
  wrote is the last it knew."""
  out = []
  try:
    files = sorted(acks_dir(ws, record.get("id")).glob("*.json"))
  except OSError:
    return []
  for f in files:
    try:
      ack = json.loads(f.read_text())
    except (OSError, ValueError):
      continue
    if not isinstance(ack, dict) or ack.get("id") != record.get("id") or ack.get("at") != record.get("at"):
      continue  # another press of the same stop file
    ack["stale"] = not pid_alive(ack.get("pid"))
    out.append(ack)
  return sorted(out, key=lambda a: (str(a.get("seen")), str(a.get("pid")), str(a.get("server"))))


def _who(ack: dict[str, Any]) -> str:
  device = ack.get("device")
  if not device:
    return f"pid {ack.get('pid')}"
  robot = f" {device['robot']!r}" if device.get("robot") else ""
  return f"pid {ack.get('pid')} ({device.get('id')}: {device.get('model')}, {device.get('backend')}{robot})"


def describe_server(ack: dict[str, Any]) -> str:
  """One line for a server: who it is, whether it is still alive, and what its device did."""
  halt = ack.get("halt")
  text = HALT_TEXT.get(halt, f"unknown result {halt!r}")
  if halt in ("not_confirmed", "other_run_active") and ack.get("detail"):
    text += f": {ack['detail']}"
  notes = ([f"up since {str(ack['started'])[11:19]}"] if ack.get("started") else []) \
    + (["STALE: that process is gone, this is the last it wrote"] if ack.get("stale") else [])
  device = ack.get("device")
  if device:
    bound = f"{device.get('id')} {device.get('model')}/{device.get('backend')}" + (f" {device['robot']!r}" if device.get("robot") else "")
    run = {True: ", run active", False: ", no run active"}.get(ack.get("run_active"), "")
    text = f"{bound}{run}: {text}"
  return f"pid {ack.get('pid')}" + (f" ({', '.join(notes)})" if notes else "") + f": {text}"


def verdict(acks: list[dict[str, Any]], waited: float) -> dict[str, str]:
  """What the person may rely on: {"state": stopped | nothing_to_stop | not_confirmed, "text"}. A robot counts as
  stopped only when a server says the robot reported its run stopped and no server left a question open."""
  problems = []
  for a in acks:
    halt, who = a.get("halt"), _who(a)
    if halt == "pending":
      problems.append(f"{who} " + ("ended before its device answered" if a.get("stale") else "is still stopping the device's run"))
    elif halt == "not_confirmed":
      problems.append(f"{who} could not confirm the stop ({a.get('detail') or 'no reason given'})")
    elif halt == "other_run_active":
      problems.append(f"{who} found another client's run on the robot, which AHC does not stop")
    elif halt == "no_stop" and (a.get("device") or {}).get("backend") != "sim":
      problems.append(f"{who} has no stop AHC can reach")
    elif halt not in HALT_TEXT:
      problems.append(f"{who} reported {halt!r}")
  if not acks:
    problems.append(f"no AHC server has acknowledged the stop ({waited:.0f} s)")
  if problems:
    return {"state": "not_confirmed", "text": "NOT confirmed: " + "; ".join(problems) + "; the power switch is the immediate stop"}
  if any(a.get("halt") == "stopped" for a in acks):
    simulated = all((a.get("device") or {}).get("backend") == "sim" for a in acks if a.get("halt") == "stopped")
    return {"state": "stopped", "text": "the simulator reports its run stopped (no real robot)" if simulated
            else "the robot reports its run stopped"}
  return {"state": "nothing_to_stop", "text": "no AHC server has a device run to stop; if a robot is still moving it is not "
          "under AHC, and the power switch is the immediate stop"}


def report(acks: list[dict[str, Any]], waited: float) -> list[str]:
  """The lines ahc-estop prints: one per server, then the verdict."""
  lines = [f"  {describe_server(a)}" for a in acks] or ["  no server has acknowledged yet"]
  return [f"Servers that took the stop ({len(acks)}):" if acks else "Servers:", *lines,
          "Verdict: " + verdict(acks, waited)["text"] + "."]


def wait_for_acks(ws: Workspace, record: dict[str, Any], wait: float, settle: float = 1.0) -> list[dict[str, Any]]:
  """Poll until at least one server answered, none of the live ones is still stopping its device, and nothing
  changed for `settle` seconds (a slower server may still be about to write), or `wait` seconds have passed.
  Servers with nothing bound acknowledge at once, so they never hold this up."""
  started = last = time.monotonic()
  seen = None
  while True:
    acks = acknowledgements(ws, record)
    key = [(a.get("server"), a.get("halt"), a.get("updated")) for a in acks]
    now = time.monotonic()
    if key != seen:
      seen, last = key, now
    waiting = any(a.get("halt") == "pending" and not a["stale"] for a in acks)
    if (acks and not waiting and now - last >= settle) or now - started >= wait:
      return acks
    time.sleep(0.1)


# -- ahc-estop -------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(prog="ahc-estop", description="Emergency stop for an AHC task folder.")
  parser.add_argument("--workspace", default=None, help="task folder (default: AHC_WORKSPACE, else the current folder)")
  group = parser.add_mutually_exclusive_group()
  group.add_argument("--release", action="store_true", help="release it (asks the person to confirm on a terminal)")
  group.add_argument("--status", action="store_true", help="show the stop and what each server did with it")
  parser.add_argument("--json", action="store_true", help="with --status: the same as JSON")
  parser.add_argument("--wait", type=float, default=3.0, help="seconds to wait for the servers to acknowledge a stop")
  args = parser.parse_args(argv)
  ws = Workspace(resolve_workspace(args.workspace))
  try:
    ws.require_task_folder()  # AHC's own data folder: no server works there, so a stop there stops nothing
  except LabError as exc:
    sys.exit(f"ahc-estop: {exc.message} {exc.hint}")
  current = read(ws)
  if args.status:
    engaged = bool(current and current.get("engaged"))
    acks = acknowledgements(ws, current) if engaged else []
    if args.json:
      out = {"folder": str(ws.root), **(current or {"engaged": False})}
      if engaged:
        out.update(servers=acks, verdict=verdict(acks, 0))
      print(json.dumps(out, indent=2))
      return
    if not engaged:
      since = f" (released {str(current['released_at']).replace('T', ' ')} by {current.get('released_by')})" \
        if current and current.get("released_at") else ""
      print(f"No emergency stop is engaged for {ws.root}{since}.")
      return
    print(f"EMERGENCY STOP engaged for {ws.root} since {current.get('at')} ({current.get('by')}).")
    print("\n".join(report(acks, _since(current))))
    return
  if args.release:
    if current is None or current.get("engaged") is False:
      sys.exit(f"ahc-estop: no emergency stop is engaged for {ws.root}: nothing to release.")
    agent = running_agent()
    if agent or not sys.stdin.isatty():
      sys.exit("ahc-estop: releasing needs the person at a terminal; run it yourself, not from an agent"
               + (f" ({agent} is set)." if agent else "."))
    answer = input(f"Release the emergency stop for {ws.root}? The deck must be safe; the agent has to check it "
                   "again before anything moves. Type 'release' to confirm: ")
    if answer.strip() != "release":
      sys.exit("Not released.")
    try:
      released = release(ws, by="terminal")
    except OSError as exc:
      sys.exit(f"ahc-estop: could not write the release record: {exc}")
    if released is None:
      sys.exit(f"ahc-estop: no emergency stop is engaged for {ws.root}: nothing to release.")
    print("Released. Nothing moves until the deck check passes again.")
    return
  if current and current.get("engaged"):
    data = current
    print(f"EMERGENCY STOP already engaged for {ws.root} since {data.get('at')} ({data.get('by')}).")
  else:
    data = engage(ws, by="terminal")
    print(f"EMERGENCY STOP engaged for {ws.root} at {data['at']}.")
  acks = wait_for_acks(ws, data, args.wait)
  if not acks and not (ws.config_path.is_file() or ws.runs_dir.is_dir()):
    print("No AHC server has worked in this folder yet: is this the folder the server runs in?")
  print("\n".join(report(acks, _since(data))))


def _since(record: dict[str, Any]) -> float:
  """Seconds since the stop was pressed."""
  try:
    return max(0.0, time.time() - time.mktime(time.strptime(str(record.get("at")), "%Y-%m-%dT%H:%M:%S")))
  except (ValueError, OverflowError):
    return 0.0
