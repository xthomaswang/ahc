"""Opentrons: what its robots share (deck slots, robot-server HTTP API)."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pylabrobot.resources as plr
from pylabrobot.resources import OTDeck

from ahc.core.errors import LabError
from ahc.core.layout import Layout, deck_position, make_labware
from ahc.devices.base import Adapter

MOUNTS = ("left", "right")
_HOST = re.compile(r"[A-Za-z0-9.-]+")  # a hostname or an IPv4 address: no scheme, port or path

# PyLabRobot builds Opentrons tip racks from Opentrons' own labware definitions: it reads a copy in the
# temp folder, or downloads one from GitHub with no timeout. AHC puts the copy there first: from the
# OT-2 simulator's checkout when it is installed (the same version-1 definitions), else downloaded from
# PyLabRobot's pinned URL with a timeout. Blocking: callers use a thread.
DEFINITION_URL = ("https://raw.githubusercontent.com/Opentrons/opentrons/5b51a98ce736b2bb5aff780bf3fdf91941a038fa/"
                  "shared-data/labware/definitions/2/{name}/1.json")
DOWNLOAD_TIMEOUT_S = 15


def definition_cache(name: str) -> Path:
  """Where PyLabRobot reads an Opentrons definition before downloading it (its own rule)."""
  return Path("/tmp" if os.path.exists("/tmp") else "C:/Windows/Temp") / f"{name}.json"


def _readable(path: Path) -> bool:
  try:
    return json.loads(path.read_bytes())["metadata"]["displayCategory"] == "tipRack"
  except (OSError, ValueError, KeyError, TypeError):
    return False


def _simulator_copy(name: str) -> Path | None:
  from ahc.devices.opentrons.sim import robot_server  # late: the simulator module imports the workspace

  base = robot_server.installed_base()
  if base is None:
    return None
  path = base.robot_server.parent / "shared-data" / "labware" / "definitions" / "2" / name / "1.json"
  return path if path.is_file() else None


def _download(url: str, timeout: float) -> bytes:
  with urllib.request.urlopen(url, timeout=timeout) as response:
    return response.read()


def ensure_tip_rack_definitions(types: set[str]) -> None:
  """Make each Opentrons tip rack's definition readable where PyLabRobot looks, before the deck is built."""
  for name in sorted(types):
    factory = getattr(plr, name, None)
    if getattr(factory, "__module__", None) != "pylabrobot.resources.opentrons.tip_racks":
      continue  # built into PyLabRobot: nothing to fetch
    cache = definition_cache(name)
    if _readable(cache):
      continue
    local = _simulator_copy(name)
    if local is not None:
      data = local.read_bytes()
    else:
      url = DEFINITION_URL.format(name=name)
      try:
        data = _download(url, DOWNLOAD_TIMEOUT_S)
      except OSError as exc:
        raise LabError("labware_definition_unavailable",
                       f"the {name} definition is not on this computer and could not be downloaded ({exc}).",
                       f"Connect this computer to the internet once, or install the OT-2 simulator (its "
                       f"checkout has the definition); or save {url} as {cache}.") from exc
    partial = cache.with_name(f"{cache.name}.{os.getpid()}.part")
    partial.write_bytes(data)
    os.replace(partial, cache)
    if not _readable(cache):
      raise LabError("labware_definition_unavailable", f"the {name} definition at {cache} is not a tip rack definition.",
                     f"Delete {cache} and load the layout again.")


def _request(host: str, port: int, path: str, timeout: float, body: dict[str, Any] | None = None) -> dict[str, Any]:
  """One blocking robot-server request (async callers use a thread). A POST's reply is not needed: only
  its status counts, so an odd body never hides that it was accepted."""
  headers = {"Opentrons-Version": "*"}
  data = None
  if body is not None:
    data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
  request = urllib.request.Request(f"http://{host}:{port}{path}", data=data, headers=headers,
                                   method="GET" if body is None else "POST")
  with urllib.request.urlopen(request, timeout=timeout) as response:
    return json.load(response) if body is None else {}


def read_identity(host: str, port: int, timeout: float) -> dict[str, Any]:
  """The robot's name, serial and software from /health, and what each mount carries (None: empty)."""
  health = _request(host, port, "/health", timeout)
  out: dict[str, Any] = {k: health.get(k) for k in ("name", "robot_serial", "robot_model", "api_version",
                                                     "fw_version", "system_version")}
  pipettes: dict[str, Any] = {m: None for m in MOUNTS}
  try:
    for i in _request(host, port, "/instruments", timeout).get("data") or []:
      if i.get("mount") in MOUNTS and i.get("instrumentType", "pipette") == "pipette":
        pipettes[i["mount"]] = {"name": i.get("instrumentName"), "model": i.get("instrumentModel"),
                                "serial": i.get("serialNumber")}
  except urllib.error.HTTPError:  # older robot software: the pipettes endpoint instead
    pass
  if not any(pipettes.values()):  # /instruments is partial on an OT-2: /pipettes is what PyLabRobot loads
    for mount, p in _request(host, port, "/pipettes", timeout).items():
      if mount in MOUNTS and isinstance(p, dict) and p.get("name"):
        pipettes[mount] = {"name": p.get("name"), "model": p.get("model"), "serial": p.get("id")}
  out["pipettes"] = pipettes
  return out


