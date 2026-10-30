"""Slot maps through the tools: map_camera, the capture analysis, physical_world_context, vision points and the shadow
record (propose_verdict). ffmpeg is faked with synthetic deck pictures (cameras._run); the robot is the fake
robot-server, so nothing leaves this machine."""

import base64
import json
import types
from contextlib import asynccontextmanager

import cv2
import numpy as np
import pytest

from ahc import cameras as cams
from ahc.examples import OT2_LAYOUT
from ahc.workspace import estop
from conftest import confirm_as_person, load_verified, robot_entry
from vision_images import H, W, deck, jpeg, other_scene, slot_box

pytestmark = pytest.mark.anyio

USB = "USB Camera"
SIDE = "Intel(R) RealSense(TM) Depth Camera 435 with RGB Module RGB"
TOP = {"name": "usb", "device": USB}
RS = {"name": "rs", "device": SIDE}
FULL_W = 1920  # the camera's own resolution; the agent's preview and the maps are 1280 wide
OT2_SLOTS = {"1": slot_box(1), "2": slot_box(2), "3": slot_box(3), "4": slot_box(4)}
BOXES = {k: [round(v, 1) for v in box] for k, box in OT2_SLOTS.items()}


class FakeCameras:
  """Stands in for cameras._run: each camera shows `pictures[device]` (a deck picture), full size on a capture
  and 1280 wide on a downscale."""

  def __init__(self):
    self.pictures = {USB: deck(seed=1), SIDE: deck(seed=1)}
    self.calls: list[list[str]] = []

  def __call__(self, args, timeout):
    self.calls.append(args)
    if "-list_devices" in args:
      lines = ["[AVFoundation indev @ 0x1] AVFoundation video devices:"]
      lines += [f"[AVFoundation indev @ 0x1] [{i}] {d}" for i, d in enumerate((SIDE, USB, "Capture screen 0"))]
      return 1, "\n".join(lines + ["[AVFoundation indev @ 0x1] AVFoundation audio devices:"])
    out = cams.Path(args[-1])
    if "-vf" in args:
      source = cv2.imread(args[args.index("-i") + 1])
      out.write_bytes(jpeg(source, min(source.shape[1], 1280)))
      return 0, ""
    device = args[args.index("-i") + 1].removesuffix(":none")
    out.write_bytes(jpeg(self.pictures[device], FULL_W))
    return 0, ""

  def captures(self):
    return [a for a in self.calls if "-list_devices" not in a and "-vf" not in a]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
  monkeypatch.setenv("AHC_HOME", str(tmp_path / "ahc-home"))
  monkeypatch.delenv("AHC_BACKEND", raising=False)


@pytest.fixture
def camera(monkeypatch):
  fake = FakeCameras()
  monkeypatch.setattr(cams, "_run", fake)
  monkeypatch.setattr(cams, "ffmpeg", lambda: "/fake/ffmpeg")
  monkeypatch.setattr(cams, "sys", types.SimpleNamespace(platform="darwin"))
  return fake


@asynccontextmanager
async def mapped_lab(open_lab, folder, bot, cameras=(TOP,), slots=BOXES, camera_name="usb"):
  """A confirmed real-robot config with cameras; with `slots`, the first camera mapped and the config confirmed again."""
  async with open_lab(folder, backend=None) as lab:
    await lab.call("configure_devices", devices=robot_entry("127.0.0.1", bot.port, robot_name=bot.name), cameras=list(cameras))
    await confirm_as_person(lab)
    if slots:
      await lab.call("map_camera", camera=camera_name)
      await lab.call("map_camera", camera=camera_name, slots=slots)
      await confirm_as_person(lab)
    yield lab


def config_text(folder):
  return (folder / ".ahc" / "config.yaml").read_text()


async def raw(lab, tool, **args):
  result = await lab.client.call_tool(tool, args)
  assert not result.is_error, result.content[0].text
  return result


def images_of(result):
  return [base64.b64decode(c.data) for c in result.content if c.type == "image"]


# -- map_camera -------------------------------------------------------------------------------------

