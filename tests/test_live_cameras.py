"""The live camera view: feeds that start when a page asks and stop when it stops asking, capture sharing the Mac
camera's feed (never opening the device twice), the robot camera's one request at a time, frames that stay in memory,
and the dashboard routes that serve them. ffmpeg is faked (cameras._run, livecam.spawn); the robot is the fake server."""

import json
import os
import threading
import time

import anyio
import pytest

from ahc import cameras as cams
from ahc import livecam
from ahc.examples import OT2_LAYOUT
from ahc.viz.dashboard import PAGE, serve
from ahc.workspace import Workspace, estop
from conftest import load_verified
from fake_robot import make_jpeg
from test_cameras import FULL, SIDE, STARLET, TOP, FakeFfmpeg, ffmpeg, home, real_lab  # noqa: F401 - fixtures
from test_dashboard import _http
from test_robot_camera import ROBOT_CAM, robot_cam_lab

pytestmark = pytest.mark.anyio

BIG = make_jpeg(1920, 1080, "big")  # wider than the page shows


class FakeProc:
  """What livecam.spawn returns: ffmpeg's stdout is a pipe the test writes JPEGs into."""

  def __init__(self, args, stderr=b"", exit_now=False):
    self.args, self.returncode, self.terminated = args, None, False
    read, self._write = os.pipe()
    self.stdout = os.fdopen(read, "rb")
    err_read, self._err_write = os.pipe()
    self.stderr = os.fdopen(err_read, "rb")
    if stderr:
      os.write(self._err_write, stderr)
    if exit_now:
      self._close(1)

  def _close(self, code):
    if self.returncode is None:
      self.returncode = code
      for fd in (self._write, self._err_write):
        os.close(fd)

  def send(self, jpeg: bytes) -> None:
    os.write(self._write, jpeg)

  def poll(self):
    return self.returncode

  def terminate(self):
    self.terminated = True
    self._close(-15)

  kill = terminate

  def wait(self, timeout=None):
    return self.returncode


class Spawner:
  def __init__(self):
    self.procs: list[FakeProc] = []
    self.stderr, self.exit_now = b"", False
    self.on_spawn = None

  def __call__(self, args):
    if self.on_spawn:
      self.on_spawn()
    proc = FakeProc(args, self.stderr, self.exit_now)
    self.procs.append(proc)
    return proc


@pytest.fixture
def spawner(monkeypatch):
  fake = Spawner()
  monkeypatch.setattr(livecam, "spawn", fake)
  return fake


@pytest.fixture(autouse=True)
def quick(monkeypatch):
  monkeypatch.setattr(livecam, "TICK_S", 0.02)


def until(condition, seconds: float = 5.0):
  deadline = time.monotonic() + seconds
  while not condition():
    assert time.monotonic() < deadline, "timed out"
    time.sleep(0.02)


def width_of(jpeg: bytes) -> int:
  import cv2
  import numpy as np

  return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape[1]


async def watched(open_lab, folder, bot, ffmpeg):
  cm, lab = await real_lab(open_lab, folder, bot)
  return cm, lab, lab.server.lab.live


# -- a Mac camera's feed -----------------------------------------------------------------------------

async def test_a_feed_starts_when_asked_and_stops_when_nobody_asks(tmp_path, open_lab, fake_robot, ffmpeg, spawner, monkeypatch):
  monkeypatch.setattr(livecam, "IDLE_S", 0.6)
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  try:
    assert spawner.procs == []  # nothing runs until a page asks
    assert live.view("top")["status"] in ("starting", "idle")
    until(lambda: len(spawner.procs) == 1)
    args = spawner.procs[0].args
    assert args[args.index("-i") + 1] == "USB Camera:none" and "fps=1" in args and args[-1] == "pipe:1" and "mjpeg" in args
    live.view("top")
    live.view("top")
    time.sleep(0.2)
    assert len(spawner.procs) == 1  # asking again is not another process
    assert live.frame("top") is None and live.view("top")["t"] is None  # no frame yet
    spawner.procs[0].send(BIG)
    until(lambda: live.view("top")["status"] == "live")
    shown = live.frame("top")
    assert shown.startswith(b"\xff\xd8") and width_of(shown) == 1280 and shown != BIG  # at most 1280 px wide
    info = live.view("top")
    assert info["t"] and info["age_s"] is not None and info["age_s"] < 5
    until(lambda: spawner.procs[0].terminated, 4)  # nobody asked for IDLE_S: the process ends
    until(lambda: not live.feed("top").running)
    assert live.feed("top").status == "idle"
    live.view("top")  # asked again: a new one
    until(lambda: len(spawner.procs) == 2)
  finally:
    live.stop()
    await cm.__aexit__(None, None, None)


