"""The instrument for the STARlet adapter's tests: PyLabRobot's simulator, with the real guard on top of it.

Two stand-ins for `starlet.open_instrument`, both built on `STARSimulationDriver` (PyLabRobot's recorded STARlet, serial
`T0001`) behind `guard_driver`, and both with PyLabRobot's `stop()` replaced as the real factory does:

- `sim_instrument(...)`: a `FakeBridge` stands where the bridge handle is, and the bridge's own rule (a motion command
  needs an armed bridge) is enforced under the guard, so a test shows both layers. A command
  can be made to fail, hang or raise there. No process, no transport.
- `shadow_instrument(tmp_path, monkeypatch, ...)`: the strongest set-up. Every command that passes the guard is first
  written, as `MMCCid####`, by PyLabRobot's own `ReplyRouter` through a real `GuardedIO(BridgeIO(...))` to a spawned
  process (part 1's launcher: the real `star_bridge.py` serving a `FakeStarIO`), and its reply is read back, before the
  simulator answers it. So the bridge's arming rule, the in-flight tracking, the reply routing and the
  instrument's log of what reached it are the real code. `hold(code)` plays a command in flight.

Nothing here reaches ssh, USB or a network.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from pylabrobot.hamilton.protocol.text.framing import assemble_channel_command
from pylabrobot.hamilton.star import STARlet
from pylabrobot.hamilton.star.device import RECORDING_STARLET
from pylabrobot.hamilton.star.driver.master import STARDriver
from pylabrobot.hamilton.star.driver.simulator import STARSimulationDriver

from ahc.devices.hamilton import link, starlet
from ahc.devices.hamilton.link import BridgeIO, BridgeLost, BridgeRefused, GuardedIO, GuardState, guard_driver
from ahc.devices.hamilton.star_bridge import __file__ as BRIDGE_FILE
from ahc.devices.hamilton.star_bridge import is_read_only
from fake_star import steer, write_launcher, written

BRIDGE_PATH = Path(BRIDGE_FILE)
SERIAL = "T0001"
_TRANSPORT_KEYWORDS = ("auto_id", "tip_pattern", "write_timeout", "read_timeout", "fmt")  # not firmware parameters
SHADOW_READ_TIMEOUT_S = 30  # a command whose reply never comes ends here, if nothing ends it sooner


class FakeBridge:
  """Stands where `BridgeIO` is when no process is wanted. Follows the bridge's rules."""

  def __init__(self):
    self.armed = False
    self.lost: str | None = None
    self.on_lost = None
    self.closed = False
    self.calls: list[tuple] = []

  @property
  def hello(self) -> dict[str, Any]:
    return {"op": "hello", "protocol": 1, "armed": False, "device": {"bus": 1, "address": 2}}

  def _live(self) -> None:
    if self.lost is not None:
      raise BridgeLost(self.lost)

  async def arm(self) -> dict[str, Any]:
    self.calls.append(("arm",))
    self._live()
    self.armed = True
    return {"op": "armed"}

  async def disarm(self) -> dict[str, Any]:
    self.calls.append(("disarm",))
    self._live()
    self.armed = False
    return {"op": "disarmed"}

  async def status(self) -> dict[str, Any]:
    self.calls.append(("status",))
    self._live()
    return {"op": "status", "armed": self.armed}

  async def stop(self) -> None:
    self.calls.append(("stop",))
    self.closed = True

  def lose(self, reason: str = "the bridge process ended") -> None:
    """Play a lost link: `lost` is set and `on_lost` is called from another thread, as the real one does."""
    self.lost = reason
    callback = self.on_lost
    if callback is not None:
      thread = threading.Thread(target=callback, args=(reason,))
      thread.start()
      thread.join(5)


