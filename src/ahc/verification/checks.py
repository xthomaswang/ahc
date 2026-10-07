"""Physical-world verification: checks, evidence, verdicts.

The agent judges each check; this module gathers the evidence it judges from. In simulation the
simulator's state is compared with the digital twin and the verdict is marked simulated. On real
hardware a person verifies for now (placeholder until camera and CV verifiers are connected).
"""

from __future__ import annotations

import uuid
from typing import Any

from ahc.devices.base import Adapter
from ahc.devices.spec import DeviceSpec
from ahc.core.errors import LabError
from ahc.records import RunLog

CHECKS = {
  "tips_mounted": "Each channel in use carries a tip (expect='mounted') or none does (expect='none').",
  "liquid_present": "Each target holds at least min_volume_ul.",
  "deck_matches_layout": "The physical deck matches the loaded layout: labware types and positions.",
}

CAMERA_NOTE = "no camera verifier connected yet (placeholder)"


class Verification:
  def __init__(self, spec: DeviceSpec, adapter: Adapter, runlog: RunLog):
    self.spec = spec
    self.adapter = adapter
    self.runlog = runlog
    self.pending: dict[str, dict[str, Any]] = {}

  async def verify(self, check: str, device: str | None = None, targets: list[str] | None = None,
                   expect: str = "mounted", min_volume_ul: float = 0.0, person: bool = False) -> dict[str, Any]:
    """`person`: the verdict is the person's even in simulation (a layout without a reference)."""
    if check not in CHECKS:
      raise LabError("unknown_check", f"no check named {check!r}.", f"Checks: {', '.join(CHECKS)}.")
    twin, sensors, note = await self._evidence(check, device, targets, expect, min_volume_ul)
    if self.adapter.simulated and not person:
      passed = twin["as_expected"] and (sensors is None or sensors.get("agrees_with_twin", True))
      verdict, judged_by = ("pass" if passed else "fail"), "simulation"
    elif self.adapter.simulated:
      verdict, judged_by = "pending", "the person: layout without a reference"
    else:
      verdict, judged_by = "pending", "human (placeholder)"
    check_id = uuid.uuid4().hex[:8]
    result = {"check_id": check_id, "check": check, "verdict": verdict, "judged_by": judged_by,
              "simulated": self.adapter.simulated,
              "evidence": {"digital_twin": twin, "sensors": sensors, "camera": CAMERA_NOTE},
              "note": note}
    if verdict == "pending":
      self.pending[check_id] = result
      result["next"] = ("This layout follows no reference in this task folder, so the person confirms it: show them "
                        "the labware, positions and liquids (also on the run dashboard) and ask. Record their answer "
                        "with record_verdict only when they give it; never decide it yourself."
                        if person and self.adapter.simulated else
                        "The person on site checks the deck and records the verdict with record_verdict.")
    self.runlog.record({"type": "verification", **{k: result[k] for k in ("check_id", "check", "verdict", "judged_by")},
                        "evidence": result["evidence"]})
    return result

  async def _evidence(self, check, device, targets, expect, min_volume_ul):
    if check == "deck_matches_layout":
      layout = self.adapter.require_layout()
      comparison = layout.compare_placements()
      note = ("The device model's deck tree is compared with what load_layout placed there. On real hardware "
              "that is not enough: the person on site checks the deck (a camera later).")
      return ({**comparison, "layout": layout.summary()}, None, note)
    if check == "tips_mounted":
      if device is None:
        raise LabError("device_required", "tips_mounted needs a device component.")
      comp = self.spec.component(device)
      if expect not in ("mounted", "none"):
        raise LabError("bad_expect", "expect must be 'mounted' or 'none'.")
      mounted = len(self.adapter.mounted_tips(comp))
      twin = {"tips_mounted": mounted, "expect": expect,
              "as_expected": (mounted > 0) if expect == "mounted" else (mounted == 0)}
      reading = await self.adapter.sense(comp, check)
      if reading is not None:
        sensed = sum(1 for p in reading["tip_presence_per_channel"] if p)
        reading = {**reading, "sensed_tips": sensed, "agrees_with_twin": sensed == mounted}
      note = None if reading is not None else "this device has no tip sensor; a camera is needed"
      return twin, reading, note
    if check == "liquid_present":
      containers = self.adapter.require_layout().resolve_targets(targets or [])
      if not containers:
        raise LabError("targets_required", "liquid_present needs targets.")
      volumes = {c.name: c.tracker.get_used_volume() for c in containers}
      twin = {"volumes_ul": volumes, "min_volume_ul": min_volume_ul,
              "as_expected": all(v >= min_volume_ul and v > 0 for v in volumes.values())}
      return twin, None, "liquid-level sensing is not wired in this prototype"
    raise AssertionError(check)

  def record_verdict(self, check_id: str, verdict: str, note: str = "") -> dict[str, Any]:
    if check_id not in self.pending:
      raise LabError("unknown_check_id",
                     f"no check {check_id!r} waits for the person's verdict on this server. Simulated checks are judged by "
                     "the simulator (except a layout without a reference), and a check made before the server restarted is gone.",
                     "Run verify again for a new check (after a restart: load_layout first); for a layout without a "
                     "reference, show the person the layout and record their verdict for that check.")
    if verdict not in ("pass", "fail"):
      raise LabError("bad_verdict", "verdict must be 'pass' or 'fail'.")
    result = {**self.pending.pop(check_id), "verdict": verdict, "judged_by": "human", "note": note}
    self.runlog.record({"type": "verification", "check_id": check_id, "check": result["check"],
                        "verdict": verdict, "judged_by": "human", "note": note})
    return result
