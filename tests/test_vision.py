"""Slot maps and the quality gate on synthetic deck pictures: alignment under a moved camera, blur and a changed scene,
the per-slot levels, the points and their coverage, the track record. No camera and no robot."""

import json

import cv2
import pytest

from ahc import physical_world, vision, vision_points
from ahc.vision_context import VisionContext
from ahc.core.errors import LabError
from ahc.vision import analyze, labware_geometry
from ahc.workspace import CameraEntry, Config, DeviceEntry, parse_cameras, parse_config, parse_slot_map, slots_text
from ahc.workspace.store import Workspace
from vision_images import H, W, deck, jpeg, other_scene, slot_box

PLATE = "Cor_96_wellplate_360ul_Fb"
TROUGH = "nest_1_troughplate_195000uL_Vb"
RACK = "opentrons_96_filtertiprack_200ul"
SLOTS = {str(n): slot_box(n) for n in range(1, 10)}


@pytest.fixture
def reference(tmp_path):
  path = tmp_path / "reference.jpg"
  path.write_bytes(jpeg(deck()))
  return path


def smap(slots=None, view="top"):
  return {"reference": "cameras/top/reference.jpg", "size": [W, H], "view": view, "slots": slots or SLOTS}


def capture(tmp_path, image, name="top_120000.jpg"):
  path = tmp_path / name
  path.write_bytes(jpeg(image))
  return path


def by_slot(result):
  return {e["slot"]: e for e in result["slots"]}


# -- the config key ---------------------------------------------------------------------------------

def test_slot_map_is_parsed_normalized_and_in_the_digest():
  raw = {"reference": "cameras/top/reference.jpg", "size": [1280, 720], "slots": {"3": [10, 10.04, 200, 160], "tips:ot2.right": [5, 5, 90, 90], "2": [0, 0, 50, 50]}}
  entry = parse_cameras([{"name": "top", "device": "USB Camera", "slot_map": raw}])[0]
  assert list(entry.slot_map["slots"]) == ["2", "3", "tips:ot2.right"] and entry.slot_map["slots"]["3"] == [10.0, 10.0, 200.0, 160.0]
  assert entry.slot_map["view"] == "top" and entry.sees() == "slots 2-3, tips:ot2.right"
  devices = [DeviceEntry("starlet", "hamilton.starlet", "sim")]
  plain = Config(devices=devices, limits={}, cameras=[CameraEntry("top", "USB Camera")])
  mapped = Config(devices=devices, limits={}, cameras=[entry])
  moved = Config(devices=devices, limits={}, cameras=[CameraEntry("top", "USB Camera", {**entry.slot_map, "slots": {"2": [1, 0, 50, 50]}})])
  assert len({plain.digest(), mapped.digest(), moved.digest()}) == 3
  assert parse_config(__import__("yaml").safe_dump(mapped.data())).cameras == [entry]  # it round-trips through the file
  mapped.confirm("human")
  assert mapped.status(False)[0] == "confirmed"
  mapped.cameras = moved.cameras  # a box dragged: the config goes back to pending
  status, why = mapped.status(False)
  assert status == "pending" and "slot maps" in why


@pytest.mark.parametrize("change", [
  {"reference": "cameras/other/reference.jpg"}, {"reference": "/etc/passwd"}, {"size": [0, 5]}, {"size": "big"},
  {"slots": {}}, {"slots": {"13": [0, 0, 50, 50]}}, {"slots": {"tips:Ot2": [0, 0, 50, 50]}}, {"slots": {"2": [0, 0, 5, 50]}},
  {"slots": {"2": [50, 0, 10, 50]}}, {"slots": {"2": [0, 0, 50]}}, {"slots": {"2": [0, 0, 50, "x"]}}, {"slots": {"2": [0, 0, 1e9, 50]}},
  {"view": "left"}, {"sha256": "abc"}, {"extra": 1}])
