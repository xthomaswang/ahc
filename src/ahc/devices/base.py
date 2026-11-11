"""What every device adapter implements. The capability layer talks only to this.

Brand classes (Hamilton, Opentrons) hold what a vendor's models share: the deck model, the
transport, the command log. Model classes (STARlet, OT-2) map components to the driver and translate
general operations into what the hardware actually does, returning a note when they had to.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from pylabrobot.resources import Container, TipSpot

from ahc.devices.spec import Component, DeviceSpec
from ahc.core.errors import LabError
from ahc.core.layout import Layout


def unknown_text(unknown: dict[str, Any]) -> str:
  """What an unknown-outcome record says: the call whose outcome on the device is unknown, or the record's own
  sentence ("says") when it is not about one call (a link that was lost)."""
  return unknown.get("says") or f"the outcome of {unknown['op']} on the device is unknown ({unknown['error']})"


class Adapter(ABC):
  # The options a device entry may set, per backend; anything else is refused before the person confirms it.
  OPTIONS: dict[str, frozenset[str]] = {}
  # uL/s a mix runs at when the call gives no flow rate, if the adapter chooses it (then it is recorded);
  # None: the device's own default.
  MIX_FLOW_RATE: float | None = None
  # The option that pins which real device an entry means, and the field of probe_identity()'s answer it is read from:
  # configure_devices fills the option from the device itself, the person confirms it, and a device that answers with
  # another value is refused (wrong_robot). A robot is pinned by its name, a Hamilton instrument by its serial.
  IDENTITY_OPTION = "robot_name"
  IDENTITY_FIELD = "name"
  # What load_layout does to a real device before anything else, as the person is told when they approve it ("load_layout
  # <this> next").
  LOAD_MOVES = "homes the robot"
  # What the person's approval before load_layout also means ("approving also means <this>").
  LOAD_NEEDS = "hands and deck are clear"
  # What a stop did once halt() returned True, for the stop's acknowledgement and the run log.
  HALTED = "the robot accepted the stop and its run reports stopped; the command in flight may have finished first"
  # What the person is told about a real device, in the device's own words. Each default is the robot's wording.
  IDENTITY_SAYS = "name, serial, software, the pipette on each mount"  # what configure_devices read, for them to confirm
  PROBE_NEEDS = ("configure_devices reads the robot's name and pipettes before it writes the config, so the robot must "
                 "be on and reachable; ask the person.")  # appended to a probe that found nothing to read
  STOPPED = "The emergency stop stopped the robot's run"  # the stop reached the device and it is stopped
  STOP_UNCONFIRMED = ("The emergency stop was sent to the robot, which has not confirmed that its run stopped: it may "
                      "still be executing its last command, and the power switch is the immediate stop")
  # Whether the device forgets which tips are on once its run stops (a robot's run does; an instrument's channels hold
  # what they hold), which decides how a stop is explained to the person.
  FORGETS_TIPS = True
  # How disconnect answers when no run was open on a real device, or None for the server's own sentences.
  LET_GO: str | None = None
  # Whether the device takes one connection at a time: a second one (configure_devices reading its identity) is refused
  # while this server holds a link, so the server asks the bound adapter instead.
  EXCLUSIVE_LINK = False
  # What the overview adds about the emergency stop on a real device.
  STOP_NOTE = ("On a real OT-2 it ends AHC's run, but the robot first finishes the command it is executing (a move, an "
               "aspirate, a pick-up); the power switch is the immediate stop.")

  def __init__(self, spec: DeviceSpec, backend: str, options: dict[str, Any]):
    if backend not in spec.backends:
      raise LabError("backend_not_enabled", f"{spec.model} has no {backend!r} backend here.",
                     f"Enabled backends: {', '.join(spec.backends)}.")
    self.spec = spec
    self.backend = backend
    self.options = options
    self.layout: Layout | None = None
    self.halt_requested = False  # the emergency stop started stopping the device
    self.halted = False  # ... and the device confirmed it stopped
    # A call failed after commands may have reached real hardware: {"op", "error", "at"}. Motion stays
    # refused until a new layout load; simulated backends never set it.
    self.outcome_unknown: dict[str, Any] | None = None

  @property
  def simulated(self) -> bool:
    return self.backend == "sim"

  @classmethod
  def check_options(cls, backend: str, options: dict[str, Any]) -> None:
    """Refuse a device entry this adapter could not run, before the person is asked to confirm it.

    Takes the entry's own options only, never merged with the server's overrides.
    """
    if not isinstance(options, dict):
      raise LabError("bad_option", "options must be a mapping.")
    if backend == "sim":
      given = [k for k in ("host", "port", "robot_name") if k in options]
      if given:
        raise LabError("sim_options_refused",
                       "a sim entry takes no host, port or robot_name: the simulator is this task's own.",
                       f"Remove {', '.join(given)} from this entry's options.")
    allowed = cls.OPTIONS.get(backend, frozenset())
    unknown = sorted(set(options) - allowed)
    if unknown:
      raise LabError("bad_option", f"the {backend!r} backend takes no option(s) {', '.join(map(str, unknown))}.",
                     f"Options it takes: {', '.join(sorted(allowed)) or 'none'}.")

  @classmethod
  def placement(cls, layout: Any) -> dict[str, str] | None:
    """Where a layout (as given to load_layout) puts each labware, position -> type, as a new layout is compared
    with the task folder's references: labware names, liquids, volumes and aliases do not count. Read from the
    layout alone and never raises; None for a device whose layouts cannot be compared (a new one needs the person)."""
    return None

  @classmethod
  def probe_identity(cls, backend: str, options: dict[str, Any]) -> dict[str, Any]:
    """Who answers at an entry's address, before the person confirms it: read-only, no run, no motion.
    Blocking (async callers use a thread); {} when there is nothing to probe."""
    return {}

  @classmethod
  def check_identity(cls, identity: dict[str, Any], options: dict[str, Any]) -> None:
    """Refuse (wrong_robot) an identity that is not the device the entry is pinned to. The default checks nothing: the
    server compares a robot's name itself."""

  @classmethod
  def pinned(cls, options: dict[str, Any]) -> str | None:
    """The device a real entry is pinned to (the robot's name, the instrument's serial); None while it names none."""
    value = options.get(cls.IDENTITY_OPTION) if isinstance(options, dict) else None
    return str(value) if value else None

  @classmethod
  def describe_entry(cls, options: dict[str, Any]) -> str:
    """How the confirm summary names a real entry's device, after "<id> = <model>, backend <backend>": which device,
    where it is reached, and anything else about it the person confirms. Starts with ", ". Never raises."""
    name = cls.pinned(options)
    return f", device {name!r}" if name else ", device (not identified)"

  @classmethod
  def named(cls, entry_id: str, model: str, options: dict[str, Any]) -> str:
    """A real entry's device as a sentence names it: the one it is pinned to, else the entry's id and model."""
    pinned = cls.pinned(options)
    return repr(pinned) if pinned else f"{entry_id} ({model})"

  @classmethod
  def go_ahead_why(cls, named: str) -> str:
    """Why the person's go-ahead is needed before a real device is driven; `named` is what named() returned."""
    return f"the real robot {named} homes on load_layout"

  def reaches(self, options: dict[str, Any]) -> bool:
    """Whether an entry's options reach the same place as this adapter's (the same USB host, the same address), for a
    device that takes one connection at a time (EXCLUSIVE_LINK)."""
    return False

  def identity(self) -> dict[str, Any]:
    """What the device says it is (name, serial, software), for the run log; {} if it cannot say."""
    return {}

  def run_id(self) -> str | None:
    """The device's own id for the run its state belongs to (the tips it holds), also a run the emergency stop
    halted; None when it keeps no runs, or none is live."""
    return None

  def robot_record(self, n: int) -> list[dict[str, Any]] | None:
    """The device's own record of the last n commands this adapter sent ({"command", "status", "error"}),
    read back after a call whose outcome is unknown; None when the device keeps none. Blocking."""
    return None

  @property
  def has_stop(self) -> bool:
    """Whether halt() can reach a stop on this device. False: the emergency stop only keeps AHC from sending more."""
    return type(self).halt is not Adapter.halt

  async def halt(self, stop: dict[str, Any] | None = None) -> bool:
    """The emergency stop: stop the device itself, if it has a stop this adapter can reach. False if not.
    stop: the stop's record (its id among others), for a device that keeps the stop under that id."""
    return False

  async def run_ended(self, run_id: str | None) -> bool | None:
    """After stop(): whether the device itself says that run ended (asked, not assumed); None for a device that keeps
    no runs. A device that cannot answer says False."""
    return None

  def require_layout(self) -> Layout:
    if self.layout is None:
      raise LabError("no_layout", "no deck layout is loaded.", "Call load_layout first.")
    return self.layout

  def require_motion(self) -> None:
    """Refuse motion after the emergency stop reached the device, or when a call's outcome is unknown."""
    if self.halted:
      raise LabError("device_halted", "the emergency stop stopped the device's run, so it takes no more commands.",
                     "Load the layout again (a new run). The person removes any tips left on the pipettes and "
                     "checks the deck first.")
    if self.halt_requested:
      raise LabError("device_halted",
                     "the emergency stop could not confirm that the device stopped: it may still be executing its "
                     "last command.",
                     "The power switch is the immediate stop. Once the person has made it safe, load the layout "
                     "again (a new run).")
    if self.outcome_unknown:
      unknown = self.outcome_unknown
      raise LabError("completion_unknown",
                     f"{unknown_text(unknown)}; nothing moves until the layout is loaded again.",
                     "Stop and tell the person: they check the tips and wells; then load_layout again with the "
                     "volumes they report.")

  def tip_fits(self, comp: Component, rack: Any) -> bool:
    """Whether comp takes the tips of this rack: a pick-up that names no tips skips the racks it does not. True
    when the device cannot say (the pick-up itself refuses a tip it cannot take)."""
    return True

  def tip_sizes(self, comp: Component) -> str | None:
    """The tips comp takes, as a refusal names them (e.g. '10 or 20 uL'); None when any, or unknown."""
    return None

  def transport_air(self, op: str, comp: Component, tips: list[Any], specialized: dict[str, Any]) -> list[float]:
    """The air each tip draws after this call's liquid (the STAR's transport air), which the tip must hold as
    well; zero for a device that draws none."""
    return [0.0] * len(tips)

  def mark_outcome_unknown(self, op: str, error: str, call: dict[str, Any] | None = None) -> None:
    """call: the call's basic parameters (targets, tips, mode), so what it may have touched can be named."""
    self.outcome_unknown = {"op": op, "error": error, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            **({"call": dict(call)} if call else {})}

  # -- lifecycle ------------------------------------------------------------------------------

  @abstractmethod
  async def start(self) -> None: ...

  @abstractmethod
  async def stop(self) -> None: ...

  @abstractmethod
  async def load_layout(self, layout: dict[str, Any]) -> Layout: ...

  async def preview_layout(self, layout: dict[str, Any]) -> tuple[Layout, Any]:
    """The deck a layout describes, built on a scratch PyLabRobot deck, for the person to set up by hand before
    load_layout: (the Layout, its deck). Touches no device: no backend, no run, no request to a robot, and it
    changes nothing this adapter holds (self.layout stays). Refused with the same LabError load_layout gives."""
    raise LabError("not_supported", f"{self.spec.model} cannot preview a layout.")

  # -- state ----------------------------------------------------------------------------------

  @abstractmethod
  def mounted_tips(self, comp: Component) -> list[Any]:
    """The tip on each channel in use, in channel order; empty when none are mounted."""

  @abstractmethod
  def command_count(self) -> int:
    """Commands sent to the device or its simulator so far (firmware strings, run commands), counted
    in this process. Synchronous; async callers use a thread."""

  # -- liquid handling ------------------------------------------------------------------------

  @abstractmethod
  async def pick_up_tips(self, comp: Component, spots: list[TipSpot]) -> str | None: ...

  @abstractmethod
  async def aspirate(self, comp: Component, containers: list[Container], volumes: list[float],
                     flow_rate: float | None, liquid_height: float | None,
                     specialized: dict[str, Any]) -> str | None: ...

  @abstractmethod
  async def dispense(self, comp: Component, containers: list[Container], volumes: list[float],
                     flow_rate: float | None, liquid_height: float | None,
                     specialized: dict[str, Any]) -> str | None: ...

  @abstractmethod
  async def mix(self, comp: Component, containers: list[Container], volume: float, repetitions: int,
                flow_rate: float | None, liquid_height: float | None,
                specialized: dict[str, Any]) -> str | None: ...

  @abstractmethod
  async def drop_tips(self, comp: Component, mode: str) -> str | None: ...

  # -- recovery -------------------------------------------------------------------------------

  def recovers_tips(self) -> bool:
    """Whether recover_tips() can put tips left on the channels (after a stop, say) back or into the waste, with no
    person pulling them off by hand. False: the person removes them."""
    return False

  async def recover_tips(self, comp: Component, layout: dict[str, Any], mode: str,
                         target: str | None) -> dict[str, Any]:
    """Return ('return': back into the spots `target` names, which they were picked up from) or discard ('discard': into
    the tip waste) the tips on the channels, on a fresh link and a scratch deck built from `layout`, and let go of the
    link. Returns what was sensed and moved. Refuses with a LabError when it cannot do so safely."""
    raise LabError("recovery_not_supported", f"{self.spec.model} has no tip recovery.",
                   "The person removes the tips by hand, then verify(check='tips_mounted', expect='none') and "
                   "record_verdict.")

  # -- model-only operations and sensors ------------------------------------------------------

  async def invoke(self, comp: Component, op: str, params: dict[str, Any]) -> dict[str, Any]:
    raise LabError("not_supported", f"{self.spec.model} has no model-only operation {op!r}.")

  def view_root(self):
    """The resource a live 3D view should draw, or None before a layout is loaded."""
    return None

  def preview_root(self, deck):
    """The resource a live 3D view should draw for a previewed (scratch) deck: sized as view_root() sizes the
    device's, so swapping the device in at load_layout fits the same view."""
    return deck

  async def sense(self, comp: Component, check: str) -> dict[str, Any] | None:
    """Readings from the instrument's own sensors for a check, or None when it has none."""
    return None

  def sensor_report(self) -> dict[str, Any]:
    """What this instrument's own sensors can observe, for physical_world_context's instrument_context:
    {"observes": {check: level}, "note": text}. `observes` names the checks a sensor can read ('tips_mounted' at level
    'tip', 'liquid_present' at level 'well'); empty for an instrument with none, which is the honest default. The
    readings themselves come through sense(). This is the one place a device adds a sensor: nothing in the vision code
    changes. The digital twin is not a sensor and never appears here."""
    return {"observes": {}, "note": f"{self.spec.title}: no sensors AHC reads as evidence"}
