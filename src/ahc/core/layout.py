"""The deck the agent supplies, on PyLabRobot's resource model (shared by every brand).

Targets are written `labware`, `labware:A1` or `labware:A1:H1`, or as an alias the layout defines,
so a protocol can say `diluent` while each device's layout says where that liquid physically is.
"""

from __future__ import annotations

from typing import Any

import pylabrobot.resources as plr
from pylabrobot.resources import Container, Deck, Plate, TipRack, TipSpot

from ahc.devices.spec import DeviceSpec
from ahc.core.errors import LabError

ROWS = "ABCDEFGH"


def make_labware(spec: DeviceSpec, kind: str, type_name: str, name: str):
  """Create labware the description file allows for this device."""
  allowed = spec.layout.get("labware", {})
  every = [t for group in allowed.values() for t in group]
  if type_name not in every:
    raise LabError("labware_not_allowed", f"{type_name!r} is not listed for {spec.model}.",
                   f"Allowed labware: {', '.join(every)}.")
  factory = getattr(plr, type_name, None)
  if factory is None:
    raise LabError("labware_unknown", f"PyLabRobot has no labware named {type_name!r}.")
  return factory(name=name)


class Layout:
  """Labware by name, liquids and aliases, resolved against the live PyLabRobot deck."""

  def __init__(self, spec: DeviceSpec, kind: str):
    self.spec = spec
    self.kind = kind
    self.labware: dict[str, Any] = {}
    self.types: dict[str, str] = {}
    self.aliases: dict[str, str] = {}
    self.liquids: dict[str, float] = {}

  def add(self, name: str, resource: Any, type_name: str) -> None:
    if name in self.labware:
      raise LabError("duplicate_name", f"labware name {name!r} is used twice.")
    self.labware[name] = resource
    self.types[name] = type_name

  # -- addressing -----------------------------------------------------------------------------

  def _lookup(self, name: str) -> Any:
    if name not in self.labware:
      known = ", ".join(list(self.labware) + list(self.aliases))
      raise LabError("unknown_target", f"{name!r} is not in the loaded layout.",
                     f"Known names: {known or '(no layout loaded)'}.")
    return self.labware[name]

  def resolve_targets(self, targets: list[str]) -> list[Container]:
    out: list[Container] = []
    for target in targets:
      target = self.aliases.get(target, target)
      name, _, wells = target.partition(":")
      resource = self._lookup(name)
      if isinstance(resource, TipRack):
        raise LabError("not_a_container", f"{name!r} is a tip rack, not a liquid container.")
      if isinstance(resource, Plate):
        if not wells:
          raise LabError("wells_required", f"{name!r} is a plate; name the wells.",
                         f"Write {name}:A1 or {name}:A1:H1.")
        try:
          picked = resource[wells] if ":" in wells else [resource.get_well(wells)]
        except Exception as exc:  # noqa: BLE001 - PLR raises several types for bad identifiers
          raise LabError("bad_wells", f"{target!r}: {exc}") from exc
        out.extend(picked)
      elif isinstance(resource, Container):
        if wells:
          raise LabError("no_wells", f"{name!r} is a single container; drop ':{wells}'.")
        out.append(resource)
      else:
        raise LabError("not_a_container", f"{name!r} cannot hold liquid.")
    return out

  def resolve_tips(self, tips: list[str]) -> list[TipSpot]:
    out: list[TipSpot] = []
    for item in tips:
      name, _, spots = item.partition(":")
      rack = self._lookup(name)
      if not isinstance(rack, TipRack) or not spots:
        raise LabError("bad_tips", f"{item!r} must name tip spots, e.g. tips:A1:H1.")
      out.extend(rack[spots] if ":" in spots else [rack.get_item(spots)])
    return out

  def next_tips(self, n: int) -> list[TipSpot]:
    """The next full column of tips for an 8-channel component, or the next tip for one channel."""
    for rack in (r for r in self.labware.values() if isinstance(r, TipRack)):
      for col in range(1, rack.num_items_x + 1):
        column = [rack.get_item(f"{row}{col}") for row in ROWS[: rack.num_items_y]]
        if n == len(column) and all(s.has_tip() for s in column):
          return column
        if n == 1:
          for spot in column:
            if spot.has_tip():
              return [spot]
    raise LabError("out_of_tips", f"no fresh tips for {n} channel(s) in the layout.",
                   "Add a tip rack to the layout.")

  # -- liquids and aliases --------------------------------------------------------------------

  def set_aliases(self, aliases: dict[str, str]) -> None:
    for alias, target in aliases.items():
      if alias == target:
        continue  # naming a labware after itself is harmless
      if alias in self.labware:
        raise LabError("alias_clash", f"alias {alias!r} is also a labware name.",
                       f"Rename the labware or the alias; {alias!r} cannot mean two things.")
      self.aliases[alias] = target
      self.resolve_targets([alias])  # refuse an alias that points nowhere

  def set_liquids(self, liquids: dict[str, float]) -> None:
    for target, volume in liquids.items():
      for container in self.resolve_targets([target]):
        capacity = container.max_volume
        if volume < 0 or volume > capacity:
          raise LabError("overfill", f"{target}: {volume} uL does not fit {container.name}.",
                         f"Use at most {capacity:g} uL.")
        container.tracker.set_volume(volume)
        self.liquids[container.name] = volume

  # -- where the labware sits -----------------------------------------------------------------

  @staticmethod
  def _placement(res) -> dict[str, Any]:
    """Where a labware sits relative to its deck: what a camera or a person would check.

    Measured against the deck, not the tree's root, because a live view wraps the device in its
    own root without moving anything.
    """
    deck = res.parent
    while deck is not None and not isinstance(deck, Deck):
      deck = deck.parent
    if deck is None:  # taken off the deck, or its carrier was
      return {"parent": res.parent.name if res.parent is not None else None, "deck": None, "location_mm": None}
    loc = res.get_location_wrt(deck)
    return {"parent": res.parent.name, "deck": deck.name,
            "location_mm": [round(loc.x, 2), round(loc.y, 2), round(loc.z, 2)]}

  def record_placements(self) -> None:
    """Remember where each labware sits on the device's deck right after loading."""
    self.placements = {name: self._placement(res) for name, res in self.labware.items()}

  def compare_placements(self) -> dict[str, Any]:
    """The deck as the device model has it now, against what load_layout put there."""
    rows = {}
    for name, expected in getattr(self, "placements", {}).items():
      found = self._placement(self.labware[name])
      rows[name] = {"expected": expected, "found": found, "matches": found == expected}
    return {"labware": rows, "as_expected": bool(rows) and all(r["matches"] for r in rows.values())}

  def geometry(self) -> dict[str, Any]:
    """Shape and capacity of each labware, for anything that draws the deck."""
    out: dict[str, Any] = {}
    for name, res in self.labware.items():
      if isinstance(res, TipRack):
        out[name] = {"kind": "tip_rack", "rows": res.num_items_y, "cols": res.num_items_x}
      elif isinstance(res, Plate):
        out[name] = {"kind": "plate", "rows": res.num_items_y, "cols": res.num_items_x,
                     "well_max_ul": res.get_item("A1").max_volume}
      elif isinstance(res, Container):
        out[name] = {"kind": "container", "max_ul": res.max_volume}
    return out

  def summary(self) -> dict[str, Any]:
    return {
      "kind": self.kind,
      "labware": dict(self.types),
      "geometry": self.geometry(),
      "aliases": dict(self.aliases),
      "liquids_ul": dict(self.liquids),
    }
