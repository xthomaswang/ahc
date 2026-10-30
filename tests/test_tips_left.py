"""Tips a real robot forgets. Once its run stops (the server shuts down or is killed, the config changes, the
emergency stop, a call whose outcome is unknown) the tips stay on the pipettes, but the robot no longer knows
them, and the next run's first pick-up would drive them into a rack. .ahc/tips-left.json holds them from the
pick-up on, and load_layout waits for the person's word that the pipettes are empty: one check of every
pipette. tests/fake_robot.py stands in for the robot; simulation notes and holds back nothing."""

import anyio
import pytest

from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT
from ahc.workspace import estop
from ahc.workspace.store import Workspace
from conftest import confirm_pipettes_empty, give_reference, load_verified, robot_entry, robot_lab

pytestmark = pytest.mark.anyio

DEV = "ot2.right"
ASK_THE_PERSON = "verify(check='tips_mounted', expect='none') without device"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
  monkeypatch.setenv("AHC_HOME", str(tmp_path / "ahc-home"))
  monkeypatch.delenv("AHC_BACKEND", raising=False)


def posts(bot) -> list[str]:
  """What reached the robot that could move it: runs, commands, a home."""
  return [path for _, method, path, _ in bot.log if method == "POST"]


async def go_ahead(lab):
  """The person's go-ahead for a new session: they approve confirm_config with the summary they were shown."""
  summary = (await lab.call("lab_overview"))["config"]["confirm_summary"]
  await lab.call("confirm_config", summary=summary)


def killed(lab) -> None:
  """The server will end as a kill or a crash ends it: no shutdown, nothing written on the way out."""
  async def nothing():
    pass

  lab.server.lab.shutdown = nothing


async def until(condition, seconds: float = 5.0):
  with anyio.fail_after(seconds):
    while not condition():
      await anyio.sleep(0.02)


# -- a run that knows its tips ------------------------------------------------------------------------

