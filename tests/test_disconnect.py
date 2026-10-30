"""The server's life: disconnect (let go of the robot, keep the session), SIGTERM and SIGINT, one server per robot,
and a robot that moved to another address. tests/fake_robot.py stands in for the robot; nothing leaves 127.0.0.1."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

import ahc.server as server_module
from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT
from ahc.workspace.robot_lock import RobotLock
from ahc.workspace.store import Workspace
from conftest import (confirm_as_person, confirm_pipettes_empty, give_reference, load_verified, robot_entry,
                      robot_lab)
from fake_robot import FakeRobot

pytestmark = pytest.mark.anyio

DEV = "ot2.right"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
  monkeypatch.setenv("AHC_HOME", str(tmp_path / "ahc-home"))
  monkeypatch.delenv("AHC_BACKEND", raising=False)


def posts(bot, suffix: str = "") -> list[str]:
  return [path for _, method, path, _ in bot.log if method == "POST" and path.endswith(suffix)]


def free(robot: str) -> bool:
  """Nobody holds the robot's lock (taken and let go again here)."""
  lock = RobotLock(robot)
  if lock.acquire({}) is not None:
    return False
  lock.release()
  return True


# -- disconnect -----------------------------------------------------------------------------------------

async def test_disconnect_ends_the_robots_run_and_keeps_the_session(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    run = fake_robot.current
    assert not free(fake_robot.name)  # a run is open: the robot is held
    tools = {t.name: t for t in (await lab.client.list_tools()).tools}
    assert tools["disconnect"].annotations.read_only_hint is False and "Not read-only" in tools["disconnect"].description

    result = await lab.call("disconnect")
    assert set(result) == {"disconnected", "run_ended_confirmed", "tips_left", "next"}
    assert result["disconnected"] is True and result["run_ended_confirmed"] is True and result["tips_left"] == {}
    assert fake_robot.runs[run]["status"] == "stopped"  # the robot's own word, asked for after the stop
    assert "The robot reports its run ended" in result["next"] and "dashboard" in result["next"]
    assert "confirm_config" in result["next"] and "load_layout" in result["next"]
    assert free(fake_robot.name)  # and the robot is free for another session

    # The session stays: it reads, and the run log has the disconnect.
    server = lab.server.lab
    overview = await lab.call("lab_overview")
    assert overview["session"]["go_ahead"] == "needed" and "disconnected" in overview["session"]["why"]
    [logged] = [e for e in server.runlog.entries if e.get("op") == "disconnect"]
    assert logged["status"] == "ok" and logged["effective"]["run_ended_confirmed"] is True
    assert (await lab.call("get_run"))["refused"] == {}  # the run log reads on

    # Disconnecting again changes nothing.
    sent = len(posts(fake_robot))
    again = await lab.call("disconnect")
    assert again["disconnected"] is True and "Already disconnected" in again["next"] and len(posts(fake_robot)) == sent


async def test_nothing_moves_after_disconnect_until_the_go_ahead_and_a_load(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("disconnect")
    runs = len(posts(fake_robot, "/runs"))
    for tool, args in (("pick_up_tips", {"device": DEV}), ("drop_tips", {"device": DEV}),
                       ("aspirate", {"device": DEV, "targets": ["diluent"], "volumes": 100})):
      text = await lab.call_text(tool, **args)
      assert "[disconnected]" in text and "confirm_config" in text.partition("Hint:")[2], tool
    # load_layout is the way back, with the person's go-ahead first.
    assert await lab.refused("load_layout", layout=OT2_LAYOUT) == "session_unconfirmed"
    assert len(posts(fake_robot, "/runs")) == runs  # no new run, no home
    await confirm_as_person(lab)
    assert (await lab.call("lab_overview"))["session"]["go_ahead"] == "given"
    assert await lab.refused("pick_up_tips", device=DEV) == "disconnected"  # a go-ahead is not a layout
    await load_verified(lab, layout=OT2_LAYOUT)
    assert len(posts(fake_robot, "/runs")) == runs + 1
    await lab.call("pick_up_tips", device=DEV)  # connected again


async def test_tips_still_on_are_noted_when_disconnecting(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    run = lab.server.lab.adapter.device._run.id
    await lab.call("pick_up_tips", device=DEV)
    result = await lab.call("disconnect")
    assert result["tips_left"] == {DEV: 8} and result["run_ended_confirmed"] is True
    note = ws.tips_left()  # as a restart notes them
    assert note["mounted"] == {DEV: 8} and note["run"] == run and "disconnected" in note["why"]
    assert "Tips may still be on the pipettes" in result["next"]
    await confirm_as_person(lab)
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)  # the next load asks the person
    assert "[tips_unconfirmed]" in text and f"8 on {DEV}" in text and "disconnected" in text
    await confirm_pipettes_empty(lab)
    await load_verified(lab, layout=OT2_LAYOUT)
    assert ws.tips_left() is None


async def test_a_robot_that_does_not_confirm_is_reported_not_assumed(tmp_path, open_lab, fake_robot, monkeypatch):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    adapter = lab.server.lab.adapter

    async def silent(run_id):
      return False  # the robot-server does not say the run ended

    monkeypatch.setattr(type(adapter), "run_ended", lambda self, run_id: silent(run_id))
    result = await lab.call("disconnect")
    assert result["disconnected"] is True and result["run_ended_confirmed"] is False
    assert "NOT confirmed" in result["next"] and "power switch" in result["next"]


async def test_disconnect_in_simulation_does_the_same(starlet):
  give_reference(starlet)
  await load_verified(starlet, layout=STARLET_LAYOUT)
  await starlet.call("pick_up_tips", device="starlet.pip")
  result = await starlet.call("disconnect")
  assert result["disconnected"] is True and result["run_ended_confirmed"] is None  # a simulated STAR keeps no run
  assert result["tips_left"] == {"starlet.pip": 8}
  assert "No run was open" in result["next"]
  assert Workspace(starlet.server.lab.ws.root).tips_left() is None  # a simulation forgets nothing real
  assert await starlet.refused("aspirate", device="starlet.pip", targets=["diluent"], volumes=100) == "disconnected"
  assert await starlet.refused("load_layout", layout=STARLET_LAYOUT) == "session_unconfirmed"  # forced simulation too
  await confirm_as_person(starlet)
  await load_verified(starlet, layout=STARLET_LAYOUT)
  await starlet.call("pick_up_tips", device="starlet.pip")


async def test_disconnect_against_the_ot2_simulator(ot2):
  await load_verified(ot2, layout=OT2_LAYOUT)
  result = await ot2.call("disconnect")
  assert result["disconnected"] is True and result["run_ended_confirmed"] is True
  assert await ot2.refused("pick_up_tips", device=DEV) == "disconnected"


async def test_disconnect_is_refused_while_the_emergency_stop_is_engaged(tmp_path, open_lab, fake_robot):
  from ahc.workspace import estop

  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    estop.engage(lab.server.lab.ws, by="test")
    assert await lab.refused("disconnect") == "emergency_stop"


# -- one server per robot -------------------------------------------------------------------------------

async def test_a_second_server_cannot_load_while_the_first_holds_the_robot(tmp_path, open_lab, fake_robot, monkeypatch):
  first_folder, second_folder = tmp_path / "first", tmp_path / "second"
  first_folder.mkdir()
  second_folder.mkdir()
  monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-abc")
  lock_file = tmp_path / "ahc-home" / "locks" / f"{fake_robot.name}.lock"
  async with robot_lab(open_lab, first_folder, fake_robot) as first:
    assert not lock_file.exists()  # reading and configuring take nothing: only a run does
    await load_verified(first, layout=OT2_LAYOUT)
    holder = json.loads(lock_file.read_text())
    assert holder["pid"] == os.getpid() and holder["workspace"] == str(first_folder.resolve())
    assert holder["claude_session"] == "session-abc" and holder["robot"] == fake_robot.name and holder["session_started"]
    async with robot_lab(open_lab, second_folder, fake_robot) as second:
      runs = len(posts(fake_robot, "/runs"))
      text = await second.call_text("load_layout", layout=OT2_LAYOUT)
      assert "[robot_in_use]" in text and f"pid {os.getpid()}" in text and str(first_folder.resolve()) in text
      assert f"session started {holder['session_started'].replace('T', ' ')}" in text and "session-abc" in text
      hint = text.partition("Hint:")[2]
      assert "must disconnect (or end)" in hint and "person decides which session drives the robot" in hint
      assert len(posts(fake_robot, "/runs")) == runs  # nothing reached the robot
      assert (await second.call("lab_overview"))["session"]["go_ahead"] == "given"  # an idle session is not in the way
      # Once the first lets go, the second drives it, and the first is the one refused.
      await first.call("disconnect")
      assert not lock_file.read_text().strip()
      await load_verified(second, layout=OT2_LAYOUT)
      await confirm_as_person(first)
      assert await first.refused("load_layout", layout=OT2_LAYOUT) == "robot_in_use"
  assert free(fake_robot.name)  # the second server ended: the robot is free


async def test_a_servers_end_frees_the_robot(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    assert not free(fake_robot.name)
  assert free(fake_robot.name)


async def test_a_refused_load_does_not_hold_the_robot(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    assert await lab.refused("load_layout", layout={"slots": {"99": {"name": "x", "type": "opentrons_96_tiprack_300ul"}}}) == "bad_slot"
    assert free(fake_robot.name)  # no run was started


async def test_a_new_config_frees_the_robot_it_leaves(tmp_path, open_lab, fake_robot):
  other = FakeRobot(name="other-ot2")
  try:
    async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
      await load_verified(lab, layout=OT2_LAYOUT)
      assert not free(fake_robot.name)
      await lab.call("configure_devices", devices=robot_entry("127.0.0.1", other.port, robot_name=other.name))
      await confirm_as_person(lab)  # the config names another robot now: the device is dropped
      assert free(fake_robot.name) and free(other.name)
      assert fake_robot.runs[fake_robot.current]["status"] == "stopped"
  finally:
    other.close()


async def test_simulation_takes_no_lock(starlet, tmp_path):
  give_reference(starlet)
  await load_verified(starlet, layout=STARLET_LAYOUT)
  assert not (tmp_path / "ahc-home" / "locks").exists()


def test_the_lock_frees_itself_when_its_process_dies(tmp_path):
  code = ("import sys, time; from ahc.workspace.robot_lock import RobotLock; "
          "l = RobotLock('dying-ot2'); assert l.acquire({'workspace': '/somewhere'}) is None; print('held', flush=True); "
          "time.sleep(60)")
  proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
  try:
    assert proc.stdout.readline().strip() == "held"
    mine = RobotLock("dying-ot2")
    holder = mine.acquire({})
    assert holder["pid"] == proc.pid and holder["workspace"] == "/somewhere"
    proc.kill()  # no release, no shutdown: SIGKILL
    proc.wait(timeout=10)
    assert mine.acquire({}) is None  # flock went with the process
    mine.release()
  finally:
    proc.kill()


async def test_a_lock_a_dead_process_left_does_not_hold_a_server(tmp_path, open_lab, fake_robot):
  code = ("from ahc.workspace.robot_lock import RobotLock; import time; l = RobotLock(%r); "
          "assert l.acquire({'workspace': '/elsewhere'}) is None; print('held', flush=True); time.sleep(60)" % fake_robot.name)
  proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
  try:
    assert proc.stdout.readline().strip() == "held"
    async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
      text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
      assert "[robot_in_use]" in text and f"pid {proc.pid}" in text and "/elsewhere" in text
      proc.kill()
      proc.wait(timeout=10)
      await load_verified(lab, layout=OT2_LAYOUT)
  finally:
    proc.kill()


def test_a_robot_name_is_a_safe_file_name(tmp_path):
  from ahc.workspace.robot_lock import lock_path

  assert lock_path("OT2CEP20210329").name == "OT2CEP20210329.lock"
  odd = lock_path("../etc/passwd")
  assert odd.parent == lock_path("x").parent and "/" not in odd.name and odd != lock_path("..etcpasswd")


# -- signals --------------------------------------------------------------------------------------------

class StdioServer:
  """A real `ahc-mcp` stdio server in a subprocess, spoken to by hand (newline-delimited JSON-RPC). SIGINT keeps its
  default here: a process backgrounded from a shell ignores it."""

  def __init__(self, folder, home):
    env = {**os.environ, "AHC_HOME": str(home)}
    env.pop("AHC_BACKEND", None)
    self.proc = subprocess.Popen([sys.executable, "-m", "ahc.server", "--workspace", str(folder)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=env, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    self.watchdog = threading.Timer(120, self.proc.kill)  # a test that hangs fails instead
    self.watchdog.start()
    self.n = 0
    self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "test", "version": "0"}})
    self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

  def _send(self, message):
    self.proc.stdin.write(json.dumps(message) + "\n")
    self.proc.stdin.flush()

  def rpc(self, method, params):
    self.n += 1
    self._send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
    while True:
      line = self.proc.stdout.readline()
      assert line, "the server ended: " + self.proc.stderr.read()
      message = json.loads(line)
      if message.get("id") == self.n:
        assert "error" not in message, message
        return message["result"]

  def call(self, tool, **args):
    result = self.rpc("tools/call", {"name": tool, "arguments": args})
    assert not result.get("isError"), result["content"][0]["text"]
    return result.get("structuredContent")

  def close(self):
    self.watchdog.cancel()
    self.proc.kill()
    self.proc.wait(timeout=10)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_stops_the_robots_run_before_the_server_exits(tmp_path, fake_robot, sig):
  folder, home = tmp_path / "task", tmp_path / "ahc-home"
  folder.mkdir()
  server = StdioServer(folder, home)
  try:
    server.call("configure_devices", devices=robot_entry("127.0.0.1", fake_robot.port, robot_name=fake_robot.name))
    summary = server.call("lab_overview")["config"]["confirm_summary"]
    server.call("confirm_config", summary=summary)
    server.call("report_decision", decision="Test", plan=["Load"])
    server.call("load_layout", layout=OT2_LAYOUT, plan_step=1)
    run = fake_robot.current
    assert fake_robot.runs[run]["status"] != "stopped" and posts(fake_robot, f"/runs/{run}/actions") == []
    holder = json.loads((home / "locks" / f"{fake_robot.name}.lock").read_text())
    assert holder["pid"] == server.proc.pid

    started = time.monotonic()
    os.kill(server.proc.pid, sig)
    assert server.proc.wait(timeout=30) == 0  # exits by itself, cleanly
    assert time.monotonic() - started < 15
    assert posts(fake_robot, f"/runs/{run}/actions") == [f"/runs/{run}/actions"]  # the stop went to the robot
    assert fake_robot.runs[run]["status"] == "stopped"
    assert free(fake_robot.name)
    log = "".join(p.read_text() for p in (folder / ".ahc" / "runs").glob("*.jsonl"))
    assert '"signal_shutdown"' in log and signal.Signals(sig).name in log and "unconfirmed" not in log
  finally:
    server.close()


async def test_a_signal_that_the_robot_does_not_answer_is_bounded(tmp_path, open_lab, fake_robot, monkeypatch):
  monkeypatch.setattr(server_module, "SIGNAL_SHUTDOWN_S", 0.3)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    server = lab.server.lab
    stopping = server.adapter.stop

    async def slow_stop():
      await asyncio.sleep(1.5)  # a robot that takes longer than the server will wait
      await stopping()

    server.adapter.stop = slow_stop
    exits = []
    server._exit = exits.append
    started = time.monotonic()
    await server.end_on_signal(signal.SIGTERM)
    assert exits == [1] and time.monotonic() - started < 1.2  # exits anyway, saying the stop was not confirmed
    events = [e["event"] for e in server.runlog.entries if e.get("type") == "audit"]
    assert "signal_shutdown" in events and "signal_shutdown_unconfirmed" in events
  assert fake_robot.runs[fake_robot.current]["status"] == "stopped"  # the shutdown went on and finished


async def test_the_signal_shutdown_and_the_lifespan_end_are_one_shutdown(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    run = fake_robot.current
    server = lab.server.lab
    exits = []
    server._exit = exits.append
    await asyncio.gather(server.end_on_signal(signal.SIGTERM), server.shutdown(), server.shutdown())
    assert exits == [0] and server.adapter is None
    assert posts(fake_robot, f"/runs/{run}/actions") == [f"/runs/{run}/actions"]  # one stop, however many asked
  assert posts(fake_robot, f"/runs/{run}/actions") == [f"/runs/{run}/actions"]  # and the lifespan end added none


async def test_a_second_signal_while_ending_changes_nothing(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    server = lab.server.lab
    exits = []
    server._exit = exits.append
    server._signalled(signal.SIGINT)
    first = server._ending
    server._signalled(signal.SIGTERM)  # a client sends SIGINT, then SIGTERM
    assert server._ending is first
    await first
    assert exits == [0]


# -- a robot that moved ---------------------------------------------------------------------------------

def gone(bot) -> None:
  """The robot no longer answers where it did."""
  bot.close()
  bot.server.server_close()


async def test_a_robot_that_moved_is_named_with_its_new_address(tmp_path, open_lab, fake_robot, monkeypatch):
  moved = FakeRobot(name=fake_robot.name)  # the same robot, answering at another address
  searches = []

  def search(timeout_s):
    searches.append(timeout_s)
    return [{"name": moved.name, "host": "127.0.0.1", "port": moved.port, "simulator": False}]

  monkeypatch.setattr(server_module, "search_robots", search)
  try:
    async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
      server = lab.server.lab
      gone(fake_robot)
      overview = await lab.call("lab_overview")  # the identity of a robot that does not answer
      identity = overview["device_identity"]
      assert f"127.0.0.1:{moved.port}" in identity["error"] and "options.host 127.0.0.1" in identity["hint"]
      assert "the person confirms" in identity["hint"] and "name is checked" in identity["hint"]
      text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
      assert "[backend_unreachable]" in text and f"A robot named {moved.name!r} answers at 127.0.0.1:{moved.port}" in text
      hint = text.partition("Hint:")[2]
      assert f"configure_devices with options.host 127.0.0.1 and options.port {moved.port}" in hint
      assert "the person confirms (the name is checked)" in hint
      assert server.config.devices[0].options["port"] == fake_robot.port  # the address is not switched
      assert len(searches) == 1 and searches[0] <= 5  # short, and kept for the next call
      assert free(moved.name)  # a load that started no run holds nothing
  finally:
    moved.close()


async def test_a_robot_nobody_finds_leaves_the_refusal_as_it_was(tmp_path, open_lab, fake_robot, monkeypatch):
  monkeypatch.setattr(server_module, "search_robots",
                      lambda timeout_s: [{"name": "another-ot2", "host": "169.254.1.1", "port": 31950}])
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    gone(fake_robot)
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[backend_unreachable]" in text and "answers at" not in text and "169.254.1.1" not in text


async def test_the_search_is_for_an_unreachable_robot_only(tmp_path, open_lab, fake_robot, monkeypatch):
  searches = []
  monkeypatch.setattr(server_module, "search_robots", lambda timeout_s: searches.append(timeout_s) or [])
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)  # a robot that answers is never looked for
    await lab.call("lab_overview")
  sim_folder = tmp_path / "sim-task"
  sim_folder.mkdir()
  async with open_lab(sim_folder, "sim", "hamilton.starlet") as lab:
    give_reference(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)
    assert await lab.server.lab.search_by_name() is None  # asked outright: simulating looks for nothing
  assert searches == []  # nor in simulation
