"""One server per robot: an exclusive lock on `<AHC_HOME>/locks/<robot name>.lock`, held while a server's run is open.

A real robot is driven by at most one AHC server at a time, however many agent sessions are open on however many
task folders. The lock is a `flock` on the file, so it frees itself when the holding process dies (a kill, a
crash); the file stays. The holder writes who it is into the file (pid, task folder, when its session started,
the Claude Code session id if the environment has one) for the refusal of the next server to name; the lock never
depends on that text, which a reader may find empty or odd.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from ahc.workspace.store import ahc_home

SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")  # what Claude Code puts in a child's environment


def lock_path(robot: str) -> Path:
  """locks/<robot>.lock; a name with characters a file name should not carry is made safe and told apart by a hash."""
  safe = re.sub(r"[^A-Za-z0-9_.-]", "_", robot).lstrip(".") or "robot"
  if safe != robot:
    safe = f"{safe}-{hashlib.sha1(robot.encode()).hexdigest()[:8]}"
  return ahc_home() / "locks" / f"{safe}.lock"


def claude_session() -> str | None:
  return next((os.environ[v] for v in SESSION_VARS if os.environ.get(v)), None)


class RobotLock:
  """The lock on one robot. acquire() returns None once held, else what the file says about the holder."""

  def __init__(self, robot: str):
    self.robot = robot
    self.path = lock_path(robot)
    self._fd: int | None = None

  @property
  def held(self) -> bool:
    return self._fd is not None

  def acquire(self, who: dict[str, Any]) -> dict[str, Any] | None:
    """Take the lock (non-blocking) and write `who` into it. None: taken. A dict: another process holds it, with
    whatever its file says ({} when that is empty or unreadable). Raises OSError where the lock cannot be made."""
    if self._fd is not None:
      return None
    self.path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
      fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      holder = self._read(fd)
      os.close(fd)
      return holder
    except BaseException:
      os.close(fd)
      raise
    self._fd = fd
    try:
      os.ftruncate(fd, 0)
      os.pwrite(fd, (json.dumps({"robot": self.robot, "pid": os.getpid(), **who}, indent=2) + "\n").encode(), 0)
    except OSError:  # the text is a courtesy: the lock is held all the same
      pass
    return None

  @staticmethod
  def _read(fd: int) -> dict[str, Any]:
    for _ in range(5):  # the holder writes right after it locks: a moment of an empty file is not "no holder"
      try:
        data = json.loads(os.pread(fd, 65536, 0) or b"null")
      except (OSError, ValueError):
        data = None
      if isinstance(data, dict):
        return data
      time.sleep(0.02)
    return {}

  def release(self) -> None:
    """Let go (closing the descriptor frees the flock). The file stays: deleting it would let two processes hold
    a lock each, on two files of one name."""
    fd, self._fd = self._fd, None
    if fd is not None:
      try:
        os.ftruncate(fd, 0)  # the text goes with the hold
      except OSError:
        pass
      os.close(fd)