async def test_map_camera_takes_a_reference_then_writes_a_pending_map(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot, slots=None) as lab:
    first = await raw(lab, "map_camera", camera="usb")
    shown = images_of(first)
    assert len(shown) == 1 and cv2.imdecode(np.frombuffer(shown[0], np.uint8), 1).shape[:2] == (H, W)  # the 1280-wide preview
    info = first.structured_content
    assert info["image_size"] == [W, H] and "slots 1-3 are the front row" in info["deck_guide"] and "slots=" in info["next"]
    candidate = tmp_path / ".ahc" / "cameras" / "usb" / "candidate.jpg"
    assert candidate.is_file() and not (candidate.parent / "reference.jpg").exists()
    assert "slot_map" not in config_text(tmp_path)  # nothing is written before boxes are proposed
    second = await raw(lab, "map_camera", camera="usb", slots=BOXES)
    result = second.structured_content
    assert result["sees"] == "slots 1-4" and result["slot_map"]["size"] == [W, H] and result["slot_map"]["view"] == "top"
    assert len(images_of(second)) == 1  # the reference with the boxes drawn, to check
    assert result["config"]["status"] == "pending" and "changed after the last confirmation" in result["config"]["reason"]
    assert "(sees slots 1-4)" in result["config"]["confirm_summary"] and "confirm_config" in result["next"]
    folder = tmp_path / ".ahc" / "cameras" / "usb"
    assert (folder / "reference.jpg").is_file() and not candidate.exists()
    assert "slot_map" in config_text(tmp_path) and "sha256" in config_text(tmp_path)
    assert await lab.refused("capture", label="x") == "config_pending"  # the person confirms first
    await confirm_as_person(lab)
    status = (await lab.call("lab_overview"))["config"]
    assert status["status"] == "confirmed" and status["cameras"] == [{"name": "usb", "device": USB, "sees": "slots 1-4"}]
    context = (await lab.call("lab_overview"))["physical_world_context"]
    assert context["sources"] == {"vision:usb": "slots 1-4", "instrument:ot2": "OT-2: no tip or liquid sensors"}


async def test_map_camera_refusals(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot, slots=None) as lab:
    assert await lab.refused("map_camera", camera="ghost") == "unknown_camera"
    assert await lab.refused("map_camera", camera="usb", slots=BOXES) == "no_reference_image"  # nothing to put boxes on
    await lab.call("map_camera", camera="usb")
    assert await lab.refused("map_camera", camera="usb", slots={}) == "bad_slot_map"
    assert await lab.refused("map_camera", camera="usb", slots={"13": [0, 0, 50, 50]}) == "bad_slot_map"
    assert await lab.refused("map_camera", camera="usb", slots={"2": [50, 0, 10, 50]}) == "bad_slot_map"
    assert await lab.refused("map_camera", camera="usb", slots={"2": [W + 10, 0, W + 60, 50]}) == "box_outside_frame"
    assert "slot_map" not in config_text(tmp_path)
    estop.engage(lab.server.lab.ws, by="test")
    assert await lab.refused("map_camera", camera="usb", slots=BOXES) == "emergency_stop"
    assert (await raw(lab, "map_camera", camera="usb")).structured_content["image_size"]  # a photo is evidence only


async def test_map_camera_is_for_real_hardware_and_a_declared_camera(tmp_path, open_lab, camera):
  async with open_lab(tmp_path, "sim") as lab:
    await lab.call("configure_devices", devices=[{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}], cameras=[TOP])
    assert await lab.refused("map_camera", camera="usb") == "camera_not_in_simulation"
    assert await lab.refused("physical_world_context") == "camera_not_in_simulation"


async def test_a_second_map_keeps_the_agents_view_and_a_side_view_is_marked(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot, cameras=(TOP, RS), slots=None) as lab:
    await lab.call("map_camera", camera="rs")
    done = await lab.call("map_camera", camera="rs", slots={"2": slot_box(2), "3": slot_box(3)}, view="side")
    assert done["sees"] == "slots 2-3 (side view)" and done["slot_map"]["view"] == "side"
    await confirm_as_person(lab)
    await lab.call("map_camera", camera="rs")
    again = await lab.call("map_camera", camera="rs", slots={"2": slot_box(2)})  # the view stays
    assert again["slot_map"]["view"] == "side"
    assert (await lab.call("lab_overview"))["config"]["cameras"][0] == {"name": "usb", "device": USB}  # the other has none


