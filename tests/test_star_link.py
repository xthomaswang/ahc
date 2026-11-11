"""The Mac side of the STARlet's real backend: the link, PyLabRobot's transport over it, and the guard (layer 1).
The spawned process is a local Python process serving a fake instrument; nothing here reaches ssh, USB or a network."""

import asyncio
import os
import subprocess
import sys
import threading
import time
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from pylabrobot.hamilton.star.driver.errors import STARFirmwareError
from pylabrobot.hamilton.star.driver.master import STARDriver

from ahc.core.errors import LabError
from ahc.devices.hamilton import link
from ahc.devices.hamilton.link import (
  BridgeError, BridgeIO, BridgeLink, BridgeLost, BridgeRefused, GuardedIO, GuardRefused, GuardState, fail_waiting,
  guard_driver, listen_for_replies, ssh_argv,
)
from ahc.devices.hamilton.star_bridge import __file__ as BRIDGE_FILE
from fake_star import FakeStarIO, write_launcher, written

pytestmark = pytest.mark.anyio

BRIDGE_PATH = Path(BRIDGE_FILE)
DESTINATION, PYTHON, SCRIPT = "usb-host", "python3", "~/star_bridge.py"


# -- ssh_argv -----------------------------------------------------------------------------------

def test_ssh_argv_builds_the_specified_command():
  assert ssh_argv("pi@usb-host.local", "~/venv/bin/python", "~/star_bridge.py") == [
    "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=3", "--", "pi@usb-host.local", "~/venv/bin/python -u ~/star_bridge.py serve",
  ]
  assert ssh_argv("T0001", "python3", "star_bridge.py")[-1] == "python3 -u star_bridge.py serve"


@pytest.mark.parametrize("destination", [
  "-oProxyCommand=touch_x", "-o", "-host", "", "host name", "host;ls", "host$(x)", "a@-b", "@host", "host@", "a@b@c",
  "host\n", "ho'st", "host|x", "host`x`",
])
def test_ssh_argv_refuses_a_destination_that_could_be_an_option_or_shell(destination):
  with pytest.raises(LabError) as raised:
    ssh_argv(destination, "python3", "~/star_bridge.py")
  assert raised.value.code == "bad_option"


@pytest.mark.parametrize("python, script", [
  ("python3; ls", "~/s.py"), ("python3 -c x", "~/s.py"), ("-python", "~/s.py"), ("", "~/s.py"), ("$(x)", "~/s.py"),
  ("python3", "~/s.py; ls"), ("python3", "~/s.py && ls"), ("python3", "-s.py"), ("python3", ""), ("python3", "~/s.py\n"),
  ("python3", "'~/s.py'"), ("python3", "~/s.py `x`"), ("python3", "~/s.py|x"),
])
def test_ssh_argv_refuses_spaces_and_shell_syntax_in_the_remote_command(python, script):
  with pytest.raises(LabError) as raised:
    ssh_argv("usb-host", python, script)
  assert raised.value.code == "bad_option"


# -- the fake instrument, spawned ---------------------------------------------------------------

