"""Live 3D view of the connected device, using PyLabRobot's Viewer3D.

The viewer draws whatever resource tree it is given and follows its tracking state, so tips leave
their rack and wells fill as the capability layer runs. A layout change swaps the device in the
tree and the viewer rebuilds.
"""

from __future__ import annotations

import builtins
import functools
import sys

import pylabrobot.visualizer3D.server as _viewer_server
from pylabrobot.resources import Coordinate, Resource
from pylabrobot.visualizer3D import Viewer3D

# Viewer3D prints its link and connection notices; under the MCP stdio transport stdout is the
# protocol channel, so its prints go to stderr instead.
_viewer_server.print = functools.partial(builtins.print, file=sys.stderr)


class LiveView:
  def __init__(self, title: str, open_browser: bool = True, port: int = 1338):
    self.root: Resource | None = None
    self.title = title
    self.open_browser = open_browser
    self.port = port
    self.viewer: Viewer3D | None = None
    self.current: Resource | None = None

  async def show(self, resource: Resource) -> str:
    """Put this resource in the view (replacing the previous one) and return the viewer link."""
    if self.root is None:
      # The view frames the root's box, so it is sized to the device (a reloaded layout brings the
      # same model back); the OT-2 deck reports no height, so leave room for labware.
      self.root = Resource(name="lab", size_x=resource.get_size_x(), size_y=resource.get_size_y(),
                           size_z=max(resource.get_size_z(), 300.0), category="facility")
    if resource is not self.current:
      if self.current is not None:
        self.root.unassign_child_resource(self.current)
      self.root.assign_child_resource(resource, location=Coordinate(0, 0, 0))
      self.current = resource
    if self.viewer is None:
      self.viewer = Viewer3D(self.root, open_browser=self.open_browser, name=self.title,
                             fs_port=self.port, ws_port=self.port + 1)
      await self.viewer.start()
    return self.viewer.url

  @property
  def url(self) -> str | None:
    return self.viewer.url if self.viewer is not None else None

  async def wait_for_browser(self, timeout: float) -> bool:
    if self.viewer is None:
      return False
    try:
      await self.viewer.wait_for_browser(timeout)
      return True
    except TimeoutError:
      return False

  async def stop(self) -> None:
    if self.viewer is not None:
      await self.viewer.stop()
      self.viewer = None
