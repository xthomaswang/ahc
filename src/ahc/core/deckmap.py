"""A deck as a JSON "deck map": what the dashboard draws as a top view, read from PyLabRobot's resource tree.

All coordinates are deck millimetres in PyLabRobot's frame: x grows to the right, y grows toward the BACK of the
robot, so the front-most site of a carrier has the lowest y. The page flips y to draw the deck the way a person
standing in front of the robot sees it (front at the bottom, left on the left).

  {"kind": "slots" | "tracks", "model": ..., "size": {"x", "y"}, "view": [x0, y0, x1, y1],
   "fixed": [{"name", "label", "x", "y", "w", "h"}],        # trash, waste: not placed by the layout
   "slots": [{"slot", "x", "y", "w", "h", "trash", "labware"}],                 # slots layouts (OT-2)
   "tracks": {"first", "last", "width", "ticks": [{"track", "x"}]},              # tracks layouts (STARlet)
   "carriers": [{"name", "type", "track", "track_end", "x", "y", "w", "h",
                 "sites": [{"site", "venus_position", "front", "x", "y", "w", "h", "labware"}]}],
   "labware": [{"name", "type", "kind": "plate" | "tip_rack" | "container", "x", "y", "w", "h", "where", "alias",
                "aliases": [{"alias", "wells"}], "rows", "cols", "a1": [x, y], "liquids": [{"wells", "ul", "ids"}],
                "tips": {"empty": [ids], "returned": [ids]}, "tips_text"}]}
"""

from __future__ import annotations

from typing import Any

from pylabrobot.resources import Carrier

from ahc.core.layout import Layout

# Labels for what PyLabRobot puts on a deck besides the layout's labware.
FIXED_LABELS = {"trash_core96": "CO-RE 96 waste", "waste_block": "waste block", "trash": "waste"}
FIXED_CATEGORIES = ("trash", "waste_block")  # what a deck has built in; a device adds its arm and sled later
MARGIN_MM = 12.0


def _round(value: float) -> float:
  return round(float(value), 2)


def _box(resource: Any, deck: Any) -> dict[str, float]:
  loc = resource.get_location_wrt(deck)
  return {"x": _round(loc.x), "y": _round(loc.y), "w": _round(resource.get_size_x()), "h": _round(resource.get_size_y())}


def _labware(layout: Layout, deck: Any, name: str, where: dict[str, Any]) -> dict[str, Any]:
  res = layout.labware[name]
  geometry = layout.geometry()[name]
  entry: dict[str, Any] = {"name": name, "type": layout.types[name], "kind": geometry["kind"], **_box(res, deck),
                           "where": layout.position(name), **where}
  aliases = []
  for alias, target in layout.aliases.items():
    labware, _, wells = target.partition(":")
    if labware == name:
      aliases.append({"alias": alias, "wells": wells or None})
  entry["aliases"] = aliases
  entry["alias"] = next((a["alias"] for a in aliases if a["wells"] is None), None)  # names the whole labware
  if geometry["kind"] in ("plate", "tip_rack"):
    first = res.get_item("A1")
    a1 = first.get_location_wrt(deck)
    entry.update(rows=geometry["rows"], cols=geometry["cols"],
                 a1=[_round(a1.x + first.get_size_x() / 2), _round(a1.y + first.get_size_y() / 2)])
  entry["liquids"] = [{"wells": ", ".join(Layout._runs(wells)), "ul": volume, "ids": wells}
                      for wells, volume in layout.liquid_groups(name)]
  states = layout.tip_states.get(name) or {}
  if geometry["kind"] == "tip_rack":
    entry["tips"] = {state: list(states.get(state) or []) for state in ("empty", "returned")}
    entry["tips_text"] = layout._tips_of(name)
  return entry


