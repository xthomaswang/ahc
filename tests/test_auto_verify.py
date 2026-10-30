"""Auto-verify: the config that authorises it, when verify hands a check to the agent (and why not), agent_verdict,
and the dashboard's badges, pause and review list. Cameras are faked as in test_vision_tools (synthetic deck pictures
through a fake ffmpeg); the robot is the fake robot-server, so nothing leaves this machine."""

import json
import re
import time

import pytest

from ahc import auto_verify
from ahc.examples import OT2_LAYOUT
from ahc.viz.dashboard import replay, serve
from ahc.workspace import AutoVerify, CameraEntry, Config, DeviceEntry, estop, parse_auto_verify, parse_config
from ahc.workspace.config import HEADER
from ahc.workspace.store import Workspace
from conftest import confirm_as_person, load_verified, robot_entry
from test_dashboard import _http
from test_vision_tools import BOXES, TOP, USB, FakeCameras, camera, home, mapped_lab  # noqa: F401 - fixtures
from vision_images import deck, jpeg, other_scene

pytestmark = pytest.mark.anyio

PLAN = ["Load the layout", "Check the liquid", "Report"]
POINTS = [{"what": "reagents:A1", "level": "well", "check": "liquid_present"}]
BOTH = {"checks": ["deck_matches_layout", "liquid_present"], "min_streak": 2}
ALL = {"checks": ["deck_matches_layout", "liquid_present", "tips_mounted"], "min_streak": 2}
JUDGED_BY = "agent (auto, cameras)"


async def authorise(lab, bot, auto=BOTH):
  """The person lets the cameras answer these checks: configure_devices keeps the cameras and their maps, they confirm."""
  devices = robot_entry("127.0.0.1", bot.port, robot_name=bot.name)
  out = await lab.call("configure_devices", devices=devices, auto_verify=auto)
  assert out["config"]["status"] == "pending"
  await confirm_as_person(lab)


async def ready(lab):
  await lab.call("report_decision", decision="Plan", plan=PLAN, vision_points=POINTS)
  await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)


async def liquid(lab, step=2):
  return await lab.call("verify", check="liquid_present", targets=["reagents:A1"], min_volume_ul=10, plan_step=step)


async def crops(lab):
  """A capture of every mapped camera: the crops of slots 1-4, the way the agent gets them from capture."""
  image = (await lab.call("capture", label="the deck", cameras=["usb"]))["images"][0]
  return {e["slot"]: e["crop"] for e in image["slots"]}


async def agree(lab, check, times):
  """The person's verdicts that match the agent's proposals, `times` in a row (shadow mode builds the streak)."""
  for _ in range(times):
    if check == "liquid_present":
      pending = await liquid(lab)
    else:
      pending = await lab.call("verify", check="deck_matches_layout", plan_step=1)
    assert pending["verdict"] == "pending" and "auto" not in pending
    shown = await crops(lab)
    await lab.call("propose_verdict", check_id=pending["check_id"], verdict="pass", images=[shown["2"]])
    await lab.call("record_verdict", check_id=pending["check_id"], verdict="pass", note="the person looked")


@pytest.fixture
async def eligible(tmp_path, open_lab, fake_robot, camera):
  """A real-robot task with a mapped camera, both checks authorised and 2 agreements on each; the deck as it was."""
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await authorise(lab, fake_robot)
    await ready(lab)
    await agree(lab, "liquid_present", 2)
    await agree(lab, "deck_matches_layout", 2)
    yield lab


def server_lab(lab):
  return lab.server.lab


# -- the config -------------------------------------------------------------------------------------

def test_auto_verify_is_parsed_checked_and_defaulted():
  assert parse_auto_verify(None) is None and parse_auto_verify({}) is None and parse_auto_verify({"checks": []}) is None
  assert parse_auto_verify({"checks": ["liquid_present"]}) == AutoVerify(("liquid_present",), 5)  # the default streak is 5
  assert parse_auto_verify({"checks": ["liquid_present", "deck_matches_layout", "liquid_present"], "min_streak": 1}).checks == (
    "deck_matches_layout", "liquid_present")  # fixed order, no duplicates: one digest per meaning
  from ahc.core.errors import LabError
  for bad in ({"checks": ["volumes_match"]}, {"checks": ["recovery"]}, {"checks": "liquid_present"}, {"checks": [1]},
              {"checks": ["liquid_present"], "min_streak": 0}, {"checks": ["liquid_present"], "min_streak": True},
              {"checks": ["liquid_present"], "min_streak": "5"}, {"checks": ["liquid_present"], "min_streak": 2.5},
              {"checks": ["liquid_present"], "min_streak": 5000}, {"checks": ["liquid_present"], "extra": 1}, ["liquid_present"]):
    with pytest.raises(LabError) as caught:
      parse_auto_verify(bad)
    assert caught.value.code == "config_invalid", bad


