"""vision_context: the cameras as a physical-world context source.

Each mapped camera is one sub-source ("vision:<camera>"). For a verification point it says whether the camera's slot map
contains the point's slot and, from the latest capture's analysis (ahc.vision: alignment, quality gate, per-slot
level), at what level the point is resolved. A capture from before the last motion shows an older deck, so it is not
fresh and covers nothing.
"""

from __future__ import annotations

from typing import Any

from ahc import vision
from ahc.physical_world import RANK, Observation

KIND = "vision"


class VisionContext:
  """`cameras`: name -> {map (the slot map), analysis (the latest capture newer than the last motion, else None),
  captured (a capture of any age exists), image (its path)}; Lab.vision_cameras() builds it."""
  kind = KIND

  def __init__(self, cameras: dict[str, dict[str, Any]], plates: dict[str, str] | None = None):
    self.cameras = cameras
    self.plates = plates or {}  # labware -> calibrated | requested, for the loaded layout (ahc.calibration)

  def describe(self) -> dict[str, Any]:
    """The cameras as the tool shows them: the latest analysis' status, why, sharpness and alignment."""
    out: dict[str, Any] = {"cameras": {}}
    for name, cam in self.cameras.items():
      seen = cam.get("analysis") or {}
      out["cameras"][name] = {"status": seen.get("status", "not captured"), "why": seen.get("why"),
                              "frame_sharpness": seen.get("frame_sharpness"), "alignment": seen.get("alignment"),
                              "image": cam.get("image"), "fresh": cam.get("analysis") is not None}
    if not self.cameras:
      out["note"] = "no camera has a slot map yet (map_camera)"
    if self.plates:
      out["plates"] = dict(self.plates)
    return out

  def observe(self, point: dict[str, str], target: dict[str, Any]) -> list[Observation]:
    key, wells, out = target["key"], target["wells"], []
    for name, cam in self.cameras.items():
      if key not in cam["map"]["slots"]:
        continue
      source = f"{KIND}:{name}"
      analysis = cam.get("analysis")
      if analysis is None:
        out.append(Observation(source, None, False, "unclear",
                               f"{name} has no capture since the last motion; physical_world_context captures"
                               if cam.get("captured") else
                               f"{name} has not been captured yet; physical_world_context captures"))
        continue
      if analysis["status"] == "needs_new_map":
        out.append(Observation(source, None, True, "needs_new_map",
                               f"{name} no longer aligns to its slot map ({analysis['why'] or 'moved'}); map_camera again"))
        continue
      if analysis["status"] != "ok":
        out.append(Observation(source, None, True, "unclear",
                               f"{name}: the capture is too blurred or covered to judge ({analysis['why'] or 'unclear'})"))
        continue
      entry = next((e for e in analysis["slots"] if e["slot"] == key), None)
      if entry is None:
        continue
      if entry["level"] == "unclear":
        out.append(Observation(source, "unclear", True, "unclear",
                               f"{name}: the crop is too blurred or covered to judge ({entry.get('why') or 'unclear'})"))
      elif point["level"] == "well" and self._grid_labware(target):
        out.append(self._well_level(source, name, entry, target, wells, analysis["image_size"]))
      elif (wells and point["level"] == "well" and RANK[entry["level"]] >= RANK["well"]
            and (outside := vision.wells_outside(entry, target["type"], wells, analysis["image_size"]))):
        out.append(Observation(source, entry["level"], True, "uncovered",
                               f"{name}: wells {', '.join(outside[:6])}{' ...' if len(outside) > 6 else ''} are outside the frame"))
      else:
        out.append(Observation(source, entry["level"], True))
    return out

  @staticmethod
  def _grid_labware(target: dict[str, Any]) -> bool:
    """A plate or rack with rows and columns of wells: single wells are found by the person's plate calibration."""
    geometry = vision.labware_geometry(target["type"]) if target.get("type") else None
    return bool(geometry and geometry["rows"] > 1 and geometry["cols"] > 1)

  def _well_level(self, source: str, camera: str, entry: dict[str, Any], target: dict[str, Any],
                  wells: list[str] | None, frame: list[int]) -> Observation:
    """Single wells of a plate: only where the person calibrated the plate (four clicked wells), at the grid's own pitch."""
    labware, grid, level = target["labware"], entry.get("grid"), entry["level"]
    if grid is None:
      state = self.plates.get(labware)
      return Observation(source, "slot" if RANK[level] >= RANK["slot"] else level, True, "uncovered",
                         f"{camera}: single wells of {labware} are found only after the person calibrates the plate "
                         + ("(requested: the person has not clicked its corner wells yet)" if state == "requested" else
                            f"(request_plate_calibration(labware='{labware}', camera='{camera}', why=...))"))
    if grid["pitch_px"] < vision.WELL_MIN_PX:
      return Observation(source, "slot", True, "uncovered",
                         f"{camera}: the wells of {labware} are {grid['pitch_px']:.0f} px apart; single wells need "
                         f"{vision.WELL_MIN_PX:g} px")
    wanted = wells or vision.well_ids(grid["rows"], grid["cols"])
    if outside := vision.grid_wells_outside(grid, wanted, frame):
      return Observation(source, level, True, "uncovered",
                         f"{camera}: wells {', '.join(outside[:6])}{' ...' if len(outside) > 6 else ''} are outside the frame")
    return Observation(source, "well", True)
