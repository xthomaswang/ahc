"""Hamilton STARlet: components mapped to PyLabRobot's STAR driver, general ops translated."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from typing import Any

import pylabrobot.resources as plr
from pylabrobot.hamilton.star.liquid_classes.mapping import get_star_liquid_class
from pylabrobot.lib.liquid_handling.mix import Mix
from pylabrobot.resources import Container, Coordinate, TipSpot
from pylabrobot.resources.liquid import Liquid

from ahc.core.errors import LabError
from ahc.core.layout import Layout
from ahc.devices.hamilton.brand import HamiltonAdapter
from ahc.devices.hamilton.link import (
  DEFAULT_PYTHON, DEFAULT_SCRIPT, BridgeIO, GuardedIO, GuardState, fail_waiting, guard_driver, listen_for_replies,
  ssh_argv,
)
from ahc.devices.spec import Component

logger = logging.getLogger(__name__)

HALT_WAIT_S = 30.0  # how long the emergency stop waits for a command in flight to be answered

# Where initialization takes the 96 head to eject: VENUS's position of this STARlet's CO-RE 96 slide waste
# (`C0EI xs00420 xd1 yh1203`), as X and Y in mm. AHC does not use the 96 head, so it keeps it at its own travel height
# there (VENUS goes down to 229.0) instead of lowering it. PyLabRobot's default (-46.5, 178.8, 216.4) is not this
# instrument's waste.
HEAD96_EJECT_X = -42.0
HEAD96_EJECT_Y = 120.3
# What a tip recovery sets up: the instrument is up already, so only its safety raise runs (the channels to Z safety and
# the 96 head retracted); never the initialization of the channels, which ejects what they hold.
RECOVERY_SETUP = {"skip_device_initialization": True, "skip_pipettes": True, "skip_iswap": True, "skip_head96": True,
                  "skip_head384": True, "skip_autoload": True}
MIN_CHANNEL_SPACING_MM = 9.0  # the pitch of a plate's rows: AHC plans the channels no closer, whoever states the spacing

# PyLabRobot's STAR model refuses a dispense its piston cannot make; said as what to change instead.
_PISTON = re.compile(r"piston holds ([\d.]+) uL of travel for the ([\d.]+) uL")


async def _piston_checked(call):
  try:
    return await call
  except ValueError as exc:
    m = _PISTON.search(str(exc))
    if m is None:
      raise
    have, need = float(m.group(1)), float(m.group(2))
    raise LabError("piston_travel",
                   f"the channel's piston has {have:g} uL of travel left and this dispense needs {need:g} uL: on the "
                   "STAR a dispense also pushes out the tip's transport air and a liquid-class correction.",
                   f"When one aspiration feeds several dispenses, aspirate about {need - have + 5:.0f} uL more than "
                   "they add up to and leave that surplus in the tip (it goes with the tip), or aspirate once per "
                   "dispense.") from exc


def _cancelled_stays_cancelled(exc: BaseException) -> None:
  """A call the emergency stop cancelled ends as cancelled. PyLabRobot's own cleanup after an interrupted command
  sends a retract; the closed guard refuses it, and that refusal would replace the cancellation, so the server would
  report a device error where the stop interrupted the call."""
  task = asyncio.current_task()
  if task is not None and task.cancelling() and not isinstance(exc, asyncio.CancelledError):
    raise asyncio.CancelledError() from exc


def open_instrument(options: dict, deck) -> tuple[Any, Any, GuardState]:
  """The PyLabRobot device for a real STARlet reached through the bridge, its bridge handle and its guard. Nothing is
  opened yet. Tests replace this."""
  from pylabrobot.hamilton.star import STARlet
  from pylabrobot.hamilton.star.driver.master import STARDriver

  state = GuardState()
  bridge = BridgeIO(options["bridge"], options.get("bridge_python") or DEFAULT_PYTHON,
                    options.get("bridge_script") or DEFAULT_SCRIPT)
  driver = STARDriver(deck=deck, io=GuardedIO(bridge, state))
  guard_driver(driver, state)

  async def no_stop() -> None:
    # PyLabRobot's stop() moves the instrument and closes the link AHC still needs to hear a command in flight.
    logger.debug("PyLabRobot's stop() was not run: AHC never moves a real instrument to let go of it")

  driver.stop = no_stop
  return STARlet(deck=deck, driver=driver), bridge, state


class _Link:
  """The three objects of one link to a real instrument, with what AHC placed on its deck and what discovery read."""

  def __init__(self, device: Any, bridge: Any, state: GuardState):
    self.device, self.bridge, self.state = device, bridge, state
    self.placed: list[Any] = []  # carriers AHC put on this link's deck
    self.identity: dict[str, Any] = {}
    self.counted = False  # its moved commands are in the adapter's running total

  @property
  def driver(self) -> Any:
    return self.device.driver


def _read_identity(driver, options: dict) -> dict[str, Any]:
  """What discover() read. PyLabRobot reads four characters of the serial."""
  config = driver.configuration
  arms = driver.arms
  return {
    "serial": config.serial_number, "firmware": config.firmware_version,
    "firmware_date": str(config.firmware_date) if config.firmware_date is not None else None,
    "channels": config.num_pip_channels,
    "fitted": {"head96": any(a.head96 is not None for a in arms), "iswap": any(a.iswap is not None for a in arms),
               "autoload": driver.autoload is not None},
    "usb_host": options.get("bridge"),
  }


async def _link_only_close(driver) -> None:
  """Let go of the link without moving the instrument: what waits is failed, the reader stops, the bridge closes."""
  try:
    fail_waiting(driver, "link closed")
  except Exception:  # noqa: BLE001
    pass
  driver._connected = False
  try:
    await driver._close()
  except Exception:  # noqa: BLE001 - best effort
    logger.debug("closing the link failed", exc_info=True)


# Specialized parameter -> PyLabRobot STAR keyword, one value per channel.
_PER_CHANNEL = {
  "jet": "jet",
  "blow_out": "blow_out",
  "pre_wetting_volume_ul": "pre_wetting_volumes",
  "settling_time_s": "settling_times",
  "transport_air_volume_ul": "transport_air_volumes",
  "swap_speed_mm_s": "swap_speeds",
}


class STARletAdapter(HamiltonAdapter):
  """A STARlet driven through PyLabRobot 1.0: its simulator (`sim`), or a real instrument through the bridge on its
  USB host (`usb`)."""

  OPTIONS = {"sim": frozenset(),
             "usb": frozenset({"bridge", "bridge_python", "bridge_script", "serial", "tip_waste",
                                  "channel_spacing_mm"})}
  IDENTITY_OPTION = "serial"
  IDENTITY_FIELD = "serial"
  LOAD_MOVES = "initializes the instrument (its channels, 96-head, iSWAP and autoload all move)"
  LOAD_NEEDS = "hands and deck are clear and they are within reach of the instrument's power switch"
  HALTED = "nothing more is sent to the instrument and no command is in flight"
  IDENTITY_SAYS = ("serial, firmware, channels and fitted parts, whether it is initialized, the tips its sensor reports "
                   "and its USB host")
  PROBE_NEEDS = ("configure_devices reads the instrument's serial before it writes the config, so the instrument must "
                 "be on and its USB host reachable over ssh; ask the person.")
  STOPPED = "The emergency stop reached the instrument: nothing more is sent to it, and no command is in flight"
  STOP_UNCONFIRMED = ("The emergency stop keeps AHC from sending more, but a command sent before it has not been "
                      "answered: the instrument may still be executing it, and the power switch is the immediate stop")
  FORGETS_TIPS = False  # the channels hold their tips whatever AHC knows
  LET_GO = ("This server let go of the instrument: its link is closed and nothing was moved, so its channels stay where "
            "they were.")
  EXCLUSIVE_LINK = True
  STOP_NOTE = ("On a real STARlet the stop keeps AHC from sending anything more; a command the instrument is already "
               "running finishes. To stop motion at once, switch the instrument off at its power switch.")
  POWER_SWITCH = ("; to stop motion at once, use the instrument's power switch (AHC's stop lets a command already "
                  "running finish)")  # ends what the person confirms for a real entry
  MIX_FLOW_RATE = 100.0  # uL/s: PyLabRobot's mix needs one, so AHC chooses it when a call gives none

  _link: _Link | None = None
  _moved_total = 0  # commands that move, sent over links already let go of
  halted_text: str | None = None  # what the stop did, when it is not HALTED as it stands

  @classmethod
  def _refuse_real(cls, backend: str) -> None:
    if backend not in cls.OPTIONS:
      raise LabError("backend_not_implemented", f"the STARlet's {backend!r} backend is not implemented.",
                     "Use backend 'sim', or 'usb' for a real STARlet through a USB host.")

  @classmethod
  def check_options(cls, backend: str, options: dict[str, Any]) -> None:
    cls._refuse_real(backend)
    super().check_options(backend, options)
    if backend != "usb":
      return
    if not options.get("bridge"):
      raise LabError("bridge_required", "a usb entry needs the ssh name of the USB host the instrument is plugged into.",
                     "Set options.bridge to that host's ssh name (a name from the ssh config, or user@host).")
    for key in ("bridge", "bridge_python", "bridge_script"):
      if key in options and not isinstance(options[key], str):
        raise LabError("bad_option", f"{key} must be text, got {options[key]!r}.")
    ssh_argv(options["bridge"], options.get("bridge_python") or DEFAULT_PYTHON,
             options.get("bridge_script") or DEFAULT_SCRIPT)
    if "serial" in options and not (isinstance(options["serial"], str) and options["serial"].strip()):
      raise LabError("bad_option", f"serial must be a non-empty string, got {options['serial']!r}.")
    if "tip_waste" in options and not isinstance(options["tip_waste"], bool):
      raise LabError("bad_option", f"tip_waste must be true or false, got {options['tip_waste']!r}.")
    if "channel_spacing_mm" in options:
      spacing = options["channel_spacing_mm"]
      if (isinstance(spacing, bool) or not isinstance(spacing, (int, float)) or not math.isfinite(spacing)
              or spacing < MIN_CHANNEL_SPACING_MM):
        raise LabError("bad_option", f"channel_spacing_mm must be a number of at least {MIN_CHANNEL_SPACING_MM:g} (mm "
                       f"between neighbouring channels), got {spacing!r}.")

  @classmethod
  def named(cls, entry_id: str, model: str, options: dict[str, Any]) -> str:
    via = f" through the USB host {options.get('bridge')!r}" if options.get("bridge") else ""
    return super().named(entry_id, model, options) + via

  @classmethod
  def go_ahead_why(cls, named: str) -> str:
    return f"load_layout initializes the real instrument {named}: every arm moves"

  @classmethod
  def describe_entry(cls, options: dict[str, Any]) -> str:
    try:
      serial = cls.pinned(options)
      who = f"instrument {serial!r}" if serial else "instrument (serial not read)"
      via = f" through the USB host {options.get('bridge')!r}" if options.get("bridge") else ""
      waste = ", tip waste fitted" if options.get("tip_waste") is True else ", no tip waste (tips go back to their rack)"
      spacing = options.get("channel_spacing_mm")
      planned = (f", channels planned {spacing:g} mm apart as the person states (not the width the channels report)"
                 if spacing is not None else "")
      return f", {who}{via}{waste}{planned}{cls.POWER_SWITCH}"
    except Exception:  # noqa: BLE001 - a description never raises
      return f", instrument (serial not read){cls.POWER_SWITCH}"

  def reaches(self, options: dict[str, Any]) -> bool:
    """The same USB host, and the same bridge there (its defaults counted)."""
    if self._sim or not options.get("bridge"):
      return False
    mine, theirs = self.options, options
    return (theirs["bridge"] == mine.get("bridge")
            and (theirs.get("bridge_python") or DEFAULT_PYTHON) == (mine.get("bridge_python") or DEFAULT_PYTHON)
            and (theirs.get("bridge_script") or DEFAULT_SCRIPT) == (mine.get("bridge_script") or DEFAULT_SCRIPT))

  @property
  def _sim(self) -> bool:
    """Whether this adapter runs PyLabRobot's simulator: the adapter's own code path, decided by its backend alone (the
    public `simulated` is what the rest of the system is told)."""
    return self.backend == "sim"

  @property
  def has_stop(self) -> bool:
    return not self.simulated

  # -- probing -----------------------------------------------------------------------------------

  @classmethod
  def probe_identity(cls, backend: str, options: dict[str, Any]) -> dict[str, Any]:
    """Who answers on the USB host, before the person confirms the entry: reads only, the bridge is never armed, so
    nothing can move. Blocking: it runs an event loop of its own."""
    if backend != "usb":
      return {}
    found = asyncio.run(cls._probe(options))  # the link is closed when this returns
    cls.check_identity(found, options)
    return found

  @classmethod
  def check_identity(cls, identity: dict[str, Any], options: dict[str, Any]) -> None:
    pinned, serial = cls.pinned(options), identity.get("serial")
    if pinned and serial != pinned:
      raise LabError("wrong_robot", f"the instrument on the USB host {options.get('bridge')!r} answers the serial "
                     f"{serial!r}, not {pinned!r}; nothing was written.",
                     "Check with the person which instrument is plugged into that host.")

  @staticmethod
  async def _probe(options: dict[str, Any]) -> dict[str, Any]:
    if not options.get("bridge"):
      raise LabError("bridge_required", "a usb entry needs the ssh name of the USB host the instrument is plugged into.")
    device, _bridge, _state = open_instrument(options, plr.STARLetDeck())
    driver = device.driver
    try:
      await driver._open()
      driver._connected = True
      await driver.discover()
      identity = _read_identity(driver, options)
      initialized = bool(await driver.request_initialization_status())
      tips = [int(t) for t in await driver.pipettes.sense_tip_presence()]
    except LabError:
      raise
    except Exception as exc:  # noqa: BLE001
      raise LabError("backend_unreachable", f"reading the instrument failed: {type(exc).__name__}: {exc}.",
                     "Check the USB host, the instrument's power and its USB cable.") from exc
    finally:
      await _link_only_close(driver)
    if not identity["serial"]:
      raise LabError("identity_unreadable", "the instrument did not answer its serial number; nothing was written.",
                     "Check that the instrument is switched on and idle, then configure the device again.")
    return {"serial": identity["serial"], "firmware": identity["firmware"],
            "firmware_date": identity["firmware_date"], "channels": identity["channels"], "fitted": identity["fitted"],
            "initialized": initialized, "tips_sensed": tips, "usb_host": options["bridge"]}

  # -- lifecycle ---------------------------------------------------------------------------------

  async def start(self) -> None:
    self._refuse_real(self.backend)
    await super().start()
    self.device = None
    self._link = None
    self._moved_total = 0

  async def stop(self) -> None:
    if not self._sim:
      # Never device.stop(): it moves the instrument. AHC lets go of a real instrument without moving it.
      link, self._link, self.device = self._link, None, None
      if link is not None:
        await self._let_go(link)
      return
    if self.device is not None:
      await self.device.stop()
      self.device = None

  async def _let_go(self, link: _Link) -> None:
    """Link-only close, best effort and never raising: disarm the bridge, fail what waits, close. Moves nothing."""
    if self._link is link:
      self._link, self.device = None, None
    if not link.counted:
      link.counted = True
      self._moved_total += link.state.moved
    link.state.latch("the link was closed")
    try:
      await link.bridge.disarm()
    except Exception:  # noqa: BLE001 - a lost link cannot be disarmed; closing it is the same
      pass
    await _link_only_close(link.driver)

  def _close_adapter(self, link: _Link, reason: str, op: str = "the instrument") -> None:
    """The adapter stops trusting the instrument's state for a reason other than the emergency stop: motion closes, and
    what the channels hold is unconfirmed (the person checks the tips before the next load). Plain assignments only:
    it also runs on the bridge's reader thread."""
    link.state.latch(reason)
    if self.halted or self.halt_requested or self.outcome_unknown or link.state.moved <= 0:
      return  # a stop or an earlier unknown outcome says so already, or nothing ever moved
    self.outcome_unknown = {
      "op": op, "error": reason, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
      "says": f"the instrument takes no more motion commands ({reason}), so what its channels hold is unconfirmed"}

  def mark_outcome_unknown(self, op: str, error: str, call: dict[str, Any] | None = None) -> None:
    if self._link is not None:
      self._link.state.latch(f"{op} did not complete")
    super().mark_outcome_unknown(op, error, call)

  def _on_lost(self, link: _Link, reason: str) -> None:
    """The bridge's reader thread: the link to the USB host is gone."""
    text = f"the link to the USB host was lost: {reason}"
    link.state.latch(text)
    try:
      fail_waiting(link.driver, reason)
    except Exception:  # noqa: BLE001
      pass
    if self._link is link:
      self._close_adapter(link, text)

  async def halt(self, stop: dict[str, Any] | None = None) -> bool:
    """The emergency stop: nothing more is sent, the bridge is disarmed over the open link, and what was in flight is
    heard to finish. The instrument has no command that aborts one in flight; its power switch is the immediate stop."""
    link = self._link
    if self._sim or link is None:
      return False
    # Before the first await: the server cancels the running call right after scheduling this, and its cleanup must
    # find the guard closed.
    link.state.latch("emergency stop")
    self.halt_requested = True
    failure = None
    try:
      await link.bridge.disarm()
    except Exception as exc:  # noqa: BLE001
      failure = f"{type(exc).__name__}: {exc}"
    left = await listen_for_replies(link.driver, link.driver.io, link.state, HALT_WAIT_S, lambda: link.bridge.lost)
    if self._link is not link:  # a newer link replaced this one meanwhile: its flags are not ours to touch
      return True
    if left:
      raise LabError("halt_unconfirmed",
                     f"{', '.join(left)} was sent before the stop and has not been answered after {HALT_WAIT_S:g} s: "
                     "the instrument may still be executing it. Nothing more is sent.",
                     "The power switch is the immediate stop.")
    self.halted = True
    self.halted_text = (None if failure is None else
                        f"{self.HALTED}; the bridge on the USB host did not confirm closing to motion ({failure}), but "
                        "AHC's own guard is closed either way")
    return True

  def robot_record(self, n: int) -> list[dict[str, Any]] | None:
    return self._link.state.record(n) if self._link is not None else None

  def command_count(self) -> int:
    if self._sim:
      return super().command_count()
    return self._moved_total + (self._link.state.moved if self._link is not None and not self._link.counted else 0)

  def identity(self) -> dict[str, Any]:
    return dict(self._link.identity) if self._link is not None else {}

  # -- layout ------------------------------------------------------------------------------------

  async def load_layout(self, layout: dict[str, Any]) -> Layout:
    if not self._sim:
      return await self._load_usb(layout)
    if self.device is not None and self.mounted_tips(self.spec.components["pip"]):
      raise LabError("tips_mounted", "cannot change the layout with tips on the channels.",
                     "Call drop_tips first.")
    from pylabrobot.hamilton.star import STARlet

    deck = plr.STARLetDeck()
    new_layout = self._build_deck(deck, layout)
    await self.stop()
    self.layout = None  # the old deck is gone
    device = STARlet(deck=deck, simulation=True)
    self._count_commands(device.driver)
    try:
      await device.setup()
    except Exception:
      try:  # release what setup opened; the setup error is the one to report
        await device.stop()
      except Exception:  # noqa: BLE001
        pass
      raise
    self.device = device  # only a device that set up: a failed load leaves none behind
    self.outcome_unknown = None
    self.layout = new_layout
    return new_layout

  @staticmethod
  def _unreachable(what: str, exc: BaseException) -> LabError:
    return LabError("backend_unreachable", f"{what} failed: {type(exc).__name__}: {exc}.",
                    "Check the USB host and the link to it, then load_layout again.")

  async def _load_usb(self, layout: dict[str, Any]) -> Layout:
    comp = self.spec.components["pip"]
    if self.device is not None and self.mounted_tips(comp):
      raise LabError("tips_mounted", "cannot change the layout with tips on the channels.", "Call drop_tips first.")
    self._build_deck(plr.STARLetDeck(), layout)  # a bad layout is refused before the link is touched
    self.halted = self.halt_requested = False  # a stop from here on is this load's
    self.halted_text = None
    link = self._link
    if link is not None and (link.bridge.lost or link.state.latched):
      await self._let_go(link)  # every recovery starts from a fresh link and a clean PyLabRobot model
      link = None
    if link is None:
      link = await self._connect(comp)
    if link.state.mode != "open":
      await self._bring_up(link)
    return self._swap_carriers(link, layout)

  async def _connect(self, comp: Component) -> _Link:
    if not self.options.get("bridge"):
      raise LabError("bridge_required", "a usb entry needs the ssh name of the USB host the instrument is plugged into.")
    device, bridge, state = open_instrument(self.options, plr.STARLetDeck())
    link = _Link(device, bridge, state)
    self._link = link  # before the first await: a stop must find it
    bridge.on_lost = lambda reason: self._on_lost(link, reason)
    driver = device.driver
    try:
      await driver._open()
      driver._connected = True
      await driver.discover()
      link.identity = _read_identity(driver, self.options)
      self._check_who_answered(link, comp)
    except BaseException as exc:
      await self._let_go(link)
      if isinstance(exc, LabError) or not isinstance(exc, Exception):
        raise
      raise self._unreachable("reading the instrument", exc) from exc
    return link

  def _check_who_answered(self, link: _Link, comp: Component) -> None:
    pinned, serial = self.pinned(self.options), link.identity.get("serial")
    if pinned and serial != pinned:
      raise LabError("wrong_robot", f"the instrument on the USB host answered serial {serial!r}, but the confirmed "
                     f"config names {pinned!r}; nothing was moved.",
                     "Check which instrument is plugged into that USB host. If the config is wrong, write it again "
                     "with configure_devices and the person confirms it.")
    if link.identity.get("channels") != comp.channels or link.device.pipettes is None:
      raise LabError("device_mismatch", f"the instrument reports {link.identity.get('channels')} pipetting channels, "
                     f"this device description has {comp.channels}; nothing was moved.",
                     "Use the description of the instrument that is actually connected.")

  async def _bring_up(self, link: _Link) -> None:
    """Initialize a fresh, read-only link: the tip sensor, then arm and set up."""
    device = link.device
    try:
      tips = list(await device.pipettes.sense_tip_presence())
    except Exception as exc:  # noqa: BLE001 - never initialize blind
      raise LabError("tips_unreadable", f"the tip sensor could not be read ({type(exc).__name__}: {exc}); nothing "
                     "was moved.", "Check the instrument, then load_layout again. Initializing it blind could eject "
                     "tips at the waste position.") from exc
    if any(tips):
      channels = ", ".join(str(i + 1) for i, t in enumerate(tips) if t)
      raise LabError("tips_on_channels", f"the instrument senses a tip on channel(s) {channels}: initializing it would "
                     "eject them at the waste position.",
                     "Tell the person: they remove the tips by hand. Then load_layout again.")
    self._require_not_halted()
    self._place_head96_eject(device.driver)  # before the bridge is armed: a refusal moves nothing
    self._check_channel_spacing(device.driver)  # likewise
    await self._arm_and_set_up(link, "load_layout", "initialization", {}, self._interrupted, self._init_failed)

  async def _arm_and_set_up(self, link: _Link, op: str, what: str, setup_kwargs: dict[str, bool], interrupted,
                            failed) -> None:
    """Arm the bridge, open the guard and run PyLabRobot's setup on a link whose checks are done, with one handling of
    a stop, a failure and a cancellation for every caller. `what` names the step in the texts ("initialization"),
    `interrupted()` and `failed(state, error)` build the caller's own refusals."""
    bridge, state, device = link.bridge, link.state, link.device
    try:
      await bridge.arm()
    except Exception as exc:  # noqa: BLE001 - the bridge's own reason is in the text; nothing was sent
      raise self._unreachable("arming the bridge", exc) from exc
    if self.halt_requested or state.latched:  # a stop landed while arming: never open motion over it
      try:
        await bridge.disarm()
      except Exception:  # noqa: BLE001
        pass
      self._require_not_halted()
      raise LabError("device_halted", f"the instrument's guard closed while it was being armed ({state.why}).",
                     "Load the layout again once the person has checked the instrument.")
    state.open()
    try:
      await device.setup(**setup_kwargs)
    except BaseException as exc:
      if not state.latched:
        state.latch(f"{what} failed")
      self.layout = None
      if not isinstance(exc, Exception):
        raise  # cancellation travels unchanged
      _cancelled_stays_cancelled(exc)
      if self.halt_requested:
        raise interrupted() from exc
      # Stopped part-way with nobody having pressed stop: the next load waits for the person's check.
      self._close_adapter(link, f"its {what} failed part-way", op)
      raise failed(state, str(exc)) from exc
    if self.halt_requested:
      state.latch("emergency stop")
      self.layout = None
      raise interrupted()
    if state.latched:
      self.layout = None
      self._close_adapter(link, state.why or f"the guard closed during its {what}", op)
      raise failed(state, state.why or "the guard closed")
    self._plan_channel_spacing(link)

  def _check_channel_spacing(self, driver) -> None:
    """The spacing the person states must be one the instrument's master allows; checked before the bridge is armed."""
    asked = self.options.get("channel_spacing_mm")
    if asked is None:
      return
    reported = driver.configuration.min_raster_pitch_pip_channels
    if reported is None:
      says = "the instrument reported no minimum pitch for its channels"
    elif asked < reported - 1e-6:
      says = f"the instrument reports a minimum pitch of {reported:g} mm between its channels"
    else:
      return
    raise LabError("channel_spacing_refused", f"the confirmed config plans the channels {asked:g} mm apart, but {says}; "
                   "nothing was moved.",
                   "Tell the person: the config has to be written again with a channel_spacing_mm the instrument "
                   "allows, or without that option (then AHC plans with the width each channel reports).")

  def _plan_channel_spacing(self, link: _Link) -> None:
    """After setup (which reads every channel's width again): plan every channel with the spacing the person states,
    else with the minimum pitch the instrument's master reports (never closer than a plate's rows), and record what
    the channels reported, what is planned and where it comes from. The width a channel reports is not the smallest
    gap to its neighbour on every channel generation: planned with it, a column can go one channel at a time. With
    no stated spacing and no pitch from the master, the reported widths stay."""
    asked = self.options.get("channel_spacing_mm")
    pitch = link.driver.configuration.min_raster_pitch_pip_channels
    if asked is not None:
      planned, source = float(asked), "config"
    elif pitch is not None:
      planned, source = max(float(pitch), MIN_CHANNEL_SPACING_MM), "instrument"
    else:
      return
    channels = [channel for arm in link.driver.arms if arm.pipettes is not None
                for channel in arm.pipettes.configuration.channels]
    reported = sorted({round(channel.width, 2) for channel in channels if channel.width is not None})
    for channel in channels:
      channel.width = planned
    link.identity["channel_spacing_mm"] = {"reported": reported, "planned": planned, "source": source}

  @staticmethod
  def _place_head96_eject(driver) -> None:
    """Tell every fitted 96 head where to eject when initialization needs it, never lower than its travel height."""
    for head in (arm.head96 for arm in driver.arms if arm.head96 is not None):
      height = head.configuration.traversal_z_position
      if height is None:
        raise LabError("head96_unreadable", "the instrument's 96 head did not report its travel height, so its "
                       "initialization has no safe place to eject; nothing was moved.",
                       "Tell the person: the 96 head's travel height could not be read. Do not load the layout "
                       "again until they have checked the instrument.")
      head.configuration.tip_discard_location = Coordinate(HEAD96_EJECT_X, HEAD96_EJECT_Y, height)

  def _require_not_halted(self) -> None:
    if self.halt_requested:
      raise LabError("device_halted", "the emergency stop was pressed while the instrument was being loaded.",
                     "Once the person has checked the instrument, load the layout again.")

  @staticmethod
  def _interrupted() -> LabError:
    return LabError("device_halted", "the emergency stop interrupted the instrument's initialization.",
                    "Once the person has checked the instrument, load the layout again.")

  @staticmethod
  def _init_failed(state: GuardState, error: str) -> LabError:
    return LabError("initialization_failed", f"initializing the instrument failed after {state.moved} command(s) that "
                    f"move were sent ({error}). It may have stopped part-way.",
                    "Stop and tell the person: they look at the instrument. load_layout again initializes it from "
                    "the start once they say it is safe.")

  # -- tip recovery ------------------------------------------------------------------------------

  def recovers_tips(self) -> bool:
    return not self._sim

  @staticmethod
  def _recovery_interrupted() -> LabError:
    return LabError("device_halted", "the emergency stop interrupted the tip recovery's set-up.",
                    "Once the person has checked the instrument, recover the tips again or load the layout again.")

  @staticmethod
  def _recovery_setup_failed(state: GuardState, error: str) -> LabError:
    return LabError("recovery_failed", f"setting up the tip recovery failed after {state.moved} command(s) that move "
                    f"were sent ({error}). It may have stopped part-way and the tips were not moved.",
                    "Stop and tell the person: they look at the instrument. Nothing more is sent until they say it is "
                    "safe.")

  async def recover_tips(self, comp: Component, layout: dict[str, Any], mode: str,
                         target: str | None) -> dict[str, Any]:
    """Put the tips the channels still hold back into the spots they were picked up from ('return') or into the tip
    waste ('discard') without initializing the instrument, whose initialization would eject them at the waste position.
    A fresh link; the model of the tips is told on that link's deck; the link is let go of afterwards."""
    if self._sim:
      return await super().recover_tips(comp, layout, mode, target)
    if mode not in ("return", "discard"):
      raise LabError("bad_param", f"mode {mode!r} is not return or discard.", "Use 'return' or 'discard'.")
    if mode == "discard" and self.options.get("tip_waste") is not True:
      raise LabError("no_tip_waste", "this STARlet's confirmed config names no tip waste, so tips are not discarded.",
                     "Call recover_tips with mode='return': the tips go back to the spots they came from. If a tip "
                     "waste is fitted, the person says so: write the config again with options.tip_waste true, and "
                     "they confirm it.")
    if not target:
      raise LabError("recovery_target_required", "the recovery needs the tip spots the tips on the channels were "
                     "picked up from (for example tips:col3): they say which tips these are and, for a return, where "
                     "they go.", "Name them as a target, e.g. target='tips:col3', or ask the person which column the "
                     "last pick-up took.")
    # A bad layout or target is refused before the link is touched.
    scratch = self._build_deck(plr.STARLetDeck(), layout)
    wanted = scratch.resolve_tips([target])
    self.halted = self.halt_requested = False  # a stop from here on is this recovery's
    self.halted_text = None
    link = self._link
    if link is not None:
      await self._let_go(link)  # a fresh link and a clean PyLabRobot model, as every load starts
    self.layout = None  # the old deck is gone
    link = await self._connect(comp)
    try:
      sensed_before = await self._check_recovery(link, len(wanted))
      if not any(sensed_before):
        await self._let_go(link)
        return {"moved": False, "sensed_before": sensed_before,
                "note": "no tip is sensed on any channel; nothing was moved"}
    except BaseException:
      await self._let_go(link)  # nothing was moved and the bridge was never armed
      raise
    # Only the safety raise of an instrument that reports itself up (the channels to Z safety, the 96 head retracted):
    # never the channels' initialization, which ejects what they hold.
    await self._arm_and_set_up(link, "recover_tips", "set-up", RECOVERY_SETUP, self._recovery_interrupted,
                               self._recovery_setup_failed)
    try:
      spots = self._tell_the_model(link, layout, target)
    except BaseException:
      await self._let_go(link)  # only the safety raise was sent: the tips were not touched
      raise
    pips = link.device.pipettes
    try:
      await self._moving(pips.discard_tips() if mode == "discard" else pips.return_tips(), "recover_tips")
    except BaseException:
      # Whatever stopped the drop, this link must never serve a load: it was set up without the channels'
      # initialization and they may still hold their tips. A guard that closed keeps the link for the stop to listen
      # on (the next load or recovery lets go of it); one still open is let go of here.
      if link.state.mode == "open":
        await self._let_go(link)
      raise
    commands = link.state.moved
    try:
      sensed_after = [int(t) for t in await pips.sense_tip_presence()]
    except Exception:  # noqa: BLE001 - the facts are returned as far as they are known
      sensed_after = None
    names = [spot.name for spot in spots]
    await self._let_go(link)
    return {"moved": True, "mode": mode, "sensed_before": sensed_before, "sensed_after": sensed_after, "spots": names,
            "commands": commands}

  async def _check_recovery(self, link: _Link, n_spots: int) -> list[int]:
    """The reads before the bridge is armed: the tips on the channels, then (when there are any) that the instrument is up
    and that these tips are the ones the target names. A refusal here moved nothing."""
    driver = link.driver
    try:
      tips = [int(t) for t in await link.device.pipettes.sense_tip_presence()]
    except Exception as exc:  # noqa: BLE001 - never act blind
      raise LabError("tips_unreadable", f"the tip sensor could not be read ({type(exc).__name__}: {exc}); nothing "
                     "was moved.", "Check the instrument, then try again. Acting on the tips blind could eject them "
                     "at the waste position.") from exc
    if not any(tips):
      return tips
    try:
      initialized = bool(await driver.request_initialization_status())
    except Exception as exc:  # noqa: BLE001
      raise self._unreachable("reading the instrument's initialization status", exc) from exc
    if not initialized:
      raise LabError("recovery_needs_initialized", "the instrument reports itself not initialized (it was switched off "
                     "or reset since the tips were picked up); initializing it ejects mounted tips at the waste "
                     "position, so AHC cannot return them. Nothing was moved.",
                     "Tell the person: the instrument lost its initialization, so AHC cannot put the tips back. They "
                     "remove the tips by hand, or accept that initializing ejects them at the waste position. Do not "
                     "retry this call.")
    sensed = [i for i, t in enumerate(tips) if t]
    if sensed != list(range(n_spots)):
      channels = ", ".join(str(i + 1) for i in sensed)
      raise LabError("recovery_mismatch", f"the instrument senses tips on channel(s) {channels}, but the target names "
                     f"{n_spots} tip spot(s): the tips must sit on channels 1 to {n_spots}, one per spot. "
                     "Nothing was moved.",
                     "Tell the person: the tips on the channels are not the ones the target names. Check which column "
                     "the last pick-up took and what the channels hold, then name it again or remove the tips by hand.")
    self._check_channel_spacing(driver)
    self._require_not_halted()
    return tips

  def _tell_the_model(self, link: _Link, layout: dict[str, Any], target: str) -> list[TipSpot]:
    """After setup, on the link's own deck: the layout's carriers are placed and each channel's shaft is told it carries
    a tip its spot made, so PyLabRobot knows where `return_tips` puts them. Nothing is sent. The link's deck is thrown
    away with the link: this plan is not the adapter's layout."""
    try:
      placed = self._build_deck(link.device.deck, layout)
      spots = placed.resolve_tips([target])
      pips = link.device.pipettes
      for channel, spot in enumerate(spots):
        shaft = pips.shaft(channel)
        if shaft is None:
          raise LabError("recovery_failed", f"the model has no shaft for channel {channel + 1}, so it cannot be told "
                         "which tip the channel holds. Nothing was moved.",
                         "Stop and tell the person: AHC cannot put these tips back.")
        if spot.tip is not None:  # declared full: the tip is on the channel, not in the spot
          spot.unassign_tip()
        shaft.mount_tip(spot.make_tip())  # a tip this spot made: its name leads PyLabRobot back to the spot
    except LabError:
      raise
    except Exception as exc:  # noqa: BLE001
      raise LabError("recovery_failed", f"the tips could not be placed in the model: {type(exc).__name__}: {exc}. "
                     "Nothing was moved.", "Stop and tell the person: AHC cannot put these tips back.") from exc
    return spots

  def _swap_carriers(self, link: _Link, layout: dict[str, Any]) -> Layout:
    """The link's deck lives as long as the link: carriers come and go on it, and nothing is sent for that."""
    deck = link.device.deck
    self.layout = None  # the old deck is gone
    for carrier in link.placed:
      try:
        deck.unassign_child_resource(carrier)
      except Exception:  # noqa: BLE001
        logger.debug("could not unassign %s", carrier, exc_info=True)
    link.placed = []
    before = {id(child) for child in deck.children}
    try:
      new_layout = self._build_deck(deck, layout)
    except BaseException:
      for child in [c for c in deck.children if id(c) not in before]:
        deck.unassign_child_resource(child)
      raise
    link.placed = [c for c in deck.children if id(c) not in before]
    self.device = link.device
    self.outcome_unknown = None
    self.layout = new_layout
    return new_layout

  @staticmethod
  def _draw_height(deck) -> None:
    # Drawing only: the deck's size_z is the 900 mm working envelope; the top of the X-arm riding
    # above it (334.7 mm channel travel + 140 mm arm) is its visible height. PyLabRobot's own 3D
    # demo applies the same correction.
    deck._size_z = deck._local_size_z = 334.7 + 140.0

  def view_root(self):
    if self.device is not None:
      self._draw_height(self.device.deck)
    return self.device

  def preview_root(self, deck):
    self._draw_height(deck)  # the same box the device's view_root() will have
    return deck

  def _pip(self, comp: Component):
    if self.device is None:
      raise LabError("no_layout", "the STARlet is not set up.", "Call load_layout first.")
    if comp.name != "pip":
      raise LabError("not_supported", f"{comp.name} is not wired in this prototype.")
    return self.device.pipettes

  async def _motion_pip(self, comp: Component):
    """The channels, for a call that moves. On a real instrument, only when everything allows it, and before
    PyLabRobot is called, so a refusal here sent nothing."""
    if self._sim:
      return self._pip(comp)
    self.require_motion()
    link = self._link
    if link is None or self.device is None or self.layout is None:
      raise LabError("no_layout", "the STARlet is not set up.", "Call load_layout first.")
    if comp.name != "pip":
      raise LabError("not_supported", f"{comp.name} is not wired in this prototype.")
    if link.state.mode != "open":
      if link.state.latched:
        # Whatever closed it (a read that got no answer does too): without this the server would still trust the
        # twin's tips and ask for a drop_tips that can no longer run.
        self._close_adapter(link, link.state.why or "the guard closed")
      raise LabError("instrument_read_only", f"the instrument takes no motion commands: {link.state.why}.",
                     "Tell the person; load_layout again initializes it once they have checked it.")
    if link.bridge.lost:
      self._close_adapter(link, f"the link to the USB host was lost: {link.bridge.lost}")
      raise LabError("link_lost", f"the link to the USB host is lost: {link.bridge.lost}; nothing was sent.",
                     "Tell the person; load_layout again reconnects once they have checked the instrument.")
    try:
      armed = (await link.bridge.status()).get("armed")
    except Exception as exc:  # noqa: BLE001
      self._close_adapter(link, f"the USB host did not answer ({type(exc).__name__}: {exc})")
      raise LabError("link_lost", f"the USB host did not answer ({type(exc).__name__}: {exc}); nothing was sent.",
                     "Tell the person; load_layout again reconnects once they have checked the instrument.") from exc
    if armed is not True:
      self._close_adapter(link, "the bridge on the USB host is not armed")
      raise LabError("instrument_read_only", "the bridge on the USB host is not armed, so the instrument takes no "
                     "motion commands; nothing was sent.",
                     "Tell the person; load_layout again initializes it once they have checked it.")
    return self.device.pipettes

  async def _moving(self, call, op: str):
    """Await a PyLabRobot call that moves. If it raises anything (cancellation included) and the guard closed, the
    adapter closes too before the exception goes on: the capability layer decides from the command count whether
    nothing was sent or the outcome is unknown."""
    if self._sim:
      return await call
    link = self._link
    try:
      return await call
    except BaseException as exc:
      if link is not None and link.state.mode != "open":
        self._close_adapter(link, link.state.why or "the guard closed", op)
      _cancelled_stays_cancelled(exc)
      raise

  def mounted_tips(self, comp: Component) -> list[Any]:
    if self.device is None:
      return []
    if not self._sim and (self.halted or self.halt_requested or self.outcome_unknown):
      return []  # after a stop or an unknown outcome the tips are the person's to check
    pip = self._pip(comp)
    return [t for t in (pip.get_mounted_tip(ch) for ch in range(pip.num_channels)) if t is not None]

  async def pick_up_tips(self, comp: Component, spots: list[TipSpot]) -> str | None:
    pip = await self._motion_pip(comp)
    await self._moving(pip.pick_up_tips(spots), "pick_up_tips")
    return None

  def transport_air(self, op: str, comp: Component, tips: list[Any], specialized: dict[str, Any]) -> list[float]:
    """The transport air each channel draws after this call's liquid: the one given, else its liquid class's,
    looked up as PyLabRobot does (the tip, water, jet and blow-out)."""
    given = specialized.get("transport_air_volume_ul")
    if given is not None:
      return [float(given)] * len(tips)
    attribute = "dispense_air_transport_volume" if op == "dispense" else "aspiration_air_transport_volume"
    out = []
    for tip in tips:
      found = get_star_liquid_class(tip_volume=tip.maximal_volume, is_core=False, is_tip=True,
                                    has_filter=tip.has_filter, liquid=Liquid.WATER,
                                    jet=bool(specialized.get("jet")), blow_out=bool(specialized.get("blow_out")))
      out.append(getattr(found, attribute) if found is not None else 0.0)  # no class: PyLabRobot refuses the call
    return out

  def _options(self, n: int, flow_rate, liquid_height, specialized: dict[str, Any]) -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if flow_rate is not None:
      kw["flow_rates"] = [flow_rate] * n
    if liquid_height is not None:
      kw["liquid_heights"] = [liquid_height] * n
    for key, value in specialized.items():
      if key in _PER_CHANNEL:
        kw[_PER_CHANNEL[key]] = [value] * n
    return kw

  async def aspirate(self, comp, containers: list[Container], volumes, flow_rate, liquid_height,
                     specialized) -> str | None:
    kw = self._options(len(containers), flow_rate, liquid_height, specialized)
    pip = await self._motion_pip(comp)
    await self._moving(pip.aspirate(containers, volumes=volumes, **kw), "aspirate")
    return None

  async def dispense(self, comp, containers: list[Container], volumes, flow_rate, liquid_height,
                     specialized) -> str | None:
    kw = self._options(len(containers), flow_rate, liquid_height, specialized)
    mix_volume = specialized.get("post_mix_volume_ul")
    mix_reps = specialized.get("post_mix_repetitions")
    if (mix_volume is None) != (mix_reps is None):
      raise LabError("paired_params", "post_mix_volume_ul and post_mix_repetitions go together.",
                     "Pass both, or neither.")
    note = None
    if mix_volume is not None:
      mix_flow = flow_rate if flow_rate is not None else self.MIX_FLOW_RATE
      kw["post_mixes"] = [Mix(volume=mix_volume, repetitions=mix_reps, flow_rate=mix_flow)] * len(containers)
      source = "the dispense's flow rate" if flow_rate is not None else "AHC's mix speed: no flow_rate given"
      note = (f"native post-dispense mix: {mix_reps} x {mix_volume:g} uL at {mix_flow:g} uL/s ({source}) in the "
              "same firmware command")
    pip = await self._motion_pip(comp)
    await _piston_checked(self._moving(pip.dispense(containers, volumes=volumes, **kw), "dispense"))
    return note

  async def mix(self, comp, containers: list[Container], volume, repetitions, flow_rate,
                liquid_height, specialized) -> str | None:
    # One firmware command: an aspirate of 0 uL whose pre-mix does the cycles. Separate
    # aspirate/dispense cycles could be refused half-way (a 0.1 uL liquid-class rounding between
    # the two directions), leaving liquid in the tips.
    n = len(containers)
    kw = self._options(n, None, liquid_height, {})
    flow = flow_rate if flow_rate is not None else self.MIX_FLOW_RATE
    cycles = Mix(volume=volume, repetitions=repetitions, flow_rate=flow)
    pip = await self._motion_pip(comp)
    await _piston_checked(self._moving(pip.aspirate(containers, volumes=[0.0] * n, pre_mixes=[cycles] * n, **kw),
                                       "mix"))
    return (f"STAR channels have no standalone mix: ran {repetitions} x {volume:g} uL at {flow:g} uL/s as the "
            "pre-mix of a 0 uL aspirate, one firmware command; dispense.post_mix_* mixes right after a dispense")

  async def drop_tips(self, comp: Component, mode: str) -> str | None:
    if not self._sim and mode == "discard" and self.options.get("tip_waste") is not True:
      raise LabError("no_tip_waste", "this STARlet's confirmed config names no tip waste, so tips are not discarded.",
                     "Call drop_tips with mode='return': the tips go back to the spots they came from. If a tip waste "
                     "is fitted, the person says so: write the config again with options.tip_waste true, and they "
                     "confirm it.")
    pip = await self._motion_pip(comp)
    await self._moving(pip.discard_tips() if mode == "discard" else pip.return_tips(), "drop_tips")
    return None

  def _reader(self, comp: Component):
    """The channels for a read: on a real instrument through the link, as long as it is not lost (a read passes a
    closed guard)."""
    if self._sim:
      return self._pip(comp)
    link = self._link
    if link is None:
      raise LabError("no_layout", "the STARlet is not connected.", "Call load_layout first.")
    if link.bridge.lost or link.device.pipettes is None:
      raise LabError("link_lost", f"the link to the USB host is lost: {link.bridge.lost}.",
                     "Tell the person; load_layout again reconnects.")
    return link.device.pipettes

  async def invoke(self, comp: Component, op: str, params: dict[str, Any]) -> dict[str, Any]:
    if op == "sense_tip_presence":
      return {"tip_presence_per_channel": list(await self._reader(comp).sense_tip_presence())}
    return await super().invoke(comp, op, params)

  def sensor_report(self) -> dict[str, Any]:
    # The STAR has tip presence (sense() below), liquid-level detection and pressure monitoring. They come later as
    # evidence: report them here ({"tips_mounted": "tip", "liquid_present": "well"}) once a real STARlet backend exists.
    return {"observes": {}, "note": "STARlet: tip presence, liquid-level detection and pressure monitoring are not read as evidence yet"}

  async def sense(self, comp: Component, check: str) -> dict[str, Any] | None:
    if check != "tips_mounted":
      return None
    if self._sim:
      if self.device is None:
        return None
      return {"tip_presence_per_channel": list(await self._pip(comp).sense_tip_presence())}
    try:
      return {"tip_presence_per_channel": list(await self._reader(comp).sense_tip_presence())}
    except Exception:  # noqa: BLE001 - a sensor that cannot be read gives nothing
      return None