def test_the_digest_covers_auto_verify_only_when_it_is_present():
  devices = [DeviceEntry("ot2", "opentrons.ot2", "robot", {"host": "h", "robot_name": "n"})]
  plain = Config(devices=devices, limits={})
  assert Config(devices=devices, limits={}, auto_verify=None).digest() == plain.digest()  # old digests stay the same
  with_auto = Config(devices=devices, limits={}, auto_verify=AutoVerify(("liquid_present",), 5))
  assert with_auto.digest() != plain.digest()
  assert Config(devices=devices, limits={}, auto_verify=AutoVerify(("liquid_present",), 6)).digest() != with_auto.digest()
  plain.confirm("human")
  assert parse_config(json.dumps({"devices": [{"id": "ot2", "model": "opentrons.ot2", "backend": "robot",
                                               "options": {"host": "h", "robot_name": "n"}}], "limits": {},
                                  "confirmation": plain.confirmation})).digest() == plain.digest()
  confirmed = Config(devices=devices, limits={}, auto_verify=AutoVerify(("liquid_present",), 5))
  confirmed.confirm("human")
  hand_edited = parse_config(__import__("yaml").safe_dump({**confirmed.data(), "auto_verify": {"checks": ["liquid_present", "deck_matches_layout"]}}))
  status, why = hand_edited.status(False)
  assert status == "pending" and "auto-verify" in why  # editing the authorisation by hand sends the config back to pending


async def test_configure_devices_writes_keeps_clears_and_refuses_auto_verify(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    devices = robot_entry("127.0.0.1", fake_robot.port, robot_name=fake_robot.name)
    summary_before = (await lab.call("lab_overview"))["config"]["confirm_summary"]
    assert "auto-verify" not in summary_before
    done = await lab.call("configure_devices", devices=devices, auto_verify={"checks": ["liquid_present", "deck_matches_layout"]})
    assert done["config"]["auto_verify"] == {"checks": ["deck_matches_layout", "liquid_present"], "min_streak": 5}
    assert done["config"]["status"] == "pending"  # the person confirms the new authorisation
    assert done["config"]["confirm_summary"].endswith("; auto-verify: deck_matches_layout, liquid_present after 5 agreements")
    assert await lab.refused("configure_devices", devices=devices, auto_verify={"checks": ["volumes_match"]}) == "config_invalid"
    assert await lab.refused("configure_devices", devices=devices, auto_verify={"checks": ["liquid_present"], "min_streak": 0}) == "config_invalid"
    assert (await lab.call("lab_overview"))["config"]["auto_verify"]["min_streak"] == 5  # a refusal wrote nothing
    await confirm_as_person(lab)
    kept = await lab.call("configure_devices", devices=devices)  # omitted: the authorisation stays, and so does the confirmation
    assert kept["config"]["auto_verify"]["checks"] == ["deck_matches_layout", "liquid_present"] and kept["config"]["status"] == "confirmed"
    one = await lab.call("configure_devices", devices=devices, auto_verify={"checks": ["liquid_present"], "min_streak": 1})
    assert one["config"]["confirm_summary"].endswith("; auto-verify: liquid_present after 1 agreement")
    cleared = await lab.call("configure_devices", devices=devices, auto_verify={})
    assert "auto_verify" not in cleared["config"] and "auto-verify" not in cleared["config"]["confirm_summary"]
    assert "auto_verify" not in (tmp_path / ".ahc" / "config.yaml").read_text()


async def test_a_new_camera_map_keeps_the_authorisation(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await authorise(lab, fake_robot)
    await lab.call("map_camera", camera="usb")
    mapped = await lab.call("map_camera", camera="usb", slots=BOXES)
    assert mapped["config"]["auto_verify"]["checks"] == ["deck_matches_layout", "liquid_present"]


# -- when verify is auto ----------------------------------------------------------------------------

async def test_an_eligible_check_is_auto_and_the_agents_pass_opens_the_gate(eligible, tmp_path, camera):
  lab, inner = eligible, server_lab(eligible)
  await crops(lab)
  assert (await lab.call("lab_overview"))["auto_verify"] == (
    "deck_matches_layout, liquid_present after 2 agreements. Run level: auto is on. "
    "Now: deck_matches_layout AUTO; liquid_present AUTO")
  check = await lab.call("verify", check="deck_matches_layout", plan_step=1)
  assert check["auto"] is True and "auto_off" not in check and check["verdict"] == "pending"
  assert "ask_person" not in check and "agent_verdict" in check["next"]
  assert {p["what"] for p in check["points"]} == {"slot:1", "slot:2", "slot:3", "slot:4"} and all(p["status"] == "covered" for p in check["points"])
  assert all(p["crops"] for p in check["points"]) and check["images"]
  assert check["gate"]["state"] == "pending"
  assert await lab.refused("pick_up_tips", device="ot2.right") == "gate_pending"  # nothing moves before the judgment
  assert "agent_verdict" in await lab.call_text("pick_up_tips", device="ot2.right")  # the refusal names the judgment
  assert (await lab.call("verify", check="deck_matches_layout", plan_step=1))["repeat"]  # the same check, still the agent's
  streaks = {k: v["agree_streak"] for k, v in inner.vision_record().items()}
  judged = await lab.call("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"],
                          per_point=[{"what": p["what"], "verdict": "pass"} for p in check["points"]], note="the plates sit as listed")
  assert judged["verdict"] == "pass" and judged["judged_by"] == JUDGED_BY and judged["gate"]["state"] == "passed"
  assert judged["evidence"]["camera"]["images"] == check["images"] and "review" in judged
  await lab.call("pick_up_tips", device="ot2.right")  # the gate is open as for the person's pass
  assert {k: v["agree_streak"] for k, v in inner.vision_record().items()} == streaks  # an auto verdict teaches nothing
  logged = [e for e in inner.runlog.entries if e.get("check_id") == check["check_id"]]
  assert [(e.get("verdict"), e.get("judged_by"), bool(e.get("auto"))) for e in logged if e.get("type") == "verification"][-1] == ("pass", JUDGED_BY, False)
  assert any(e.get("type") == "vision" and e.get("event") == "auto_verdict" and e["images"] == check["images"] for e in logged)
  reviewed = auto_verify.read_verdicts(inner.ws)
  assert len(reviewed) == 1 and reviewed[0]["check"] == "deck_matches_layout" and reviewed[0]["note"] == "the plates sit as listed"
  assert await lab.refused("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"]) == "unknown_check_id"  # answered once