def test_bad_slot_maps_are_refused(change):
  with pytest.raises(LabError) as caught:
    parse_slot_map({"reference": "cameras/top/reference.jpg", "size": [1280, 720], "slots": {"2": [0, 0, 50, 50]}, **change}, "top")
  assert caught.value.code == "config_invalid" and "map_camera" in caught.value.hint or "box" in (caught.value.hint or "").lower() or caught.value.hint


def test_slots_text():
  assert slots_text(["1", "2", "3", "4", "5", "6", "7", "8", "9"]) == "slots 1-9"
  assert slots_text(["2", "5", "6", "8", "tips:ot2.right"], "side") == "slots 2, 5-6, 8, tips:ot2.right (side view)"


# -- alignment --------------------------------------------------------------------------------------

def test_a_capture_of_the_same_deck_aligns_with_every_slot_at_its_level(tmp_path, reference):
  result = analyze(capture(tmp_path, deck(seed=1)), smap(), reference, {"2": PLATE, "3": PLATE})
  assert result["status"] == "ok" and result["alignment"]["ok"] and result["alignment"]["inliers"] > 200
  slots = by_slot(result)
  assert result["image_size"] == [W, H] and result["frame_sharpness"] > vision.FRAME_SHARP_MIN
  for n in "123456":
    assert slots[n]["level"] == "well", slots[n]
    assert slots[n]["visible"] == 1.0 and slots[n]["sharpness"] > vision.SLOT_SHARP_MIN
    assert all(abs(a - b) < 1.5 for a, b in zip(slots[n]["box"], SLOTS[n]))  # nothing moved
  assert slots["2"]["px_per_well"] == pytest.approx(9 * (317 / 127.76 + 215 / 85.48) / 2, abs=0.6)
  assert slots["8"]["visible"] == pytest.approx(0.64, abs=0.02)  # the plate cut at the top edge
  assert slots["8"]["level"] == "well"
  crops = [tmp_path / f"top_120000_slot{n}.jpg" for n in "123456789"]
  assert all(c.is_file() for c in crops) and slots["2"]["crop"] == str(crops[1])
  small = cv2.imread(str(crops[1]))
  assert abs(small.shape[1] - 317) < 3 and abs(small.shape[0] - 215) < 3


@pytest.mark.parametrize("dx,dy,angle", [(14, -9, 0.0), (0, 0, 2.5), (-20, 12, -1.5)])
def test_a_moved_camera_still_aligns_and_the_boxes_follow(tmp_path, reference, dx, dy, angle):
  result = analyze(capture(tmp_path, deck(seed=1, dx=dx, dy=dy, angle=angle)), smap(), reference)
  assert result["status"] == "ok"
  box = by_slot(result)["5"]["box"]
  expected = [(slot_box(5)[0] + dx), (slot_box(5)[1] + dy)]
  assert abs(box[0] - expected[0]) < 12 + abs(angle) * 6 and abs(box[1] - expected[1]) < 12 + abs(angle) * 6
  assert result["alignment"]["shift_px"] > 0


def test_a_blurred_frame_is_unclear_and_judges_nothing(tmp_path, reference):
  result = analyze(capture(tmp_path, deck(seed=1, blur=61)), smap(), reference)
  assert result["status"] == "unclear" and "out of focus" in result["why"] and result["slots"] == []
  assert result["frame_sharpness"] < vision.FRAME_SHARP_MIN
  assert not list(tmp_path.glob("*_slot*.jpg"))


def test_another_scene_needs_a_new_map(tmp_path, reference):
  result = analyze(capture(tmp_path, other_scene()), smap(), reference)
  assert result["status"] == "needs_new_map" and result["slots"] == [] and not result["alignment"]["ok"]
  assert "map_camera" in result["why"]


