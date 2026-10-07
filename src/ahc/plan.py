"""The agent's plan and how far it got, for the person watching.

The agent reports its plan with report_decision before load_layout or any action. From then on every
load_layout, verify and action call names the step it belongs to (`plan_step`), so progress needs no
extra reports: a step is done once the agent moves on to a later one, steps it jumped over show as
skipped, and report_decision(plan_done=true) ends the plan (that report is its last step, which is
usually "report the result"). The server keeps one PlanProgress, fed by its own run log entries; the
dashboard rebuilds the same one from the log file, so the two never disagree.
"""

from __future__ import annotations

import time
from typing import Any

from ahc.core.errors import LabError

MAX_STEPS = 40


class PlanProgress:
  def __init__(self) -> None:
    self.steps: list[str] | None = None
    self.at: str | None = None  # when the plan was reported
    self.current: int | None = None
    self.visited: set[int] = set()
    self.done = False

  def report(self, plan: list[str] | None = None, current_step: int | None = None, plan_done: bool = False,
             t: str | None = None) -> None:
    if plan is not None:  # a new plan replaces the old one and its progress
      self.steps, self.at = list(plan), t or time.strftime("%Y-%m-%dT%H:%M:%S")
      self.current, self.visited, self.done = None, set(), False
    if self.steps is None:
      return
    if current_step is not None:
      self.enter(current_step)
    if plan_done:
      self.enter(len(self.steps))  # the final report is the last step
      self.done = True

  def enter(self, step: int) -> None:
    if self.steps is None or not 1 <= step <= len(self.steps):
      return
    self.current = step
    self.visited.add(step)
    self.done = False  # a call after plan_done means the agent went on working

  def apply(self, entry: dict[str, Any]) -> None:
    """One run log entry: a report changes the plan, a call names its step."""
    if entry.get("type") == "agent":
      self.report(entry.get("plan"), entry.get("current_step"), bool(entry.get("plan_done")), entry.get("t"))
      for step in entry.get("visited") or []:  # a plan carried into a new run log keeps its progress
        if isinstance(step, int) and self.steps and 1 <= step <= len(self.steps):
          self.visited.add(step)
    elif isinstance(entry.get("plan_step"), int):
      self.enter(entry["plan_step"])

  def check(self, step: int | None) -> None:
    """Refuse a call made before any plan, or naming a step outside it."""
    if self.steps is None:
      raise LabError("plan_required", "no plan reported to this server yet (a restarted server has forgotten it): "
                     "nothing is loaded or moved before the plan.",
                     "Call report_decision with plan=[short concrete steps] first, then pass plan_step "
                     "(the step this call belongs to) on every load_layout, verify and action call.")
    if step is None or not 1 <= step <= len(self.steps):
      raise LabError("bad_plan_step", f"plan_step {step} is not a step of your plan (1-{len(self.steps)}).",
                     "Pass the step this call belongs to, or report a revised plan with report_decision.")

  def statuses(self) -> list[str]:
    """Per step: done, now, next, or skipped (passed over without a call or report)."""
    out = []
    for s in range(1, len(self.steps or []) + 1):
      if self.current is None:
        out.append("next")
      elif s == self.current:
        out.append("done" if self.done else "now")
      elif s in self.visited:
        out.append("done")
      else:
        out.append("skipped" if s < self.current else "next")
    return out

  def public(self) -> dict[str, Any] | None:
    if self.steps is None:
      return None
    return {"steps": self.steps, "status": self.statuses(), "current_step": self.current, "done": self.done,
            "at": self.at}
