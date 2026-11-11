"""Slot maps for camera images: align a capture to the camera's reference, then judge each slot's crop.

A camera's slot map (config.cameras[].slot_map) holds a reference image and one box per deck slot it sees,
in the reference's pixels. Per capture:

  1. frame check   the whole frame's edge sharpness; a frame out of focus or covered judges nothing
  2. alignment     ORB features and a RANSAC homography from the reference to the capture; too few inliers or
                   an implausible homography means the camera needs a new map
  3. slot crops    each box is warped into the capture and cropped; per crop: edge sharpness (size-normalized),
                   pixels per well (from the labware's well pitch) and how much of the box is in the frame
  4. plate grids   a plate the person calibrated (four clicked wells, ahc.calibration) carries a grid of well centres
                   warped into the capture the same way; its pitch in pixels, not the box's estimate, then decides
                   whether single wells resolve

and each crop gets a level: `unclear` (below the thresholds), `slot` (presence, type and liquid colour can be
judged) or `well` (single wells can be resolved). A `tips:<component>` region gets `tip` instead. The numbers
are AHC's measurements; nothing here asks the agent how sure it is.

Thresholds were calibrated on 16 real captures of the OT-2 deck (USB top camera, RealSense side view), the
levels below are what those captures measured:

                        sharp frames        out of focus (pipette head in front of the lens)
    frame sharpness     usb 41-44, rs 30    4
    slot crop           7-60                3-5          (slot 8, a clear plate, is the lowest: 7-12)
    alignment inliers   943-1411 of 1788    5-10
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ahc.physical_world import LEVELS, RANK  # noqa: F401 - the levels are shared with the other context sources

FRAME_SHARP_MIN = 12.0  # whole frame, p99 of |Laplacian| at 960 px wide
SLOT_SHARP_MIN = 6.0  # a slot crop, p99 of |Laplacian| at 384 px wide
MOVED_PX = 8.0  # a camera that has moved less than this (a frame corner, in the picture's pixels) "has not moved"
DEFAULT_WELL_FRACTION = 0.7  # a well's diameter over the well pitch, when the labware does not say
MIN_GRID_SPAN = 0.02  # the four clicked points enclose at least this part of the frame (else they are one spot)
MIN_VISIBLE = 0.5  # the part of a slot's box inside the frame (slot 8 shows about 0.65 on the real deck)
SLOT_MIN_PX_PER_MM = 0.8  # below it labware type and liquid colour cannot be judged
WELL_MIN_PX = 20.0  # pixels across one well pitch, to resolve single wells
TIP_MIN_PX = 60  # a tips:<component> region's short side, in pixels
SIDE_MIN_PX = 60  # a side view's box: short side in pixels for level slot
ALIGN_WIDTH = 1280  # captures are aligned at the width the reference (and the agent's preview) has
ALIGN_MIN_INLIERS = 40
ALIGN_MIN_RATIO = 0.25  # inliers over ratio-test matches
ALIGN_MAX_SHIFT = 0.2  # a frame corner may move this much of the frame width
FOOTPRINT_MM = (127.76, 85.48)  # an OT-2 slot's labware footprint
DEFAULT_PITCH_MM = 9.0  # a 96-well plate, when no layout says which labware sits in the slot

SLOT_KEY_HELP = "a slot '1'..'12' or a region 'tips:<component>' (e.g. 'tips:ot2.right')"


def region_kind(key: str) -> str:
  return "tips" if key.startswith("tips:") else "slot"


# -- reading images ---------------------------------------------------------------------------------

def read_gray(path: Path | str) -> np.ndarray | None:
  """The image as grayscale; None when it cannot be read (not an image, or missing)."""
  try:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
      return None
    return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
  except OSError:
    return None


def edge_sharpness(gray: np.ndarray, width: int) -> float:
  """p99 of |Laplacian| after scaling to `width` px wide and a light blur (which damps sensor noise). A uniform
  area adds nothing, so a crop with one edge in it still reads sharp; out-of-focus frames sit at the noise floor (3-5)."""
  if gray.size == 0 or gray.shape[0] < 4 or gray.shape[1] < 4:
    return 0.0
  height = max(4, int(round(width * gray.shape[0] / gray.shape[1])))
  small = cv2.resize(gray, (width, height), interpolation=cv2.INTER_AREA if width <= gray.shape[1] else cv2.INTER_LINEAR)
  small = cv2.GaussianBlur(small, (3, 3), 0)
  return float(np.percentile(np.abs(cv2.Laplacian(small, cv2.CV_32F)), 99))


# -- alignment --------------------------------------------------------------------------------------

@dataclass
class Alignment:
  ok: bool
  inliers: int = 0
  matches: int = 0
  shift_px: float | None = None  # the largest move of a frame corner, in preview pixels
  reason: str | None = None
  matrix: np.ndarray | None = None  # reference px -> capture px (both at ALIGN_WIDTH)

  def public(self) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": self.ok, "inliers": self.inliers}
    if self.shift_px is not None:
      out["shift_px"] = round(self.shift_px, 1)
    if self.reason:
      out["reason"] = self.reason
    return out


@lru_cache(maxsize=16)
def _features(path: str, mtime_ns: int) -> tuple[np.ndarray, np.ndarray, tuple[int, int]] | None:
  gray = read_gray(path)
  if gray is None:
    return None
  gray = _at_width(gray, ALIGN_WIDTH)
  points, descriptors = _orb(gray)
  return (points, descriptors, (gray.shape[1], gray.shape[0])) if descriptors is not None else None


def _at_width(gray: np.ndarray, width: int) -> np.ndarray:
  if gray.shape[1] == width:
    return gray
  return cv2.resize(gray, (width, int(round(gray.shape[0] * width / gray.shape[1]))), interpolation=cv2.INTER_AREA)


def _orb(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
  keypoints, descriptors = cv2.ORB_create(2000).detectAndCompute(gray, None)
  return np.float32([k.pt for k in keypoints]).reshape(-1, 2), descriptors


def align(reference: Path, gray: np.ndarray) -> Alignment:
  """The homography taking the reference's pixels to this capture's (both at ALIGN_WIDTH)."""
  try:
    ref = _features(str(reference), reference.stat().st_mtime_ns)
  except OSError:
    ref = None
  if ref is None:
    return Alignment(False, reason="the reference image cannot be read")
  ref_points, ref_desc, ref_size = ref
  moved = _at_width(gray, ALIGN_WIDTH)
  if abs(moved.shape[0] / moved.shape[1] - ref_size[1] / ref_size[0]) > 0.02:
    return Alignment(False, reason="the camera's picture has another shape than the reference (resolution changed)")
  points, desc = _orb(moved)
  if desc is None or len(points) < 8:
    return Alignment(False, reason="too few features in this capture")
  pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(ref_desc, desc, k=2)
  good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
  if len(good) < 8:
    return Alignment(False, matches=len(good), reason=f"only {len(good)} features match the reference")
  matrix, mask = cv2.findHomography(ref_points[[m.queryIdx for m in good]], points[[m.trainIdx for m in good]],
                                    cv2.RANSAC, 5.0)
  inliers = int(mask.sum()) if mask is not None else 0
  if matrix is None or inliers < ALIGN_MIN_INLIERS or inliers < ALIGN_MIN_RATIO * len(good):
    return Alignment(False, inliers, len(good), reason=f"only {inliers} of {len(good)} matches agree on one camera position")
  width, height = ref_size
  corners = np.float32([[0, 0], [width, 0], [width, height], [0, height]]).reshape(-1, 1, 2)
  warped = cv2.perspectiveTransform(corners, matrix).reshape(4, 2)
  shift = float(np.abs(warped - corners.reshape(4, 2)).max())
  area = abs(cv2.contourArea(warped)) / (width * height)
  if shift > ALIGN_MAX_SHIFT * width or not 0.6 <= area <= 1.6 or not cv2.isContourConvex(warped.reshape(-1, 1, 2)):
    return Alignment(False, inliers, len(good), shift, "the match moves the frame implausibly (the camera or the scene changed too much)")
  return Alignment(True, inliers, len(good), shift, matrix=matrix)


# -- labware geometry -------------------------------------------------------------------------------

@lru_cache(maxsize=64)
def labware_geometry(type_name: str) -> dict[str, Any] | None:
  """A plate's or tip rack's footprint and where its wells sit, as fractions of the footprint from its left and
  its back edge (A1 is at the back left): {size, rows, cols, pitch_mm (None: one container), wells}. None: no
  such labware."""
  import pylabrobot.resources as plr

  factory = getattr(plr, type_name, None)
  if factory is None:
    return None
  try:
    with warnings.catch_warnings():
      warnings.simplefilter("ignore")
      resource = factory(name="geometry")
      size_x, size_y = resource.get_size_x(), resource.get_size_y()
      rows, cols = getattr(resource, "num_items_y", 0), getattr(resource, "num_items_x", 0)
      if rows * cols <= 1:
        return {"size": (size_x, size_y), "rows": 1, "cols": 1, "pitch_mm": None, "wells": {}, "well_mm": None}
      wells = {}
      for r in range(rows):
        for c in range(cols):
          well_id = f"{'ABCDEFGHIJKLMNOP'[r]}{c + 1}"
          item = resource.get_item(well_id)
          wells[well_id] = ((item.location.x + item.get_size_x() / 2) / size_x,
                            1 - (item.location.y + item.get_size_y() / 2) / size_y)
      pitch = round(resource.get_item("A2").location.x - resource.get_item("A1").location.x, 2) if cols > 1 else None
      well_mm = resource.get_item("A1").get_size_x()
  except Exception:  # noqa: BLE001 - a labware PyLabRobot builds differently is just unknown here
    return None
  return {"size": (size_x, size_y), "rows": rows, "cols": cols, "pitch_mm": pitch, "wells": wells, "well_mm": well_mm}


# -- analysing a capture ----------------------------------------------------------------------------

def _crop_name(image: Path, key: str) -> Path:
  suffix = key if key.isdigit() else key.replace(":", "-").replace(".", "_")
  return image.with_name(f"{image.stem}_slot{suffix}.jpg" if key.isdigit() else f"{image.stem}_{suffix}.jpg")


def _entry(key: str, rbox: list[float], matrix: np.ndarray, gray: np.ndarray, scale: float, preview: tuple[int, int],
           view: str, labware: str | None, crop_to: Path | None) -> dict[str, Any]:
  x0, y0, x1, y1 = rbox
  quad = cv2.perspectiveTransform(np.float32([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]).reshape(-1, 1, 2), matrix).reshape(4, 2)
  bx0, by0 = (float(v) for v in quad.min(axis=0))
  bx1, by1 = (float(v) for v in quad.max(axis=0))  # the warped box as an upright box (a small tilt widens it a little)
  pw, ph = preview
  cx0, cy0, cx1, cy1 = max(bx0, 0), max(by0, 0), min(bx1, pw), min(by1, ph)
  box_area = max((bx1 - bx0) * (by1 - by0), 1e-6)
  visible = max(cx1 - cx0, 0) * max(cy1 - cy0, 0) / box_area
  entry: dict[str, Any] = {"slot": key, "box": [round(float(v), 1) for v in (bx0, by0, bx1, by1)],
                           "visible": round(float(visible), 2), "labware": labware}
  full = [int(round(v * scale)) for v in (cx0, cy0, cx1, cy1)]
  crop = gray[full[1]:full[3], full[0]:full[2]] if full[2] - full[0] >= 8 and full[3] - full[1] >= 8 else None
  sharpness = edge_sharpness(crop, 384) if crop is not None else 0.0
  entry["sharpness"] = round(sharpness, 1)
  width_px, height_px = (bx1 - bx0) * scale, (by1 - by0) * scale
  px_per_mm = (width_px / FOOTPRINT_MM[0] + height_px / FOOTPRINT_MM[1]) / 2
  geometry = labware_geometry(labware) if labware else None
  pitch = geometry["pitch_mm"] if geometry else DEFAULT_PITCH_MM
  if view == "top" and key.isdigit():
    entry["px_per_well"] = round(px_per_mm * pitch, 1) if pitch else None
  entry["level"], entry["why"] = _level(key, view, visible, sharpness, px_per_mm, entry.get("px_per_well"),
                                        pitch, min(width_px, height_px))
  if entry["why"] is None:
    del entry["why"]
  if crop is not None and crop_to is not None:
    # the colour crop, from the file on disk (the gray image has no colour)
    entry["_crop"] = (full, _crop_name(crop_to, key))
  return entry


def _level(key: str, view: str, visible: float, sharpness: float, px_per_mm: float, px_per_well: float | None,
           pitch: float | None, short_side: float) -> tuple[str, str | None]:
  if region_kind(key) == "tips":
    if sharpness < SLOT_SHARP_MIN:
      return "unclear", f"too blurry or covered (sharpness {sharpness:.1f} < {SLOT_SHARP_MIN:g})"
    if short_side < TIP_MIN_PX:
      return "unclear", f"the region is {short_side:.0f} px across; tips need at least {TIP_MIN_PX}"
    return "tip", None
  if visible < MIN_VISIBLE:
    return "unclear", f"only {visible:.0%} of the slot is inside the frame"
  if sharpness < SLOT_SHARP_MIN:
    return "unclear", f"too blurry or covered (sharpness {sharpness:.1f} < {SLOT_SHARP_MIN:g})"
  if view == "side":
    if short_side < SIDE_MIN_PX:
      return "unclear", f"the box is {short_side:.0f} px across; a slot needs at least {SIDE_MIN_PX}"
    return "slot", None  # a side view never resolves single wells
  if px_per_mm < SLOT_MIN_PX_PER_MM:
    return "unclear", f"too small ({px_per_mm:.2f} px/mm < {SLOT_MIN_PX_PER_MM:g})"
  if pitch is None or (px_per_well or 0) >= WELL_MIN_PX:
    return "well", None
  return "slot", None


def preview_matrix(matrix: np.ndarray, ref_w: float) -> np.ndarray:
  """The alignment (made at ALIGN_WIDTH) in the pixels of a map that is `ref_w` wide: a reference narrower than
  ALIGN_WIDTH (the OT-2's own 640 px camera) is aligned scaled up, and boxes live in its own pixels."""
  s = ALIGN_WIDTH / ref_w
  up, down = np.diag([s, s, 1.0]), np.diag([1 / s, 1 / s, 1.0])
  return down @ matrix @ up


def analyze(image: Path, slot_map: dict[str, Any], reference: Path, labware: dict[str, str] | None = None,
            save_crops: bool = True, plates: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
  """One capture against its camera's map: {status ok|unclear|needs_new_map, why, image_size (the preview's),
  frame_sharpness, alignment, slots: [per-slot entries, boxes in preview pixels]}. `labware`: slot -> labware type of
  the loaded layout. `plates`: slot -> a calibrated plate {corners (in the reference's pixels, A1, A<cols>, <last row>1,
  <last row><cols>), rows, cols, type}; its entry then carries `grid` (the corners in the preview's pixels, pitch_px and
  radius_px in the capture's own pixels). Crops are written next to the image when `save_crops`. Never raises for a bad image."""
  gray = read_gray(image)
  if gray is None:
    return {"status": "unclear", "why": "the image file cannot be read", "slots": []}
  ref_w, ref_h = slot_map["size"]
  scale = gray.shape[1] / ref_w
  preview = (ref_w, int(round(gray.shape[0] / scale)))
  frame = edge_sharpness(gray, 960)
  out: dict[str, Any] = {"status": "ok", "why": None, "image_size": list(preview), "frame_sharpness": round(frame, 1),
                         "slots": []}
  if frame < FRAME_SHARP_MIN:
    return {**out, "status": "unclear",
            "why": f"the frame is out of focus or covered (sharpness {frame:.1f} < {FRAME_SHARP_MIN:g}): capture again once nothing is in front of the lens"}
  result = align(reference, gray)
  out["alignment"] = result.public()
  if not result.ok:
    return {**out, "status": "needs_new_map",
            "why": f"{result.reason}: map the camera again (map_camera) if it moved or the scene changed"}
  view = slot_map.get("view", "top")
  matrix = preview_matrix(result.matrix, ref_w)
  entries = [_entry(key, box, matrix, gray, scale, preview, view, (labware or {}).get(key), image if save_crops else None)
             for key, box in slot_map["slots"].items()]
  for entry in entries:
    plate = (plates or {}).get(entry["slot"])
    if plate is not None:
      entry["grid"] = warped_grid(plate, matrix, scale)
  color = None
  for entry in entries:
    crop = entry.pop("_crop", None)
    if crop is not None:
      color = color if color is not None else cv2.imread(str(image))
      region, path = crop
      if color is not None and cv2.imwrite(str(path), color[region[1]:region[3], region[0]:region[2]], [cv2.IMWRITE_JPEG_QUALITY, 92]):
        entry["crop"] = str(path)
  return {**out, "slots": entries}


def well_positions(entry: dict[str, Any], labware: str, wells: list[str]) -> list[tuple[str, float, float]]:
  """Where each well sits in the preview, from the slot's box: [(well, x, y)]; the box may reach past the frame."""
  geometry = labware_geometry(labware)
  if not geometry or not geometry["wells"]:
    return []
  x0, y0, x1, y1 = entry["box"]
  return [(w, x0 + geometry["wells"][w][0] * (x1 - x0), y0 + geometry["wells"][w][1] * (y1 - y0))
          for w in wells if w in geometry["wells"]]


def wells_outside(entry: dict[str, Any], labware: str, wells: list[str], frame: list[int]) -> list[str]:
  """The wells whose centre (or half a pitch around it) is not inside the frame."""
  geometry = labware_geometry(labware)
  if not geometry:
    return []
  x0, y0, x1, y1 = entry["box"]
  half = (geometry["pitch_mm"] or 0) / 2 * ((x1 - x0) / FOOTPRINT_MM[0])
  return [w for w, x, y in well_positions(entry, labware, wells)
          if x - half < 0 or y - half < 0 or x + half > frame[0] or y + half > frame[1]]


# -- a calibrated plate: four clicked wells, a perspective grid ------------------------------------------

def well_ids(rows: int, cols: int) -> list[str]:
  return [f"{'ABCDEFGHIJKLMNOP'[r]}{c + 1}" for r in range(rows) for c in range(cols)]


def corner_wells(rows: int, cols: int) -> list[str]:
  """The four wells the person clicks, in order: A1, the last column of row A, the first column of the last row, and
  the last well (A1, A12, H1, H12 on a 96-well plate)."""
  last = "ABCDEFGHIJKLMNOP"[rows - 1]
  return ["A1", f"A{cols}", f"{last}1", f"{last}{cols}"]


def check_corners(corners: Any, size: tuple[float, float] | None = None) -> list[list[float]]:
  """Four clicked points [[x, y], ...] as floats; ValueError saying what is wrong. They must be the corners of a
  convex four-sided shape in the order A1, A<cols>, <last row>1, <last row><cols>, as a plate looks from above: a plate that
  is turned or seen at an angle is fine; a mirrored order (A12 and H1 swapped, say), a crossed shape or a collapsed one is not."""
  try:
    pts = [[float(x), float(y)] for x, y in corners]
  except (TypeError, ValueError) as exc:
    raise ValueError("give four points [x, y]") from exc
  if len(pts) != 4 or any(not np.isfinite(v) for p in pts for v in p):
    raise ValueError("give four points [x, y]")
  ring = [pts[0], pts[1], pts[3], pts[2]]  # around the plate: A1, A<cols>, <last row><cols>, <last row>1
  signed = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1])) / 2  # > 0: clockwise on the screen
  if not cv2.isContourConvex(np.float32(ring).reshape(-1, 1, 2)) or abs(signed) < 100:
    raise ValueError("the four points do not make a plate's corners: click A1, the last column of row A, the first column "
                     "of the last row, then the last well, in that order")
  if signed < 0:
    raise ValueError("the points are in a mirrored order (a plate seen from above runs A1 to the last column of row A "
                     "clockwise to the last well): click A1, the last column of row A, the first column of the last row, "
                     "then the last well")
  if size is not None and signed < MIN_GRID_SPAN * size[0] * size[1] * 0.05:
    raise ValueError("the four points are too close together to be a plate")
  return pts


