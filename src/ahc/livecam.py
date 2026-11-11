"""The live camera view: frames for the person watching the dashboard, produced only while someone asks for them.

One feed per camera, one capture process per device:
- a Mac camera runs one persistent ffmpeg (AVFoundation by name, about 1 frame a second, MJPEG on a pipe) and keeps the
  latest frame in memory. `capture` and `map_camera` take the next frame newer than their request from that worker
  instead of opening the device a second time, and a capture with no worker opens the device itself, under a lock the
  worker also takes before it starts: the live view never makes a capture fail with "device busy";
- the robot's built-in camera is asked of the robot's server every few seconds, never while a motion call runs. One
  request is in flight at a time, shared by the live view and by capture.

A feed starts when a page asks (`touch`) and stops by itself 30 s after the last ask. Live frames are for the person
only: they stay in memory, are never written under .ahc/runs and are never evidence (verify and the verdict tools take
capture files). Nothing here is served except by the dashboard of the AHC server process, on localhost.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ahc import cameras as cams
from ahc.cameras import CameraFailure

IDLE_S = 30.0  # a feed stops this long after the last ask
MAC_SETTLE_S = 1.5  # the first frames of a camera are dark while its exposure settles
ROBOT_PERIOD_S = 5.0  # one picture from the robot's server this often
RETRY_S = 5.0  # after a failure, the next try
FRAME_WAIT_S = 15.0  # how long capture waits for the next frame from a running feed
DISPLAY_WIDTH = cams.MAX_WIDTH  # live frames shown to the person are at most this wide
MAX_BUFFER = 24_000_000
TICK_S = 0.1


@dataclass(frozen=True)
class Frame:
  full: bytes  # as the camera gave it (what capture saves)
  shown: bytes  # at most DISPLAY_WIDTH wide (what the page shows)
  t: float  # time.time() when it arrived


def spawn(args: list[str]) -> Any:
  """Start ffmpeg with its output on a pipe. Tests replace this."""
  return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)


def shrink(data: bytes, width: int = DISPLAY_WIDTH) -> bytes:
  """The JPEG no wider than `width` (itself when narrower, or when it cannot be decoded)."""
  try:
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[1] <= width:
      return data
    small = cv2.resize(image, (width, round(image.shape[0] * width / image.shape[1])), interpolation=cv2.INTER_AREA)
    ok, out = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return out.tobytes() if ok else data
  except Exception:  # noqa: BLE001 - a live frame is a convenience
    return data


def split_jpegs(buffer: bytearray) -> list[bytes]:
  """Whole JPEGs at the front of `buffer` (removed from it); a partial one stays. ffmpeg's MJPEG output is JPEGs end to end."""
  out: list[bytes] = []
  while True:
    start = buffer.find(b"\xff\xd8")
    if start < 0:
      buffer.clear()
      return out
    end = buffer.find(b"\xff\xd9", start + 2)
    if end < 0:
      del buffer[:start]
      return out
    out.append(bytes(buffer[start:end + 2]))
    del buffer[:end + 2]


class Feed:
  """One camera's live view: the latest frame, what it is doing, and the thread that produces frames."""

  def __init__(self, name: str):
    self.name = name
    self.cond = threading.Condition()
    self.frame: Frame | None = None
    self.status = "idle"  # idle | starting | live | paused | error
    self.message = ""
    self.touched = 0.0  # time.monotonic() of the last ask
    self.running = False  # a thread is producing frames (or about to)
    self.device_lock = threading.Lock()  # one opener of a Mac camera at a time
    self.request_lock = threading.Lock()  # one request to the robot's server at a time
    self.proc: Any = None
    self.alive = threading.Event()

  def set(self, status: str, message: str = "") -> None:
    with self.cond:
      self.status, self.message = status, message
      self.cond.notify_all()

  def publish(self, frame: Frame) -> None:
    with self.cond:
      self.frame = frame
      self.status, self.message = "live", ""
      self.cond.notify_all()


