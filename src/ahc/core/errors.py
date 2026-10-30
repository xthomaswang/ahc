"""Refusals the agent can act on.

MCP 2.x hides the text of unexpected exceptions from the client, so every anticipated refusal is a
`ToolError`: its message reaches the model and should say what to change.
"""

import math
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from mcp.server.mcpserver.exceptions import ToolError


class LabError(ToolError):
  """An anticipated refusal, with a machine-readable code and a hint for the next attempt."""

  def __init__(self, code: str, message: str, hint: str | None = None):
    self.code = code
    self.message = message
    self.hint = hint
    super().__init__(f"[{code}] {message}" + (f" Hint: {hint}" if hint else ""))


def at_most(value: float, places: int) -> str:
  """An upper bound as a hint's "Use at most" shows it: rounded down to `places` decimals, so the number shown passes
  the check it came from (rounded to the nearest, a 1.46 mm bound would show as 1.5, which that check refuses)."""
  if not math.isfinite(value):
    return f"{value:g}"
  try:  # the float's shortest decimal, cut: never above the value itself
    shown = Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_FLOOR)
  except InvalidOperation:
    return f"{value:g}"
  return f"{shown.normalize():f}"
