"""The calibration page and what it does: the gate on load_layout, the redirect and the stop bar, still right / skip / saved
boxes, the alignment of a fresh frame, auto-verify dropping, and plate calibration (four clicked wells to a perspective
grid, its requests, its reload, and the well-level coverage it gives). Cameras are faked (cameras._run) and the robot is
the fake robot-server: nothing leaves this machine. Click order and dragging on the page are checked in a headless
browser by hand (deckview/calib_demo.py), not here."""

import hashlib
import json
import re

import anyio
import cv2
import numpy as np
import pytest

from ahc import vision
from ahc.workspace import parse_config
from conftest import calibrate_plate, calibrated, confirm_as_person, load_verified, robot_entry
from test_dashboard import _http
from test_vision_tools import BOXES, PLAN, SIDE, TOP, USB, camera, home, mapped_lab  # noqa: F401 - fixtures
from vision_images import deck, other_scene, plate_corners
from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT

pytestmark = pytest.mark.anyio

RS = {"name": "rs", "device": SIDE}


async def web(port, path, method="GET", body=None, **headers):
  """One request in a thread: the dashboard's calibration routes call back into the server's loop, which is this one."""
  return await anyio.to_thread.run_sync(lambda: _http(port, path, method, body, **headers))


async def page(lab):
  """(port, token) of the control dashboard this lab serves."""
  shown = await lab.call("open_dashboard", open_browser=False)
  port = int(shown["url"].rstrip("/").rsplit(":", 1)[1])
  status, _, body = await web(port, "/?page=run")
  assert status == 200
  return port, re.search(r'name="ahc-token" content="([0-9a-f]*)"', body.decode()).group(1), shown


def mine(port, token=None):
  return {"Origin": f"http://127.0.0.1:{port}", "X_AHC_Control": "1", **({"X_AHC_Token": token} if token else {})}


async def post(lab_page, path, body=None):
  port, token = lab_page[:2]
  status, _, data = await web(port, path, "POST", body or {}, **mine(port, token))
  return status, json.loads(data)


def inner(lab):
  return lab.server.lab


def config_of(folder):
  return parse_config((folder / ".ahc" / "config.yaml").read_text())


# -- the gate ----------------------------------------------------------------------------------------

async def test_load_layout_waits_for_the_person_to_calibrate_each_camera(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await lab.call("report_decision", decision="Plan", plan=PLAN)
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "calibration_required" in text and "usb" in text and "/calibration" in text
    assert inner(lab).gate is None and inner(lab)._robot_lock is None  # nothing loaded, the robot not taken
    overview = (await lab.call("lab_overview"))["calibration"]
    assert overview["done"] is False and overview["cameras"]["usb"]["state"] == "pending"
    assert "calibration page" in overview["cameras"]["usb"]["next"] and "Calibration comes before load_layout" in (await lab.call("lab_overview"))["next"]
    assert "open_dashboard" in overview["url"]  # no dashboard yet: it says how to get one
    shown = await lab.call("open_dashboard", open_browser=False)
    assert shown["calibration"]["url"].endswith("/calibration") and shown["calibration"]["cameras"] == ["usb"]
    assert "calibration page" in (await lab.call("lab_overview"))["calibration"]["cameras"]["usb"]["next"]


@pytest.mark.parametrize("way", ["still_right", "skip", "boxes"])
async def test_each_way_of_finishing_a_camera_opens_the_gate(tmp_path, open_lab, fake_robot, camera, way):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await lab.call("report_decision", decision="Plan", plan=PLAN)
    pg = await page(lab)
    assert (await post(pg, "/calibration/usb/frame"))[0] == 200
    if way == "boxes":
      slots = (await post(pg, "/calibration/usb/frame"))[1]["boxes"]
      status, _ = await post(pg, "/calibration/usb/boxes", {"slots": slots})
      assert status == 200
      await confirm_as_person(lab)  # saved boxes change the config: the person confirms it again
    else:
      status, body = await post(pg, f"/calibration/usb/{way.replace('_', '-')}")
      assert status == 200 and body["cameras"][0]["state"] == {"still_right": "confirmed", "skip": "skipped"}[way]
    assert (await lab.call("lab_overview"))["calibration"]["done"] is True
    await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)  # not refused


