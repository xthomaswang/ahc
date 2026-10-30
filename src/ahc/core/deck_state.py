"""The digital twin's deck, carried from one run to the next.

`snapshot` is what .ahc/state/deck.json holds after every call that changes the deck; `last_deck` turns it into a
layout the agent can load again (find_layout_references offers it; AHC never loads it by itself). What the twin could
not vouch for (a call whose outcome is unknown, an emergency stop) is written as unknown and left out of that layout.
"""

from __future__ import annotations

import copy
import time
from typing import Any

from pylabrobot.resources import Container, Plate, TipRack

from ahc.core.layout import Layout, well_runs

VERSION = 1


def contents(layout: Layout, unknown: set[int], racks_unknown: bool) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
  """What the twin holds: liquid per labware (a plate: its wells with liquid, a container: its volume; "unknown" for
  the ids in `unknown`), fresh tips per rack ("unknown" when racks_unknown) and the tips back in their spots after
  use (never next fresh, only when named). get_state and the saved deck state both read it."""
  liquids: dict[str, Any] = {}
  tips_left: dict[str, Any] = {}
  returned: dict[str, int] = {}
  for name, res in layout.labware.items():
    if isinstance(res, TipRack):
      held = [s for s in res.get_all_items() if s.has_tip()]
      tips_left[name] = "unknown" if racks_unknown else sum(1 for s in held if s.name not in layout.returned)
      if any(s.name in layout.returned for s in held):
        returned[name] = sum(1 for s in held if s.name in layout.returned)
    elif isinstance(res, Plate):
      wells = {w.get_identifier(): "unknown" if id(w) in unknown else round(w.tracker.get_used_volume(), 3)
               for w in res.get_all_items() if id(w) in unknown or w.tracker.get_used_volume() > 0}
      if wells:
        liquids[name] = wells
    elif isinstance(res, Container):
      liquids[name] = "unknown" if id(res) in unknown else round(res.tracker.get_used_volume(), 3)
  return liquids, tips_left, returned


def targets(labware: str, ids: list[str]) -> list[str]:
  """Wells or tip spots as targets in runs: ['plate:B1:H1', 'plate:D1:D11', 'plate:A2']."""
  return [f"{labware}:{run[0]}" + (f":{run[-1]}" if len(run) > 1 else "") for run in well_runs(ids)]


def snapshot(layout: Layout, *, run_id: str | None, device: dict[str, Any], mounted: dict[str, Any],
             doubt: dict[str, Any] | None) -> dict[str, Any]:
  """The deck as the twin has it now. `mounted`: tips on each pipette (a count, or "unknown"). `doubt`: what the twin
  cannot vouch for ({"why", "wells", "racks", "spots"}, see Lab.unknown_state), or None."""
  unknown_ids = {id(w) for w in doubt["wells"]} if doubt else set()
  doubt_racks = bool(doubt and doubt["racks"])
  liquids, _, _ = contents(layout, unknown_ids, doubt_racks)
  volumes: dict[str, Any] = {}
  unknown_wells: list[str] = []
  for name, held in liquids.items():
    if held == "unknown":
      unknown_wells.append(name)
    elif isinstance(held, dict):
      unknown_wells += targets(name, [w for w, v in held.items() if v == "unknown"])
      volumes[name] = {w: v for w, v in held.items() if v != "unknown"}
    else:
      volumes[name] = held
  spots = {id(s) for s in (doubt["spots"] if doubt else [])}
  tips: dict[str, dict[str, str]] = {}
  unknown_racks: list[str] = []
  for name, res in layout.labware.items():
    if not isinstance(res, TipRack):
      continue
    status: dict[str, str] = {}
    for spot in sorted(res.get_all_items(), key=lambda s: (int(res.get_child_identifier(s)[1:]),
                                                           res.get_child_identifier(s)[0])):
      state = ("unknown" if id(spot) in spots else "empty" if not spot.has_tip()
               else "returned" if spot.name in layout.returned else None)
      if state is not None:
        status[res.get_child_identifier(spot)] = state
    if status:
      tips[name] = status
    if doubt_racks or "unknown" in status.values():
      unknown_racks.append(name)
  out: dict[str, Any] = {"version": VERSION, "run_id": run_id, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "device": device, "layout": copy.deepcopy(layout.declared), "liquids_ul": volumes,
                         "tips": tips, "tips_mounted": mounted}
  if doubt:
    out["unknown"] = {"why": doubt["why"], "wells": unknown_wells, "racks": unknown_racks}
  return out


def _without(layout: dict[str, Any], names: set[str]) -> None:
  """Take labware out of a layout by name (a slot of an Opentrons layout, a site of a Hamilton carrier)."""
  holders = [layout.get("slots") or {}] + [c.get("sites") or {} for c in layout.get("carriers") or []]
  for holder in holders:
    for key in [k for k, item in holder.items() if isinstance(item, dict) and item.get("name") in names]:
      del holder[key]


def last_deck(deck: dict[str, Any]) -> dict[str, Any]:
  """A saved deck state as a layout ready to load: the layout as loaded, `liquids` from the saved volumes and
  tips_empty / tips_returned from the saved tip spots. A rack the twin could not vouch for is taken out of it, a
  well it could not vouch for gets no volume; both are listed under `unknown`."""
  layout = copy.deepcopy(deck["layout"])
  unknown = deck.get("unknown") or {}
  racks = set(unknown.get("racks") or [])
  _without(layout, racks)
  liquids: dict[str, float] = {}
  for name, held in (deck.get("liquids_ul") or {}).items():
    if isinstance(held, dict):
      for volume in sorted({v for v in held.values() if v > 0}):
        for target in targets(name, [w for w, v in held.items() if v == volume]):
          liquids[target] = volume
    elif held > 0:
      liquids[name] = held
  layout["liquids"] = liquids
  for key, state in (("tips_empty", "empty"), ("tips_returned", "returned")):
    layout.pop(key, None)
    listed = [t for rack, spots in (deck.get("tips") or {}).items() if rack not in racks
              for t in targets(rack, [s for s, v in spots.items() if v == state])]
    if listed:
      layout[key] = listed
  left_out = list(unknown.get("wells") or []) + [f"{rack} (tip states)" for rack in sorted(racks)]
  note = (f"The twin's deck at the end of run {deck.get('run_id')} ({str(deck.get('at') or '').replace('T', ' ')}): "
          "volumes and tip spots as sent to the device, not measured. Load it as is, or edit it first (e.g. add a fresh "
          "plate); AHC never loads it by itself. The person confirms it at the deck check, since anything may have been "
          "touched since.")
  if left_out:
    note += (f" Not known, so left out: {', '.join(left_out)}. The person gives those volumes (liquids) and tip states "
             "(tips_empty / tips_returned); a tip rack listed here is not in the layout: add it back with their tip states.")
  elif unknown.get("why"):
    note += f" The twin was in doubt at the end: {unknown['why']}"
  return {"run_id": deck.get("run_id"), "at": deck.get("at"), "layout": layout, "unknown": left_out, "note": note}
