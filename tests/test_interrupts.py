"""What interrupts a call, server side: a client that stops waiting never cancels the device, the emergency
stop always does, and on real hardware an interrupted call's outcome is unknown, never a twin diff."""

import asyncio
import shutil
import threading
import time
from contextlib import asynccontextmanager

import anyio
import pytest
from mcp import Client

from ahc.devices.base import Adapter
from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT
from ahc.server import create_server
from ahc.workspace import estop
from conftest import LabClient, confirm_pipettes_empty, give_reference, load_verified, robot_lab

pytestmark = pytest.mark.anyio

DEV = "starlet.pip"
ASPIRATE = {"device": DEV, "targets": ["diluent"], "volumes": 50, "plan_step": 1}


def slowed(monkeypatch, adapter, seconds: float) -> asyncio.Event:
  """The adapter's aspirate takes `seconds` first, as a long move on a device would; the event marks its end."""
  real, finished = adapter.aspirate, asyncio.Event()

  async def aspirate(*args, **kwargs):
    await asyncio.sleep(seconds)
    note = await real(*args, **kwargs)
    finished.set()
    return note

  monkeypatch.setattr(adapter, "aspirate", aspirate)
  return finished


async def ready(lab):
  give_reference(lab)
  await load_verified(lab, layout=STARLET_LAYOUT)
  await lab.call("pick_up_tips", device=DEV)
  return lab.server.lab


async def until(condition, seconds: float = 5.0):
  with anyio.fail_after(seconds):
    while not condition():
      await anyio.sleep(0.05)


@asynccontextmanager
async def over_json_rpc(folder, backend="sim"):
  """A client speaking JSON-RPC to the server, as Claude Code does over stdio: a call it abandons
  sends notifications/cancelled, which cancels the server's handler for that request."""
  server = create_server(folder, backend)
  async with Client(server, mode="legacy") as client:
    yield LabClient(client, server)


# -- a client that stops waiting (Esc in Claude Code, a client timeout) ------------------------------

async def test_a_client_that_stops_waiting_never_cancels_the_device(tmp_path, monkeypatch):
  async with over_json_rpc(tmp_path) as lab:
    server = await ready(lab)
    finished = slowed(monkeypatch, server.adapter, 0.8)
    with anyio.move_on_after(0.2) as gave_up:  # a real MCP call, abandoned: the client sends notifications/cancelled
      await lab.client.call_tool("aspirate", ASPIRATE)
    assert gave_up.cancelled_caught and not finished.is_set()
    assert server.lock.locked() and server.current_action is not None  # the call runs on, still stoppable
    state = await lab.call("get_state")  # waits for the lock: the aspirate ends first
    assert finished.is_set() and server.current_action is None
    assert state["liquids_ul"]["diluent_trough"] == pytest.approx(100000 - 8 * 50)  # the twin agrees with the device
    entries = server.runlog.entries
  assert [e["status"] for e in entries if e.get("op") == "aspirate"] == ["ok"]  # its true outcome
  assert [e["call"] for e in entries if e.get("event") == "request_cancelled"] == ["aspirate"]


async def test_the_emergency_stop_still_interrupts_a_call_nobody_waits_for(tmp_path, monkeypatch):
  async with over_json_rpc(tmp_path) as lab:
    server = await ready(lab)
    slowed(monkeypatch, server.adapter, 5)
    with anyio.move_on_after(0.2):
      await lab.client.call_tool("aspirate", ASPIRATE)
    started = time.monotonic()
    estop.engage(server.ws, by="test")
    assert await lab.refused("pick_up_tips", device=DEV) == "emergency_stop"
    assert time.monotonic() - started < 2  # not the 5 s the command would have taken
    entries = server.runlog.entries
  assert [e["error"] for e in entries if e.get("op") == "aspirate"] == ["emergency_stop"]
  assert [e["interrupted"] for e in entries if e.get("type") == "estop"] == [True]