async def test_an_eligible_liquid_check_holds_motion_until_the_agent_passes_it(eligible):
  lab = eligible
  await crops(lab)
  check = await liquid(lab)
  assert check["auto"] is True and [p["what"] for p in check["points"]] == ["reagents:A1"] and check["points"][0]["level"] == "well"
  assert await lab.refused("pick_up_tips", device="ot2.right") == "check_pending"
  held = await lab.call_text("pick_up_tips", device="ot2.right")
  assert "agent's judgment of the camera images" in held and "agent_verdict" in held
  assert await lab.refused("agent_verdict", check_id=check["check_id"], verdict="pass", images=[]) == "images_required"
  assert await lab.refused("agent_verdict", check_id=check["check_id"], verdict="pass", images=["/x/y.jpg"]) == "image_outside_run"
  assert await lab.refused("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"],
                           per_point=[{"what": "x", "verdict": "maybe"}]) == "bad_per_point"
  judged = await lab.call("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"], note="red, filled")
  assert judged["judged_by"] == JUDGED_BY and judged["evidence"]["camera"]["images"] == check["images"]
  await lab.call("pick_up_tips", device="ot2.right")


async def test_a_check_that_is_not_auto_says_why_and_stays_the_persons(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await authorise(lab, fake_robot, {"checks": ["liquid_present"], "min_streak": 2})
    await ready(lab)
    deck_check = await lab.call("verify", check="deck_matches_layout", plan_step=1)
    assert deck_check["verdict"] == "pending" and "auto" not in deck_check and "ask_person" in deck_check
    assert "not in the config's auto_verify.checks" in deck_check["auto_off"]
    assert await lab.refused("agent_verdict", check_id=deck_check["check_id"], verdict="pass", images=[]) == "not_auto"
    await lab.call("record_verdict", check_id=deck_check["check_id"], verdict="pass")


async def test_without_auto_verify_in_the_config_verify_is_as_before(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    check = await liquid(lab)
    assert "auto" not in check and "auto_off" not in check and "auto_verify" not in (await lab.call("lab_overview"))


async def test_a_paused_auto_is_off_and_resuming_turns_it_back_on(eligible):
  lab, inner = eligible, server_lab(eligible)
  await crops(lab)
  auto_verify.pause(inner.ws)
  check = await liquid(lab)
  assert "auto" not in check and "paused" in check["auto_off"] and "ask_person" in check
  assert "paused" in (await lab.call("lab_overview"))["auto_verify"]
  await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
  auto_verify.resume(inner.ws)
  assert (await liquid(lab))["auto"] is True


async def test_a_point_no_camera_covers_keeps_the_check_with_the_person(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot, slots={k: v for k, v in BOXES.items() if k != "3"}) as lab:
    await authorise(lab, fake_robot)
    await lab.call("report_decision", decision="Plan", plan=PLAN, vision_points=[
      *POINTS, {"what": "plate:B1:B8", "level": "well", "check": "liquid_present"}])
    await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)
    await agree(lab, "liquid_present", 2)
    await crops(lab)
    check = await liquid(lab)
    assert "auto" not in check and "plate:B1:B8" in check["auto_off"] and "not covered" in check["auto_off"]
    deck_check = await lab.call("verify", check="deck_matches_layout", plan_step=1)
    assert "slot:3" in deck_check["auto_off"]


async def test_one_uncovered_point_of_a_listed_type_turns_auto_off_for_every_check(tmp_path, open_lab, fake_robot, camera):
  """Auto is decided for the run: slot 3 (a deck point) is not covered, so the fully covered liquid check is the person's too."""
  async with mapped_lab(open_lab, tmp_path, fake_robot, slots={k: v for k, v in BOXES.items() if k != "3"}) as lab:
    await authorise(lab, fake_robot)
    await ready(lab)  # liquid_present: reagents:A1 on slot 2, which the camera sees at level well
    await agree(lab, "liquid_present", 2)
    await crops(lab)
    context = (await lab.call("physical_world_context"))["physical_world_confidence"]["by_check"]
    assert context["liquid_present"]["status"] == "covered" and context["deck_matches_layout"]["status"] == "uncovered"
    check = await liquid(lab)
    assert "auto" not in check and "ask_person" in check
    assert check["auto_off"].startswith("low vision confidence: ") and "deck_matches_layout point slot:3" in check["auto_off"]
    assert "reagents:A1" not in check["auto_off"]  # the covered point is not blamed
    line = (await lab.call("lab_overview"))["auto_verify"]
    assert "Run level: auto is off for every check (low vision confidence: " in line and line.count(" with the person (") == 2
    state = json.loads((tmp_path / ".ahc" / "state" / "auto.json").read_text())
    assert state["confidence"] == check["auto_off"] and state["coverage"] == {"deck_matches_layout": None, "liquid_present": None}


async def test_points_of_a_check_type_that_is_not_listed_do_not_block_auto(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot, slots={k: v for k, v in BOXES.items() if k != "3"}) as lab:
    await authorise(lab, fake_robot, {"checks": ["liquid_present"], "min_streak": 2})  # slot:3 (deck) and tips stay uncovered
    await ready(lab)
    await agree(lab, "liquid_present", 2)
    await crops(lab)
    context = (await lab.call("physical_world_context"))["physical_world_confidence"]
    assert context["by_check"]["deck_matches_layout"]["status"] == "uncovered" and context["by_check"]["tips_mounted"]["status"] == "uncovered"
    check = await liquid(lab)
    assert check["auto"] is True and "auto_off" not in check
    assert (await lab.call("lab_overview"))["auto_verify"].endswith("Run level: auto is on. Now: liquid_present AUTO")


async def test_auto_follows_physical_world_confidence_point_by_point(eligible, camera):
  lab = eligible
  seen = (await lab.call("physical_world_context", recapture=True))["physical_world_confidence"]["by_check"]
  assert seen["deck_matches_layout"]["status"] == "covered" and seen["liquid_present"]["status"] == "covered"
  assert (await liquid(lab))["auto"] is True
  camera.pictures[USB] = deck(seed=1, blur=61)  # the head in front of the lens: every point is unclear
  blurred = await lab.call("physical_world_context", recapture=True)
  by_check = blurred["physical_world_confidence"]["by_check"]
  assert by_check["deck_matches_layout"]["status"] == by_check["liquid_present"]["status"] == "unclear"
  check = await liquid(lab)
  assert "auto" not in check and check["auto_off"].startswith("low vision confidence: ")
  for point in blurred["points"]:
    if point["check"] in ("deck_matches_layout", "liquid_present"):
      assert f"{point['check']} point {point['what']}" in check["auto_off"]  # the same points the conclusion calls unclear
  camera.pictures[USB] = deck(seed=1)
  await lab.call("physical_world_context", recapture=True)
  assert (await liquid(lab))["auto"] is True  # confident again; the streak was never touched


async def test_a_liquid_target_outside_the_declared_points_is_not_auto(eligible):
  lab = eligible
  await crops(lab)
  other = await lab.call("verify", check="liquid_present", targets=["plate:A1"], min_volume_ul=0, plan_step=2)
  assert "auto" not in other and "plate:A1" in other["auto_off"] and "vision_points" in other["auto_off"]


async def test_a_liquid_check_with_no_points_is_never_vacuously_covered(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await authorise(lab, fake_robot)
    await lab.call("report_decision", decision="Plan", plan=PLAN)  # no vision_points
    await load_verified(lab, layout=OT2_LAYOUT, plan_step=1)
    await crops(lab)
    check = await liquid(lab)
    assert "auto" not in check and "no liquid_present verification points" in check["auto_off"]


async def test_a_blurred_or_misaligned_capture_is_not_enough(eligible, camera):
  lab = eligible
  camera.pictures[USB] = deck(seed=1, blur=61)  # the pipette head parked in front of the lens
  await lab.call("capture", label="parked", cameras=["usb"])
  blurred = await liquid(lab)
  assert "auto" not in blurred and "too blurred or covered" in blurred["auto_off"] and "reagents:A1" in blurred["auto_off"]
  camera.pictures[USB] = other_scene()
  await lab.call("capture", label="turned away", cameras=["usb"])
  lost = await lab.call("verify", check="deck_matches_layout", plan_step=1)
  assert "auto" not in lost and "no longer aligns" in lost["auto_off"] and "map_camera" in lost["auto_off"]
  camera.pictures[USB] = deck(seed=1)
  await crops(lab)
  assert (await liquid(lab))["auto"] is True  # a good capture brings it back


async def test_a_capture_from_before_the_last_motion_is_stale(eligible):
  lab = eligible
  await crops(lab)
  await lab.call("pick_up_tips", device="ot2.right")  # the head moved: the picture shows an older deck
  stale = await liquid(lab)
  assert "auto" not in stale and "no capture since the last motion" in stale["auto_off"]
  await lab.call("record_verdict", check_id=stale["check_id"], verdict="pass")
  await crops(lab)
  assert (await liquid(lab))["auto"] is True
  assert "not been captured yet" not in stale["auto_off"]


async def test_not_captured_yet_is_said_too(eligible):
  lab = eligible
  server_lab(lab).vision_seen.clear()
  check = await liquid(lab)
  assert "auto" not in check and "has not been captured yet" in check["auto_off"]


async def test_a_streak_below_the_minimum_keeps_the_check_with_the_person(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await authorise(lab, fake_robot)
    await ready(lab)
    await agree(lab, "liquid_present", 1)
    await crops(lab)
    check = await liquid(lab)
    assert "auto" not in check and "agreed with the person's 1 time in a row" in check["auto_off"] and "2 are needed" in check["auto_off"]
    assert (await lab.call("lab_overview"))["auto_verify"].count(" with the person (") == 2


async def test_the_recovery_check_and_tips_checks_stay_with_the_person(eligible, fake_robot):
  lab = eligible
  await authorise(lab, fake_robot, ALL)  # tips_mounted listed too, to see the other reasons
  await crops(lab)
  recovery = await lab.call("verify", check="tips_mounted", expect="none", plan_step=1)  # every pipette
  assert "auto" not in recovery and "recovery check of every pipette" in recovery["auto_off"] and "always stays with the person" in recovery["auto_off"]
  await lab.call("record_verdict", check_id=recovery["check_id"], verdict="pass")
  tips = await lab.call("verify", check="tips_mounted", device="ot2.right", plan_step=1)
  assert "auto" not in tips and "tips_mounted point tips:ot2." in tips["auto_off"]  # no camera region sees the tips: never vacuous


async def test_an_engaged_stop_is_a_reason_and_refuses_the_judgment(eligible):
  lab, inner = eligible, server_lab(eligible)
  await crops(lab)
  check = await liquid(lab)
  assert check["auto"] is True
  estop.engage(inner.ws, by="test")
  inner.poll_estop()
  assert "emergency stop is engaged" in inner.auto_assess("liquid_present")["why"]
  assert await lab.refused("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"]) == "emergency_stop"
  assert inner.verification.pending[check["check_id"]]["verdict"] == "pending"  # nothing was passed
  assert "emergency stop is engaged" in (await lab.call("lab_overview"))["auto_verify"]


# -- the agent's judgment ---------------------------------------------------------------------------

@pytest.mark.parametrize("verdict", ["fail", "unclear"])
async def test_a_fail_or_unclear_judgment_goes_to_the_person_and_motion_stays_held(eligible, verdict):
  lab, inner = eligible, server_lab(eligible)
  await crops(lab)
  check = await liquid(lab)
  handed = await lab.call("agent_verdict", check_id=check["check_id"], verdict=verdict, images=check["images"],
                          per_point=[{"what": "reagents:A1", "verdict": verdict, "note": "looks empty"}], note="cannot see liquid")
  assert handed["verdict"] == "pending" and handed["auto"] is False and f"judged {verdict}" in handed["auto_off"]
  assert handed["agent_judgment"]["verdict"] == verdict and handed["agent_judgment"]["note"] == "cannot see liquid"
  assert "ask_person" in handed and "record_verdict" in handed["next"] and "judged_by" in handed
  logged = [e for e in inner.runlog.entries if e.get("check_id") == check["check_id"]]
  assert not any(e.get("verdict") in ("pass", "fail") and e.get("type") == "verification" for e in logged)  # no verdict recorded
  assert auto_verify.read_verdicts(inner.ws) == []  # and nothing to review as a pass
  assert await lab.refused("pick_up_tips", device="ot2.right") == "check_pending"
  assert "the person's verdict" in await lab.call_text("pick_up_tips", device="ot2.right")
  assert await lab.refused("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"]) == "not_auto"  # the person's now
  done = await lab.call("record_verdict", check_id=check["check_id"], verdict="pass", note="the person looked")
  assert done["judged_by"] == "human"
  agreed = done["vision"]  # their verdict is compared with what the agent said, like a shadow judgment
  assert agreed["agent"] == verdict and agreed["agree_streak"] == 0
  await lab.call("pick_up_tips", device="ot2.right")


async def test_an_unclear_judgment_switches_auto_off_until_the_streak_is_built_again(eligible):
  lab = eligible
  await crops(lab)
  check = await liquid(lab)
  await lab.call("agent_verdict", check_id=check["check_id"], verdict="unclear", images=check["images"])
  await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
  await crops(lab)
  again = await liquid(lab)
  assert "auto" not in again and "0 times in a row" in again["auto_off"]


async def test_a_pass_with_a_point_that_is_not_a_pass_goes_to_the_person(eligible):
  lab = eligible
  await crops(lab)
  check = await liquid(lab)
  handed = await lab.call("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"],
                          per_point=[{"what": "reagents:A1", "verdict": "unclear"}])
  assert handed["verdict"] == "pending" and handed["auto"] is False and "not a pass" in handed["auto_off"]


