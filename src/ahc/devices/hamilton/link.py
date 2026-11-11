"""The Mac side of the STARlet's real backend: the link to the bridge, PyLabRobot's transport over it, and AHC's guard.

The instrument is plugged into a USB host (a Raspberry Pi), where `star_bridge.py` forwards firmware commands. AHC
reaches it by running the bridge over ssh and speaking JSON lines on the pipe (protocol in `star_bridge.py`):
- `BridgeLink` is the process and the protocol: blocking, thread-safe, no asyncio inside.
- `BridgeIO` is PyLabRobot's `IOBase` over a link, for a real `STARDriver(io=...)`.
- `GuardState`, `guard_driver` and `GuardedIO` are AHC's guard: only read-only firmware commands pass unless motion is
  open, and the guard closes again on a failed command. The bridge applies the same read-only rule on the host and
  forwards a command that moves only while it is armed.

Threads and loops. PyLabRobot's `ReplyRouter` calls `io.read()` from its own thread and its own event loop and
catches only `TimeoutError` there: any other exception ends that thread and leaves commands waiting forever. So
`BridgeIO.read` raises nothing but `TimeoutError`, also after the link is lost, and `fail_waiting` ends the commands
that wait. `io.write` runs on the caller's loop.
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import itertools
import json
import logging
import queue
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from pylabrobot.hamilton.protocol.text.framing import parse_fw_string, read_id
from pylabrobot.hamilton.star.driver.errors import STARFirmwareError, check_fw_string_error
from pylabrobot.io.io import IOBase

from ahc.core.errors import LabError
from ahc.devices.hamilton.star_bridge import PROTOCOL, is_read_only

logger = logging.getLogger(__name__)

READ_WAIT_S = 1.0  # one wait for a transfer: what PyLabRobot's router expects of `read`
WRITE_ANSWER_S = 10.0  # how long past the instrument's own write timeout the bridge may take to answer a write
LOG_SIZE = 64  # commands that are not read-only, kept for `GuardState.record`
STDERR_KEEP = 4096

# Where the bridge lives on the USB host unless the device entry says otherwise (starlet.md describes the set-up).
DEFAULT_PYTHON = "~/ahc/.venv/bin/python"
DEFAULT_SCRIPT = "~/ahc/star_bridge.py"

_DESTINATION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(@[A-Za-z0-9_][A-Za-z0-9_.-]*)?")
_PATHLIKE = re.compile(r"[A-Za-z0-9_./~+-]+")


def ssh_argv(destination: str, python: str, script: str) -> list[str]:
  """The ssh command that runs the bridge (`serve`) on the USB host. Nothing is quoted (`~` has to expand on the host),
  so the checks below are the protection: no option can be smuggled in as a host, and no shell syntax in the command."""
  if not _DESTINATION.fullmatch(destination or ""):
    raise LabError("bad_option", f"The bridge host {destination!r} is not a host name or user@host.",
                   "Use letters, digits, '.', '_' and '-', not starting with '-'.")
  for name, value in (("python", python), ("script", script)):
    if not _PATHLIKE.fullmatch(value or "") or value.startswith("-"):
      raise LabError("bad_option", f"The bridge {name} {value!r} is not a plain path.",
                     "Use letters, digits and ./_~+- only, not starting with '-'.")
  return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5",
          "-o", "ServerAliveCountMax=3", "--", destination, f"{python} -u {script} serve"]


class BridgeError(Exception):
  """The bridge could not do what was asked (a write failed on the instrument, or an answer made no sense)."""


class BridgeRefused(Exception):
  """The bridge on the USB host refused a request (a command that moves, while it is not armed)."""

  def __init__(self, code: str, command: str = ""):
    self.code = code
    self.command = command
    super().__init__(f"the bridge refused {command or 'the request'}: {code}")


class BridgeLost(Exception):
  """The link to the bridge is gone, or it did not answer in time."""

  def __init__(self, reason: str):
    self.reason = reason
    super().__init__(f"the link to the bridge is lost: {reason}")


class GuardRefused(Exception):
  """A command that is not read-only, refused because motion is not open."""

  def __init__(self, command: str, why: str):
    self.command = command
    self.why = why
    super().__init__(f"{command} was not sent: the instrument is read-only ({why})")


# -- the process --------------------------------------------------------------------------------

def spawn(argv: list[str]) -> subprocess.Popen:
  """Start the process that runs the bridge, with pipes on stdin, stdout and stderr. Tests replace this."""
  return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class _Pending:
  def __init__(self) -> None:
    self.event = threading.Event()
    self.answer: dict | None = None


class BridgeLink:
  """The bridge process and its protocol. `lost` is why the link is gone (None while it is up); `on_lost` is called
  once with that reason, from the reader thread when the process ends, stdout closes or a `fatal` arrives (from the
  requesting thread when an answer does not come). A link closed on purpose does not call it."""

  def __init__(self, destination: str, python: str, script: str):
    self.destination = destination
    self.argv = ssh_argv(destination, python, script)
    self.reads: queue.Queue[bytes] = queue.Queue()  # transfers the instrument sent, in order
    self.hello: dict | None = None
    self.lost: str | None = None
    self.on_lost: Callable[[str], None] | None = None
    self._lock = threading.Lock()
    self._write_lock = threading.Lock()
    self._counter = itertools.count(1)
    self._pending: dict[int, _Pending] = {}
    self._proc: Any = None
    self._closed = False
    self._fatal: dict | None = None
    self._garbled = False
    self._hello_event = threading.Event()
    self._stderr = bytearray()
    self._threads: list[threading.Thread] = []

  # -- start ------------------------------------------------------------------------------------

  def start(self, timeout: float = 20.0) -> dict:
    """Spawn the bridge and wait for its hello. Failures are `LabError`s a person can act on."""
    if self._proc is not None:
      if self.hello is not None and self.lost is None:
        return self.hello
      raise LabError("backend_unreachable", "This link was used before and is closed.", "Connect again.")
    try:
      proc = spawn(self.argv)
    except OSError as error:
      raise LabError("backend_unreachable", f"ssh could not be started: {error}.", "Check that ssh is installed.")
    self._proc = proc
    for target in (self._read_loop, self._drain_stderr):
      thread = threading.Thread(target=target, args=(proc,), daemon=True, name=f"bridge-{target.__name__}")
      thread.start()
      self._threads.append(thread)
    self._hello_event.wait(timeout)
    hello = self.hello
    if hello is None:
      timed_out = self.lost is None
      self.close(timeout=1.0)
      raise self._start_error(timed_out, timeout)
    if hello.get("protocol") != PROTOCOL or hello.get("armed") is not False:
      self.close(timeout=1.0)
      raise LabError("bridge_protocol", f"The bridge speaks protocol {hello.get('protocol')!r}, armed={hello.get('armed')!r}; "
                     f"AHC needs protocol {PROTOCOL}, disarmed.",
                     "Copy star_bridge.py from this AHC version to the USB host.")
    return hello

  def _start_error(self, timed_out: bool, timeout: float) -> LabError:
    self._join_stderr()
    tail = self.stderr_tail()
    suffix = f" Its error output: {tail}" if tail else ""
    fatal = self._fatal
    if fatal is not None:
      code, detail = str(fatal.get("code")), str(fatal.get("detail"))
      if code == "in_use":
        return LabError("bridge_in_use", f"Another AHC session is using the bridge ({detail}).",
                        "Close that session, or end the old bridge on the USB host, then connect again.")
      if code == "no_device":
        return LabError("instrument_not_found", f"The bridge found no STAR on the USB host ({detail}).",
                        "Check that the instrument is switched on and its USB cable is plugged into the USB host.")
      if code == "several_devices":
        return LabError("several_instruments", f"The bridge found more than one STAR on the USB host ({detail}).",
                        "Unplug all but one instrument from the USB host.")
      return LabError("backend_unreachable", f"The bridge could not open the instrument: {code}: {detail}.",
                      "Check the USB cable, the host's USB permissions and that PyLabRobot's usb extra is installed there.")
    if self._garbled:
      return LabError("bridge_protocol", f"The bridge did not speak the protocol.{suffix}",
                      "Check the path to star_bridge.py on the USB host, and copy this version of it there.")
    if timed_out:
      return LabError("backend_unreachable", f"The bridge did not answer within {timeout:g} s.{suffix}",
                      "Check that the USB host is reachable over ssh without a password prompt, then connect again.")
    return LabError("backend_unreachable", f"The connection to the bridge ended: {self.lost}.{suffix}",
                    "Check that the USB host is reachable over ssh without a password prompt (key login), then connect again.")

  # -- the threads ------------------------------------------------------------------------------

  def _read_loop(self, proc: Any) -> None:
    try:
      for raw in proc.stdout:
        self._dispatch(raw)
    except (OSError, ValueError):
      pass
    try:
      code = proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
      code = None
    self._set_lost("the bridge process ended" + (f" (exit code {code})" if code is not None else ""))

  def _drain_stderr(self, proc: Any) -> None:
    stream = proc.stderr
    take = getattr(stream, "read1", stream.read)
    try:
      while True:
        chunk = take(4096)
        if not chunk:
          return
        with self._lock:
          self._stderr += chunk
          del self._stderr[:-STDERR_KEEP]
    except (OSError, ValueError):
      return

  def _join_stderr(self) -> None:
    for thread in self._threads:
      if thread.name.endswith("_drain_stderr") and thread is not threading.current_thread():
        thread.join(timeout=1.0)

  def stderr_tail(self) -> str:
    with self._lock:
      return bytes(self._stderr).decode("utf-8", "replace").strip()[-400:]

  def _dispatch(self, raw: bytes) -> None:
    try:
      message = json.loads(raw.decode("utf-8"))
      op = message["op"]
      if not isinstance(op, str):
        raise ValueError("op")
      if op == "read":
        self.reads.put(message["data"].encode("latin-1"))
        return
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
      self._garbled = True
      self._set_lost("a line from the bridge could not be understood")
      return
    if op == "hello":
      if self.hello is None:
        self.hello = message
        self._hello_event.set()
    elif op == "fatal":
      self._fatal = message
      self._set_lost(f"{message.get('code')}: {message.get('detail')}")
    else:
      with self._lock:
        slot = self._pending.pop(message.get("n"), None)
      if slot is not None:
        slot.answer = message
        slot.event.set()

  def _set_lost(self, reason: str) -> None:
    """The link is gone: wake everything waiting, and tell `on_lost` once."""
    with self._lock:
      if self.lost is not None:
        return
      self.lost = reason
      slots = list(self._pending.values())
      self._pending.clear()
      callback = None if self._closed else self.on_lost
    for slot in slots:
      slot.event.set()
    self._hello_event.set()
    if callback is not None:
      try:
        callback(reason)
      except Exception:  # noqa: BLE001
        logger.exception("on_lost raised")

  # -- requests ---------------------------------------------------------------------------------

  def request(self, op: str, timeout: float = 10.0, fields: dict | None = None, **more: Any) -> dict:
    """One request and its answer. `fields` carries fields that would clash with these parameters (the write's own
    `timeout`). Raises `BridgeLost` when the link is gone or does not answer in time (then it is closed)."""
    message: dict[str, Any] = {"op": op, **(fields or {}), **more}
    slot = _Pending()
    with self._lock:
      if self.lost is not None:
        raise BridgeLost(self.lost)
      if self._proc is None:
        raise BridgeLost("the link is not started")
      n = next(self._counter)
      self._pending[n] = slot
      proc = self._proc
    message["n"] = n
    try:
      line = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
      with self._write_lock:
        proc.stdin.write(line)
        proc.stdin.flush()
    except (OSError, ValueError) as error:
      self._set_lost(f"the bridge cannot be written to ({type(error).__name__})")
      raise BridgeLost(self.lost or "closed")
    if not slot.event.wait(timeout) and slot.answer is None:
      with self._lock:
        self._pending.pop(n, None)
      self._set_lost(f"no answer to {op} within {timeout:g} s")
      self.close(timeout=2.0)
      raise BridgeLost(self.lost or "closed")
    if slot.answer is None:
      raise BridgeLost(self.lost or "closed")
    return slot.answer

  def close(self, timeout: float = 5.0) -> None:
    """Ask the bridge to close, wait, then terminate, then kill. Idempotent; never raises."""
    try:
      with self._lock:
        if self._closed:
          return
        self._closed = True
        proc = self._proc
      self._set_lost("closed")
      if proc is None:
        return
      with self._write_lock:
        try:
          proc.stdin.write(b'{"op":"close"}\n')
          proc.stdin.flush()
        except (OSError, ValueError):
          pass
        try:
          proc.stdin.close()
        except (OSError, ValueError):
          pass
      try:
        proc.wait(timeout=timeout)
      except subprocess.TimeoutExpired:
        proc.terminate()
        try:
          proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
          proc.kill()
          proc.wait(timeout=2)
      for thread in self._threads:
        if thread is not threading.current_thread():
          thread.join(timeout=1.0)
      for stream in (proc.stdout, proc.stderr):
        try:
          stream.close()
        except (OSError, ValueError):
          pass
    except Exception:  # noqa: BLE001
      logger.exception("closing the bridge link failed")


# -- PyLabRobot's transport over the link -------------------------------------------------------

def _answer(answer: dict, ok: str, command: str = "") -> dict:
  op = answer.get("op")
  if op == ok:
    return answer
  if op == "refused":
    raise BridgeRefused(str(answer.get("code")), str(answer.get("command") or command))
  if op == "failed":
    raise BridgeError(str(answer.get("error")))
  raise BridgeError(f"unexpected answer {op!r}")


class BridgeIO(IOBase):
  """PyLabRobot's transport over a `BridgeLink`. Works from any thread and any event loop: the blocking part runs in
  private single-thread executors, one for reads and one for writes (as PyLabRobot's `USB` does) and one for the
  control requests, so a disarm is never queued behind a write."""

  def __init__(self, destination: str, python: str, script: str, start_timeout: float = 20.0):
    super().__init__()
    self.destination = destination
    self.python = python
    self.script = script
    self.start_timeout = start_timeout
    self.hello: dict | None = None
    self.on_lost: Callable[[str], None] | None = None
    self.link: BridgeLink | None = None
    self._pools: dict[str, ThreadPoolExecutor] = {}

  @property
  def lost(self) -> str | None:
    return self.link.lost if self.link is not None else None

  def _make_pools(self) -> None:
    self._pools = {name: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"bridge-{name}")
                   for name in ("read", "write", "control")}

  def _shutdown_pools(self) -> None:
    for pool in self._pools.values():
      pool.shutdown(wait=False, cancel_futures=True)
    self._pools = {}

  async def _run(self, pool: str, fn: Callable[[], Any]) -> Any:
    try:
      future = asyncio.get_running_loop().run_in_executor(self._pools[pool], fn)
    except (KeyError, RuntimeError):  # no pool, or one shut down
      raise BridgeLost("the link is closed")
    return await future

  def _lost(self, reason: str) -> None:
    callback = self.on_lost
    if callback is not None:
      callback(reason)

  async def setup(self, *args: Any, **kwargs: Any) -> None:
    self._shutdown_pools()
    self._make_pools()
    self.link = BridgeLink(self.destination, self.python, self.script)
    self.link.on_lost = self._lost
    link = self.link
    try:
      self.hello = await self._run("control", lambda: link.start(self.start_timeout))
    except BaseException:
      await self.stop()
      raise

  async def stop(self) -> None:
    link = self.link
    if link is not None:
      try:
        await self._run("control", link.close)
      except BridgeLost:
        link.close()
    self._shutdown_pools()

  async def write(self, data: bytes, timeout: float | None = None, *args: Any, **kwargs: Any) -> None:
    link = self.link
    if link is None:
      raise BridgeLost("not connected")
    text = bytes(data).decode("latin-1")
    wait = (30.0 if timeout is None else float(timeout)) + WRITE_ANSWER_S
    answer = await self._run("write", lambda: link.request("write", timeout=wait, fields={"data": text, "timeout": timeout}))
    _answer(answer, "written", text[:4])

  def _take(self) -> bytes:
    link = self.link
    if link is None:
      threading.Event().wait(READ_WAIT_S)
      raise TimeoutError("not connected")
    try:
      return link.reads.get(timeout=READ_WAIT_S)
    except queue.Empty:
      raise TimeoutError("nothing arrived")

  async def read(self, *args: Any, **kwargs: Any) -> bytes:
    """The next transfer from the instrument, or TimeoutError after about a second. Never raises anything else
    (the router's reading thread dies on any other exception), also after the link is lost."""
    try:
      return await self._run("read", self._take)
    except TimeoutError:
      raise
    except asyncio.CancelledError:
      raise
    except Exception:  # noqa: BLE001 - a closed link: wait as a read would, then say nothing arrived
      await asyncio.sleep(READ_WAIT_S)
      raise TimeoutError("the link is closed")

  def serialize(self) -> dict:
    return {"type": "BridgeIO", "bridge": self.destination}

  # -- for the device adapter ---------------------------------------------------------------------

  async def _control(self, op: str, ok: str, fields: dict | None = None) -> dict:
    link = self.link
    if link is None:
      raise BridgeLost("not connected")
    answer = await self._run("control", lambda: link.request(op, timeout=10.0, fields=fields))
    return _answer(answer, ok)

  async def arm(self) -> dict:
    return await self._control("arm", "armed")

  async def disarm(self) -> dict:
    return await self._control("disarm", "disarmed")

  async def status(self) -> dict:
    return await self._control("status", "status")


# -- the guard -------------------------------------------------------------------------

def _now() -> str:
  return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _reply_key(reply: str) -> Any:
  """What matches a reply to its command, as the router does: the `id####` field, else the module and code."""
  try:
    parsed = parse_fw_string(reply, "id####")
    if parsed.get("id") is not None:
      return int(parsed["id"])
  except Exception:  # noqa: BLE001
    pass
  return reply[:4]


class GuardState:
  """The guard's state, shared by `guard_driver` and `GuardedIO`. Thread-safe: the router's thread reads replies while
  the event loop sends commands."""

  def __init__(self) -> None:
    self._lock = threading.Lock()
    self.mode = "read_only"  # "read_only" | "open"
    self.why: str | None = "not initialized"
    self.sent = 0  # commands that passed the guard, counted before they are sent
    self.moved = 0  # of those, the ones that are not read-only
    self.log: collections.deque[dict] = collections.deque(maxlen=LOG_SIZE)  # commands that are not read-only
    self._latched = False
    self._flight: dict[Any, dict] = {}

  @property
  def latched(self) -> bool:
    """Whether `latch()` ran since the last `open()` (or since creation)."""
    with self._lock:
      return self._latched

  def open(self) -> None:
    with self._lock:
      self.mode, self.why, self._latched = "open", None, False

  def latch(self, why: str) -> None:
    """Back to read-only. The first latch's reason stands until `open()`."""
    with self._lock:
      self.mode = "read_only"
      if not self._latched:
        self.why, self._latched = why, True

  def admit(self, command: str, count: bool = True) -> None:
    """Let a command pass, or raise `GuardRefused` (and log it) when it is not read-only and motion is not open."""
    with self._lock:
      if self.mode != "open" and not is_read_only(command):
        code = command[:4] if isinstance(command, str) else str(command)[:4]
        self.log.append({"command": code, "id": None, "at": _now(), "status": "refused"})
        raise GuardRefused(code, self.why or "read-only")
      if count:
        self.sent += 1
        if not is_read_only(command):
          self.moved += 1

  def failed(self, command: str, error: BaseException) -> None:
    """A command that passed the guard did not complete (`error` is what it raised). Closes the guard at once, so a
    cleanup further up (PyLabRobot's `setup()` calls `stop()` after a failure, which moves) finds it closed. A
    firmware error reply to a read-only command is not a failure of the link: discovery tolerates some."""
    code = command[:4]
    if not is_read_only(command):
      self.latch(f"{code} did not complete")
    elif not isinstance(error, STARFirmwareError):
      self.latch(f"{code} got no answer")

  def register(self, command: str) -> dict | None:
    """A command about to be written: in flight and logged, if it is not read-only."""
    if is_read_only(command):
      return None
    try:
      ident = read_id(command)
    except ValueError:
      ident = None
    entry = {"command": command[:4], "id": ident, "at": _now(), "status": "sent"}
    with self._lock:
      self.log.append(entry)
      self._flight[ident if ident is not None else command[:4]] = entry
    return entry

  def write_failed(self, entry: dict | None, refused: str | None = None) -> None:
    if entry is None:
      return
    with self._lock:
      self._flight.pop(entry["id"] if entry["id"] is not None else entry["command"], None)
      entry["status"] = "refused" if refused else "error write failed"
    self.latch(f"the bridge refused a command: {refused}" if refused else "a command could not be written")

  def reply(self, text: str) -> None:
    """A reply was read: match it to its command and log it; a firmware error latches."""
    key = _reply_key(text)
    with self._lock:
      entry = self._flight.pop(key, None)
    if entry is None:
      return
    try:
      check_fw_string_error(text)
    except Exception:  # noqa: BLE001 - an error, or a reply that cannot be read: either way, not a success
      found = re.search(r"er\d\d(?:/\d\d)?", text)
      error = found.group(0) if found else "er??"
      with self._lock:
        entry["status"] = f"error {error}"
      self.latch(f"a command failed: {entry['command']} {error}")
      return
    with self._lock:
      entry["status"] = "answered"

  def in_flight(self) -> list[str]:
    """Codes of commands that are not read-only, were written, and have not been answered."""
    with self._lock:
      return [entry["command"] for entry in self._flight.values()]

  def record(self, n: int) -> list[dict]:
    """The last n logged commands as {"command", "status"} (the shape of `Adapter.robot_record`)."""
    with self._lock:
      entries = list(self.log)[-n:] if n > 0 else []
      return [{"command": e["command"], "status": e["status"]} for e in entries]


def guard_driver(driver: Any, state: GuardState) -> None:
  """Wrap the driver's `_send` and `send_raw_command` (on this instance) with the guard. It runs before anything is
  assembled or sent, so it also covers `STARSimulationDriver`, whose `_send` never reaches an IO. A call that does not
  complete latches the guard before the exception travels up, so PyLabRobot's own cleanup (which moves) is refused."""
  send, send_raw = driver._send, driver.send_raw_command

  async def _send(module: str, command: str, *args: Any, **kwargs: Any) -> Any:
    text = f"{module}{command}"
    state.admit(text)
    try:
      return await send(module, command, *args, **kwargs)
    except GuardRefused:
      raise
    except BaseException as error:  # includes cancellation: the call may have been sent, its outcome is unknown
      state.failed(text, error)
      raise

  async def send_raw_command(command: str, *args: Any, **kwargs: Any) -> Any:
    state.admit(command)
    try:
      return await send_raw(command, *args, **kwargs)
    except GuardRefused:
      raise
    except BaseException as error:
      state.failed(command, error)
      raise

  driver._send = _send
  driver.send_raw_command = send_raw_command


class GuardedIO(IOBase):
  """Another IO behind the guard: the last check before the wire (a backstop behind `guard_driver`)."""

  def __init__(self, io: IOBase, state: GuardState):
    super().__init__()
    self.io = io
    self.state = state

  async def setup(self, *args: Any, **kwargs: Any) -> Any:
    return await self.io.setup(*args, **kwargs)

  async def stop(self) -> Any:
    return await self.io.stop()

  async def write(self, data: bytes, *args: Any, **kwargs: Any) -> Any:
    text = bytes(data).decode("latin-1")
    self.state.admit(text, count=False)
    entry = self.state.register(text)  # before the write: the answer can come before it returns
    try:
      return await self.io.write(data, *args, **kwargs)
    except BaseException as error:
      self.state.write_failed(entry, error.code if isinstance(error, BridgeRefused) else None)
      raise

  async def read(self, *args: Any, **kwargs: Any) -> bytes:
    data = await self.io.read(*args, **kwargs)
    self.state.reply(bytes(data).decode("latin-1"))
    return data

  def serialize(self) -> dict:
    return self.io.serialize()


def fail_waiting(driver: Any, reason: str) -> None:
  """Fail every command of a real `STARDriver` that still waits for a reply, so a lost link ends the waiting call now
  instead of at its read timeout. Uses PyLabRobot privates: `driver._replies` (the `ReplyRouter`) with its
  `_waiting_tasks` and `_waiting_tasks_lock`, and each task's `loop` and `fut`. Safe from any thread."""
  router = driver._replies
  with router._waiting_tasks_lock:
    waiting, router._waiting_tasks = router._waiting_tasks, []

  def fail(future: asyncio.Future, error: Exception) -> None:
    if not future.done():
      future.set_exception(error)

  for task in waiting:
    try:
      task.loop.call_soon_threadsafe(fail, task.fut, BridgeLost(reason))
    except RuntimeError:  # that loop is closed: nobody waits there
      pass


async def listen_for_replies(driver: Any, io: IOBase, state: GuardState, timeout: float,
                             lost: Callable[[], str | None] | None = None) -> list[str]:
  """After a stop, hear what was in flight without sending anything: read from `io` (the `GuardedIO`, which logs each
  reply and clears it from `state.in_flight()`) until nothing is in flight, `timeout` seconds have passed (one read
  may overshoot it by its own second) or `lost()` names a reason. PyLabRobot's router keeps a cancelled command in its
  waiting list and reads only while one waits, so what waits is failed first (`fail_waiting`) and its reader goes
  idle. Returns the codes still in flight, empty when all were answered. It never writes."""
  deadline = time.monotonic() + timeout
  fail_waiting(driver, "emergency stop")
  while state.in_flight():
    if (lost is not None and lost()) or time.monotonic() >= deadline:
      break
    fail_waiting(driver, "emergency stop")
    try:
      await io.read()
    except TimeoutError:
      continue
  return state.in_flight()
