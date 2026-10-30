"""How the agent asks the person: their approval of the call, in the client's dialog, is the confirmation.

confirm_config and record_verdict carry a flag that makes the client ask the person on every call, so the agent
calls them right away instead of waiting for a typed answer. A denied call is not a confirmation: the agent stops,
asks what the person sees and records a fail with their words. A client without such a dialog gets the question in
chat first.
"""

from __future__ import annotations

NO_DIALOG = "A client that shows no approval dialog on each call: ask in chat first."
DENIED = ("If they deny it, stop, ask what they see and record_verdict(verdict='fail') with their words as the note. "
          + NO_DIALOG)


# load_layout homes the robot and has no dialog of its own: the dialog right before it carries the go-ahead.
HOMING = ("load_layout homes the robot next, so in that same message, before the call, tell the person that approving "
          "also means hands and deck are clear.")


def pass_now(check_id: str, note: str = "<ask_person>", homes: bool = False) -> str:
  """The next call for a check that waits for the person: made right away, the dialog is the question. `homes`:
  this is the last dialog before load_layout on a real robot."""
  return (f"Report waiting_for='person', then call record_verdict(check_id='{check_id}', verdict='pass', note={note}) "
          "now: the person's approval in the dialog is their confirmation; never approve for them."
          + (f" {HOMING}" if homes else ""))


def confirm_now(summary: str = "config.confirm_summary", homes: bool = False) -> str:
  """The next call for a config (or session go-ahead) that waits for the person. `homes`: a real robot, whose
  load_layout homes it."""
  return (f"Report waiting_for='person', then call confirm_config(summary={summary}) now: the person's approval in the "
          "dialog is their confirmation (denied: not confirmed; stop and ask what they want). Never approve for "
          "them. " + NO_DIALOG + (f" {HOMING}" if homes else ""))
