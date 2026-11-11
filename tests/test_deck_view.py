"""The 3D live view inside the dashboard's Deck setup card: started by open_dashboard (no --view), found by every
dashboard through .ahc/state/view.json, and never able to break the dashboard."""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.request
from pathlib import Path

import anyio
import pytest

from ahc.viz import viewer, viewlink
from ahc.viz.dashboard import PAGE, current_state
from ahc.workspace import Workspace

from test_deck_preview import STAR_DECK, log_of, preview

pytestmark = pytest.mark.anyio

LINK = re.compile(r"http://127\.0\.0\.1:(\d+)/#token=[\w-]+")


class FakeViewer:
  """Stands in for PyLabRobot's Viewer3D: no web servers, no browser."""
  made: list["FakeViewer"] = []

  def __init__(self, root, **kwargs):
    self.root, self.kwargs, self.started = root, kwargs, False
    self.url = f"http://127.0.0.1:{kwargs['fs_port']}/#token=abc-123"
    FakeViewer.made.append(self)

  async def start(self):
    self.started = True

  async def stop(self):
    self.started = False


class BrokenViewer(FakeViewer):
  async def start(self):
    raise OSError("address already in use")


@pytest.fixture
def fake_viewer(monkeypatch):
  FakeViewer.made = []
  monkeypatch.setattr(viewer, "Viewer3D", FakeViewer)
  return FakeViewer


@pytest.fixture
def no_browser(monkeypatch):
  opened: list[str] = []
  monkeypatch.setattr("ahc.server.webbrowser.open", lambda url: opened.append(url) or True)
  return opened


def view_of(tmp_path: Path):
  return viewlink.read(Workspace(tmp_path))


def get_json(url: str):
  with urllib.request.urlopen(url, timeout=5) as r:
    return json.loads(r.read())


# -- the server starts the view when the dashboard opens -----------------------------------------------

async def test_open_dashboard_starts_the_view_on_free_ports_without_a_browser(tmp_path, open_lab, fake_viewer, no_browser):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    assert lab.server.lab.view is None  # no --view
    await preview(lab, STAR_DECK)  # a deck first: the viewer starts as soon as the dashboard is there
    assert view_of(tmp_path) is None and fake_viewer.made == []
    await lab.call("open_dashboard", open_browser=False)
    [started] = fake_viewer.made
    assert started.started and started.kwargs["open_browser"] is False
    assert started.kwargs["fs_port"] not in (1338, 1339) and started.kwargs["ws_port"] != started.kwargs["fs_port"]
    seen = view_of(tmp_path)
    assert LINK.fullmatch(seen["url"]) and seen["error"] is None and seen["real_hardware"] is False
    assert no_browser == []  # no tab of its own
    await lab.call("open_dashboard", open_browser=False)  # asked again: the same viewer
    assert len(fake_viewer.made) == 1


async def test_the_view_shows_the_preview_then_swaps_to_the_device_deck(tmp_path, open_lab, fake_viewer, no_browser):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await lab.call("open_dashboard", open_browser=False)
    assert view_of(tmp_path) is None  # nothing to show before any deck
    await preview(lab, STAR_DECK)
    view = lab.server.lab.view
    scratch = view.current
    assert scratch is not None
    assert view_of(tmp_path)["url"] == fake_viewer.made[0].url
    result = await lab.call("load_layout", plan_step=2)
    assert "live_view_url" not in result  # the page embeds it: no separate link to open
    assert view.current is not scratch and view.current is lab.server.lab.adapter.view_root()
    assert len(fake_viewer.made) == 1


async def test_the_view_flag_keeps_its_own_tab_and_ports(tmp_path, fake_viewer, no_browser):
  from mcp import Client

  from ahc.server import create_server
  from conftest import LabClient

  server = create_server(tmp_path, "sim", "hamilton.starlet", view=True)
  async with Client(server) as client:
    lab = LabClient(client, server)
    assert server.lab.view.open_browser is True and (server.lab.view.port, server.lab.view.ws_port) == (1338, 1339)
    await preview(lab, STAR_DECK)
    [started] = fake_viewer.made
    assert started.kwargs["open_browser"] is True and started.kwargs["fs_port"] == 1338
    assert view_of(tmp_path)["url"] == started.url  # the embedded page learns it all the same
    loaded = await lab.call("load_layout", plan_step=2)
    assert loaded["live_view_url"] == started.url  # today's behaviour: the link for the person's own tab


async def test_the_link_file_is_removed_when_the_server_stops_and_ignored_when_its_pid_is_gone(tmp_path, open_lab,
                                                                                                fake_viewer, no_browser):
  ws = Workspace(tmp_path)
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await lab.call("open_dashboard", open_browser=False)
    await preview(lab, STAR_DECK)
    assert viewlink.path(ws).is_file() and json.loads(viewlink.path(ws).read_text())["pid"] == os.getpid()
  assert not viewlink.path(ws).exists()  # graceful shutdown
  assert fake_viewer.made[0].started is False

  viewlink.write(ws, "http://127.0.0.1:5000/#token=t", None, False)
  assert viewlink.read(ws)["url"] == "http://127.0.0.1:5000/#token=t"
  data = json.loads(viewlink.path(ws).read_text())
  viewlink.path(ws).write_text(json.dumps({**data, "pid": 2 ** 22 + 12345}))  # a crashed server's leftover
  assert viewlink.read(ws) is None
  viewlink.path(ws).write_text(json.dumps({**data, "pid": os.getpid() + 0}))
  viewlink.path(ws).write_text(json.dumps({**data, "pid": 1, "url": data["url"]}))
  viewlink.remove(ws)  # not this process's record: left alone
  assert viewlink.path(ws).exists()


