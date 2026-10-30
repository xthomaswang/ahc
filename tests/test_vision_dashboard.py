"""The dashboard's camera card: the reference image and boxes (both pages), the dragged boxes saved by the control
page only, and the coverage it lists."""

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
  assert 'id="cameracard"' in _http(control, "/")[2].decode()


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


def test_dragged_boxes_are_saved_by_the_control_page_and_the_config_goes_pending(pages):
  ws, control, _ = pages
  token = token_of(control)
  moved = {**MAP["slots"], "2": [MAP["slots"]["2"][0] + 20, MAP["slots"]["2"][1], MAP["slots"]["2"][2] + 20, MAP["slots"]["2"][3]]}
  assert parse_config(ws.config_path.read_text()).status(False)[0] == "confirmed"
  status, _, body = _http(control, "/cameras/usb/slots", "POST", {"slots": moved}, **mine(control, token))
  assert status == 200 and json.loads(body)["saved"] is True and json.loads(body)["status"] == "pending"
  saved = parse_config(ws.config_path.read_text())
  assert saved.cameras[0].slot_map["slots"]["2"] == moved["2"] and saved.cameras[0].slot_map["reference"] == MAP["reference"]
  status, why = saved.status(False)
  assert status == "pending" and "slot maps" in why and saved.confirmation["by"] == "human"  # the old confirmation no longer fits
  assert saved.cameras[1] == CameraEntry("rs", "Other") and saved.devices[0].id == "starlet"
  again = json.loads(_http(control, "/cameras/usb/slots", "POST", {"slots": moved}, **mine(control, token))[2])
  assert again["saved"] is False  # nothing changed
  assert (ws.cameras_dir / "usb" / "reference.jpg").read_bytes() == REFERENCE


def test_a_save_must_come_from_the_control_page_with_its_token_and_name_the_maps_own_slots(pages):
  ws, control, viewer = pages
  token = token_of(control)
  before = ws.config_path.read_text()
  body = {"slots": MAP["slots"]}
  assert _http(viewer, "/cameras/usb/slots", "POST", body, **mine(viewer, token))[0] == 403  # the audience's page saves nothing
  assert _http(viewer, "/cameras/usb/slots", "POST", body, **mine(viewer))[0] == 403
  assert _http(control, "/cameras/usb/slots", "POST", body)[0] == 403  # no header, no origin
  assert _http(control, "/cameras/usb/slots", "POST", body, **mine(control))[0] == 403  # no token
  assert _http(control, "/cameras/usb/slots", "POST", body, **{**mine(control, token), "Origin": "http://evil.example"})[0] == 403
  assert _http(control, "/cameras/usb/slots", "POST", body, **{**mine(control, "0" * 32)})[0] == 403
  assert _http(control, "/cameras/usb/slots", "POST", body, Host="evil.example", **mine(control, token))[0] == 403
  for bad, code in (({"slots": {"2": MAP["slots"]["2"]}}, 400), ({"slots": {**MAP["slots"], "5": [0, 0, 50, 50]}}, 400),
                    ({"slots": {**MAP["slots"], "2": [50, 0, 10, 50]}}, 400), ({"slots": {**MAP["slots"], "2": [0, 0, 1e9, 50]}}, 400),
                    ({"slots": "x"}, 400), ({}, 400)):
    assert _http(control, "/cameras/usb/slots", "POST", bad, **mine(control, token))[0] == code, bad
  assert _http(control, "/cameras/rs/slots", "POST", body, **mine(control, token))[0] == 404  # no map to move boxes in
  assert _http(control, "/cameras/ghost/slots", "POST", body, **mine(control, token))[0] == 404
  assert ws.config_path.read_text() == before
