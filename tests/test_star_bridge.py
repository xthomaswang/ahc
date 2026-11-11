"""The bridge on the USB host, alone: the read-only rule, arming, and the protocol. Mostly in-process
through `serve(io, stdin, stdout, home)` over pipes with a fake instrument; the command line through subprocess.
Nothing here opens USB or leaves this machine."""

import ast
import asyncio
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import warnings
from pathlib import Path

import pytest

from ahc.devices.hamilton import star_bridge as bridge
from fake_star import FakeStarIO, write_launcher

BRIDGE_PATH = Path(bridge.__file__)
WAIT_S = 5.0

# The parameterless queries the person approved besides the requests (command codes starting with R or Q).
QUERIES = ["C0UA", "C0CQ"] + [f"P{n}{c}" for n in range(1, 9) for c in ("VW", "VY")]


# -- the read-only rule -------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
  "C0RFid0001", "C0QMid0002", "P1RDid0003", "I0RQ", "H0RF", b"C0RFid0001", bytearray(b"C0QM"),
  "C0UAid0004", "C0CQid0005", "P1VWid0006", "P8VYid0007", "C0UA", "P4VW",
])
def test_read_only_commands(command):
  assert bridge.is_read_only(command) is True


@pytest.mark.parametrize("command", [
  "R0ZA",  # module R0, command ZA: not a request
  "C0ZA", "C0VI", "C0AS", "C0DI", "C0TP", "H0ZA", "P1AA", "I0XP", "X0XP",
  "C0UB", "C0CP", "P9VW", "P0VY", "P1VX", "P1VZ", "p1vw", "c0rf", "C0UX", b"C0AS", b"R0ZA",
  "", "C0R", "C0", "abc", b"", None, 7, "C0 RF", "C0 R",
  "C0RFid0001\rC0ASid0002", "C0RFid0001\nC0ZA", "C0RF\x00", "C0RF\x7f", "C0QM\xff", b"C0RF\r\nC0AS", "C0UA\t",
])
def test_everything_else_is_not_read_only(command):
  assert bridge.is_read_only(command) is False


def simulated_codes():
  """The firmware commands PyLabRobot's simulated STARlet sends, by phase (module + code, as `_send` gets them)."""
  import pylabrobot.resources as plr
  from pylabrobot.hamilton.star import STARlet

  def spy(driver, log):
    real = driver._send

    async def _send(module, command, *args, **kwargs):
      log.append(module + command)
      return await real(module, command, *args, **kwargs)

    driver._send = _send

  async def main():
    discover, setup, transfer, stop = [], [], [], []
    device = STARlet(deck=plr.STARLetDeck(), simulation=True)
    spy(device.driver, discover)
    await device.driver._open()
    device.driver._connected = True
    await device.driver.discover()
    device.driver._connected = False
    await device.driver._close()

    deck = plr.STARLetDeck()
    device = STARlet(deck=deck, simulation=True)
    spy(device.driver, setup)
    await device.setup()
    plates = plr.PLT_CAR_L5_DWP(name="plates")
    plates[0] = plr.Cor_96_wellplate_360ul_Fb(name="target")
    tips = plr.TIP_CAR_480_A00(name="tipcar")
    tips[0] = plr.hamilton_96_tiprack_300uL_filter(name="tips")
    deck.assign_child_resource(plates, track=1)
    deck.assign_child_resource(tips, track=13)
    spy(device.driver, transfer)
    rack, target = deck.get_resource("tips"), deck.get_resource("target")
    for well in target["A1:H1"]:
      well.tracker.set_volume(200)
    await device.pipettes.pick_up_tips(rack["A1:H1"])
    await device.pipettes.aspirate(target["A1:H1"], volumes=[50] * 8)
    await device.pipettes.dispense(target["A2:H2"], volumes=[50] * 8)
    await device.pipettes.return_tips()
    spy(device.driver, stop)
    await device.stop()
    return discover, setup, transfer, stop

  with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    return asyncio.run(main())


