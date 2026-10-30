"""Hamilton STARlet: components mapped to PyLabRobot's STAR driver, general ops translated."""

from __future__ import annotations

import re
from typing import Any

import pylabrobot.resources as plr
from pylabrobot.hamilton.star.liquid_classes.mapping import get_star_liquid_class
from pylabrobot.lib.liquid_handling.mix import Mix
from pylabrobot.resources import Container, TipSpot
from pylabrobot.resources.liquid import Liquid

from ahc.core.errors import LabError
from ahc.core.layout import Layout
from ahc.devices.hamilton.brand import HamiltonAdapter
from ahc.devices.spec import Component

# PyLabRobot's STAR model refuses a dispense its piston cannot make; said as what to change instead.
_PISTON = re.compile(r"piston holds ([\d.]+) uL of travel for the ([\d.]+) uL")


async def _piston_checked(call):
  try:
    return await call
  except ValueError as exc:
    m = _PISTON.search(str(exc))
    if m is None:
      raise
    have, need = float(m.group(1)), float(m.group(2))
    raise LabError("piston_travel",
                   f"the channel's piston has {have:g} uL of travel left and this dispense needs {need:g} uL: on the "
                   "STAR a dispense also pushes out the tip's transport air and a liquid-class correction.",
                   f"When one aspiration feeds several dispenses, aspirate about {need - have + 5:.0f} uL more than "
                   "they add up to and dispense the rest to waste, or aspirate once per dispense.") from exc


# Specialized parameter -> PyLabRobot STAR keyword, one value per channel.
_PER_CHANNEL = {
  "jet": "jet",
  "blow_out": "blow_out",
  "pre_wetting_volume_ul": "pre_wetting_volumes",
  "settling_time_s": "settling_times",
  "transport_air_volume_ul": "transport_air_volumes",
  "swap_speed_mm_s": "swap_speeds",
}