def simulated_driver(deck, tips_mounted: list[bool] | None, initialized: bool, serial: str,
                     channel_width: float | None = None, raster_pitch: float | None = None,
                     no_raster_pitch: bool = False) -> STARSimulationDriver:
  """`channel_width` is what every simulated channel answers to `VY` (the width PyLabRobot plans with); `raster_pitch`
  is the minimum pitch the simulated master reports (`C0QM`), `no_raster_pitch` makes it report none. All of them set
  before discovery; left out, the recording's own values stay."""
  driver = STARSimulationDriver(deck=deck, tips_mounted=tips_mounted, initialized=initialized,
                                declared_configuration_json=RECORDING_STARLET)
  driver.simulated_configuration.serial_number = serial  # made-up: the recording's own is not used
  if channel_width is not None:
    for channel in driver.simulated_pipettes.channels:
      channel.width = channel_width
  if raster_pitch is not None:
    driver.simulated_configuration.min_raster_pitch_pip_channels = raster_pitch
  if no_raster_pitch:
    driver.simulated_configuration.min_raster_pitch_pip_channels = None
  return driver


def release_started_with_tips(driver: STARSimulationDriver, module: str, command: str, kwargs: dict[str, Any]) -> None:
  """A tip drop (`C0TR`) that went through lets go of the tips an instrument was started with (`tips_mounted`), as a real
  instrument's channels do: PyLabRobot's simulator clears those only when it initializes, never for a drop."""
  if (module, command) != ("C0", "TR"):
    return
  pattern = kwargs.get("tip_pattern")
  for channel in range(len(driver.tips_mounted)):
    if pattern is None or (channel < len(pattern) and pattern[channel]):
      driver.tips_mounted[channel] = False


async def _no_stop() -> None:
  """What the real factory puts in place of PyLabRobot's stop(): it moves, and it closes the link."""


class _Opener:
  """What the two stand-ins share: a callable with `starlet.open_instrument`'s signature, and what it built."""

  def __init__(self, tips_mounted: list[bool] | None = None, initialized: bool = False, serial: str = SERIAL,
               channel_width: float | None = None, raster_pitch: float | None = None, no_raster_pitch: bool = False,
               carry_tips: bool = False):
    """`carry_tips`: the instrument outlives a link. What its channels hold (picked up, returned) and whether it reports
    itself initialized is what the next link opened finds, as with a real instrument; left out, every link starts from
    `tips_mounted` and `initialized` as given."""
    self.tips_mounted, self.initialized, self.serial = tips_mounted, initialized, serial
    self.carry_tips = carry_tips
    self.channel_width, self.raster_pitch, self.no_raster_pitch = channel_width, raster_pitch, no_raster_pitch
    self.opened = 0
    self.devices: list[Any] = []
    self.bridges: list[Any] = []
    self.states: list[GuardState] = []

  def _driver(self, deck) -> STARSimulationDriver:
    return simulated_driver(deck, self.tips_mounted, self.initialized, self.serial, self.channel_width,
                            self.raster_pitch, self.no_raster_pitch)

  def _track(self, driver, module: str, command: str, kwargs: dict[str, Any]) -> None:
    """After a command went through the simulator: its tips are released by a drop, and a carrying instrument keeps what
    its channels hold and its initialization for the next link."""
    release_started_with_tips(driver, module, command, kwargs)
    if not self.carry_tips:
      return
    channels = len(driver.tips_mounted)
    held = self.tips_mounted = list(self.tips_mounted or [False] * channels)
    pattern = kwargs.get("tip_pattern") or [True] * channels
    if (module, command) == ("C0", "TP"):
      for channel in range(channels):
        held[channel] = held[channel] or bool(pattern[channel])
    elif (module, command) == ("C0", "TR"):
      for channel in range(channels):
        held[channel] = held[channel] and not pattern[channel]
    elif command == "DI":
      held[:] = [False] * channels  # initializing ejects what the channels hold
    self.initialized = bool(driver.initialized.get("C0"))

  @property
  def device(self):
    return self.devices[-1]

  @property
  def driver(self):
    return self.devices[-1].driver

  @property
  def bridge(self):
    return self.bridges[-1]

  @property
  def state(self) -> GuardState:
    return self.states[-1]

  def _built(self, device, bridge, state) -> tuple[Any, Any, GuardState]:
    self.opened += 1
    self.devices.append(device)
    self.bridges.append(bridge)
    self.states.append(state)
    return device, bridge, state