class Instrument:
  """Replaces `link.spawn`: each spawn is a local Python process serving a FakeStarIO."""

  def __init__(self, tmp_path: Path, monkeypatch):
    self.tmp_path = tmp_path
    self.home = tmp_path / "home"
    self.procs: list[subprocess.Popen] = []
    self.launcher: dict | None = None
    monkeypatch.setattr(link, "spawn", self.spawn)

  def configure(self, hold: tuple[str, ...] = (), fail: dict[str, str] | None = None) -> "Instrument":
    self.launcher = write_launcher(self.tmp_path / "fake", BRIDGE_PATH, hold, fail)
    return self

  def spawn(self, argv: list[str]) -> subprocess.Popen:
    assert argv[0] == "ssh" and argv[-1].endswith(" serve")  # what AHC would have run; here it is not
    if self.launcher is None:
      self.configure()
    env = {**os.environ, "AHC_BRIDGE_HOME": str(self.home)}
    proc = subprocess.Popen([sys.executable, str(self.launcher["script"])], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    self.procs.append(proc)
    return proc

  def written(self) -> list[str]:
    return written(self.launcher["log"])

  def codes(self) -> list[str]:
    return [text[:4] for text in self.written()]

  def release(self, code: str) -> None:
    (self.launcher["release_dir"] / f"release-{code}").touch()

  def kill(self) -> None:
    for proc in self.procs:
      proc.kill()

  def stop(self) -> None:
    for proc in self.procs:
      if proc.poll() is None:
        proc.kill()
      proc.wait(timeout=10)
      for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
          stream.close()
        except OSError:
          pass


@pytest.fixture
def instrument(tmp_path, monkeypatch):
  inst = Instrument(tmp_path, monkeypatch)
  yield inst
  inst.stop()


def new_io(**kwargs) -> BridgeIO:
  return BridgeIO(DESTINATION, PYTHON, SCRIPT, **kwargs)


async def until(predicate, timeout: float = 5.0) -> None:
  deadline = time.monotonic() + timeout
  while not predicate():
    assert time.monotonic() < deadline, "timed out waiting"
    await anyio.sleep(0.01)


@asynccontextmanager
async def session(inst: Instrument, **configure):
  """A real STARDriver over GuardedIO(BridgeIO) with the guard on its calls, connected as `setup()` would connect."""
  inst.configure(**configure)
  state = GuardState()
  bio = new_io(start_timeout=15)
  driver = STARDriver(io=GuardedIO(bio, state))
  guard_driver(driver, state)
  await driver._open()
  driver._connected = True
  try:
    yield driver, state, bio
  finally:
    driver._connected = False
    await driver._close()


# -- a real driver over the link ----------------------------------------------------------------

async def test_a_real_driver_reads_refuses_motion_in_read_only_and_moves_once_open_and_armed(instrument):
  async with session(instrument) as (driver, state, bio):
    assert bio.hello["protocol"] == 1 and bio.hello["armed"] is False
    reply = await driver._send("C0", "RF")
    assert reply.startswith("C0RFid") and "rfT0001" in reply
    with pytest.raises(GuardRefused) as refused:
      await driver._send("C0", "AS")
    assert refused.value.command == "C0AS" and "not initialized" in refused.value.why
    assert instrument.codes() == ["C0RF"]  # the motion command never reached the instrument
    assert (state.sent, state.moved) == (1, 0)

    state.open()
    await bio.arm()
    reply = await driver._send("C0", "AS")
    assert reply.startswith("C0ASid") and "er00/00" in reply
    assert instrument.codes() == ["C0RF", "C0AS"]
    assert (state.sent, state.moved) == (2, 1)
    assert state.in_flight() == []
    assert state.record(5) == [{"command": "C0AS", "status": "refused"}, {"command": "C0AS", "status": "answered"}]
    assert state.mode == "open"
  # link-only close: the process ends by itself, having been told to close
  assert instrument.procs[0].wait(timeout=10) == 0
  assert instrument.codes() == ["C0RF", "C0AS"]  # closing wrote nothing


async def test_the_bridge_refuses_when_unarmed_although_the_mac_guard_is_open(instrument):
  async with session(instrument) as (driver, state, bio):
    state.open()
    with pytest.raises(BridgeRefused) as refused:
      await driver._send("C0", "AS")
    assert (refused.value.code, refused.value.command) == ("not_armed", "C0AS")
    assert instrument.written() == []
    assert state.in_flight() == []
    assert state.record(3) == [{"command": "C0AS", "status": "refused"}]
    assert state.mode == "read_only"  # a refusal from the bridge closes the guard too
    assert "not_armed" in state.why


async def test_a_command_in_flight_shows_until_its_reply_is_released(instrument):
  async with session(instrument, hold=("C0AS",)) as (driver, state, bio):
    state.open()
    await bio.arm()
    task = asyncio.ensure_future(driver._send("C0", "AS"))
    await until(lambda: instrument.codes() == ["C0AS"])
    assert state.in_flight() == ["C0AS"]
    assert state.record(1) == [{"command": "C0AS", "status": "sent"}]
    assert not task.done()
    instrument.release("C0AS")
    reply = await asyncio.wait_for(task, 5)
    assert reply.startswith("C0ASid")
    assert state.in_flight() == []
    assert state.record(1) == [{"command": "C0AS", "status": "answered"}]


async def test_a_firmware_error_reply_to_a_motion_command_latches_the_guard(instrument):
  async with session(instrument, fail={"C0AS": "er01/00"}) as (driver, state, bio):
    state.open()
    await bio.arm()
    with pytest.raises(STARFirmwareError):
      await driver._send("C0", "AS")
    assert state.mode == "read_only"
    assert state.why.startswith("a command failed: C0AS er01")  # the reply's own latch comes first
    assert state.record(1) == [{"command": "C0AS", "status": "error er01/00"}]
    assert state.in_flight() == []
    with pytest.raises(GuardRefused):
      await driver._send("C0", "DI")
    assert instrument.codes() == ["C0AS"]  # nothing else was written
    state.open()
    assert state.why is None and state.mode == "open"


async def test_a_lost_link_ends_the_waiting_call_and_everything_after_it(instrument):
  lost: list[str] = []
  async with session(instrument, hold=("C0AS",)) as (driver, state, bio):
    bio.on_lost = lambda reason: (lost.append(reason), fail_waiting(driver, reason))
    state.open()
    await bio.arm()
    task = asyncio.ensure_future(driver._send("C0", "AS"))
    await until(lambda: instrument.codes() == ["C0AS"])
    instrument.kill()
    with anyio.fail_after(3):
      with pytest.raises(BridgeLost):
        await task
    assert len(lost) == 1 and "ended" in lost[0]
    assert bio.lost == lost[0]
    with pytest.raises(TimeoutError):  # and only that, so the router's reading thread lives
      await bio.read()
    with anyio.fail_after(1):
      with pytest.raises(BridgeLost):
        await bio.write(b"C0RFid0099")
    # the call that waited did not complete, so the guard closed at the point of failure
    assert (state.mode, state.why) == ("read_only", "C0AS did not complete")
    with pytest.raises(GuardRefused):
      await driver._send("C0", "AS")
    with anyio.fail_after(1):
      with pytest.raises(BridgeLost):  # a read-only command reaches the dead link and fails at once
        await driver._send("C0", "RF")
    assert state.why == "C0AS did not complete"  # the first latch's reason stands


# -- the BridgeIO pieces ------------------------------------------------------------------------

async def test_bridge_io_serializes_and_passes_the_arming_calls_through(instrument):
  io = new_io(start_timeout=15)
  assert io.serialize() == {"type": "BridgeIO", "bridge": DESTINATION}
  assert GuardedIO(io, GuardState()).serialize() == io.serialize()
  instrument.configure()
  await io.setup()
  try:
    assert io.hello["op"] == "hello"
    assert (await io.status())["armed"] is False
    assert (await io.arm())["op"] == "armed"
    assert (await io.status())["armed"] is True
    assert (await io.disarm())["op"] == "disarmed"
    with pytest.raises(BridgeRefused) as refused:  # disarmed again: a command that moves does not pass
      await io.write(b"C0ASid0001")
    assert refused.value.code == "not_armed"
    await io.write(b"C0RFid0002")
    assert await io.read() == b"C0RFid0002er00/00rfT0001 1.0S 25 2025_01_01"
  finally:
    await io.stop()
  assert instrument.codes() == ["C0RF"]


async def test_bridge_io_read_times_out_and_the_bridge_answers_are_mapped(instrument):
  io = new_io(start_timeout=15)
  instrument.configure()
  await io.setup()
  try:
    started = time.monotonic()
    with pytest.raises(TimeoutError):
      await io.read()
    assert 0.5 < time.monotonic() - started < 3
    link_ = io.link
    answer = link_.request("write", fields={"data": "C0RFĀ", "timeout": None})  # not latin-1: refused as a bad request
    assert (answer["op"], answer["code"]) == ("refused", "bad_request")
  finally:
    await io.stop()


async def test_bridge_io_works_from_any_event_loop(instrument):
  io = new_io(start_timeout=15)
  instrument.configure()
  await io.setup()
  results: list = []

  def other_loop():
    async def go():
      await io.write(b"C0RFid0003")
      results.append(await io.read())

    asyncio.run(go())

  try:
    thread = threading.Thread(target=other_loop)
    thread.start()
    await anyio.to_thread.run_sync(thread.join)
    assert results and results[0].startswith(b"C0RFid0003")
  finally:
    await io.stop()


async def test_a_failed_setup_leaves_nothing_behind(monkeypatch):
  monkeypatch.setattr(link, "spawn", lambda argv: scripted("import sys; sys.exit(3)"))
  io = new_io(start_timeout=10)
  with pytest.raises(LabError) as raised:
    await io.setup()
  assert raised.value.code == "backend_unreachable"
  assert io.lost is not None
  await io.stop()  # and stopping afterwards is fine


def test_answers_map_to_exceptions():
  assert link._answer({"op": "written"}, "written") == {"op": "written"}
  with pytest.raises(BridgeError, match="OSError: x"):
    link._answer({"op": "failed", "error": "OSError: x"}, "written")
  with pytest.raises(BridgeRefused) as refused:
    link._answer({"op": "refused", "code": "not_armed", "command": "C0AS"}, "written")
  assert (refused.value.code, refused.value.command) == ("not_armed", "C0AS")
  with pytest.raises(BridgeError):
    link._answer({"op": "surprise"}, "written")


# -- start failures -----------------------------------------------------------------------------

def scripted(code: str) -> subprocess.Popen:
  return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)