def test_a_covered_slot_is_unclear_while_the_others_stay_judged(tmp_path, reference):
  image = deck(seed=1)
  x0, y0, x1, y1 = (int(v) for v in slot_box(5))
  image[y0 - 6:y1 + 6, x0 - 6:x1 + 6] = (200, 200, 200)  # a sheet of blank paper over slot 5
  slots = by_slot(analyze(capture(tmp_path, image), smap(), reference))
  assert slots["5"]["level"] == "unclear" and "blurry or covered" in slots["5"]["why"]
  assert slots["4"]["level"] == "well" and slots["6"]["level"] == "well"


def test_the_levels_follow_the_pixels_per_well(tmp_path, reference):
  small = {"2": [141.0, 371.0, 141 + 150.0, 371 + 101.0]}  # about 1.2 px/mm: a slot, not single wells
  slot = by_slot(analyze(capture(tmp_path, deck()), smap(small), reference, {"2": PLATE}))["2"]
  assert slot["level"] == "slot" and slot["px_per_well"] < vision.WELL_MIN_PX
  tiny = {"2": [141.0, 371.0, 141 + 60.0, 371 + 40.0]}
  assert by_slot(analyze(capture(tmp_path, deck()), smap(tiny), reference))["2"]["level"] == "unclear"
  trough = by_slot(analyze(capture(tmp_path, deck()), smap(small), reference, {"2": TROUGH}))["2"]
  assert trough["level"] == "well" and trough["px_per_well"] is None  # one container: the box is the well
  side = by_slot(analyze(capture(tmp_path, deck()), smap(view="side"), reference, {"2": PLATE}))["2"]
  assert side["level"] == "slot" and "px_per_well" not in side  # a side view never resolves wells


def test_a_box_mostly_outside_the_frame_is_unclear(tmp_path, reference):
  half = {"8": [slot_box(8)[0], -190.0, slot_box(8)[2], 25.0]}
  slot = by_slot(analyze(capture(tmp_path, deck()), smap(half), reference))["8"]
  assert slot["level"] == "unclear" and slot["visible"] < vision.MIN_VISIBLE and "inside the frame" in slot["why"]


def test_a_tips_region_gets_the_tip_level_only_when_big_enough(tmp_path, reference):
  regions = {"tips:ot2.right": [100.0, 100.0, 260.0, 220.0], "tips:ot2.left": [300.0, 100.0, 340.0, 130.0]}
  slots = by_slot(analyze(capture(tmp_path, deck()), smap(regions), reference))
  assert slots["tips:ot2.right"]["level"] == "tip" and slots["tips:ot2.left"]["level"] == "unclear"
  assert (tmp_path / "top_120000_tips-ot2_right.jpg").is_file()


def test_an_unreadable_image_never_raises(tmp_path, reference):
  bad = tmp_path / "bad.jpg"
  bad.write_bytes(b"not a jpeg")
  assert analyze(bad, smap(), reference)["status"] == "unclear"
  assert analyze(tmp_path / "missing.jpg", smap(), reference)["status"] == "unclear"
  assert analyze(capture(tmp_path, deck()), smap(), tmp_path / "no-reference.jpg")["status"] == "needs_new_map"


def test_labware_geometry_and_wells_outside_the_frame():
  plate = labware_geometry(PLATE)
  assert plate["pitch_mm"] == 9.0 and plate["rows"] == 8 and plate["cols"] == 12
  assert labware_geometry(TROUGH)["pitch_mm"] is None and labware_geometry("no_such_labware") is None
  assert labware_geometry(RACK)["pitch_mm"] == 9.0
  entry = {"box": slot_box(8)}  # rows A-C above the frame, D-H inside
  frame = [W, H]
  assert vision.wells_outside(entry, PLATE, ["A1", "B1", "C1"], frame) == ["A1", "B1", "C1"]
  assert vision.wells_outside(entry, PLATE, ["D1", "E5", "H12"], frame) == []


def test_the_overlay_draws_the_boxes():
  data = vision.overlay(jpeg(deck()), {"2": slot_box(2), "tips:ot2.right": [10, 10, 90, 90]})
  image = cv2.imdecode(__import__("numpy").frombuffer(data, "uint8"), cv2.IMREAD_COLOR)
  assert image.shape[:2] == (H, W) and data != jpeg(deck())


