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

    ahc-estop             stop now (in the task folder, or --workspace); says whether a server took it
    ahc-estop --status
    ahc-estop --release   the person only: asks for confirmation on a terminal
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
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


# -- acknowledgement: did a server take the stop? ---------------------------------------------------

def acknowledgement(ws: Workspace, record: dict[str, Any]) -> dict[str, Any] | None:
  """Whether an AHC server took this stop: a run log in this folder holds its estop entry (matched by
  id and when it was pressed; an entry written without an id, by when and by whom). With the device and
  backend that server's run log names, and what the device's own stop reported, when it did: halted,
  not_halted (nothing running, or no device-level stop) or failed. None when no server has logged it."""
  if not ws.runs_dir.is_dir():
    return None
  try:
    since = time.mktime(time.strptime(str(record.get("at")), "%Y-%m-%dT%H:%M:%S")) - 2
  except (ValueError, OverflowError):
    since = 0.0
  logs = []
  for log in ws.runs_dir.glob("*.jsonl"):
    try:
      st = log.stat()
    except OSError:
      continue
    if st.st_mtime >= since:  # a log that took the stop was written after it
      logs.append((st.st_mtime_ns, st.st_size, log))
  key = tuple(_key(record.get(k)) for k in ("id", "at", "by"))
  rank = {"failed": 0, "halted": 1, "not_halted": 2, None: 3}  # the most important outcome first
  best = None
  for mtime, size, log in sorted(logs, reverse=True)[:20]:
    found = _ack_in_log(str(log), mtime, size, key)
    if found is not None and (best is None or rank[found["halt"]] < rank[best["halt"]]):
      best = dict(found)
  return best


def _key(value: Any) -> str:
  return json.dumps(value, sort_keys=True, default=str)


def _same_press(e: dict[str, Any], key: tuple[str, str, str]) -> bool:
  """An estop entry for this press: the same id and, where the entry says, the same press time (a stop
  file AHC did not write keeps its id when the same text is written again later); an entry without an
  id, by when and by whom."""
  if "id" in e:
    return _key(e.get("id")) == key[0] and ("at" not in e or _key(e.get("at")) == key[1])
  return (_key(e.get("at")), _key(e.get("by"))) == key[1:]


@functools.lru_cache(maxsize=64)
def _ack_in_log(log: str, _mtime: int, _size: int, key: tuple[str, str, str]) -> dict[str, Any] | None:
  """The estop entry of this stop in one run log (cached until the log changes), and the halt after it."""
  found = None
  header: dict[str, Any] = {}
  try:
    lines = Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
  except OSError:
    return None
  for n, line in enumerate(lines):
    try:
      e = json.loads(line)
    except ValueError:
      continue
    if not isinstance(e, dict):
      continue
    if n == 0 and e.get("type") == "run":  # which device and backend this server drives
      header = e
    if found is None:
      if e.get("type") == "estop" and e.get("state") == "engaged" and _same_press(e, key):
        found = {"at": e.get("t"), "log": log, "halt": None, "detail": None,
                 "device": header.get("device"), "backend": header.get("backend")}
    elif e.get("type") == "audit" and e.get("event") in HALT_EVENTS:
      found.update(halt=HALT_EVENTS[e["event"]], detail=e.get("detail"))
      break
    elif e.get("type") == "estop" and e.get("state") == "engaged":  # pressed again: a later halt is that stop's
      break
  return found


def describe_ack(ack: dict[str, Any] | None, waited: float) -> str:
  if ack is None:
    return (f"Not acknowledged by any AHC server yet ({waited:.0f} s). If a run is going, stop the robot with its own "
            "controls or the power switch, and check that the server works in this folder.")
  halt = {"halted": " The device's run is stopped.",
          "not_halted": " The device reported nothing running, or it has no device-level stop.",
          "failed": " The device's stop FAILED: the robot may still be executing its last command; the power switch "
                    "is the immediate stop.",
          None: " No device stop reported (yet)."}[ack["halt"]]
  return f"Acknowledged by the AHC server{server_text(ack)} at {(ack['at'] or '').replace('T', ' ')}.{halt}"


def server_text(ack: dict[str, Any]) -> str:
  """' for opentrons.ot2 (robot)': which server took the stop, from its run log, so a simulator's is not
  taken for the robot's. Empty when the log names no device."""
  if not ack.get("device"):
    return ""
  backend = ack.get("backend")
  return f" for {ack['device']} ({'simulated' if backend == 'sim' else backend or 'backend unknown'})"


# -- ahc-estop -------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(prog="ahc-estop", description="Emergency stop for an AHC task folder.")
  parser.add_argument("--workspace", default=None, help="task folder (default: AHC_WORKSPACE, else the current folder)")
  group = parser.add_mutually_exclusive_group()
  group.add_argument("--release", action="store_true", help="release it (asks the person to confirm on a terminal)")
  group.add_argument("--status", action="store_true")
  parser.add_argument("--wait", type=float, default=3.0, help="seconds to wait for a server to acknowledge a stop")
  args = parser.parse_args(argv)
  ws = Workspace(resolve_workspace(args.workspace))
  try:
    ws.require_task_folder()  # AHC's own data folder: no server works there, so a stop there stops nothing
  except LabError as exc:
    sys.exit(f"ahc-estop: {exc.message} {exc.hint}")
  current = read(ws)
  if args.status:
    out = {"folder": str(ws.root), **(current or {"engaged": False})}
    if current and current.get("engaged"):
      out["acknowledged"] = acknowledgement(ws, current)
    print(json.dumps(out, indent=2))
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
  if not (ws.config_path.is_file() or ws.runs_dir.is_dir()):
    print("No AHC server has worked in this folder yet: is this the folder the server runs in?")
  started = time.monotonic()
  ack = acknowledgement(ws, data)
  while ack is None and time.monotonic() - started < args.wait:
    time.sleep(0.2)
    ack = acknowledgement(ws, data)
  print(describe_ack(ack, time.monotonic() - started))