HELLO = '{"op":"hello","protocol":%s,"armed":%s,"device":{"bus":1,"address":2}}'


def start_failure(monkeypatch, code: str, timeout: float = 10.0) -> LabError:
  processes: list[subprocess.Popen] = []
  monkeypatch.setattr(link, "spawn", lambda argv: processes.append(scripted(code)) or processes[-1])
  bridge_link = BridgeLink(DESTINATION, PYTHON, SCRIPT)
  with pytest.raises(LabError) as raised:
    bridge_link.start(timeout=timeout)
  assert processes[0].poll() is not None  # nothing is left running
  assert raised.value.hint  # a person can act on it
  return raised.value


def test_start_fails_when_the_process_ends_at_once_with_its_error_output(monkeypatch):
  error = start_failure(monkeypatch, "import sys; sys.stderr.write('Permission denied (publickey).\\n'); sys.exit(255)")
  assert error.code == "backend_unreachable"
  assert "Permission denied (publickey)" in str(error) and "255" in str(error)


def test_start_fails_when_there_is_no_hello_in_time(monkeypatch):
  error = start_failure(monkeypatch, "import sys, time; sys.stdin.read()", timeout=0.3)
  assert error.code == "backend_unreachable" and "did not answer" in str(error)


@pytest.mark.parametrize("fatal, code", [
  ("in_use", "bridge_in_use"), ("no_device", "instrument_not_found"), ("several_devices", "several_instruments"),
  ("usb_failed", "backend_unreachable"), ("something_new", "backend_unreachable"),
])
def test_a_fatal_from_the_bridge_maps_to_a_lab_error(monkeypatch, fatal, code):
  error = start_failure(
    monkeypatch, f"import sys; print('{{\"op\":\"fatal\",\"code\":\"{fatal}\",\"detail\":\"made up\"}}'); sys.exit(2)")
  assert error.code == code and "made up" in str(error)


