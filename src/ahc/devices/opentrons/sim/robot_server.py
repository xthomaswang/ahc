"""Opentrons' simulated OT-2 robot-server: one base install for every task, one instance per task.

The base is the robot-server from the opentrons-ot2 repository at a pinned commit, installed once
under $AHC_HOME/sim/opentrons-ot2 (AHC_HOME defaults to ~/.ahc). Each task folder runs its own
instance from that base, with its own pipette config, state directory and port, all under
<folder>/.ahc/sim/ot2/. The instance runs in the foreground; agents start it in a background shell.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import yaml

from ahc.core.errors import LabError
from ahc.workspace.store import Workspace

REPO = "https://github.com/Opentrons/opentrons-ot2.git"
# Validated with AHC on 2026-10-01: the opentrons-ot2 HEAD of 2026-09-17, "feat(robot-server): Make
# the front button do something (#128)". To bump it, run the OT-2 tests against the new commit.
COMMIT = "a5611d1694dbd50463636fb66e6df2af1e4d66a4"
SPARSE = ("api", "shared-data", "robot-server", "server-utils", "hardware")
PYTHON = "3.10"  # what OT-2 software runs on; uv downloads it if needed
SIZE = "about 45 MB of git data and 100 MB of Python packages, about 270 MB on disk (measured 2026-10-01)"
PORTS = range(31950, 32000)
HOST = "127.0.0.1"


class SimError(Exception):
  """Something the person or agent must fix before the simulator can run."""


def ahc_home() -> Path:
  return Path(os.environ.get("AHC_HOME") or "~/.ahc").expanduser()


def base_dir() -> Path:
  return ahc_home() / "sim" / "opentrons-ot2"


def registry_path() -> Path:
  return ahc_home() / "sim" / "opentrons-ot2.json"


def ahc_sim_command() -> str:
  """This install's ahc-sim by absolute path: under a plugin it is not on PATH."""
  exe = Path(sys.executable).parent / "ahc-sim"
  return str(exe) if exe.exists() else "ahc-sim"


def setup_plan() -> str:
  """What setup downloads and where. The error hint, --dry-run and the skill all show this text."""
  return (f"The OT-2 simulator is installed once for all tasks: a sparse clone of {REPO} at commit "
          f"{COMMIT[:7]} (folders {', '.join(SPARSE)}) plus its Python {PYTHON} environment built with uv; "
          f"{SIZE}; into {base_dir()}. Command: {ahc_sim_command()} ot2 --setup")


def start_command(ws: Workspace) -> str:
  return f"{ahc_sim_command()} ot2 --workspace {ws.root}"


# -- the base install ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Base:
  robot_server: Path
  commit: str | None
  source: str  # "setup" or "existing"


def installed_base() -> Base | None:
  path = registry_path()
  if not path.is_file():
    return None
  data = json.loads(path.read_text())
  rs = Path(data["robot_server"])
  if not (rs / ".venv" / "bin" / "uvicorn").is_file():
    return None  # moved or deleted since it was registered
  return Base(rs, data.get("commit"), data.get("source", "setup"))


def _commit_of(path: Path) -> str | None:
  try:
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
  except (OSError, subprocess.SubprocessError):
    return None
  return out.stdout.strip() if out.returncode == 0 else None


def validate_robot_server(path: Path) -> tuple[Path, str | None, list[str]]:
  """An opentrons-ot2 checkout or its robot-server folder -> (robot-server dir, commit, warnings)."""
  path = Path(path).expanduser().resolve()
  rs = path / "robot-server" if (path / "robot-server").is_dir() else path
  problems = []
  if not (rs / "robot_server" / "app.py").is_file():
    problems.append("no robot_server/app.py")
  if not (rs / ".venv" / "bin" / "uvicorn").is_file():
    problems.append(f"no .venv/bin/uvicorn (run `uv sync --python {PYTHON} --no-dev` in robot-server)")
  if problems:
    raise SimError(f"{path} is not a usable OT-2 robot-server: " + "; ".join(problems) + ".")
  commit = _commit_of(rs)
  warnings = []
  if commit != COMMIT:
    warnings.append(f"this checkout is at {commit or 'an unknown commit'}; AHC is validated with {COMMIT[:7]}.")
  return rs, commit, warnings


