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


class Adapter(ABC):
  # The options a device entry may set, per backend; anything else is refused before the person confirms it.
  OPTIONS: dict[str, frozenset[str]] = {}
  # uL/s a mix runs at when the call gives no flow rate, if the adapter chooses it (then it is recorded);
  # None: the device's own default.
  MIX_FLOW_RATE: float | None = None

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

  async def halt(self) -> bool:
    """The emergency stop: stop the device itself, if it has a stop this adapter can reach. False if not."""
    return False

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
                     f"the outcome of {unknown['op']} on the device is unknown ({unknown['error']}); nothing moves "
                     "until the layout is loaded again.",
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

  # -- model-only operations and sensors ------------------------------------------------------

  async def invoke(self, comp: Component, op: str, params: dict[str, Any]) -> dict[str, Any]:
    raise LabError("not_supported", f"{self.spec.model} has no model-only operation {op!r}.")

  def view_root(self):
    """The resource a live 3D view should draw, or None before a layout is loaded."""
    return None

  async def sense(self, comp: Component, check: str) -> dict[str, Any] | None:
    """Readings from the instrument's own sensors for a check, or None when it has none."""
    return None