@pytest.mark.parametrize("printed", [
  HELLO % (2, "false"),  # another protocol number
  HELLO % (1, "true"),  # a bridge that says it is armed
  "this is not the bridge",  # garbage
  '{"op": 5}',
])
def test_a_wrong_protocol_or_garbage_is_a_protocol_error(monkeypatch, printed):
  error = start_failure(monkeypatch, f"import sys; print({printed!r}); sys.stdout.flush(); sys.stdin.read()")
  assert error.code == "bridge_protocol"


def test_start_fails_when_ssh_cannot_be_started(monkeypatch):
  def broken(argv):
    raise FileNotFoundError("ssh")

  monkeypatch.setattr(link, "spawn", broken)
  with pytest.raises(LabError) as raised:
    BridgeLink(DESTINATION, PYTHON, SCRIPT).start()
  assert raised.value.code == "backend_unreachable"


async def test_a_second_link_to_the_same_host_is_in_use(instrument):
  instrument.configure()
  first, second = BridgeLink(DESTINATION, PYTHON, SCRIPT), BridgeLink(DESTINATION, PYTHON, SCRIPT)
  try:
    assert first.start(timeout=15)["op"] == "hello"
    with pytest.raises(LabError) as raised:
      second.start(timeout=15)
    assert raised.value.code == "bridge_in_use" and raised.value.hint
  finally:
    first.close()
    second.close()


