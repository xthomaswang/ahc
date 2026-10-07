"""The motion gate: after a layout loads, nothing moves until the deck check has passed.

The server enforces it; a prompt cannot. One gate per loaded layout:

  pending  the layout is loaded and the deck is not confirmed yet (or the person has not answered)
  passed   deck_matches_layout passed: the simulator's verdict, or the person on site on real hardware.
           In simulation, a layout that follows no reference in the task folder needs the person too
           (needs_person); it is saved as a reference only once they confirm it (pending_layout).
  failed   the last deck check failed. Every action tool is refused, drop_tips included, because the
           deck is not what the server thinks it is. Recovery: the person fixes the deck and the check
           runs again, or a corrected layout is loaded.

Loading a layout starts a new gate; so does the config going back to pending.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ahc.core.errors import LabError

STATES = {"pass": "passed", "fail": "failed", "pending": "pending"}


@dataclass
class Gate:
  layout: str | None
  state: str = "pending"
  check_id: str | None = None
  needs_person: bool = False
  pending_layout: dict[str, Any] | None = None  # saved in .ahc/layouts/ once the person confirms it

  def public(self) -> dict[str, Any]:
    return {"layout": self.layout, "state": self.state, "check_id": self.check_id, "needs_person": self.needs_person}

  def require_open(self) -> None:
    if self.state == "passed":
      return
    if self.state == "pending" and self.needs_person:
      raise LabError("gate_pending", "the person has not confirmed this layout yet (no reference in this task folder), so nothing may move.",
                     "verify(check='deck_matches_layout') asks for their verdict; show them the layout and wait for it.")
    if self.state == "failed":
      raise LabError("gate_failed", "the last deck check failed: the deck does not match the loaded layout, so nothing may move.",
                     "The person on site fixes the deck, then verify(check='deck_matches_layout') again, or load a corrected layout.")
    raise LabError("gate_pending", "the deck has not been confirmed against the loaded layout, so nothing may move.",
                   "Call verify(check='deck_matches_layout'); on real hardware the person on site records the verdict.")

  def apply(self, check_id: str, verdict: str) -> None:
    self.state, self.check_id = STATES[verdict], check_id