def _slots_map(layout: Layout, deck: Any) -> dict[str, Any]:
  by_slot: dict[int, str] = {}
  labware = []
  for name, res in layout.labware.items():
    number = int(res.parent.name.rsplit("_", 1)[1])  # the holder is named ot2_deck_slot_<n>
    by_slot[number] = name
    labware.append(_labware(layout, deck, name, {"slot": number}))
  slots = []
  for holder in deck.children:
    number = int(holder.name.rsplit("_", 1)[1])
    slots.append({"slot": number, **_box(holder, deck), "labware": by_slot.get(number),
                  "trash": any(c.category == "trash" for c in holder.children)})
  slots.sort(key=lambda s: s["slot"])
  return {"slots": slots, "fixed": [], "carriers": [], "tracks": None, "labware": labware}


def _tracks_map(layout: Layout, deck: Any) -> dict[str, Any]:
  rules = layout.spec.layout.get("tracks", {})
  first, last = int(rules.get("min", 1)), int(rules.get("max", deck.num_tracks))
  ticks = [{"track": t, "x": _round(deck.track_to_location(t).x)} for t in range(first, last + 1)]
  width = _round(ticks[1]["x"] - ticks[0]["x"]) if len(ticks) > 1 else 22.5
  carriers, labware = [], []
  types = {c.get("name"): c.get("type") for c in (layout.declared or {}).get("carriers") or [] if isinstance(c, dict)}
  for carrier in deck.children:
    if not isinstance(carrier, Carrier):
      continue
    box = _box(carrier, deck)
    track = next((t["track"] for t in ticks if abs(t["x"] - box["x"]) < 0.01), None)
    count = len(carrier.sites)
    sites = []
    for index, holder in sorted(carrier.sites.items()):
      sites.append({"site": index, "venus_position": count - index, **_box(holder, deck), "front": False,
                    "labware": next((n for n, r in layout.labware.items() if r.parent is holder), None)})
    if sites:
      min(sites, key=lambda s: s["y"])["front"] = True  # PyLabRobot's y grows toward the back
    for site in sites:
      if site["labware"]:
        labware.append(_labware(layout, deck, site["labware"], {"carrier": carrier.name, "site": site["site"]}))
    carriers.append({"name": carrier.name, "type": types.get(carrier.name), "track": track,
                     "track_end": deck.compute_right_track_of_carrier(carrier), **box, "sites": sites})
  carriers.sort(key=lambda c: c["x"])
  fixed = []
  for item in deck.children:
    if item.category not in FIXED_CATEGORIES or item.get_size_x() <= 0 or item.get_size_y() <= 0:
      continue  # not the arm or the autoload a device adds; a zero-width marker (the channels' waste position) has nothing to draw
    fixed.append({"name": item.name, "label": FIXED_LABELS.get(item.name, item.name), **_box(item, deck)})
  return {"slots": None, "fixed": fixed, "carriers": carriers, "labware": labware,
          "tracks": {"first": first, "last": last, "width": width, "ticks": ticks}}


def deck_map(layout: Layout) -> dict[str, Any]:
  """The deck of a built layout as a JSON deck map (see the module doc). Reads the resource tree only."""
  deck = layout.deck
  out: dict[str, Any] = {"kind": layout.kind, "model": layout.spec.model,
                         "size": {"x": _round(deck.get_size_x()), "y": _round(deck.get_size_y())}}
  out.update(_slots_map(layout, deck) if layout.kind == "slots" else _tracks_map(layout, deck))
  xs, ys = [0.0, out["size"]["x"]], [0.0, out["size"]["y"]]
  if layout.kind == "tracks":  # the working area: the rails, the carriers and the fixed items, not the empty right end
    track = out["tracks"]
    xs = [track["ticks"][0]["x"], track["ticks"][-1]["x"] + track["width"]]
    for item in out["fixed"] + out["carriers"]:
      xs += [item["x"], item["x"] + item["w"]]
  out["view"] = [_round(min(xs) - MARGIN_MM), _round(min(ys)), _round(max(xs) + MARGIN_MM), _round(max(ys))]
  return out
