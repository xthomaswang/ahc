"""Refusals the agent can act on.

MCP 2.x hides the text of unexpected exceptions from the client, so every anticipated refusal is a
`ToolError`: its message reaches the model and should say what to change.
"""

from mcp.server.mcpserver.exceptions import ToolError


class LabError(ToolError):
  """An anticipated refusal, with a machine-readable code and a hint for the next attempt."""

  def __init__(self, code: str, message: str, hint: str | None = None):
    self.code = code
    self.message = message
    self.hint = hint
    super().__init__(f"[{code}] {message}" + (f" Hint: {hint}" if hint else ""))
