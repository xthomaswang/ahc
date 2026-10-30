"""Hamilton: what its frames share (carriers on tracks, firmware command log)."""

from __future__ import annotations

from typing import Any, Callable

import pylabrobot.resources as plr

from ahc.core.errors import LabError
from ahc.core.layout import Layout, deck_position, make_labware
from ahc.devices.base import Adapter


def _counting(send: Callable, count: Callable[[], None]) -> Callable:
  async def counted(*args, **kwargs):
    count()
    return await send(*args, **kwargs)
  return counted


class HamiltonAdapter(Adapter):
  """What Hamilton frames share: carriers on numbered tracks, firmware commands over USB."""

  @classmethod
  def placement(cls, layout: Any) -> dict[str, str]:
    """'track 7' -> the carrier type on track 7, 'track 7 site 0' -> the labware type on its site 0."""
    out: dict[str, str] = {}
    carriers = layout.get("carriers") if isinstance(layout, dict) else None
    for carrier in carriers if isinstance(carriers, list) else []:
      if not isinstance(carrier, dict):
        continue
      track = f"track {deck_position(carrier.get('track'))}"
      out[track] = str(carrier.get("type"))
      sites = carrier.get("sites")
      for site, item in (sites.items() if isinstance(sites, dict) else []):
        out[f"{track} site {deck_position(site)}"] = str(item.get("type") if isinstance(item, dict) else item)
    return out

  async def start(self) -> None:
    self._sent = 0  # firmware commands this adapter's drivers were asked to send

  def command_count(self) -> int:
    return self._sent

  def _count_commands(self, driver) -> None:
    """Count the firmware commands sent through this driver instance: the simulator and a real link both
    send through these two, and nothing global is touched."""
    def count() -> None:
      self._sent += 1

    for name in ("send_command", "send_raw_command"):
      setattr(driver, name, _counting(getattr(driver, name), count))

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