async def test_a_motion_after_verify_means_it_is_not_auto_any_more(eligible):
  lab, inner = eligible, server_lab(eligible)
  await crops(lab)
  check = await liquid(lab)
  assert check["auto"] is True
  inner.last_motion = time.time()  # the robot moved after the check began
  handed = await lab.call("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"], note="looked fine")
  assert handed["verdict"] == "pending" and handed["auto"] is False and "no capture since the last motion" in handed["auto_off"]
  assert handed["agent_judgment"]["verdict"] == "pass"  # shown to the person, not recorded
  assert auto_verify.read_verdicts(inner.ws) == [] and inner.verification.pending[check["check_id"]]["verdict"] == "pending"
  assert (await lab.call("record_verdict", check_id=check["check_id"], verdict="pass"))["judged_by"] == "human"


async def test_a_pause_after_verify_hands_the_check_to_the_person(eligible):
  lab, inner = eligible, server_lab(eligible)
  await crops(lab)
  check = await liquid(lab)
  auto_verify.pause(inner.ws)
  handed = await lab.call("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"])
  assert handed["auto"] is False and "paused" in handed["auto_off"] and handed["verdict"] == "pending"


async def test_a_blurred_capture_after_verify_hands_the_check_to_the_person(eligible, camera):
  lab = eligible
  await crops(lab)
  check = await lab.call("verify", check="deck_matches_layout", plan_step=1)
  assert check["auto"] is True
  camera.pictures[USB] = deck(seed=1, blur=61)
  await lab.call("capture", label="parked", cameras=["usb"])
  handed = await lab.call("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"])
  assert handed["auto"] is False and "too blurred" in handed["auto_off"]
  assert await lab.refused("pick_up_tips", device="ot2.right") == "gate_pending"
  assert "person" in await lab.call_text("pick_up_tips", device="ot2.right")  # the gate now waits for them, not for the agent
  await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
  await lab.call("pick_up_tips", device="ot2.right")