# -- the simulator behind a fake bridge ---------------------------------------------------------

class SimInstrument(_Opener):
  def __init__(self, **kwargs: Any):
    super().__init__(**kwargs)
    self.reached: list[str] = []  # commands that got past the bridge's rule to the simulator
    self._raise: dict[str, BaseException] = {}
    self._hang: dict[str, asyncio.Event] = {}

  # -- steering: placed underneath the guard ----------------------------------------------------

  def fail(self, code: str, error: BaseException | None = None) -> None:
    """Make that command code raise `error` (a firmware error by default) when it reaches the instrument."""
    self._raise[code] = error if error is not None else RuntimeError(f"{code} failed on the instrument")

  def hang(self, code: str) -> None:
    """Hold that command code at the instrument until `release`."""
    self._hang[code] = asyncio.Event()

  def release(self, code: str) -> None:
    event = self._hang.pop(code, None)
    if event is not None:
      event.set()

  def moved(self) -> list[str]:
    """The commands that move which reached the simulator."""
    return [code for code in self.reached if not is_read_only(code)]

  # -- the replacement for starlet.open_instrument ----------------------------------------------

  def __call__(self, options: dict, deck) -> tuple[Any, Any, GuardState]:
    bridge, state = FakeBridge(), GuardState()
    driver = self._driver(deck)
    real_send, real_raw = driver._send, driver.send_raw_command

    async def reach(text: str) -> None:
      if bridge.lost is not None:
        raise BridgeLost(bridge.lost)
      if not is_read_only(text) and not bridge.armed:  # the bridge's own rule: a command that moves needs it armed
        raise BridgeRefused("not_armed", text[:4])
      self.reached.append(text[:4])
      if text[:4] in self._raise:
        raise self._raise[text[:4]]
      event = self._hang.get(text[:4])
      if event is not None:
        await event.wait()

    async def _send(module: str, command: str, *args: Any, **kwargs: Any) -> Any:
      await reach(f"{module}{command}")
      reply = await real_send(module, command, *args, **kwargs)
      self._track(driver, module, command, kwargs)
      return reply

    async def send_raw_command(command: str, *args: Any, **kwargs: Any) -> Any:
      await reach(command)
      return await real_raw(command, *args, **kwargs)

    async def _close() -> None:
      await bridge.stop()

    driver._send, driver.send_raw_command, driver._close = _send, send_raw_command, _close
    guard_driver(driver, state)
    driver.stop = _no_stop
    return self._built(STARlet(deck=deck, driver=driver), bridge, state)


def sim_instrument(tips_mounted: list[bool] | None = None, initialized: bool = False, serial: str = SERIAL,
                   **instrument: Any) -> SimInstrument:
  """A replacement for `starlet.open_instrument`: `monkeypatch.setattr(starlet, "open_instrument", sim)`. `instrument`
  may carry `channel_width`, `raster_pitch` and `no_raster_pitch` (see `simulated_driver`) and `carry_tips`."""
  return SimInstrument(tips_mounted=tips_mounted, initialized=initialized, serial=serial, **instrument)


# -- the simulator behind the real transport ----------------------------------------------------