def plate_grid(corners: list[list[float]], rows: int, cols: int, well_fraction: float | None = None) -> dict[str, Any]:
  """Every well's centre from the four corner wells, through a perspective transform (cv2.getPerspectiveTransform): the
  OT-2's own camera looks at the deck at an angle, so the grid is not a plain rectangle. -> {rows, cols, centres
  ([[x, y] per well, row by row]), pitch_px (the smallest distance between neighbouring wells), radius_px}."""
  src = np.float32([[0, 0], [cols - 1, 0], [0, rows - 1], [cols - 1, rows - 1]])
  transform = cv2.getPerspectiveTransform(src, np.float32(corners))
  grid = np.float32([[c, r] for r in range(rows) for c in range(cols)]).reshape(-1, 1, 2)
  centres = cv2.perspectiveTransform(grid, transform).reshape(rows, cols, 2)
  gaps = [float(np.linalg.norm(centres[r, c] - centres[r, c + 1])) for r in range(rows) for c in range(cols - 1)]
  gaps += [float(np.linalg.norm(centres[r, c] - centres[r + 1, c])) for r in range(rows - 1) for c in range(cols)]
  pitch = min(gaps)
  fraction = well_fraction or DEFAULT_WELL_FRACTION
  return {"rows": rows, "cols": cols, "centres": [[round(float(x), 2), round(float(y), 2)] for x, y in centres.reshape(-1, 2)],
          "pitch_px": round(pitch, 2), "radius_px": round(pitch * fraction / 2, 2)}


