"""Hamilton: what its frames share (carriers on tracks, firmware command log)."""

from __future__ import annotations

import logging
from typing import Any

import pylabrobot.resources as plr

from ahc.core.errors import LabError
from ahc.core.layout import Layout, make_labware
from ahc.devices.base import Adapter

# PyLabRobot logs each simulated firmware command at this level, as "[simulation] write: C0AS...".
_SIM_LOGGER = "pylabrobot.hamilton.star.driver.simulator"
_LOG_LEVEL_IO = 5


class _FirmwareCounter(logging.Handler):
  def __init__(self) -> None:
    super().__init__(level=_LOG_LEVEL_IO)
    self.count = 0

  def emit(self, record: logging.LogRecord) -> None:
    if record.getMessage().startswith("[simulation] write:"):
      self.count += 1


class HamiltonAdapter(Adapter):
  """What Hamilton frames share: carriers on numbered tracks, firmware commands over USB."""

  async def start(self) -> None:
    self._counter = _FirmwareCounter()
    logger = logging.getLogger(_SIM_LOGGER)
    logger.setLevel(_LOG_LEVEL_IO)
    logger.addHandler(self._counter)

  def command_count(self) -> int:
    return self._counter.count

  def _build_deck(self, deck: Any, layout: dict[str, Any]) -> Layout:
    rules = self.spec.layout
    if set(layout) - {"carriers", "liquids", "aliases"}:
      raise LabError("bad_layout", f"unknown layout keys {sorted(set(layout) - {'carriers', 'liquids', 'aliases'})}.",
                     "A Hamilton layout has carriers (type, track, sites), liquids and aliases.")
    out = Layout(self.spec, kind="tracks")
    lo, hi = rules["tracks"]["min"], rules["tracks"]["max"]
    for carrier in layout.get("carriers", []):
      kind, track, name = carrier.get("type"), carrier.get("track"), carrier.get("name")
      if kind not in rules["carriers"]:
        raise LabError("carrier_not_allowed", f"{kind!r} is not listed for {self.spec.model}.",
                       f"Allowed carriers: {', '.join(rules['carriers'])}.")
      if not isinstance(track, int) or not lo <= track <= hi:
        raise LabError("bad_track", f"carrier {name!r}: track {track!r} is outside {lo}..{hi}.")
      holder = getattr(plr, kind)(name=name)
      for site, item in (carrier.get("sites") or {}).items():
        resource = make_labware(self.spec, "", item["type"], item["name"])
        try:
          holder[int(site)] = resource
        except Exception as exc:  # noqa: BLE001
          raise LabError("bad_site", f"carrier {name!r} site {site}: {exc}") from exc
        out.add(item["name"], resource, item["type"])
      try:
        deck.assign_child_resource(holder, track=track)
      except Exception as exc:  # noqa: BLE001 - PLR refuses overlapping carriers
        raise LabError("bad_track", f"carrier {name!r} on track {track}: {exc}") from exc
    out.set_aliases(layout.get("aliases") or {})
    out.set_liquids(layout.get("liquids") or {})
    return out