async def test_images_from_before_the_last_motion_are_refused(eligible):
  lab = eligible
  old = (await crops(lab))["2"]
  await lab.call("pick_up_tips", device="ot2.right")
  await lab.call("physical_world_context")  # captures again: the deck as it is now
  check = await liquid(lab)
  assert check["auto"] is True
  assert await lab.refused("agent_verdict", check_id=check["check_id"], verdict="pass", images=[old]) == "stale_images"
  assert (await lab.call("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"]))["judged_by"] == JUDGED_BY


async def test_the_person_can_still_answer_an_auto_check_themselves(eligible):
  lab = eligible
  await crops(lab)
  check = await liquid(lab)
  done = await lab.call("record_verdict", check_id=check["check_id"], verdict="pass", note="I looked")
  assert done["judged_by"] == "human" and "auto" not in done
  assert await lab.refused("agent_verdict", check_id=check["check_id"], verdict="pass", images=check["images"]) == "unknown_check_id"


async def test_a_new_verify_replaces_a_waiting_auto_check(eligible):
  lab = eligible
  await crops(lab)
  first = await liquid(lab)
  second = await liquid(lab)
  assert first["check_id"] != second["check_id"] and second["auto"] is True
  assert await lab.refused("agent_verdict", check_id=first["check_id"], verdict="pass", images=second["images"]) == "check_superseded"