# -- the points and their coverage ------------------------------------------------------------------

class Layout:
  """The part of a loaded layout the points read: its declared slots, aliases and labware."""

  def __init__(self):
    import pylabrobot.resources as plr

    self.declared = {"slots": {"2": {"name": "reagents", "type": PLATE}, "3": {"name": "plate", "type": PLATE},
                               "8": {"name": "top", "type": PLATE}, "10": {"name": "tips", "type": RACK}}}
    self.labware = {n: getattr(plr, t)(name=n) for n, t in (("reagents", PLATE), ("plate", PLATE), ("top", PLATE), ("tips", RACK))}
    self.aliases = {"diluent": "reagents:A1:H1"}

  _items = staticmethod(__import__("ahc.core.layout", fromlist=["Layout"]).Layout._items)


def analysis(tmp_path, reference, image=None, slots=None):
  return analyze(capture(tmp_path, image if image is not None else deck()), smap(slots), reference,
                 {"2": PLATE, "3": PLATE, "8": PLATE})


def test_points_are_derived_from_the_layout_and_the_agent_adds(tmp_path, reference):
  layout = Layout()
  agent = vision_points.parse_points([{"what": "plate:B1:B8", "level": "well", "check": "liquid_present"},
                                      {"what": "diluent", "level": "well", "check": "liquid_present"}])
  points = vision_points.derive(layout, ["ot2.left", "ot2.right"], agent)
  assert [(p["what"], p["check"], p["level"], p["declared_by"]) for p in points] == [
    ("slot:2", "deck_matches_layout", "slot", "derived"), ("slot:3", "deck_matches_layout", "slot", "derived"),
    ("slot:8", "deck_matches_layout", "slot", "derived"), ("slot:10", "deck_matches_layout", "slot", "derived"),
    ("tips:ot2.left", "tips_mounted", "tip", "derived"), ("tips:ot2.right", "tips_mounted", "tip", "derived"),
    ("plate:B1:B8", "liquid_present", "well", "agent"), ("diluent", "liquid_present", "well", "agent")]
  # the agent cannot remove a derived point by declaring it again at a lower level: it is listed once
  again = vision_points.derive(layout, [], vision_points.parse_points([{"what": "slot:2", "level": "slot", "check": "deck_matches_layout"}]))
  assert [p["what"] for p in again].count("slot:2") == 1
  merged = vision_points.merged(agent, [{"what": "plate:B1:B8", "level": "slot", "check": "liquid_present"}])
  assert [p["level"] for p in merged] == ["slot", "well"]


@pytest.mark.parametrize("raw", [[{"what": "", "level": "well", "check": "liquid_present"}],
                                 [{"what": "plate", "level": "pixel", "check": "liquid_present"}],
                                 [{"what": "plate", "level": "well", "check": "volumes_match"}],
                                 [{"what": "plate", "level": "well", "check": "liquid_present", "x": 1}], "plate", [3]])
def test_bad_points_are_refused(raw):
  with pytest.raises(LabError) as caught:
    vision_points.parse_points(raw)
  assert caught.value.code == "bad_vision_point"


def test_points_naming_nothing_in_the_layout_are_refused():
  layout = Layout()
  for what in ("ghost:A1", "plate:Z9", "slot:5", "plate:colX"):
    with pytest.raises(LabError) as caught:
      vision_points.check_points([{"what": what, "level": "well", "check": "liquid_present"}], layout)
    assert caught.value.code == "bad_vision_point", what
  vision_points.check_points([{"what": w, "level": "well", "check": "liquid_present"} for w in ("plate:B1:B8", "diluent", "plate:col2", "plate:rowD", "slot:2", "tips:ot2.right")], layout)


def covered(points, cams, layout=None):
  layout = layout or Layout()
  return physical_world.evaluate(points, lambda what: vision_points.resolve(what, layout), [VisionContext(cams)])


