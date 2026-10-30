"""The real-hardware path of the capability layer and the OT-2 adapter, against tests/fake_robot.py (no
simulator, no robot): liquid heights, calls that fail after a command reached the robot, the emergency
stop, a run the robot no longer knows, the identity probe before confirmation, and the options check."""

import asyncio
import json
import socket

import anyio
import pytest

from ahc.core.errors import LabError
from ahc.devices.hamilton.starlet import STARletAdapter
from ahc.devices.opentrons.ot2 import OT2Adapter
from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT
from ahc.workspace import estop
from ahc.workspace.store import Workspace
from conftest import confirm_pipettes_empty, load_verified, robot_lab

pytestmark = pytest.mark.anyio

DEV = "ot2.right"


def descents(bot, since: int) -> list[float]:
  """z of each move into a well since the bot's move `since` (retracts go to the 120 mm traversal height)."""
  return [m["z"] for m in bot.moves[since:] if m["z"] < 100]


def closed_port() -> int:
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


async def loaded(lab, tips: bool = True):
  await load_verified(lab, layout=OT2_LAYOUT)
  if tips:
    await lab.call("pick_up_tips", device=DEV, tips=["tips:A1:H1"])
  return lab.server.lab.adapter


# -- liquid height ------------------------------------------------------------------------------

async def test_an_omitted_liquid_height_is_the_description_default_and_recorded(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = await loaded(lab)
    n = len(fake_robot.moves)
    drawn = await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
    assert drawn["effective"]["liquid_height_mm"] == 1.0  # ot2.md's default, not the well floor
    device = adapter.device
    well = adapter.layout.labware["reagents"].get_item("A1")
    floor = device._deck_to_robot_frame(well.get_location_wrt(device.deck, "c", "c", "cavity_bottom")).z
    assert descents(fake_robot, n) == [pytest.approx(floor + 1.0)]
    n = len(fake_robot.moves)
    await lab.call("dispense", device=DEV, targets=["diluent"], volumes=100, liquid_height=3)
    assert descents(fake_robot, n) == [pytest.approx(floor + 3.0)]
    mixed = await lab.call("mix", device=DEV, targets=["diluent"], volume=50, repetitions=2)
    assert mixed["effective"]["liquid_height_mm"] == 1.0
    logged = [e for e in lab.server.lab.runlog.entries if e.get("op") == "aspirate"][-1]
    assert logged["effective"]["liquid_height_mm"] == 1.0


async def test_the_starlet_records_the_height_it_uses(starlet):
  await load_verified(starlet, layout=STARLET_LAYOUT)
  await starlet.call("pick_up_tips", device="starlet.pip")
  drawn = await starlet.call("aspirate", device="starlet.pip", targets=["diluent"], volumes=50)
  assert drawn["effective"]["liquid_height_mm"] == 0.0  # the cavity bottom, PyLabRobot's STAR default


def test_the_ot2_adapter_never_sends_the_well_floor_by_default():
  with pytest.raises(LabError) as refused:
    OT2Adapter._height(None)
  assert refused.value.code == "liquid_height_required"
  assert OT2Adapter._height(0.0) == 0.0  # an explicit 0 is the caller's choice


# -- failures after a command reached the robot -------------------------------------------------

async def test_a_refusal_before_anything_was_sent_stays_a_refusal(tmp_path, open_lab, fake_robot, monkeypatch):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = await loaded(lab)

    async def refused_by_the_model(*args, **kwargs):
      raise ValueError("the device model says no")

    monkeypatch.setattr(adapter.device.right_pipette, "aspirate", refused_by_the_model)
    text = await lab.call_text("aspirate", device=DEV, targets=["diluent"], volumes=100)
    assert "[device_refused]" in text and "Nothing was sent to the robot" in text
    assert adapter.outcome_unknown is None
    monkeypatch.undo()
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)  # nothing latched