async def test_cameras_are_independent_and_unknown_names_have_no_view(tmp_path, open_lab, fake_robot, ffmpeg, spawner):
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  try:
    assert live.view("nope") is None and live.frame("nope") is None
    live.view("top")
    until(lambda: len(spawner.procs) == 1)
    time.sleep(0.2)
    assert spawner.procs[0].args[spawner.procs[0].args.index("-i") + 1] == "USB Camera:none"  # "side" was not asked for
    live.view("side")
    until(lambda: len(spawner.procs) == 2)
    assert spawner.procs[1].args[spawner.procs[1].args.index("-i") + 1] == SIDE["device"] + ":none"
  finally:
    live.stop()
    await cm.__aexit__(None, None, None)


async def test_a_feed_that_ends_reports_why_and_tries_again(tmp_path, open_lab, fake_robot, ffmpeg, spawner, monkeypatch):
  monkeypatch.setattr(livecam, "RETRY_S", 0.2)
  spawner.stderr, spawner.exit_now = b"warning\nSelected video device not found\n", True
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  try:
    live.view("top")
    until(lambda: live.view("top")["status"] == "error")
    assert live.view("top")["message"] == "Selected video device not found"
    until(lambda: len(spawner.procs) >= 2)  # asked again while it is watched
    spawner.exit_now = False
    until(lambda: len(spawner.procs) >= 3)
    spawner.procs[-1].send(FULL)
    until(lambda: live.view("top")["status"] == "live")
  finally:
    live.stop()
    await cm.__aexit__(None, None, None)


async def test_no_live_view_in_simulation_or_without_a_confirmed_device(tmp_path, open_lab, spawner, ffmpeg):
  async with open_lab(tmp_path, "sim") as lab:
    await lab.call("configure_devices", devices=STARLET, cameras=[TOP])
    live = lab.server.lab.live
    await lab.call("lab_overview")
    info = live.view("top")
    assert info["status"] == "unavailable" and "simulation" in info["message"] and live.frame("top") is None
  assert spawner.procs == []


# -- capture shares the feed ----------------------------------------------------------------------------

async def test_capture_takes_the_next_frame_from_the_running_feed(tmp_path, open_lab, fake_robot, ffmpeg, spawner):
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  try:
    server = lab.server.lab
    live.view("top")
    until(lambda: len(spawner.procs) == 1)
    spawner.procs[0].send(make_jpeg(label="old"))
    until(lambda: live.feed("top").frame is not None)
    fresh = make_jpeg(label="next")
    earlier = make_jpeg(label="before the last motion")

    server.last_motion = time.time() + 0.5  # the last motion ends half a second from now

    def frames():
      time.sleep(0.2)
      spawner.procs[0].send(earlier)  # newer than the request, but not than the motion: not used
      time.sleep(0.6)
      spawner.procs[0].send(fresh)

    threading.Thread(target=frames, daemon=True).start()
    result = await lab.call("capture", label="from the feed", cameras=["top"])
    assert cams.Path(result["images"][0]["path"]).read_bytes() == fresh  # the file is the camera's own frame
    assert ffmpeg.captures() == []  # the device was not opened a second time
    assert len(spawner.procs) == 1
  finally:
    live.stop()
    await cm.__aexit__(None, None, None)


async def test_capture_without_a_feed_opens_the_device_as_before(tmp_path, open_lab, fake_robot, ffmpeg, spawner):
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  try:
    result = await lab.call("capture", label="no feed", cameras=["top"])
    assert cams.Path(result["images"][0]["path"]).read_bytes() == FULL
    assert [c[c.index("-i") + 1] for c in ffmpeg.captures()] == ["USB Camera:none"] and spawner.procs == []
  finally:
    await cm.__aexit__(None, None, None)