def by_check(result):
  return {k: v["status"] for k, v in result["physical_world_confidence"]["by_check"].items()}


def test_coverage_of_a_real_looking_deck(tmp_path, reference):
  """Slots 2, 3 and 8 are seen; slot 10 and the tips are not; plate rows A-C of slot 8 are out of the frame."""
  layout = Layout()
  seen = analysis(tmp_path, reference)
  cams = {"usb": {"map": smap({k: v for k, v in SLOTS.items() if k in "2358"}), "analysis": seen}}
  agent = vision_points.parse_points([
    {"what": "plate:B1:B8", "level": "well", "check": "liquid_present"},  # slot 3: fully in the frame
    {"what": "top:D1:D11", "level": "well", "check": "liquid_present"},  # slot 8: rows D-H are in the frame
    {"what": "top:B1:B8", "level": "well", "check": "liquid_present"}])  # slot 8: row B is above the frame
  result = covered(vision_points.derive(layout, ["ot2.right"], agent), cams, layout)
  status = {(p["what"], p["check"]): p for p in result["points"]}
  assert [status[(f"slot:{n}", "deck_matches_layout")]["status"] for n in (2, 3, 8, 10)] == ["covered", "covered", "covered", "uncovered"]
  assert status[("slot:10", "deck_matches_layout")]["why"] == "no source observes it"
  assert status[("slot:2", "deck_matches_layout")]["sources"] == [{"source": "vision:usb", "level": "well", "fresh": True, "covers": True}]
  assert status[("plate:B1:B8", "liquid_present")]["status"] == "covered"
  assert status[("top:D1:D11", "liquid_present")]["status"] == "covered"
  assert status[("top:B1:B8", "liquid_present")]["status"] == "uncovered" and "outside the frame" in status[("top:B1:B8", "liquid_present")]["why"]
  assert status[("tips:ot2.right", "tips_mounted")]["status"] == "uncovered"
  assert not result["physical_world_confidence"]["all_confident"]
  assert by_check(result) == {"deck_matches_layout": "uncovered", "tips_mounted": "uncovered", "liquid_present": "uncovered"}
  deck_check = result["physical_world_confidence"]["by_check"]["deck_matches_layout"]
  assert deck_check["points"] == {"covered": 3, "uncovered": 1, "unclear": 0} and deck_check["sources"] == [{"source": "vision:usb", "level": "well"}]
  assert result["physical_world_confidence"]["by_source"] == {"vision:usb": {"observes": 6, "covers": 5}}
  assert any("slot:10" in s for s in result["suggestions"]) and any("tips:ot2.right" in s for s in result["suggestions"])
  assert any("px per well" in s for s in result["suggestions"])


def test_a_level_below_the_need_is_uncovered_and_everything_covered_says_so(tmp_path, reference):
  layout = Layout()
  small = {"2": [141.0, 371.0, 141 + 150.0, 371 + 101.0]}  # level slot only
  cams = {"usb": {"map": smap(small), "analysis": analysis(tmp_path, reference, slots=small)}}
  need_well = [{"what": "reagents:A1", "level": "well", "check": "liquid_present", "declared_by": "agent"}]
  [row] = covered(need_well, cams, layout)["points"]
  assert row["status"] == "uncovered" and row["sources"][0]["level"] == "slot" and "the point needs well" in row["why"]
  ok = covered([{"what": "slot:2", "level": "slot", "check": "deck_matches_layout", "declared_by": "derived"}], cams, layout)
  assert ok["physical_world_confidence"]["all_confident"] and by_check(ok) == {"deck_matches_layout": "covered"} and ok["suggestions"] == []


