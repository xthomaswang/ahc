"""How long a task run took and where the time went, computed from the run log alone.

The dashboard replay and old run logs use the same function as the server, so the numbers agree.

Times in the log. Every entry has `t` (to the second) which is when the entry was written: for a tool
call, when it ended. Calls logged since timing was added also carry `t_start` (ISO, milliseconds) and
`duration_s` (3 decimals, measured with a monotonic clock). An old entry has `t` only: its start and end are
both `t`, and its duration is unknown.

The split of the run into robot, agent and others:
  robot_s   the motion calls (load_layout, pick_up_tips, aspirate, dispense, mix, drop_tips, invoke) running on
            the device: the sum of their `duration_s`. Measured; None when the log has no durations.
  others_s  everything that is neither robot nor agent, in four parts (the `others` dict). All estimates; each
            includes a little agent time (calling record_verdict, reading the result). In Claude Code the
            approval dialog is before the server sees the call, so a wait ends when the person's call arrives.
              person_checks_s  a check pending until its verdict (or the layout reload that voids it)
              go_ahead_s       the config/go-ahead wait: the `wait` marker the server logs until confirm_config
              estop_engaged_s  an emergency stop from engaged to released (only the person releases it)
              after_release_s  the recovery after a release (tips removed by hand, the deck checked again): from the
                               release until the next pending check, verdict or motion call, whichever is first
  agent_s   the rest: the agent thinking, plus tool overhead. An estimate (the remainder).
Time claimed twice counts once: the robot wins, then the others in the order above. All of it is clipped to the
run, so robot_s + others_s + agent_s = total_s (robot_s counts as 0 when it is None, and agent_s then holds it).

A plan step runs from the first entry that names it (a call's plan_step, a report's current_step, the
plan_done report for the last step, a verdict by way of its check) to the last such entry. The time between
steps (the agent deciding what comes next) belongs to no step, so the steps add up to less than total_s. It
is first-to-last, not first-to-next-step, because a step then never absorbs thinking that belongs to the next.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ahc.devices.spec import LIQUID_HANDLING_OPS

MOTION_OPS = {*LIQUID_HANDLING_OPS, "load_layout", "invoke"}
OTHERS = ("person_checks_s", "go_ahead_s", "estop_engaged_s", "after_release_s")  # in the order they claim time
NOTES = {
  "robot_s": "measured: the sum of the motion calls' durations (None if the log has no durations)",
  "others_s": "estimate: neither robot nor agent: checks pending until the person's verdict, the config/go-ahead "
              "wait, an emergency stop engaged until released, and the recovery after a release until the next "
              "check, verdict or motion call; each includes a little agent time",
  "agent_s": "estimate: the rest of the run (agent thinking, tool overhead; the robot's time too if not measured)",
  "steps": "first to last entry naming the step; the gaps between steps belong to no step",
}


def _time(value: Any) -> datetime | None:
  try:
    return datetime.fromisoformat(value) if isinstance(value, str) else None
  except ValueError:
    return None


def _iso(moment: datetime) -> str:
  return moment.isoformat(timespec="milliseconds" if moment.microsecond else "seconds")


def _span(entry: dict[str, Any]) -> tuple[datetime, datetime] | None:
  """When the entry began and ended: from t_start and duration_s when it has them, else t alone."""
  end = _time(entry.get("t"))
  start = _time(entry.get("t_start"))
  duration = entry.get("duration_s")
  if start is not None and isinstance(duration, (int, float)):
    return start, datetime.fromtimestamp(start.timestamp() + duration)
  if end is None:
    return None
  return (start, end) if start is not None and start <= end else (end, end)


def _merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
  out: list[tuple[float, float]] = []
  for a, b in sorted(spans):
    if out and a <= out[-1][1]:
      out[-1] = (out[-1][0], max(out[-1][1], b))
    else:
      out.append((a, b))
  return out


def rounded(x: float | None) -> float | None:
  return None if x is None else round(x, 3)


def _length(spans: list[tuple[float, float]]) -> float:
  return sum(b - a for a, b in spans)


def _minus(spans: list[tuple[float, float]], cut: list[tuple[float, float]]) -> list[tuple[float, float]]:
  """Merged `spans` without the merged `cut`."""
  out = []
  for a, b in spans:
    for c, d in cut:
      if d <= a or c >= b:
        continue
      if c > a:
        out.append((a, c))
      a = max(a, d)
      if a >= b:
        break
    if a < b:
      out.append((a, b))
  return out


def is_motion(entry: dict[str, Any]) -> bool:
  op = entry.get("op")
  return isinstance(op, str) and op.split(":")[0] in MOTION_OPS


def _moved(entry: dict[str, Any]) -> bool:
  """A motion call that reached the device: not one refused before anything moved."""
  return is_motion(entry) and (entry.get("status") != "refused" or bool(entry.get("partial")))


def run_timing(entries: list[dict[str, Any]]) -> dict[str, Any]:
  """The timing of a run from its log entries (see the module doc). Works on any log, old or new."""
  spans = [(e, _span(e)) for e in entries if isinstance(e, dict)]
  spans = [(e, s) for e, s in spans if s is not None]
  if not spans:
    return {"run": None, "steps": [], "split": None, "counts": {"calls": 0, "refused": 0, "checks": 0, "estops": 0},
            "precise": False, "notes": NOTES}
  start = min(s[0] for _, s in spans)
  plan: list[str] | None = None
  steps: dict[int, dict[str, Any]] = {}
  check_step: dict[Any, int] = {}  # a check's step, so its verdict (which names none) counts for that step
  waiting: dict[Any, datetime] = {}  # a check pending: since when
  go_ahead: datetime | None = None
  stopped: dict[Any, datetime] = {}  # emergency stops engaged: since when, by stop id
  others: dict[str, list[tuple[datetime, datetime]]] = {k: [] for k in OTHERS}
  released: datetime | None = None  # the last stop released, the recovery not yet over
  robot: list[tuple[datetime, datetime]] = []
  measured = unmeasured = 0
  done_at: datetime | None = None
  latest = start  # the latest end so far
  counts = {"calls": 0, "refused": 0, "checks": 0, "estops": 0}

  def reached(step: int, entry: dict[str, Any], span: tuple[datetime, datetime]) -> None:
    rec = steps.setdefault(step, {"start": span[0], "end": span[1], "calls": 0})
    rec["start"], rec["end"] = min(rec["start"], span[0]), max(rec["end"], span[1])
    rec["calls"] += 1 if _moved(entry) else 0

  for e, span in spans:
    kind = e.get("type")
    step: int | None = None
    if released is not None and (is_motion(e) or kind == "verification" or (kind == "estop" and e.get("state") == "engaged")):
      others["after_release_s"].append((released, max(span[0], released)))  # the recovery is over
      released = None
    if kind == "agent":
      if e.get("plan") is not None:  # a new plan replaces the old one and its steps
        plan, steps, check_step = list(e["plan"]), {}, {}
      if isinstance(e.get("current_step"), int):
        step, done_at = e["current_step"], None
      if e.get("plan_done") and plan:  # its `t` is to the second: never before a call that ended just ahead of it
        step, done_at = len(plan), max(span[1], latest)
    else:
      if isinstance(e.get("plan_step"), int):
        step = e["plan_step"]
      if is_motion(e) or kind == "verification":
        done_at = None  # a call after plan_done: the agent went on working
    if kind == "verification":
      check_id, verdict = e.get("check_id"), e.get("verdict")
      if verdict == "pending":
        waiting[check_id] = span[1]  # the person is asked once verify has answered
        counts["checks"] += 1
        if step is not None:
          check_step[check_id] = step
      else:
        if step is None:
          step = check_step.get(check_id)
        if check_id in waiting:
          since = waiting.pop(check_id)
          others["person_checks_s"].append((since, max(span[0], since)))
    elif kind == "wait":  # the server noted a wait for the person (the config or the session's go-ahead)
      go_ahead = go_ahead or span[0]
    elif e.get("op") == "confirm_config" and go_ahead is not None:
      others["go_ahead_s"].append((go_ahead, max(span[0], go_ahead)))
      go_ahead = None
    elif kind == "estop":
      if e.get("state") == "engaged":
        if e.get("id") not in stopped:
          counts["estops"] += 1
          stopped[e.get("id")] = span[0]
      elif e.get("id") in stopped:  # released: the recovery starts
        others["estop_engaged_s"].append((stopped.pop(e.get("id")), span[0]))
        released = span[0]
    if is_motion(e):
      if e.get("status") == "refused":
        counts["refused"] += 1
      if _moved(e):
        counts["calls"] += 1
        if isinstance(e.get("duration_s"), (int, float)):
          measured += 1
          robot.append(span)
        else:
          unmeasured += 1
      if e.get("op") == "load_layout" and e.get("status") == "ok":
        for since in waiting.values():  # a new layout: earlier checks no longer ask the person anything
          others["person_checks_s"].append((since, max(span[0], since)))
        waiting.clear()
    elif e.get("status") == "refused":
      counts["refused"] += 1
    if step is not None and (plan is None or 1 <= step <= len(plan)):
      reached(step, e, span)
    latest = max(latest, span[1])

  end = done_at or max(s[1] for _, s in spans)
  for key, open_since in (("person_checks_s", waiting.values()), ("estop_engaged_s", stopped.values()),
                          ("go_ahead_s", [go_ahead] if go_ahead else []), ("after_release_s", [released] if released else [])):
    others[key] += [(since, end) for since in open_since if since < end]  # still going when the log ends
  lo, hi = start.timestamp(), end.timestamp()

  def clipped(spans_: list[tuple[datetime, datetime]]) -> list[tuple[float, float]]:
    return _merge([(max(a.timestamp(), lo), min(b.timestamp(), hi)) for a, b in spans_ if min(b.timestamp(), hi) > max(a.timestamp(), lo)])

  claimed = clipped(robot)
  robot_s = _length(claimed) if measured else None
  parts: dict[str, float] = {}
  for key in OTHERS:  # time claimed twice counts once: the robot first, then the others in this order
    mine = _minus(clipped(others[key]), claimed)
    parts[key] = rounded(_length(mine))
    claimed = _merge(claimed + mine)
  total_s = hi - lo
  others_s = rounded(sum(parts.values()))
  agent_s = rounded(max(0.0, rounded(total_s) - (rounded(robot_s) or 0.0) - others_s))
  return {
    "run": {"start": _iso(start), "end": _iso(end), "total_s": rounded(total_s), "done": done_at is not None},
    "steps": [{"step": n, "text": plan[n - 1] if plan and n <= len(plan) else None, "start": _iso(r["start"]),
               "end": _iso(r["end"]), "duration_s": rounded((r["end"] - r["start"]).total_seconds()),
               "hardware_calls": r["calls"]} for n, r in sorted(steps.items())],
    "split": {"robot_s": rounded(robot_s), "agent_s": agent_s, "others_s": others_s, "others": parts,
              "robot_measured": measured > 0 and unmeasured == 0},
    "counts": counts,
    "precise": any(e.get("t_start") for e, _ in spans),  # False: a log from before timing, to the second
    "notes": NOTES,
  }

