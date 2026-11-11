"""A scripted Hamilton STAR for tests: no USB, no PyLabRobot simulator, nothing leaves this machine.

`FakeStarIO` stands where PyLabRobot's USB handle would be. A command `MMCCid####...` that is written is answered
`MMCCid####er00/00` plus a payload from a table by command code (made-up identity values). `hold(code)` never answers
that code until `release(code)`; `fail(code, error)` answers it with a firmware error in the format PyLabRobot's error
check recognises. Everything written is recorded.

`write_launcher` writes a small script that loads `star_bridge.py` by path and serves a `FakeStarIO` over its own
stdin and stdout, so a test can spawn it where AHC would spawn ssh. There is no way to select a fake instrument from
the bridge's own command line: this is test code that stands outside the bridge.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from pylabrobot.io.io import IOBase

# What each answered command code carries after `er00/00`. Made-up values.
PAYLOADS = {
  "RF": "rfT0001 1.0S 25 2025_01_01",
  "RI": "riT0001",
  "RQ": "rqT0001 FAKE",
  "QM": "qm1",
  "RT": "rt0001",
}
HELD_POLL_S = 0.002


def command_code(text: str) -> str:
  return text[:4]


def command_id(text: str) -> str | None:
  match = re.search(r"id(\d{4})", text[4:])
  return match.group(1) if match else None


class FakeStarIO(IOBase):
  def __init__(self, log_path: str | None = None, release_dir: str | None = None):
    self.written: list[str] = []
    self.is_setup = False
    self.stopped = False
    self.setup_error: Exception | None = None
    self.read_error: Exception | None = None  # raised by the next read, then cleared
    self._answers: list[str] = []
    self._held: dict[str, list[str]] = {}  # code -> replies waiting for release
    self._hold: set[str] = set()
    self._fail: dict[str, str] = {}
    self._log_path = log_path  # every written command, one per line (for a spawned launcher)
    self._release_dir = release_dir  # a file `release-<code>` in it releases that code

  # -- scripting -------------------------------------------------------------------------------

  def hold(self, code: str) -> None:
    self._hold.add(code)

  def release(self, code: str) -> None:
    """Stop holding `code` and answer what was held."""
    self._hold.discard(code)
    self._answers.extend(self._held.pop(code, []))

  def fail(self, code: str, error: str = "er01/00") -> None:
    self._fail[code] = error

  def codes(self) -> list[str]:
    return [command_code(text) for text in self.written]

  def _control_file(self, kind: str, code: str) -> Path | None:
    """A file `<kind>-<code>` in the control folder, if there is one (a spawned launcher is steered by files)."""
    path = Path(self._release_dir, f"{kind}-{code}") if self._release_dir else None
    return path if path is not None and path.exists() else None

  def reply_for(self, text: str) -> str:
    code, ident = command_code(text), command_id(text)
    error = self._fail.get(code)
    failing = self._control_file("fail", code)
    if failing is not None:
      error = failing.read_text().strip()
    body = error if error is not None else "er00/00" + PAYLOADS.get(code[2:], "")
    return code + ("id" + ident if ident else "") + body

  # -- the IO ----------------------------------------------------------------------------------

  async def setup(self, *args, **kwargs):
    if self.setup_error is not None:
      raise self.setup_error
    self.is_setup = True

  async def stop(self):
    self.stopped = True

  async def write(self, data: bytes, *args, **kwargs):
    text = data.decode("latin-1")
    self.written.append(text)
    if self._log_path:
      with open(self._log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(text) + "\n")
    code = command_code(text)
    if self._control_file("hold", code) is not None:
      self._hold.add(code)
    if code in self._hold:
      self._held.setdefault(code, []).append(self.reply_for(text))
    else:
      self._answers.append(self.reply_for(text))

  def _poll_release_files(self) -> None:
    if not self._release_dir:
      return
    for code in list(self._hold):
      if Path(self._release_dir, "release-" + code).exists():
        self.release(code)

  async def read(self, *args, timeout: float | None = None, **kwargs) -> bytes:
    """The next answer, or TimeoutError after `timeout` seconds (1 by default). Polls, so it never blocks a loop."""
    deadline = time.monotonic() + (1.0 if timeout is None else timeout)
    while True:
      if self.read_error is not None:
        error, self.read_error = self.read_error, None
        raise error
      self._poll_release_files()
      if self._answers:
        return self._answers.pop(0).encode("latin-1")
      if time.monotonic() >= deadline:
        raise TimeoutError("nothing arrived")
      await asyncio.sleep(HELD_POLL_S)


# -- a spawnable instrument ---------------------------------------------------------------------

LAUNCHER = '''\
import asyncio, importlib.util, json, os, sys

sys.path.insert(0, {tests_dir!r})
from fake_star import FakeStarIO

spec = importlib.util.spec_from_file_location("star_bridge_under_test", {bridge!r})
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

io = FakeStarIO(log_path={log!r}, release_dir={release_dir!r})
for code in {hold!r}:
  io.hold(code)
for code, error in {fail!r}.items():
  io.fail(code, error)
sys.exit(asyncio.run(bridge.serve(io, sys.stdin.buffer, sys.stdout.buffer, os.environ["AHC_BRIDGE_HOME"])))
'''


def write_launcher(folder: Path, bridge_path: Path, hold: tuple[str, ...] = (), fail: dict[str, str] | None = None) -> dict:
  """Write `launcher.py` in `folder`; returns {"script", "log", "release_dir"}. The launcher serves a FakeStarIO that
  logs what it was written to `log` (one JSON string per line) and releases a held code when `release-<code>` appears
  in `release_dir`."""
  folder.mkdir(parents=True, exist_ok=True)
  script, log = folder / "launcher.py", folder / "written.log"
  script.write_text(LAUNCHER.format(
    tests_dir=str(Path(__file__).parent), bridge=str(bridge_path), log=str(log), release_dir=str(folder),
    hold=list(hold), fail=dict(fail or {}),
  ))
  return {"script": script, "log": log, "release_dir": folder}


def steer(folder: Path, kind: str, code: str, text: str = "") -> None:
  """Steer a spawned launcher's instrument while it runs: `hold`, `release` or `fail` a command code (for `fail`, the
  text is the firmware error). `hold` and `release` undo each other; `unfail` removes a `fail`."""
  if kind == "unfail":
    (folder / f"fail-{code}").unlink(missing_ok=True)
  elif kind == "fail":
    (folder / f"fail-{code}").write_text(text or "er01/00")
  elif kind == "hold":
    (folder / f"release-{code}").unlink(missing_ok=True)
    (folder / f"hold-{code}").touch()
  elif kind == "release":
    (folder / f"hold-{code}").unlink(missing_ok=True)
    (folder / f"release-{code}").touch()
  else:
    raise ValueError(kind)


def written(log: Path) -> list[str]:
  """The commands a spawned launcher's instrument was written, in order."""
  if not log.exists():
    return []
  return [json.loads(line) for line in log.read_text().splitlines() if line]