class STARletAdapter(HamiltonAdapter):
  """A STARlet driven through PyLabRobot 1.0 (simulation only in this prototype)."""

  OPTIONS = {"sim": frozenset()}
  MIX_FLOW_RATE = 100.0  # uL/s: PyLabRobot's mix needs one, so AHC chooses it when a call gives none

  @classmethod
  def _refuse_real(cls, backend: str) -> None:
    # The driver below is always PyLabRobot's simulator: a real backend lands together with its USB branch.
    if backend != "sim":
      raise LabError("backend_not_implemented", f"the STARlet's {backend!r} backend is not implemented yet.",
                     "Use backend 'sim'. A real STARlet needs the USB backend, which this prototype does not have yet.")

  @classmethod
  def check_options(cls, backend: str, options: dict[str, Any]) -> None:
    cls._refuse_real(backend)
    super().check_options(backend, options)

  async def start(self) -> None:
    self._refuse_real(self.backend)
    await super().start()
    self.device = None

  async def stop(self) -> None:
    if self.device is not None:
      await self.device.stop()
      self.device = None

  async def load_layout(self, layout: dict[str, Any]) -> Layout:
    if self.device is not None and self.mounted_tips(self.spec.components["pip"]):
      raise LabError("tips_mounted", "cannot change the layout with tips on the channels.",
                     "Call drop_tips first.")
    from pylabrobot.hamilton.star import STARlet

    deck = plr.STARLetDeck()
    new_layout = self._build_deck(deck, layout)
    await self.stop()
    self.layout = None  # the old deck is gone
    device = STARlet(deck=deck, simulation=True)
    self._count_commands(device.driver)
    try:
      await device.setup()
    except Exception:
      try:  # release what setup opened; the setup error is the one to report
        await device.stop()
      except Exception:  # noqa: BLE001
        pass
      raise
    self.device = device  # only a device that set up: a failed load leaves none behind
    self.outcome_unknown = None
    self.layout = new_layout
    return new_layout

  def view_root(self):
    if self.device is not None:
      # Drawing only: the deck's size_z is the 900 mm working envelope; the top of the X-arm riding
      # above it (334.7 mm channel travel + 140 mm arm) is its visible height. PyLabRobot's own 3D
      # demo applies the same correction.
      self.device.deck._size_z = self.device.deck._local_size_z = 334.7 + 140.0
    return self.device

  def _pip(self, comp: Component):
    if self.device is None:
      raise LabError("no_layout", "the STARlet is not set up.", "Call load_layout first.")
    if comp.name != "pip":
      raise LabError("not_supported", f"{comp.name} is not wired in this prototype.")
    return self.device.pipettes

  def mounted_tips(self, comp: Component) -> list[Any]:
    if self.device is None:
      return []
    pip = self._pip(comp)
    return [t for t in (pip.get_mounted_tip(ch) for ch in range(pip.num_channels)) if t is not None]

  async def pick_up_tips(self, comp: Component, spots: list[TipSpot]) -> str | None:
    await self._pip(comp).pick_up_tips(spots)
    return None

  def transport_air(self, op: str, comp: Component, tips: list[Any], specialized: dict[str, Any]) -> list[float]:
    """The transport air each channel draws after this call's liquid: the one given, else its liquid class's,
    looked up as PyLabRobot does (the tip, water, jet and blow-out)."""
    given = specialized.get("transport_air_volume_ul")
    if given is not None:
      return [float(given)] * len(tips)
    attribute = "dispense_air_transport_volume" if op == "dispense" else "aspiration_air_transport_volume"
    out = []
    for tip in tips:
      found = get_star_liquid_class(tip_volume=tip.maximal_volume, is_core=False, is_tip=True,
                                    has_filter=tip.has_filter, liquid=Liquid.WATER,
                                    jet=bool(specialized.get("jet")), blow_out=bool(specialized.get("blow_out")))
      out.append(getattr(found, attribute) if found is not None else 0.0)  # no class: PyLabRobot refuses the call
    return out

  def _options(self, n: int, flow_rate, liquid_height, specialized: dict[str, Any]) -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if flow_rate is not None:
      kw["flow_rates"] = [flow_rate] * n
    if liquid_height is not None:
      kw["liquid_heights"] = [liquid_height] * n
    for key, value in specialized.items():
      if key in _PER_CHANNEL:
        kw[_PER_CHANNEL[key]] = [value] * n
    return kw

  async def aspirate(self, comp, containers: list[Container], volumes, flow_rate, liquid_height,
                     specialized) -> str | None:
    kw = self._options(len(containers), flow_rate, liquid_height, specialized)
    await self._pip(comp).aspirate(containers, volumes=volumes, **kw)
    return None

  async def dispense(self, comp, containers: list[Container], volumes, flow_rate, liquid_height,
                     specialized) -> str | None:
    kw = self._options(len(containers), flow_rate, liquid_height, specialized)
    mix_volume = specialized.get("post_mix_volume_ul")
    mix_reps = specialized.get("post_mix_repetitions")
    if (mix_volume is None) != (mix_reps is None):
      raise LabError("paired_params", "post_mix_volume_ul and post_mix_repetitions go together.",
                     "Pass both, or neither.")
    note = None
    if mix_volume is not None:
      mix_flow = flow_rate if flow_rate is not None else self.MIX_FLOW_RATE
      kw["post_mixes"] = [Mix(volume=mix_volume, repetitions=mix_reps, flow_rate=mix_flow)] * len(containers)
      source = "the dispense's flow rate" if flow_rate is not None else "AHC's mix speed: no flow_rate given"
      note = (f"native post-dispense mix: {mix_reps} x {mix_volume:g} uL at {mix_flow:g} uL/s ({source}) in the "
              "same firmware command")
    await _piston_checked(self._pip(comp).dispense(containers, volumes=volumes, **kw))
    return note

  async def mix(self, comp, containers: list[Container], volume, repetitions, flow_rate,
                liquid_height, specialized) -> str | None:
    # One firmware command: an aspirate of 0 uL whose pre-mix does the cycles. Separate
    # aspirate/dispense cycles could be refused half-way (a 0.1 uL liquid-class rounding between
    # the two directions), leaving liquid in the tips.
    n = len(containers)
    kw = self._options(n, None, liquid_height, {})
    flow = flow_rate if flow_rate is not None else self.MIX_FLOW_RATE
    cycles = Mix(volume=volume, repetitions=repetitions, flow_rate=flow)
    await _piston_checked(self._pip(comp).aspirate(containers, volumes=[0.0] * n, pre_mixes=[cycles] * n, **kw))
    return (f"STAR channels have no standalone mix: ran {repetitions} x {volume:g} uL at {flow:g} uL/s as the "
            "pre-mix of a 0 uL aspirate, one firmware command; dispense.post_mix_* mixes right after a dispense")

  async def drop_tips(self, comp: Component, mode: str) -> str | None:
    pip = self._pip(comp)
    await (pip.discard_tips() if mode == "discard" else pip.return_tips())
    return None

  async def invoke(self, comp: Component, op: str, params: dict[str, Any]) -> dict[str, Any]:
    if op == "sense_tip_presence":
      return {"tip_presence_per_channel": list(await self._pip(comp).sense_tip_presence())}
    return await super().invoke(comp, op, params)

  async def sense(self, comp: Component, check: str) -> dict[str, Any] | None:
    if check == "tips_mounted" and self.device is not None:
      return {"tip_presence_per_channel": list(await self._pip(comp).sense_tip_presence())}
    return None