def well_fraction(type_name: str | None) -> float | None:
  """A well's diameter over the well pitch for this labware (None: unknown)."""
  geometry = labware_geometry(type_name) if type_name else None
  if geometry and geometry.get("well_mm") and geometry.get("pitch_mm"):
    return min(0.95, geometry["well_mm"] / geometry["pitch_mm"])
  return None


def warped_grid(plate: dict[str, Any], matrix: np.ndarray, scale: float) -> dict[str, Any]:
  """A calibrated plate's grid in a capture: its corners (the reference's pixels) through the capture's alignment
  `matrix`, then the grid of well centres in the preview's pixels. pitch_px and radius_px are in the capture's own
  pixels (preview pixels times `scale`), the unit of the other per-well measures (WELL_MIN_PX)."""
  pts = cv2.perspectiveTransform(np.float32(plate["corners"]).reshape(-1, 1, 2), matrix).reshape(4, 2)
  grid = plate_grid(pts.tolist(), plate["rows"], plate["cols"], well_fraction(plate.get("type")))
  return {"rows": grid["rows"], "cols": grid["cols"], "centres": grid["centres"],
          "pitch_px": round(grid["pitch_px"] * scale, 2), "radius_px": round(grid["radius_px"] * scale, 2), "scale": scale,
          "corners": [[round(float(x), 1), round(float(y), 1)] for x, y in pts]}