def test_blur_and_a_moved_camera_make_points_unclear_or_need_a_new_map(tmp_path, reference):
  layout = Layout()
  point = [{"what": "slot:2", "level": "slot", "check": "deck_matches_layout", "declared_by": "derived"}]
  blurred = analysis(tmp_path, reference, deck(blur=61))
  moved = analysis(tmp_path, reference, other_scene(9))
  assert covered(point, {"usb": {"map": smap(), "analysis": blurred}}, layout)["points"][0]["status"] == "unclear"
  [lost] = covered(point, {"usb": {"map": smap(), "analysis": moved}}, layout)["points"]
  assert lost["status"] == "unclear" and lost["sources"][0]["state"] == "needs_new_map"  # unclear to the conclusion, the source says why
  [waiting] = covered(point, {"usb": {"map": smap(), "analysis": None}}, layout)["points"]
  assert waiting["status"] == "unclear" and waiting["sources"][0]["fresh"] is False and "not been captured yet" in waiting["why"]
  # one camera covering is enough, whatever the other one says
  good = analysis(tmp_path, reference)
  both = {"usb": {"map": smap(), "analysis": blurred}, "rs": {"map": smap(view="side"), "analysis": good}}
  [row] = covered(point, both, layout)["points"]
  assert row["status"] == "covered" and [e["source"] for e in row["sources"] if e["covers"]] == ["vision:rs"]
  assert [e["source"] for e in row["sources"]] == ["vision:usb", "vision:rs"]  # the blurred camera is listed too, not covering


def test_a_tips_region_covers_the_tips_point(tmp_path, reference):
  layout = Layout()
  regions = {"tips:ot2.right": [100.0, 100.0, 260.0, 220.0]}
  cams = {"close": {"map": smap(regions), "analysis": analysis(tmp_path, reference, slots=regions)}}
  [row] = covered([{"what": "tips:ot2.right", "level": "tip", "check": "tips_mounted", "declared_by": "derived"}], cams, layout)["points"]
  assert row["status"] == "covered" and row["sources"][0]["level"] == "tip"


# -- the track record -------------------------------------------------------------------------------

def test_the_record_counts_agreements_and_resets_on_a_disagreement_or_a_map_change(tmp_path):
  record = vision_points.VisionRecord(Workspace(tmp_path))
  assert record.read("m1") == {}
  for n in range(3):
    entry = record.compare("m1", "liquid_present", "pass", "pass", f"c{n}")
  assert entry["agree_streak"] == 3 and entry["total"] == 3 and entry["disagreements"] == 0
  entry = record.compare("m1", "liquid_present", "pass", "fail", "c3")
  assert entry["agree_streak"] == 0 and entry["disagreements"] == 1 and entry["last"]["agreed"] is False
  record.compare("m1", "liquid_present", "fail", "fail", "c4")  # a mismatch the person confirms agrees too
  entry = record.compare("m1", "liquid_present", "unclear", "pass", "c5")
  assert entry["agree_streak"] == 0 and entry["unclear"] == 1 and entry["disagreements"] == 1 and entry["total"] == 6
  assert record.compare("m1", "tips_mounted", "pass", "pass", "c6")["agree_streak"] == 1  # per check type
  assert set(record.read("m1")) == {"liquid_present", "tips_mounted"}
  assert record.read("m2") == {}  # another slot map: every streak starts again
  assert record.compare("m2", "liquid_present", "pass", "pass", "c7")["total"] == 1
  assert set(json.loads(record.path.read_text())["checks"]) == {"liquid_present"}


def test_the_map_digest_changes_with_any_box():
  devices = [DeviceEntry("starlet", "hamilton.starlet", "sim")]
  none = Config(devices=devices, limits={}, cameras=[CameraEntry("top", "USB Camera")])
  a = Config(devices=devices, limits={}, cameras=[CameraEntry("top", "USB Camera", smap({"2": [0, 0, 50, 50]}))])
  b = Config(devices=devices, limits={}, cameras=[CameraEntry("top", "USB Camera", smap({"2": [0, 0, 51, 50]}))])
  assert vision_points.maps_digest(none) == "none" and vision_points.maps_digest(None) == "none"
  assert len({vision_points.maps_digest(c) for c in (none, a, b)}) == 3