async def test_a_deck_check_the_person_was_asked_becomes_auto_once_the_cameras_cover_it(tmp_path, open_lab, fake_robot, camera):
  async with mapped_lab(open_lab, tmp_path, fake_robot) as lab:
    await authorise(lab, fake_robot)
    await ready(lab)
    await agree(lab, "deck_matches_layout", 2)
    server_lab(lab).vision_seen.clear()
    first = await lab.call("verify", check="deck_matches_layout", plan_step=1)
    assert "auto" not in first and "has not been captured yet" in first["auto_off"]
    await crops(lab)
    again = await lab.call("verify", check="deck_matches_layout", plan_step=1)  # the same check, now the agent's
    assert again["check_id"] == first["check_id"] and again["auto"] is True and "agent_verdict" in again["next"]
    assert (await lab.call("agent_verdict", check_id=again["check_id"], verdict="pass", images=again["images"]))["gate"]["state"] == "passed"


async def test_agent_verdict_is_for_real_hardware(tmp_path, open_lab, camera):
  async with open_lab(tmp_path, "sim") as lab:
    await lab.call("configure_devices", devices=[{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}])
    assert await lab.refused("agent_verdict", check_id="x", verdict="pass", images=["a.jpg"]) == "auto_not_in_simulation"
    names = {t.name for t in (await lab.client.list_tools()).tools}
    assert "agent_verdict" in names


# -- the pure rule ----------------------------------------------------------------------------------

def test_the_first_failed_condition_is_the_reason():
  auto = AutoVerify(("liquid_present",), 3)
  ok = dict(pause=None, stopped=False, recovery=False, coverage=None, streak=3)
  assert auto_verify.off_reason("liquid_present", auto, **ok) is None
  assert "not in the config's auto_verify.checks" in auto_verify.off_reason("deck_matches_layout", auto, **ok)
  assert "no auto_verify" in auto_verify.off_reason("liquid_present", None, **ok)
  assert "paused" in auto_verify.off_reason("liquid_present", auto, **{**ok, "pause": {"by": "dashboard"}, "stopped": True})
  assert "emergency stop" in auto_verify.off_reason("liquid_present", auto, **{**ok, "stopped": True, "recovery": True})
  assert "recovery" in auto_verify.off_reason("liquid_present", auto, **{**ok, "recovery": True, "coverage": "x"})
  assert auto_verify.off_reason("liquid_present", auto, **{**ok, "coverage": "stale", "streak": 0}) == "stale"
  assert auto_verify.off_reason("liquid_present", auto, **{**ok, "confidence": "low vision confidence: x", "coverage": "y", "streak": 0}) == "low vision confidence: x"
  assert "recovery" in auto_verify.off_reason("liquid_present", auto, **{**ok, "recovery": True, "confidence": "low vision confidence: x"})
  assert "2 times in a row" in auto_verify.off_reason("liquid_present", auto, **{**ok, "streak": 2}) and "3 are needed" in auto_verify.off_reason("liquid_present", auto, **{**ok, "streak": 2})