def grid_wells_outside(grid: dict[str, Any], wells: list[str], frame: list[int]) -> list[str]:
  """The wells of a calibrated plate whose centre, or half a pitch around it, is not inside the frame."""
  half = grid["pitch_px"] / grid.get("scale", 1.0) / 2
  index = {w: i for i, w in enumerate(well_ids(grid["rows"], grid["cols"]))}
  out = []
  for w in wells:
    if w not in index:
      continue
    x, y = grid["centres"][index[w]]
    if x - half < 0 or y - half < 0 or x + half > frame[0] or y + half > frame[1]:
      out.append(w)
  return out


def compare_to_reference(reference: Path, gray: np.ndarray, ref_size: tuple[int, int]) -> dict[str, Any]:
  """Has the camera moved since its reference image was taken? ORB alignment of a fresh frame against it:
  {result: unmoved | moved | could_not_compare, shift_px (the largest move of a frame corner, in the frame's pixels),
  inliers, text, matrix (reference -> fresh frame, at ALIGN_WIDTH; None unless aligned)}."""
  if gray is None:
    return {"result": "could_not_compare", "shift_px": None, "inliers": 0, "matrix": None,
            "text": "could not compare: the picture cannot be read"}
  result = align(reference, gray)
  if not result.ok:
    return {"result": "could_not_compare", "shift_px": None, "inliers": result.inliers, "matrix": None,
            "text": f"could not compare: {result.reason}"}
  shift = float(result.shift_px or 0.0) * ref_size[0] / ALIGN_WIDTH  # in the frame's own pixels
  if shift < MOVED_PX:
    return {"result": "unmoved", "shift_px": round(shift, 1), "inliers": result.inliers, "matrix": result.matrix,
            "text": "camera has not moved"}
  return {"result": "moved", "shift_px": round(shift, 1), "inliers": result.inliers, "matrix": result.matrix,
          "text": f"camera moved about {shift:.0f} px"}


def overlay(reference_jpeg: bytes, slots: dict[str, list[float]]) -> bytes:
  """The reference image with each box drawn and labelled, for the agent to check its proposal."""
  image = cv2.imdecode(np.frombuffer(reference_jpeg, np.uint8), cv2.IMREAD_COLOR)
  if image is None:
    return reference_jpeg
  thick = max(2, image.shape[1] // 400)
  for key, (x0, y0, x1, y1) in slots.items():
    top = region_kind(key) == "tips"
    color = (0, 140, 255) if top else (0, 200, 0)
    cv2.rectangle(image, (int(x0), int(y0)), (int(x1), int(y1)), color, thick)
    label = key.removeprefix("tips:") if top else f"slot {key}"
    cv2.putText(image, label, (int(x0) + 6, int(max(y0, 0)) + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), thick + 2)
    cv2.putText(image, label, (int(x0) + 6, int(max(y0, 0)) + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, thick)
  ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
  return data.tobytes() if ok else reference_jpeg


def read_gray_bytes(data: bytes) -> np.ndarray | None:
  """Grayscale pixels from JPEG bytes; None when they are no image."""
  if not data:
    return None
  return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