async def test_a_feed_waits_for_a_capture_that_holds_the_device(tmp_path, open_lab, fake_robot, ffmpeg, spawner):
  """A capture that opens the device and a feed that starts meanwhile never hold it together."""
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  try:
    entered, release = threading.Event(), threading.Event()
    real = ffmpeg.__call__

    def slow(args, timeout):
      if "-list_devices" not in args and "-vf" not in args:
        entered.set()
        release.wait(5)
      return real(args, timeout)

    cams._run = slow
    spawner.on_spawn = lambda: spawned_while_open.append(entered.is_set() and not release.is_set())
    spawned_while_open: list[bool] = []
    async with anyio.create_task_group() as tg:
      async def capture():
        await lab.call("capture", label="direct", cameras=["top"])
      tg.start_soon(capture)
      with anyio.fail_after(5):
        while not entered.is_set():
          await anyio.sleep(0.02)
      live.view("top")  # a page asks while the capture holds the device
      await anyio.sleep(0.4)
      assert spawner.procs == []  # the feed waits its turn
      release.set()
    until(lambda: len(spawner.procs) == 1)
    assert spawned_while_open == [False]  # it opened the device only after the capture let go
  finally:
    release.set()
    live.stop()
    await cm.__aexit__(None, None, None)


async def test_a_feed_that_does_not_deliver_does_not_block_capture(tmp_path, open_lab, fake_robot, ffmpeg, spawner):
  spawner.stderr, spawner.exit_now = b"Device busy\n", True
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  try:
    live.view("top")
    until(lambda: live.view("top")["status"] == "error")
    result = await lab.call("capture", label="direct", cameras=["top"])  # the feed has the problem: capture tries the device itself
    assert cams.Path(result["images"][0]["path"]).read_bytes() == FULL
  finally:
    live.stop()
    await cm.__aexit__(None, None, None)


async def test_map_camera_takes_its_picture_the_same_way(tmp_path, open_lab, fake_robot, ffmpeg, spawner):
  """map_camera asks Lab.snapshot, like capture: a running feed gives its next frame, the device is not opened again."""
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  try:
    server = lab.server.lab
    live.view("top")
    until(lambda: len(spawner.procs) == 1)
    threading.Timer(0.3, lambda: spawner.procs[0].send(FULL)).start()
    dest = tmp_path / "frame.jpg"
    assert await server.snapshot(server.config.cameras[0], dest, time.time()) is None
    assert dest.read_bytes() == FULL and ffmpeg.captures() == []
  finally:
    live.stop()
    await cm.__aexit__(None, None, None)


async def test_live_frames_are_never_written_under_the_runs_folder(tmp_path, open_lab, fake_robot, ffmpeg, spawner):
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  try:
    live.view("top")
    until(lambda: len(spawner.procs) == 1)
    for _ in range(3):
      spawner.procs[0].send(FULL)
      time.sleep(0.05)
    until(lambda: live.frame("top") is not None)
    assert list((tmp_path / ".ahc").rglob("*.jpg")) == [] and list((tmp_path / ".ahc" / "runs").glob("*/*")) == []  # nothing on disk
    threading.Timer(0.2, lambda: spawner.procs[0].send(FULL)).start()
    taken = await lab.call("capture", label="evidence", cameras=["top"])  # a capture is a file
    assert [p.name for p in (tmp_path / ".ahc").rglob("*.jpg")] == [cams.Path(taken["images"][0]["path"]).name]
    # a live frame is no evidence: verify takes only the files capture wrote
    assert await lab.refused("verify", check="tips_mounted", expect="none", images=["live.jpg"]) in ("image_outside_run", "image_not_found")
  finally:
    live.stop()
    await cm.__aexit__(None, None, None)


# -- the robot camera's feed ----------------------------------------------------------------------------

