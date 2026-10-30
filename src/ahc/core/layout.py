"""The deck the agent supplies, on PyLabRobot's resource model (shared by every brand).

Targets are written `labware`, `labware:A1`, `labware:A1:H1`, `labware:col2` (A2 to H2) or
`labware:rowB` (B1 to B12), or as an alias the layout defines, so a protocol can say `diluent` while
each device's layout says where that liquid physically is.
"""

from __future__ import annotations

import re
from typing import Any, Callable

import pylabrobot.resources as plr
from pylabrobot.resources import Container, Deck, Plate, TipRack, TipSpot

from ahc.devices.spec import DeviceSpec
from ahc.core.errors import LabError, at_most

ROWS = "ABCDEFGHIJKLMNOP"
_COLUMN = re.compile(r"col(\d+)", re.IGNORECASE)
_ROW = re.compile(r"row([a-z])", re.IGNORECASE)
TARGET_FORMS = ("Forms: 'labware' (a single container), 'labware:A1', 'labware:A1:H1' (a range), 'labware:col2' "
                "(A2 to H2, top to bottom), 'labware:rowB' (B1 to B12, left to right), or an alias from load_layout.")
TIP_FORMS = "Forms: 'tips:A1', 'tips:A1:H1' (a range), 'tips:col1' (A1 to H1, top to bottom) or 'tips:rowA'."


def deck_position(value: Any) -> str:
  """A slot, track or site as a layout writes it (1, '1' or '01'), as one text."""
  text = str(value).strip()
  return str(int(text)) if text.isdigit() else text


def _deck_order(where: str) -> list[Any]:
  """'slot 2' before 'slot 10', 'track 7' before 'track 7 site 0' before 'track 13'."""
  return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", where)]


def deck_differences(layout: dict[str, str], reference: dict[str, str]) -> list[str]:
  """How one deck placement (position -> what sits there) differs from a reference's, in deck order."""
  out = []
  for where in sorted(set(layout) | set(reference), key=_deck_order):
    mine, theirs = layout.get(where), reference.get(where)
    if mine == theirs:
      continue
    if theirs is None:
      out.append(f"{where}: not in the reference (this layout has {mine})")
    elif mine is None:
      out.append(f"{where}: not in this layout (the reference has {theirs})")
    else:
      out.append(f"{where}: the reference has {theirs}, this layout {mine}")
  return out


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
    self.picked: dict[str, list[TipSpot]] = {}  # component -> the spots its mounted tips came from
    self.returned: set[str] = set()  # spots holding a returned tip: never "next fresh" again

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

  @staticmethod
  def _items(resource, spots: str) -> list[Any]:
    """Wells or tip spots: A1, a range A1:H1, a column colN (top to bottom) or a row rowX (left to right)."""
    rows = ROWS[: resource.num_items_y]
    column = _COLUMN.fullmatch(spots)
    if column:
      n = int(column.group(1))
      if not 1 <= n <= resource.num_items_x:
        raise ValueError(f"column {n} is outside 1..{resource.num_items_x}")
      return [resource.get_item(f"{r}{n}") for r in rows]
    row = _ROW.fullmatch(spots)
    if row:
      letter = row.group(1).upper()
      if letter not in rows:
        raise ValueError(f"row {letter} is outside A..{rows[-1]}")
      return [resource.get_item(f"{letter}{c}") for c in range(1, resource.num_items_x + 1)]
    return resource[spots] if ":" in spots else [resource.get_item(spots)]

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
                         f"Write {name}:A1, {name}:A1:H1 or {name}:col1.")
        try:
          picked = self._items(resource, wells)
        except Exception as exc:  # noqa: BLE001 - PLR raises several types for bad identifiers
          raise LabError("bad_wells", f"{target!r}: {exc}", TARGET_FORMS) from exc
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
        raise LabError("bad_tips", f"{item!r} must name tip spots, e.g. tips:col1 or tips:A1:H1.", TIP_FORMS)
      try:
        out.extend(self._items(rack, spots))
      except Exception as exc:  # noqa: BLE001 - PLR raises several types for bad identifiers
        raise LabError("bad_tips", f"{item!r}: {exc}", TIP_FORMS) from exc
    return out

  def next_tips(self, n: int, fits: Callable[[TipRack], bool] | None = None, wanted: str | None = None) -> list[TipSpot]:
    """The next full column of fresh tips for an 8-channel component, or the next tip for one channel, in layout
    order from the racks whose tips the component takes (`fits`; `wanted` names those tips for the refusal). A
    spot a tip was returned to is not fresh: that tip may have touched liquid."""
    def fresh(spot: TipSpot) -> bool:
      return spot.has_tip() and spot.name not in self.returned

    for rack in (r for r in self.labware.values() if isinstance(r, TipRack) and (fits is None or fits(r))):
      for col in range(1, rack.num_items_x + 1):
        column = [rack.get_item(f"{row}{col}") for row in ROWS[: rack.num_items_y]]
        if n == len(column) and all(fresh(s) for s in column):
          return column
        if n == 1:
          for spot in column:
            if fresh(spot):
              return [spot]
    tips = f"{wanted} tips" if wanted else "tips"
    raise LabError("out_of_tips", f"no fresh {tips} for {n} channel(s) in the layout.",
                   f"Add a rack of {tips} to the layout, or name returned tips explicitly to use them again.")

  @staticmethod
  def column_of(items: list[Any]) -> int | None:
    """The column number when the items are one full column of their plate or rack, top to bottom."""
    parent = items[0].parent if items else None
    if not hasattr(parent, "num_items_y") or any(i.parent is not parent for i in items):
      return None
    names = [parent.get_child_identifier(i) for i in items]
    column = names[0][1:]
    return int(column) if names == [f"{r}{column}" for r in ROWS[: parent.num_items_y]] else None

  def where(self, items: list[Any]) -> str:
    """Where a call went, per labware: 'plate: column 2 (A2-H2)', 'plate: row B (B1-B12)', 'plate: A1, A3'
    or a single container's name; several labware joined with '; '."""
    names = {id(res): name for name, res in self.labware.items()}
    groups: dict[str, list[Any]] = {}
    for item in items:
      if id(item) in names or not hasattr(item.parent, "num_items_x"):  # a single container: a trough
        groups.setdefault(names.get(id(item), item.name), [])
        continue
      group = groups.setdefault(names.get(id(item.parent), item.parent.name), [])
      if all(i is not item for i in group):
        group.append(item)
    parts = []
    for name, group in groups.items():
      if not group:
        parts.append(name)
        continue
      parent = group[0].parent
      ids = [parent.get_child_identifier(i) for i in group]
      column = self.column_of(group)
      if column is not None:
        parts.append(f"{name}: column {column} ({ids[0]}-{ids[-1]})")
      elif ids == [f"{ids[0][0]}{c}" for c in range(1, parent.num_items_x + 1)]:
        parts.append(f"{name}: row {ids[0][0]} ({ids[0]}-{ids[-1]})")
      else:
        parts.append(f"{name}: {', '.join(ids)}")
    return "; ".join(parts)

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
                         f"Use at most {at_most(capacity, 2)} uL.")
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
