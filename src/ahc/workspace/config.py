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
      # a camera may carry a slot map (written by map_camera or the dashboard, never by hand): a reference image
      # under .ahc and one box per deck slot it sees, in the reference's pixels
      #   slot_map: {reference: cameras/top/reference.jpg, size: [1280, 720], sha256: ..., view: top,
      #              slots: {"2": [x0, y0, x1, y1], "tips:ot2.right": [...]}}
    auto_verify:              # optional; the person lets the cameras answer some checks once the agent's image
      checks: [deck_matches_layout, liquid_present]   # judgments agreed with theirs min_streak times in a row
      min_streak: 5
    sim: {...}                # base simulator versions this task used, written by AHC

The server is the only writer of `confirmation`. Its digest covers devices, limits, cameras and auto_verify (cameras only when there are any, a slot map only
when a camera has one, auto_verify only when it is set, so a config without them keeps the digest it always had), so editing
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

KEYS = {"devices", "limits", "cameras", "auto_verify", "confirmation", "sim"}
DEVICE_KEYS = {"id", "model", "backend", "options"}
CAMERA_KEYS = {"name", "device", "slot_map"}
SLOT_MAP_KEYS = {"reference", "size", "slots", "sha256", "view"}
SLOT_KEY = re.compile(r"(?:[1-9]|1[0-2])|tips:[a-z][a-z0-9_]*\.[a-z0-9_]+")
VIEWS = ("top", "side")
# The checks a camera may answer by itself. The recovery checks, the config and the go-ahead, the homing go-ahead
# and the stop release are not checks of these types or are never listed: they stay with the person.
AUTO_CHECKS = ("deck_matches_layout", "tips_mounted", "liquid_present")
DEFAULT_STREAK = 5
MAX_STREAK = 1000
AUTO_KEYS = {"checks", "min_streak"}
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
  slot_map: dict[str, Any] | None = None  # reference image, size and one box per slot: see parse_slot_map

  def data(self) -> dict[str, Any]:
    """The entry as the file and the digest hold it: a slot map only when there is one."""
    out: dict[str, Any] = {"name": self.name, "device": self.device}
    if self.slot_map:
      out["slot_map"] = self.slot_map
    return out

  def sees(self) -> str | None:
    """"slots 1-9" (and regions), from the slot map; None without one."""
    return slots_text(list(self.slot_map["slots"]), self.slot_map.get("view", "top")) if self.slot_map else None


@dataclass(frozen=True)
class AutoVerify:
  """The person's authorisation: these check types may be answered by the agent from camera images (auto-verify)
  once its judgments agreed with the person's `min_streak` times in a row."""
  checks: tuple[str, ...]
  min_streak: int = DEFAULT_STREAK

  def data(self) -> dict[str, Any]:
    return {"checks": list(self.checks), "min_streak": self.min_streak}

  def summary(self) -> str:
    return f"{', '.join(self.checks)} after {self.min_streak} agreement{'' if self.min_streak == 1 else 's'}"


@dataclass
class Config:
  devices: list[DeviceEntry]
  limits: dict[str, dict[str, Any]]
  confirmation: dict[str, Any] | None = None
  sim: dict[str, Any] = field(default_factory=dict)
  cameras: list[CameraEntry] = field(default_factory=list)
  auto_verify: AutoVerify | None = None

  def digest(self) -> str:
    """What a confirmation covers: the devices, the task limits, the cameras and the auto-verify authorisation."""
    covered: dict[str, Any] = {"devices": [asdict(d) for d in self.devices], "limits": self.limits}
    if self.cameras:  # only when present: a config without cameras keeps the digest it was confirmed with
      covered["cameras"] = [c.data() for c in self.cameras]
    if self.auto_verify:  # likewise: only when set
      covered["auto_verify"] = self.auto_verify.data()
    body = json.dumps(covered, sort_keys=True)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()

  def data(self) -> dict[str, Any]:
    out: dict[str, Any] = {"devices": [_device_data(d) for d in self.devices], "limits": self.limits}
    if self.cameras:
      out["cameras"] = [c.data() for c in self.cameras]
    if self.auto_verify:
      out["auto_verify"] = self.auto_verify.data()
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
      what = "devices, limits, cameras or their slot maps" if any(c.slot_map for c in self.cameras) else (
        "devices, limits or cameras" if self.cameras else "devices or limits")
      if self.auto_verify:
        what += " or auto-verify"
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


def slots_text(keys: list[str], view: str = "top") -> str:
  """['1', '2', '3', '5', 'tips:ot2.right'] -> "slots 1-3, 5, tips:ot2.right"; a side view says so."""
  numbers = sorted(int(k) for k in keys if k.isdigit())
  runs: list[str] = []
  start = prev = None
  for n in numbers + [None]:
    if start is not None and (n is None or n != prev + 1):
      runs.append(str(start) if start == prev else f"{start}-{prev}")
      start = None
    if n is not None:
      start = n if start is None else start
      prev = n
  parts = runs + sorted(k for k in keys if not k.isdigit())
  return ("slots " if numbers else "") + ", ".join(parts) + (" (side view)" if view == "side" else "") if parts else "no slots"