def test_the_link_is_local_only_and_a_damaged_file_is_ignored(tmp_path):
  ws = Workspace(tmp_path)
  (tmp_path / ".ahc").mkdir()
  for url in ("http://example.com:1338/#token=x", "http://127.0.0.1:1338/", "javascript:alert(1)",
              "http://127.0.0.1:1338/#token=x\"onload=\"y", "https://127.0.0.1:1338/#token=x"):
    viewlink.write(ws, url, None, False)
    assert viewlink.read(ws) is None, url
  viewlink.write(ws, None, "the 3D view could not start (OSError: busy)", True)
  assert viewlink.read(ws) == {"url": None, "error": "the 3D view could not start (OSError: busy)", "real_hardware": True}
  viewlink.path(ws).write_text("{not json")
  assert viewlink.read(ws) is None
  viewlink.path(ws).write_text("[]")
  assert viewlink.read(ws) is None


# -- what the dashboard is told ---------------------------------------------------------------------------

async def test_the_dashboard_state_carries_the_view_link_and_the_real_hardware_flag(tmp_path, open_lab, fake_viewer, no_browser):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    got = await lab.call("open_dashboard", open_browser=False)
    await preview(lab, STAR_DECK)
    state = await anyio.to_thread.run_sync(get_json, got["url"] + "state")
    assert state["view"]["url"] == fake_viewer.made[0].url and state["view"]["real_hardware"] is False
    assert state["deck"]["pinned"]
    # the standalone dashboard reads the same file from the task folder
    assert current_state(Workspace(tmp_path), log_of(lab))["view"]["url"] == fake_viewer.made[0].url
    viewlink.write(Workspace(tmp_path), fake_viewer.made[0].url, None, True)  # a real robot's server
    assert current_state(Workspace(tmp_path), log_of(lab))["view"]["real_hardware"] is True


async def test_the_state_of_a_server_without_a_view_has_none(tmp_path, open_lab, no_browser):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await preview(lab, STAR_DECK)
    assert current_state(Workspace(tmp_path), log_of(lab))["view"] is None


async def test_a_real_robot_server_marks_the_view_as_a_digital_twin(tmp_path, open_lab, fake_viewer, no_browser, monkeypatch):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await lab.call("open_dashboard", open_browser=False)
    from ahc.devices.hamilton.starlet import STARletAdapter
    monkeypatch.setattr(STARletAdapter, "simulated", property(lambda self: False))
    await preview(lab, STAR_DECK)
    assert view_of(tmp_path)["real_hardware"] is True


# -- a viewer that cannot start ----------------------------------------------------------------------------

async def test_a_viewer_that_fails_to_start_leaves_the_dashboard_and_the_calls_working(tmp_path, open_lab, monkeypatch, no_browser):
  monkeypatch.setattr(viewer, "Viewer3D", BrokenViewer)
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    got = await lab.call("open_dashboard", open_browser=False)
    shown = await preview(lab, STAR_DECK)
    assert "layout_shown" in shown  # the call itself went through
    seen = view_of(tmp_path)
    assert seen["url"] is None and "could not start" in seen["error"] and "address already in use" in seen["error"]
    state = await anyio.to_thread.run_sync(get_json, got["url"] + "state")
    assert state["view"]["error"] == seen["error"] and state["deck"]["pinned"]
    loaded = await lab.call("load_layout", plan_step=2)
    assert loaded["labware"]  # loading and the dashboard are unaffected
    monkeypatch.setattr(viewer, "Viewer3D", FakeViewer)  # a later deck tries again, and now it works
    FakeViewer.made = []
    await lab.call("open_dashboard", open_browser=False)  # asked again: the viewer is tried again, on the loaded deck
    assert view_of(tmp_path)["url"] == FakeViewer.made[0].url and view_of(tmp_path)["error"] is None


async def test_a_view_that_cannot_even_be_made_is_reported_not_raised(tmp_path, open_lab, monkeypatch, no_browser):
  def refuse(*args, **kwargs):
    raise ImportError("no module named pylabrobot.visualizer3D")
  monkeypatch.setattr(viewer, "free_port", refuse)
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await lab.call("open_dashboard", open_browser=False)
    await preview(lab, STAR_DECK)
    assert "not available" in view_of(tmp_path)["error"] and lab.server.lab.view is None


def test_free_ports_are_free_and_different():
  a, b = viewer.free_port(), viewer.free_port()
  assert a != 1338 and 1024 < a < 65536 and 1024 < b < 65536
  with socket.socket() as s:
    s.bind(("127.0.0.1", a))


# -- the page -----------------------------------------------------------------------------------------------

def test_the_page_has_the_tabs_the_label_and_one_persistent_iframe():
  assert "Top view (2D)" in PAGE and "3D live" in PAGE
  assert "Digital twin: AHC's simulated state of the deck, not a camera image." in PAGE
  assert PAGE.count('el("iframe"') == 1 and "deckOpen" not in PAGE and "<details" not in PAGE.split("function deckCard")[1].split("// Cameras")[0]
  assert "ahc-deck-tab" in PAGE and "localStorage" in PAGE
  assert "height:520px" in PAGE and "height:360px" in PAGE
  assert "X-Frame-Options" not in PAGE