def test_rule_on_the_simulators_corpus():
  discover, setup, transfer, stop = simulated_codes()
  assert len(discover) == 59  # the discover-only connect the person approved
  assert all(bridge.is_read_only(code) for code in discover)
  assert {code for code in discover if code[2] not in "RQ"} == set(QUERIES)

  added = set(setup + transfer + stop) - set(discover)
  motion = {code for code in added if code[2] not in "RQ"}
  # setup, stop and a tip pick-up / aspirate / dispense / tip return all add motion commands, none of them read-only
  assert {"C0ZA", "C0DI", "C0VI", "R0ZA", "H0ZA", "C0TP", "C0AS", "C0DS", "C0TR"} <= motion
  assert not any(bridge.is_read_only(code) for code in motion)
  # and every other code added is a request, which is read-only
  assert all(bridge.is_read_only(code) for code in added - motion)
  # the queries stay allowed wherever they appear
  assert all(bridge.is_read_only(code) for code in set(setup + transfer + stop) if code in QUERIES)


# -- the file itself ----------------------------------------------------------------------------

def test_file_parses_as_python_39():
  ast.parse(BRIDGE_PATH.read_text(), feature_version=(3, 9))


def test_imports_nothing_but_the_standard_library_at_import():
  code = (
    "import importlib.util, sys\n"
    "before = set(sys.modules)\n"
    f"spec = importlib.util.spec_from_file_location('bridge_alone', {str(BRIDGE_PATH)!r})\n"
    "module = importlib.util.module_from_spec(spec); sys.modules['bridge_alone'] = module\n"
    "spec.loader.exec_module(module)\n"
    "new = {name.split('.')[0] for name in set(sys.modules) - before}\n"
    "print(sorted(new - set(sys.stdlib_module_names) - {'bridge_alone'}))\n"
    "print('ahc' in sys.modules, 'pylabrobot' in sys.modules)\n"
  )
  out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30, check=True)
  assert out.stdout.split("\n")[:2] == ["[]", "False False"]


# -- running the bridge -------------------------------------------------------------------------

class Served:
  """`serve` (or `main`) running in a thread over pipes; the test is the other end."""

  def __init__(self, tmp_path: Path, io=None, home: Path | None = None, run=None):
    self.io = io or FakeStarIO()
    self.home = home or tmp_path / "home"
    self.answers: queue.Queue = queue.Queue()
    self.reads: queue.Queue = queue.Queue()
    self.code: list[int] = []
    self.counter = 0
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    self.stdin = os.fdopen(in_w, "wb", buffering=0)
    self._serve_stdin = os.fdopen(in_r, "rb")
    self._serve_stdout = os.fdopen(out_w, "wb")
    self._out = os.fdopen(out_r, "rb")
    self.run = run or (lambda stdin, stdout: asyncio.run(bridge.serve(self.io, stdin, stdout, str(self.home))))
    self.thread = threading.Thread(target=self._serve, daemon=True)
    self.reader = threading.Thread(target=self._read, daemon=True)
    self.thread.start()
    self.reader.start()

  def _serve(self):
    try:
      self.code.append(self.run(self._serve_stdin, self._serve_stdout))
    finally:
      self._serve_stdout.close()

  def _read(self):
    for line in self._out:
      message = json.loads(line)
      (self.reads if message.get("op") == "read" else self.answers).put(message)
    self.answers.put(None)  # the bridge's output ended

  def send_line(self, line: bytes):
    self.stdin.write(line if line.endswith(b"\n") else line + b"\n")

  def next(self) -> dict | None:
    return self.answers.get(timeout=WAIT_S)

  def ask(self, op: str, **fields) -> dict:
    self.counter += 1
    self.send_line(json.dumps({"op": op, "n": self.counter, **fields}).encode())
    answer = self.next()
    assert answer is not None and answer.get("n") == self.counter, answer
    return answer

  def write(self, command: str, timeout=None) -> dict:
    return self.ask("write", data=command, timeout=timeout)

  def read(self) -> dict:
    return self.reads.get(timeout=WAIT_S)

  def end(self) -> int:
    try:
      self.stdin.close()
    except OSError:
      pass
    self.thread.join(timeout=WAIT_S)
    assert not self.thread.is_alive(), "the bridge did not exit"
    self.reader.join(timeout=WAIT_S)
    self._out.close()
    return self.code[0] if self.code else -1