class OpentronsAdapter(Adapter):
  """What Opentrons robots share: numbered deck slots and the robot-server HTTP API."""

  OPTIONS = {"sim": frozenset(), "robot": frozenset({"host", "port", "robot_name"})}
  _EXPECTED: dict[str, tuple[str, int]] = {}  # mount -> (pipette name, channels) the description file expects

  @classmethod
  def check_options(cls, backend: str, options: dict[str, Any]) -> None:
    super().check_options(backend, options)
    if backend == "sim":
      return
    host = options.get("host")
    if not host:  # never fall back to this machine for a real robot
      raise LabError("robot_host_required", f"the {backend!r} backend needs the robot's address.",
                     "Set options.host (and options.port if not 31950) in this device's entry in .ahc/config.yaml.")
    if not isinstance(host, str) or not _HOST.fullmatch(host):
      raise LabError("bad_option", f"options.host {host!r} is not a hostname or IP address.",
                     "Write the robot's address alone, e.g. 192.168.1.20: no http://, port or path.")
    port = options.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
      raise LabError("bad_option", f"options.port {port!r} is not a port number.",
                     "Use a whole number from 1 to 65535 (the robot-server listens on 31950), or leave it out.")
    name = options.get("robot_name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
      raise LabError("bad_option", f"options.robot_name {name!r} is not a robot name.",
                     "Use the name the robot reports (its /health name), or leave it out.")

  @classmethod
  def placement(cls, layout: Any) -> dict[str, str]:
    """'slot 2' -> the labware type in slot 2."""
    slots = layout.get("slots") if isinstance(layout, dict) else None
    return {f"slot {deck_position(slot)}": str(item.get("type") if isinstance(item, dict) else item)
            for slot, item in (slots.items() if isinstance(slots, dict) else [])}

  @classmethod
  def check_mounts(cls, pipettes: dict[str, Any]) -> dict[str, Any]:
    """Each mount against the description file: {"ok", "problems"}."""
    problems = []
    for mount, (name, _channels) in cls._EXPECTED.items():
      found = (pipettes.get(mount) or {}).get("name")
      if found != name:
        problems.append(f"{mount} mount has {found!r}, the description file expects {name!r}" if found else
                        f"{mount} mount is empty, the description file expects {name!r}")
    return {"ok": not problems, "problems": problems}

  @classmethod
  def probe_identity(cls, backend: str, options: dict[str, Any]) -> dict[str, Any]:
    """Who answers at a robot entry's address: /health and the mounts, read-only (no run, no motion)."""
    if backend == "sim":
      return {}
    host, port = options.get("host"), int(options.get("port") or 31950)
    try:
      out = read_identity(host, port, timeout=5)
    except (OSError, ValueError) as exc:  # nothing answers, or not a robot-server
      raise LabError("backend_unreachable", f"no robot-server answers at {host}:{port} ({exc}).",
                     "Check that the robot is on and on this network, and its address. A robot that is off "
                     "cannot be checked: write the config once it answers.") from exc
    out["mounts"] = cls.check_mounts(out["pipettes"])
    return out

  async def start(self) -> None:
    if self.backend != "sim" and not self.options.get("host"):  # never fall back to this machine for a real robot
      raise LabError("robot_host_required", f"the {self.backend!r} backend needs the robot's address.",
                     "Set options.host (and options.port if not 31950) in this device's entry in .ahc/config.yaml.")
    self.host = self.options.get("host") or "127.0.0.1"
    self.port = int(self.options.get("port") or 31950)

  def _get(self, path: str, timeout: float = 10) -> dict[str, Any]:
    return _request(self.host, self.port, path, timeout=timeout)

  def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
    return _request(self.host, self.port, path, timeout=10, body=body)

  def check_reachable(self) -> dict[str, Any]:
    """Blocking (async callers use a thread)."""
    check = self.options.get("check_endpoint")  # simulated: is this the task's own simulator?
    if check is not None:
      check(self.host, self.port)
    try:
      health = self._get("/health")
    except OSError as exc:
      diagnose = self.options.get("diagnose_unreachable")  # the simulator's own diagnosis, when simulated
      if diagnose is not None:
        raise diagnose(self.host, self.port, exc) from exc
      raise LabError("backend_unreachable", f"no robot-server at {self.host}:{self.port} ({exc}).",
                     "Check that the robot is on and reachable, or set its host and port.") from exc
    expected = self.options.get("robot_name")  # a lab may have several robots: drive only the one confirmed
    if expected and health.get("name") != expected:
      raise LabError("wrong_robot", f"{self.host}:{self.port} is the robot {health.get('name')!r}, not {expected!r}.",
                     "Check the robot's address, or correct options.robot_name in the config with the person.")
    return health

  def identity(self) -> dict[str, Any]:
    """The robot's name, serial and software from /health, and what each mount carries. Blocking."""
    out = read_identity(self.host, self.port, timeout=10)
    out["mounts"] = self.check_mounts(out["pipettes"])
    return out

  def _build_deck(self, deck: OTDeck, layout: dict[str, Any]) -> Layout:
    rules = self.spec.layout
    extra = set(layout) - {"slots", "liquids", "aliases"}
    if extra:
      raise LabError("bad_layout", f"unknown layout keys {sorted(extra)}.",
                     "An Opentrons layout has slots, liquids and aliases.")
    out = Layout(self.spec, kind="slots")
    lo, hi = rules["slots"]["min"], rules["slots"]["max"]
    for slot, item in (layout.get("slots") or {}).items():
      if not str(slot).isdigit() or not lo <= int(slot) <= hi:
        raise LabError("bad_slot", f"slot {slot!r} is outside {lo}..{hi}.")
      resource = make_labware(self.spec, "", item["type"], item["name"])
      deck.assign_child_at_slot(resource, slot=int(slot))
      out.add(item["name"], resource, item["type"])
    out.set_aliases(layout.get("aliases") or {})
    out.set_liquids(layout.get("liquids") or {})
    return out