class LiveCameras:
  """The live feeds of one AHC server. `lab` is the server's Lab: its config (the cameras), its device (the robot's
  address), and whether a motion call runs (`current_action`) or ran lately (`last_motion`)."""

  def __init__(self, lab: Any):
    self.lab = lab
    self.feeds: dict[str, Feed] = {}
    self.mutex = threading.Lock()
    self.closed = False

  # -- what is asked ------------------------------------------------------------------------------

  def camera(self, name: str) -> Any:
    config = self.lab.config
    return next((c for c in (config.cameras if config else []) if c.name == name), None)

  def moving(self) -> bool:
    """A call that moves the robot is running."""
    action = self.lab.current_action
    return action is not None and not action.done()

  def motion_at(self) -> float:
    return self.lab.last_motion

  def feed(self, name: str) -> Feed:
    with self.mutex:
      return self.feeds.setdefault(name, Feed(name))

  def unavailable(self, camera: Any) -> str | None:
    """Why this camera has no live view now (None: it can have one). Like capture: real hardware, a confirmed config."""
    adapter = self.lab.adapter
    if adapter is None:
      return "the live view starts once the person has confirmed the config and the device is connected."
    if adapter.simulated:
      return "this server runs a simulation, and a camera shows the real deck, not the simulator."
    if camera.is_robot and (self.lab.spec is None or camera.device != self.lab.spec.id or not hasattr(adapter, "camera_picture")):
      return f"the robot {camera.device!r} is not the device this server drives."
    if not camera.is_robot and cams.ffmpeg() is None:
      return f"ffmpeg is not installed ({cams.INSTALL_HINT})."
    return None

  def view(self, name: str) -> dict[str, Any] | None:
    """What the page shows for a camera, and the ask that keeps its feed running; None for a name the config lacks."""
    camera = self.camera(name)
    if camera is None:
      return None
    feed = self.feed(name)
    why = self.unavailable(camera)
    if why is not None:
      return {"status": "unavailable", "message": why, "t": None, "age_s": None}
    self.touch(camera, feed)
    with feed.cond:
      frame, status, message = feed.frame, feed.status, feed.message
    now = time.time()
    return {"status": status, "message": message, "t": frame.t if frame else None,
            "age_s": round(now - frame.t, 1) if frame else None}

  def frame(self, name: str) -> bytes | None:
    """The latest frame for the page (also an ask). None when there is none yet."""
    camera = self.camera(name)
    if camera is None or self.unavailable(camera) is not None:
      return None
    feed = self.feed(name)
    self.touch(camera, feed)
    with feed.cond:
      return feed.frame.shown if feed.frame else None

  def touch(self, camera: Any, feed: Feed) -> None:
    """Somebody is watching: start the feed if it is not running, and keep it for another IDLE_S."""
    with self.mutex:
      feed.touched = time.monotonic()
      if feed.running or self.closed:
        return
      feed.running = True
      feed.alive.clear()
      feed.set("starting", "")
    target = self._robot_loop if camera.is_robot else self._mac_loop
    threading.Thread(target=target, args=(camera, feed), daemon=True, name=f"ahc-live-{feed.name}").start()

  def stop(self) -> None:
    """End every feed (the server shuts down)."""
    with self.mutex:
      self.closed = True
      feeds = list(self.feeds.values())
    for feed in feeds:
      feed.alive.set()
      proc = feed.proc
      if proc is not None:
        _end(proc)
    for feed in feeds:
      deadline = time.monotonic() + 3
      while feed.running and time.monotonic() < deadline:
        time.sleep(0.02)

  def _wanted(self, feed: Feed) -> bool:
    return not self.closed and not feed.alive.is_set() and time.monotonic() - feed.touched < IDLE_S

  def _finish(self, feed: Feed) -> None:
    with self.mutex:
      feed.running = False
      feed.proc = None
    feed.set("idle", "")

  # -- a Mac camera: one persistent ffmpeg ----------------------------------------------------------

  def _mac_loop(self, camera: Any, feed: Feed) -> None:
    try:
      while self._wanted(feed):
        failed = self._mac_run(camera, feed)
        if failed is not None:
          feed.set("error", failed)
          deadline = time.monotonic() + RETRY_S
          while self._wanted(feed) and time.monotonic() < deadline:
            time.sleep(TICK_S)
    finally:
      self._finish(feed)

  def _mac_run(self, camera: Any, feed: Feed) -> str | None:
    """One ffmpeg until it ends or nobody watches; None when it was stopped on purpose, else why it ended."""
    try:
      binary = cams.require_ffmpeg()
    except Exception as exc:  # noqa: BLE001
      return str(exc)
    args = [binary, "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-framerate", "30", "-pixel_format", "nv12",
            "-i", f"{camera.device}:none", "-an", "-ss", str(MAC_SETTLE_S), "-vf", "fps=1", "-q:v", "2", "-f", "mjpeg", "pipe:1"]
    with feed.device_lock:  # a capture that holds the device lets it go first
      if not self._wanted(feed):
        return None
      try:
        proc = spawn(args)
      except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
      feed.proc = proc
    errors: list[bytes] = []
    reader = threading.Thread(target=self._read, args=(proc, feed, errors), daemon=True)
    reader.start()
    try:
      while proc.poll() is None and self._wanted(feed):
        time.sleep(TICK_S)
    finally:
      if proc.poll() is None:
        _end(proc)
      reader.join(2)
    feed.proc = None
    if not self._wanted(feed):
      return None
    text = b"".join(errors).decode(errors="replace").strip().splitlines()
    return (text[-1] if text else f"ffmpeg ended with code {proc.poll()} and no frame")[:300]

  def _read(self, proc: Any, feed: Feed, errors: list[bytes]) -> None:
    buffer = bytearray()
    try:
      while True:
        chunk = proc.stdout.read1(65536)
        if not chunk:
          break
        buffer += chunk
        if len(buffer) > MAX_BUFFER:
          buffer.clear()
        for jpeg in split_jpegs(buffer):
          feed.publish(Frame(jpeg, shrink(jpeg), time.time()))
    except (OSError, ValueError):
      pass
    try:
      errors.append(proc.stderr.read() or b"")
    except (OSError, ValueError, AttributeError):
      pass

  def grab_mac(self, camera: Any, dest: Path, after: Callable[[], float]) -> str | None:
    """A picture from a Mac camera into `dest` (the camera's own size); None when it worked, else why not. From the
    running feed: the next frame newer than `after()`. With no feed: the device is opened for this one frame, and a feed
    that starts meanwhile waits for it."""
    feed = self.feed(camera.name)
    with self.mutex:
      running = feed.running
    if running:
      deadline = time.monotonic() + FRAME_WAIT_S
      with feed.cond:
        while True:
          frame = feed.frame
          if frame is not None and frame.t > after():
            break
          if feed.status == "error" or not feed.running or time.monotonic() >= deadline:
            frame = None
            break
          feed.cond.wait(0.1)
      if frame is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(frame.full)
        return None
      # the feed gave nothing: it may have ended (idle), or the device has a problem the direct way reports
    with feed.device_lock:
      return cams.capture_frame(camera.device, dest)

  # -- the robot's built-in camera ------------------------------------------------------------------

  def picture(self, camera: Any, wait_for_motion: bool = False) -> bytes:
    """One picture from the robot's server, never while a motion call runs, one request at a time (the live view and
    capture share the lock). Raises CameraFailure."""
    adapter, feed = self.lab.adapter, self.feed(camera.name)
    if adapter is None or not hasattr(adapter, "camera_picture"):
      raise CameraFailure("camera_unavailable", "no robot is connected to take the picture.", "Confirm the config first.")
    with feed.request_lock:
      for _ in range(2):  # a motion that began during the request makes its picture useless: once more
        if self.moving():
          raise CameraFailure("robot_busy", "a call that moves the robot is running; no picture is taken while it does.",
                              "Capture again when it has ended.")
        started = time.time()
        marker = self.motion_at()
        data = adapter.camera_picture(cams.ROBOT_TIMEOUT_S)
        if not self.moving() and self.motion_at() == marker and self.motion_at() <= started:
          return data
      raise CameraFailure("robot_busy", "the robot moved while its picture was taken.", "Capture again when it has stopped.")

  def _robot_loop(self, camera: Any, feed: Feed) -> None:
    try:
      while self._wanted(feed):
        wait = ROBOT_PERIOD_S
        if self.moving():
          feed.set("paused", "paused while the robot moves")
          wait = 0.5
        else:
          try:
            data = self.picture(camera)
          except CameraFailure as exc:
            if exc.code == "robot_busy":
              feed.set("paused", "paused while the robot moves")
              wait = 0.5
            else:
              feed.set("error", exc.why)
              wait = RETRY_S
          except Exception as exc:  # noqa: BLE001
            feed.set("error", f"{type(exc).__name__}: {exc}")
            wait = RETRY_S
          else:
            feed.publish(Frame(data, data, time.time()))
        deadline = time.monotonic() + wait
        while self._wanted(feed) and time.monotonic() < deadline:
          time.sleep(TICK_S)
    finally:
      self._finish(feed)


def _end(proc: Any) -> None:
  """Stop an ffmpeg: politely, then for sure."""
  try:
    proc.terminate()
    try:
      proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
      proc.kill()
  except Exception:  # noqa: BLE001
    pass
