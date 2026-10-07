"""The run log: every call with what took effect, plus the departure count the prototype measures.

A departure is anything a serial dilution could not express with basic parameters alone:
specialized parameters, an operation the adapter had to translate, a refusal, a device-specific
layout.
"""

from __future__ import annotations

import json
import platform
import subprocess
import time
import uuid
from collections import Counter
from importlib import metadata
from pathlib import Path
from typing import Any

from ahc.devices.spec import LIQUID_HANDLING_OPS


def _sh(cmd: list[str]) -> str:
  try:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
  except (OSError, subprocess.SubprocessError) as exc:
    return f"unavailable ({exc})"


def environment() -> dict[str, str]:
  def version(pkg: str) -> str:
    try:
      return metadata.version(pkg)
    except metadata.PackageNotFoundError:
      return "not installed"

  return {"python": platform.python_version(), "pylabrobot": version("pylabrobot"),
          "mcp": version("mcp"), "ahc": version("ahc"),
          "macos_build": _sh(["sw_vers", "-buildVersion"]),
          "xcodebuild": " / ".join(_sh(["xcodebuild", "-version"]).splitlines())}


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
    self.started = False

  def _start(self) -> None:
    self.runs_dir.mkdir(parents=True, exist_ok=True)
    self.started = True
    self._write({"type": "run", "run_id": self.run_id, "device": self.device, "backend": self.backend,
                 **self.extra, "environment": environment()})

  def _write(self, entry: dict[str, Any]) -> None:
    entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    with self.path.open("a") as f:
      f.write(json.dumps(entry, default=str) + "\n")

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
    }