@pytest.fixture
def served(tmp_path):
  started: list[Served] = []

  def start(**kwargs) -> Served:
    s = Served(tmp_path, **kwargs)
    started.append(s)
    return s

  yield start
  for s in started:
    if s.thread.is_alive():
      s.end()


def hello_of(s: Served) -> dict:
  hello = s.next()
  assert hello["op"] == "hello"
  return hello


def cli(home: Path, *args: str) -> dict:
  env = {**os.environ, "AHC_BRIDGE_HOME": str(home)}
  out = subprocess.run([sys.executable, str(BRIDGE_PATH), *args], capture_output=True, text=True, env=env, timeout=30)
  assert out.returncode == 0, out.stderr
  lines = out.stdout.strip().split("\n")
  assert len(lines) == 1  # one JSON line, nothing else
  return json.loads(lines[0])


def test_hello_and_a_read_only_write_goes_through_unarmed(served):
  s = served()
  hello = hello_of(s)
  assert hello == {"op": "hello", "protocol": 1, "armed": False, "device": {"bus": 0, "address": 0}}
  assert s.write("C0RFid0001") == {"op": "written", "n": 1}
  assert s.io.written == ["C0RFid0001"]
  read = s.read()
  assert read["data"].startswith("C0RFid0001er00/00")
  assert s.write("P1VWid0002", timeout=2.5)["op"] == "written"
  assert s.end() == 0


def test_a_motion_write_is_refused_until_armed(served):
  s = served()
  hello_of(s)
  assert s.write("C0ASid0003") == {"op": "refused", "n": 1, "code": "not_armed", "command": "C0AS"}
  assert s.write("R0ZAid0004") == {"op": "refused", "n": 2, "code": "not_armed", "command": "R0ZA"}
  assert s.io.written == []
  assert s.ask("status")["armed"] is False
  assert s.ask("arm") == {"op": "armed", "n": 4}
  assert s.write("C0ASid0005")["op"] == "written"
  assert s.io.written == ["C0ASid0005"]
  assert s.ask("disarm")["op"] == "disarmed"
  assert s.write("C0ASid0006")["code"] == "not_armed"
  assert s.io.written == ["C0ASid0005"]
  assert s.end() == 0


def test_stop_and_release_are_unknown_ops(served):
  s = served()
  hello_of(s)
  s.ask("arm")
  for op in ("stop", "release"):
    assert s.ask(op, id="stop-1") == {"op": "refused", "n": s.counter, "code": "bad_request"}
  assert s.ask("status") == {"op": "status", "n": s.counter, "armed": True}  # an unknown op changes nothing
  assert s.write("C0ASid0001")["op"] == "written"
  s.end()


def test_the_command_line_has_serve_and_status_only(tmp_path):
  home = tmp_path / "home"
  assert cli(home, "status") == {"serving": None}
  env = {**os.environ, "AHC_BRIDGE_HOME": str(home)}
  for command in ("estop", "release"):
    out = subprocess.run([sys.executable, str(BRIDGE_PATH), command], capture_output=True, text=True, env=env,
                         stdin=subprocess.DEVNULL, timeout=30)
    assert out.returncode == 2 and out.stdout == ""
    assert "invalid choice" in out.stderr


def test_status_reports_the_serving_pid_and_a_second_serve_is_in_use(served, tmp_path):
  s = served()
  hello_of(s)
  assert cli(s.home, "status")["serving"] == os.getpid()
  second = served(home=s.home, io=FakeStarIO())
  fatal = second.next()
  assert fatal["op"] == "fatal" and fatal["code"] == "in_use"
  assert second.end() == 2
  assert second.io.is_setup is False and second.io.written == []
  assert s.write("C0RFid0001")["op"] == "written"  # the first is untouched
  assert s.end() == 0
  assert cli(s.home, "status")["serving"] is None