def test_a_request_that_is_not_answered_loses_and_closes_the_link(monkeypatch):
  code = f"import sys; print({HELLO % (1, 'false')!r}); sys.stdout.flush(); sys.stdin.read()"
  monkeypatch.setattr(link, "spawn", lambda argv: scripted(code))
  bridge_link = BridgeLink(DESTINATION, PYTHON, SCRIPT)
  lost: list[str] = []
  bridge_link.on_lost = lost.append
  assert bridge_link.start(timeout=15)["protocol"] == 1
  with pytest.raises(BridgeLost):
    bridge_link.request("status", timeout=0.3)
  assert len(lost) == 1 and "no answer" in lost[0]
  assert bridge_link._proc.poll() is not None
  with pytest.raises(BridgeLost):
    bridge_link.request("status")
  bridge_link.close()
  bridge_link.close()  # idempotent, never raises
  assert len(lost) == 1


def test_a_deliberate_close_is_quiet_and_idempotent(instrument):
  instrument.configure()
  bridge_link = BridgeLink(DESTINATION, PYTHON, SCRIPT)
  lost: list[str] = []
  bridge_link.on_lost = lost.append
  bridge_link.start(timeout=15)
  assert bridge_link.request("status")["armed"] is False
  bridge_link.close()
  bridge_link.close()
  assert instrument.procs[0].returncode == 0
  assert lost == []
  with pytest.raises(BridgeLost):
    bridge_link.request("status")
  BridgeLink(DESTINATION, PYTHON, SCRIPT).close()  # never started: also fine


# -- GuardState and GuardedIO (a fake IO, no link) ----------------------------------------------

def test_guard_state_basics():
  state = GuardState()
  assert (state.mode, state.why, state.sent, state.moved, state.in_flight(), state.record(5)) == (
    "read_only", "not initialized", 0, 0, [], [])
  state.open()
  assert (state.mode, state.why) == ("open", None)
  state.latch("first")
  state.latch("second")  # never overwrites an earlier latch's why
  assert (state.mode, state.why) == ("read_only", "first")
  state.open()
  assert (state.mode, state.why) == ("open", None)
  state.latch("third")
  assert state.why == "third"


def test_guard_state_keeps_the_last_64_commands_that_are_not_read_only():
  state = GuardState()
  state.open()
  for number in range(70):
    state.admit(f"C0AS{number:04d}")
    state.register(f"C0ASid{number:04d}")
    state.admit("C0RFid0001")
    assert state.register("C0RFid0001") is None  # a read-only command is not logged
  assert len(state.log) == 64
  assert len(state.record(100)) == 64 and state.record(0) == [] and len(state.record(3)) == 3
  assert state.record(1) == [{"command": "C0AS", "status": "sent"}]
  assert state.sent == 140 and state.moved == 70


async def test_guarded_io_backstop_refuses_motion_in_read_only_and_forwards_reads():
  fake, state = FakeStarIO(), GuardState()
  gio = GuardedIO(fake, state)
  await gio.setup()
  assert fake.is_setup
  with pytest.raises(GuardRefused):
    await gio.write(b"C0ASid0001")
  assert fake.written == [] and state.sent == 0 and state.in_flight() == []
  assert state.record(1) == [{"command": "C0AS", "status": "refused"}]
  await gio.write(b"C0RFid0002")
  assert (await gio.read(timeout=1)).startswith(b"C0RFid0002")
  with pytest.raises(TimeoutError):  # a timeout passes through
    await gio.read(timeout=0.05)
  await gio.stop()
  assert fake.stopped


async def test_guarded_io_matches_replies_by_id_and_latches_on_an_error():
  fake, state = FakeStarIO(), GuardState()
  fake.fail("C0DS", "er02/00")
  gio = GuardedIO(fake, state)
  state.open()
  await gio.write(b"C0ASid0007")
  await gio.write(b"C0DSid0008")
  assert state.in_flight() == ["C0AS", "C0DS"]
  assert state.mode == "open"
  await gio.read(timeout=1)  # the C0AS reply
  assert state.in_flight() == ["C0DS"] and state.mode == "open"
  await gio.read(timeout=1)  # the C0DS error
  assert state.in_flight() == []
  assert state.mode == "read_only" and state.why == "a command failed: C0DS er02/00"
  assert [e["status"] for e in state.record(5)] == ["answered", "error er02/00"]
  fake._answers.append("C0ASid5555er00/00")  # a reply nobody waits for is ignored
  await gio.read(timeout=1)
  assert state.in_flight() == []