def parse_slot_map(raw: Any, camera: str) -> dict[str, Any]:
  """A camera's slot map, checked and normalized (boxes rounded to 0.1 px). The reference path is always
  cameras/<camera>/reference.jpg: nothing else can be named."""
  where = f"cameras[{camera}].slot_map"
  hint = "Write slot maps with map_camera (or drag the boxes on the dashboard); never by hand."
  if not isinstance(raw, dict):
    raise invalid(f"{where} must be a mapping.", hint)
  extra = set(raw) - SLOT_MAP_KEYS
  if extra:
    raise invalid(f"{where} has unknown keys {sorted(extra)}.", hint)
  if raw.get("reference") != f"cameras/{camera}/reference.jpg":
    raise invalid(f"{where}.reference must be 'cameras/{camera}/reference.jpg'.", hint)
  size = raw.get("size")
  if (not isinstance(size, list) or len(size) != 2 or not all(isinstance(v, int) and not isinstance(v, bool) and 16 <= v <= 8192
                                                              for v in size)):
    raise invalid(f"{where}.size must be [width, height] in pixels.", hint)
  view = raw.get("view", "top")
  if view not in VIEWS:
    raise invalid(f"{where}.view must be one of {', '.join(VIEWS)}.", hint)
  sha = raw.get("sha256")
  if sha is not None and (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)):
    raise invalid(f"{where}.sha256 must be the reference image's SHA-256.", hint)
  slots = raw.get("slots")
  if not isinstance(slots, dict) or not slots:
    raise invalid(f"{where}.slots must hold at least one box.", hint)
  out_slots: dict[str, list[float]] = {}
  for key, box in slots.items():
    key = str(key)
    if not SLOT_KEY.fullmatch(key):
      raise invalid(f"{where}.slots: {key!r} is not a slot or region.", "Keys: a slot '1'..'12' or 'tips:<component>' (e.g. 'tips:ot2.right').")
    ok = (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                           and abs(v) <= 4 * max(size) for v in box))
    if not ok or box[2] - box[0] < 8 or box[3] - box[1] < 8:
      raise invalid(f"{where}.slots[{key}] must be [x0, y0, x1, y1] in the reference's pixels, at least 8 px across.",
                    "A box may reach past the frame (a slot cut off at the edge): give the labware's whole footprint.")
    out_slots[key] = [round(float(v), 1) for v in box]
  out: dict[str, Any] = {"reference": raw["reference"], "size": list(size), "view": view}
  if sha:
    out["sha256"] = sha
  out["slots"] = dict(sorted(out_slots.items(), key=lambda kv: (not kv[0].isdigit(), int(kv[0]) if kv[0].isdigit() else 0, kv[0])))
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
      raise invalid(f"cameras[{i}] has unknown keys {sorted(extra)}.", "Keys: name, device, slot_map.")
    name, device = item.get("name"), item.get("device")
    if not isinstance(name, str) or not ID.fullmatch(name):
      raise invalid(f"cameras[{i}]: name {name!r} must be lowercase letters, digits or '_', starting with a letter.")
    if not isinstance(device, str) or not device.strip():
      raise invalid(f"cameras[{i}]: device must be the camera's exact AVFoundation device name.",
                    "Ask the person for the name, or list the devices with `ffmpeg -f avfoundation -list_devices true -i \"\"`.")
    slot_map = parse_slot_map(item["slot_map"], name) if item.get("slot_map") is not None else None
    out.append(CameraEntry(name, device, slot_map))
  names = [c.name for c in out]
  if len(set(names)) != len(names):
    raise invalid(f"camera names must be unique, got {names}.")
  return out


def parse_auto_verify(raw: Any) -> AutoVerify | None:
  """auto_verify: {checks: [...], min_streak: N}. None, {} and an empty list of checks all mean off. Only the three
  check types a camera may answer are allowed; min_streak is a whole number of at least 1 (default 5)."""
  hint = ("auto_verify: {checks: [deck_matches_layout, liquid_present], min_streak: 5}. Allowed checks: "
          f"{', '.join(AUTO_CHECKS)}; min_streak a whole number from 1 (default {DEFAULT_STREAK}). The recovery checks, "
          "the config, the go-aheads and the stop release always stay with the person.")
  if raw is None or raw == {}:
    return None
  if not isinstance(raw, dict):
    raise invalid("auto_verify must be a mapping with checks and min_streak.", hint)
  extra = set(raw) - AUTO_KEYS
  if extra:
    raise invalid(f"auto_verify has unknown keys {sorted(extra)}.", hint)
  checks = raw.get("checks", [])
  if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
    raise invalid("auto_verify.checks must be a list of check names.", hint)
  bad = [c for c in checks if c not in AUTO_CHECKS]
  if bad:
    raise invalid(f"auto_verify.checks: {', '.join(repr(c) for c in bad)} may not be answered by a camera.", hint)
  streak = raw.get("min_streak", DEFAULT_STREAK)
  if isinstance(streak, bool) or not isinstance(streak, int) or not 1 <= streak <= MAX_STREAK:
    raise invalid(f"auto_verify.min_streak must be a whole number from 1 to {MAX_STREAK}, got {streak!r}.", hint)
  if not checks:
    return None
  return AutoVerify(tuple(c for c in AUTO_CHECKS if c in checks), streak)  # in a fixed order: one digest per meaning


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
                cameras=parse_cameras(data.get("cameras")), auto_verify=parse_auto_verify(data.get("auto_verify")))


def template_text() -> str:
  return resources.files("ahc.workspace").joinpath("template.yaml").read_text()


def simulation_config(model: str) -> Config:
  """The config generated without asking when the server is forced to simulate."""
  spec = load_device(model)
  config = Config(devices=[DeviceEntry(spec.short_name, model, "sim")], limits={})
  config.confirm("simulation")
  return config