async def test_a_camera_with_no_slot_map_needs_the_agent_first_and_a_skip_clears_it(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot, slots=None) as lab:
    await lab.call("report_decision", decision="Plan", plan=PLAN)
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "calibration_required" in text and "map_camera for usb" in text
    pg = await page(lab)
    assert (await post(pg, "/calibration/usb/frame"))[0] == 409  # nothing to compare or box yet
    assert "usb" in (await lab.call("open_dashboard", open_browser=False))["calibration"]["note"]
    assert (await web(pg[0], "/"))[0] == 200  # nothing the person can do yet: no redirect
    assert (await post(pg, "/calibration/usb/skip"))[0] == 200
    await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)


async def test_a_new_slot_map_from_the_agent_makes_a_camera_pending_again(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    pg = await page(lab)
    await post(pg, "/calibration/usb/still-right")
    assert (await lab.call("lab_overview"))["calibration"]["done"] is True
    await lab.call("map_camera", camera="usb", slots={**BOXES, "1": [BOXES["1"][0] + 5, *BOXES["1"][1:]]})
    await confirm_as_person(lab)
    assert (await lab.call("lab_overview"))["calibration"]["cameras"]["usb"]["state"] == "pending"


async def test_no_cameras_and_simulation_are_not_gated(tmp_path, open_lab, fake_robot, camera):
  (tmp_path / "a").mkdir()
  (tmp_path / "b").mkdir()
  async with open_lab(tmp_path / "a", backend=None) as lab:
    await lab.call("configure_devices", devices=robot_entry("127.0.0.1", fake_robot.port, robot_name=fake_robot.name))
    await confirm_as_person(lab)
    await lab.call("report_decision", decision="Plan", plan=PLAN)
    await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)
    assert "calibration" not in await lab.call("lab_overview")
  async with open_lab(tmp_path / "b", backend=None) as lab:  # a simulation shows no real deck
    await lab.call("configure_devices", devices=[{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}], cameras=[TOP])
    await confirm_as_person(lab)
    await lab.call("load_layout", layout=STARLET_LAYOUT)  # not refused
    assert "calibration" not in await lab.call("lab_overview")


# -- the page ----------------------------------------------------------------------------------------