def test_bad_requests_are_refused_and_the_bridge_keeps_serving(served):
  s = served()
  hello_of(s)
  for line in [b"not json at all", b"[1, 2]", b'"op"', b"\xff\xfe\xfa", b"",
               b'{"op": "write", "n": "x", "data": "C0RF"}', b'{"op": "write", "n": true, "data": "C0RF"}']:
    s.send_line(line)
    assert s.next() == {"op": "refused", "n": None, "code": "bad_request"}
  s.send_line(b'{"n": 4}')  # no op: refused, with the n it carried
  assert s.next() == {"op": "refused", "n": 4, "code": "bad_request"}
  s.send_line(b'{"op": "nope", "n": 7}')
  assert s.next() == {"op": "refused", "n": 7, "code": "bad_request"}
  for fields in [{"data": 5}, {"data": "C0RF", "timeout": "soon"}, {"data": "C0RF", "timeout": -1},
                 {"data": "C0RFĀ"}, {}]:
    answer = s.ask("write", **fields)
    assert (answer["op"], answer["code"]) == ("refused", "bad_request")
  assert s.io.written == []
  assert s.write("C0RFid0001")["op"] == "written"
  assert s.end() == 0


def test_a_failed_write_is_reported_and_the_bridge_keeps_serving(served):
  class Broken(FakeStarIO):
    async def write(self, data, *args, **kwargs):
      raise OSError("pipe broke")

  s = served(io=Broken())
  hello_of(s)
  assert s.write("C0RFid0001") == {"op": "failed", "n": 1, "error": "OSError: pipe broke"}
  assert s.ask("status")["op"] == "status"
  s.end()


def test_end_of_stdin_closes_the_io_and_writes_nothing_to_it(served):
  s = served()
  hello_of(s)
  assert s.end() == 0
  assert s.io.stopped is True and s.io.written == []
  assert s.answers.get(timeout=WAIT_S) is None  # nothing more came out


def test_close_op_shuts_down_and_pending_requests_are_not_written(served):
  s = served()
  hello_of(s)
  s.send_line(b'{"op": "close"}')
  assert s.answers.get(timeout=WAIT_S) is None
  assert s.end() == 0
  assert s.io.stopped is True and s.io.written == []


def test_a_read_failure_is_fatal(served):
  s = served()
  hello_of(s)
  s.io.read_error = OSError("device gone")
  fatal = s.next()
  assert (fatal["op"], fatal["code"]) == ("fatal", "usb_read_failed")
  assert "device gone" in fatal["detail"]
  assert s.end() == 2
  assert s.io.stopped is True


def test_the_pump_forwards_what_the_instrument_sends_on_its_own(served):
  s = served()
  hello_of(s)
  s.io._answers.append("C0RFid9999er00/00unsolicited")
  assert s.read() == {"op": "read", "data": "C0RFid9999er00/00unsolicited"}
  s.end()


def test_a_failed_setup_is_fatal_usb_failed(served):
  io = FakeStarIO()
  io.setup_error = RuntimeError("USB device not found.")
  s = served(io=io)
  fatal = s.next()
  assert (fatal["op"], fatal["code"]) == ("fatal", "usb_failed")
  assert s.end() == 2 and io.written == []


# -- main: the real USB handle, replaced by a fake class ----------------------------------------

class FakeUSB(FakeStarIO):
  devices: list = []
  made: list = []
  setup_error_for_next: Exception | None = None

  def __init__(self, **kwargs):
    super().__init__()
    self.setup_error, FakeUSB.setup_error_for_next = FakeUSB.setup_error_for_next, None
    self.kwargs = kwargs
    FakeUSB.made.append(self)

  def get_available_devices(self):
    return list(FakeUSB.devices)


class FakeDevice:
  def __init__(self, bus, address):
    self.bus, self.address = bus, address


@pytest.fixture
def fake_usb(monkeypatch, tmp_path):
  import pylabrobot.io.usb as plr_usb

  FakeUSB.devices, FakeUSB.made = [], []
  monkeypatch.setattr(plr_usb, "USB", FakeUSB)
  monkeypatch.setattr(plr_usb, "USE_USB", True)
  monkeypatch.setenv("AHC_BRIDGE_HOME", str(tmp_path / "home"))
  return FakeUSB


def run_main(served, tmp_path):
  return served(run=lambda stdin, stdout: bridge.main(["serve"], stdin=stdin, stdout=stdout))