async def test_configure_devices_keeps_a_slot_map_for_the_same_camera_and_refuses_to_write_one(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    devices = robot_entry("127.0.0.1", fake_robot.port, robot_name=fake_robot.name)
    kept = await lab.call("configure_devices", devices=devices, cameras=[TOP, RS])
    assert kept["config"]["cameras"][0]["sees"] == "slots 1-4" and "sees" not in kept["config"]["cameras"][1]
    assert kept["config"]["status"] == "pending"  # a camera was added
    assert await lab.refused("configure_devices", devices=devices,
                             cameras=[{**TOP, "slot_map": {"reference": "x", "size": [1, 1], "slots": {}}}]) == "config_invalid"
    moved = await lab.call("configure_devices", devices=devices, cameras=[{"name": "usb", "device": SIDE}])
    assert "sees" not in moved["config"]["cameras"][0] and "slot map of usb is gone" in moved["note"]


# -- capture with a slot map ------------------------------------------------------------------------

async def test_capture_judges_each_slot_and_keeps_the_crops(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    camera.pictures[USB] = deck(seed=1, dx=9, dy=-6)  # the camera was bumped a little
    result = (await raw(lab, "capture", label="the deck", cameras=["usb"])).structured_content
    [image] = result["images"]
    assert image["map"] == "ok" and image["image_size"] == [W, H] and image["alignment"]["ok"] and image["frame_sharpness"] > 12
    assert image["alignment"]["inliers"] > 100 and 5 < image["alignment"]["shift_px"] < 25
    slots = {e["slot"]: e for e in image["slots"]}
    assert sorted(slots) == ["1", "2", "3", "4"] and all(e["level"] == "well" for e in slots.values())
    assert abs(slots["2"]["box"][0] - (slot_box(2)[0] + 9)) < 6 and abs(slots["2"]["box"][1] - (slot_box(2)[1] - 6)) < 6
    assert slots["2"]["sharpness"] > 6 and slots["2"]["px_per_well"] > 20 and slots["2"]["visible"] == 1.0
    folder = cams.Path(result["folder"])
    stem = cams.Path(image["path"]).stem
    crop = cams.Path(slots["2"]["crop"])
    assert crop == folder / f"{stem}_slot2.jpg" and crop.is_file()
    assert cv2.imread(str(crop)).shape[1] > 400  # a crop of the full-size file (1920 wide), not of the preview
    line = json.loads((folder / "captures.jsonl").read_text().splitlines()[0])
    assert line["vision"] == {"status": "ok", "levels": {"1": "well", "2": "well", "3": "well", "4": "well"}}
    assert len(images_of(await raw(lab, "capture", label="again", cameras=["usb"]))) == 1  # the inline image as before


async def test_capture_says_when_the_view_is_blurred_or_the_camera_moved(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    camera.pictures[USB] = deck(seed=1, blur=61)  # the pipette head in front of the lens
    [blur] = (await lab.call("capture", label="parked over the camera", cameras=["usb"]))["images"]
    assert blur["map"] == "unclear" and "out of focus" in blur["why"] and blur["slots"] == []
    camera.pictures[USB] = other_scene()
    [lost] = (await lab.call("capture", label="turned away", cameras=["usb"]))["images"]
    assert lost["map"] == "needs_new_map" and "map_camera" in lost["why"] and not lost["alignment"]["ok"]


async def test_a_replaced_reference_image_needs_a_new_map(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    (tmp_path / ".ahc" / "cameras" / "usb" / "reference.jpg").write_bytes(jpeg(deck(seed=3), W))
    [image] = (await lab.call("capture", label="x", cameras=["usb"]))["images"]
    assert image["map"] == "needs_new_map" and "not the one this map was made on" in image["why"]


async def test_a_camera_without_a_map_captures_as_before(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot, slots=None) as lab:
    [image] = (await lab.call("capture", label="x", cameras=["usb"]))["images"]
    assert set(image) == {"camera", "device", "path"}


# -- points and coverage ----------------------------------------------------------------------------

PLAN = ["Load the layout", "Check the liquid", "Report"]


def covering_sources(result, check):
  """The sources that cover a check type, from the conclusion."""
  return result["physical_world_confidence"]["by_check"][check]["sources"]


async def ready(lab):
  await lab.call("report_decision", decision="Plan", plan=PLAN, vision_points=[
    {"what": "plate:B1:B8", "level": "well", "check": "liquid_present"}])
  await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)


async def test_coverage_needs_a_plan_and_a_layout_then_lists_the_points(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    taken = len(camera.captures())  # the reference's
    empty = await lab.call("physical_world_context")
    assert empty["points"] == [] and "load_layout" in empty["note"] and len(camera.captures()) == taken  # nothing to check yet
    assert empty["instrument_context"]["note"] == "OT-2: no tip or liquid sensors" and not empty["physical_world_confidence"]["all_confident"]
    await ready(lab)
    first = await lab.call("physical_world_context")
    assert set(first) == {"vision_context", "instrument_context", "points", "physical_world_confidence", "suggestions", "checked_at"}
    rows = {(p["what"], p["check"]): p for p in first["points"]}
    assert len(camera.captures()) == taken + 1 and first["vision_context"]["cameras"]["usb"]["status"] == "ok"
    assert first["vision_context"]["cameras"]["usb"]["fresh"] is True
    # OT2_LAYOUT: slots 1 and 4 tip racks, 2 reagents, 3 plate; the map sees slots 1-4
    for slot in ("slot:1", "slot:2", "slot:3", "slot:4"):
      row = rows[(slot, "deck_matches_layout")]
      assert row["status"] == "covered" and [(e["source"], e["fresh"], e["covers"]) for e in row["sources"]] == [("vision:usb", True, True)]
    well = rows[("plate:B1:B8", "liquid_present")]
    assert well["status"] == "covered" and well["declared_by"] == "agent"
    tips = [p for p in first["points"] if p["check"] == "tips_mounted"]
    assert tips and {p["status"] for p in tips} == {"uncovered"} and all(p["what"].startswith("tips:ot2.") for p in tips)
    assert all(p["sources"] == [] for p in tips)  # no camera region sees the tips and the OT-2 has no tip sensor
    confidence = first["physical_world_confidence"]
    assert {k: v["status"] for k, v in confidence["by_check"].items()} == {
      "deck_matches_layout": "covered", "liquid_present": "covered", "tips_mounted": "uncovered"}
    assert confidence["by_check"]["deck_matches_layout"]["sources"] == [{"source": "vision:usb", "level": "well"}]
    assert confidence["by_source"] == {"vision:usb": {"observes": 5, "covers": 5}} and confidence["all_confident"] is False
    assert any("tips:" in s for s in first["suggestions"])
    saved = json.loads((tmp_path / ".ahc" / "state" / "physical_world.json").read_text())
    assert saved["physical_world_confidence"] == confidence and saved["points"][0]["what"] == "slot:1"
    context = (await lab.call("lab_overview"))["physical_world_context"]
    assert "covered" in context["summary"] and "tips_mounted tips:" in context["summary"]
    assert context["confidence"] == {"deck_matches_layout": "covered", "liquid_present": "covered", "tips_mounted": "uncovered"}
    assert context["sources"]["vision:usb"] == "slots 1-4; covers 5 of 5 points it observes"
    assert context["sources"]["instrument:ot2"] == "OT-2: no tip or liquid sensors"


async def test_the_digital_twin_is_not_a_source_of_confidence(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot, slots=None) as lab:  # no camera map: only the twin knows the deck
    await ready(lab)
    await lab.call("pick_up_tips", device="ot2.right")  # the twin now tracks 8 mounted tips and the reagent volumes
    assert [s.kind for s in lab.server.lab.context_sources()] == ["vision", "instrument"]
    result = await lab.call("physical_world_context")
    assert {p["status"] for p in result["points"]} == {"uncovered"} and all(p["sources"] == [] for p in result["points"])
    assert result["physical_world_confidence"]["by_source"] == {} and result["physical_world_confidence"]["all_confident"] is False
    assert "digital" not in json.dumps({k: v for k, v in result.items() if k != "suggestions"})


async def test_a_capture_newer_than_the_last_motion_is_reused_and_a_motion_makes_it_stale(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    await lab.call("physical_world_context")
    n = len(camera.captures())
    await lab.call("physical_world_context")
    assert len(camera.captures()) == n  # the picture still shows the deck as it is
    await lab.call("capture", label="by hand", cameras=["usb"])  # a newer capture counts too
    await lab.call("physical_world_context")
    assert len(camera.captures()) == n + 1
    await lab.call("physical_world_context", recapture=True)
    assert len(camera.captures()) == n + 2
    await lab.call("pick_up_tips", device="ot2.right")  # the head moved: the old picture is stale
    again = await lab.call("physical_world_context")
    assert len(camera.captures()) == n + 3 and again["vision_context"]["cameras"]["usb"]["status"] == "ok"


async def test_blur_makes_points_unclear_and_a_moved_camera_needs_a_new_map(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    camera.pictures[USB] = deck(blur=61)
    blurred = await lab.call("physical_world_context")
    assert {p["status"] for p in blurred["points"] if p["check"] != "tips_mounted"} == {"unclear"}
    assert blurred["vision_context"]["cameras"]["usb"]["status"] == "unclear" and any("capture again" in s for s in blurred["suggestions"])
    camera.pictures[USB] = other_scene()
    lost = await lab.call("physical_world_context", recapture=True)
    deck_rows = [p for p in lost["points"] if p["check"] == "deck_matches_layout"]
    assert {p["status"] for p in deck_rows} == {"unclear"} and {p["sources"][0]["state"] for p in deck_rows} == {"needs_new_map"}
    assert lost["physical_world_confidence"]["by_check"]["deck_matches_layout"]["status"] == "unclear"
    assert any("map_camera" in s for s in lost["suggestions"])


async def test_a_second_camera_covers_what_the_first_does_not(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot, cameras=(TOP, RS)) as lab:
    await lab.call("map_camera", camera="rs")
    await lab.call("map_camera", camera="rs", slots={"3": slot_box(3)}, view="side")
    await confirm_as_person(lab)
    await ready(lab)
    camera.pictures[USB] = deck(blur=61)
    result = await lab.call("physical_world_context")
    slot3 = next(p for p in result["points"] if p["what"] == "slot:3")
    assert slot3["status"] == "covered" and [(e["source"], e["covers"]) for e in slot3["sources"]] == [("vision:usb", False), ("vision:rs", True)]
    assert {s["source"]: s["level"] for s in covering_sources(result, "deck_matches_layout")} == {"vision:rs": "slot"}
    well = next(p for p in result["points"] if p["check"] == "liquid_present")
    assert well["status"] == "unclear"  # the side view never resolves wells, and the top view is blurred


async def test_vision_points_are_checked_and_a_new_plan_starts_the_agents_list_again(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    assert await lab.refused("report_decision", decision="x", plan=PLAN, vision_points=[{"what": "plate", "level": "pixel", "check": "liquid_present"}]) == "bad_vision_point"
    await ready(lab)
    assert await lab.refused("report_decision", decision="x", vision_points=[{"what": "ghost:A1", "level": "well", "check": "liquid_present"}]) == "bad_vision_point"
    await lab.call("report_decision", decision="Also the diluent", vision_points=[{"what": "diluent", "level": "well", "check": "liquid_present"}])
    whats = lambda r: [p["what"] for p in r["points"] if p["declared_by"] == "agent"]
    assert whats(await lab.call("physical_world_context")) == ["plate:B1:B8", "diluent"]
    await lab.call("report_decision", decision="New plan", plan=["Only this"])
    assert whats(await lab.call("physical_world_context")) == []
    entry = [e for e in lab.server.lab.runlog.entries if e.get("vision_points")][0]
    assert entry["vision_points"] == [{"what": "plate:B1:B8", "level": "well", "check": "liquid_present"}]


async def test_without_a_slot_map_the_cameras_cover_nothing(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot, slots=None) as lab:
    await ready(lab)
    result = await lab.call("physical_world_context")  # still the umbrella: the instrument is asked, no camera has a map
    assert result["vision_context"] == {"cameras": {}, "note": "no camera has a slot map yet (map_camera)"}
    assert {p["status"] for p in result["points"]} == {"uncovered"} and not result["physical_world_confidence"]["all_confident"]
    assert all(p["sources"] == [] and p["why"] == "no source observes it" for p in result["points"])
    context = (await lab.call("lab_overview"))["physical_world_context"]
    assert "summary" not in context and context["sources"]["vision:usb"].startswith("no slot map")


async def test_the_instrument_context_of_a_real_ot2_says_it_has_no_sensors(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    result = await lab.call("physical_world_context")
    assert result["instrument_context"] == {"device": "ot2", "observes": {}, "note": "OT-2: no tip or liquid sensors"}
    assert not any(e["source"].startswith("instrument:") for p in result["points"] for e in p["sources"])


# -- the shadow record ------------------------------------------------------------------------------

async def liquid_check(lab, plan_step=1):
  return await lab.call("verify", check="liquid_present", targets=["reagents:A1"], min_volume_ul=10, plan_step=plan_step)


async def crop_of(lab):
  result = (await lab.call("capture", label="the liquid", cameras=["usb"]))["images"][0]
  return result["slots"][1]["crop"]


async def test_the_agents_judgments_are_compared_with_the_persons_verdicts(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    record = tmp_path / ".ahc" / "state" / "vision_record.json"
    check = await liquid_check(lab)
    assert check["verdict"] == "pending"
    crop = await crop_of(lab)
    proposed = await lab.call("propose_verdict", check_id=check["check_id"], verdict="pass", images=[crop],
                              per_point=[{"what": "reagents:A1", "verdict": "pass", "note": "red, filled"}], note="looks right")
    assert proposed["shadow"] is True and "passed nothing" in proposed["next"]
    assert lab.server.lab.verification.pending[check["check_id"]]["verdict"] == "pending"  # nothing changed
    done = await lab.call("record_verdict", check_id=check["check_id"], verdict="pass", note="the person looked")
    assert done["vision"] == {"agent": "pass", "agreed": True, "agree_streak": 1}
    assert json.loads(record.read_text())["checks"]["liquid_present"]["agree_streak"] == 1
    # a second check: the agent says pass, the person finds it wrong: the streak starts again
    check = await liquid_check(lab)
    await lab.call("propose_verdict", check_id=check["check_id"], verdict="pass", images=[crop])
    done = await lab.call("record_verdict", check_id=check["check_id"], verdict="fail", note="empty")
    assert done["vision"] == {"agent": "pass", "agreed": False, "agree_streak": 0}
    entry = json.loads(record.read_text())["checks"]["liquid_present"]
    assert entry["total"] == 2 and entry["disagreements"] == 1 and entry["last"]["person"] == "fail"
    # a verdict with no proposal records nothing
    check = await liquid_check(lab)
    done = await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    assert "vision" not in done and json.loads(record.read_text())["checks"]["liquid_present"]["total"] == 2
    events = [e for e in lab.server.lab.runlog.entries if e.get("type") == "vision"]
    assert [(e["event"], e.get("agent") or e.get("verdict")) for e in events] == [
      ("proposal", "pass"), ("agreement", "pass"), ("proposal", "pass"), ("agreement", "pass")]
    assert (await lab.call("lab_overview"))["physical_world_context"]["record"]["agree_streak"] == {"liquid_present": 0}


async def test_propose_verdict_refusals(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    check = await liquid_check(lab)
    crop = await crop_of(lab)
    assert await lab.refused("propose_verdict", check_id="nope", verdict="pass", images=[crop]) == "unknown_check_id"
    assert await lab.refused("propose_verdict", check_id=check["check_id"], verdict="pass", images=[]) == "images_required"
    assert await lab.refused("propose_verdict", check_id=check["check_id"], verdict="pass", images=[str(tmp_path / "x.jpg")]) == "image_outside_run"
    assert await lab.refused("propose_verdict", check_id=check["check_id"], verdict="pass", images=[crop],
                             per_point=[{"what": "x", "verdict": "maybe"}]) == "bad_per_point"
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    recovery = await lab.call("verify", check="tips_mounted", expect="none")  # the check of every pipette
    assert await lab.refused("propose_verdict", check_id=recovery["check_id"], verdict="pass", images=[crop]) == "recovery_check"
    estop.engage(lab.server.lab.ws, by="test")
    assert await lab.refused("propose_verdict", check_id=recovery["check_id"], verdict="pass", images=[crop]) == "emergency_stop"


async def test_an_unclear_judgment_breaks_the_streak_and_a_map_change_resets_it(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    crop = await crop_of(lab)
    for verdict, person, streak in (("pass", "pass", 1), ("pass", "pass", 2), ("unclear", "pass", 0), ("pass", "pass", 1)):
      check = await liquid_check(lab)
      await lab.call("propose_verdict", check_id=check["check_id"], verdict=verdict, images=[crop])
      assert (await lab.call("record_verdict", check_id=check["check_id"], verdict=person))["vision"]["agree_streak"] == streak
    assert lab.server.lab.vision_record()["liquid_present"]["unclear"] == 1
    await lab.call("map_camera", camera="usb")
    await lab.call("map_camera", camera="usb", slots={**BOXES, "1": [slot_box(1)[0] + 2, *slot_box(1)[1:]]})  # one box moved
    assert lab.server.lab.vision_record() == {}  # every streak starts again


async def test_propose_verdict_is_for_real_hardware(tmp_path, open_lab, camera):
  async with open_lab(tmp_path, "sim") as lab:
    await lab.call("configure_devices", devices=[{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}])
    assert await lab.refused("propose_verdict", check_id="x", verdict="pass", images=["a.jpg"]) == "proposal_not_needed"


async def test_the_tool_is_called_physical_world_context(open_lab, tmp_path):
  async with open_lab(tmp_path, "sim") as lab:
    names = {t.name for t in (await lab.client.list_tools()).tools}
    assert {"physical_world_context", "map_camera", "propose_verdict"} <= names and "vision_coverage" not in names