class ShadowInstrument(_Opener):
  def __init__(self, tmp_path: Path, monkeypatch, hold: tuple[str, ...] = (), fail: dict[str, str] | None = None,
               **kwargs: Any):
    super().__init__(**kwargs)
    self.home = tmp_path / "bridge-home"
    self.launcher = write_launcher(tmp_path / "fake", BRIDGE_PATH, hold, fail)
    self.folder = self.launcher["release_dir"]
    self.procs: list[subprocess.Popen] = []
    monkeypatch.setattr(link, "spawn", self._spawn)
    monkeypatch.setattr(starlet, "open_instrument", self)

  def _spawn(self, argv: list[str]) -> subprocess.Popen:
    assert argv[0] == "ssh" and argv[-1].endswith(" serve")  # what AHC would have run; here it is not
    env = {**os.environ, "AHC_BRIDGE_HOME": str(self.home)}
    proc = subprocess.Popen([sys.executable, str(self.launcher["script"])], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    self.procs.append(proc)
    return proc

  # -- what reached the instrument ---------------------------------------------------------------

  def written(self) -> list[str]:
    return written(self.launcher["log"])

  def codes(self) -> list[str]:
    return [text[:4] for text in self.written()]

  def sent(self, code: str) -> list[str]:
    """The full text of every command with that code the instrument was written, parameters included."""
    return [text for text in self.written() if text[:4] == code]

  def moved(self) -> list[str]:
    """The commands that move which the instrument was written, in order."""
    return [code for code in self.codes() if not is_read_only(code)]

  # -- steering the instrument while it runs ----------------------------------------------------

  def hold(self, code: str) -> None:
    steer(self.folder, "hold", code)

  def release(self, code: str) -> None:
    steer(self.folder, "release", code)

  def fail(self, code: str, error: str = "er01/00") -> None:
    steer(self.folder, "fail", code, error)

  def kill(self) -> None:
    for proc in self.procs:
      proc.kill()

  def close(self) -> None:
    for proc in self.procs:
      if proc.poll() is None:
        proc.kill()
      proc.wait(timeout=10)
      for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
          stream.close()
        except OSError:
          pass

  # -- the replacement for starlet.open_instrument ----------------------------------------------

  def __call__(self, options: dict, deck) -> tuple[Any, Any, GuardState]:
    state = GuardState()
    bridge = BridgeIO(options["bridge"], options.get("bridge_python") or starlet.DEFAULT_PYTHON,
                      options.get("bridge_script") or starlet.DEFAULT_SCRIPT, start_timeout=15)
    gio = GuardedIO(bridge, state)
    driver = self._driver(deck)
    real_send, real_raw = driver._send, driver.send_raw_command
    driver.io = driver._replies.io = gio  # the router PyLabRobot's STARDriver has, over the real transport

    async def _send(module: str, command: str, *args: Any, **kwargs: Any) -> Any:
      ident = driver._replies.next_id()
      params = {k: v for k, v in kwargs.items() if k not in _TRANSPORT_KEYWORDS}
      carries_a_list = any(isinstance(v, list) for v in params.values())
      text = assemble_channel_command(module, command, kwargs.get("tip_pattern"),
                                      driver.num_channels if carries_a_list else 0, id_=ident, **params)
      await driver._replies.send(text, ident, read_timeout=SHADOW_READ_TIMEOUT_S)  # the whole command, as PyLabRobot's
      reply = await real_send(module, command, *args, **kwargs)
      self._track(driver, module, command, kwargs)
      return reply

    async def send_raw_command(command: str, *args: Any, **kwargs: Any) -> Any:
      await driver._replies.send_raw(command, read_timeout=SHADOW_READ_TIMEOUT_S)
      return await real_raw(command, *args, **kwargs)

    async def _open() -> None:
      await STARDriver._open(driver)  # opens the transport and starts the router's reader, as a real driver does

    async def _close() -> None:
      await STARDriver._close(driver)

    driver._send, driver.send_raw_command, driver._open, driver._close = _send, send_raw_command, _open, _close
    guard_driver(driver, state)
    driver.stop = _no_stop
    return self._built(STARlet(deck=deck, driver=driver), bridge, state)


def shadow_instrument(tmp_path: Path, monkeypatch, hold: tuple[str, ...] = (), fail: dict[str, str] | None = None,
                      tips_mounted: list[bool] | None = None, initialized: bool = False,
                      serial: str = SERIAL, **instrument: Any) -> ShadowInstrument:
  """The strongest set-up: replaces `link.spawn` and `starlet.open_instrument`. Call `.close()` when done. `instrument`
  may carry `channel_width`, `raster_pitch` and `no_raster_pitch` (see `simulated_driver`) and `carry_tips`."""
  return ShadowInstrument(tmp_path, monkeypatch, hold=hold, fail=fail, tips_mounted=tips_mounted,
                          initialized=initialized, serial=serial, **instrument)
