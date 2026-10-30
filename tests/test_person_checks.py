"""The person's tips and liquid checks hold motion on real hardware. While one waits for their verdict nothing
moves (check_pending); a new check of the same pipette or containers replaces it. Once they fail a tips check
only drop_tips moves, once they fail a liquid check no pick-up and no liquid moves (check_failed), until what
the check found is put right. tests/fake_robot.py stands in for the robot; in simulation the simulator judges
at once and nothing is held."""

from pathlib import Path

import anyio
import pytest

from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT
from ahc.viz.dashboard import replay
from ahc.workspace import estop
from ahc.workspace.store import Workspace
from conftest import give_reference, load_verified, robot_lab

pytestmark = pytest.mark.anyio

DEV = "ot2.right"
MOTION = [("load_layout", dict(layout=OT2_LAYOUT)),
          ("pick_up_tips", dict(device=DEV, tips=["tips:col2"])),
          ("aspirate", dict(device=DEV, targets=["diluent"], volumes=100)),
          ("dispense", dict(device=DEV, targets=["plate:col1"], volumes=100)),
          ("mix", dict(device=DEV, targets=["plate:col1"], volume=50, repetitions=2)),
          ("drop_tips", dict(device=DEV)),
          ("invoke", dict(device=DEV, op="sense_tip_presence"))]


def posts(bot) -> list[str]:
  """What reached the robot that could move it: runs, commands, a home."""
  return [path for _, method, path, _ in bot.log if method == "POST"]


async def ready(lab, tips: bool = True):
  """The deck checked, and (by default) the 8-channel head carrying tips from column 1."""
  await load_verified(lab, layout=OT2_LAYOUT)
  if tips:
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col1"])


async def asked(lab, check: str, **args) -> dict:
  result = await lab.call("verify", check=check, **args)
  assert result["verdict"] == "pending", result  # real hardware: the person's verdict
  return result


# -- a check that waits for the person ------------------------------------------------------------------

async def test_while_a_tips_check_waits_for_the_person_nothing_moves(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    check = await asked(lab, "tips_mounted", device=DEV, expect="mounted")
    sent = len(posts(fake_robot))
    for tool, args in MOTION:
      text = await lab.call_text(tool, **args)
      assert "[check_pending]" in text, (tool, text)
      assert f"tips_mounted check {check['check_id']}" in text and "a tip on every channel in use, seated straight" in text
      assert "record_verdict(check_id=...)" in text.partition("Hint:")[2]
    assert len(posts(fake_robot)) == sent  # nothing reached the robot
    await lab.call("get_state")  # reading, checking and reporting go on
    await lab.call("report_decision", decision="Waiting for the person's tips check", waiting_for="person")
    overview = await lab.call("lab_overview")
    assert f"tips_mounted check {check['check_id']} waits for the person's verdict" in overview["next"]
    assert "Liquid handling is open" not in overview["next"]
    refused = [e["error"] for e in lab.server.lab.runlog.entries if e.get("status") == "refused"]
    assert refused == ["check_pending"] * len(MOTION)  # in the run log too
    passed = await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    assert passed["next"] == "Liquid handling is open; describe_device(component) for limits."
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)