# -- the dashboard ----------------------------------------------------------------------------------

@pytest.fixture
def task(tmp_path):
  """A task folder: both checks authorised, a pause-able dashboard, one auto verdict with a capture to review."""
  ws = Workspace(tmp_path / "task")
  ws.root.mkdir()
  config = Config(devices=[DeviceEntry("starlet", "hamilton.starlet", "sim")], limits={}, cameras=[CameraEntry("usb", "USB Camera")],
                  auto_verify=AutoVerify(("deck_matches_layout", "liquid_present"), 2))
  config.confirm("human")
  ws.write_config(config.data(), HEADER)
  ws.runs_dir.mkdir(parents=True, exist_ok=True)
  (ws.runs_dir / "r.jsonl").write_text(json.dumps({"type": "run", "run_id": "r", "device": "hamilton.starlet", "backend": "sim"}) + "\n")
  step = ws.runs_dir / "r" / "2_step"
  step.mkdir(parents=True, exist_ok=True)
  (step / "usb_121500_slot2.jpg").write_bytes(jpeg(deck()))
  ws.state_dir.mkdir(parents=True, exist_ok=True)
  (ws.state_dir / "vision_record.json").write_text(json.dumps({"maps": "none", "checks": {"liquid_present": {"agree_streak": 2}, "deck_matches_layout": {"agree_streak": 1}}}))
  auto_verify.write_coverage(ws, {"deck_matches_layout": None, "liquid_present": None})
  auto_verify.append_verdict(ws, {"check_id": "abc", "check": "liquid_present", "images": [str(step / "usb_121500_slot2.jpg"), "/elsewhere/x.jpg"],
                                  "per_point": [{"what": "reagents:A1", "verdict": "pass", "note": "red"}], "note": "filled to the top"})
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


def test_the_state_says_auto_or_why_it_is_off_per_check(pages):
  ws, control, viewer = pages
  shown = replay(ws.runs_dir / "r.jsonl")["auto"]
  assert shown["min_streak"] == 2 and shown["paused"] is None
  assert shown["checks"]["liquid_present"] == {"state": "auto", "why": None, "streak": 2}
  assert shown["checks"]["deck_matches_layout"]["state"] == "off" and "agreed with the person's 1 time" in shown["checks"]["deck_matches_layout"]["why"]
  auto_verify.write_coverage(ws, {"deck_matches_layout": "slot:3 has no capture since the last motion", "liquid_present": "x is blurred"})
  shown = replay(ws.runs_dir / "r.jsonl")["auto"]["checks"]
  assert shown["liquid_present"]["why"] == "x is blurred" and shown["deck_matches_layout"]["why"].startswith("slot:3")  # the server's coverage first
  assert replay(ws.runs_dir / "r.jsonl")["auto"]["run"] == {"state": "on", "why": None}
  auto_verify.write_coverage(ws, {"deck_matches_layout": None, "liquid_present": None}, "low vision confidence: deck_matches_layout point slot:3 (level slot) is not covered: x")
  shown = replay(ws.runs_dir / "r.jsonl")["auto"]
  assert shown["run"]["state"] == "off" and shown["run"]["why"].startswith("low vision confidence: ")
  assert shown["checks"]["liquid_present"]["state"] == "off" and shown["checks"]["liquid_present"]["why"] == shown["run"]["why"]  # the run, not the check
  auto_verify.write_coverage(ws, {"deck_matches_layout": "slot:3 has no capture since the last motion", "liquid_present": "x is blurred"})
  auto_verify.pause(ws)
  assert "paused" in replay(ws.runs_dir / "r.jsonl")["auto"]["checks"]["liquid_present"]["why"]
  assert "paused" in replay(ws.runs_dir / "r.jsonl")["auto"]["run"]["why"]
  for port in (control, viewer):
    assert json.loads(_http(port, "/state")[2])["auto"]["paused"]["by"] == "dashboard"
  estop.engage(ws, by="test")
  auto_verify.resume(ws)
  assert "emergency stop" in replay(ws.runs_dir / "r.jsonl")["auto"]["checks"]["liquid_present"]["why"]


def test_without_auto_verify_in_the_config_the_state_has_no_auto_part(tmp_path):
  ws = Workspace(tmp_path / "plain")
  ws.root.mkdir()
  config = Config(devices=[DeviceEntry("starlet", "hamilton.starlet", "sim")], limits={})
  ws.write_config(config.data(), HEADER)
  ws.runs_dir.mkdir(parents=True, exist_ok=True)
  (ws.runs_dir / "r.jsonl").write_text(json.dumps({"type": "run", "run_id": "r", "device": "hamilton.starlet", "backend": "sim"}) + "\n")
  assert replay(ws.runs_dir / "r.jsonl")["auto"] is None


