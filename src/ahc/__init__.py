"""Agentic Hardware Control (AHC): an agent-agnostic MCP hardware layer for lab robots (prototype)."""

from importlib import metadata

try:
  __version__ = metadata.version("ahc")  # one source: pyproject.toml (the plugin manifest is checked against it)
except metadata.PackageNotFoundError:  # a source tree that was never installed
  __version__ = "0+unknown"