async def test_a_liquid_check_waiting_for_the_person_holds_motion_too(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    check = await asked(lab, "liquid_present", targets=["plate:col1"], min_volume_ul=50)
    for tool, args in MOTION:
      text = await lab.call_text(tool, **args)
      assert "[check_pending]" in text and "each container of plate:col1 holds at least 50 uL" in text, tool
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    await lab.call("drop_tips", device=DEV)


async def test_a_new_check_of_the_same_subject_replaces_the_one_waiting(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    first = await asked(lab, "tips_mounted", device=DEV, expect="mounted")
    second = await asked(lab, "tips_mounted", device="right", expect="none")  # the same pipette, named otherwise
    assert second["supersedes"] == first["check_id"]
    text = await lab.call_text("record_verdict", check_id=first["check_id"], verdict="pass")
    assert "[check_superseded]" in text and f"Check {second['check_id']} waits for the person's verdict" in text
    held = await lab.call_text("aspirate", device=DEV, targets=["diluent"], volumes=100)
    assert second["check_id"] in held and first["check_id"] not in held  # the hold follows the new check
    other = await asked(lab, "tips_mounted", device="ot2.left", expect="none")  # another pipette: both wait
    assert "supersedes" not in other
    await lab.call("record_verdict", check_id=second["check_id"], verdict="pass")
    text = await lab.call_text("aspirate", device=DEV, targets=["diluent"], volumes=100)
    assert "[check_pending]" in text and other["check_id"] in text  # the other one still waits
    await lab.call("record_verdict", check_id=other["check_id"], verdict="pass")
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)


async def test_a_superseded_check_never_holds_and_never_shows_as_waiting(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    first = await asked(lab, "liquid_present", targets=["diluent"], min_volume_ul=100)
    second = await asked(lab, "liquid_present", targets=["reagents:A1:H1"], min_volume_ul=50)  # the same wells
    assert second["supersedes"] == first["check_id"]
    elsewhere = await asked(lab, "liquid_present", targets=["reagents:col2"])  # other wells: a check of its own
    assert "supersedes" not in elsewhere
    assert await lab.refused("record_verdict", check_id=first["check_id"], verdict="pass") == "check_superseded"
    text = await lab.call_text("pick_up_tips", device=DEV)
    assert second["check_id"] in text and elsewhere["check_id"] in text and first["check_id"] not in text
    shown = replay(Path(lab.server.lab.runlog.path))["agent"]["checks_waiting"]  # the person's dashboard
    assert [c["check_id"] for c in shown] == [second["check_id"], elsewhere["check_id"]]
    for check in (second, elsewhere):
      await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
    logged = [(e["check_id"], e["verdict"]) for e in lab.server.lab.runlog.entries if e.get("type") == "verification"
              and e["check"] == "liquid_present"]
  assert (first["check_id"], "superseded") in logged


# -- a check the person failed ---------------------------------------------------------------------------

async def test_after_a_failed_tips_check_only_drop_tips_moves_until_it_goes_through(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    check = await asked(lab, "tips_mounted", device=DEV, expect="mounted")
    failed = await lab.call("record_verdict", check_id=check["check_id"], verdict="fail")  # a tip is missing
    assert failed["next"].startswith("The person found tips_mounted not as expected")
    assert f"Only drop_tips moves until a drop_tips on {DEV} goes through" in failed["next"]
    sent = len(posts(fake_robot))
    for tool, args in MOTION:
      if tool != "drop_tips":
        text = await lab.call_text(tool, **args)
        assert "[check_failed]" in text and f"the person failed tips_mounted check {check['check_id']}" in text, tool
    assert f"verify(check='tips_mounted', device='{DEV}', expect='none')" in text.partition("Hint:")[2]  # a call that works
    assert len(posts(fake_robot)) == sent
    assert (await lab.call("verify", check="volumes_match", expected={"diluent": 1800}))["verdict"] == "pass"
    await lab.call("get_state")  # checks and reading go on
    assert f"The person failed tips_mounted check {check['check_id']}" in (await lab.call("lab_overview"))["next"]
    await lab.call("drop_tips", device=DEV, mode="discard")  # allowed: and once it went through, the hold is gone
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col2"])


async def test_a_later_passed_tips_check_of_that_pipette_clears_a_failed_one(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    check = await asked(lab, "tips_mounted", device=DEV, expect="mounted")
    await lab.call("record_verdict", check_id=check["check_id"], verdict="fail")
    other = await asked(lab, "tips_mounted", device="ot2.left", expect="none")
    await lab.call("record_verdict", check_id=other["check_id"], verdict="pass")  # another pipette: still held
    assert await lab.refused("aspirate", device=DEV, targets=["diluent"], volumes=100) == "check_failed"
    again = await asked(lab, "tips_mounted", device=DEV, expect="mounted")  # the person reseated them
    await lab.call("record_verdict", check_id=again["check_id"], verdict="pass")
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)


async def test_a_failed_check_of_every_pipette_holds_each_until_it_is_cleared(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab, tips=False)  # tips the run holds are dropped, not checked away: see the next test
    check = await asked(lab, "tips_mounted", expect="none")  # every pipette, at once
    await lab.call("record_verdict", check_id=check["check_id"], verdict="fail")
    text = await lab.call_text("pick_up_tips", device=DEV, tips=["tips:col2"])
    assert "[check_failed]" in text and f"each of {DEV}, ot2.left" in text
    assert "expect='none') without device again" in text.partition("Hint:")[2]
    right = await asked(lab, "tips_mounted", device=DEV, expect="none")  # the head is clear now, the P20 is not
    await lab.call("record_verdict", check_id=right["check_id"], verdict="pass")
    text = await lab.call_text("pick_up_tips", device=DEV, tips=["tips:col2"])
    assert "[check_failed]" in text and "a drop_tips on ot2.left goes through" in text  # and it holds everything
    left = await asked(lab, "tips_mounted", device="ot2.left", expect="none")
    await lab.call("record_verdict", check_id=left["check_id"], verdict="pass")
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col2"])


async def test_tips_the_run_knows_are_dropped_not_checked_away(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)  # 8 tips on the head, and the robot's run knows them
    text = await lab.call_text("verify", check="tips_mounted", expect="none")
    assert "[tips_mounted]" in text and f"8 on {DEV} in this run" in text and "Call drop_tips first" in text
    assert not lab.server.lab.verification.pending  # the person was not asked to take them off by hand
    assert ws.tips_left()["mounted"] == {DEV: 8}  # and the note stands
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)  # the run goes on with its tips
    await lab.call("dispense", device=DEV, targets=["plate:col1"], volumes=100)
    await lab.call("drop_tips", device=DEV)
    check = await asked(lab, "tips_mounted", expect="none")  # nothing the run knows of is on: the person's word
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")


async def test_while_the_check_of_every_pipette_waits_nothing_moves(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab, tips=False)
    check = await asked(lab, "tips_mounted", expect="none")
    for tool, args in MOTION:
      text = await lab.call_text(tool, **args)
      assert "[check_pending]" in text and "every pipette (ot2.right, ot2.left)" in text, tool
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col2"])


async def test_after_a_failed_liquid_check_no_tips_or_liquid_move_until_it_passes(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab)
    check = await asked(lab, "liquid_present", targets=["diluent"], min_volume_ul=100)
    failed = await lab.call("record_verdict", check_id=check["check_id"], verdict="fail")  # the person sees too little
    assert "No tips are picked up and no liquid moves until the person passes a later liquid_present check of " \
           "diluent" in failed["next"]
    for tool, args in MOTION:
      if tool not in ("drop_tips", "load_layout"):
        text = await lab.call_text(tool, **args)
        assert "[check_failed]" in text and f"liquid_present check {check['check_id']}" in text, tool
    part = await asked(lab, "liquid_present", targets=["reagents:A1"])  # one of its wells: not all of them
    await lab.call("record_verdict", check_id=part["check_id"], verdict="pass")
    assert await lab.refused("aspirate", device=DEV, targets=["diluent"], volumes=100) == "check_failed"
    await lab.call("drop_tips", device=DEV)  # still allowed
    covering = await asked(lab, "liquid_present", targets=["reagents:col1", "reagents:col2"])  # covers them all
    await lab.call("record_verdict", check_id=covering["check_id"], verdict="pass")
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col2"])
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)


async def test_a_new_layout_clears_a_failed_liquid_check(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await ready(lab, tips=False)
    check = await asked(lab, "liquid_present", targets=["plate:col1"], min_volume_ul=10)
    await lab.call("record_verdict", check_id=check["check_id"], verdict="fail")
    assert await lab.refused("pick_up_tips", device=DEV, tips=["tips:col2"]) == "check_failed"
    await lab.call("load_layout", layout=OT2_LAYOUT)  # allowed (the same layout too), and the deck it described is gone
    found = f"the person failed liquid_present check {check['check_id']} (they checked that each container of plate:col1"
    deck = await asked(lab, "deck_matches_layout")  # but the person checking the new deck is told what they found
    assert found in deck["next"]
    await lab.call("record_verdict", check_id=deck["check_id"], verdict="pass")
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col2"])
    await lab.call("drop_tips", device=DEV, mode="return")
    server = lab.server.lab
    estop.engage(Workspace(tmp_path), by="test")  # the deck is checked again once the stop is released: still told
    await until(lambda: server.adapter.halted)
    estop.release(Workspace(tmp_path), by="person")
    await until(lambda: server.estop is None)
    assert found in (await asked(lab, "deck_matches_layout"))["next"]


async def until(condition, seconds: float = 5.0):
  with anyio.fail_after(seconds):
    while not condition():
      await anyio.sleep(0.02)


# -- simulation --------------------------------------------------------------------------------------------

async def test_in_simulation_the_simulator_judges_at_once_and_nothing_is_held(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    give_reference(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device="starlet.pip")
    for _ in range(2):  # the same check again: judged again, nothing replaced
      wrong = await lab.call("verify", check="tips_mounted", device="starlet.pip", expect="none")
      assert wrong["verdict"] == "fail" and wrong["judged_by"] == "simulation" and "supersedes" not in wrong
    empty = await lab.call("verify", check="liquid_present", targets=["plate:A1"], min_volume_ul=10)
    assert empty["verdict"] == "fail"
    await lab.call("aspirate", device="starlet.pip", targets=["diluent"], volumes=50)  # nothing holds it
    await lab.call("dispense", device="starlet.pip", targets=["plate:A1:H1"], volumes=50)
    assert not lab.server.lab.verification.holds