def test_the_control_page_pauses_and_resumes_and_the_view_page_cannot(pages):
  ws, control, viewer = pages
  token = token_of(control)
  assert 'id="autocard"' in _http(control, "/")[2].decode() and "Pause auto" in _http(control, "/")[2].decode()
  status, _, body = _http(control, "/auto/pause", "POST", {}, **mine(control, token))
  assert status == 200 and json.loads(body)["paused"] is True
  assert auto_verify.paused(ws)["by"] == "dashboard" and (ws.dir / "auto-pause.json").is_file()
  status, _, body = _http(control, "/auto/resume", "POST", {}, **mine(control, token))
  assert status == 200 and json.loads(body) == {"paused": False, "changed": True}
  assert not (ws.dir / "auto-pause.json").exists() and auto_verify.paused(ws) is None
  assert json.loads(_http(control, "/auto/resume", "POST", {}, **mine(control, token))[2])["changed"] is False


def test_a_pause_needs_the_page_its_token_and_its_origin(pages):
  ws, control, viewer = pages
  token = token_of(control)
  for route in ("/auto/pause", "/auto/resume"):
    assert _http(viewer, route, "POST", {}, **mine(viewer, token))[0] == 403  # the audience's page changes nothing
    assert _http(viewer, route, "POST", {}, **mine(viewer))[0] == 403
    assert _http(control, route, "POST", {})[0] == 403  # no header, no origin
    assert _http(control, route, "POST", {}, **mine(control))[0] == 403  # no token
    assert _http(control, route, "POST", {}, **{**mine(control, token), "Origin": "http://evil.example"})[0] == 403
    assert _http(control, route, "POST", {}, **mine(control, "0" * 32))[0] == 403
    assert _http(control, route, "POST", {}, Host="evil.example", **mine(control, token))[0] == 403
  assert not (ws.dir / "auto-pause.json").exists()
  page = _http(viewer, "/")[2].decode()  # the view-only page shows the state; its script gets no token and hides the button
  assert 'name="ahc-view" content="1"' in page and token not in page and 'name="ahc-token" content=""' in page


def test_the_review_list_has_the_agents_verdicts_with_images_the_pages_serve(pages):
  ws, control, viewer = pages
  [item] = replay(ws.runs_dir / "r.jsonl")["auto"]["review"]
  assert item["check"] == "liquid_present" and item["note"] == "filled to the top" and item["check_id"] == "abc"
  assert item["images"] == ["r/2_step/usb_121500_slot2.jpg"]  # the one outside the runs folder is left out
  assert item["per_point"] == [{"what": "reagents:A1", "verdict": "pass", "note": "red"}]
  for port in (control, viewer):
    status, headers, body = _http(port, "/run-image/r/2_step/usb_121500_slot2.jpg")
    assert status == 200 and headers["content-type"] == "image/jpeg" and body[:2] == b"\xff\xd8"
    assert _http(port, "/run-image/r/2_step/nothing.jpg")[0] == 404
    assert _http(port, "/run-image/..%2Fconfig.yaml")[0] == 404 and _http(port, "/run-image/../config.yaml")[0] == 404
    assert _http(port, "/run-image/r.jsonl")[0] == 404  # only .jpg files
    assert _http(port, "/run-image/r/2_step/usb_121500_slot2.jpg", Host="evil.example")[0] == 403
    assert _http(port, "/run-image/%2e%2e/%2e%2e/config.yaml")[0] == 404


def test_the_run_log_tells_the_page_what_the_agent_said_when_it_handed_a_check_over(task):
  log = task.runs_dir / "r.jsonl"
  entries = [{"type": "verification", "check_id": "c1", "check": "liquid_present", "verdict": "pending", "auto": True, "judged_by": "x",
              "evidence": {"digital_twin": {"volumes_ul": {"reagents:A1": 0}, "min_volume_ul": 10}}},
             {"type": "verification", "check_id": "c1", "check": "liquid_present", "verdict": "pending", "judged_by": "the person: auto-verify is off",
              "evidence": {"digital_twin": {"volumes_ul": {"reagents:A1": 0}, "min_volume_ul": 10}}},
             {"type": "vision", "event": "handback", "check_id": "c1", "check": "liquid_present", "verdict": "unclear", "note": "glare", "why": "you judged unclear"}]
  with log.open("a") as f:
    f.writelines(json.dumps(e) + "\n" for e in entries)
  state = replay(log)
  [waiting] = state["agent"]["checks_waiting"]
  assert waiting["check_id"] == "c1" and waiting["said"] == {"verdict": "unclear", "note": "glare", "why": "you judged unclear"}
  with log.open("a") as f:
    f.write(json.dumps({"type": "vision", "event": "auto_verdict", "check_id": "c2", "check": "deck_matches_layout", "images": ["a"], "note": "ok"}) + "\n")
  assert replay(log)["events"][-1]["op"] == "auto verdict"


def test_the_agents_time_on_an_auto_check_is_not_the_persons():
  from test_timing import at, check, run
  from ahc.timing import run_timing
  auto_pending = {**check("c1", "pending", 14), "auto": True}
  log = run(auto_pending, check("c1", "pass", 44), check("c2", "pending", 50), {**check("c2", "pending", 52), "auto": True},
            check("c3", "pending", 60), check("c3", "pending", 61), check("c3", "pass", 70), {"t": at(80), "type": "agent", "decision": "x", "plan_done": True})
  t = run_timing(log)
  assert t["counts"]["checks"] == 3  # each check once, however often the log mentions it
  assert t["split"]["others"]["person_checks_s"] == pytest.approx(2.0 + 9.0 + 0.0, abs=0.5)  # c2 until auto took it, c3 until the verdict