def test_main_builds_the_usb_handle_as_stardriver_does_and_says_hello(served, tmp_path, fake_usb, monkeypatch):
  fake_usb.devices = [FakeDevice(3, 7)]
  s = run_main(served, tmp_path)
  hello = hello_of(s)
  assert hello["device"] == {"bus": 3, "address": 7} and hello["armed"] is False
  # the same arguments STARDriver.__init__ builds its own USB handle with
  import pylabrobot.hamilton.star.driver.master as master

  seen = {}

  class Spy(FakeStarIO):
    def __init__(self, **kwargs):
      super().__init__()
      seen.update(kwargs)

  monkeypatch.setattr(master, "USB", Spy)
  master.STARDriver()
  assert fake_usb.made[0].kwargs == seen
  assert seen["id_vendor"] == bridge.ID_VENDOR and seen["id_product"] == bridge.ID_PRODUCT
  assert s.write("C0RFid0001")["op"] == "written" and fake_usb.made[0].written == ["C0RFid0001"]
  assert s.end() == 0


@pytest.mark.parametrize("devices, code", [([], "no_device"), ([FakeDevice(1, 2), FakeDevice(1, 3)], "several_devices")])
def test_main_needs_exactly_one_instrument(served, tmp_path, fake_usb, devices, code):
  fake_usb.devices = devices
  s = run_main(served, tmp_path)
  fatal = s.next()
  assert (fatal["op"], fatal["code"]) == ("fatal", code)
  assert s.end() == 2
  assert fake_usb.made[0].is_setup is False and fake_usb.made[0].written == []


def test_main_reports_a_usb_failure(served, tmp_path, fake_usb):
  fake_usb.devices = [FakeDevice(1, 2)]
  fake_usb.setup_error_for_next = RuntimeError("Access denied")
  s = run_main(served, tmp_path)
  fatal = s.next()
  assert (fatal["op"], fatal["code"]) == ("fatal", "usb_failed") and "Access denied" in fatal["detail"]
  assert s.end() == 2


def test_the_lock_is_taken_before_the_usb_is_looked_at(served, tmp_path, fake_usb):
  fake_usb.devices = [FakeDevice(1, 2)]
  first = served(home=tmp_path / "home")
  hello_of(first)
  second = run_main(served, tmp_path)
  fatal = second.next()
  assert (fatal["op"], fatal["code"]) == ("fatal", "in_use")
  assert second.end() == 2
  first.end()


# -- a real process -----------------------------------------------------------------------------

def test_sigterm_and_sighup_end_a_spawned_bridge_cleanly(tmp_path):
  for sig in (signal.SIGTERM, signal.SIGHUP):
    folder = tmp_path / sig.name
    launcher = write_launcher(folder, BRIDGE_PATH)
    env = {**os.environ, "AHC_BRIDGE_HOME": str(folder / "home")}
    proc = subprocess.Popen([sys.executable, str(launcher["script"])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    try:
      hello = json.loads(proc.stdout.readline())
      assert hello["op"] == "hello" and hello["armed"] is False
      proc.send_signal(sig)
      assert proc.wait(timeout=WAIT_S) == 0
      assert proc.stdout.read() == b""  # nothing after the hello
    finally:
      if proc.poll() is None:
        proc.kill()
        proc.wait()
      for stream in (proc.stdin, proc.stdout, proc.stderr):
        stream.close()
    assert launcher["log"].exists() is False  # nothing was ever written to the instrument


def test_nothing_but_json_lines_reach_stdout(tmp_path):
  folder = tmp_path / "run"
  launcher = write_launcher(folder, BRIDGE_PATH)
  env = {**os.environ, "AHC_BRIDGE_HOME": str(folder / "home")}
  proc = subprocess.Popen([sys.executable, str(launcher["script"])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env)
  try:
    proc.stdin.write(b'{"op":"status","n":1}\ngarbage\n')
    proc.stdin.flush()
    lines = [json.loads(proc.stdout.readline()) for _ in range(3)]
    proc.stdin.close()  # end of input
    assert proc.wait(timeout=WAIT_S) == 0
    assert proc.stdout.read() == b""
  finally:
    if proc.poll() is None:
      proc.kill()
      proc.wait()
    for stream in (proc.stdout, proc.stderr):
      stream.close()
  assert [line["op"] for line in lines] == ["hello", "status", "refused"]
