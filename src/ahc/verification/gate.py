"""The motion gate: after a layout loads, nothing moves until the deck check has passed.

The server enforces it; a prompt cannot. One gate per loaded layout:

  pending  the layout is loaded and the deck is not confirmed yet (or the person has not answered)
  passed   deck_matches_layout passed: the simulator's verdict, or the person on site on real hardware.
           In simulation, a layout whose deck (each labware type and where it sits) matches no layout
           saved or run in the task folder needs the person too (needs_person), told how it differs from
           the closest one (differs); it is saved as a reference only once they confirm it (pending_layout).
  failed   the last deck check failed. Every action tool is refused, drop_tips included, because the
           deck is not what the server thinks it is. Recovery: the person fixes the deck and the check
           runs again, or a corrected layout is loaded.

Loading a layout starts a new gate; so does the config being confirmed again after it went back to
pending. The gate belongs to the Layout object the device loaded (layout_ref): a device holding any
other layout, or a loaded layout without a gate, counts as not checked, so the gate fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ahc.core.errors import LabError
from ahc.verification.ask import pass_now

STATES = {"pass": "passed", "fail": "failed", "pending": "pending"}


@dataclass
class Gate:
  layout: str | None
  state: str = "pending"
  check_id: str | None = None
  needs_person: bool = False
  pending_layout: dict[str, Any] | None = None  # saved in .ahc/layouts/ once the person confirms it
  layout_ref: Any = field(default=None, repr=False, compare=False)  # the Layout object load_layout built
  differs: str | None = None  # how the layout differs from the task folder's references, for the person
  found: str | None = None  # what the person found on the deck before this load (a failed liquid check), to look at again

  def public(self) -> dict[str, Any]:
    return {"layout": self.layout, "state": self.state, "check_id": self.check_id, "needs_person": self.needs_person}

  def renewed(self) -> Gate:
    """A new pending gate for the same loaded layout: the deck is checked again."""
    return Gate(layout=self.layout, needs_person=self.needs_person, pending_layout=self.pending_layout,
                layout_ref=self.layout_ref, differs=self.differs, found=self.found)

  def waiting_for_person(self) -> str | None:
    """The deck check that waits for the person's verdict, if one does."""
    return self.check_id if self.state == "pending" else None

  def require_open(self) -> None:
    if self.state == "passed":
      return
    if self.state == "failed":
      raise LabError("gate_failed", "the last deck check failed: the deck does not match the loaded layout, so nothing may move.",
                     "The person on site fixes the deck, then verify(check='deck_matches_layout') again, or load a corrected layout.")
    check = self.waiting_for_person()
    waiting = (f"Deck check {check} waits for the person's verdict: {pass_now(check, '<ask_person from verify>')}"
               if check else None)
    if self.needs_person:
      raise LabError("gate_pending", "the person has not confirmed this layout yet, so nothing may move. "
                     + (self.differs or "No layout saved or run in this task folder to compare it with."),
                     waiting or "verify(check='deck_matches_layout') gives ask_person for the person; then record_verdict "
                     "for that check right away.")
    raise LabError("gate_pending", "the deck has not been confirmed against the loaded layout, so nothing may move.",
                   waiting or "Call verify(check='deck_matches_layout'); on real hardware it gives ask_person, and you "
                   "record_verdict for that check right away.")

  def apply(self, check_id: str, verdict: str) -> None:
    self.state, self.check_id = STATES[verdict], check_id
