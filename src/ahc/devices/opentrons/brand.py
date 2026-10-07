"""Opentrons: what its robots share (deck slots, robot-server HTTP API)."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from pylabrobot.resources import OTDeck

from ahc.core.errors import LabError
from ahc.core.layout import Layout, make_labware
from ahc.devices.base import Adapter


class OpentronsAdapter(Adapter):
  """What Opentrons robots share: numbered deck slots and the robot-server HTTP API."""

  async def start(self) -> None:
    self.host = self.options.get("host", "127.0.0.1")
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
      return self._get("/health")
    except OSError as exc:
      diagnose = self.options.get("diagnose_unreachable")  # the simulator's own diagnosis, when simulated
      if diagnose is not None:
        raise diagnose(self.host, self.port, exc) from exc
      raise LabError("backend_unreachable", f"no robot-server at {self.host}:{self.port} ({exc}).",
                     "Check that the robot is on and reachable, or set its host and port.") from exc

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