async def test_guarded_io_marks_a_failed_write_and_latches():
  class Broken(FakeStarIO):
    async def write(self, data, *args, **kwargs):
      raise OSError("pipe broke")

  state = GuardState()
  state.open()
  gio = GuardedIO(Broken(), state)
  with pytest.raises(OSError):
    await gio.write(b"C0ASid0001")
  assert state.in_flight() == []
  assert state.record(1) == [{"command": "C0AS", "status": "error write failed"}]
  assert (state.mode, state.why) == ("read_only", "a command could not be written")


# -- guard_driver on a driver that never reaches an IO ------------------------------------------

def simulated_device():
  import pylabrobot.resources as plr
  from pylabrobot.hamilton.star import STARlet

  deck = plr.STARLetDeck()
  with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    return STARlet(deck=deck, simulation=True), deck


def place_tips_and_plate(deck):
  import pylabrobot.resources as plr

  plates = plr.PLT_CAR_L5_DWP(name="plates")
  plates[0] = plr.Cor_96_wellplate_360ul_Fb(name="target")
  tips = plr.TIP_CAR_480_A00(name="tipcar")
  tips[0] = plr.hamilton_96_tiprack_300uL_filter(name="tips")
  deck.assign_child_resource(plates, track=1)
  deck.assign_child_resource(tips, track=13)
  return deck.get_resource("tips")


async def test_guard_driver_on_the_simulated_driver():
  device, deck = simulated_device()
  driver, state = device.driver, GuardState()
  guard_driver(driver, state)
  await driver._open()
  driver._connected = True
  await driver.discover()  # all reads: passes in read-only mode
  assert state.sent == 59 and state.moved == 0 and state.mode == "read_only" and len(state.log) == 0

  with pytest.raises(GuardRefused):  # setup() stops at its first motion command
    await device.setup()
  assert state.moved == 0
  assert state.log and all(entry["status"] == "refused" for entry in state.log)
  assert state.mode == "read_only" and state.why == "not initialized"

  state.open()
  await device.setup()
  assert state.moved > 0 and state.mode == "open"

  rack = place_tips_and_plate(deck)
  sent, moved = state.sent, state.moved
  state.latch("emergency stop")
  with pytest.raises(GuardRefused) as refused:
    await device.pipettes.pick_up_tips(rack["A1:H1"])
  assert state.sent == sent  # not one more command passed
  assert "emergency stop" in str(refused.value) and state.why == "emergency stop"
  await device.stop()  # PyLabRobot's own cleanup finds the guard closed: its reads pass, nothing moves
  assert state.moved == moved


async def test_a_motion_command_that_raises_latches_before_pylabrobots_cleanup_runs():
  device, deck = simulated_device()
  driver, state = device.driver, GuardState()
  real_send = driver._send
  state.open()

  async def failing(module, command, *args, **kwargs):
    if module + command == "C0DI":  # a motion command setup() sends
      raise TimeoutError("no answer from the instrument")
    return await real_send(module, command, *args, **kwargs)

  driver._send = failing  # underneath the guard
  guard_driver(driver, state)
  with pytest.raises(TimeoutError):
    await device.setup()
  assert (state.mode, state.why) == ("read_only", "C0DI did not complete")
  # setup()'s own `except: await self.stop()` tried to move, and the guard refused all of it
  failing_at = next(i for i, e in enumerate(state.log) if e["status"] == "refused")
  assert failing_at >= 0 and all(e["status"] == "refused" for e in list(state.log)[failing_at:])
  moved_before = state.moved
  assert moved_before > 0
  with pytest.raises(GuardRefused):
    await driver._send("C0", "AS")
  assert state.moved == moved_before


async def test_cancelling_a_motion_call_latches_the_guard(instrument):
  async with session(instrument, hold=("C0AS",)) as (driver, state, bio):
    state.open()
    await bio.arm()
    task = asyncio.ensure_future(driver._send("C0", "AS"))
    await until(lambda: instrument.codes() == ["C0AS"])
    assert state.mode == "open"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
    assert (state.mode, state.why) == ("read_only", "C0AS did not complete")
    with pytest.raises(GuardRefused):
      await driver._send("C0", "DI")
    assert instrument.codes() == ["C0AS"]
    assert state.moved == 1
    # PyLabRobot's router keeps the cancelled command in its waiting list and would later set an exception on its
    # cancelled future (InvalidStateError on the loop): failing what waits first clears it
    fail_waiting(driver, "cancelled")