async def test_a_robot_error_after_a_command_makes_the_outcome_unknown(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = await loaded(lab)
    fake_robot.fail_commands["aspirateInPlace"] = "pressure fault"  # after the tips went down into the wells
    text = await lab.call_text("aspirate", device=DEV, targets=["diluent"], volumes=100)
    assert "[completion_unknown]" in text and "OpentronsCommandError" in text and "Do not retry" in text
    assert adapter.outcome_unknown["op"] == "aspirate" and "pressure fault" in adapter.outcome_unknown["error"]
    assert adapter.outcome_unknown["at"]
    entry = [e for e in lab.server.lab.runlog.entries if e.get("op") == "aspirate"][-1]
    assert entry["error"] == "completion_unknown" and entry["partial"] == "unknown"
    # What the robot says became of the call's commands, as evidence for the person.
    assert entry["robot_record"] == [{"command": "moveToCoordinates", "status": "succeeded"},
                                     {"command": "aspirateInPlace", "status": "failed", "error": "pressure fault"}]
    assert "aspirateInPlace failed (pressure fault)" in text
    # Every later motion call is refused, and logged, until a new layout load.
    assert await lab.refused("dispense", device=DEV, targets=["plate:A1:H1"], volumes=100) == "completion_unknown"
    assert await lab.refused("drop_tips", device=DEV) == "completion_unknown"
    assert [e["error"] for e in lab.server.lab.runlog.entries if e.get("op") in ("dispense", "drop_tips")] == \
           ["completion_unknown", "completion_unknown"]
    fake_robot.fail_commands.clear()
    assert await lab.refused("load_layout", layout=OT2_LAYOUT) == "tips_unconfirmed"  # the tips may still be on
    await confirm_pipettes_empty(lab)  # the person removed them
    await load_verified(lab, layout=OT2_LAYOUT)  # the person checked; a new run
    assert adapter.outcome_unknown is None
    await lab.call("pick_up_tips", device=DEV, tips=["tips:A2:H2"])
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)


