"""Verification points: what must be observable, at what level, and the agent's image-judgment track record.

Once a plan is reported and a layout is loaded, AHC derives the run's points:

  deck_matches_layout  every layout slot, at level `slot`
  liquid_present       the targets the agent declares (report_decision vision_points), at level `well` (or as asked)
  tips_mounted         `tips:<pipette>` at level `tip`: needs a camera map with such a region (none does yet,
                       so these points stay uncovered and tips checks stay with the person)

The agent may add points; the derived ones cannot be removed. physical_world.evaluate() compares them with what each
context source observes (cameras, the instrument's sensors) and gives every point a status: covered, uncovered or
unclear. `declared_by` says who made a point (derived or agent).

The track record (.ahc/state/vision_record.json) counts, per check type, how often the agent's image judgment
(propose_verdict, shadow mode) agreed with the person's verdict. A disagreement or an unclear judgment resets the
streak; a change of any camera's slot map resets every streak.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from ahc.core.errors import LabError
from ahc.workspace.config import Config
from ahc.workspace.store import Workspace, write_atomic

POINT_CHECKS = ("deck_matches_layout", "liquid_present", "tips_mounted")
POINT_LEVELS = ("slot", "well", "tip")
MAX_POINTS = 40
DEFAULT_STREAK = 5
WELLS = re.compile(r"(?:[A-P]\d{1,2}(?::[A-P]\d{1,2})?|col\d+|row[A-Pa-p])")


def bad_point(message: str, hint: str | None = None) -> LabError:
  return LabError("bad_vision_point", message, hint or (
    "A point is {'what': 'plate:B1:B8', 'level': 'well', 'check': 'liquid_present'}: what is a target ('labware', "
    "'labware:A1:H1', 'labware:col2', an alias), 'slot:<n>' or 'tips:<component>'; level slot, well or tip; check "
    "deck_matches_layout, liquid_present or tips_mounted."))


def parse_points(raw: Any) -> list[dict[str, str]]:
  """The agent's vision_points, checked for shape (what they name is checked against the layout once one is loaded)."""
  if not isinstance(raw, list) or len(raw) > MAX_POINTS:
    raise bad_point(f"vision_points must be a list of at most {MAX_POINTS} points.")
  out: list[dict[str, str]] = []
  for i, item in enumerate(raw):
    if not isinstance(item, dict) or set(item) - {"what", "level", "check"}:
      raise bad_point(f"vision_points[{i}] must be a mapping with what, level and check.")
    what, level, check = item.get("what"), item.get("level", "slot"), item.get("check", "liquid_present")
    if not isinstance(what, str) or not what.strip() or len(what) > 80:
      raise bad_point(f"vision_points[{i}].what must be a short text.")
    if level not in POINT_LEVELS:
      raise bad_point(f"vision_points[{i}].level {level!r} is not one of {', '.join(POINT_LEVELS)}.")
    if check not in POINT_CHECKS:
      raise bad_point(f"vision_points[{i}].check {check!r} is not one of {', '.join(POINT_CHECKS)}.")
    out.append({"what": what.strip(), "level": level, "check": check})
  return out


def merged(declared: list[dict[str, str]], new: list[dict[str, str]]) -> list[dict[str, str]]:
  """The agent's points plus new ones; the same what and check again replaces the earlier level."""
  keyed = {(p["what"], p["check"]): p for p in declared}
  keyed.update({(p["what"], p["check"]): p for p in new})
  return list(keyed.values())[:MAX_POINTS]


# -- the points of this run -------------------------------------------------------------------------

def layout_slots(layout: Any) -> dict[str, dict[str, str]]:
  """The loaded layout's slots: slot -> {name, type}. A layout without slots (the STARlet's carriers) has none."""
  slots = (getattr(layout, "declared", None) or {}).get("slots")
  if not isinstance(slots, dict):
    return {}
  return {str(k): {"name": v["name"], "type": v["type"]} for k, v in slots.items()
          if isinstance(v, dict) and "name" in v and "type" in v}


