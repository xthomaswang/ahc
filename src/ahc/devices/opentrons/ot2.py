"""Opentrons OT-2: driven step by step through PyLabRobot 1.0 over the robot-server HTTP API."""

from __future__ import annotations

from typing import Any

from pylabrobot.resources import Container, OTDeck, TipSpot

from ahc.core.errors import LabError
from ahc.core.layout import Layout
from ahc.devices.opentrons.brand import OpentronsAdapter
from ahc.devices.spec import Component


class OT2Adapter(OpentronsAdapter):
  """An OT-2 driven step by step through PyLabRobot 1.0 over the robot-server HTTP API."""

  _EXPECTED = {"right": ("p300_multi_gen2", 8), "left": ("p20_single_gen2", 1)}

  async def start(self) -> None:
    await super().start()
    self.device = None

  async def stop(self) -> None:
    if self.device is not None:
      self._commands_before = self.command_count()
      await self.device.stop()
      self.device = None

  def command_count(self) -> int:
    if self.device is None or self.device._run is None:
      return self._commands_before
    run = self._get(f"/runs/{self.device._run.id}/commands?pageLength=1")
    return self._commands_before + int(run["meta"]["totalLength"])

  async def load_layout(self, layout: dict[str, Any]) -> Layout:
    if self.device is not None and any(self.mounted_tips(c) for c in self.spec.components.values()):
      raise LabError("tips_mounted", "cannot change the layout with tips mounted.",
                     "Call drop_tips first.")
    from pylabrobot.opentrons import OT2

    self.check_reachable()
    deck = OTDeck()
    new_layout = self._build_deck(deck, layout)
    await self.stop()
    device = OT2(host=self.host, port=self.port, deck=deck)
    await device.setup()
    if self.simulated and device._run.software_version.startswith("0.0.0"):
      # SIM ONLY: a dev robot-server has no /etc/VERSION.json and reports 0.0.0.dev0, which sends
      # PyLabRobot down its pre-7.1 fixed-trash path. A real robot reports its own release.
      device._run._software_version = "8.8.2"
    for mount, (name, channels) in self._EXPECTED.items():
      pipette = getattr(device, f"{mount}_pipette")
      if pipette is None or pipette.name != name:
        await device.stop()
        raise LabError("mount_mismatch",
                       f"{mount} mount has {getattr(pipette, 'name', None)!r}, the description file expects {name!r}.",
                       "Fix the description file or the robot before running.")
    self.device = device
    self.layout = new_layout
    return new_layout

  def view_root(self):
    return self.device.deck if self.device is not None else None

  def _pipette(self, comp: Component):
    if self.device is None:
      raise LabError("no_layout", "the OT-2 is not set up.", "Call load_layout first.")
    return getattr(self.device, f"{comp.name}_pipette")

  def mounted_tips(self, comp: Component) -> list[Any]:
    if self.device is None:
      return []
    pipette = self._pipette(comp)
    if comp.channels == 1:
      return [pipette.tip] if pipette.tip is not None else []
    return list(pipette.tips)

  async def pick_up_tips(self, comp: Component, spots: list[TipSpot]) -> str | None:
    pipette = self._pipette(comp)
    await (pipette.pick_up_tip(spots[0]) if comp.channels == 1 else pipette.pick_up_tips(spots))
    return None

  async def _liquid(self, op: str, comp, containers: list[Container], volumes, flow_rate,
                    liquid_height) -> None:
    pipette = self._pipette(comp)
    target = containers[0] if comp.channels == 1 else containers
    await getattr(pipette, op)(target, volumes[0], flow_rate=flow_rate,
                               liquid_height=liquid_height or 0)

  async def aspirate(self, comp, containers, volumes, flow_rate, liquid_height, specialized):
    await self._liquid("aspirate", comp, containers, volumes, flow_rate, liquid_height)
    return None

  async def dispense(self, comp, containers, volumes, flow_rate, liquid_height, specialized):
    await self._liquid("dispense", comp, containers, volumes, flow_rate, liquid_height)
    return None

  async def mix(self, comp, containers, volume, repetitions, flow_rate, liquid_height, specialized):
    pipette = self._pipette(comp)
    target = containers[0] if comp.channels == 1 else containers
    await pipette.mix(target, volume, repetitions, aspiration_flow_rate=flow_rate,
                      dispense_flow_rate=flow_rate, liquid_height=liquid_height or 0)
    return None

  async def drop_tips(self, comp: Component, mode: str) -> str | None:
    pipette = self._pipette(comp)
    single = comp.channels == 1
    if mode == "discard":
      await (pipette.discard_tip() if single else pipette.discard_tips())
    else:
      await (pipette.return_tip() if single else pipette.return_tips())
    return None
