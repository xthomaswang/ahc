"""The bridge: forwards firmware commands between AHC and a Hamilton STAR on the machine the instrument's USB cable
is plugged into (a Raspberry Pi: macOS never enumerates the STAR).

This is one standalone file. It is copied to the USB host and run there (`python star_bridge.py serve`) with a Python
that has PyLabRobot and its `usb` extra. At import time it needs the standard library only; PyLabRobot is imported
where USB is opened. It imports nothing from `ahc`, and AHC imports `is_read_only` from here, so the rule exists once.
Nothing in it needs Python newer than 3.9.

It applies the read-only rule by itself: a command that only reads is always forwarded, and a command that moves is
forwarded only while the bridge is armed. The bridge starts disarmed; AHC arms it once, at a confirmed load, and
disarms it again when AHC's own stop is pressed. The bridge never originates a firmware command: not on start, not
on a disarm, not on exit.

Protocol (`serve`): JSON lines on stdin and stdout, UTF-8, flushed per line; firmware text travels as a string (bytes
decoded latin-1); logs go to stderr and nothing else is ever written to stdout. Requests carry an integer `n` that the
answer echoes:
  write   {"op", "n", "data", "timeout"}  ->  written | refused (not_armed) | failed
  arm / disarm / status                   ->  armed | disarmed | status | refused
  close   {"op"}                          ->  the bridge shuts down
and the bridge sends `hello` once at start, `read` for every transfer from the instrument, and `fatal` before it
exits with 2.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
import threading

PROTOCOL = 1
ID_VENDOR = 0x08AF
ID_PRODUCT = 0x8000
READ_TIMEOUT_S = 1.0  # one wait for a transfer from the instrument
MAX_LINE = 1 << 20

LOCK_FILE = "lock"

# Parameterless queries whose command code does not start with R or Q. The person approved exactly these.
_QUERIES = frozenset(["C0UA", "C0CQ"] + ["P%d%s" % (n, c) for n in range(1, 9) for c in ("VW", "VY")])


# -- the read-only rule -------------------------------------------------------------------------

def is_read_only(command) -> bool:
  """True for a firmware command that only reads. A command is `MMCC...`: a two-character module, then a two-letter
  code, so a request has R or Q first in its code (`R0ZA` is module R0, code ZA: not a request). Anything else must be
  one of the few parameterless queries. Fails closed: short, empty and non-text input is not read-only, and neither
  is text with a control character or anything outside plain ASCII (an assembled command has none)."""
  if isinstance(command, (bytes, bytearray)):
    command = bytes(command).decode("latin-1")
  if not isinstance(command, str) or len(command) < 4:
    return False
  if any(ch < " " or ch > "~" for ch in command):  # a line end could hide a second command behind a request
    return False
  return command[2] in ("R", "Q") or command[:4] in _QUERIES


# -- the state on this host ---------------------------------------------------------------------

def home_dir() -> str:
  """Where the lock file lives: AHC_BRIDGE_HOME, else `.star-bridge` next to this script, so that everything the
  bridge leaves on a shared host stays in the one folder it was copied to."""
  return os.environ.get("AHC_BRIDGE_HOME") or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".star-bridge")


def log(text: str) -> None:
  print("star_bridge: " + text, file=sys.stderr, flush=True)


def take_lock(home: str):
  """An exclusive, non-blocking lock on `lock` with this pid written in it; the open descriptor, or None when another
  process holds it. The lock goes with the process, however it ends."""
  os.makedirs(home, mode=0o700, exist_ok=True)
  fd = os.open(os.path.join(home, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except OSError:
    os.close(fd)
    return None
  os.ftruncate(fd, 0)
  os.write(fd, str(os.getpid()).encode("ascii"))
  return fd


def serving_pid(home: str):
  """The pid in the lock file when that process holds the lock, else None."""
  try:
    fd = os.open(os.path.join(home, LOCK_FILE), os.O_RDONLY)
  except OSError:
    return None
  try:
    try:
      fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:  # held: someone is serving
      try:
        return int(os.read(fd, 32).decode("ascii").strip())
      except ValueError:
        return None
    fcntl.flock(fd, fcntl.LOCK_UN)
    return None
  finally:
    os.close(fd)


# -- the forwarder ------------------------------------------------------------------------------

class Fatal(Exception):
  def __init__(self, code: str, detail: str):
    super().__init__("%s: %s" % (code, detail))
    self.code = code
    self.detail = detail


def _valid_n(value) -> bool:
  return isinstance(value, int) and not isinstance(value, bool)


class Bridge:
  """The state of one `serve`: armed or not, and the lines going out. Everything here runs on one event loop."""

  def __init__(self, io, stdout):
    self.io = io
    self.stdout = stdout
    self.armed = False  # the bridge always starts disarmed
    self.code = 0
    self.lines = asyncio.Queue()  # request lines; None ends the service

  def emit(self, message: dict) -> None:
    line = json.dumps(message, separators=(",", ":")) + "\n"
    try:
      self.stdout.write(line.encode("utf-8"))
      self.stdout.flush()
    except (OSError, ValueError):  # nobody reads our answers any more
      self.finish(0)

  def finish(self, code: int) -> None:
    if self.code == 0:
      self.code = code
    self.lines.put_nowait(None)

  def refuse(self, n, code: str, **more) -> None:
    message = {"op": "refused", "n": n, "code": code}
    message.update(more)
    self.emit(message)

  async def pump(self) -> None:
    """Forward every transfer from the instrument."""
    while True:
      try:
        data = await self.io.read(timeout=READ_TIMEOUT_S)
      except (TimeoutError, asyncio.TimeoutError):
        continue
      except asyncio.CancelledError:
        raise
      except Exception as error:  # noqa: BLE001
        log("reading from the instrument failed: %s: %s" % (type(error).__name__, error))
        self.emit({"op": "fatal", "code": "usb_read_failed", "detail": "%s: %s" % (type(error).__name__, error)})
        self.finish(2)
        return
      if data:
        self.emit({"op": "read", "data": bytes(data).decode("latin-1")})

  def parse(self, raw: bytes):
    """The request in one line, or None after answering bad_request."""
    try:
      request = json.loads(raw.decode("utf-8"))
    except ValueError:
      request = None
    if isinstance(request, dict) and isinstance(request.get("op"), str):
      return request
    n = request.get("n") if isinstance(request, dict) else None
    self.refuse(n if _valid_n(n) else None, "bad_request")
    return None

  async def handle(self, request: dict) -> None:
    op, n = request["op"], request.get("n")
    if op == "close":
      self.finish(0)
      return
    if op not in ("write", "arm", "disarm", "status") or not _valid_n(n):
      self.refuse(n if _valid_n(n) else None, "bad_request")
      return
    if op == "write":
      await self.write(n, request)
    elif op == "arm":
      self.armed = True
      self.emit({"op": "armed", "n": n})
    elif op == "disarm":
      self.armed = False
      self.emit({"op": "disarmed", "n": n})
    else:
      self.emit({"op": "status", "n": n, "armed": self.armed})

  async def write(self, n: int, request: dict) -> None:
    data, timeout = request.get("data"), request.get("timeout")
    bad_timeout = timeout is not None and (
      isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < 3600
    )
    try:
      payload = data.encode("latin-1") if isinstance(data, str) else None
    except UnicodeEncodeError:
      payload = None
    if payload is None or bad_timeout:
      self.refuse(n, "bad_request")
      return
    if not is_read_only(data) and not self.armed:
      self.refuse(n, "not_armed", command=data[:4])
      return
    try:
      await self.io.write(payload, timeout=timeout)
    except asyncio.CancelledError:
      raise
    except Exception as error:  # noqa: BLE001
      self.emit({"op": "failed", "n": n, "error": "%s: %s" % (type(error).__name__, error)})
      return
    self.emit({"op": "written", "n": n})


def _lines(stdin):
  """Request lines from a binary stream. Read straight from the descriptor when there is one: a thread blocked inside
  a buffered reader holds its lock, and Python aborts at exit when it cannot take that lock (after SIGTERM)."""
  try:
    fd = stdin.fileno()
  except (AttributeError, OSError, ValueError):
    for raw in iter(lambda: stdin.readline(MAX_LINE), b""):
      yield raw
    return
  buffer = b""
  while True:
    chunk = os.read(fd, 65536)
    if not chunk:
      break
    buffer += chunk
    while True:
      end = buffer.find(b"\n")
      if end < 0:
        break
      yield buffer[:end + 1]
      buffer = buffer[end + 1:]
    if len(buffer) > MAX_LINE:  # no end of line in sight: hand it over as it is, it will be refused
      yield buffer
      buffer = b""
  if buffer:
    yield buffer


def _read_stdin(stdin, loop, bridge: Bridge) -> None:
  """Feed request lines to the loop. A daemon thread: a blocked read must not keep a stopping bridge alive."""
  try:
    for raw in _lines(stdin):
      loop.call_soon_threadsafe(bridge.lines.put_nowait, raw)
  except (OSError, ValueError, RuntimeError):  # the stream or the loop is gone
    pass
  try:
    loop.call_soon_threadsafe(bridge.finish, 0)  # end of input
  except RuntimeError:  # the loop is gone already
    pass


async def serve(io, stdin, stdout, home: str, probe=None) -> int:
  """Run the bridge on `io` (any PyLabRobot IOBase) with binary streams; returns the exit code.

  `probe`, when given, checks which instrument is there before the link is opened and returns {"bus", "address"}
  or raises Fatal. Tests pass none, with a fake `io`."""
  loop = asyncio.get_running_loop()
  bridge = Bridge(io, stdout)
  try:
    lock = take_lock(home)
  except OSError as error:
    bridge.emit({"op": "fatal", "code": "home_unusable", "detail": "%s: %s" % (type(error).__name__, error)})
    return 2
  if lock is None:
    bridge.emit({"op": "fatal", "code": "in_use", "detail": "another bridge is serving from %s" % home})
    return 2
  tasks = set()
  pump = None
  opened = False  # the link to the instrument is open
  handlers = []
  try:
    try:
      device = probe() if probe is not None else {"bus": 0, "address": 0}
    except Fatal as fatal:
      bridge.emit({"op": "fatal", "code": fatal.code, "detail": fatal.detail})
      return 2
    try:
      await io.setup()
    except Exception as error:  # noqa: BLE001
      bridge.emit({"op": "fatal", "code": "usb_failed", "detail": "%s: %s" % (type(error).__name__, error)})
      try:
        await io.stop()
      except Exception:  # noqa: BLE001
        pass
      return 2
    opened = True
    bridge.emit({"op": "hello", "protocol": PROTOCOL, "armed": False, "device": device})

    for sig in (signal.SIGTERM, signal.SIGHUP):
      try:
        loop.add_signal_handler(sig, bridge.finish, 0)
        handlers.append(sig)
      except (NotImplementedError, RuntimeError, ValueError):  # not the main thread
        pass
    threading.Thread(target=_read_stdin, args=(stdin, loop, bridge), daemon=True).start()
    pump = loop.create_task(bridge.pump())

    while True:
      raw = await bridge.lines.get()
      if raw is None:
        break
      task = loop.create_task(_guarded(bridge, raw))  # a task even for a bad line: answers keep the order asked
      tasks.add(task)
      task.add_done_callback(tasks.discard)
  finally:
    for sig in handlers:
      loop.remove_signal_handler(sig)
    # Pending requests are dropped, not finished: nothing may be written after the end of the service.
    stopping = [t for t in [pump] + list(tasks) if t is not None]
    for task in stopping:
      task.cancel()
    await asyncio.gather(*stopping, return_exceptions=True)
    if opened:
      try:
        await io.stop()
      except Exception as error:  # noqa: BLE001
        log("closing the instrument failed: %s: %s" % (type(error).__name__, error))
    os.close(lock)
  return bridge.code


async def _guarded(bridge: Bridge, raw: bytes) -> None:
  """A request that raises must never take the bridge down."""
  request = None
  try:
    request = bridge.parse(raw)
    if request is not None:
      await bridge.handle(request)
  except asyncio.CancelledError:
    raise
  except Exception as error:  # noqa: BLE001
    log("a request failed: %s: %s" % (type(error).__name__, error))
    n = request.get("n") if request is not None else None
    bridge.refuse(n if _valid_n(n) else None, "bad_request")


# -- the command line ---------------------------------------------------------------------------

def _usb_io():
  """PyLabRobot's own USB handle, built with the arguments `STARDriver.__init__` uses, and the probe that insists on
  exactly one instrument. PyLabRobot is imported here and nowhere else."""
  try:
    from pylabrobot.io import usb as plr_usb
  except Exception as error:  # noqa: BLE001
    raise Fatal("usb_failed", "PyLabRobot cannot be imported: %s: %s" % (type(error).__name__, error))
  if not plr_usb.USE_USB:
    raise Fatal("usb_failed", "pyusb/libusb is not installed (pip install 'pylabrobot[usb]')")
  io = plr_usb.USB(
    human_readable_device_name="Hamilton STAR",
    id_vendor=ID_VENDOR,
    id_product=ID_PRODUCT,
    device_address=None,
    write_timeout=30,
    serial_number=None,
  )

  def probe() -> dict:
    try:
      found = list(io.get_available_devices())
    except Exception as error:  # noqa: BLE001
      raise Fatal("usb_failed", "%s: %s" % (type(error).__name__, error))
    if not found:
      raise Fatal("no_device", "no USB device %04x:%04x is present" % (ID_VENDOR, ID_PRODUCT))
    if len(found) > 1:
      raise Fatal("several_devices", "%d USB devices %04x:%04x are present" % (len(found), ID_VENDOR, ID_PRODUCT))
    device = found[0]
    return {"bus": getattr(device, "bus", None), "address": getattr(device, "address", None)}

  return io, probe


def _print(stdout, message: dict) -> None:
  stdout.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
  stdout.flush()


def main(argv=None, stdin=None, stdout=None) -> int:
  parser = argparse.ArgumentParser(prog="star_bridge.py", description="Bridge to a Hamilton STAR on this host.")
  commands = parser.add_subparsers(dest="command", required=True)
  commands.add_parser("serve", help="forward firmware commands (JSON lines on stdin and stdout)")
  commands.add_parser("status", help="show whether a bridge is serving")
  args = parser.parse_args(argv)

  home = home_dir()
  if stdout is None:
    stdout = sys.stdout.buffer
    sys.stdout = sys.stderr  # whatever a library prints must not corrupt the protocol
  if args.command == "status":
    _print(stdout, {"serving": serving_pid(home)})
  else:
    if stdin is None:
      stdin = sys.stdin.buffer
    try:
      io, probe = _usb_io()
    except Fatal as fatal:
      _print(stdout, {"op": "fatal", "code": fatal.code, "detail": fatal.detail})
      return 2
    return asyncio.run(serve(io, stdin, stdout, home, probe))
  return 0


if __name__ == "__main__":
  sys.exit(main())
