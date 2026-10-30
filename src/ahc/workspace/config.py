"""The workspace config: the task's devices, its stricter limits, and who confirmed it.

    devices:                  # a list; this prototype drives one device per server
      - id: starlet           # address prefix: starlet.pip
        model: hamilton.starlet
        backend: sim
    limits:                   # optional, may only tighten the description file's limits
      starlet.pip:
        volume_ul: {max: 200}
    confirmation: {by: human | simulation, at: ..., digest: sha256:...}   # written by the server
    cameras:                  # optional; the task's cameras, by their exact AVFoundation device name
      - {name: top, device: "USB Camera"}
    sim: {...}                # base simulator versions this task used, written by AHC

The server is the only writer of `confirmation`. Its digest covers devices, limits and cameras (cameras only when there are any, so a config without
cameras keeps the digest it always had), so editing
either by hand sends the config back to pending. That stops accidental bypass and makes any bypass
visible in the run log; it cannot stop a client that also rewrites the digest.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from importlib import resources
from typing import Any

import yaml

from ahc.core.errors import LabError
from ahc.devices.spec import known_models, load_device

KEYS = {"devices", "limits", "cameras", "confirmation", "sim"}
DEVICE_KEYS = {"id", "model", "backend", "options"}
CAMERA_KEYS = {"name", "device"}
# Where a device is: a 'sim' entry never names one, because simulation runs only on this task's own simulator.
ENDPOINT_OPTIONS = ("host", "port", "robot_name")
ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
HEADER = """\
# AHC workspace config for this task folder.
# devices: what this task drives (one per server for now). limits: stricter task limits, inside the
# description files' limits. confirmation is written by the server: change devices or limits by hand
# and the config goes back to pending until the person confirms it again (confirm_config).
"""


def invalid(message: str, hint: str | None = None) -> LabError:
  return LabError("config_invalid", message, hint or (
    "Tell the person what is wrong: they fix .ahc/config.yaml, or you write it again with configure_devices once "
    "they say what it should hold; that rewrite needs their approval (confirm_config)."))


@dataclass(frozen=True)
class DeviceEntry:
  id: str
  model: str
  backend: str
  options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CameraEntry:
  name: str  # what capture calls it: lowercase, like a device id
  device: str  # the camera's exact AVFoundation device name


@dataclass
class Config:
  devices: list[DeviceEntry]
  limits: dict[str, dict[str, Any]]
  confirmation: dict[str, Any] | None = None
  sim: dict[str, Any] = field(default_factory=dict)
  cameras: list[CameraEntry] = field(default_factory=list)

  def digest(self) -> str:
    """What a confirmation covers: the devices, the task limits and the cameras."""
    covered: dict[str, Any] = {"devices": [asdict(d) for d in self.devices], "limits": self.limits}
    if self.cameras:  # only when present: a config without cameras keeps the digest it was confirmed with
      covered["cameras"] = [asdict(c) for c in self.cameras]
    body = json.dumps(covered, sort_keys=True)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()

  def data(self) -> dict[str, Any]:
    out: dict[str, Any] = {"devices": [_device_data(d) for d in self.devices], "limits": self.limits}
    if self.cameras:
      out["cameras"] = [asdict(c) for c in self.cameras]
    if self.confirmation:
      out["confirmation"] = self.confirmation
    if self.sim:
      out["sim"] = self.sim
    return out

  def status(self, forced_sim: bool) -> tuple[str, str]:
    """('confirmed' | 'pending', why)."""
    if not self.devices:
      return "pending", "no device chosen yet"
    c = self.confirmation
    if not c:
      return "pending", "not confirmed yet"
    if c.get("digest") != self.digest():
      what = "devices, limits or cameras" if self.cameras else "devices or limits"
      return "pending", f"{what} changed after the last confirmation"
    if c.get("by") == "human":
      return "confirmed", "confirmed by the person"
    if forced_sim or all(d.backend == "sim" for d in self.devices):
      return "confirmed", "simulation only, confirmed automatically"
    return "pending", "a real backend needs the person's confirmation"

  def confirm(self, by: str) -> None:
    self.confirmation = {"by": by, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "digest": self.digest()}


def _device_data(d: DeviceEntry) -> dict[str, Any]:
  out: dict[str, Any] = {"id": d.id, "model": d.model, "backend": d.backend}
  if d.options:
    out["options"] = d.options
  return out


def parse_devices(raw: Any) -> list[DeviceEntry]:
  if raw is None:
    return []
  if not isinstance(raw, list):
    raise invalid("devices must be a list.")
  out: list[DeviceEntry] = []
  for i, item in enumerate(raw):
    if not isinstance(item, dict):
      raise invalid(f"devices[{i}] must be a mapping with id, model and backend.")
    extra = set(item) - DEVICE_KEYS
    if extra:
      raise invalid(f"devices[{i}] has unknown keys {sorted(extra)}.", "Keys: id, model, backend, options.")
    model = item.get("model")
    if model not in known_models():
      raise invalid(f"devices[{i}]: unknown model {model!r}.", f"Models: {', '.join(known_models())}.")
    spec = load_device(model)
    dev_id = item.get("id", spec.short_name)
    if not isinstance(dev_id, str) or not ID.fullmatch(dev_id):
      raise invalid(f"devices[{i}]: id {dev_id!r} must be lowercase letters, digits or '_', starting with a letter.")
    backend = item.get("backend")
    if backend not in spec.backends:
      raise invalid(f"devices[{i}]: {model} has no {backend!r} backend.",
                    f"Backends in its description file: {', '.join(spec.backends)}.")
    options = item.get("options") or {}
    if not isinstance(options, dict):
      raise invalid(f"devices[{i}]: options must be a mapping.")
    named = [k for k in ENDPOINT_OPTIONS if k in options]
    if backend == "sim" and named:
      raise invalid(f"devices[{i}]: a sim entry takes no host, port or robot_name (it names {', '.join(named)}): "
                    "the simulator is this task's own.",
                    "Drop those options from the sim entry; a real robot is the 'robot' backend, which the person confirms.")
    out.append(DeviceEntry(dev_id, model, backend, dict(options)))
  ids = [d.id for d in out]
  if len(set(ids)) != len(ids):
    raise invalid(f"device ids must be unique, got {ids}.")
  if len(out) > 1:
    raise invalid("this prototype drives one device per server; the config lists several.",
                  "Keep one entry in devices (the list format is ready for more later).")
  return out


def parse_cameras(raw: Any) -> list[CameraEntry]:
  if raw is None:
    return []
  if not isinstance(raw, list):
    raise invalid("cameras must be a list.", "Each entry: {name: top, device: \"USB Camera\"}.")
  out: list[CameraEntry] = []
  for i, item in enumerate(raw):
    if not isinstance(item, dict):
      raise invalid(f"cameras[{i}] must be a mapping with name and device.", "Each entry: {name: top, device: \"USB Camera\"}.")
    extra = set(item) - CAMERA_KEYS
    if extra:
      raise invalid(f"cameras[{i}] has unknown keys {sorted(extra)}.", "Keys: name, device.")
    name, device = item.get("name"), item.get("device")
    if not isinstance(name, str) or not ID.fullmatch(name):
      raise invalid(f"cameras[{i}]: name {name!r} must be lowercase letters, digits or '_', starting with a letter.")
    if not isinstance(device, str) or not device.strip():
      raise invalid(f"cameras[{i}]: device must be the camera's exact AVFoundation device name.",
                    "Ask the person for the name, or list the devices with `ffmpeg -f avfoundation -list_devices true -i \"\"`.")
    out.append(CameraEntry(name, device))
  names = [c.name for c in out]
  if len(set(names)) != len(names):
    raise invalid(f"camera names must be unique, got {names}.")
  return out


def parse_config(text: str) -> Config:
  try:
    data = yaml.safe_load(text) or {}
  except yaml.YAMLError as exc:
    raise invalid(f"config.yaml is not valid YAML: {exc}") from exc
  if not isinstance(data, dict):
    raise invalid("config.yaml must be a mapping.")
  extra = set(data) - KEYS
  if extra:
    raise invalid(f"unknown keys {sorted(extra)}.", f"Keys: {', '.join(sorted(KEYS))}.")
  devices = parse_devices(data.get("devices"))
  limits = data.get("limits") or {}
  if not isinstance(limits, dict):
    raise invalid("limits must be a mapping of <device>.<component> to parameter bounds.")
  confirmation = data.get("confirmation")
  if confirmation is not None and (not isinstance(confirmation, dict)
                                   or confirmation.get("by") not in ("human", "simulation")):
    raise invalid("confirmation is written by the server; remove it and confirm again.")
  sim = data.get("sim") or {}
  if not isinstance(sim, dict):
    raise invalid("sim must be a mapping.")
  return Config(devices=devices, limits=limits, confirmation=confirmation, sim=sim,
                cameras=parse_cameras(data.get("cameras")))


def template_text() -> str:
  return resources.files("ahc.workspace").joinpath("template.yaml").read_text()


def simulation_config(model: str) -> Config:
  """The config generated without asking when the server is forced to simulate."""
  spec = load_device(model)
  config = Config(devices=[DeviceEntry(spec.short_name, model, "sim")], limits={})
  config.confirm("simulation")
  return config
