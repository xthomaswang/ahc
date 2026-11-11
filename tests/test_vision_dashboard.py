"""The dashboard's camera data: the reference image (both pages) and the coverage it lists. The slot-box editor is on
the calibration page (test_calibration.py)."""

import json
import re

import pytest

from ahc.viz.dashboard import replay, serve
from ahc.workspace import CameraEntry, Config, DeviceEntry, parse_config
from ahc.workspace.config import HEADER
from ahc.workspace.store import Workspace
from test_dashboard import _http
from vision_images import H, W, deck, jpeg, slot_box

REFERENCE = jpeg(deck())
SLOTS = {"2": slot_box(2), "3": slot_box(3), "tips:ot2.right": [10.0, 10.0, 90.0, 90.0]}
MAP = {"reference": "cameras/usb/reference.jpg", "size": [W, H], "view": "top", "slots": {k: [round(v, 1) for v in b] for k, b in SLOTS.items()}}


@pytest.fixture
def task(tmp_path):
  """A task folder whose config holds a confirmed camera map, with a run log so /state answers."""
  ws = Workspace(tmp_path / "task")
  ws.root.mkdir()
  config = Config(devices=[DeviceEntry("starlet", "hamilton.starlet", "sim")], limits={},
                  cameras=[CameraEntry("usb", "USB Camera", MAP), CameraEntry("rs", "Other")])
  config.confirm("human")
  ws.write_config(config.data(), HEADER)
  (ws.cameras_dir / "usb").mkdir(parents=True)
  (ws.cameras_dir / "usb" / "reference.jpg").write_bytes(REFERENCE)
  ws.runs_dir.mkdir(parents=True, exist_ok=True)
  (ws.runs_dir / "r.jsonl").write_text(json.dumps({"type": "run", "run_id": "r", "device": "hamilton.starlet", "backend": "sim"}) + "\n")
  return ws


@pytest.fixture
def pages(task):
  control, viewer = serve(task, None, 0, open_browser=False), serve(task, None, 0, open_browser=False, view_only=True)
  try:
    yield task, control.server_address[1], viewer.server_address[1]
  finally:
    control.shutdown()
    viewer.shutdown()


def token_of(port):
  return re.search(r'name="ahc-token" content="([0-9a-f]*)"', _http(port, "/")[2].decode()).group(1)


def mine(port, token=None):
  return {"Origin": f"http://127.0.0.1:{port}", "X_AHC_Control": "1", **({"X_AHC_Token": token} if token else {})}


def test_both_pages_serve_the_reference_image_of_a_mapped_camera_only(pages):
  _, control, viewer = pages
  for port in (control, viewer):
    status, headers, body = _http(port, "/camera/usb/reference.jpg")
    assert status == 200 and headers["content-type"] == "image/jpeg" and body == REFERENCE
    assert _http(port, "/camera/rs/reference.jpg")[0] == 404  # no map
    assert _http(port, "/camera/ghost/reference.jpg")[0] == 404
    assert _http(port, "/camera/..%2Fconfig/reference.jpg")[0] == 404
    assert _http(port, "/camera/usb/reference.jpg", Host="evil.example")[0] == 403  # only to its own name
  assert 'id="cameracard"' not in _http(control, "/")[2].decode()  # the slot-box editor moved to /calibration


def test_the_state_carries_the_maps_and_the_coverage(pages):
  ws, control, _ = pages
  state = replay(ws.runs_dir / "r.jsonl")
  usb, rs = state["config"]["cameras"]
  assert usb["sees"] == "slots 2-3, tips:ot2.right" and usb["slot_map"]["size"] == [W, H] and usb["slot_map"]["slots"]["2"] == MAP["slots"]["2"]
  assert rs == {"name": "rs", "device": "Other", "sees": None, "slot_map": None} and "physical_world" not in state["config"]
  ws.state_dir.mkdir(parents=True)
  (ws.state_dir / "physical_world.json").write_text(json.dumps({
    "checked_at": "2026-10-29T19:12:00", "points": [{"what": "slot:2", "status": "covered"}], "suggestions": ["x"],
    "vision_context": {"cameras": {"usb": {"status": "ok"}}}, "instrument_context": {"device": "ot2", "note": "OT-2: no tip or liquid sensors"},
    "physical_world_confidence": {"by_check": {"deck_matches_layout": {"status": "covered"}}, "all_confident": False}, "unrelated": 1}))
  seen = replay(ws.runs_dir / "r.jsonl")["config"]["physical_world"]
  assert seen["points"] == [{"what": "slot:2", "status": "covered"}] and seen["suggestions"] == ["x"] and "unrelated" not in seen
  assert seen["instrument_context"]["note"] == "OT-2: no tip or liquid sensors" and seen["physical_world_confidence"]["by_check"]["deck_matches_layout"]["status"] == "covered"
  assert json.loads(_http(control, "/state")[2])["config"]["physical_world"]["checked_at"] == "2026-10-29T19:12:00"
  assert "Physical world context" in _http(control, "/")[2].decode()  # the section's heading is in the page script
