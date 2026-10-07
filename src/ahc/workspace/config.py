"""The workspace config: the task's devices, its stricter limits, and who confirmed it.

    devices:                  # a list; this prototype drives one device per server
      - id: starlet           # address prefix: starlet.pip
        model: hamilton.starlet
        backend: sim
    limits:                   # optional, may only tighten the description file's limits
      starlet.pip:
        volume_ul: {max: 200}
    confirmation: {by: human | simulation, at: ..., digest: sha256:...}   # written by the server
    sim: {...}                # base simulator versions this task used, written by AHC

The server is the only writer of `confirmation`. Its digest covers devices and limits, so editing
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

KEYS = {"devices", "limits", "confirmation", "sim"}
DEVICE_KEYS = {"id", "model", "backend", "options"}
ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
HEADER = """\
# AHC workspace config for this task folder.
# devices: what this task drives (one per server for now). limits: stricter task limits, inside the
# description files' limits. confirmation is written by the server: change devices or limits by hand
# and the config goes back to pending until the person confirms it again (confirm_config).
"""


def invalid(message: str, hint: str | None = None) -> LabError:
  return LabError("config_invalid", message, hint or "Fix .ahc/config.yaml, or rewrite it with configure_devices.")


@dataclass(frozen=True)
class DeviceEntry:
  id: str
  model: str
  backend: str
  options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
  devices: list[DeviceEntry]
  limits: dict[str, dict[str, Any]]
  confirmation: dict[str, Any] | None = None
  sim: dict[str, Any] = field(default_factory=dict)

  def digest(self) -> str:
    """What a confirmation covers: the devices and the task limits."""
    body = json.dumps({"devices": [asdict(d) for d in self.devices], "limits": self.limits}, sort_keys=True)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()

  def data(self) -> dict[str, Any]:
    out: dict[str, Any] = {"devices": [_device_data(d) for d in self.devices], "limits": self.limits}
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
      return "pending", "devices or limits changed after the last confirmation"
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
    out.append(DeviceEntry(dev_id, model, backend, dict(options)))
  ids = [d.id for d in out]
  if len(set(ids)) != len(ids):
    raise invalid(f"device ids must be unique, got {ids}.")
  if len(out) > 1:
    raise invalid("this prototype drives one device per server; the config lists several.",
                  "Keep one entry in devices (the list format is ready for more later).")
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
  return Config(devices=devices, limits=limits, confirmation=confirmation, sim=sim)


def template_text() -> str:
  return resources.files("ahc.workspace").joinpath("template.yaml").read_text()


def simulation_config(model: str) -> Config:
  """The config generated without asking when the server is forced to simulate."""
  spec = load_device(model)
  config = Config(devices=[DeviceEntry(spec.short_name, model, "sim")], limits={})
  config.confirm("simulation")
  return config
