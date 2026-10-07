"""Opentrons: what its robots share (deck slots, robot-server HTTP API)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from pylabrobot.resources import OTDeck

from ahc.core.errors import LabError
from ahc.core.layout import Layout, make_labware
from ahc.devices.base import Adapter


class OpentronsAdapter(Adapter):
  """What Opentrons robots share: numbered deck slots and the robot-server HTTP API."""

  @classmethod
  def check_options(cls, backend: str, options: dict[str, Any]) -> None:
    if backend != "sim" and not options.get("host"):  # never fall back to this machine for a real robot
      raise LabError("robot_host_required", f"the {backend!r} backend needs the robot's address.",
                     "Set options.host (and options.port if not 31950) in this device's entry in .ahc/config.yaml.")

  async def start(self) -> None:
    self.check_options(self.backend, self.options)
    self.host = self.options.get("host") or "127.0.0.1"
    self.port = int(self.options.get("port") or 31950)
    self._commands_before = 0  # from runs this session already closed

  def _get(self, path: str) -> dict[str, Any]:
    request = urllib.request.Request(f"http://{self.host}:{self.port}{path}",
                                     headers={"Opentrons-Version": "*"})
    with urllib.request.urlopen(request, timeout=10) as response:
      return json.load(response)

  def check_reachable(self) -> dict[str, Any]:
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
    """The robot's name, serial and software from /health, and what each mount carries."""
    health = self._get("/health")
    out: dict[str, Any] = {k: health.get(k) for k in ("name", "robot_serial", "robot_model", "api_version",
                                                       "fw_version", "system_version")}
    try:
      instruments = self._get("/instruments").get("data") or []
      out["pipettes"] = {i.get("mount"): {"name": i.get("instrumentName"), "model": i.get("instrumentModel"),
                                          "serial": i.get("serialNumber")} for i in instruments}
    except urllib.error.HTTPError:  # older robot software: the pipettes endpoint instead
      pipettes = self._get("/pipettes")
      out["pipettes"] = {m: {"name": p.get("name"), "model": p.get("model"), "serial": p.get("id")}
                         for m, p in pipettes.items() if isinstance(p, dict)}
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
