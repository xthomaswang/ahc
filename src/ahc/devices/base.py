"""What every device adapter implements. The capability layer talks only to this.

Brand classes (Hamilton, Opentrons) hold what a vendor's models share: the deck model, the
transport, the command log. Model classes (STARlet, OT-2) map components to the driver and translate
general operations into what the hardware actually does, returning a note when they had to.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pylabrobot.resources import Container, TipSpot

from ahc.devices.spec import Component, DeviceSpec
from ahc.core.errors import LabError
from ahc.core.layout import Layout


class Adapter(ABC):
  def __init__(self, spec: DeviceSpec, backend: str, options: dict[str, Any]):
    if backend not in spec.backends:
      raise LabError("backend_not_enabled", f"{spec.model} has no {backend!r} backend here.",
                     f"Enabled backends: {', '.join(spec.backends)}.")
    self.spec = spec
    self.backend = backend
    self.options = options
    self.layout: Layout | None = None

  @property
  def simulated(self) -> bool:
    return self.backend == "sim"

  def require_layout(self) -> Layout:
    if self.layout is None:
      raise LabError("no_layout", "no deck layout is loaded.", "Call load_layout first.")
    return self.layout

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
    """Commands that reached the device or its simulator so far (firmware strings, HTTP calls)."""

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
