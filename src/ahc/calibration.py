"""Calibration: the person's part of seeing the deck with a camera, done on the dashboard's /calibration page.

Two kinds, both for real hardware with cameras configured:

  slot calibration    each camera's slot boxes (where each deck slot is in its picture). A camera is done for this server
                      session once the person saved its boxes (calibrated), said they are still right (confirmed: a fresh
                      frame was compared with the camera's reference image by ahc.vision, it says whether the camera
                      has moved) or skipped it. Until every camera is done, load_layout is refused (calibration_required).
                      Still right and Skip are per session and change nothing in the config; saved boxes are a config
                      change (the person confirms the config again) and the fresh frame becomes the camera's reference.
  plate calibration   the person clicks four wells of a plate (A1, A<cols>, <last row>1, <last row><cols>) in a camera's
                      picture; a perspective transform gives every well's centre and radius (ahc.vision.plate_grid).
                      The agent asks for it with request_plate_calibration. It belongs to the loaded layout: the next
                      load_layout clears it, and the page then offers the earlier corners over a fresh frame to accept
                      with one click.

Every action drops the last physical_world_context (and the camera's analysis of its latest capture, made with the old
boxes): auto-verify stays off with the reason until the agent runs physical_world_context again. The agent's track
record (vision_record.json) is kept.

State: .ahc/state/calibration.json (this server's cameras and plates, for the dashboard: it holds the server's pid) and
.ahc/state/plate_calibration.json (the plates of the loaded layout, and the earlier corners a reload offers again).
Per-session state is not config: it never enters the config digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ahc import cameras as cams
from ahc import vision
from ahc import vision_points as vpoints
from ahc.core.errors import LabError
from ahc.workspace import estop
from ahc.workspace.config import HEADER, CameraEntry, parse_slot_map
from ahc.workspace.store import write_atomic

DONE = ("calibrated", "confirmed", "skipped")  # what clears a camera for this session
ACTION_TIMEOUT_S = 60.0  # an HTTP thread waits this long for the server's loop


def _now() -> str:
  return time.strftime("%Y-%m-%dT%H:%M:%S")


def _sig(camera: CameraEntry) -> str:
  """What a camera's done state was given for: its config entry (the slot map included)."""
  return hashlib.sha256(json.dumps(camera.data(), sort_keys=True).encode()).hexdigest()[:16]


def write_bytes(path: Path, data: bytes) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
  try:
    tmp.write_bytes(data)
    os.replace(tmp, path)
  except BaseException:
    tmp.unlink(missing_ok=True)
    raise


def read_state(ws: Any) -> dict[str, Any] | None:
  """The calibration state the AHC server of this folder wrote, if that server is still running (else None)."""
  try:
    data = json.loads((ws.state_dir / "calibration.json").read_text())
  except (OSError, ValueError):
    return None
  if not isinstance(data, dict) or not estop.pid_alive(data.get("pid")):
    return None
  return data