def resolve(what: str, layout: Any) -> dict[str, Any]:
  """What a point names, in the loaded layout: {key (the slot, or the tips region), labware, type, wells}. Raises
  ValueError saying what is wrong."""
  if what.startswith("tips:"):
    return {"key": what, "labware": None, "type": None, "wells": None}
  slots = layout_slots(layout)
  if what.startswith("slot:"):
    slot = what[5:].strip()
    if slot not in slots:
      raise ValueError(f"the layout has no labware in slot {slot}")
    return {"key": slot, "labware": slots[slot]["name"], "type": slots[slot]["type"], "wells": None}
  target = layout.aliases.get(what, what)
  name, _, spec = target.partition(":")
  where = {v["name"]: k for k, v in slots.items()}
  if name not in layout.labware:
    raise ValueError(f"{name!r} is not in the loaded layout")
  if name not in where:
    raise ValueError(f"{name!r} sits in no slot")
  wells = None
  if spec:
    if not WELLS.fullmatch(spec.split(":")[0]) or (":" in spec and not WELLS.fullmatch(spec.split(":")[1])):
      raise ValueError(f"{spec!r} is not a well, range, column or row")
    resource = layout.labware[name]
    try:
      wells = [resource.get_child_identifier(item) for item in layout._items(resource, spec)]
    except Exception as exc:  # noqa: BLE001 - PyLabRobot raises several types for bad identifiers
      raise ValueError(f"{spec!r}: {exc}") from exc
  return {"key": where[name], "labware": name, "type": slots[where[name]]["type"], "wells": wells}


def check_points(points: list[dict[str, str]], layout: Any) -> None:
  """Refuse points that name something the loaded layout does not have (a layout loaded already)."""
  for p in points:
    try:
      resolve(p["what"], layout)
    except ValueError as exc:
      raise bad_point(f"vision point {p['what']!r}: {exc}.",
                      "Name labware, wells or aliases of the loaded layout (lab_overview, find_layout_references), "
                      "'slot:<n>' for a slot or 'tips:<component>' for a pipette's tips.") from exc


def derive(layout: Any, pipettes: list[str], declared: list[dict[str, str]]) -> list[dict[str, str]]:
  """The run's points: the derived ones first (they cannot be removed), then the agent's."""
  points = [{"what": f"slot:{slot}", "level": "slot", "check": "deck_matches_layout", "declared_by": "derived",
             "labware": info["name"]}
            for slot, info in sorted(layout_slots(layout).items(), key=lambda kv: int(kv[0]))]
  points += [{"what": f"tips:{p}", "level": "tip", "check": "tips_mounted", "declared_by": "derived"} for p in pipettes]
  seen = {(p["what"], p["check"]) for p in points}
  points += [{**p, "declared_by": "agent"} for p in declared if (p["what"], p["check"]) not in seen]
  return points


# -- the track record -------------------------------------------------------------------------------

def maps_digest(config: Config | None) -> str:
  """What a change of any slot map must reset: a hash of every camera's map."""
  maps = {c.name: c.slot_map for c in (config.cameras if config else []) if c.slot_map}
  return hashlib.sha256(json.dumps(maps, sort_keys=True).encode()).hexdigest()[:16] if maps else "none"


def _fresh() -> dict[str, Any]:
  return {"agree_streak": 0, "total": 0, "disagreements": 0, "unclear": 0, "last": None}


class VisionRecord:
  """.ahc/state/vision_record.json: per check type {agree_streak, total, disagreements, unclear, last}."""

  def __init__(self, ws: Workspace):
    self.ws = ws
    self.path = ws.state_dir / "vision_record.json"

  def read(self, digest: str) -> dict[str, dict[str, Any]]:
    """The record for these slot maps; another map than the one it was kept for starts it again."""
    try:
      data = json.loads(self.path.read_text())
    except (OSError, ValueError):
      return {}
    if not isinstance(data, dict) or data.get("maps") != digest or not isinstance(data.get("checks"), dict):
      return {}
    return {k: v for k, v in data["checks"].items() if isinstance(v, dict)}

  def compare(self, digest: str, check: str, agent: str, person: str, check_id: str) -> dict[str, Any]:
    """Record the agent's judgment (pass, fail or unclear) next to the person's verdict (pass or fail)."""
    checks = self.read(digest)
    entry = {**_fresh(), **checks.get(check, {})}
    agreed = agent == person  # an unclear judgment is neither agreement nor disagreement
    entry["total"] += 1
    if agent == "unclear":
      entry["unclear"] += 1
      entry["agree_streak"] = 0
    elif agreed:
      entry["agree_streak"] += 1
    else:
      entry["disagreements"] += 1
      entry["agree_streak"] = 0
    entry["last"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "check_id": check_id, "agent": agent, "person": person,
                     "agreed": agreed}
    checks[check] = entry
    self.ws.require_task_folder()
    self.ws.state_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(self.path, json.dumps({"maps": digest, "checks": checks}, indent=2) + "\n")
    return entry