def _register(rs: Path, commit: str | None, source: str) -> Base:
  registry_path().parent.mkdir(parents=True, exist_ok=True)
  registry_path().write_text(json.dumps({"robot_server": str(rs), "commit": commit, "source": source,
                                         "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2) + "\n")
  return Base(rs, commit, source)


def setup(existing: Path | None = None, out: Callable[[str], None] = print,
          run: Callable[..., Any] = subprocess.run) -> Base:
  """Install the base once (sparse clone + uv sync), or register an existing robot-server."""
  if existing is not None:
    rs, commit, warnings = validate_robot_server(existing)
    for w in warnings:
      out(f"warning: {w}")
    return _register(rs, commit, "existing")
  for tool in ("git", "uv"):
    if shutil.which(tool) is None:
      raise SimError(f"setup needs {tool} on PATH.")
  dest = base_dir()
  out(setup_plan())
  if not (dest / ".git").is_dir():
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--filter=blob:none", "--sparse", "--no-checkout", REPO, str(dest)], check=True)
  run(["git", "-C", str(dest), "sparse-checkout", "set", *SPARSE], check=True)
  if run(["git", "-C", str(dest), "checkout", "--quiet", "--detach", COMMIT]).returncode != 0:
    run(["git", "-C", str(dest), "fetch", "--filter=blob:none", "origin", COMMIT], check=True)
    run(["git", "-C", str(dest), "checkout", "--quiet", "--detach", COMMIT], check=True)
  env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
  run(["uv", "sync", "--python", PYTHON, "--no-dev"], cwd=str(dest / "robot-server"), env=env, check=True)
  rs, commit, warnings = validate_robot_server(dest)
  for w in warnings:
    out(f"warning: {w}")
  return _register(rs, commit, "setup")


# -- one instance per task folder ---------------------------------------------------------------

def instance_dir(ws: Workspace) -> Path:
  return ws.sim_dir / "ot2"


def settings_path(ws: Workspace) -> Path:
  return instance_dir(ws) / "server.yaml"


def read_settings(ws: Workspace) -> dict[str, Any]:
  path = settings_path(ws)
  return (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}


def robot_name(ws: Workspace) -> str:
  """Tags the instance, so a busy port can be told apart: this task's simulator, or another's."""
  return "ahc-" + hashlib.sha256(str(ws.root).encode()).hexdigest()[:10]


def health(port: int, host: str = HOST, timeout: float = 1.0) -> dict[str, Any] | None:
  request = urllib.request.Request(f"http://{host}:{port}/health", headers={"Opentrons-Version": "*"})
  try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
      return json.load(response)
  except (OSError, ValueError):
    return None


def _port_free(port: int) -> bool:
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    try:
      s.bind((HOST, port))
    except OSError:
      return False
  return True


def choose_port(ws: Workspace) -> int:
  """This task's last port if it is free, else the first free port from 31950."""
  last = read_settings(ws).get("port")
  if isinstance(last, int):
    if _port_free(last):
      return last
    answer = health(last)
    if answer and answer.get("name") == robot_name(ws):
      raise SimError(f"this task's OT-2 simulator is already running on port {last}.")
  for port in PORTS:
    if _port_free(port):
      return port
  raise SimError(f"no free port in {PORTS.start}-{PORTS.stop - 1}.")


def record_base_version(ws: Workspace, commit: str | None) -> None:
  """config.yaml keeps the base simulator version a task used (outside what a confirmation covers)."""
  from ahc.workspace.config import HEADER, parse_config  # late: config imports the device registry

  text = ws.read_config_text()
  if text is None or commit is None:
    return
  try:
    config = parse_config(text)
  except LabError:
    return  # a broken config is the server's to report, not ours to rewrite
  if config.sim.get("opentrons-ot2") != commit:
    config.sim["opentrons-ot2"] = commit
    ws.write_config(config.data(), HEADER)


def base_versions(ws: Workspace) -> dict[str, str]:
  """For a config created after the simulator started: the version that instance used."""
  commit = (read_settings(ws).get("base") or {}).get("commit")
  return {"opentrons-ot2": commit} if commit else {}


def prepare_instance(ws: Workspace, base: Base) -> tuple[list[str], dict[str, str], Path, int]:
  """Write this task's simulator settings and return how to launch it: (argv, env, cwd, port)."""
  ws.ensure()
  d = instance_dir(ws)
  (d / "state").mkdir(parents=True, exist_ok=True)
  config = d / "sim_ot2.json"
  if not config.is_file():
    config.write_text(resources.files("ahc.devices.opentrons.sim").joinpath("sim_ot2.json").read_text())
  port = choose_port(ws)
  env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
  env.update({"ENABLE_VIRTUAL_SMOOTHIE": "true",
              # Opentrons keeps settings and calibration in ~/.opentrons unless told otherwise: per task here.
              "OT_API_CONFIG_DIR": str(d / "opentrons"),
              "OT_ROBOT_SERVER_persistence_directory": str(d / "state"),
              "OT_ROBOT_SERVER_simulator_configuration_file_path": str(config),
              "DEV_ROBOT_NAME": robot_name(ws)})
  argv = [str(base.robot_server / ".venv" / "bin" / "uvicorn"), "robot_server.app:app",
          "--host", HOST, "--port", str(port), "--ws", "wsproto"]
  settings = {"port": port, "robot_name": robot_name(ws), "pipettes": str(config), "state": str(d / "state"),
              "opentrons_config": str(d / "opentrons"),
              "base": {"robot_server": str(base.robot_server), "commit": base.commit},
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
  settings_path(ws).write_text("# Written by ahc-sim each time this task's OT-2 simulator starts.\n"
                               + yaml.safe_dump(settings, sort_keys=False))
  record_base_version(ws, base.commit)
  return argv, env, base.robot_server, port


# -- what the MCP server needs ------------------------------------------------------------------

def workspace_options(ws: Workspace) -> dict[str, Any]:
  port = read_settings(ws).get("port")
  return {"host": HOST, "port": port} if isinstance(port, int) else {}


def status(ws: Workspace) -> dict[str, Any]:
  base = installed_base()
  settings = read_settings(ws)
  port = settings.get("port")
  answer = health(port) if isinstance(port, int) else None
  return {"installed": base is not None, "base": str(base.robot_server) if base else None,
          "base_commit": base.commit if base else None, "port": port,
          "running": bool(answer and answer.get("name") == robot_name(ws))}


def check_endpoint(ws: Workspace, host: str, port: int) -> None:
  """Before connecting: the simulator on this port must be this task's own instance.

  Without this, a task that never started its simulator would reach whatever answers on the
  default port, another task's simulator, and the two tasks would share one deck.
  """
  if not read_settings(ws):
    raise diagnose(ws, host, port, "this task has not started its simulator")
  answer = health(port, host)
  if answer is None:
    raise diagnose(ws, host, port, "nothing answers")
  if answer.get("name") != robot_name(ws):
    raise LabError("sim_not_running", f"port {port} answers, but it is another task's simulator ({answer.get('name')}).",
                   f"Start this task's own simulator in a background shell: {start_command(ws)}; then load the layout again.")


def diagnose(ws: Workspace, host: str, port: int, exc: Exception | str) -> LabError:
  """Why no simulator answered, and the exact next step."""
  if installed_base() is None:
    return LabError("sim_not_installed", "this task needs the OT-2 simulator, which is not installed yet.",
                    f"Tell the person what setup downloads and run it only after they agree. {setup_plan()}. "
                    f"Then start it for this task in a background shell: {start_command(ws)}")
  last = read_settings(ws).get("port")
  why = (f"this task's simulator last ran on port {port} and nothing answers now: it has exited"
         if last == port else f"no OT-2 simulator answers at {host}:{port}")
  return LabError("sim_not_running", f"{why} ({exc}).",
                  f"Start it for this task in a background shell: {start_command(ws)}; it keeps running. "
                  "Then load the layout again.")
