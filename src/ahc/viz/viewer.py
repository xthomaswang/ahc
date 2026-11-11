"""Live 3D view of the connected device, using PyLabRobot's Viewer3D.

The viewer draws whatever resource tree it is given and follows its tracking state, so tips leave
their rack and wells fill as the capability layer runs. A layout change swaps the device in the
tree and the viewer rebuilds.
"""

from __future__ import annotations

import builtins
import functools
import socket
import sys

import pylabrobot.visualizer3D.server as _viewer_server
from pylabrobot.resources import Coordinate, Resource
from pylabrobot.visualizer3D import Viewer3D

# Viewer3D prints its link and connection notices; under the MCP stdio transport stdout is the
# protocol channel, so its prints go to stderr instead.
_viewer_server.print = functools.partial(builtins.print, file=sys.stderr)


def free_port() -> int:
  """A port nothing listens on now (taken from the system, so two task folders' servers never share one)."""
  with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    return probe.getsockname()[1]


class LiveView:
  def __init__(self, title: str, open_browser: bool = True, port: int = 1338, ws_port: int | None = None):
    self.root: Resource | None = None
    self.title = title
    self.open_browser = open_browser
    self.port = port
    self.ws_port = ws_port if ws_port is not None else port + 1
    self.viewer: Viewer3D | None = None
    self.current: Resource | None = None

  async def show(self, resource: Resource) -> str:
    """Put this resource in the view (replacing the previous one) and return the viewer link."""
    if self.root is not None:  # a later resource (the device's, after a preview) that is larger than the box: grow it
      for axis, need in (("size_x", resource.get_size_x()), ("size_y", resource.get_size_y()),
                         ("size_z", resource.get_size_z())):
        if need > getattr(self.root, f"_{axis}"):
          setattr(self.root, f"_{axis}", need)
          if axis == "size_z":
            self.root._local_size_z = need
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
      viewer = Viewer3D(self.root, open_browser=self.open_browser, name=self.title,
                        fs_port=self.port, ws_port=self.ws_port)
      await viewer.start()  # kept only once it runs: a viewer that failed to start is tried again at the next deck
      self.viewer = viewer
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
