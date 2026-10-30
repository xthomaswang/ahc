"""Physical-world context: what AHC can observe of the real deck, from independent sources, and how far that goes.

physical_world_context is the umbrella. It is built from context sources, each answering one question per
verification point: can I observe it, at what level, and how fresh is my observation? Today's sources:

  vision_context       the cameras: slot maps, alignment and the quality gate (ahc.vision_context)
  instrument_context   the device's own sensors, which the adapter reports (ahc.instrument_context); the OT-2 has none

Later sources (a balance, a plate reader, room sensors) implement the same small interface and join the list
Lab.context_sources() returns. There is no plugin system.

physical_world_confidence is the conclusion, per verification point and per check type: covered, uncovered or
unclear, with the sources and levels that cover each point. There is no numeric score. A point is confident when at
least one source covers it at the level it needs. Auto-verify reads this; the check type's track record (agreement
streak) stays a separate condition there.

The digital twin is NOT a source. Its tracked volumes and tips are the expectation that observations are compared with;
they never add confidence, however well they match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

LEVELS = ("unclear", "slot", "well", "tip")  # what an observation resolves, low to high
RANK = {level: i for i, level in enumerate(LEVELS)}

STATES = ("ok", "unclear", "needs_new_map", "uncovered")  # what a source can say about a point it can observe
SHOWN_REASONS = 5  # points named in a one-line reason


@dataclass(frozen=True)
class Observation:
  """What one source (one camera, one sensor) says about one point it can observe."""
  source: str  # "vision:usb", "instrument:ot2"
  level: str | None  # the highest level it resolves the point at; None: nothing usable yet
  fresh: bool = True  # the observation is newer than the last motion
  state: str = "ok"  # ok (usable at `level`), unclear (blurred, covered, not captured), needs_new_map, uncovered
  why: str | None = None  # in plain words, when it is not usable


class ContextSource(Protocol):
  """One source of physical-world evidence. Implementations are small and hold what they measured; evaluate() asks."""
  kind: str  # "vision", "instrument"

  def describe(self) -> dict[str, Any]:
    """This source's section of the tool result (vision_context, instrument_context)."""

  def observe(self, point: dict[str, str], target: dict[str, Any]) -> list[Observation]:
    """One Observation per sub-source (each camera) that can observe the point; [] when none can. `target`: what the
    point names in the loaded layout (vision_points.resolve: key, labware, type, wells)."""


def evaluate(points: list[dict[str, str]], resolve: Callable[[str], dict[str, Any]],
             sources: list[ContextSource]) -> dict[str, Any]:
  """Every point against every source. `resolve(what)` gives the target a point names (raises ValueError when the
  layout has no such thing). -> {points, physical_world_confidence, suggestions}; each point has `sources` (the
  observations: source, level, fresh, covers, and state and why when it does not cover) and a status."""
  rows = []
  for p in points:
    try:
      target = resolve(p["what"])
    except ValueError as exc:
      rows.append({**p, "sources": [], "status": "uncovered", "why": str(exc)})
      continue
    observed: list[Observation] = []
    for source in sources:
      observed += source.observe(p, target)
    entries, notes, states = [], [], set()
    for o in observed:
      state, why = o.state, o.why
      if state == "ok" and RANK[o.level or "unclear"] < RANK[p["level"]]:
        state, why = "uncovered", f"{o.source} sees it at level {o.level}, the point needs {p['level']}"
      entry: dict[str, Any] = {"source": o.source, "level": o.level, "fresh": o.fresh, "covers": state == "ok"}
      if state != "ok":
        entry.update(state=state, why=why)
        states.add(state)
        notes.append(why or state)
      entries.append(entry)
    covered = any(e["covers"] for e in entries)
    status = "covered" if covered else ("unclear" if states & {"needs_new_map", "unclear"} else "uncovered")
    row = {**p, "sources": entries, "status": status}
    if not covered:
      row["why"] = "; ".join(notes) if notes else "no source observes it"
    rows.append(row)
  return {"points": rows, "physical_world_confidence": confidence(rows), "suggestions": suggestions(rows)}


def confidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
  """The conclusion: per check type its status (covered when every point is, else unclear if any is, else uncovered),
  the point counts and the sources and levels that cover; per source what it observes and covers; all_confident."""
  by_check: dict[str, Any] = {}
  by_source: dict[str, dict[str, int]] = {}
  for r in rows:
    mine = by_check.setdefault(r["check"], {"status": "covered", "points": {"covered": 0, "uncovered": 0, "unclear": 0}, "sources": {}})
    mine["points"][r["status"]] += 1
    for e in r["sources"]:
      seen = by_source.setdefault(e["source"], {"observes": 0, "covers": 0})
      seen["observes"] += 1
      seen["covers"] += e["covers"]
      if e["covers"] and RANK[e["level"]] >= RANK.get(mine["sources"].get(e["source"]), 0):
        mine["sources"][e["source"]] = e["level"]
  for mine in by_check.values():
    p = mine["points"]
    mine["status"] = "covered" if not p["uncovered"] and not p["unclear"] else ("unclear" if p["unclear"] else "uncovered")
    mine["sources"] = [{"source": s, "level": lvl} for s, lvl in sorted(mine["sources"].items())]
  return {"by_check": by_check, "by_source": by_source,
          "all_confident": bool(rows) and all(r["status"] == "covered" for r in rows)}


def point_reason(row: dict[str, Any]) -> str:
  """Why one point is not confident now, in plain words."""
  what = f"{row['check']} point {row['what']} (level {row['level']})"
  if row["status"] == "unclear":
    return f"{what}: {row.get('why') or 'unclear'}"
  return f"{what} is not covered: {row.get('why') or 'no source observes it at that level'}"


def reasons(rows: list[dict[str, Any]]) -> str | None:
  """The points that are not covered, one line; None when every one is."""
  bad = [r for r in rows if r["status"] != "covered"]
  if not bad:
    return None
  text = "; ".join(point_reason(r) for r in bad[:SHOWN_REASONS])
  return text + (f"; and {len(bad) - SHOWN_REASONS} more" if len(bad) > SHOWN_REASONS else "")


def suggestions(rows: list[dict[str, Any]]) -> list[str]:
  from ahc import vision  # the camera thresholds the advice names

  out: list[str] = []
  for r in rows:
    if r["status"] == "covered":
      continue
    what = r["what"]
    states = {e.get("state") for e in r["sources"]}
    if "needs_new_map" in states:
      text = "map_camera again for the camera that moved, then check again"
    elif r["status"] == "unclear":
      text = "capture again with nothing in front of the lens (the pipette head parked away from the camera)"
    elif r["check"] == "tips_mounted":
      text = (f"a camera with a '{what}' region (map_camera) close enough to see the tip: at least "
              f"{vision.TIP_MIN_PX} px across, or an instrument with a tip sensor")
    elif r["level"] == "well":
      text = (f"a camera seeing {what} from above at >= {vision.WELL_MIN_PX:g} px per well, with those wells inside "
              "the frame (map_camera)")
    else:
      text = f"a camera seeing {what} from above at >= {vision.SLOT_MIN_PX_PER_MM:g} px/mm (map_camera)"
    if text not in out:
      out.append(text)
  return out[:8]


def summary_line(result: dict[str, Any] | None, at: str | None = None) -> str:
  if not result or not result["points"]:
    return "no verification points yet (they come from the plan and the loaded layout)"
  pts = result["points"]
  covered = sum(1 for p in pts if p["status"] == "covered")
  bad = [f"{p['check']} {p['what']} {p['status']}" for p in pts if p["status"] != "covered"]
  return (f"{covered}/{len(pts)} points covered" + (f" ({at})" if at else "")
          + ("; " + ", ".join(bad[:4]) + (f" and {len(bad) - 4} more" if len(bad) > 4 else "") if bad else ""))