def slow_after_the_config_write(monkeypatch, server, seconds: float) -> asyncio.Event:
  """The call writes the config, then takes `seconds` more (binding a robot can); the event marks its end."""
  written, finished = [], asyncio.Event()
  write, refresh = server.ws.write_config, server.refresh

  def write_config(*args, **kwargs):
    write(*args, **kwargs)
    written.append(True)

  async def slow_refresh(write=False):
    if written and not finished.is_set():
      await asyncio.sleep(seconds)
      finished.set()
    return await refresh(write)

  monkeypatch.setattr(server.ws, "write_config", write_config)
  monkeypatch.setattr(server, "refresh", slow_refresh)
  return finished


STARLET_SIM = [{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}]


async def test_a_config_write_the_client_stops_waiting_for_is_still_recorded(tmp_path, monkeypatch):
  async with over_json_rpc(tmp_path, backend=None) as lab:
    server = lab.server.lab
    finished = slow_after_the_config_write(monkeypatch, server, 0.8)
    with anyio.move_on_after(0.3) as gave_up:
      await lab.client.call_tool("configure_devices", {"devices": STARLET_SIM})
    assert gave_up.cancelled_caught and not finished.is_set()
    summary = (await lab.call("lab_overview"))["config"]["confirm_summary"]  # waits for the lock: the call ends first
    assert finished.is_set()
    finished = slow_after_the_config_write(monkeypatch, server, 0.8)
    with anyio.move_on_after(0.3) as gave_up:
      await lab.client.call_tool("confirm_config", {"summary": summary})
    assert gave_up.cancelled_caught and not finished.is_set()
    config = (await lab.call("lab_overview"))["config"]
    assert finished.is_set() and config["status"] == "confirmed" and config["confirmed_by"] == "human"
    entries = server.runlog.entries if server.runlog else []
  assert [(e["op"], e["status"]) for e in entries if e.get("op") in ("configure_devices", "confirm_config")] == \
         [("configure_devices", "ok"), ("confirm_config", "ok")]  # their true outcomes
  assert [e["call"] for e in entries if e.get("event") == "request_cancelled"] == ["configure_devices", "confirm_config"]


# -- the emergency stop -----------------------------------------------------------------------------

async def test_on_real_hardware_an_interrupted_call_has_an_unknown_outcome(tmp_path, open_lab, monkeypatch):
  monkeypatch.setattr(Adapter, "simulated", property(lambda self: False))  # judged as on real hardware
  async with open_lab(tmp_path, backend=None) as lab:
    await lab.call("configure_devices", devices=[{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}])
    await lab.call("confirm_config", summary=(await lab.call("lab_overview"))["config"]["confirm_summary"])
    server = lab.server.lab
    await lab.call("load_layout", layout=STARLET_LAYOUT)
    check = await lab.call("verify", check="deck_matches_layout")
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    await lab.call("pick_up_tips", device=DEV)
    slowed(monkeypatch, server.adapter, 5)

    async def press_soon():
      await anyio.sleep(0.3)
      estop.engage(server.ws, by="test")

    async with anyio.create_task_group() as tg:
      tg.start_soon(press_soon)
      text = await lab.call_text("aspirate", **ASPIRATE)
    assert "outcome on the robot is unknown" in text and "tips may be left down in a well" in text
    assert "The outcome of aspirate on the robot is unknown" in (await lab.call("lab_overview"))["next"]
    estop.release(server.ws, by="person")
    await until(lambda: server.estop is None)  # the server's watcher sees the release
    overview = await lab.call("lab_overview")
    assert overview["next"].startswith("The outcome of aspirate on the robot is unknown")
    refused = [e for e in server.runlog.entries if e.get("op") == "aspirate"][-1]
  assert refused["error"] == "emergency_stop" and refused["partial"] == "unknown"  # never a twin diff