async def test_the_robot_feed_asks_every_few_seconds_and_pauses_while_the_robot_moves(tmp_path, open_lab, fake_robot, monkeypatch):
  monkeypatch.setattr(livecam, "ROBOT_PERIOD_S", 0.2)
  monkeypatch.setattr(livecam, "IDLE_S", 1.0)
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot)
  live = lab.server.lab.live
  try:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device="ot2.right", tips=["tips:A1:H1"])
    live.view("deck")
    until(lambda: live.view("deck")["status"] == "live")
    assert live.frame("deck") == fake_robot.picture  # the robot's 640 px picture, as it is
    fake_robot.durations["aspirateInPlace"] = 1.2
    live.view("deck")

    async def keep_watching():
      while True:
        live.view("deck")
        await anyio.sleep(0.1)

    async with anyio.create_task_group() as tg:
      tg.start_soon(keep_watching)
      async def aspirate():
        await lab.call("aspirate", device="ot2.right", targets=["diluent"], volumes=100)
      tg.start_soon(aspirate)
      with anyio.fail_after(5):
        while not any(m == "EXEC-START" and p == "aspirateInPlace" for _, m, p, _ in fake_robot.log):
          await anyio.sleep(0.02)
      with anyio.fail_after(3):
        while live.view("deck")["status"] != "paused":
          await anyio.sleep(0.02)
      assert "robot moves" in live.view("deck")["message"]
      during = fake_robot.pictures_taken
      await anyio.sleep(0.6)
      assert fake_robot.pictures_taken == during  # no picture while the call runs
      with anyio.fail_after(5):
        while live.view("deck")["status"] != "live" or fake_robot.pictures_taken == during:
          await anyio.sleep(0.05)  # and it goes on afterwards
      tg.cancel_scope.cancel()
    until(lambda: not live.feed("deck").running, 4)  # nobody asks: it ends
    n = fake_robot.pictures_taken
    time.sleep(0.5)
    assert fake_robot.pictures_taken == n
  finally:
    live.stop()
    await cm.__aexit__(None, None, None)


async def test_the_live_view_and_capture_share_one_request_at_a_time(tmp_path, open_lab, fake_robot, monkeypatch):
  monkeypatch.setattr(livecam, "ROBOT_PERIOD_S", 0.05)
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot)
  live = lab.server.lab.live
  try:
    fake_robot.picture_seconds = 0.25
    live.view("deck")

    async def keep_watching():
      while True:
        live.view("deck")
        await anyio.sleep(0.05)

    async with anyio.create_task_group() as tg:
      tg.start_soon(keep_watching)
      await anyio.sleep(0.3)
      for _ in range(2):
        assert len((await lab.call("capture", label="while watched"))["images"]) == 1
      tg.cancel_scope.cancel()
    assert fake_robot.max_pictures_in_flight == 1
  finally:
    live.stop()
    await cm.__aexit__(None, None, None)


async def test_a_disabled_robot_camera_shows_in_the_feed(tmp_path, open_lab, fake_robot, monkeypatch):
  monkeypatch.setattr(livecam, "RETRY_S", 0.2)
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot)
  live = lab.server.lab.live
  try:
    fake_robot.camera_enabled = False
    live.view("deck")
    until(lambda: live.view("deck")["status"] == "error")
    assert "turned off" in live.view("deck")["message"]
    fake_robot.camera_enabled = True
    until(lambda: live.view("deck")["status"] == "live")
  finally:
    live.stop()
    await cm.__aexit__(None, None, None)


# -- the dashboard routes -----------------------------------------------------------------------------