async def test_a_read_only_command_latches_on_a_transport_failure_but_not_on_a_firmware_error(instrument):
  async with session(instrument, fail={"C0RF": "er01/00"}, hold=("C0RI",)) as (driver, state, bio):
    with pytest.raises(STARFirmwareError):  # answered with an error: discovery tolerates some, so no latch
      await driver._send("C0", "RF")
    assert state.mode == "read_only" and state.why == "not initialized"
    state.open()
    with pytest.raises(STARFirmwareError):
      await driver._send("C0", "RF")
    assert state.mode == "open"  # still open: a read-only error reply is not a failed link
    with pytest.raises(TimeoutError):  # no answer at all
      await driver._send("C0", "RI", read_timeout=0.3)
    assert (state.mode, state.why) == ("read_only", "C0RI got no answer")


async def test_a_read_only_command_that_cannot_be_written_latches():
  device, _ = simulated_device()
  driver, state = device.driver, GuardState()
  state.open()

  async def broken(module, command, *args, **kwargs):
    raise BridgeLost("test")

  driver._send = broken
  guard_driver(driver, state)
  with pytest.raises(BridgeLost):
    await driver._send("C0", "RF")
  assert (state.mode, state.why) == ("read_only", "C0RF got no answer")


async def test_sent_and_moved_count_what_passes_and_a_refusal_counts_in_neither(instrument):
  async with session(instrument) as (driver, state, bio):
    await driver._send("C0", "RF")
    await driver._send("P1", "RD")
    assert (state.sent, state.moved) == (2, 0)
    with pytest.raises(GuardRefused):
      await driver._send("C0", "AS")
    assert (state.sent, state.moved) == (2, 0)
    state.open()
    await bio.arm()
    await driver._send("C0", "AS")
    await driver._send("C0", "RF")
    assert (state.sent, state.moved) == (4, 1)
    state.latch("emergency stop")
    with pytest.raises(GuardRefused):
      await driver._send("C0", "DI")
    assert (state.sent, state.moved) == (4, 1)


# -- listening after a stop ---------------------------------------------------------------------

async def cancelled_while_held(driver, state, instrument) -> None:
  """A held C0AS in flight whose call is then cancelled (the server's emergency stop does that)."""
  state.open()
  await driver.io.io.arm()
  task = asyncio.ensure_future(driver._send("C0", "AS"))
  await until(lambda: instrument.codes() == ["C0AS"])
  task.cancel()
  with pytest.raises(asyncio.CancelledError):
    await task
  assert state.latched and state.in_flight() == ["C0AS"]


async def test_listening_hears_the_reply_of_a_command_in_flight_after_a_stop(instrument):
  async with session(instrument, hold=("C0AS",)) as (driver, state, bio):
    await cancelled_while_held(driver, state, instrument)
    asyncio.get_running_loop().call_later(0.3, instrument.release, "C0AS")
    assert await listen_for_replies(driver, driver.io, state, 10) == []
    assert state.in_flight() == [] and state.record(1) == [{"command": "C0AS", "status": "answered"}]
    assert instrument.codes() == ["C0AS"]  # listening wrote nothing


async def test_listening_reports_what_is_never_answered(instrument):
  async with session(instrument, hold=("C0AS",)) as (driver, state, bio):
    await cancelled_while_held(driver, state, instrument)
    started = time.monotonic()
    assert await listen_for_replies(driver, driver.io, state, 0.5) == ["C0AS"]
    assert time.monotonic() - started < 4
    assert instrument.codes() == ["C0AS"]  # nothing was written meanwhile
    assert not driver._replies._waiting_tasks  # what PyLabRobot still waited for was failed


async def test_listening_stops_at_a_lost_link_and_with_nothing_in_flight(instrument):
  async with session(instrument, hold=("C0AS",)) as (driver, state, bio):
    assert await listen_for_replies(driver, driver.io, state, 5) == []  # nothing in flight: returns at once
    await cancelled_while_held(driver, state, instrument)
    started = time.monotonic()
    assert await listen_for_replies(driver, driver.io, state, 30, lost=lambda: "gone") == ["C0AS"]
    assert time.monotonic() - started < 2