async def test_every_stop_record_carries_the_stops_id(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    server = await ready(lab)
    pressed = estop.engage(server.ws, by="test")
    assert await lab.refused("drop_tips", device=DEV) == "emergency_stop"
    estop.release(server.ws, by="person")
    await until(lambda: server.estop is None)
    entries = server.runlog.entries
  stops = [(e["state"], e["id"]) for e in entries if e.get("type") == "estop"]
  assert stops == [("engaged", pressed["id"]), ("released", pressed["id"])]


async def test_the_stop_reaches_the_device_even_when_the_run_log_cannot_be_written(tmp_path, open_lab, monkeypatch):
  async with open_lab(tmp_path, "sim") as lab:
    server = await ready(lab)
    halted = []

    async def halt():
      halted.append(time.monotonic())
      return False

    monkeypatch.setattr(server.adapter, "halt", halt)
    shutil.rmtree(tmp_path / ".ahc" / "runs")  # the person tidied the folder mid-session

    def full_disk(entry, defer=False):
      raise OSError("No space left on device")

    monkeypatch.setattr(server, "record", full_disk)
    estop.engage(server.ws, by="test")
    await until(lambda: halted)  # scheduled before anything is written
    assert server.estop is not None
    monkeypatch.undo()
    assert await lab.refused("pick_up_tips", device=DEV) == "emergency_stop"  # a coded refusal, not a crash
  assert (tmp_path / ".ahc" / "runs").is_dir()  # the run log made its folder again


async def test_a_server_with_nothing_bound_writes_nothing_for_a_stop(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as active, open_lab(tmp_path, backend=None) as idle:
    server = await ready(active)
    pressed = estop.engage(server.ws, by="test")
    await until(lambda: idle.server.lab.estop is not None and server.estop is not None)  # both latched it
    assert (await idle.call("lab_overview"))["emergency_stop"]["engaged"]
    estop.release(server.ws, by="person")
    await until(lambda: idle.server.lab.estop is None and server.estop is None)
  logs = sorted((tmp_path / ".ahc" / "runs").glob("*.jsonl"))
  assert logs == [server.runlog.path] and idle.server.lab.runlog is None  # only the active server's log
  assert pressed["id"] in logs[0].read_text()


async def test_the_stop_reaches_a_run_the_load_is_still_setting_up(tmp_path, open_lab, monkeypatch):
  async with open_lab(tmp_path, "sim") as lab:
    give_reference(lab)
    await lab.call("lab_overview")
    server = lab.server.lab
    adapter, homing, seen = server.adapter, threading.Event(), []

    async def load_layout(layout):  # as the OT-2's: setup() homes, in PyLabRobot's transport thread
      adapter._setting_up = "run-1"
      try:
        await asyncio.to_thread(homing.wait, 5)
      finally:
        adapter._setting_up = None
      raise AssertionError("the emergency stop ends this load")

    async def halt():  # contract C4: its first step finds the run being set up
      seen.append(adapter._setting_up)
      homing.set()  # the robot stops
      return seen[-1] is not None

    monkeypatch.setattr(adapter, "load_layout", load_layout)
    monkeypatch.setattr(adapter, "halt", halt)

    async def press_soon():
      await anyio.sleep(0.3)
      estop.engage(server.ws, by="test")

    async with anyio.create_task_group() as tg:
      tg.start_soon(press_soon)
      assert "[emergency_stop]" in await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    await until(lambda: any(str(e.get("event", "")).startswith("device_") for e in server.runlog.entries))
    events = [e["event"] for e in server.runlog.entries if str(e.get("event", "")).startswith("device_")]
  assert seen == ["run-1"] and events == ["device_halted"]


async def test_the_halt_is_logged_as_the_device_answered(tmp_path, open_lab, monkeypatch):
  async with open_lab(tmp_path, "sim") as lab:
    server = await ready(lab)

    async def failing():
      raise TimeoutError("no answer from the robot")

    monkeypatch.setattr(server.adapter, "halt", failing)
    estop.engage(server.ws, by="test")
    await until(lambda: any(e.get("event", "").startswith("device_halt") for e in server.runlog.entries))
    failed = [e for e in server.runlog.entries if e.get("event") == "device_halt_failed"][0]
  assert "may still be executing its last command" in failed["detail"]
  assert "the power switch is the immediate stop" in failed["detail"]


async def test_next_names_a_halt_in_progress_and_an_unknown_outcome_first(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    server = await ready(lab)
    server.adapter.halt_requested = True  # contract C4: the stop was sent, not yet confirmed
    text = (await lab.call("lab_overview"))["next"]
    assert "has not confirmed that its run stopped" in text and "power switch" in text
    server.adapter.halted = True
    assert (await lab.call("lab_overview"))["next"].startswith("The emergency stop stopped the robot's run")
    server.adapter.outcome_unknown = {"op": "dispense", "error": "TimeoutError", "at": "2026-10-22T12:00:00"}  # C2
    text = (await lab.call("lab_overview"))["next"]
  assert text.startswith("The outcome of dispense on the robot is unknown") and "tips and wells" in text
  assert "removes any tips" in text


# -- the emergency stop on the robot backend, end to end: server, OT-2 adapter, robot ----------------

def stops_of(bot, run_id: str) -> list[float]:
  """When the robot received a stop action for this run."""
  return [t for t, method, path, body in bot.log if method == "POST" and path == f"/runs/{run_id}/actions"
          and ((body or {}).get("data") or {}).get("actionType") == "stop"]


def motion_after(bot, since: float) -> list[tuple[float, str]]:
  """Requests that would move the robot (a home, a run command) that reached it after `since`."""
  return [(t, path if path == "/robot/home" else body["data"]["commandType"]) for t, method, path, body in bot.log
          if method == "POST" and t > since and (path == "/robot/home" or path.endswith("/commands"))]


def halt_events(server) -> list[str]:
  return [e["event"] for e in server.runlog.entries if str(e.get("event", "")).startswith("device_")]


async def press_when(server, seen):
  with anyio.fail_after(5):
    while not seen():
      await anyio.sleep(0.02)
  estop.engage(server.ws, by="test")


async def stays_stopped_until_reloaded_and_checked(lab, server, bot):
  """After the person's release: nothing moves until load_layout and the person's deck check."""
  assert await lab.refused("pick_up_tips", device="ot2.right", tips=["tips:A3:H3"]) == "emergency_stop"
  estop.release(server.ws, by="person")
  await until(lambda: server.estop is None)
  assert await lab.refused("pick_up_tips", device="ot2.right", tips=["tips:A3:H3"]) in ("device_halted", "gate_pending")
  bot.home_seconds, bot.durations = 0.0, {}
  # A new run forgets any tips on the pipettes: load_layout waits for the person's word that they are empty.
  assert await lab.refused("load_layout", layout=OT2_LAYOUT) == "tips_unconfirmed"
  await confirm_pipettes_empty(lab)
  await lab.call("load_layout", layout=OT2_LAYOUT)
  assert await lab.refused("pick_up_tips", device="ot2.right", tips=["tips:A3:H3"]) == "gate_pending"
  check = await lab.call("verify", check="deck_matches_layout")
  assert check["verdict"] == "pending"  # the person on site judges the deck
  await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
  await lab.call("pick_up_tips", device="ot2.right", tips=["tips:A3:H3"])


async def test_go_no_go_the_stop_during_the_setup_home_reaches_the_robot(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    server = lab.server.lab
    fake_robot.home_seconds = 1.5  # setup() homes last; PyLabRobot's transport waits for it
    homing = lambda: any(path == "/robot/home" for _, _, path, _ in fake_robot.log)
    async with anyio.create_task_group() as tg:
      tg.start_soon(press_when, server, homing)
      text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[emergency_stop]" in text and "its outcome on the robot is unknown" in text
    await until(lambda: halt_events(server))
    (run_id,) = fake_robot.runs  # the run the load was setting up
    stopped = stops_of(fake_robot, run_id)
    homed = [t for t, _, what, _ in fake_robot.log if what == "home finished"]
    assert stopped and homed and stopped[0] < homed[0]  # the stop reached the robot during the home
    assert motion_after(fake_robot, stopped[0]) == []
    assert halt_events(server) == ["device_halted"]
    refused = [e for e in server.runlog.entries if e.get("op") == "load_layout"][-1]
    assert refused["error"] == "emergency_stop" and refused["partial"] == "unknown"
    await stays_stopped_until_reloaded_and_checked(lab, server, fake_robot)


async def test_go_no_go_the_stop_during_a_slow_aspirate_reaches_the_robot(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    server = lab.server.lab
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device="ot2.right", tips=["tips:A1:H1"])
    run_id = server.adapter.device._run.id
    fake_robot.durations["aspirateInPlace"] = 1.5
    drawing = lambda: any(method == "EXEC-START" and what == "aspirateInPlace" for _, method, what, _ in fake_robot.log)
    async with anyio.create_task_group() as tg:
      tg.start_soon(press_when, server, drawing)
      text = await lab.call_text("aspirate", device="ot2.right", targets=["diluent"], volumes=100)
    assert "[emergency_stop]" in text and "its outcome on the robot is unknown" in text
    await until(lambda: halt_events(server))
    stopped = stops_of(fake_robot, run_id)
    assert stopped and fake_robot.runs[run_id]["status"] == "stopped"
    assert motion_after(fake_robot, stopped[0]) == []
    assert halt_events(server) == ["device_halted"]
    refused = [e for e in server.runlog.entries if e.get("op") == "aspirate"][-1]
    assert refused["error"] == "emergency_stop" and refused["partial"] == "unknown"
    await stays_stopped_until_reloaded_and_checked(lab, server, fake_robot)


def started(bot, command: str):
  return lambda: any(method == "EXEC-START" and what == command for _, method, what, _ in bot.log)


async def test_go_no_go_the_stop_during_the_first_command_after_the_home_reaches_the_robot(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    server = lab.server.lab
    await load_verified(lab, layout=OT2_LAYOUT)  # the home is setup()'s last step: the next command is the pick-up's
    run_id = server.adapter.device._run.id
    fake_robot.durations["loadLabware"] = 1.5  # the tip rack, registered in the run before the first pick-up
    async with anyio.create_task_group() as tg:
      tg.start_soon(press_when, server, started(fake_robot, "loadLabware"))
      assert "[emergency_stop]" in await lab.call_text("pick_up_tips", device="ot2.right", tips=["tips:A1:H1"])
    await until(lambda: halt_events(server))
    stopped = stops_of(fake_robot, run_id)
    ended = [t for t, method, what, _ in fake_robot.log if method == "EXEC-END" and what == "loadLabware"]
    assert stopped and ended and stopped[0] < ended[0]  # the stop reached the robot during the command
    assert motion_after(fake_robot, stopped[0]) == []  # no pickUpTip after it
    assert halt_events(server) == ["device_halted"]
    state = await lab.call("get_state")
    assert state["tips_mounted"]["ot2.right"] == "unknown" and state["tips_left"]["tips"] == "unknown"
    await stays_stopped_until_reloaded_and_checked(lab, server, fake_robot)


async def test_go_no_go_a_stop_pressed_again_after_a_release_reaches_the_robot_again(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    server = lab.server.lab
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device="ot2.right", tips=["tips:A1:H1"])
    run_id = server.adapter.device._run.id
    fake_robot.durations["aspirateInPlace"] = 1.5

    async def press_release_press():  # the second press lands while the robot still finishes the first one's command
      await press_when(server, started(fake_robot, "aspirateInPlace"))
      await until(lambda: server.estop is not None)
      estop.release(server.ws, by="person")
      await until(lambda: server.estop is None)
      estop.engage(server.ws, by="test again")

    async with anyio.create_task_group() as tg:
      tg.start_soon(press_release_press)
      assert "[emergency_stop]" in await lab.call_text("aspirate", device="ot2.right", targets=["diluent"], volumes=100)
    await until(lambda: len(halt_events(server)) == 2)
    stopped = stops_of(fake_robot, run_id)
    assert len(stopped) == 2 and motion_after(fake_robot, stopped[0]) == []
    assert halt_events(server) == ["device_halted", "device_halted"]
    assert [e["state"] for e in server.runlog.entries if e.get("type") == "estop"] == ["engaged", "released", "engaged"]
    await stays_stopped_until_reloaded_and_checked(lab, server, fake_robot)


async def test_a_stop_the_robot_never_confirmed_is_never_said_to_have_stopped_its_run(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    server = lab.server.lab
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device="ot2.right", tips=["tips:A1:H1"])
    fake_robot.durations["aspirateInPlace"], fake_robot.stop_status = 1.0, 500  # the robot does not take the stop
    async with anyio.create_task_group() as tg:
      tg.start_soon(press_when, server, started(fake_robot, "aspirateInPlace"))
      assert "[emergency_stop]" in await lab.call_text("aspirate", device="ot2.right", targets=["diluent"], volumes=100)
    await until(lambda: halt_events(server))
    assert halt_events(server) == ["device_halt_failed"]
    estop.release(server.ws, by="person")
    await until(lambda: server.estop is None)
    # What the agent and the person read next: the robot may still be moving, so never "stopped".
    for text in ((await lab.call("lab_overview"))["next"], (await lab.call("get_state"))["unknown"]):
      assert "stopped the robot's run" not in text
      assert "has not confirmed that its run stopped" in text and "power switch" in text


async def test_after_the_stop_cut_a_call_short_the_tips_and_the_touched_wells_are_unknown(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    server = lab.server.lab
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device="ot2.right", tips=["tips:A1:H1"])
    await lab.call("aspirate", device="ot2.right", targets=["diluent"], volumes=100)
    fake_robot.durations["dispenseInPlace"] = 1.5
    dispensing = lambda: any(method == "EXEC-START" and what == "dispenseInPlace" for _, method, what, _ in fake_robot.log)
    async with anyio.create_task_group() as tg:
      tg.start_soon(press_when, server, dispensing)
      assert "[emergency_stop]" in await lab.call_text("dispense", device="ot2.right", targets=["plate:col2"], volumes=100)
    state = await lab.call("get_state")  # a read: answered while the stop is engaged
    assert state["tips_mounted"]["ot2.right"] == "unknown"
    # The twin rolled the dispense back (empty wells), but the robot may have finished it.
    assert [state["liquids_ul"]["plate"][f"{r}2"] for r in "ABCDEFGH"] == ["unknown"] * 8
    assert [state["liquids_ul"]["reagents"][f"{r}1"] for r in "ABCDEFGH"] == [1700] * 8  # done before the stop
    assert "interrupted dispense" in state["unknown"] and "plate: column 2 (A2-H2)" in state["unknown"]
    estop.release(server.ws, by="person")
    await until(lambda: server.estop is None)
    # The twin's own check never passes a well the robot may have filled, and never offers its 0 uL as evidence.
    check = await lab.call("verify", check="volumes_match", expected={"plate:col2": 0, "reagents:col1": 1700})
    assert check["verdict"] == "fail" and "interrupted dispense" in check["note"]
    assert len(check["mismatches"]) == 8 and {m["twin"] for m in check["mismatches"]} == {"unknown"}
    rows = check["evidence"]["digital_twin"]["containers"]
    assert [rows[f"reagents_well_{r}1"]["matches"] for r in "ABCDEFGH"] == [True] * 8  # what the twin still knows
    present = await lab.call("verify", check="liquid_present", targets=["plate:col2"])
    twin = present["evidence"]["digital_twin"]
    assert present["verdict"] == "pending" and set(twin["volumes_ul"].values()) == {"unknown"}
    assert twin["as_expected"] is None and "interrupted dispense" in present["note"]


async def test_in_simulation_the_twin_still_knows_after_a_stop(tmp_path, open_lab, monkeypatch):
  async with open_lab(tmp_path, "sim") as lab:
    server = await ready(lab)
    slowed(monkeypatch, server.adapter, 5)
    async with anyio.create_task_group() as tg:
      tg.start_soon(press_when, server, lambda: server.current_action is not None)
      assert "[emergency_stop]" in await lab.call_text("aspirate", **ASPIRATE)
    state = await lab.call("get_state")
  assert state["tips_mounted"] == {"starlet.pip": 8} and "unknown" not in state
  assert state["liquids_ul"]["diluent_trough"] == 100000