class Calibration:
  """The calibration of one AHC server session (`lab.calibration`). Actions are coroutines for the server's loop; the
  dashboard's HTTP threads reach them through call()."""

  def __init__(self, lab: Any):
    self.lab = lab
    self.states: dict[str, dict[str, Any]] = {}  # camera -> {state, sig, at}: done in this session
    self.frames: dict[str, dict[str, Any]] = {}  # camera -> the fresh frame the page works on (memory only)
    self.plates: dict[str, dict[str, Any]] = {}  # labware name -> request and corners, for the loaded layout
    self.stale: str | None = None  # why the last physical_world_context no longer counts
    self.earlier = self._read_earlier()

  # -- who needs what -------------------------------------------------------------------------

  def active(self) -> bool:
    """Calibration applies: real hardware with cameras configured (a simulation shows no real deck)."""
    cfg = self.lab.config
    return bool(cfg is not None and cfg.cameras and cfg.devices and not self.lab.forced_sim
                and self.lab._real_robot() is not None)

  def cameras(self) -> list[CameraEntry]:
    return list(self.lab.config.cameras) if self.lab.config is not None else []

  def camera(self, name: str) -> CameraEntry:
    for c in self.cameras():
      if c.name == name:
        return c
    raise LabError("unknown_camera", f"no camera {name!r} in this config.",
                   f"Cameras: {', '.join(c.name for c in self.cameras()) or 'none'}.")

  def state(self, camera: CameraEntry) -> str:
    """needs_map (the agent maps it first), pending, calibrated, confirmed or skipped. A done state was given for the
    camera's config entry as it is: a new slot map (map_camera) or another device makes it pending again."""
    held = self.states.get(camera.name)
    if held and held["sig"] == _sig(camera):
      return held["state"]
    return "pending" if camera.slot_map else "needs_map"

  def skipped(self, name: str) -> bool:
    return any(c.name == name and self.state(c) == "skipped" for c in self.cameras())

  def outstanding(self) -> list[CameraEntry]:
    """The cameras not done this session (needs_map included): what the gate waits for."""
    return [c for c in self.cameras() if self.state(c) not in DONE] if self.active() else []

  def for_person(self) -> list[str]:
    """The outstanding cameras the person can calibrate now (they have a slot map)."""
    return [c.name for c in self.outstanding() if c.slot_map]

  def page_url(self) -> str | None:
    shown = self.lab.dashboard
    return shown["url"].rstrip("/") + "/calibration" if shown else None

  def where(self) -> str:
    url = self.page_url()
    return f"the calibration page ({url})" if url else "the calibration page (open_dashboard gives its address: /calibration)"

  def camera_next(self, camera: CameraEntry) -> str:
    state = self.state(camera)
    if state == "needs_map":
      return (f"map_camera(camera='{camera.name}') first: the camera has no slot map. The person then adjusts the "
              f"boxes on {self.where()}.")
    if state == "pending":
      return f"The person finishes camera {camera.name} on {self.where()}: adjusts and saves the boxes, says they are still right, or skips it."
    return "done for this session"

  def next_text(self) -> str:
    """lab_overview.next: calibration before the first load_layout, when a camera waits for it."""
    left = self.outstanding()
    if not left:
      return ""
    parts = [self.camera_next(c) for c in left]
    return "Calibration comes before load_layout: " + " ".join(parts) + " "

  def require_done(self) -> None:
    """The gate of load_layout: every camera calibrated, confirmed still right, or skipped in this session."""
    left = self.outstanding()
    if not left:
      return
    names = ", ".join(c.name for c in left)
    needs = [c.name for c in left if not c.slot_map]
    steps = []
    if needs:
      steps.append(f"map_camera for {', '.join(needs)} (no slot map yet)")
    if self.for_person():
      steps.append(f"the person finishes {', '.join(self.for_person())} on {self.where()}")
    raise LabError("calibration_required",
                   f"the camera{'s' if len(left) > 1 else ''} {names} {'are' if len(left) > 1 else 'is'} not calibrated in this "
                   "session, so no layout is loaded yet.",
                   "Tell the person; then " + "; then ".join(steps) + ". Calibration is the person's: each camera is calibrated "
                   "(boxes saved), confirmed still right, or skipped (a skipped camera still takes photos but covers nothing). "
                   "Call load_layout again when lab_overview.calibration says it is done.")

  def overview(self) -> dict[str, Any] | None:
    """lab_overview's `calibration`: per camera its state and next step, the page, what changed for auto-verify."""
    if self.lab.config is None or not self.lab.config.cameras or not self.active():
      return None
    out: dict[str, Any] = {"url": self.page_url() or "open_dashboard gives the dashboard; the page is /calibration",
                           "done": not self.outstanding(),
                           "cameras": {c.name: {"state": self.state(c), "next": self.camera_next(c)} for c in self.cameras()}}
    if self.stale:
      out["auto_verify_note"] = self.stale
    plates = self.plates_public()
    if plates:
      out["plates"] = plates
    return out

  def redirect(self) -> bool:
    """The control dashboard sends the person to the calibration page: a camera waits for them."""
    return bool(self.for_person())

  # -- the state files ------------------------------------------------------------------------

  def _read_earlier(self) -> dict[str, dict[str, Any]]:
    try:
      data = json.loads((self.lab.ws.state_dir / "plate_calibration.json").read_text())
    except (OSError, ValueError):
      return {}
    earlier = data.get("earlier") if isinstance(data, dict) else None
    return {k: v for k, v in earlier.items() if isinstance(v, dict)} if isinstance(earlier, dict) else {}

  def write_state(self) -> None:
    """Both state files. Never raises: the dashboard just shows an older picture."""
    try:
      ws = self.lab.ws
      if not ws.root.is_dir() or not ws.dir.is_dir():
        return
      ws.state_dir.mkdir(parents=True, exist_ok=True)
      cameras = {c.name: {"state": self.state(c), "device": c.device} for c in self.cameras()}
      body = {"pid": os.getpid(), "at": _now(), "active": self.active(), "cameras": cameras,
              "waiting_for_person": self.for_person(), "plates": self.plates_public(), "stale": self.stale}
      write_atomic(ws.state_dir / "calibration.json", json.dumps(body, indent=2) + "\n")
      layout = self.lab.gate.layout if self.lab.gate is not None else None
      write_atomic(ws.state_dir / "plate_calibration.json", json.dumps(
        {"layout": layout, "run_id": self.lab._run_id, "plates": self.plates, "earlier": self.earlier}, indent=2) + "\n")
    except Exception:  # noqa: BLE001
      pass

  def forget(self) -> None:
    """The server ends: its state file goes (a page reading it would think a stale server is still there)."""
    try:
      (self.lab.ws.state_dir / "calibration.json").unlink(missing_ok=True)
    except OSError:
      pass

  # -- one action changes what the cameras can vouch for --------------------------------------

  def acted(self, camera: str, event: str, **detail: Any) -> None:
    """Every calibration action: the last physical_world_context and the camera's latest analysis no longer count, so
    auto-verify is off until the agent runs physical_world_context again; the run log holds the action."""
    self.stale = (f"the calibration changed ({event} for {camera}, {time.strftime('%H:%M:%S')}): "
                  "physical_world_context must run again before the cameras count")
    self.lab.vision_result = None
    self.lab.vision_seen.pop(camera, None)
    self.lab.write_auto_state()
    self.lab.record({"type": "calibration", "event": event, "camera": camera, **detail}, defer=True)
    self.write_state()

  def checked_again(self) -> None:
    """physical_world_context ran: what the cameras cover now is measured with the new calibration."""
    if self.stale:
      self.stale = None
      self.write_state()

  # -- a fresh frame --------------------------------------------------------------------------

  async def look(self, name: str) -> dict[str, Any]:
    """A fresh frame (Lab.snapshot), compared with the camera's reference image: has the camera moved? -> the frame's
    public description. The frame stays in memory for the page; it is not a capture and not evidence."""
    camera = self.camera(name)
    if not camera.slot_map:
      raise LabError("no_slot_map", f"camera {name} has no slot map yet.", f"The agent maps it first: map_camera(camera='{name}').")
    with tempfile.TemporaryDirectory(prefix="ahc-calib-") as tmp:
      dest = Path(tmp) / "frame.jpg"
      failure = await self.lab.snapshot(camera, dest, time.time())
      if failure is not None:
        raise LabError(failure.code if camera.is_robot else "capture_failed", f"{name}: {failure.why}",
                       failure.hint or "Check that the camera is plugged in, not used by another app, and allowed for this terminal.")
      jpeg = dest.read_bytes() if camera.is_robot else await asyncio.to_thread(cams.downscaled, dest)
    gray = vision.read_gray_bytes(jpeg)
    if gray is None:
      raise LabError("capture_failed", f"{name} gave an image that cannot be read.", "Take the picture again.")
    size = [gray.shape[1], gray.shape[0]]
    slot_map, reference = camera.slot_map, self.lab.reference_path(camera)
    matrix, boxes = None, {k: [float(v) for v in b] for k, b in slot_map["slots"].items()}
    scale = size[0] / slot_map["size"][0]
    try:
      same = not slot_map.get("sha256") or hashlib.sha256(reference.read_bytes()).hexdigest() == slot_map["sha256"]
    except OSError:
      same = False
    if not same:
      compare = {"result": "could_not_compare", "shift_px": None, "inliers": 0, "matrix": None,
                 "text": "could not compare: the reference image is not the one this map was made on"}
    else:
      compare = await asyncio.to_thread(vision.compare_to_reference, reference, gray, tuple(slot_map["size"]))
    boxes = {k: [v * scale for v in b] for k, b in boxes.items()}
    if compare["matrix"] is not None:
      matrix = np.diag([scale, scale, 1.0]) @ vision.preview_matrix(compare["matrix"], slot_map["size"][0])
      if compare["result"] == "moved":  # the saved boxes where the deck is now
        boxes = {k: _box_through(matrix, b0) for k, b0 in slot_map["slots"].items()}
    self.frames[name] = {"t": time.time(), "jpeg": jpeg, "size": size, "matrix": matrix, "scale": scale,
                         "compare": {k: v for k, v in compare.items() if k != "matrix"}, "boxes": boxes,
                         "reference_sha": slot_map.get("sha256")}
    return self.frame_public(name)

  def frame_public(self, name: str) -> dict[str, Any]:
    f = self.frames[name]
    return {"camera": name, "t": f["t"], "size": f["size"], "compare": f["compare"], "boxes": f["boxes"],
            "image": f"/calibration/{name}/frame.jpg?t={int(f['t'] * 1000)}"}

  def frame_bytes(self, name: str) -> bytes | None:
    f = self.frames.get(name)
    return f["jpeg"] if f else None

  # -- the person's decisions about a camera --------------------------------------------------

  def _mark(self, camera: CameraEntry, state: str) -> None:
    self.states[camera.name] = {"state": state, "sig": _sig(camera), "at": _now()}

  async def still_right(self, name: str) -> dict[str, Any]:
    """The boxes are still right: done for this session. The config, and its digest, stay as they are."""
    camera = self.camera(name)
    if not camera.slot_map:
      raise LabError("no_slot_map", f"camera {name} has no slot map yet.", f"The agent maps it first: map_camera(camera='{name}').")
    compare = (self.frames.get(name) or {}).get("compare") or {}
    self._mark(camera, "confirmed")
    self.acted(name, "still_right", comparison=compare.get("text"))
    return self.public()

  async def skip(self, name: str) -> dict[str, Any]:
    """The camera is left out for this session: it still takes photos but covers nothing. A camera with no slot map
    can be skipped too."""
    camera = self.camera(name)
    self._mark(camera, "skipped")
    self.acted(name, "skipped")
    return self.public()

  async def save_boxes(self, name: str, slots: Any) -> dict[str, Any]:
    """The person's boxes, drawn over the fresh frame. The frame becomes the camera's reference image; the config is
    written (its digest changes: pending until confirm_config) and the track record moves to the new maps digest."""
    lab, camera = self.lab, self.camera(name)
    if not camera.slot_map:
      raise LabError("no_slot_map", f"camera {name} has no slot map yet.", f"The agent maps it first: map_camera(camera='{name}').")
    frame = self.frames.get(name)
    if frame is None:
      raise LabError("no_frame", f"no fresh picture of {name} to put the boxes on.", "Take a new picture on the page first.")
    if not isinstance(slots, dict) or set(slots) != set(camera.slot_map["slots"]):
      raise LabError("bad_slot_map", "save the boxes of the slots the map has, no more and no fewer.", "Move or resize the boxes; do not add or drop slots.")
    reference = lab.reference_path(camera)
    try:
      new = parse_slot_map({**camera.slot_map, "size": frame["size"], "sha256": hashlib.sha256(frame["jpeg"]).hexdigest(),
                            "slots": slots}, name)
    except LabError as exc:
      raise LabError("bad_slot_map", exc.message, "Boxes are [x0, y0, x1, y1] in the picture's pixels, x0 < x1, y0 < y1.") from exc
    width, height = frame["size"]
    outside = [k for k, (x0, y0, x1, y1) in new["slots"].items() if x1 <= 0 or y1 <= 0 or x0 >= width or y0 >= height]
    if outside:
      raise LabError("box_outside_frame", f"no part of {', '.join(outside)} is inside the {width} x {height} picture.",
                     "Keep part of every box in the picture.")
    old_digest = vpoints.maps_digest(lab.config)
    await asyncio.to_thread(write_bytes, reference, frame["jpeg"])
    cameras_now = [replace(c, slot_map=new) if c.name == name else c for c in lab.config.cameras]
    config = replace(lab.config, cameras=cameras_now)
    lab.ws.write_config(config.data(), HEADER)
    lab._audited = config.digest()  # written here, by the person's save: not an outside edit
    await lab.refresh(write=False)
    vpoints.VisionRecord(lab.ws).carry(old_digest, vpoints.maps_digest(lab.config))
    self._mark(self.camera(name), "calibrated")
    for plate in self.plates.values():  # corners are in the old reference's pixels
      if plate["camera"] == name and plate.get("corners"):
        plate.update(corners=None, ref_sha=None, note="the camera's boxes were saved again: click the corner wells again")
    self.frames.pop(name, None)
    self.acted(name, "boxes_saved", config_digest=lab.config.digest() if lab.config else None)
    return self.public()

  # -- plate calibration ----------------------------------------------------------------------

  def request_plate(self, labware: str, camera: str, why: str, plan_step: int | None) -> dict[str, Any]:
    """The agent asks the person to click a plate's four corner wells. Refused with a code when the plate or the
    camera cannot do it."""
    lab = self.lab
    layout = lab.loaded_layout()
    if layout is None:
      raise LabError("no_layout", "no layout is loaded yet.", "load_layout first; then ask for the plate's calibration.")
    if lab.adapter is not None and lab.adapter.simulated:
      raise LabError("camera_not_in_simulation", "this server runs a simulation: a camera shows the real deck.",
                     "Plate calibration is for real hardware.")
    if not why.strip():
      raise LabError("bad_reason", "why is empty.", "Say in one line why the plate needs calibrating (e.g. 'to check wells A1:A8 by camera').")
    slots = vpoints.layout_slots(layout)
    where = {v["name"]: (k, v["type"]) for k, v in slots.items()}
    if labware not in getattr(layout, "labware", {}):
      raise LabError("unknown_labware", f"{labware!r} is not in the loaded layout.",
                     f"Labware in it: {', '.join(sorted(layout.labware)) or 'none'}.")
    if labware not in where:
      raise LabError("labware_not_in_slot", f"{labware!r} sits in no slot, so no camera's slot box covers it.",
                     "Only labware in a deck slot can be calibrated by a camera.")
    slot, kind = where[labware]
    geometry = vision.labware_geometry(kind)
    if not geometry or geometry["rows"] < 2 or geometry["cols"] < 2:
      raise LabError("not_a_plate", f"{labware!r} ({kind}) has no grid of wells to calibrate.",
                     "Calibrate plates and racks with at least two rows and two columns.")
    entry = self.camera(camera)
    if not entry.slot_map:
      raise LabError("no_slot_map", f"camera {camera} has no slot map yet.", f"map_camera(camera='{camera}') first.")
    if slot not in entry.slot_map["slots"] or entry.slot_map.get("view", "top") != "top":
      raise LabError("not_covered_by_camera", f"camera {camera}'s slot boxes do not cover slot {slot} (where {labware!r} is).",
                     f"Cameras that do: {', '.join(c.name for c in self.cameras() if c.slot_map and slot in c.slot_map['slots'] and c.slot_map.get('view', 'top') == 'top') or 'none'}.")
    if self.state(entry) in ("pending", "needs_map"):
      raise LabError("calibration_required", f"camera {camera} is not calibrated in this session.", f"The person finishes it on {self.where()} first.")
    if self.state(entry) == "skipped":
      raise LabError("camera_skipped", f"the person skipped camera {camera} for this session.", "Ask for another camera that covers the slot, or leave the plate to the person.")
    held = self.plates.get(labware)
    record = {"labware": labware, "slot": slot, "type": kind, "camera": camera, "rows": geometry["rows"], "cols": geometry["cols"],
              "why": why.strip(), "plan_step": plan_step, "requested_at": _now(), "corners": None, "ref_sha": None,
              "wells": vision.corner_wells(geometry["rows"], geometry["cols"])}
    if held and held.get("camera") == camera and held.get("corners") and held.get("ref_sha") == entry.slot_map.get("sha256"):
      record.update(corners=held["corners"], ref_sha=held["ref_sha"], calibrated_at=held.get("calibrated_at"), source=held.get("source"))
      record["requested_again"] = True
    self.plates[labware] = record
    lab.record({"type": "calibration", "event": "plate_requested", "labware": labware, "camera": camera, "slot": slot,
                "why": record["why"], "plan_step": plan_step}, defer=True)
    self.write_state()
    return {"labware": labware, "slot": slot, "camera": camera, "wells_to_click": record["wells"], "url": self.page_url(),
            "next": (f"The person clicks wells {', '.join(record['wells'])} of {labware} on {self.where()}. Report "
                     "report_decision(waiting_for='person') and wait; physical_world_context says when it is calibrated. Motion is not held.")}

  def earlier_for(self, plate: dict[str, Any]) -> dict[str, Any] | None:
    """The corners an earlier load had for this plate's place, when they are in the camera's current reference."""
    held = self.earlier.get(f"{plate['camera']}|{plate['slot']}|{plate['type']}")
    cam = next((c for c in self.cameras() if c.name == plate["camera"]), None)
    if held and cam and cam.slot_map and held.get("ref_sha") == cam.slot_map.get("sha256") and held.get("corners"):
      return held
    return None

  async def save_plate(self, labware: str, corners: Any, earlier: bool = False) -> dict[str, Any]:
    """The person's four clicked wells (in the frame's pixels), or (earlier) the corners of an earlier load, accepted."""
    plate = self.plates.get(labware)
    if plate is None:
      raise LabError("no_plate_request", f"no plate calibration is requested for {labware!r}.", "The agent asks with request_plate_calibration.")
    camera = self.camera(plate["camera"])
    if not camera.slot_map:
      raise LabError("no_slot_map", f"camera {camera.name} has no slot map.", "The agent maps it first.")
    if earlier:
      held = self.earlier_for(plate)
      if held is None:
        raise LabError("no_earlier_corners", "there are no earlier corners for this plate in the camera's current picture.",
                       "Click the corner wells.")
      ref_corners, source = held["corners"], "earlier"
    else:
      frame = self.frames.get(plate["camera"])
      if frame is None:
        raise LabError("no_frame", "no fresh picture to click on.", "Take a new picture on the page first.")
      if frame["matrix"] is None and frame["compare"]["result"] != "unmoved":
        raise LabError("cannot_compare", "this picture could not be compared with the camera's reference image, so the corners "
                       "cannot be tied to it.", "Save the camera's boxes again (the fresh picture becomes its reference), or take a new picture.")
      try:
        pts = vision.check_corners(corners, tuple(frame["size"]))
      except ValueError as exc:
        raise LabError("bad_corners", str(exc), "Click A1, then the last column of row A, then the first column of the last row, then the last well.") from exc
      ref_corners = _to_reference(frame, pts)
      source = "clicked"
    plate.update(corners=[[round(x, 2), round(y, 2)] for x, y in ref_corners], ref_sha=camera.slot_map.get("sha256"),
                 calibrated_at=_now(), source=source)
    plate.pop("note", None)
    self.earlier[f"{plate['camera']}|{plate['slot']}|{plate['type']}"] = {
      "corners": plate["corners"], "ref_sha": plate["ref_sha"], "labware": labware, "at": plate["calibrated_at"]}
    self.acted(plate["camera"], "plate_still_right" if earlier else "plate_saved", labware=labware, slot=plate["slot"],
               corners=plate["corners"], source=source)
    return self.public()

  def plate_for_page(self, labware: str) -> dict[str, Any] | None:
    return self.plates.get(labware)

  def on_load(self) -> None:
    """A layout loaded: its plates are not the next one's. The corners stay as the earlier ones a reload offers."""
    for plate in self.plates.values():
      if plate.get("corners"):
        self.earlier[f"{plate['camera']}|{plate['slot']}|{plate['type']}"] = {
          "corners": plate["corners"], "ref_sha": plate["ref_sha"], "labware": plate["labware"], "at": plate.get("calibrated_at")}
    self.plates = {}
    self.write_state()

  def calibrated_plates(self, camera: str) -> dict[str, dict[str, Any]]:
    """slot -> the plates of this camera the person calibrated against its current reference (ahc.vision.analyze)."""
    cam = next((c for c in self.cameras() if c.name == camera), None)
    out = {}
    for plate in self.plates.values():
      if (cam is not None and cam.slot_map and plate["camera"] == camera and plate.get("corners")
          and plate.get("ref_sha") == cam.slot_map.get("sha256")):
        out[plate["slot"]] = {"corners": plate["corners"], "rows": plate["rows"], "cols": plate["cols"], "type": plate["type"]}
    return out

  def plate_status(self) -> dict[str, str]:
    """labware -> calibrated, requested (waiting for the person) for this load."""
    out = {}
    for name, plate in self.plates.items():
      cam = next((c for c in self.cameras() if c.name == plate["camera"]), None)
      ok = plate.get("corners") and cam is not None and cam.slot_map and plate.get("ref_sha") == cam.slot_map.get("sha256")
      out[name] = "calibrated" if ok else "requested"
    return out

  def plates_public(self) -> dict[str, Any]:
    status = self.plate_status()
    return {n: {"camera": p["camera"], "slot": p["slot"], "state": status[n], "why": p["why"], "plan_step": p.get("plan_step"),
                "wells": p["wells"], "calibrated_at": p.get("calibrated_at"), "source": p.get("source")}
            for n, p in self.plates.items()}

  # -- the page's view ------------------------------------------------------------------------

  def public(self) -> dict[str, Any]:
    """Everything the page draws, as the server holds it now."""
    cameras = []
    for c in self.cameras():
      state = self.state(c)
      cam: dict[str, Any] = {"name": c.name, "device": c.device, "source": c.source, "state": state,
                             "done_at": (self.states.get(c.name) or {}).get("at") if state in DONE else None,
                             "next": self.camera_next(c)}
      if c.slot_map:
        cam.update(size=c.slot_map["size"], view=c.slot_map.get("view", "top"), slots=c.slot_map["slots"],
                   reference=f"/camera/{c.name}/reference.jpg?v={(c.slot_map.get('sha256') or '')[:8]}")
      if c.name in self.frames:
        cam["frame"] = self.frame_public(c.name)
      cameras.append(cam)
    plates = []
    for name, plate in self.plates.items():
      held = self.earlier_for(plate)
      item = {**{k: plate.get(k) for k in ("labware", "slot", "type", "camera", "rows", "cols", "why", "plan_step", "wells",
                                          "calibrated_at", "source", "note")},
              "state": self.plate_status()[name], "fraction": vision.well_fraction(plate["type"]) or vision.DEFAULT_WELL_FRACTION,
              "corners_in_frame": self._in_frame(plate["camera"], plate.get("corners"), plate.get("ref_sha")),
              "earlier": bool(held and plate.get("source") != "earlier"),
              "earlier_corners": self._in_frame(plate["camera"], held["corners"], held["ref_sha"]) if held else None}
      plates.append(item)
    return {"active": self.active(), "cameras": cameras, "plates": plates, "stale": self.stale,
            "auto_verify": bool(self.lab.config is not None and self.lab.config.auto_verify),
            "run_page": "/?page=run", "waiting_for_person": self.for_person()}

  def _in_frame(self, camera: str, corners: list[list[float]] | None, ref_sha: str | None) -> list[list[float]] | None:
    """Corners kept in the camera's reference pixels, in its current fresh frame (None without a frame, an alignment, or
    when the reference is another one)."""
    frame = self.frames.get(camera)
    if not corners or frame is None:
      return None
    cam = next((c for c in self.cameras() if c.name == camera), None)
    if cam is None or not cam.slot_map or ref_sha != cam.slot_map.get("sha256"):
      return None
    if frame["matrix"] is not None:
      return _through(frame["matrix"], corners)
    if frame["compare"]["result"] == "unmoved":
      return [[x * frame["scale"], y * frame["scale"]] for x, y in corners]
    return None

  # -- from the dashboard's HTTP threads ------------------------------------------------------

  def call(self, coro: Any) -> Any:
    """Run an action on the server's loop and wait for it (the dashboard's threads are not the loop's)."""
    loop = getattr(self.lab, "loop", None)
    if loop is None:
      coro.close()
      raise LabError("server_not_ready", "the server's loop is not known yet.", "Open the page again.")
    return asyncio.run_coroutine_threadsafe(coro, loop).result(ACTION_TIMEOUT_S)


def _through(matrix: np.ndarray, points: list[list[float]]) -> list[list[float]]:
  moved = cv2.perspectiveTransform(np.float32(points).reshape(-1, 1, 2), matrix).reshape(-1, 2)
  return [[float(x), float(y)] for x, y in moved]


def _box_through(matrix: np.ndarray, box: list[float]) -> list[float]:
  x0, y0, x1, y1 = box
  quad = np.array(_through(matrix, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]))
  return [float(quad[:, 0].min()), float(quad[:, 1].min()), float(quad[:, 0].max()), float(quad[:, 1].max())]


def _to_reference(frame: dict[str, Any], points: list[list[float]]) -> list[list[float]]:
  """Points in the fresh frame's pixels into the camera's reference image's pixels (the alignment, inverted)."""
  if frame["matrix"] is None:  # unmoved, and no alignment kept: the same place, in the reference's scale
    return [[x / frame["scale"], y / frame["scale"]] for x, y in points]
  return _through(np.linalg.inv(frame["matrix"]), points)