async def test_the_server_dashboard_serves_live_frames_and_protects_them(tmp_path, open_lab, fake_robot, ffmpeg, spawner):
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  try:
    page = await lab.call("open_dashboard", open_browser=False)
    port = int(page["url"].rstrip("/").rsplit(":", 1)[1])
    status, headers, body = await anyio.to_thread.run_sync(lambda: _http(port, "/camera/top/live.json"))
    assert status == 200 and json.loads(body)["status"] in ("starting", "live")
    until(lambda: len(spawner.procs) == 1)  # asking by the page started the feed
    spawner.procs[0].send(BIG)
    until(lambda: live.frame("top") is not None)
    status, headers, body = await anyio.to_thread.run_sync(lambda: _http(port, "/camera/top/live.jpg?t=1"))
    assert status == 200 and headers["content-type"] == "image/jpeg" and body == live.frame("top") and width_of(body) == 1280
    assert headers["cache-control"] == "no-store" and headers["cross-origin-resource-policy"] == "same-origin"
    assert headers["x-frame-options"] == "DENY"
    status, _, body = await anyio.to_thread.run_sync(lambda: _http(port, "/state"))
    assert json.loads(body)["live"] is True
    # protected like the reference image: our own name and port only, from our own page or no page
    assert (await anyio.to_thread.run_sync(lambda: _http(port, "/camera/top/live.jpg", Host="evil.example")))[0] == 403
    assert (await anyio.to_thread.run_sync(lambda: _http(port, "/camera/top/live.json", Host=f"rebind.example:{port}")))[0] == 403
    assert (await anyio.to_thread.run_sync(lambda: _http(port, "/camera/top/live.jpg", Origin="http://evil.example")))[0] == 403
    assert (await anyio.to_thread.run_sync(lambda: _http(port, "/camera/nope/live.json")))[0] == 404
    assert (await anyio.to_thread.run_sync(lambda: _http(port, "/camera/Top/live.jpg")))[0] == 404
    # it keeps working during an emergency stop
    estop.engage(lab.server.lab.ws, by="test")
    assert (await anyio.to_thread.run_sync(lambda: _http(port, "/camera/top/live.jpg")))[0] == 200
    assert (await anyio.to_thread.run_sync(lambda: _http(port, "/camera/top/live.json")))[0] == 200
    # and the view-only page serves it too
    viewer = await lab.call("open_dashboard", open_browser=False, view_only=True)
    vport = int(viewer["url"].rstrip("/").rsplit(":", 1)[1])
    status, _, body = await anyio.to_thread.run_sync(lambda: _http(vport, "/camera/top/live.jpg"))
    assert status == 200 and body == live.frame("top")
  finally:
    live.stop()
    await cm.__aexit__(None, None, None)


async def test_the_standalone_dashboard_has_no_live_frames_and_says_so(tmp_path, open_lab, fake_robot, ffmpeg, spawner):
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  try:
    await lab.call("report_decision", decision="x", plan=["a"])
    httpd = serve(Workspace(tmp_path), None, 0, open_browser=False)
    try:
      port = httpd.server_address[1]
      status, _, body = await anyio.to_thread.run_sync(lambda: _http(port, "/camera/top/live.json"))
      assert status == 404 and "server's own dashboard" in json.loads(body)["error"]
      assert (await anyio.to_thread.run_sync(lambda: _http(port, "/camera/top/live.jpg")))[0] == 404
      status, _, body = await anyio.to_thread.run_sync(lambda: _http(port, "/state"))
      assert json.loads(body)["live"] is False
    finally:
      httpd.shutdown()
      httpd.server_close()
    assert spawner.procs == []  # nothing asked, nothing started
  finally:
    await cm.__aexit__(None, None, None)


async def test_the_server_stops_its_feeds_when_it_shuts_down(tmp_path, open_lab, fake_robot, ffmpeg, spawner):
  cm, lab, live = await watched(open_lab, tmp_path, fake_robot, ffmpeg)
  live.view("top")
  until(lambda: len(spawner.procs) == 1)
  await cm.__aexit__(None, None, None)
  until(lambda: spawner.procs[0].terminated)
  assert not live.feed("top").running


# -- the tab ----------------------------------------------------------------------------------------------

def test_the_page_has_the_cameras_tab_and_asks_for_frames_only_while_it_is_visible():
  assert 'b4=el("button",{role:"tab",type:"button"},"Cameras")' in PAGE and '"Top view (2D)"' in PAGE and '"3D live"' in PAGE
  assert "/live.json" in PAGE and "/live.jpg" in PAGE and "slot boxes" in PAGE and "document.hidden" in PAGE
  assert "camPoll(tab===\"cams\")" in PAGE and 'addEventListener("visibilitychange"' in PAGE  # asks only while the tab shows
  assert "come from the AHC server's own dashboard" in PAGE  # what the standalone page says
  assert "paused" in PAGE and "s ago" in PAGE
