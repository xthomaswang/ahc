"""instrument_context: the device's own sensors as a physical-world context source.

The adapter says what its instrument can sense (Adapter.sensor_report). Today that is nothing: the OT-2 GEN2 pipettes
have no tip or liquid sensors, and the STARlet's (tip presence, liquid-level detection, pressure monitoring) are not read
as evidence yet. With none, this source observes no point and the cameras carry the confidence.

Adding a sensor: report it in the adapter's sensor_report() (`observes`: check -> level), read it in the adapter's
sense(), and this source starts covering the matching points. Nothing here talks to a device.
"""

from __future__ import annotations

from typing import Any

from ahc.physical_world import Observation

KIND = "instrument"


class InstrumentContext:
  kind = KIND

  def __init__(self, device_id: str, report: dict[str, Any]):
    self.device_id = device_id
    self.report = report

  @property
  def observes(self) -> dict[str, str]:
    return dict(self.report.get("observes") or {})

  def describe(self) -> dict[str, Any]:
    return {"device": self.device_id, "observes": self.observes, "note": self.report.get("note", "")}

  def observe(self, point: dict[str, str], target: dict[str, Any]) -> list[Observation]:
    level = self.observes.get(point["check"])
    if level is None:
      return []
    # A sensor is read when asked (Adapter.sense), so what it reports is as new as the call; a sensor whose reading
    # needs the head to move would have to say it is stale until that call is made.
    return [Observation(f"{KIND}:{self.device_id}", level)]