async def test_the_dashboard_sends_the_person_to_the_calibration_page_which_has_the_stop_bar(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    pg = await page(lab)
    port = pg[0]
    status, headers, _ = await web(port, "/")
    assert status == 302 and headers["location"] == "/calibration"
    assert (await web(port, "/index.html"))[0] == 302 and (await web(port, "/?page=run"))[0] == 200  # the run page stays reachable
    status, headers, body = await web(port, "/calibration")
    html = body.decode()
    assert status == 200 and headers["x-frame-options"] == "DENY"
    assert 'id="stopbtn"' in html and "EMERGENCY STOP" in html and 'id="stopbar"' in html and "pollStop" in html
    assert 'href="/?page=run"' in html and 'name="ahc-token"' in html
    await lab.call_text("load_layout", layout=OT2_LAYOUT)  # a refusal is logged: the run page has a run to show
    state = json.loads((await web(port, "/state"))[2])
    assert state["calibration"] == {"waiting_for_person": ["usb"], "active": True, "stale": None, "plates": {}, "serves": True}
    await post(pg, "/calibration/usb/still-right")
    assert (await web(port, "/"))[0] == 200  # nothing waits: the run page
    run_page = (await web(port, "/"))[2].decode()
    assert "Recalibrate" in run_page and 'id="cameracard"' not in run_page and "pwcSection" in run_page  # the editor moved off this page


async def test_view_only_is_never_redirected_and_has_no_calibration_page(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await lab.call("open_dashboard", open_browser=False)
    viewer = await lab.call("open_dashboard", open_browser=False, view_only=True)
    port = int(viewer["url"].rstrip("/").rsplit(":", 1)[1])
    assert "calibration" not in viewer  # the audience's page is not sent anywhere
    await lab.call_text("load_layout", layout=OT2_LAYOUT)  # a refusal is logged: the page has a run to show
    status, _, body = await web(port, "/")
    assert status == 200 and "Calibration is in progress" in body.decode()
    shown = json.loads((await web(port, "/state"))[2])["calibration"]
    assert shown["waiting_for_person"] == ["usb"] and shown["serves"] is True
    assert (await web(port, "/calibration"))[0] == 404 and (await web(port, "/calibration/state"))[0] == 404
    status, _, _ = await web(port, "/calibration/usb/skip", "POST", {}, **mine(port))
    assert status == 403
    assert not inner(lab).calibration.states


async def test_calibration_routes_answer_only_to_the_control_page_with_its_token(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    port, token, _ = await page(lab)
    for headers in ({}, mine(port), {**mine(port, token), "Origin": "http://evil.example"}, {**mine(port, "0" * 32)},
                    {**mine(port, token), "Host": "evil.example"}):
      assert (await web(port, "/calibration/usb/skip", "POST", {}, **headers))[0] == 403, headers
    assert (await web(port, "/calibration/state", Host="evil.example"))[0] == 403
    assert not inner(lab).calibration.states
    assert (await web(port, "/calibration/ghost/skip", "POST", {}, **mine(port, token)))[0] == 404
    assert (await web(port, "/calibration/state"))[0] == 200


def test_the_standalone_dashboard_has_no_calibration_page(tmp_path):
  from ahc.viz.dashboard import serve
  from ahc.workspace.store import Workspace

  ws = Workspace(tmp_path / "task")
  ws.root.mkdir()
  httpd = serve(ws, None, 0, open_browser=False)
  try:
    port = httpd.server_address[1]
    assert _http(port, "/calibration")[0] == 404 and _http(port, "/")[0] == 200  # never redirected to a page it cannot serve
  finally:
    httpd.shutdown()


# -- Still right, skip and saved boxes: what the config digest does -------------------------------------------

async def test_still_right_and_skip_keep_the_digest_and_saved_boxes_change_it(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    pg = await page(lab)
    before = config_of(tmp_path)
    assert before.status(False)[0] == "confirmed"
    await post(pg, "/calibration/usb/frame")
    await post(pg, "/calibration/usb/still-right")
    await post(pg, "/calibration/usb/skip")
    after = config_of(tmp_path)
    assert after.digest() == before.digest() and after.status(False)[0] == "confirmed"  # no dialog
    assert (await lab.call("lab_overview"))["config"]["status"] == "confirmed"
    boxes = (await post(pg, "/calibration/usb/frame"))[1]["boxes"]
    boxes["2"][0] += 12  # a box moved
    status, _ = await post(pg, "/calibration/usb/boxes", {"slots": boxes})
    saved = config_of(tmp_path)
    assert status == 200 and saved.digest() != before.digest() and saved.status(False)[0] == "pending"
    assert saved.cameras[0].slot_map["slots"]["2"][0] == pytest.approx(boxes["2"][0], abs=0.1)
    kept = (tmp_path / ".ahc" / "cameras" / "usb" / "reference.jpg").read_bytes()
    assert hashlib.sha256(kept).hexdigest() == saved.cameras[0].slot_map["sha256"]  # the fresh frame is the reference now
    assert (await lab.call("lab_overview"))["config"]["status"] == "pending"
    await confirm_as_person(lab)  # saved boxes need the person's confirm_config again
    assert (await lab.call("lab_overview"))["calibration"]["cameras"]["usb"]["state"] == "calibrated"


async def test_saved_boxes_name_the_maps_own_slots_and_stay_in_the_frame(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    pg = await page(lab)
    assert (await post(pg, "/calibration/usb/boxes", {"slots": BOXES}))[0] == 409  # no fresh picture to put them on
    boxes = (await post(pg, "/calibration/usb/frame"))[1]["boxes"]
    before = (tmp_path / ".ahc" / "config.yaml").read_text()
    for bad in ({"slots": {"2": boxes["2"]}}, {"slots": {**boxes, "9": [0, 0, 50, 50]}}, {"slots": {**boxes, "2": [50, 0, 10, 50]}},
                {"slots": {**boxes, "2": [5000, 0, 6000, 50]}}, {"slots": "x"}, {}):
      assert (await post(pg, "/calibration/usb/boxes", bad))[0] == 400, bad
    assert (tmp_path / ".ahc" / "config.yaml").read_text() == before


# -- a fresh frame against the reference: has the camera moved? -------------------------------------------------

async def test_a_fresh_frame_says_whether_the_camera_moved(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    pg = await page(lab)
    status, still = await post(pg, "/calibration/usb/frame")
    assert status == 200 and still["compare"]["result"] == "unmoved" and still["compare"]["text"] == "camera has not moved"
    assert still["size"] == [1280, 720] and still["boxes"]["2"] == pytest.approx(BOXES["2"], abs=0.1)
    image = await web(pg[0], still["image"])
    assert image[0] == 200 and image[1]["content-type"] == "image/jpeg" and cv2.imdecode(np.frombuffer(image[2], np.uint8), 1).shape[:2] == (720, 1280)
    camera.pictures[USB] = deck(seed=1, dx=40, dy=12)  # the camera moved
    status, moved = await post(pg, "/calibration/usb/frame")
    assert moved["compare"]["result"] == "moved" and re.fullmatch(r"camera moved about \d+ px", moved["compare"]["text"])
    assert 30 < moved["compare"]["shift_px"] < 60
    assert moved["boxes"]["2"][0] == pytest.approx(BOXES["2"][0] + 40, abs=4)  # the saved boxes are drawn where the deck is now
    camera.pictures[USB] = other_scene(5)  # looking at something else
    status, lost = await post(pg, "/calibration/usb/frame")
    assert status == 200 and lost["compare"]["result"] == "could_not_compare" and lost["compare"]["text"].startswith("could not compare")
    assert lost["boxes"]["2"] == pytest.approx(BOXES["2"], abs=0.1)
    assert (await post(pg, "/calibration/usb/still-right"))[0] == 200  # a failed comparison warns, it does not block
    assert (await lab.call("lab_overview"))["calibration"]["done"] is True


def test_compare_to_reference_unit(tmp_path):
  reference = tmp_path / "reference.jpg"
  from vision_images import jpeg

  reference.write_bytes(jpeg(deck()))
  size = (1280, 720)
  read = lambda image: vision.read_gray_bytes(jpeg(image))  # noqa: E731
  assert vision.compare_to_reference(reference, read(deck()), size)["result"] == "unmoved"
  moved = vision.compare_to_reference(reference, read(deck(dx=30, dy=0)), size)
  assert moved["result"] == "moved" and moved["shift_px"] == pytest.approx(30, abs=3) and moved["matrix"] is not None
  assert vision.compare_to_reference(reference, read(other_scene()), size)["result"] == "could_not_compare"
  assert vision.compare_to_reference(reference, None, size)["result"] == "could_not_compare"


def test_boxes_of_a_narrow_reference_are_warped_in_its_own_pixels(tmp_path):
  """The robot's own camera is 640 px wide: the alignment is made at 1280 and must be scaled back for its boxes."""
  from vision_images import jpeg, slot_box

  half = lambda image: cv2.resize(image, (640, 360), interpolation=cv2.INTER_AREA)  # noqa: E731
  reference = tmp_path / "reference.jpg"
  reference.write_bytes(jpeg(half(deck())))
  slots = {"2": [v / 2 for v in slot_box(2)]}
  shot = tmp_path / "shot.jpg"
  shot.write_bytes(jpeg(half(deck(dx=40, dy=0))))  # moved 20 px in the narrow picture
  seen = vision.analyze(shot, {"reference": "r", "size": [640, 360], "view": "top", "slots": slots}, reference, {"2": "Cor_96_wellplate_360ul_Fb"})
  assert seen["status"] == "ok" and seen["image_size"] == [640, 360]
  assert seen["slots"][0]["box"][0] == pytest.approx(slots["2"][0] + 20, abs=3)


# -- every calibration action drops the cameras' last check ------------------------------------------------

async def ready_for_checks(lab, camera_fake):
  await lab.call("report_decision", decision="Plan", plan=PLAN, vision_points=[])
  await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)
  return await lab.call("physical_world_context")


@pytest.mark.parametrize("action", ["still-right", "skip", "boxes"])
async def test_a_calibration_action_turns_auto_off_until_physical_world_context_runs_again(tmp_path, open_lab, fake_robot, camera, action):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await lab.call("configure_devices", devices=robot_entry("127.0.0.1", fake_robot.port, robot_name=fake_robot.name),
                   auto_verify={"checks": ["deck_matches_layout"], "min_streak": 1})
    await confirm_as_person(lab)
    pg = await page(lab)
    await post(pg, "/calibration/usb/still-right")
    first = await ready_for_checks(lab, camera)
    assert first["physical_world_confidence"]["by_check"]["deck_matches_layout"]["status"] == "covered"
    record = tmp_path / ".ahc" / "state" / "vision_record.json"
    digest = inner(lab).vision_digest()
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"maps": digest, "checks": {"deck_matches_layout": {"agree_streak": 4, "total": 4, "disagreements": 0, "unclear": 0, "last": None}}}))
    assert inner(lab).vision_result is not None and inner(lab).calibration.stale is None
    body = None
    if action == "boxes":
      body = {"slots": (await post(pg, "/calibration/usb/frame"))[1]["boxes"]}
      body["slots"]["2"][0] += 8
    assert (await post(pg, f"/calibration/usb/{action}", body))[0] == 200
    if action == "boxes":
      await confirm_as_person(lab)
    # the check is dropped and auto is off, with the reason
    assert inner(lab).vision_result is None
    why = inner(lab).auto_assess("deck_matches_layout")["why"]
    assert why is not None and "calibration changed" in why and "physical_world_context" in why
    overview = await lab.call("lab_overview")
    assert "calibration changed" in overview["calibration"]["auto_verify_note"] and overview["physical_world_context"]["summary"].startswith("calibration changed")
    state = json.loads((tmp_path / ".ahc" / "state" / "auto.json").read_text())
    assert "calibration changed" in state["confidence"]
    shot = await lab.call("capture", label="after")
    assert "calibration changed" in shot["calibration_changed"]
    # the track record stays
    assert json.loads(record.read_text())["checks"]["deck_matches_layout"]["agree_streak"] == 4
    assert inner(lab).vision_record()["deck_matches_layout"]["agree_streak"] == 4  # also under the new maps digest
    if action != "skip":  # a skipped camera covers nothing: auto stays off for another reason
      again = await lab.call("physical_world_context")
      assert again["physical_world_confidence"]["by_check"]["deck_matches_layout"]["status"] == "covered"
      assert inner(lab).calibration.stale is None and "calibration changed" not in (inner(lab).auto_assess("deck_matches_layout")["why"] or "")


async def test_a_skipped_camera_takes_photos_but_covers_nothing(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    pg = await page(lab)
    await post(pg, "/calibration/usb/skip")
    result = await ready_for_checks(lab, camera)
    assert {p["status"] for p in result["points"] if p["check"] == "deck_matches_layout"} == {"uncovered"}
    assert all(p["sources"] == [] for p in result["points"]) and result["vision_context"] == {"cameras": {}, "note": "no camera has a slot map yet (map_camera)"}
    shot = await raw_capture(lab)
    assert shot["images"][0]["camera"] == "usb" and "map" not in shot["images"][0] and "slots" not in shot["images"][0]  # a photo, no analysis
    assert (await lab.call("lab_overview"))["physical_world_context"]["sources"]["vision:usb"].startswith("skipped")


async def raw_capture(lab):
  result = await lab.client.call_tool("capture", {"label": "x"})
  assert not result.is_error, result.content[0].text
  return result.structured_content


# -- plate calibration ---------------------------------------------------------------------------------

def homography(points):
  return cv2.getPerspectiveTransform(np.float32([[0, 0], [11, 0], [0, 7], [11, 7]]), np.float32(points))


def test_the_grid_is_right_on_a_known_perspective():
  truth = homography([[200, 150], [900, 190], [170, 520], [930, 600]])  # a plate seen at an angle
  grid_points = np.float32([[c, r] for r in range(8) for c in range(12)]).reshape(-1, 1, 2)
  wells = cv2.perspectiveTransform(grid_points, truth).reshape(-1, 2)
  corners = [wells[0], wells[11], wells[84], wells[95]]  # A1, A12, H1, H12
  grid = vision.plate_grid([list(map(float, c)) for c in corners], 8, 12, 0.7)
  assert np.allclose(np.array(grid["centres"]), wells, atol=0.02) and len(grid["centres"]) == 96
  nearest = min(np.linalg.norm(wells[i] - wells[i + 1]) for i in range(0, 95) if (i + 1) % 12)
  assert grid["pitch_px"] <= nearest + 0.01 and grid["radius_px"] == pytest.approx(grid["pitch_px"] * 0.35, abs=0.01)
  # a perspective plate is not a plain rectangle: the middle well is not the middle of the corners
  assert not np.allclose(np.array(grid["centres"][47]), np.mean(corners, axis=0), atol=1.0)


@pytest.mark.parametrize("turn", [0, 90, 180, 270])
def test_a_rotated_plate_is_fixed_by_the_click_order(turn):
  """The person clicks A1 where A1 is: a plate turned on the deck gives the same wells in the same ids."""
  truth = homography([[200, 150], [900, 190], [170, 520], [930, 600]])
  angle = np.deg2rad(turn)
  rotate = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
  about = np.array([[1, 0, -550], [0, 1, -375], [0, 0, 1]])
  spin = np.linalg.inv(about) @ rotate @ about  # the whole plate turned about the picture's middle
  def at(c, r):
    x, y, w = spin @ (truth @ [c, r, 1])
    return [float(x / w), float(y / w)]  # noqa: E731
  corners = [at(0, 0), at(11, 0), at(0, 7), at(11, 7)]  # A1, A12, H1, H12, wherever they are
  grid = vision.plate_grid(corners, 8, 12, 0.7)
  for r, c in ((0, 0), (0, 11), (7, 0), (7, 11), (3, 5), (6, 9)):
    assert np.allclose(grid["centres"][r * 12 + c], at(c, r), atol=0.05), (turn, r, c)


def test_corners_in_the_wrong_order_or_collapsed_are_refused():
  good = [[100, 100], [900, 110], [95, 500], [905, 520]]
  assert vision.check_corners(good, (1280, 720)) == [[float(x), float(y)] for x, y in good]
  for bad in ([good[0], good[2], good[1], good[3]],  # A12 and H1 swapped: the shape crosses itself
              [[5, 5]] * 4, good[:3], [[1, 2, 3]] * 4, "x", [[float("nan"), 1]] * 4):
    with pytest.raises(ValueError):
      vision.check_corners(bad, (1280, 720))
  assert vision.corner_wells(8, 12) == ["A1", "A12", "H1", "H12"] and vision.corner_wells(4, 6) == ["A1", "A6", "D1", "D6"]


async def asked(lab, labware="plate", camera="usb", why="to check wells B1:B8", plan_step=1):
  return await lab.call("request_plate_calibration", labware=labware, camera=camera, why=why, plan_step=plan_step)


async def loaded_lab(lab):
  await lab.call("report_decision", decision="Plan", plan=PLAN)
  await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)


async def test_a_plate_calibration_is_requested_after_load_for_a_covered_plate_only(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot, cameras=(TOP, RS), slots={k: v for k, v in BOXES.items() if k != "3"}) as lab:
    await lab.call("report_decision", decision="Plan", plan=PLAN)
    assert await lab.refused("request_plate_calibration", labware="plate", camera="usb", why="x", plan_step=1) == "no_layout"
    await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)
    assert await lab.refused("request_plate_calibration", labware="plate", camera="ghost", why="x", plan_step=1) == "unknown_camera"
    assert await lab.refused("request_plate_calibration", labware="nothing", camera="usb", why="x", plan_step=1) == "unknown_labware"
    assert await lab.refused("request_plate_calibration", labware="plate", camera="usb", why=" ", plan_step=1) == "bad_reason"
    assert await lab.refused("request_plate_calibration", labware="plate", camera="usb", why="x", plan_step=1) == "not_covered_by_camera"  # slot 3 has no box
    assert await lab.refused("request_plate_calibration", labware="plate", camera="rs", why="x", plan_step=1) == "no_slot_map"
    assert await lab.refused("request_plate_calibration", labware="plate", camera="usb", why="x", plan_step=9) == "bad_plan_step"
    ok = await asked(lab, "reagents")
    assert ok["slot"] == "2" and ok["wells_to_click"] == ["A1", "A12", "H1", "H12"] and "waiting_for='person'" in ok["next"]
    reagents = (await lab.call("physical_world_context"))["plate_calibration"]
    assert reagents == {"reagents": {"state": "requested", "camera": "usb", "slot": "2", "waiting_for": "person", "why": "to check wells B1:B8"}}
    assert (await asked(lab, "tips"))["slot"] == "1"  # a rack has a grid of tips too


async def test_a_skipped_or_uncalibrated_camera_cannot_be_asked(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await loaded_lab(lab)
    pg = await page(lab)
    await post(pg, "/calibration/usb/skip")
    assert await lab.refused("request_plate_calibration", labware="plate", camera="usb", why="x", plan_step=1) == "camera_skipped"
    inner(lab).calibration.states.clear()
    assert await lab.refused("request_plate_calibration", labware="plate", camera="usb", why="x", plan_step=1) == "calibration_required"


async def test_the_request_is_refused_during_an_emergency_stop(tmp_path, open_lab, fake_robot, camera):
  from ahc.workspace import estop

  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await loaded_lab(lab)
    estop.engage(inner(lab).ws, by="test", reason="x")
    assert await lab.refused("request_plate_calibration", labware="plate", camera="usb", why="x", plan_step=1) == "emergency_stop"


async def test_the_person_clicks_the_corners_and_every_well_is_found(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await loaded_lab(lab)
    pg = await page(lab)
    await post(pg, "/calibration/usb/still-right")
    await asked(lab)
    frame = (await post(pg, "/calibration/usb/frame"))[1]
    state = json.loads((await web(pg[0], "/calibration/state"))[2])
    [plate] = state["plates"]
    assert plate["labware"] == "plate" and plate["state"] == "requested" and plate["wells"] == ["A1", "A12", "H1", "H12"]
    assert plate["why"] == "to check wells B1:B8" and plate["rows"] == 8 and plate["cols"] == 12 and 0 < plate["fraction"] < 1
    corners = plate_corners(frame["boxes"]["3"])
    assert (await post(pg, "/calibration/plate/plate/save", {"corners": [corners[0], corners[2], corners[1], corners[3]]}))[0] == 400  # wrong order
    assert (await post(pg, "/calibration/plate/plate/save", {"corners": corners[:3]}))[0] == 400
    assert (await post(pg, "/calibration/plate/nothing/save", {"corners": corners}))[0] == 404
    status, body = await post(pg, "/calibration/plate/plate/save", {"corners": corners})
    assert status == 200 and [p["state"] for p in body["plates"]] == ["calibrated"]
    # the grid sits on the synthetic plate's wells
    analysis = inner(lab).analyze_capture(inner(lab).config.cameras[0], await capture_path(lab))
    grid = next(e for e in analysis["slots"] if e["slot"] == "3")["grid"]
    assert grid["rows"] == 8 and grid["cols"] == 12 and len(grid["centres"]) == 96 and grid["pitch_px"] > vision.WELL_MIN_PX
    box = next(e for e in analysis["slots"] if e["slot"] == "3")["box"]
    assert box[0] < grid["centres"][0][0] < grid["centres"][11][0] < box[2] and box[1] < grid["centres"][0][1] < grid["centres"][84][1] < box[3]
    log = [json.loads(line) for line in next((tmp_path / ".ahc" / "runs").glob("*.jsonl")).read_text().splitlines()]
    kinds = [e["event"] for e in log if e.get("type") == "calibration"]
    assert kinds == ["still_right", "plate_requested", "plate_saved"]
    files = json.loads((tmp_path / ".ahc" / "state" / "plate_calibration.json").read_text())
    assert files["plates"]["plate"]["corners"] and files["plates"]["plate"]["source"] == "clicked"


async def capture_path(lab):
  from pathlib import Path

  shot = await raw_capture(lab)
  return Path(shot["images"][0]["path"])


async def test_wells_are_covered_only_after_the_plate_is_calibrated(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await lab.call("report_decision", decision="Plan", plan=PLAN, vision_points=[{"what": "plate:B1:B8", "level": "well", "check": "liquid_present"}])
    await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)
    before = {(p["what"], p["check"]): p for p in (await lab.call("physical_world_context"))["points"]}
    row = before[("plate:B1:B8", "liquid_present")]
    assert row["status"] == "uncovered" and "calibrates the plate" in row["why"] and before[("slot:3", "deck_matches_layout")]["status"] == "covered"
    await asked(lab)
    waiting = {(p["what"], p["check"]): p for p in (await lab.call("physical_world_context"))["points"]}[("plate:B1:B8", "liquid_present")]
    assert waiting["status"] == "uncovered" and "requested" in waiting["why"]
    await calibrate_plate(lab, "plate")
    result = await lab.call("physical_world_context")
    after = {(p["what"], p["check"]): p for p in result["points"]}[("plate:B1:B8", "liquid_present")]
    assert after["status"] == "covered" and after["sources"][0]["level"] == "well" and result["plate_calibration"]["plate"]["state"] == "calibrated"


async def test_a_plate_the_grid_puts_outside_the_frame_stays_uncovered(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await lab.call("report_decision", decision="Plan", plan=PLAN, vision_points=[{"what": "plate:B1:B8", "level": "well", "check": "liquid_present"}])
    await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)
    await calibrate_plate(lab, "plate")
    plate = inner(lab).calibration.plates["plate"]
    plate["corners"] = [[x, y - 700] for x, y in plate["corners"]]  # the wells now lie above the picture
    row = next(p for p in (await lab.call("physical_world_context"))["points"] if p["what"] == "plate:B1:B8")
    assert row["status"] == "uncovered" and "outside the frame" in row["why"]


async def test_a_reload_clears_the_plates_and_the_earlier_corners_are_offered(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await loaded_lab(lab)
    pg = await page(lab)
    await post(pg, "/calibration/usb/still-right")
    await calibrate_plate(lab, "plate")
    corners = inner(lab).calibration.plates["plate"]["corners"]
    await lab.call("report_decision", decision="Again", plan=PLAN, layout=OT2_LAYOUT)
    await load_verified(lab, plan_step=1)  # a reload of the deck the agent reported
    cal = inner(lab).calibration
    assert cal.plates == {}
    assert "plate_calibration" not in await lab.call("physical_world_context")  # cleared by the next successful load_layout
    await asked(lab, plan_step=1)
    frame = (await post(pg, "/calibration/usb/frame"))[1]
    [plate] = json.loads((await web(pg[0], "/calibration/state"))[2])["plates"]
    assert plate["state"] == "requested" and plate["earlier"] is True and plate["earlier_corners"] is not None
    assert plate["earlier_corners"][0] == pytest.approx([corners[0][0], corners[0][1]], abs=2.0)
    digest = config_of(tmp_path).digest()
    status, body = await post(pg, "/calibration/plate/plate/save", {"earlier": True})  # one click
    assert status == 200 and [p["state"] for p in body["plates"]] == ["calibrated"] and body["plates"][0]["source"] == "earlier"
    assert config_of(tmp_path).digest() == digest and config_of(tmp_path).status(False)[0] == "confirmed"  # not config, no dialog
    assert inner(lab).calibration.stale is not None  # but it is a calibration action
    # a failed load leaves them alone
    assert await lab.refused("load_layout", layout={"slots": {"1": {"name": "x", "type": "nope"}}}, plan_step=1) == "labware_not_allowed"
    assert "plate" in inner(lab).calibration.plates


async def test_earlier_corners_are_not_offered_for_another_picture(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await loaded_lab(lab)
    pg = await page(lab)
    await post(pg, "/calibration/usb/still-right")
    await calibrate_plate(lab, "plate")
    camera.pictures[USB] = deck(seed=1, dx=14, dy=6)  # the camera moved a little
    boxes = (await post(pg, "/calibration/usb/frame"))[1]["boxes"]
    await post(pg, "/calibration/usb/boxes", {"slots": boxes})  # the fresh picture is the reference now: the corners were in the old one
    await confirm_as_person(lab)
    assert inner(lab).calibration.calibrated_plates("usb") == {}
    assert inner(lab).calibration.plates["plate"]["corners"] is None
    await lab.call("report_decision", decision="Again", plan=PLAN, layout=OT2_LAYOUT)
    await load_verified(lab, plan_step=1)
    await asked(lab)
    [plate] = json.loads((await web(pg[0], "/calibration/state"))[2])["plates"]
    assert plate["earlier"] is False
    assert (await post(pg, "/calibration/plate/plate/save", {"earlier": True}))[0] == 409


async def test_a_plate_calibration_does_not_hold_motion(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await loaded_lab(lab)
    await asked(lab)
    assert inner(lab).gate.state == "passed"
    await lab.call("pick_up_tips", device="ot2.right", plan_step=1)
