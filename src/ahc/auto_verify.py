"""Auto-verify: when a camera check is answered by the agent instead of the person, and what is kept for review.

The person authorises it once in the confirmed config (auto_verify: checks, min_streak). Whether a check is auto
*now* is decided by AHC from measurements, never by the agent: off_reason() gives the first condition that fails,
in plain words, or None when the check is auto:

  listed       the check type is in auto_verify.checks
  paused       .ahc/auto-pause.json (the dashboard's "Pause auto" button) is absent
  stop         no emergency stop is engaged
  recovery     the check is not the recovery check of every pipette (after a stop, an unknown outcome or a restart)
  confidence   run level: every verification point of every check type in auto_verify.checks is confident in
               physical_world_confidence (covered at its level by a fresh observation, the camera aligned). One point
               of a listed type that is uncovered or unclear turns auto off for every check; points of check types
               that are not listed do not count
  coverage     the check itself has points, and a liquid check's targets lie inside them
  streak       the agent's image judgments agreed with the person's verdicts min_streak times in a row (per check type)

An auto check is answered by agent_verdict. A pass is logged with its images and listed for the person to review
(.ahc/auto-verdicts.jsonl, shown on the dashboard); a fail or an unclear judgment is never recorded as a verdict: the
check goes to the person. The dashboard shows the same reasons from the files the server writes
(.ahc/state/auto.json: the run-level confidence reason and the coverage reason per check type).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ahc import vision_points as vpoints
from ahc.workspace import estop
from ahc.workspace.config import Config
from ahc.workspace.store import Workspace, write_atomic

JUDGED_BY = "agent (auto, cameras)"
REVIEW_KEPT = 30  # auto verdicts the dashboard lists


def pause_path(ws: Workspace) -> Path:
  return ws.dir / "auto-pause.json"


def verdicts_path(ws: Workspace) -> Path:
  return ws.dir / "auto-verdicts.jsonl"


def state_path(ws: Workspace) -> Path:
  return ws.state_dir / "auto.json"


def paused(ws: Workspace) -> dict[str, Any] | None:
  """The pause the person set on the dashboard, or None. A file that cannot be read counts as a pause."""
  path = pause_path(ws)
  if not path.exists():
    return None
  try:
    data = json.loads(path.read_text())
  except (OSError, ValueError):
    return {"at": None, "by": "unreadable file"}
  return data if isinstance(data, dict) else {"at": None, "by": "unreadable file"}


def pause(ws: Workspace, by: str = "dashboard") -> dict[str, Any]:
  record = {"paused": True, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": by}
  ws.dir.mkdir(parents=True, exist_ok=True)
  write_atomic(pause_path(ws), json.dumps(record, indent=2) + "\n")
  return record


def resume(ws: Workspace) -> bool:
  """Remove the pause; False when there was none."""
  existed = pause_path(ws).exists()
  pause_path(ws).unlink(missing_ok=True)
  return existed


def off_reason(check: str, auto: Any, *, pause: dict[str, Any] | None, stopped: bool, recovery: bool,
               coverage: str | None, streak: int, confidence: str | None = None) -> str | None:
  """The first condition that keeps `check` with the person, in plain words; None when it is auto. `auto`: the
  config's AutoVerify (or None); `confidence`: why the run's listed points are not all confident (None: they are);
  `coverage`: why this check has nothing to judge from (no points, a target outside them; None: it has)."""
  if auto is None or check not in auto.checks:
    return (f"{check} is not in the config's auto_verify.checks: the person has not let the cameras answer it"
            if auto is not None else "the config has no auto_verify: the person has not let the cameras answer checks")
  if pause is not None:
    return "auto-verify is paused (Pause auto on the dashboard): the person resumes it"
  if stopped:
    return "an emergency stop is engaged"
  if recovery:
    return ("this is the recovery check of every pipette (after a stop, an unknown outcome or a restart): "
            "it always stays with the person")
  if confidence is not None:
    return confidence
  if coverage is not None:
    return coverage
  if streak < auto.min_streak:
    return (f"the agent's image judgments agreed with the person's {streak} time{'' if streak == 1 else 's'} in a row "
            f"on {check}; {auto.min_streak} are needed (propose_verdict before each record_verdict)")
  return None


# -- the files the dashboard reads -------------------------------------------------------------------

def write_coverage(ws: Workspace, coverage: dict[str, str | None], confidence: str | None = None) -> None:
  """What the server measured now: the run-level confidence reason (None: every listed point is confident) and, per
  check type, None when it has points to judge from, else why not."""
  ws.state_dir.mkdir(parents=True, exist_ok=True)
  write_atomic(state_path(ws), json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "confidence": confidence,
                                           "coverage": coverage}, indent=2) + "\n")


def read_coverage(ws: Workspace) -> dict[str, Any]:
  try:
    data = json.loads(state_path(ws).read_text())
  except (OSError, ValueError):
    return {}
  return data if isinstance(data, dict) else {}


def append_verdict(ws: Workspace, entry: dict[str, Any]) -> None:
  """One auto verdict (a pass the agent gave) for the person to review."""
  ws.dir.mkdir(parents=True, exist_ok=True)
  with verdicts_path(ws).open("a") as f:
    f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}) + "\n")


def read_verdicts(ws: Workspace, limit: int = REVIEW_KEPT) -> list[dict[str, Any]]:
  """The newest auto verdicts, newest first. Images are shown by their path under .ahc/runs."""
  try:
    lines = verdicts_path(ws).read_text().splitlines()
  except OSError:
    return []
  out: list[dict[str, Any]] = []
  for line in reversed(lines):
    try:
      item = json.loads(line)
    except ValueError:
      continue
    if isinstance(item, dict):
      out.append(item)
    if len(out) >= limit:
      break
  return out


def review_images(ws: Workspace, paths: list[str]) -> list[str]:
  """Image paths relative to .ahc/runs (what the dashboard serves); anything outside it is left out."""
  runs = ws.runs_dir.resolve()
  out = []
  for raw in paths:
    try:
      out.append(str(Path(raw).resolve().relative_to(runs)))
    except (ValueError, OSError):
      continue
  return out


def dashboard_view(ws: Workspace, config: Config | None) -> dict[str, Any] | None:
  """What the dashboard shows: the run-level state (on, or off with why), per authorised check type AUTO or the
  reason it is off, the pause, the review list. None when the config has no auto_verify."""
  auto = config.auto_verify if config else None
  if auto is None:
    return None
  record = {}
  try:
    data = json.loads((ws.state_dir / "vision_record.json").read_text())
    if isinstance(data, dict) and data.get("maps") == vpoints.maps_digest(config) and isinstance(data.get("checks"), dict):
      record = data["checks"]
  except (OSError, ValueError):
    pass
  held = paused(ws)
  stopped = bool((estop.read(ws) or {}).get("engaged"))
  measured = read_coverage(ws)
  cover = measured.get("coverage") if isinstance(measured.get("coverage"), dict) else {}
  confidence = measured.get("confidence") if isinstance(measured.get("confidence"), str) else None
  checks = {}
  for check in auto.checks:
    if check in cover:
      coverage = cover[check]
    else:
      coverage = "no camera check yet: the agent runs physical_world_context once a plan is reported and a layout loaded"
    streak = (record.get(check) or {}).get("agree_streak", 0) if isinstance(record.get(check), dict) else 0
    why = off_reason(check, auto, pause=held, stopped=stopped, recovery=False, coverage=coverage, streak=int(streak or 0),
                     confidence=confidence)
    checks[check] = {"state": "off" if why else "auto", "why": why, "streak": int(streak or 0)}
  run_why = ("auto-verify is paused (Pause auto on the dashboard)" if held is not None else
             "an emergency stop is engaged" if stopped else
             "the verification points are not checked yet: the agent runs physical_world_context once a plan is reported "
             "and a layout is loaded" if not cover else confidence)
  run = {"state": "off" if run_why else "on", "why": run_why}
  review = []
  for item in read_verdicts(ws):
    review.append({"t": item.get("t"), "check": item.get("check"), "check_id": item.get("check_id"), "note": item.get("note"),
                   "per_point": item.get("per_point") or [], "images": review_images(ws, [str(p) for p in item.get("images") or []])})
  return {"run": run, "checks": checks, "min_streak": auto.min_streak, "paused": held, "measured_at": measured.get("at"), "review": review}