async def test_after_an_unknown_outcome_the_tips_and_the_touched_wells_are_unknown(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await loaded(lab)
    assert (await lab.call("get_state"))["tips_mounted"] == {"ot2.right": 8, "ot2.left": 0}
    fake_robot.fail_commands["aspirateInPlace"] = "pressure fault"
    assert await lab.refused("aspirate", device=DEV, targets=["diluent"], volumes=100) == "completion_unknown"
    state = await lab.call("get_state")
    assert state["tips_mounted"] == {"ot2.right": "unknown", "ot2.left": "unknown"}  # never "0 tips" as if known
    assert [state["liquids_ul"]["reagents"][f"{r}1"] for r in "ABCDEFGH"] == ["unknown"] * 8
    assert state["liquids_ul"]["reagents"]["A2"] == 500  # a well the call did not touch: the twin still knows
    assert "aspirate" in state["unknown"] and "the liquid in reagents: column 1 (A1-H1)" in state["unknown"]
    assert "the person checks them on the robot" in state["unknown"]
    check = await lab.call("verify", check="tips_mounted", device=DEV, expect="none")
    twin = check["evidence"]["digital_twin"]
    assert check["verdict"] == "pending" and twin["tips_mounted"] == "unknown" and twin["as_expected"] is None
    assert "aspirate" in check["note"]
    logged = [e for e in lab.server.lab.runlog.entries if e.get("type") == "verification"][-1]
    assert logged["evidence"]["digital_twin"]["tips_mounted"] == "unknown"
    assert await lab.refused("load_layout", layout=OT2_LAYOUT) == "check_pending"  # nothing moves while it waits
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")  # the person: no tip on ot2.right
    fake_robot.fail_commands.clear()
    await confirm_pipettes_empty(lab)
    await load_verified(lab, layout=OT2_LAYOUT)  # the person reported; a new run
    state = await lab.call("get_state")
    assert state["tips_mounted"] == {"ot2.right": 0, "ot2.left": 0} and "unknown" not in state


async def test_a_timeout_or_a_lost_link_after_submission_is_unknown(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = await loaded(lab, tips=False)
    fake_robot.durations["pickUpTip"] = 1.5
    adapter.device._run.command_timeout = 0.3  # the poll deadline passes while the robot still works
    text = await lab.call_text("pick_up_tips", device=DEV, tips=["tips:A1:H1"])
    assert "[completion_unknown]" in text and "OpentronsCommandTimeout" in text
    assert "pickUpTip running" in text  # the robot was still at it when the poll gave up
    await anyio.sleep(1.5)
    fake_robot.durations.clear()
    await confirm_pipettes_empty(lab)  # the pick-up's outcome is unknown: the person checks the pipette
    adapter = await loaded(lab)
    fake_robot.poll_status = 500  # the command went out; the answer about it does not come back
    text = await lab.call_text("aspirate", device=DEV, targets=["diluent"], volumes=100)
    assert "[completion_unknown]" in text and adapter.outcome_unknown["op"] == "aspirate"
    assert "moveToCoordinates unknown: HTTPError" in text  # asked, but the robot's answer did not come back


async def test_simulated_backends_never_latch(starlet, monkeypatch):
  await load_verified(starlet, layout=STARLET_LAYOUT)
  await starlet.call("pick_up_tips", device="starlet.pip")
  adapter = starlet.server.lab.adapter
  original = adapter.aspirate

  async def sent_then_failed(*args):
    await original(*args)
    raise RuntimeError("the simulator gave up")

  monkeypatch.setattr(adapter, "aspirate", sent_then_failed)
  text = await starlet.call_text("aspirate", device="starlet.pip", targets=["diluent"], volumes=50)
  assert "[device_refused]" in text and "The simulator stopped it after" in text
  assert adapter.outcome_unknown is None
  state = await starlet.call("get_state")  # the simulator's own state: known
  assert state["tips_mounted"] == {"starlet.pip": 8} and "unknown" not in state
  assert state["liquids_ul"]["diluent_trough"] == pytest.approx(100000 - 8 * 50)
  monkeypatch.undo()
  await starlet.call("aspirate", device="starlet.pip", targets=["diluent"], volumes=50)


async def test_a_command_count_that_fails_never_hides_the_call(starlet, monkeypatch):
  await load_verified(starlet, layout=STARLET_LAYOUT)
  await starlet.call("pick_up_tips", device="starlet.pip")

  def count_fails():
    raise OSError("count unavailable")

  monkeypatch.setattr(starlet.server.lab.adapter, "command_count", count_fails)
  drawn = await starlet.call("aspirate", device="starlet.pip", targets=["diluent"], volumes=50)
  assert drawn["ok"] and drawn["commands_sent"] is None
  entry = [e for e in starlet.server.lab.runlog.entries if e.get("op") == "aspirate"][-1]
  assert entry["status"] == "ok" and "commands" not in entry
  assert (await starlet.call("get_run"))["liquid_handling_calls"] == 2


async def test_commands_are_counted_here_not_asked_of_the_robot(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await loaded(lab)
    drawn = await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  assert drawn["commands_sent"] == 4  # move down, aspirate, read the position, move up
  assert not any(method == "GET" and "pageLength" in path for _, method, path, _ in fake_robot.log)


# -- tips, targets and the liquid surface on the 8-channel head ----------------------------------

async def test_the_eight_channel_head_takes_exactly_one_full_column(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await loaded(lab, tips=False)
    text = await lab.call_text("pick_up_tips", device=DEV, tips=["tips:A1:H8"])
    assert "[tip_count]" in text and "64 tip spots for the 8-channel ot2.right: pass one column, e.g. tips:col1 or tips:A1:H1" in text
    assert await lab.refused("pick_up_tips", device=DEV, tips=["tips:E1:H1"]) == "tip_count"  # half a column
    assert await lab.refused("pick_up_tips", device=DEV, tips=["tips:A1:A8"]) == "tips_not_a_column"
    text = await lab.call_text("pick_up_tips", device="ot2.left", tips=["tips:A1:H1"])
    assert "[tip_count]" in text and "8 tip spots for the single-channel ot2.left" in text
    assert not any(e[2].endswith("/commands") and e[3]["data"]["commandType"] == "pickUpTip"
                   for e in fake_robot.log if e[1] == "POST" and isinstance(e[3], dict) and "data" in e[3])
    picked = await lab.call("pick_up_tips", device=DEV, tips=["tips:col3"])
    assert picked["effective"]["where"] == "tips: column 3 (A3-H3)"
    drawn = await lab.call("aspirate", device=DEV, targets=["reagents:col1"], volumes=100)
    assert drawn["effective"]["where"] == "reagents: column 1 (A1-H1)"


async def test_the_default_height_is_refused_where_too_little_liquid_is_left(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await loaded(lab)
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
    await lab.call("dispense", device=DEV, targets=["plate:col1"], volumes=100)
    # 40 uL would be left in the flat wells: 1.3 mm deep, so 1 mm above the floor is not under the surface.
    text = await lab.call_text("aspirate", device=DEV, targets=["plate:col1"], volumes=60)
    assert "[above_liquid]" in text and "Use at most 0.7 mm" in text  # 0.78 mm, rounded down
    await lab.call("aspirate", device=DEV, targets=["plate:col1"], volumes=50)  # 50 left: 1.7 mm deep
    assert await lab.refused("dispense", device=DEV, targets=["plate:col2"], volumes=60) == "insufficient_in_tips"
    await lab.call("dispense", device=DEV, targets=["plate:col2"], volumes=50)
    # Emptying a well: the hint never sends the tips to the well floor (0 mm).
    text = await lab.call_text("aspirate", device=DEV, targets=["plate:col2"], volumes=50)
    assert "[above_liquid]" in text and "Draw less" in text and "0.0 mm" not in text.partition("Hint:")[2]


# -- the emergency stop -------------------------------------------------------------------------

async def test_halt_stops_the_run_and_motion_stays_refused(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = await loaded(lab)
    run_id = adapter.device._run.id
    assert await adapter.halt() is True
    assert adapter.halted and adapter.halt_requested and fake_robot.runs[run_id]["status"] == "stopped"
    assert await lab.refused("drop_tips", device=DEV) == "device_halted"
    assert await lab.refused("load_layout", layout=OT2_LAYOUT) == "tips_unconfirmed"  # the robot forgot the tips
    await confirm_pipettes_empty(lab)
    await load_verified(lab, layout=OT2_LAYOUT)  # a new run; the old one is let go, not stopped again
    assert not adapter.halted and not adapter.halt_requested
    await lab.call("pick_up_tips", device=DEV, tips=["tips:A2:H2"])


async def test_a_halt_the_robot_does_not_confirm_keeps_motion_refused(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = await loaded(lab)
    fake_robot.stop_status = 500
    with pytest.raises(Exception):
      await adapter.halt()
    assert adapter.halt_requested and not adapter.halted
    text = await lab.call_text("drop_tips", device=DEV)
    assert "[device_halted]" in text and "power switch" in text
    fake_robot.stop_status = None
    await confirm_pipettes_empty(lab)
    await load_verified(lab, layout=OT2_LAYOUT)  # the old run is stopped first, then a new one starts
    await lab.call("pick_up_tips", device=DEV, tips=["tips:A2:H2"])


async def test_halt_reaches_a_run_still_being_set_up(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = lab.server.lab.adapter
    fake_robot.home_seconds = 2.0  # setup() homes the robot last; PyLabRobot's transport waits for it
    load = asyncio.ensure_future(adapter.load_layout(OT2_LAYOUT))
    with anyio.fail_after(5):
      while not any(path == "/robot/home" for _, _, path, _ in fake_robot.log):
        await anyio.sleep(0.02)
    assert await adapter.halt() is True
    stopped = [t for t, method, path, _ in fake_robot.log if method == "POST" and path.endswith("/actions")]
    homed = [t for t, method, path, _ in fake_robot.log if path == "home finished"]
    assert stopped and not homed  # the stop reached the robot during the home, not after it
    with pytest.raises(LabError) as refused:
      await load
    assert refused.value.code == "device_halted" and adapter.device is None and adapter.halted


async def test_a_halt_confirmed_after_a_reload_leaves_the_new_run_alone(tmp_path, open_lab, fake_robot):
  import threading

  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = await loaded(lab, tips=False)
    old = adapter.device
    slow, stop_run = threading.Event(), adapter._stop_run

    def slow_to_confirm(run_id, timeout=30.0):  # the robot finishes its command before it reports stopped
      slow.wait(5)
      return stop_run(run_id, timeout)

    adapter._stop_run = slow_to_confirm
    halt = asyncio.ensure_future(adapter.halt())
    await anyio.sleep(0.1)
    assert adapter.halt_requested and not adapter.halted
    await adapter.load_layout(OT2_LAYOUT)  # meanwhile the person released the stop: a new run
    slow.set()
    assert await halt is True  # the old run did stop
    assert adapter.device is not old and not adapter.halted and not adapter.halt_requested
    adapter.require_motion()  # the new run takes commands


async def test_the_emergency_stop_during_a_layout_load_reaches_the_robot(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    fake_robot.home_seconds = 2.0
    async with anyio.create_task_group() as tg:
      async def press_during_the_home():
        with anyio.fail_after(5):
          while not any(path == "/robot/home" for _, _, path, _ in fake_robot.log):
            await anyio.sleep(0.02)
        estop.engage(Workspace(tmp_path), by="test")
      tg.start_soon(press_during_the_home)
      text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[emergency_stop]" in text
    with anyio.fail_after(5):
      while not any(e.get("event") == "device_halted" for e in lab.server.lab.runlog.entries):
        await anyio.sleep(0.05)
  stopped = [t for t, method, path, _ in fake_robot.log if method == "POST" and path.endswith("/actions")]
  homed = [t for t, method, path, _ in fake_robot.log if path == "home finished"]
  assert stopped and homed and stopped[0] < homed[0]


# -- a run the robot no longer knows ------------------------------------------------------------

async def test_a_run_no_longer_current_counts_as_stopped(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = await loaded(lab)
    fake_robot.reboot()  # the power switch: the robot forgets the run and the tips
    assert await lab.refused("drop_tips", device=DEV) == "completion_unknown"  # answered 409: what is left?
    await confirm_pipettes_empty(lab)
    await load_verified(lab, layout=OT2_LAYOUT)  # the old run is let go instead of failing every reload
    await lab.call("pick_up_tips", device=DEV, tips=["tips:A2:H2"])
    fake_robot.reboot()
    assert await adapter.halt() is True  # the emergency stop has nothing left to stop there


async def test_a_run_another_client_started_is_never_reported_as_halted(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = await loaded(lab)
    theirs = fake_robot.other_client_run()  # the Opentrons App: AHC's idle run is cleared, theirs runs
    estop.engage(Workspace(tmp_path), by="test")
    entries = lab.server.lab.runlog.entries
    with anyio.fail_after(5):
      while not any(str(e.get("event", "")).startswith("device_") for e in entries):
        await anyio.sleep(0.05)
    (halt,) = [e for e in entries if str(e.get("event", "")).startswith("device_")]
    assert halt["event"] == "device_halt_failed" and "[other_run_active]" in halt["detail"]
    assert f"run {theirs} (status running)" in halt["detail"] and "AHC did not stop that run" in halt["detail"]
    assert "power switch" in halt["detail"] and ".." not in halt["detail"]
    assert adapter.halt_requested and not adapter.halted  # motion stays refused
    assert not any(path == f"/runs/{theirs}/actions" for _, _, path, _ in fake_robot.log)  # never stopped by AHC
    assert fake_robot.runs[theirs]["status"] == "running"
    unknown = (await lab.call("get_state"))["unknown"]  # what the agent reads: the robot may be moving, never "stopped"
    assert "stopped the robot's run" not in unknown
    assert "has not confirmed that its run stopped" in unknown and "power switch" in unknown


async def test_shutdown_after_a_restart_does_not_fail(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = await loaded(lab, tips=False)
    fake_robot.reboot()
    await adapter.stop()
    assert adapter.device is None


async def test_a_run_that_may_still_be_active_is_kept_for_a_retry(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    adapter = await loaded(lab, tips=False)
    fake_robot.stop_status = 500
    with pytest.raises(Exception):
      await adapter.stop()
    assert adapter.device is not None  # not let go: the run may still run
    fake_robot.stop_status = None
    await adapter.stop()
    assert adapter.device is None


# -- who answers, before the person confirms -----------------------------------------------------

def test_the_identity_probe_reads_the_robot_without_moving_it(fake_robot):
  found = OT2Adapter.probe_identity("robot", {"host": "127.0.0.1", "port": fake_robot.port})
  assert found["name"] == "fake-ot2" and found["robot_serial"] == "OT2FAKE" and found["robot_model"] == "OT-2 Standard"
  assert {"api_version", "fw_version", "system_version"} <= set(found)
  assert found["pipettes"]["right"] == {"name": "p300_multi_gen2", "model": "p300_multi_v2.1", "serial": "R1"}
  assert found["mounts"] == {"ok": True, "problems": []}
  assert not any(method == "POST" for _, method, _, _ in fake_robot.log)  # no run, no motion
  assert OT2Adapter.probe_identity("sim", {}) == {}
  assert STARletAdapter.probe_identity("sim", {}) == {}


def test_the_probe_reports_mounts_that_differ(fake_robot):
  fake_robot.pipettes["right"] = {"name": "p300_single_gen2", "model": "p300_single_v2.1", "id": "R2"}
  fake_robot.pipettes["left"] = None
  fake_robot.instruments_status = 404  # older robot software: the pipettes endpoint answers instead
  found = OT2Adapter.probe_identity("robot", {"host": "127.0.0.1", "port": fake_robot.port})
  assert found["pipettes"]["left"] is None and found["pipettes"]["right"]["name"] == "p300_single_gen2"
  assert not found["mounts"]["ok"]
  assert any("right mount has 'p300_single_gen2'" in p for p in found["mounts"]["problems"])
  assert any("left mount is empty" in p for p in found["mounts"]["problems"])


def test_an_instruments_list_without_pipettes_falls_back_to_the_mounts(fake_robot):
  fake_robot.instruments_listed = False  # the robot-server answers, but its OT-2 list is empty
  found = OT2Adapter.probe_identity("robot", {"host": "127.0.0.1", "port": fake_robot.port})
  assert found["pipettes"]["left"]["name"] == "p20_single_gen2" and found["mounts"]["ok"]


def test_nothing_answering_is_refused():
  with pytest.raises(LabError) as refused:
    OT2Adapter.probe_identity("robot", {"host": "127.0.0.1", "port": closed_port()})
  assert refused.value.code == "backend_unreachable"


async def test_wrong_mounts_are_refused_before_the_robot_homes(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    fake_robot.pipettes["right"] = {"name": "p300_single_gen2", "model": "p300_single_v2.1", "id": "R2"}
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
  assert "[mount_mismatch]" in text and "nothing was moved" in text
  assert not any(method == "POST" for _, method, _, _ in fake_robot.log)  # no run created, no home


# -- the options of a device entry ---------------------------------------------------------------

OPTION_REFUSALS = [
  (OT2Adapter, "robot", {}, "robot_host_required"),
  (OT2Adapter, "robot", {"host": "http://10.0.0.5"}, "bad_option"),
  (OT2Adapter, "robot", {"host": "10.0.0.5", "port": 0}, "bad_option"),
  (OT2Adapter, "robot", {"host": "10.0.0.5", "port": "31950"}, "bad_option"),
  (OT2Adapter, "robot", {"host": "10.0.0.5", "robot_name": ""}, "bad_option"),
  (OT2Adapter, "robot", {"host": "10.0.0.5", "token": "x"}, "bad_option"),
  (OT2Adapter, "sim", {"port": 31950}, "sim_options_refused"),
  (OT2Adapter, "sim", {"robot_name": "lab-ot2"}, "sim_options_refused"),
  (OT2Adapter, "sim", {"speed": 2}, "bad_option"),
  (STARletAdapter, "usb", {}, "backend_not_implemented"),
  (STARletAdapter, "sim", {"host": "10.0.0.5"}, "sim_options_refused"),
]


@pytest.mark.parametrize("adapter,backend,options,code", OPTION_REFUSALS,
                         ids=[f"{a.__name__}-{b}-{sorted(o)}" for a, b, o, _ in OPTION_REFUSALS])
def test_an_entry_the_adapter_cannot_run_is_refused(adapter, backend, options, code):
  with pytest.raises(LabError) as refused:
    adapter.check_options(backend, options)
  assert refused.value.code == code


def test_entries_the_adapters_can_run():
  OT2Adapter.check_options("robot", {"host": "192.168.1.20"})
  OT2Adapter.check_options("robot", {"host": "ot2-lab.local", "port": 31950, "robot_name": "lab-ot2"})
  OT2Adapter.check_options("sim", {})
  STARletAdapter.check_options("sim", {})


# -- the event loop and the tip-rack definition ---------------------------------------------------

async def test_slow_robot_requests_never_block_the_event_loop(tmp_path, open_lab, fake_robot, monkeypatch):
  import time

  from ahc.devices.opentrons import ot2 as ot2_module

  ensure = ot2_module.ensure_tip_rack_definitions

  def slow_definitions(types):  # a definition download on a slow network
    time.sleep(0.6)
    ensure(types)

  monkeypatch.setattr(ot2_module, "ensure_tip_rack_definitions", slow_definitions)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    fake_robot.health_seconds = 0.6  # the robot-server answers slowly
    ticks = []

    async def tick():
      while True:
        ticks.append(time.monotonic())
        await asyncio.sleep(0.02)

    ticker = asyncio.ensure_future(tick())
    try:
      await lab.server.lab.adapter.load_layout(OT2_LAYOUT)
    finally:
      ticker.cancel()
  gaps = [b - a for a, b in zip(ticks, ticks[1:])]
  assert len(ticks) > 50 and max(gaps) < 0.3  # the emergency stop watcher would have run all along


def _definition(marker: str) -> bytes:
  return json.dumps({"metadata": {"displayCategory": "tipRack"}, "marker": marker}).encode()


@pytest.fixture
def definitions(tmp_path, monkeypatch):
  """Where PyLabRobot reads Opentrons definitions, moved into the test's folder; no simulator installed."""
  from ahc.devices.opentrons import brand

  cache = tmp_path / "cache"
  cache.mkdir()
  monkeypatch.setattr(brand, "definition_cache", lambda name: cache / f"{name}.json")
  monkeypatch.setenv("AHC_HOME", str(tmp_path / "ahc-home"))
  return cache


def test_a_tip_rack_definition_comes_from_the_simulator_checkout(tmp_path, definitions, monkeypatch):
  from ahc.devices.opentrons import brand
  from ahc.devices.opentrons.sim import robot_server

  checkout = tmp_path / "opentrons-ot2"
  (checkout / "robot-server" / "robot_server").mkdir(parents=True)
  (checkout / "robot-server" / "robot_server" / "app.py").write_text("")
  (checkout / "robot-server" / ".venv" / "bin").mkdir(parents=True)
  (checkout / "robot-server" / ".venv" / "bin" / "uvicorn").write_text("")
  local = checkout / "shared-data" / "labware" / "definitions" / "2" / "opentrons_96_tiprack_300ul" / "1.json"
  local.parent.mkdir(parents=True)
  local.write_bytes(_definition("from the checkout"))
  robot_server.setup(checkout, out=lambda _: None)

  def no_download(url, timeout):
    raise AssertionError("downloaded although the checkout has it")

  monkeypatch.setattr(brand, "_download", no_download)
  brand.ensure_tip_rack_definitions({"opentrons_96_tiprack_300ul", "Cor_96_wellplate_360ul_Fb"})
  assert json.loads((definitions / "opentrons_96_tiprack_300ul.json").read_text())["marker"] == "from the checkout"
  assert not (definitions / "Cor_96_wellplate_360ul_Fb.json").exists()  # built into PyLabRobot


def test_otherwise_it_is_downloaded_with_a_timeout(definitions, monkeypatch):
  import urllib.error

  from ahc.devices.opentrons import brand

  asked = []

  def download(url, timeout):
    asked.append((url, timeout))
    return _definition("downloaded")

  monkeypatch.setattr(brand, "_download", download)
  (definitions / "opentrons_96_tiprack_20ul.json").write_text("{")  # a download that broke off earlier
  brand.ensure_tip_rack_definitions({"opentrons_96_tiprack_20ul"})
  assert asked == [(brand.DEFINITION_URL.format(name="opentrons_96_tiprack_20ul"), brand.DOWNLOAD_TIMEOUT_S)]
  assert json.loads((definitions / "opentrons_96_tiprack_20ul.json").read_text())["marker"] == "downloaded"
  brand.ensure_tip_rack_definitions({"opentrons_96_tiprack_20ul"})  # there now: not asked again
  assert len(asked) == 1

  def offline(url, timeout):
    raise urllib.error.URLError("nodename nor servname provided")

  monkeypatch.setattr(brand, "_download", offline)
  with pytest.raises(LabError) as refused:
    brand.ensure_tip_rack_definitions({"opentrons_96_tiprack_300ul"})
  assert refused.value.code == "labware_definition_unavailable" and "simulator" in refused.value.hint


# -- the test run itself --------------------------------------------------------------------------

def test_a_named_test_simulator_that_does_not_answer_fails_the_run(monkeypatch):
  import conftest

  monkeypatch.setattr(conftest, "OT2_PORT", closed_port())
  with pytest.raises(pytest.fail.Exception, match="no OT-2 simulator answers"):
    conftest.robot_server_up.__wrapped__()
  monkeypatch.setattr(conftest, "OT2_PORT", None)
  assert conftest.robot_server_up.__wrapped__() is False  # not named: skipped, as before
