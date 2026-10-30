"""The run log: every call with what took effect, plus the departure count the prototype measures.

A departure is anything the basic parameters alone could not express: specialized parameters, an
operation the adapter had to translate, a refusal, a device-specific layout.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
import uuid
from collections import Counter
from importlib import metadata
from pathlib import Path
from typing import Any

from ahc.devices.spec import LIQUID_HANDLING_OPS
from ahc.timing import run_timing


def _sh(cmd: list[str]) -> str:
  try:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
  except (OSError, subprocess.SubprocessError) as exc:
    return f"unavailable ({exc})"


def _git(root: Path, *args: str) -> str | None:
  try:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
  except (OSError, subprocess.SubprocessError):
    return None
  return out.stdout.strip() if out.returncode == 0 else None


def ahc_checkout() -> dict[str, Any] | None:
  """The git checkout AHC runs from (the plugin's folder, an editable install): its commit, and whether
  tracked files differ from it. None when AHC runs from an installed package."""
  root = Path(__file__).resolve().parents[2]
  if not (root / ".git").exists():
    return None
  commit = _git(root, "rev-parse", "HEAD")
  if commit is None:
    return None
  changes = _git(root, "status", "--porcelain", "--untracked-files=no")
  return {"path": str(root), "commit": commit, "dirty": None if changes is None else bool(changes)}


def resolved_commit(pkg: str) -> str | None:
  """The commit a package installed from git resolved to (its direct_url.json), e.g. PyLabRobot's pin."""
  try:
    info = json.loads(metadata.distribution(pkg).read_text("direct_url.json") or "{}")
  except (metadata.PackageNotFoundError, ValueError):
    return None
  return (info.get("vcs_info") or {}).get("commit_id") if isinstance(info, dict) else None


def environment() -> dict[str, Any]:
  def version(pkg: str) -> str:
    try:
      return metadata.version(pkg)
    except metadata.PackageNotFoundError:
      return "not installed"

  out: dict[str, Any] = {"python": platform.python_version(), "pylabrobot": version("pylabrobot"),
                         "pylabrobot_commit": resolved_commit("pylabrobot"),
                         "mcp": version("mcp"), "ahc": version("ahc"),
                         "macos_build": _sh(["sw_vers", "-buildVersion"]),
                         "xcodebuild": " / ".join(_sh(["xcodebuild", "-version"]).splitlines())}
  checkout = ahc_checkout()
  if checkout is not None:
    out["ahc_checkout"] = checkout
  return out


class RunLog:
  """One server session's log. The file appears with the first entry, never at startup."""

  def __init__(self, runs_dir: Path, device: str | None, backend: str | None,
               extra: dict[str, Any] | None = None):
    self.runs_dir = runs_dir
    self.run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    self.path = runs_dir / f"{self.run_id}.jsonl"
    self.device = device
    self.backend = backend
    self.extra = extra or {}
    self.entries: list[dict[str, Any]] = []
    self.written: list[dict[str, Any]] = []  # the same entries as written, with their `t`, for the timing
    self.started = False

  def _start(self) -> None:
    self.runs_dir.mkdir(parents=True, exist_ok=True)
    self.started = True
    self._write({"type": "run", "run_id": self.run_id, "device": self.device, "backend": self.backend,
                 **self.extra, "environment": environment()})

  def _write(self, entry: dict[str, Any]) -> None:
    """Append one entry. The folder is made again if someone removed it; if the file still cannot be
    written (a full disk), the entry goes to stderr, so a log failure never hides what happened."""
    stamped = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    self.written.append(stamped)
    line = json.dumps(stamped, default=str) + "\n"
    try:
      self.runs_dir.mkdir(parents=True, exist_ok=True)
      with self.path.open("a") as f:
        f.write(line)
    except OSError as exc:
      print(f"ahc: run log {self.path} not writable ({exc}); entry: {line}", end="", file=sys.stderr)

  def record(self, entry: dict[str, Any]) -> None:
    if not self.started:
      self._start()
    self.entries.append(entry)
    self._write(entry)

  def summary(self) -> dict[str, Any]:
    calls = [e for e in self.entries if "op" in e]
    lh_ok = [e for e in calls if e["op"] in LIQUID_HANDLING_OPS and e["status"] == "ok"]
    specialized = [e for e in lh_ok if e.get("specialized")]
    translated = [e for e in lh_ok if e.get("translated")]
    layouts = [e for e in calls if e["op"] == "load_layout" and e["status"] == "ok"]
    return {
      "run_id": self.run_id,
      "device": self.device,
      "backend": self.backend,
      "log": str(self.path) if self.started else None,
      "liquid_handling_calls": len(lh_ok),
      "basic_only": len([e for e in lh_ok if not e.get("specialized") and not e.get("translated")]),
      "with_specialized": {"calls": len(specialized),
                           "params": dict(Counter(k for e in specialized for k in e["specialized"]))},
      "translated": {"calls": len(translated), "ops": dict(Counter(e["op"] for e in translated))},
      "refused": dict(Counter(e["error"] for e in calls if e["status"] == "refused")),
      "layout_kind": layouts[-1]["effective"]["kind"] if layouts else None,
      "commands_sent": sum(e.get("commands", 0) for e in calls if e["status"] == "ok"),
      "verifications": [{k: e[k] for k in ("check", "verdict", "judged_by")}
                        for e in self.entries if e.get("type") == "verification"],
      "timing": run_timing(self.written),
    }