async def test_a_run_that_knows_its_tips_notes_them_and_holds_nothing_back(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    run = lab.server.lab.adapter.device._run.id
    await lab.call("pick_up_tips", device=DEV)
    note = ws.tips_left()  # from the pick-up on, so neither a kill nor a crash loses them
    assert note["mounted"] == {DEV: 8} and note["run"] == run and note["why"] == "picked up"
    assert note["device"] == "opentrons.ot2" and note["endpoint"] == f"127.0.0.1:{fake_robot.port}"
    assert "Tips may" not in (await lab.call("lab_overview"))["next"]  # this run's own tips: no warning
    check = await lab.call("verify", check="tips_mounted", device=DEV, expect="mounted")
    assert check["verdict"] == "pending" and "Tips may" not in check["next"]
    assert check["evidence"]["digital_twin"]["tips_mounted"] == 8
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
    await lab.call("dispense", device=DEV, targets=["plate:col1"], volumes=100)
    await lab.call("mix", device=DEV, targets=["plate:col1"], volume=50, repetitions=2)
    assert ws.tips_left() == note
    await lab.call("drop_tips", device=DEV, mode="return")
    assert ws.tips_left() is None  # no tip on any pipette of the live run
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col2"])
    assert ws.tips_left()["mounted"] == {DEV: 8}
    await lab.call("drop_tips", device=DEV, mode="discard")
    assert ws.tips_left() is None
    await load_verified(lab, layout=OT2_LAYOUT)  # nothing on: the next run starts without asking anyone


async def test_a_pick_up_is_on_disk_before_the_robot_has_the_tips(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    fake_robot.durations["pickUpTip"] = 1.0
    during = {}

    async def look():  # what a server that ends now leaves (killed, crashed, or its client gone)
      await until(lambda: any(what == "pickUpTip" for _, method, what, _ in fake_robot.log if method == "EXEC-START"))
      during.update(ws.tips_left() or {})

    async with anyio.create_task_group() as tg:
      tg.start_soon(look)
      await lab.call("pick_up_tips", device=DEV, tips=["tips:col1"])
    assert during["mounted"] == {DEV: "unknown"} and during["why"] == "picking up"
    picked = ws.tips_left()
    assert picked["mounted"] == {DEV: 8} and picked["why"] == "picked up"  # then what the robot holds
    # A pick-up refused before anything was sent leaves the note as it was: the run's tips, or none.
    assert await lab.refused("pick_up_tips", device=DEV, tips=["tips:col2"]) == "tips_already_mounted"
    assert ws.tips_left() == picked
    await lab.call("drop_tips", device=DEV)
    assert await lab.refused("pick_up_tips", device=DEV, tips=["tips:A3"]) == "tip_count"
    assert ws.tips_left() is None
  ws.mark_tips_left(during)  # as a server that ended during the pick-up left it
  async with open_lab(tmp_path, backend=None) as lab:
    await go_ahead(lab)
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
  assert "[tips_unconfirmed]" in text and f"an unknown number on {DEV} (picked up in a run whose server ended during" in text


async def test_a_reload_with_tips_on_says_to_drop_them_first(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device=DEV)
    sent = len(posts(fake_robot))
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[tips_unconfirmed]" in text and f"8 on {DEV} in this run" in text and "forgets them" in text
    hint = text.partition("Hint:")[2]
    assert "Call drop_tips first" in hint and "verify(" not in hint  # the run knows its tips: nobody to ask
    assert len(posts(fake_robot)) == sent  # no new run, no home
    await lab.call("drop_tips", device=DEV, mode="return")
    await load_verified(lab, layout=OT2_LAYOUT)


# -- tips a run left behind ----------------------------------------------------------------------------

async def test_tips_a_killed_server_left_on_hold_back_the_next_run(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device=DEV)
    killed(lab)
  note = ws.tips_left()
  assert note["mounted"] == {DEV: 8} and note["why"] == "picked up"  # nothing was written on the way out
  runs = posts(fake_robot).count("/runs")
  async with open_lab(tmp_path, backend=None) as lab:  # the next session
    before = (await lab.call("lab_overview"))["next"]  # both wait for the person: the go-ahead first, then the tips
    assert before.startswith("This session needs the person's go-ahead") and "Tips may still be on the pipettes" in before
    assert before.index("Tips may still") < before.index("load_layout, then")
    await go_ahead(lab)
    after = (await lab.call("lab_overview"))["next"]
    assert after.startswith(f"Tips may still be on the pipettes: 8 on {DEV}") and ASK_THE_PERSON in after
    assert after.index(ASK_THE_PERSON) < after.index("load_layout, then")  # before load_layout
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[tips_unconfirmed]" in text and f"8 on {DEV}" in text  # which pipette, how many
    assert "picked up in a run that never dropped them: its server was killed or crashed" in text  # why
    assert note["at"].replace("T", " ") in text  # when
    hint = text.partition("Hint:")[2]
    assert "Tell the person exactly that" in hint and "remove any tips by hand (or there are none)" in hint
    assert f"Then call {ASK_THE_PERSON} and record_verdict for it with its ask_person right away" in hint
    assert "hands and deck are clear" in hint  # the dialog before load_layout carries the homing go-ahead
  assert posts(fake_robot).count("/runs") == runs  # no new run


async def test_tips_of_another_session_on_this_folder_are_never_said_to_be_left_behind(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as first:
    await load_verified(first, layout=OT2_LAYOUT)
    await first.call("pick_up_tips", device=DEV)
    async with open_lab(tmp_path, backend=None) as second:  # a second agent session in the same folder
      await go_ahead(second)
      text = await second.call_text("load_layout", layout=OT2_LAYOUT)
  assert "[tips_unconfirmed]" in text and "ended without dropping them" not in text  # its server may well be alive
  assert "unless another agent session open on this folder still holds them (then that one drops them first)" in text


@pytest.mark.parametrize("why", ["the server shut down", "the device config changed"])
async def test_the_note_says_why_the_robot_forgot_them(tmp_path, open_lab, fake_robot, why):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device=DEV)
    if why == "the device config changed":  # the person switches to the simulation of the same model
      await lab.call("configure_devices", devices=[{"id": "ot2", "model": "opentrons.ot2", "backend": "sim"}])
      await go_ahead(lab)
      overview = await lab.call("lab_overview")
      assert overview["simulated"] and "Tips may" not in overview["next"]  # simulation never shows it
      assert ws.tips_left()["why"] == why
      await lab.call("configure_devices", devices=robot_entry("127.0.0.1", fake_robot.port))  # and back
      await go_ahead(lab)
      assert await lab.refused("load_layout", layout=OT2_LAYOUT) == "tips_unconfirmed"
  note = ws.tips_left()
  assert note["mounted"] == {DEV: 8} and note["why"] == why
  async with open_lab(tmp_path, backend=None) as lab:
    await go_ahead(lab)
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
  assert "[tips_unconfirmed]" in text and f"8 on {DEV} ({why}, {note['at'].replace('T', ' ')})" in text


async def test_with_no_device_bound_a_note_waits_behind_the_config(tmp_path, open_lab):
  Workspace(tmp_path).mark_tips_left({"at": "2026-10-23T14:02:00", "why": "the server shut down", "device": "opentrons.ot2",
                                      "run": "abc", "mounted": {DEV: 8}, "endpoint": "10.0.0.5:31950"})
  async with open_lab(tmp_path, backend=None) as lab:
    await lab.call("configure_devices", devices=[{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}])
    after = (await lab.call("lab_overview"))["next"]
    assert after.startswith("The config waits for the person")  # what can be done now comes first
    assert f"Tips may still be on the pipettes of the robot at 10.0.0.5:31950: 8 on {DEV}" in after  # then the note
    assert ASK_THE_PERSON not in after  # not a check the config refuses
    assert await lab.refused("verify", check="tips_mounted", expect="none") == "config_pending"


async def test_tips_left_on_another_robot_name_that_robot(tmp_path, open_lab, fake_robot):
  from fake_robot import FakeRobot

  other = FakeRobot("other-ot2")
  try:
    async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
      await load_verified(lab, layout=OT2_LAYOUT)
      await lab.call("pick_up_tips", device=DEV)
      await lab.call("configure_devices", devices=robot_entry("127.0.0.1", other.port))  # the folder moves on
      await go_ahead(lab)
      assert Workspace(tmp_path).tips_left()["robot"] == fake_robot.name
      robot = f"of the robot {fake_robot.name!r} at 127.0.0.1:{fake_robot.port}"
      text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
      assert "[tips_unconfirmed]" in text and f"Tips may still be on the pipettes {robot}: 8 on {DEV}" in text
      check = await lab.call("verify", check="tips_mounted", expect="none")
      assert robot in check["next"]  # the person checks the robot the tips are on, not only this one
  finally:
    other.close()


async def test_the_person_clears_them_with_one_check_of_every_pipette(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device=DEV)
  runs = posts(fake_robot).count("/runs")
  async with open_lab(tmp_path, backend=None, auto_plan=False) as lab:  # after a restart, before any layout
    await go_ahead(lab)
    await lab.call("report_decision", decision="Clear the pipettes, then load", plan=["Pipettes", "Load"])
    assert await lab.refused("load_layout", layout=OT2_LAYOUT, plan_step=2) == "tips_unconfirmed"
    sent = len(fake_robot.log)
    check = await lab.call("verify", check="tips_mounted", expect="none", plan_step=1)  # no device: every pipette
    assert check["verdict"] == "pending" and check["judged_by"] == "human (placeholder)"
    twin = check["evidence"]["digital_twin"]
    assert twin["pipettes"] == {"ot2.right": "unknown", "ot2.left": "unknown"} and twin["as_expected"] is None
    assert f"checks every pipette (ot2.left, {DEV})" in check["next"] and "8 on ot2.right" in check["next"]
    failed = await lab.call("record_verdict", check_id=check["check_id"], verdict="fail")  # tips are still on
    assert "they remove them by hand" in failed["next"] and "again without device" in failed["next"]
    assert ws.tips_left() is not None
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT, plan_step=2)  # the failed check holds it, and why
    assert "[check_failed]" in text and "Tips may still be on the pipettes: 8 on ot2.right" in text
    check = await lab.call("verify", check="tips_mounted", expect="none", plan_step=1)
    passed = await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    assert fake_robot.log[sent:] == []  # the check never touched the robot
    assert ws.tips_left() is None and "Tips may" not in passed["next"]
    await lab.call("load_layout", layout=OT2_LAYOUT, plan_step=2)
  assert posts(fake_robot).count("/runs") == runs + 1


# -- tips the person found -----------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["restart", "kill"])
async def test_a_tip_the_person_found_holds_back_the_next_run(tmp_path, open_lab, fake_robot, how):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    check = await lab.call("verify", check="tips_mounted", device="ot2.left", expect="none")
    await lab.call("record_verdict", check_id=check["check_id"], verdict="fail")  # a tip the robot does not know of
    note = ws.tips_left()  # on disk at once
    assert note["mounted"] == {"ot2.left": "unknown"}
    assert note["why"].startswith(f"the person failed tips_mounted check {check['check_id']} at ")
    if how == "kill":
      killed(lab)
  async with open_lab(tmp_path, backend=None) as lab:  # what the check held back ended with that server; the note did not
    await go_ahead(lab)
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
  assert "[tips_unconfirmed]" in text and f"an unknown number on ot2.left ({note['why']}" in text


async def test_what_the_person_found_comes_off_the_note_once_it_is_cleared(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col1"])
    check = await lab.call("verify", check="tips_mounted", device="ot2.left", expect="none")
    await lab.call("record_verdict", check_id=check["check_id"], verdict="fail")
    noted = ws.tips_left()
    assert noted["mounted"] == {"ot2.left": "unknown", DEV: 8}
    await anyio.sleep(1.1)  # a verdict that changes nothing leaves the note as it is, with the time it gives
    seated = await lab.call("verify", check="tips_mounted", device=DEV, expect="mounted")
    await lab.call("record_verdict", check_id=seated["check_id"], verdict="pass")
    assert ws.tips_left() == noted
    await lab.call("drop_tips", device=DEV)  # the head's tips come off the note; the tip the person found stays
    assert ws.tips_left()["mounted"] == {"ot2.left": "unknown"}
    again = await lab.call("verify", check="tips_mounted", device="ot2.left", expect="none")
    await lab.call("record_verdict", check_id=again["check_id"], verdict="pass")  # the person took it off
    assert ws.tips_left() is None
    await load_verified(lab, layout=OT2_LAYOUT)


async def test_the_note_of_another_run_keeps_what_it_says(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col1"])
    killed(lab)
  note = ws.tips_left()
  async with open_lab(tmp_path, backend=None) as lab:  # no layout yet: the person checks one pipette and fails it
    await go_ahead(lab)
    check = await lab.call("verify", check="tips_mounted", device="ot2.left", expect="none")
    await lab.call("record_verdict", check_id=check["check_id"], verdict="fail")
  assert ws.tips_left() == note  # still 8 on ot2.right; the check of every pipette that clears it covers the P20 too


# -- a stop or an unknown outcome in this run ----------------------------------------------------------

async def test_after_the_emergency_stop_with_tips_on_the_person_confirms_the_pipettes_are_empty(tmp_path, open_lab,
                                                                                                fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    server = lab.server.lab
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col1"])
    fake_robot.durations["aspirateInPlace"] = 1.0

    async def press_while_drawing():
      await until(lambda: any(what == "aspirateInPlace" for _, method, what, _ in fake_robot.log
                              if method == "EXEC-START"))
      estop.engage(ws, by="test")

    async with anyio.create_task_group() as tg:
      tg.start_soon(press_while_drawing)
      assert "[emergency_stop]" in await lab.call_text("aspirate", device=DEV, targets=["diluent"], volumes=100)
    note = ws.tips_left()  # written as the stop was taken: a crash from here on loses nothing
    assert note["mounted"] == {DEV: 8} and note["why"].startswith("the emergency stop at ")
    assert note["why"].endswith(" during aspirate") and note["run"] == server.adapter.device._run.id
    await until(lambda: server.adapter.halted)
    estop.release(ws, by="person")
    await until(lambda: server.estop is None)
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[tips_unconfirmed]" in text and f"8 on {DEV} ({note['why']})" in text
    after = (await lab.call("lab_overview"))["next"]
    assert after.startswith("The outcome of aspirate on the robot is unknown") and ASK_THE_PERSON in after
    sent = len(fake_robot.log)
    check = await lab.call("verify", check="tips_mounted", expect="none")  # the adapter is halted
    assert check["verdict"] == "pending" and set(check["evidence"]["digital_twin"]["pipettes"].values()) == {"unknown"}
    assert f"Tips check {check['check_id']} waits for the person's verdict" in server.next_step()
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    assert fake_robot.log[sent:] == []  # nothing reached the robot: no motion, no new run, not even a read
    assert ws.tips_left() is None
    assert "The person confirmed the pipettes are empty" in (await lab.call("lab_overview"))["next"]
    await load_verified(lab, layout=OT2_LAYOUT)  # a new run
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col2"])


async def test_a_check_of_one_pipette_waiting_at_a_stop_gives_way_to_the_check_of_every_pipette(tmp_path, open_lab,
                                                                                                 fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    server = lab.server.lab
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col1"])
    one = await lab.call("verify", check="tips_mounted", device=DEV, expect="mounted")  # waits for the person
    estop.engage(ws, by="test")
    await until(lambda: server.adapter.halted)
    estop.release(ws, by="person")
    await until(lambda: server.estop is None)
    after = (await lab.call("lab_overview"))["next"]
    assert f"tips_mounted check {one['check_id']} waits for the person's verdict" in after  # it still holds motion
    assert ASK_THE_PERSON in after
    every = await lab.call("verify", check="tips_mounted", expect="none")  # about the tips the robot forgot
    assert every["supersedes"] == one["check_id"]
    assert await lab.refused("record_verdict", check_id=one["check_id"], verdict="fail") == "check_superseded"
    await lab.call("record_verdict", check_id=every["check_id"], verdict="pass")
    await load_verified(lab, layout=OT2_LAYOUT)  # a fresh run, as the person's word allows


async def test_an_unknown_outcome_with_tips_on_is_noted_and_holds_the_next_run(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col1"])
    fake_robot.fail_commands["aspirateInPlace"] = "pressure fault"
    assert await lab.refused("aspirate", device=DEV, targets=["diluent"], volumes=100) == "completion_unknown"
    assert await lab.refused("drop_tips", device=DEV) == "completion_unknown"  # refused before it reached the robot
    note = ws.tips_left()
    assert note["mounted"] == {DEV: 8} and note["why"].startswith("an unknown outcome of aspirate at ")
    killed(lab)
  fake_robot.fail_commands.clear()
  async with open_lab(tmp_path, backend=None) as lab:  # after a crash: the note still holds the next run back
    await go_ahead(lab)
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[tips_unconfirmed]" in text and note["why"] in text
    await confirm_pipettes_empty(lab)
    await load_verified(lab, layout=OT2_LAYOUT)


@pytest.mark.parametrize("device", [DEV, "right"])  # the pipette by its address, or by its component alone
async def test_a_pick_up_the_stop_cut_short_leaves_that_pipette_unknown(tmp_path, open_lab, fake_robot, device):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    server = lab.server.lab
    await load_verified(lab, layout=OT2_LAYOUT)
    fake_robot.durations["pickUpTip"] = 1.0

    async def press_while_picking_up():
      await until(lambda: any(what == "pickUpTip" for _, method, what, _ in fake_robot.log if method == "EXEC-START"))
      estop.engage(ws, by="test")

    async with anyio.create_task_group() as tg:
      tg.start_soon(press_while_picking_up)
      assert "[emergency_stop]" in await lab.call_text("pick_up_tips", device=device, tips=["tips:col1"])
    note = ws.tips_left()
    assert note["mounted"] == {DEV: "unknown"} and note["why"].endswith(" during pick_up_tips")
    await until(lambda: server.adapter.halted)
    estop.release(ws, by="person")
    await until(lambda: server.estop is None)
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert f"an unknown number on {DEV}" in text


async def test_a_pick_up_named_by_its_component_alone_is_noted_all_the_same(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    fake_robot.fail_commands["pickUpTip"] = "motor stall"
    assert await lab.refused("pick_up_tips", device="right", tips=["tips:col1"]) == "completion_unknown"
    assert ws.tips_left()["mounted"] == {DEV: "unknown"}  # 'right' is ot2.right: the robot may have taken them
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert f"an unknown number on {DEV}" in text and "no tips were on" not in text
    killed(lab)
  fake_robot.fail_commands.clear()
  runs = posts(fake_robot).count("/runs")
  async with open_lab(tmp_path, backend=None) as lab:  # after a crash: the note holds the next run back
    await go_ahead(lab)
    assert await lab.refused("load_layout", layout=OT2_LAYOUT) == "tips_unconfirmed"
  assert posts(fake_robot).count("/runs") == runs  # no new run, no home


async def test_a_stop_with_no_tips_on_still_waits_for_the_persons_word(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    server = lab.server.lab
    await load_verified(lab, layout=OT2_LAYOUT)
    estop.engage(ws, by="test")  # between calls, no tips on
    await until(lambda: server.adapter.halted)
    assert ws.tips_left() is None  # nothing was on to note
    estop.release(ws, by="person")
    await until(lambda: server.estop is None)
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)  # the halted run: only the person can say
    assert "[tips_unconfirmed]" in text and "no tips were on as far as this server knows" in text
    await confirm_pipettes_empty(lab)
    await load_verified(lab, layout=OT2_LAYOUT)
  assert ws.tips_left() is None


@pytest.mark.parametrize("why", ["the server shut down", "the device config changed"])
async def test_a_stop_nobody_saw_through_outlives_the_server_and_the_device(tmp_path, open_lab, fake_robot, why):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    server = lab.server.lab
    await load_verified(lab, layout=OT2_LAYOUT)
    estop.engage(ws, by="test")  # no tips on as far as this server knows
    await until(lambda: server.adapter.halted)
    estop.release(ws, by="person")
    await until(lambda: server.estop is None)
    if why == "the device config changed":  # to the simulation and back: neither confirmation is about the pipettes
      await lab.call("configure_devices", devices=[{"id": "ot2", "model": "opentrons.ot2", "backend": "sim"}])
      await go_ahead(lab)
      await lab.call("configure_devices", devices=robot_entry("127.0.0.1", fake_robot.port))
      await go_ahead(lab)
      assert await lab.refused("load_layout", layout=OT2_LAYOUT) == "tips_unconfirmed"
  note = ws.tips_left()
  assert note["mounted"] == {} and note["why"] == f"the emergency stop stopped the robot's run, then {why}"
  async with open_lab(tmp_path, backend=None) as lab:
    await go_ahead(lab)
    assert "Tips may still be on the pipettes" in (await lab.call("lab_overview"))["next"]
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
  assert "[tips_unconfirmed]" in text and "how many, and on which pipette, is unknown (the emergency stop" in text


@pytest.mark.parametrize("content", ['{"at": "2026-10-23T14:02:00", "mounted": {"ot2.right": 8', "[]"],
                         ids=["cut short", "not a note"])
async def test_a_note_that_cannot_be_read_still_holds_back_the_next_run(tmp_path, open_lab, fake_robot, content):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    ws.tips_left_path.write_text(content)  # a full disk, a sync tool, a hand edit
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[tips_unconfirmed]" in text and "exists but cannot be read" in text
    await confirm_pipettes_empty(lab)  # the person's word clears it
    assert ws.tips_left() is None
    await load_verified(lab, layout=OT2_LAYOUT)
    ws.tips_left_path.write_text(content)  # one that turns up during a run is left as it is
    assert await lab.refused("pick_up_tips", device=DEV, tips=["tips:A3"]) == "tip_count"
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col1"])
    assert ws.tips_left_path.read_text() == content


# -- the check and simulation ---------------------------------------------------------------------------

async def test_only_expect_none_checks_every_pipette(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    text = await lab.call_text("verify", check="tips_mounted")  # expect='mounted' needs its pipette
    assert "[device_required]" in text and "expect='none' without a device checks every pipette at once" in text
    assert await lab.refused("verify", check="tips_mounted", expect="mounted") == "device_required"


async def test_simulation_never_notes_or_holds_back_tips(tmp_path, open_lab):
  ws = Workspace(tmp_path)
  async with open_lab(tmp_path, "sim") as lab:
    give_reference(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device="starlet.pip")
    assert ws.tips_left() is None
    on = await lab.call("verify", check="tips_mounted", expect="none")  # the simulator judges every pipette
    assert on["verdict"] == "fail" and on["evidence"]["digital_twin"]["pipettes"] == {"starlet.pip": 8}
    assert on["evidence"]["sensors"]["agrees_with_twin"]
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "tips_mounted"  # the device's own refusal
    killed(lab)
  assert ws.tips_left() is None
  ws.mark_tips_left({"at": "2026-10-23T14:02:00", "why": "the server shut down", "device": "hamilton.starlet",
                     "run": "abc", "mounted": {"starlet.pip": 8}})  # as a robot run would leave it
  async with open_lab(tmp_path, "sim") as lab:
    assert "Tips may" not in (await lab.call("lab_overview"))["next"]
    await load_verified(lab, layout=STARLET_LAYOUT)  # never refused in simulation
    empty = await lab.call("verify", check="tips_mounted", expect="none")
    assert empty["verdict"] == "pass" and empty["simulated"]
