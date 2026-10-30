"""Live run dashboard: replays a task's newest run log into a plate heatmap and a call timeline.

It reads only the run log the MCP server writes, so it works the same whether a script or an agent
is driving. Concentrations are the digital twin's estimate (liquid named by a `stock` alias is 1,
everything else 0, complete mixing assumed); volumes match the server's trackers.

    ahc-dashboard                       # in a task folder: follows the newest active log in .ahc/runs
    ahc-dashboard --workspace <folder>
    ahc-dashboard --log <folder>/.ahc/runs/X.jsonl

Its stop button stops the server of the task folder the page names (in its title and stop bar); it
answers only to its own page (Host and Origin checked, never framed).

    ahc-dashboard --view-only           # the same page for an audience: no stop or release, every POST refused

Anyone at a dashboard can press release, so the person keeps the control page and shows the view-only one.

Cameras: a camera with a slot map shows its reference image with the boxes drawn over it, and what the cameras
cover (the last physical_world_context: per source and per check type). The control page lets the person drag the boxes (corners resize, the body
moves) and save; a save writes the config, which is pending until they confirm it again. The view-only page
shows the same, with no handles and no save.
"""

from __future__ import annotations

import argparse
import functools
import hmac
import html
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ahc import auto_verify
from ahc.core.errors import LabError
from ahc.devices.spec import LIQUID_HANDLING_OPS
from ahc.plan import PlanProgress
from ahc.timing import run_timing
from ahc.workspace import estop
from ahc.workspace.store import Workspace, resolve_workspace

ROWS = "ABCDEFGH"
QUIET = {"run", "estop", "audit"}  # what any server on the folder logs for a stop, busy or idle


def latest_log(runs_dir: Path) -> Path | None:
  """The log of the run going on: the newest with device or agent activity (a call, a report, a check, a
  layout), not merely the newest file. Every server open on the folder logs a stop, and an idle one's
  log must not take the page over while the person decides whether the deck is safe."""
  logs = []
  for p in runs_dir.glob("*.jsonl"):
    try:
      st = p.stat()
    except OSError:  # removed meanwhile
      continue
    logs.append((st.st_mtime_ns, st.st_size, p))
  logs.sort(reverse=True)
  for mtime, size, p in logs:
    if _active(str(p), mtime, size):
      return p
  return logs[0][2] if logs else None


@functools.lru_cache(maxsize=256)
def _active(path: str, _mtime: int, _size: int) -> bool:
  try:
    with open(path, encoding="utf-8", errors="replace") as f:
      for line in f:
        try:
          e = json.loads(line)
        except ValueError:
          continue
        if isinstance(e, dict) and ("op" in e or e.get("type") not in QUIET):
          return True
  except OSError:
    pass
  return False


def _expand(target: str, geometry: dict[str, Any] | None = None) -> list[str]:
  """`plate:A1:H1` -> plate:A1 .. plate:H1 (column by column); `plate:col2` -> plate:A2 .. plate:H2;
  `plate:rowB` -> plate:B1 .. plate:B12 (the plate's size from `geometry`); `trough` -> trough. A form
  this cannot read stays as it is: the page never fails on a target."""
  name, _, wells = target.partition(":")
  if not wells:
    return [name]
  shape = (geometry or {}).get(name) or {}
  rows = ROWS[: shape.get("rows") or len(ROWS)]
  column = re.fullmatch(r"col(\d+)", wells, re.IGNORECASE)
  if column:
    return [f"{name}:{r}{int(column.group(1))}" for r in rows]
  row = re.fullmatch(r"row([a-z])", wells, re.IGNORECASE)
  if row:
    return [f"{name}:{row.group(1).upper()}{c}" for c in range(1, (shape.get("cols") or 12) + 1)]
  try:
    first, _, last = wells.partition(":")
    last = last or first
    rows = ROWS[ROWS.index(first[0].upper()): ROWS.index(last[0].upper()) + 1]
    return [f"{name}:{r}{c}" for c in range(int(first[1:]), int(last[1:]) + 1) for r in rows]
  except (ValueError, IndexError):
    return [target]


class _Names:
  """PyLabRobot child names (plate_well_A1, tips_tipspot_A1, OT-2 racks' tips_A1) -> labware:A1."""

  def __init__(self, geometry: dict[str, Any]):
    self.labware = set(geometry)
    self.parents = sorted((n for n, g in geometry.items() if g["kind"] in ("plate", "tip_rack")),
                          key=len, reverse=True)

  def __call__(self, name: str) -> str:
    if name in self.labware:
      return name
    for parent in self.parents:
      if name.startswith(parent + "_"):
        rest = name[len(parent) + 1:].removeprefix("well_").removeprefix("tipspot_")
        if re.fullmatch(r"[A-P]\d{1,2}", rest):
          return f"{parent}:{rest}"
    return name


def _read(path: Path) -> list[dict[str, Any]]:
  entries = []
  for line in path.open():
    try:
      entries.append(json.loads(line))
    except json.JSONDecodeError:  # the server may be mid-way through appending the last line
      break
  return entries


def replay(path: Path | None, stock: list[str] | None = None, ahc_dir: Path | None = None) -> dict[str, Any]:
  """A run log as the dashboard shows it. Without one (`path` None, the task's `ahc_dir` given) only
  the agent and the config: in Claude Code the agent shows from its first AHC call, before any run."""
  entries = _read(path) if path is not None else []
  ahc_dir = path.parent.parent if path is not None else ahc_dir
  header = entries[0] if entries and entries[0].get("type") == "run" else {}
  geometry: dict[str, Any] = {}
  aliases: dict[str, str] = {}
  names = _Names({})
  vol: dict[str, float] = {}  # uL per container, keyed labware or labware:A1
  amt: dict[str, float] = {}  # of which stock, in uL
  used_tips: set[str] = set()
  returned_tips: set[str] = set()  # back in their spots after use: never fresh again (next fresh skips them)
  last_pick: dict[str, list[str]] = {}
  tips_in_hand: dict[tuple[str, int], tuple[float, float]] = {}  # (device, channel) -> (uL, stock uL)
  events: list[dict[str, Any]] = []
  decisions: list[dict[str, Any]] = []  # what the agent said it decided (report_decision)
  progress = PlanProgress()  # rebuilt from the log exactly as the server keeps it
  gate: dict[str, Any] | None = None
  layout_info: dict[str, Any] | None = None
  stopped: dict[str, Any] | None = None  # the engaged emergency stop, as the server logged it
  waiting: dict[str, dict[str, Any]] = {}  # the person's checks still without a verdict, by check id

  for i, e in enumerate(entries[1:], 1):
    progress.apply(e)
    if e.get("type") == "agent":
      decisions.append({k: e[k] for k in ("t", "decision", "why", "current_step", "waiting_for", "plan_done") if k in e})
      continue
    if e.get("type") == "verification":
      if e.get("verdict") == "pending" and e.get("auto"):  # the agent judges it from camera images (auto-verify)
        waiting.pop(e.get("check_id"), None)
      elif e.get("verdict") == "pending":  # the person's verdict, on real hardware or for a layout without a reference
        waiting[e.get("check_id")] = {"check_id": e.get("check_id"), "check": e.get("check"), "t": e.get("t"),
                                      "what": _check_text(e, names)}
      else:
        waiting.pop(e.get("check_id"), None)
      events.append({"i": i, "t": e.get("t"), "op": "verify", "status": e.get("verdict"),
                     "detail": f"{e.get('check')} → {e.get('verdict')} ({'waits for the agent to judge it from camera images' if e.get('auto') else e.get('judged_by')})"})
      continue
    if e.get("type") == "vision":
      if e.get("event") == "handback" and e.get("check_id") in waiting:  # the person's now: show what the agent said
        waiting[e["check_id"]]["said"] = {k: e.get(k) for k in ("verdict", "note", "why") if e.get(k)}
      elif e.get("event") == "auto_verdict":
        events.append({"i": i, "t": e.get("t"), "op": "auto verdict", "status": "pass",
                       "detail": f"{e.get('check')}: the agent judged pass from {len(e.get('images') or [])} image(s)"
                                 + (f" · {e['note']}" if e.get("note") else "")})
      continue
    if e.get("type") == "gate":
      # A gate waits for its own deck check: an earlier one (before a reload, or a released stop) opens nothing.
      for check_id in [k for k, c in waiting.items() if c["check"] == "deck_matches_layout" and k != e.get("check_id")]:
        del waiting[check_id]
      gate = {"state": e.get("state"), "layout": e.get("layout"), "reason": e.get("reason"), "t": e.get("t"),
              "needs_person": bool(e.get("needs_person"))}
      if layout_info is not None and e.get("layout") == layout_info.get("name") and e.get("state") == "passed":
        layout_info["confirmed_by"] = "the person" if "person" in (e.get("reason") or "") else "the simulator"
      events.append({"i": i, "t": e.get("t"), "op": "motion gate", "status": e.get("state"),
                     "detail": f"layout {e.get('layout')}: {e.get('reason')}"})
      continue
    if e.get("type") == "estop":
      engaged = e.get("state") == "engaged"
      stopped = {"at": e.get("at"), "by": e.get("by")} if engaged else None
      events.append({"i": i, "t": e.get("t"), "op": "EMERGENCY STOP", "status": "stop" if engaged else "released",
                     "detail": (f"pressed ({e.get('by')})" + (", interrupted the running command" if e.get("interrupted") else ""))
                               if engaged else f"released ({e.get('by')}); the deck must be checked again"})
      continue
    if e.get("type") == "audit":
      events.append({"i": i, "t": e.get("t"), "op": "audit", "status": "audit",
                     "detail": f"{e.get('event')}: {e.get('detail')}"})
      continue
    if "op" not in e:
      continue
    op, ok, eff = e["op"], e.get("status") == "ok", e.get("effective") or {}
    if ok and op == "load_layout":
      waiting.clear()  # a new layout: what was checked before no longer describes the deck
      layout_info = {"name": eff.get("saved_as"), "kind": eff.get("kind"), "labware": eff.get("labware", {}),
                     "layout": (e.get("basic") or {}).get("layout"), "t": e.get("t"),
                     "needs_person": bool(eff.get("needs_person"))}
      geometry, aliases = eff.get("geometry", {}), eff.get("aliases", {})
      names = _Names(geometry)
      vol, amt, used_tips, returned_tips, last_pick, tips_in_hand = {}, {}, set(), set(), {}, {}
      stock_keys = {k for alias, target in aliases.items() if "stock" in alias or alias in (stock or [])
                    for k in _expand(target, geometry)}
      stock_keys |= {n for n in geometry if "stock" in n or n in (stock or [])}
      for name, v in eff.get("liquids_ul", {}).items():
        key = names(name)
        vol[key], amt[key] = v, (v if key in stock_keys else 0.0)
    elif ok and op in ("aspirate", "dispense"):
      # Channel by channel, so channels sharing one trough draw from it in turn.
      for ch, (name, v) in enumerate(zip(eff.get("containers", []), eff.get("volumes_ul", []))):
        c, hand = names(name), (e.get("device"), ch)
        tv, ta = tips_in_hand.get(hand, (0.0, 0.0))
        if op == "aspirate":
          held = vol.get(c, 0.0)
          take = amt.get(c, 0.0) * v / held if held > 1e-9 else 0.0
          vol[c], amt[c] = held - v, amt.get(c, 0.0) - take
          tips_in_hand[hand] = (tv + v, ta + take)
        else:
          give = ta * v / tv if tv > 1e-9 else 0.0
          vol[c], amt[c] = vol.get(c, 0.0) + v, amt.get(c, 0.0) + give
          tips_in_hand[hand] = (max(tv - v, 0.0), ta - give)
    elif e.get("status") == "refused" and isinstance(e.get("partial"), dict):
      # Refused half-way: liquid that left containers is in the tips, one container per channel in order
      # (a shared container is split over the channels); liquid that arrived came out of them.
      dev = e.get("device")
      taken = [(names(n), -d) for n, d in e["partial"].items() if d < 0]
      arrived = [(names(n), d) for n, d in e["partial"].items() if d > 0]
      channels = max(len(last_pick.get(dev, [])), 1)
      shares = [(taken[0][0], taken[0][1] / channels)] * channels if len(taken) == 1 and channels > 1 else taken
      for ch, (c, v) in enumerate(shares):
        held = vol.get(c, 0.0)
        take = amt.get(c, 0.0) * v / held if held > 1e-9 else 0.0
        vol[c], amt[c] = held - v, amt.get(c, 0.0) - take
        tv, ta = tips_in_hand.get((dev, ch), (0.0, 0.0))
        tips_in_hand[(dev, ch)] = (tv + v, ta + take)
      for ch, (c, v) in enumerate(arrived):
        tv, ta = tips_in_hand.get((dev, ch), (0.0, 0.0))
        give = ta * v / tv if tv > 1e-9 else 0.0
        vol[c], amt[c] = vol.get(c, 0.0) + v, amt.get(c, 0.0) + give
        tips_in_hand[(dev, ch)] = (max(tv - v, 0.0), ta - give)
    elif ok and op == "pick_up_tips":
      spots = [names(s) for s in eff.get("tips", [])]
      used_tips.update(spots)
      returned_tips.difference_update(spots)  # picked up again (named explicitly): in hand now
      last_pick[e.get("device", "")] = spots
    elif ok and op == "drop_tips":
      tips_in_hand = {k: v for k, v in tips_in_hand.items() if k[0] != e.get("device")}
      if eff.get("mode") == "return":  # used tips, back in their spots: shown as returned, never as fresh
        returned_tips.update(last_pick.pop(e.get("device", ""), []))

    basic = e.get("basic") or {}
    if op in ("aspirate", "dispense"):
      detail = f"{', '.join(basic.get('targets', []))} · {basic.get('volumes')} uL"
    elif op == "mix":
      detail = f"{', '.join(basic.get('targets', []))} · {basic.get('repetitions')} × {basic.get('volume')} uL"
    elif op == "pick_up_tips":
      tips = [names(s) for s in eff.get("tips") or []]
      detail = (tips[0] if len(tips) == 1 else f"{tips[0]}–{tips[-1].split(':')[-1]}") if tips else ""
    elif op == "load_layout":
      detail = f"{eff.get('kind', '')} layout · {len(eff.get('labware', {}))} labware" if ok else ""
    else:
      detail = ", ".join(f"{k}={v}" for k, v in basic.items() if v is not None)
    events.append({"i": i, "t": e.get("t"), "op": op, "device": e.get("device"),
                   "status": e.get("status"), "error": e.get("error"), "message": e.get("message"),
                   # a real robot's call cut short or failed after it was sent: the twin cannot tell what happened
                   "unknown": e.get("partial") == "unknown",
                   "translated": bool(e.get("translated")), "note": e.get("note"),
                   "specialized": sorted((e.get("specialized") or {}).keys()),
                   "commands": e.get("commands"), "detail": detail})

  def conc(key: str) -> float:
    return amt.get(key, 0.0) / vol[key] if vol.get(key, 0.0) > 1e-9 else 0.0

  plates, containers, racks = [], [], []
  alias_of: dict[str, str] = {}
  for alias, target in aliases.items():
    alias_of.setdefault(target.partition(":")[0], alias)
  plate_names = [n for n, g in geometry.items() if g["kind"] == "plate"]
  main_plate = "plate" if "plate" in plate_names else (plate_names[-1] if plate_names else None)
  # Liquids kept in the wells of another plate (an OT-2 reagent plate) show as reservoirs, per alias.
  for alias, target in aliases.items():
    g = geometry.get(target.partition(":")[0], {})
    if g.get("kind") == "plate" and target.partition(":")[0] != main_plate:
      wells = _expand(target, geometry)
      v = sum(vol.get(w, 0.0) for w in wells)
      containers.append({"name": target, "alias": alias, "v": round(v, 3), "max": g["well_max_ul"] * len(wells),
                         "c": sum(amt.get(w, 0.0) for w in wells) / v if v > 1e-9 else 0.0})
  for name, g in geometry.items():
    if g["kind"] == "plate" and name == main_plate:
      wells = {f"{row}{col}": {"v": round(vol.get(f"{name}:{row}{col}", 0.0), 3), "c": conc(f"{name}:{row}{col}")}
               for col in range(1, g["cols"] + 1) for row in ROWS[: g["rows"]]}
      plates.append({"name": name, "rows": g["rows"], "cols": g["cols"], "max": g["well_max_ul"], "wells": wells})
    elif g["kind"] == "container":
      containers.append({"name": name, "alias": alias_of.get(name), "v": round(vol.get(name, 0.0), 3),
                         "max": g["max_ul"], "c": conc(name)})
    elif g["kind"] == "tip_rack":
      used = sorted(s.partition(":")[2] for s in used_tips if s.startswith(name + ":"))
      returned = sorted(s.partition(":")[2] for s in returned_tips if s.startswith(name + ":"))
      racks.append({"name": name, "rows": g["rows"], "cols": g["cols"], "used": used, "returned": returned})

  calls = [ev for ev in events if ev["op"] in LIQUID_HANDLING_OPS]
  ok_calls = [ev for ev in calls if ev["status"] == "ok"]
  summary = {
    "calls": len(ok_calls),
    "basic_only": sum(1 for ev in ok_calls if not ev["translated"] and not ev["specialized"]),
    "translated": sum(1 for ev in ok_calls if ev["translated"]),
    "specialized": sum(1 for ev in ok_calls if ev["specialized"]),
    "refused": sum(1 for ev in events if ev["status"] == "refused"),
    "refused_codes": dict(Counter(ev["error"] for ev in events if ev["status"] == "refused")),
    "commands": sum(ev["commands"] or 0 for ev in events if ev["status"] == "ok"),
  }
  return {"run": {k: header.get(k) for k in ("run_id", "device", "backend")}, "log": str(path) if path else None,
          "workspace": header.get("workspace") or str(ahc_dir.parent), "summary": summary, "plates": plates,
          "containers": containers, "tip_racks": racks, "events": events[-200:], "gate": gate,
          "layout": layout_info, "estop": stopped,
          # as_of: the page ticks the live clocks from it, so they never depend on the browser's time zone
          "timing": {**run_timing(entries), "as_of": datetime.now().isoformat(timespec="milliseconds")},
          "agent": agent_view(decisions, progress, gate, ahc_dir / "agent-feed.jsonl", list(waiting.values())),
          "config": workspace_config(ahc_dir / "config.yaml"), "auto": auto_state(ahc_dir)}


def auto_state(ahc_dir: Path) -> dict[str, Any] | None:
  """Auto-verify as the page shows it: AUTO or the reason it is off per authorised check type, the pause, the review
  list of the agent's verdicts. None when the config has no auto_verify."""
  from ahc.workspace.config import parse_config

  try:
    config = parse_config((ahc_dir / "config.yaml").read_text())
  except (OSError, LabError):
    return None
  return auto_verify.dashboard_view(Workspace(ahc_dir.parent), config)


def _check_text(entry: dict[str, Any], names: Callable[[str], str]) -> str:
  """What a person check asks, in words, from its run-log entry."""
  twin = ((entry.get("evidence") or {}).get("digital_twin")) or {}
  check = entry.get("check")
  if check == "deck_matches_layout":
    return "the deck matches the loaded layout"
  if check == "tips_mounted" and "pipettes" in twin:  # the check of every pipette at once
    return f"every pipette is empty ({', '.join(twin['pipettes'])})"
  if check == "tips_mounted":
    return "no tips are left on the pipette" if twin.get("expect") == "none" else "the tips are on the pipette, seated straight"
  if check == "liquid_present":
    wells = [names(str(n)) for n in (twin.get("volumes_ul") or {})]
    where = ", ".join(wells[:3]) + (f" and {len(wells) - 3} more" if len(wells) > 3 else "")
    least = twin.get("min_volume_ul")
    return f"liquid in {where or 'the targets'}" + (f", at least {least:g} uL each" if isinstance(least, (int, float)) and least else "")
  return str(check)


def current_state(ws: Workspace, log: Path | Callable[[], Path | None] | None) -> dict[str, Any] | None:
  """What /state serves: the pinned run log (given as a function: once it exists), else the folder's
  newest active log; without one the agent alone once it has started, else nothing."""
  path = log() if callable(log) else (log or latest_log(ws.runs_dir))
  if path is not None and path.is_file():
    return replay(path)
  if (ws.dir / "agent-feed.jsonl").is_file():
    return replay(None, ahc_dir=ws.dir)
  return None


def agent_view(decisions: list[dict[str, Any]], progress: PlanProgress, gate: dict[str, Any] | None,
               feed_path: Path, checks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
  """The agent, live: its decisions and plan (any client: report_decision, and the plan step every call
  names) and, in Claude Code, what it was asked, what it says, what it is calling (or tried while the
  emergency stop refused it) and when its turn ended (from the plugin's hooks). `checks`: the person's
  checks still waiting for a verdict. `state`: waiting (for the person), finished (the plan is done),
  working, ended (the turn ended before the plan was done) or idle."""
  feed = [e for e in _read(feed_path) if isinstance(e, dict)] if feed_path.is_file() else []
  if feed:  # the session that made the newest AHC call; another session in the folder does not take over
    ahc_calls = [e for e in feed if e.get("ahc") and e.get("kind") in ("doing", "denied")]
    session = (ahc_calls[-1] if ahc_calls else feed[-1]).get("session")
    feed = [e for e in feed if e.get("session") == session]
  said = [e for e in feed if e.get("kind") in ("said", "finished")]
  stream = sorted([{"kind": e["kind"], "t": e.get("t"), "text": e.get("text")} for e in said + [
                     e for e in feed if e.get("kind") == "request"]]
                  + [{"kind": "decision", "t": d.get("t"), "text": d.get("decision"), "why": d.get("why"),
                      "waiting_for": d.get("waiting_for")} for d in decisions], key=lambda e: e.get("t") or "")
  doing = [e for e in feed if e.get("kind") in ("doing", "denied")]
  last_doing = doing[-1] if doing else None
  now = next((e for e in reversed(stream) if e["kind"] != "request"), None)
  finished = bool(now and now["kind"] == "finished" and (not last_doing or (last_doing.get("t") or "") <= (now.get("t") or "")))
  requests = [e for e in stream if e["kind"] == "request"]
  plan = progress.public() or {}
  layout_waits = bool(gate and gate.get("needs_person") and gate.get("state") == "pending")
  checks = checks or []
  last = decisions[-1] if decisions else None
  asked = bool(last and last.get("waiting_for") and not (last_doing and (last_doing.get("t") or "") > (last.get("t") or "")))
  if layout_waits or checks or asked:
    state = "waiting"
  elif plan.get("done"):
    state = "finished"
  elif finished:
    state = "ended"
  elif feed or plan:
    state = "working"
  else:
    state = "idle"
  return {"current": last, "plan": plan.get("steps"), "steps": plan.get("status"), "current_step": plan.get("current_step"),
          "plan_at": plan.get("at"), "plan_done": bool(plan.get("done")), "state": state, "layout_waits": layout_waits,
          "checks_waiting": checks, "history": decisions[-8:], "request": requests[-1] if requests else None, "now": now,
          "doing": {**{k: last_doing.get(k) for k in ("t", "tool", "brief", "ahc")},
                    "denied": last_doing.get("kind") == "denied"} if last_doing else None,
          "finished": finished, "live": bool(feed), "stream": stream[-14:]}


def workspace_config(path: Path) -> dict[str, Any] | None:
  """The task's config as the server would read it; a broken file shows as invalid, never as a crash."""
  from ahc.core.errors import LabError
  from ahc.workspace.config import parse_config

  if not path.is_file():
    return None
  try:
    config = parse_config(path.read_text())
  except LabError as exc:
    return {"status": "invalid", "reason": exc.message, "path": str(path)}
  status, why = config.status(forced_sim=False)
  out = {"status": status, "reason": why, "path": str(path),
         "devices": [{"id": d.id, "model": d.model, "backend": d.backend} for d in config.devices],
         "limits": config.limits, "confirmation": config.confirmation, "sim": config.sim}
  if config.auto_verify:
    out["auto_verify"] = config.auto_verify.data()
  if config.cameras:
    out["cameras"] = [{"name": c.name, "device": c.device, "sees": c.sees(),
                       "slot_map": {k: c.slot_map[k] for k in ("size", "view", "slots")} if c.slot_map else None}
                      for c in config.cameras]
    seen = _json(path.parent / "state" / "physical_world.json")
    if seen and any(c.slot_map for c in config.cameras):
      out["physical_world"] = {k: seen.get(k) for k in ("checked_at", "vision_context", "instrument_context", "points",
                                                       "physical_world_confidence", "suggestions")}
  return out


def _json(path: Path) -> dict[str, Any] | None:
  try:
    data = json.loads(path.read_text())
  except (OSError, ValueError):
    return None
  return data if isinstance(data, dict) else None


def reference_image(ws: Workspace, name: str) -> bytes | None:
  """A camera's reference image, for the page; only a camera the config names, only its own file."""
  from ahc.workspace.config import parse_config

  try:
    config = parse_config(ws.config_path.read_text())
    camera = next(c for c in config.cameras if c.name == name and c.slot_map)
    return (ws.dir / camera.slot_map["reference"]).read_bytes()
  except (OSError, LabError, StopIteration):
    return None


def run_image(ws: Workspace, relative: str) -> bytes | None:
  """A capture or crop of a run, for the review list: only a .jpg file under .ahc/runs."""
  try:
    path = (ws.runs_dir / urllib.parse.unquote(relative)).resolve()
    if path.suffix != ".jpg" or not path.is_file() or not path.is_relative_to(ws.runs_dir.resolve()):
      return None
    return path.read_bytes()
  except (OSError, ValueError):
    return None


def save_slots(ws: Workspace, name: str, slots: Any) -> tuple[int, dict[str, Any]]:
  """The person's dragged boxes into the config: the camera's own slots, moved or resized, none added or dropped.
  The config's digest changes, so it is pending until they confirm it again."""
  from ahc.workspace.config import HEADER, parse_config, parse_slot_map

  try:
    config = parse_config(ws.config_path.read_text())
  except (OSError, LabError) as exc:
    return 409, {"error": f"the config cannot be read: {getattr(exc, 'message', exc)}"}
  camera = next((c for c in config.cameras if c.name == name and c.slot_map), None)
  if camera is None:
    return 404, {"error": f"no camera {name!r} with a slot map"}
  if not isinstance(slots, dict) or set(slots) != set(camera.slot_map["slots"]):
    return 400, {"error": "save the boxes of the slots the map has, no more and no fewer"}
  try:
    new = parse_slot_map({**camera.slot_map, "slots": slots}, name)
  except LabError as exc:
    return 400, {"error": exc.message}
  if new == camera.slot_map:
    return 200, {"saved": False, "reason": "no box changed", "status": config.status(False)[0]}
  cameras = [replace(c, slot_map=new) if c.name == name else c for c in config.cameras]
  config = replace(config, cameras=cameras)
  ws.write_config(config.data(), HEADER)
  status, why = config.status(False)
  return 200, {"saved": True, "status": status, "reason": why}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="ahc-token" content="__AHC_TOKEN__"><meta name="ahc-view" content="__AHC_VIEW__">
<title>AHC · __AHC_FOLDER__</title>
<style>
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--blank:#c3c2b7;--good:#0ca30c;--warning:#fab219;
--critical:#d03b3b;--chip:#f0efec;--meter-track:#cde2fb;--meter:#2a78d6}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;
--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--blank:#52514e;--chip:#2c2c2a;--meter-track:#184f95;--meter:#86b6ef}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;
--muted:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--blank:#52514e;--chip:#2c2c2a;
--meter-track:#184f95;--meter:#86b6ef}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;padding:16px 24px 8px}
h1{font-size:18px;margin:0;font-weight:600}.sub{color:var(--ink2)}.live{margin-left:auto;color:var(--ink2)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--muted);margin-right:6px}
.dot.on{background:var(--good)}.chip{display:inline-block;padding:1px 8px;border-radius:10px;background:var(--chip);
color:var(--ink2);font-size:12px}
main{padding:8px 24px 24px;display:grid;gap:16px;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr)}
@media (max-width:900px){main{grid-template-columns:1fr}}
.kpis{grid-column:1/-1;display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(130px,1fr))}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.tile .label{color:var(--ink2);font-size:12px}.tile .value{font-size:28px;font-weight:600;margin-top:2px}
.tile .hint{color:var(--muted);font-size:12px}
h2{font-size:14px;margin:0 0 10px;font-weight:600}h2 .sub{font-weight:400}
.col{display:flex;flex-direction:column;gap:16px;min-width:0}
svg text{fill:var(--muted);font-size:11px}
.legend{display:flex;align-items:center;gap:6px;flex-wrap:wrap;color:var(--ink2);font-size:12px;margin-top:8px}
.legend .ramp{display:flex}.legend .ramp span{width:18px;height:10px}
.legend .sw{width:12px;height:12px;border-radius:3px;display:inline-block;vertical-align:-2px}
.legend .item{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;margin-right:10px}
.tablewrap{overflow-x:auto}.platetable td,.platetable th{text-align:center;padding:4px 5px}
.platetable th{color:var(--muted);font-weight:500}.platetable .vol{color:var(--muted);font-size:11px}
.row{display:flex;align-items:center;gap:10px;margin:6px 0}.row .name{width:110px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}.meter{flex:1;height:8px;border-radius:4px;background:var(--meter-track);overflow:hidden}
.meter i{display:block;height:100%;background:var(--meter);border-radius:4px}
@media (max-width:600px){.row{flex-wrap:wrap;gap:4px 10px}.row .meter{flex-basis:100%;order:3}.row .num{min-width:0;margin-left:auto}}.row .num{color:var(--ink2);
font-variant-numeric:tabular-nums;font-size:12px;min-width:150px;text-align:right}
#timeline{max-height:780px;overflow:auto;margin:0;padding:0;list-style:none}
#timeline li{padding:7px 0 7px 10px;border-bottom:1px solid var(--grid);border-left:3px solid transparent}
#timeline li.refused{border-left-color:var(--critical)}#timeline li.newest{background:var(--chip)}
.st{font-weight:600;margin-right:6px}.st.ok{color:var(--ink)}.st.ok::before{content:"✓ ";color:var(--good)}
.st.refused::before{content:"✕ ";color:var(--critical)}.st.pass::before{content:"✓ ";color:var(--good)}
.st.fail::before{content:"✕ ";color:var(--critical)}.st.pending::before{content:"◷ ";color:var(--warning)}
.st.passed::before{content:"✓ ";color:var(--good)}.st.failed::before{content:"✕ ";color:var(--critical)}
.st.audit::before{content:"⚠ ";color:var(--warning)}.st.superseded{color:var(--muted)}.st.superseded::before{content:"↷ "}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}.dev{color:var(--muted);font-size:12px}
.det{color:var(--ink2);font-size:12px;margin-top:2px}.msg{color:var(--ink2);font-size:12px;margin-top:3px}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:8px;background:var(--chip);color:var(--ink2);margin-left:6px}
.tag.tr::before{content:"↻ ";color:var(--warning)}
.racks{display:flex;gap:18px;flex-wrap:wrap}.racks svg circle.free{fill:var(--axis)}
.racks svg circle.used{fill:none;stroke:var(--axis);stroke-width:1}
.racks svg circle.returned{fill:none;stroke:var(--ink2);stroke-width:1;stroke-dasharray:1.2 1}
#tip{position:fixed;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;
padding:6px 9px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none}#tip b{font-size:14px}
button{font:inherit;font-size:12px;background:var(--chip);color:var(--ink2);border:1px solid var(--border);
border-radius:6px;padding:2px 8px;cursor:pointer;float:right}
table{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}
td,th{padding:3px 6px;border-bottom:1px solid var(--grid);text-align:right}th:first-child,td:first-child{text-align:left}
.empty{color:var(--muted);padding:40px 0;text-align:center}
.stopbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:12px;padding:10px 24px;
background:var(--page);border-bottom:1px solid var(--border);flex-wrap:wrap}
.stopbtn{float:none;font-size:15px;font-weight:700;letter-spacing:.04em;padding:10px 18px;border-radius:8px;
background:var(--critical);color:#fff;border:2px solid var(--critical);cursor:pointer}
.stopbtn:hover{filter:brightness(1.08)}.stopbtn::before{content:"■ "}
.stopbar .state{color:var(--ink2);font-size:13px}
.stopbar.engaged{background:var(--critical);color:#fff}.stopbar.engaged .state{color:#fff;font-weight:600;font-size:15px}
.stopbar.engaged .stopbtn{display:none}
.releasebtn{float:none;font-size:13px;padding:6px 12px;border-radius:8px;background:#fff;color:var(--critical);
border:2px solid #fff;cursor:pointer;display:none}.stopbar.engaged .releasebtn{display:inline-block}
.stopbar .folder{margin-left:auto;color:var(--ink2);font-size:12px;overflow-wrap:anywhere}.stopbar.engaged .folder{color:#fff}
.stopbar .servers{flex-basis:100%;font-size:12px}.stopbar .servers:empty{display:none}
.stopbar .servers div{padding:1px 0;overflow-wrap:anywhere}.stopbar.engaged .servers{color:#fff}
.stopbar .note{flex-basis:100%;font-weight:700;color:var(--critical)}.stopbar .note:empty{display:none}
.stopbar.engaged .note{color:#fff;text-decoration:underline}
.st.stop::before{content:"■ ";color:var(--critical)}.st.released::before{content:"✓ ";color:var(--good)}
.agent{grid-column:1/-1}.agent .label{color:var(--ink2);font-size:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.agent .decision{font-size:20px;font-weight:600;margin:6px 0 2px}.agent .why{color:var(--ink2)}
.plan{list-style:none;margin:12px 0 0;padding:0;display:grid;gap:3px;font-size:13px}
.plan li{display:flex;gap:8px;color:var(--muted)}.plan li::before{width:14px;flex:none}
.plan li.done::before{content:"✓"}.plan li.now{color:var(--ink);font-weight:600}.plan li.now::before{content:"▶"}
.plan li.next{color:var(--ink2)}.plan li.next::before{content:"○"}
.plan li.skipped{color:var(--muted)}.plan li.skipped::before{content:"–"}
.banner{margin-top:10px;padding:8px 12px;border-left:3px solid var(--warning);background:var(--chip);font-size:13px}
.banner::before{content:"◷ ";color:var(--warning)}.banner a{color:inherit}
.history{margin-top:10px;font-size:12px;color:var(--muted)}.history div{margin-top:2px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;background:var(--chip);color:var(--ink2)}
.badge.wait::before{content:"◷ ";color:var(--warning)}.badge.working::before{content:"● ";color:var(--good)}
.badge.done::before{content:"✓ ";color:var(--good)}.badge.ended::before{content:"■ ";color:var(--muted)}
.agent .request{margin-top:6px;color:var(--ink2);font-size:13px}.agent .request .k,.agent .calling .k{color:var(--muted)}
.agent .calling{margin-top:6px;font-size:13px}.agent .calling.denied .k{color:var(--critical);font-weight:600}
.plan .dur{color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}.plan li.now .dur{color:var(--ink)}
.timesum{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.timesum .chip2{display:flex;gap:6px;align-items:baseline;background:var(--page);border:1px solid var(--border);
border-radius:8px;padding:6px 10px;font-size:12px}.timesum .k{color:var(--muted)}.timesum .v{color:var(--ink);font-variant-numeric:tabular-nums}
.timesum .fold{background:var(--page);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:12px}
.timesum .fold summary{cursor:pointer;list-style:none}.timesum .fold summary::-webkit-details-marker{display:none}
.timesum .fold summary::before{content:"▸ ";color:var(--muted)}.timesum .fold[open] summary::before{content:"▾ "}
.timesum .fold .kv{margin:8px 0 2px}
.clock{font-variant-numeric:tabular-nums}
.agent .more{white-space:pre-line;color:var(--ink2);font-size:13px;line-height:1.5;max-height:12em;overflow:auto;margin-top:2px}
.status{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:8px}
.status .chip2{display:flex;gap:6px;align-items:baseline;background:var(--surface);border:1px solid var(--border);
border-radius:8px;padding:6px 10px;font-size:12px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.status .k{color:var(--muted)}.status .v{color:var(--ink)}
.status .gate-passed .v::before{content:"✓ ";color:var(--good)}.status .gate-pending .v::before{content:"◷ ";color:var(--warning)}
.status .gate-failed .v::before{content:"✕ ";color:var(--critical)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:12px;margin:4px 0 10px}.kv .k{color:var(--muted)}
h3{font-size:12px;font-weight:600;color:var(--ink2);margin:12px 0 4px}
#configcard td,#configcard th{text-align:left}
#cameracard td,#cameracard th{text-align:left}#cameracard svg{display:block;border-radius:6px;background:var(--grid)}
#cameracard svg text{fill:#fff;stroke:#000;stroke-width:3px;paint-order:stroke;font-size:15px;pointer-events:none}
#cameracard .handle{fill:#fff;stroke:#0b0b0b;stroke-width:2;cursor:nwse-resize}#cameracard .box{fill:rgba(0,200,0,.10);stroke:#0ca30c;stroke-width:2;cursor:move}
#cameracard .box.tips{stroke:#fab219;fill:rgba(250,178,25,.12)}#cameracard .camhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:12px 0 6px}
#cameracard .camhead button{float:none}#cameracard .camnote{color:var(--ink2);font-size:12px}
#autocard .autorow{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin:6px 0}#autocard .camnote{color:var(--ink2);font-size:12px}
#autocard .autobadge{font-size:11px;font-weight:700;letter-spacing:.04em;padding:1px 8px;border-radius:10px;background:var(--chip);color:var(--ink2)}
#autocard .autobadge.auto{background:var(--good);color:#fff}#autocard .review{border-top:1px solid var(--grid);padding:8px 0}
#autocard .thumbs{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0}#autocard .thumbs img{height:64px;border-radius:4px;border:1px solid var(--border);display:block}
</style></head><body>
<div class="stopbar" id="stopbar">__AHC_STOPBTN__
<span class="state" id="stopstate">__AHC_IDLE__</span>
__AHC_RELEASEBTN__
<span class="folder" title="The task folder this page shows">Task folder <code>__AHC_FOLDER__</code></span>
<span class="note" id="stopnote"></span><div class="servers" id="stopservers"></div></div>
<header><h1>AHC · live run</h1><span class="sub" id="sub">waiting for a run log…</span>
<span class="live"><span class="dot" id="dot"></span><span id="livetext">connecting</span></span></header>
<main>
<section class="card agent" id="agent"></section>
<section class="status" id="status"></section>
<section class="kpis" id="kpis"></section>
<div class="col"><section class="card" id="platecard"><div class="empty">No layout loaded yet.</div></section>
<section class="card"><h2>Reservoirs</h2><div id="containers"></div></section>
<section class="card"><h2>Tips <span class="sub">· hollow = used, dashed = returned after use</span></h2><div class="racks" id="racks"></div></section>
<section class="card" id="configcard"></section>
<section class="card" id="autocard" style="display:none"></section>
<section class="card" id="cameracard" style="display:none"></section></div>
<div class="col"><section class="card"><h2>Hardware calls <span class="sub">· newest first</span></h2><ul id="timeline"></ul></section></div>
</main><div id="tip"></div>
<script>
// Blue ramp, steps 100..700. Sequential: light mode runs light->dark with concentration; dark mode
// flips the anchor so low concentration recedes toward the dark surface.
const RAMP=["#cde2fb","#b7d3f6","#9ec5f4","#86b6ef","#6da7ec","#5598e7","#3987e5","#2a78d6","#256abf","#1c5cab","#184f95","#104281","#0d366b"];
const dark=()=>getComputedStyle(document.documentElement).colorScheme==="dark";
let LO=0.01; // the ramp spans [LO, 1] on a log scale; LO drops below 0.01 only for a more dilute plate
function seq(c){
  const t=LO<1?Math.min(Math.max(Math.log(c)/Math.log(LO),0),1):0; // 0 = stock, 1 = most dilute
  const i=1+Math.round((1-t)*11); // steps 150..700; step 100 is too close to the surface for a well
  return dark()?RAMP[13-i]:RAMP[i];
}
const frac=c=>c>=0.9995?"1":c<=0?"0":c>=0.01?c.toFixed(3):c.toExponential(2);
const el=(t,a={},txt)=>{const e=document.createElement(t);for(const[k,v]of Object.entries(a))e.setAttribute(k,v);if(txt!==undefined)e.textContent=txt;return e};
const sv=(t,a={})=>{const e=document.createElementNS("http://www.w3.org/2000/svg",t);for(const[k,v]of Object.entries(a))e.setAttribute(k,v);return e};
const fmt=n=>Number(n).toLocaleString(undefined,{maximumFractionDigits:1});
const tip=document.getElementById("tip");
function showTip(ev,lines){tip.replaceChildren();lines.forEach((l,i)=>{const d=el(i?"div":"b",{},l);tip.append(d)});
  tip.style.display="block";tip.style.left=(ev.clientX+14)+"px";tip.style.top=(ev.clientY+14)+"px"}
const hideTip=()=>tip.style.display="none";
let showTable=false;

function kpis(s){const box=document.getElementById("kpis");box.replaceChildren();
  [["Capability calls",s.calls,"liquid handling, accepted"],["Basic parameters only",s.basic_only,"no device-specific input"],
   ["Translated",s.translated,"general op rewritten for the device"],["Specialized",s.specialized,"used device-only parameters"],
   ["Refused",s.refused,Object.entries(s.refused_codes).map(([k,v])=>`${k} ×${v}`).join(", ")||"nothing refused"],
   ["Commands sent",fmt(s.commands),"reached the device or simulator"]].forEach(([l,v,h])=>{
    const t=el("div",{class:"card tile"});t.append(el("div",{class:"label"},l),el("div",{class:"value"},String(v)),el("div",{class:"hint"},h));box.append(t)})}

function plate(p){const card=document.getElementById("platecard");card.replaceChildren();
  const h=el("h2",{},p.name);h.append(el("span",{class:"sub"}," · relative concentration (stock = 1)"));
  const b=el("button",{},showTable?"Heatmap":"Table");b.onclick=()=>{showTable=!showTable;render(last)};h.append(b);card.append(h);
  if(showTable){const wrap=el("div",{class:"tablewrap"}),tb=el("table",{class:"platetable"}),hr=el("tr");hr.append(el("th"));
    for(let c=1;c<=p.cols;c++)hr.append(el("th",{},String(c)));tb.append(hr);
    "ABCDEFGH".slice(0,p.rows).split("").forEach(r=>{const tr=el("tr");tr.append(el("th",{},r));
      for(let c=1;c<=p.cols;c++){const d=p.wells[r+c],td=el("td");
        if(d.v>0){td.append(el("div",{},frac(d.c)),el("div",{class:"vol"},fmt(d.v)+" uL"))}else td.append(el("div",{class:"vol"},"–"));tr.append(td)}
      tb.append(tr)});
    wrap.append(tb);card.append(wrap,el("div",{class:"legend"},"Relative concentration (stock = 1), volume below; – = empty."));return}
  // Drawn in CSS pixels (no viewBox scaling) so labels stay at text size; redrawn on resize.
  const gap=2,L=22,T=18,cell=Math.max(16,Math.min(52,Math.floor((card.clientWidth-34-L)/p.cols)-gap));
  const W=L+p.cols*(cell+gap),H=T+p.rows*(cell+gap);
  const s=sv("svg",{width:W,height:H,role:"img","aria-label":`${p.name} heatmap`});
  for(let c=1;c<=p.cols;c++){const t=sv("text",{x:L+(c-1)*(cell+gap)+cell/2,y:12,"text-anchor":"middle"});t.textContent=c;s.append(t)}
  "ABCDEFGH".slice(0,p.rows).split("").forEach((r,ri)=>{const t=sv("text",{x:8,y:T+ri*(cell+gap)+cell/2+4,"text-anchor":"middle"});t.textContent=r;s.append(t);
    for(let c=1;c<=p.cols;c++){const d=p.wells[r+c],x=L+(c-1)*(cell+gap),y=T+ri*(cell+gap);
      const g=sv("g");g.append(sv("rect",{x,y,width:cell,height:cell,rx:4,fill:"var(--surface)",stroke:"var(--grid)","stroke-width":1}));
      if(d.v>0){const fh=Math.max(3,(cell-2)*Math.min(d.v/p.max,1));
        g.append(sv("rect",{x:x+1,y:y+cell-1-fh,width:cell-2,height:fh,rx:3,fill:d.c>0?seq(d.c):"var(--blank)"}))}
      const hit=sv("rect",{x,y,width:cell,height:cell,fill:"transparent"});
      hit.addEventListener("pointermove",ev=>showTip(ev,[d.v>0?(d.c>0?`concentration ${frac(d.c)}`:"diluent only (0)"):"empty",`${r}${c} · ${fmt(d.v)} uL`]));
      hit.addEventListener("pointerleave",hideTip);g.append(hit);s.append(g)}});
  card.append(s);
  const lg=el("div",{class:"legend"}),item=()=>el("span",{class:"item"});
  const scale=item(),ramp=el("span",{class:"ramp"});
  for(let k=0;k<=6;k++){const sp=el("span");sp.style.background=seq(Math.pow(LO,k/6));ramp.append(sp)}
  scale.append(el("span",{},"1 (stock)"),ramp,el("span",{},frac(LO)+" (log scale)"));
  const dil=item(),bl=el("span",{class:"sw"});bl.style.background="var(--blank)";dil.append(bl,el("span",{},"diluent only"));
  const emp=item(),em=el("span",{class:"sw"});em.style.border="1px solid var(--grid)";emp.append(em,el("span",{},"empty"));
  lg.append(scale,dil,emp,el("span",{class:"item"},"fill height = volume"));card.append(lg)}

function reservoirs(cs){const box=document.getElementById("containers");box.replaceChildren();
  if(!cs.length){box.append(el("div",{class:"dev"},"none in this layout"));return}
  cs.forEach(c=>{const r=el("div",{class:"row"});const n=el("span",{class:"name",title:c.name},c.alias||c.name);
    const m=el("span",{class:"meter"});const i=el("i");i.style.width=(100*Math.min(c.v/c.max,1)).toFixed(1)+"%";m.append(i);
    r.append(n,m,el("span",{class:"num"},`${fmt(c.v)} / ${fmt(c.max)} uL${c.c>0?" · conc "+frac(c.c):""}`));box.append(r)})}

function racks(rs){const box=document.getElementById("racks");box.replaceChildren();
  rs.forEach(r=>{const used=new Set(r.used),back=new Set(r.returned||[]),d=8,W=r.cols*d+4,H=r.rows*d+4;const w=el("div");
    w.append(el("div",{class:"dev"},`${r.name} · ${r.rows*r.cols-used.size} / ${r.rows*r.cols} left`+(back.size?` · ${back.size} returned`:"")));
    const s=sv("svg",{width:W*1.5,height:H*1.5,viewBox:`0 0 ${W} ${H}`});
    for(let c=1;c<=r.cols;c++)"ABCDEFGH".slice(0,r.rows).split("").forEach((row,ri)=>{
      s.append(sv("circle",{cx:2+(c-1)*d+d/2,cy:2+ri*d+d/2,r:2.6,class:back.has(row+c)?"returned":used.has(row+c)?"used":"free"}))});
    w.append(s);box.append(w)})}

function timeline(evs){const ul=document.getElementById("timeline");ul.replaceChildren();
  [...evs].reverse().forEach((e,k)=>{const li=el("li",{class:(e.status==="refused"?"refused ":"")+(k===0?"newest":"")});
    const st=["ok","refused","pass","fail","pending","passed","failed","audit","stop","released","superseded"].includes(e.status)?e.status:"ok";
    const top=el("div");top.append(el("span",{class:`st ${st}`},st),el("code",{},e.op));if(e.device)top.append(el("span",{class:"dev"},"  "+e.device));
    if(e.translated)top.append(el("span",{class:"tag tr"},"translated"));
    if(e.specialized&&e.specialized.length)top.append(el("span",{class:"tag"},"specialized: "+e.specialized.join(", ")));
    if(e.commands)top.append(el("span",{class:"tag"},`${e.commands} cmds`));
    if(e.unknown)top.append(el("span",{class:"tag"},"outcome on the robot unknown: check tips and wells"));li.append(top);
    if(e.detail)li.append(el("div",{class:"det"},e.detail));
    if(e.status==="refused"&&e.message)li.append(el("div",{class:"msg"},e.message.replace(/^Error executing tool \w+: /,"")));
    else if(e.note)li.append(el("div",{class:"msg"},e.note));ul.append(li)})}

function agentPanel(a,tm){const box=document.getElementById("agent");box.replaceChildren();
  const head=el("div",{class:"label"});head.append(el("span",{},"Agent"));box.append(head);
  const nothing=!a||(!a.now&&!a.current&&!a.request&&!a.doing&&!(a.checks_waiting||[]).length);
  if(nothing){box.append(el("div",{class:"why"},"Nothing from the agent yet. In Claude Code the plugin shows what it is asked, says and calls as it happens; any agent can report decisions with report_decision."));return}
  const badge={working:["working","working"],waiting:["wait","waiting for you"],finished:["done","finished"],ended:["ended","turn ended"],idle:["","idle"]}[a.state]||["","idle"];
  head.append(el("span",{class:"badge "+badge[0]},badge[1]));
  const latest=[a.now&&a.now.t,a.doing&&a.doing.t].filter(Boolean).sort().at(-1);if(latest)head.append(el("span",{class:"sub"},"updated "+latest.slice(11)));
  if(a.request){const r=el("div",{class:"request"});r.append(el("span",{class:"k"},"Asked "),el("span",{},a.request.text));box.append(r)}
  const now=a.now;
  if(now){const lines=plain(now.text).split("\n");box.append(el("div",{class:"decision"},lines[0]||""));
    if(lines.length>1)box.append(el("div",{class:"more"},lines.slice(1).join("\n")));
    if(now.why)box.append(el("div",{class:"why"},"Why: "+now.why));
    if(now.waiting_for)box.append(el("span",{class:"badge wait"},"waiting for the "+now.waiting_for))}
  if(a.doing&&!a.finished&&a.state!=="finished"){const c=el("div",{class:"calling"+(a.doing.denied?" denied":"")});
    c.append(el("span",{class:"k"},a.doing.denied?"Refused by the emergency stop ":"Calling "),el("code",{},a.doing.tool),
    el("span",{class:"dev"},a.doing.brief?"  "+a.doing.brief:""),el("span",{class:"dev"},"  "+(a.doing.t||"").slice(11)));box.append(c)}
  if(a.layout_waits){const b=el("div",{class:"banner"});b.append(el("span",{},"The agent proposes a layout no reference in this folder matches. Check its labware, positions and liquids in "),
    el("a",{href:"#layoutsec"},"Configuration › Layout"),el("span",{},", then tell the agent to go on, or what to change. Nothing moves until you do."));box.append(b)}
  // Every check that waits for the person's verdict (on real hardware all of them), from the run log.
  (a.checks_waiting||[]).filter(c=>!(a.layout_waits&&c.check==="deck_matches_layout")).forEach(c=>{const b=el("div",{class:"banner"});
    b.append(el("span",{},`Waiting for your check: ${c.what} (${c.check}, asked ${(c.t||"").slice(11)}). Look, then answer where you were asked (the agent, or the ahc-smoke terminal)`
      +(c.check==="deck_matches_layout"?"; nothing moves until you do.":".")
      +(c.said?` The agent looked at the camera images${c.said.verdict?" and judged "+c.said.verdict:""}${c.said.note?" ("+c.said.note+")":""}; auto-verify handed this check to you${c.said.why?": "+c.said.why:""}.`:"")));box.append(b)})
  if(a.plan&&a.plan.length){const ol=el("ol",{class:"plan"}),n=a.plan.length,st=a.steps||[];
    // Each call names its step: a step is ticked once the agent moves on, and the plan ends when it reports it done.
    const where=a.plan_done?"finished":a.current_step?`step ${a.current_step} of ${n}`:`${n} steps, not started`;
    const h=el("h3",{},`Plan · ${where}`+(a.plan_at?` · reported ${a.plan_at.slice(11)}`:""));
    if(tm&&tm.run){h.append(" · run ",el("span",{class:"clock","data-clock":"run"}))}
    box.append(h);
    const byStep=Object.fromEntries(((tm&&tm.steps)||[]).map(x=>[x.step,x]));
    a.plan.forEach((step,k)=>{const s=st[k]||"next",li=el("li",{class:s},`${k+1}. ${step}`+(s==="skipped"?" · skipped":""));
      // elapsed on the step in progress (ticking), the time first call to last call on a step that is past
      if(byStep[k+1]&&s==="now")li.append(el("span",{class:"dur","data-clock":"step:"+(k+1)}));
      else if(byStep[k+1]&&s==="done")li.append(el("span",{class:"dur",title:"first to last call of this step"},dur(byStep[k+1].duration_s)));
      ol.append(li)});box.append(ol);
    if(a.plan_done&&tm&&tm.run&&tm.split){const sp=tm.split,tot=el("div",{class:"timesum"});
      const chip=(k,v,title)=>{const c=el("span",{class:"chip2",title:title||""});c.append(el("span",{class:"k"},k),el("span",{class:"v"},v));tot.append(c)};
      chip("Total",dur(tm.run.total_s),tm.run.start.replace("T"," ")+" to "+tm.run.end.slice(11));
      chip("Robot",sp.robot_s===null?"not measured":dur(sp.robot_s)+(sp.robot_measured?"":" (some calls not timed)"),"Motion calls running on the device (measured)");
      chip("Agent","~"+dur(sp.agent_s),"Estimate: the rest of the run (thinking and tool overhead)"+(sp.robot_s===null?"; includes the robot's time, which this log did not measure":""));
      // Others: neither robot nor agent. A fold, closed until opened (it stays as left when the page redraws).
      const fold=el("details",{class:"fold"}),sum=el("summary",{title:"Estimate: everything that is neither robot nor agent"});
      sum.append(el("span",{class:"k"},"Others "),el("span",{class:"v"},"~"+dur(sp.others_s)));fold.append(sum);
      const kv=el("div",{class:"kv"});
      [["person_checks_s","Your checks, pending until your verdict"],["go_ahead_s","Go-ahead and config wait"],
       ["estop_engaged_s","Emergency stop engaged"],["after_release_s","Recovery after a stop release"]].forEach(([k,label])=>
        kv.append(el("span",{class:"k"},label),el("span",{},"~"+dur(sp.others[k]))));
      fold.append(kv);fold.open=othersOpen;fold.addEventListener("toggle",()=>{othersOpen=fold.open});tot.append(fold);
      box.append(tot)}}
  const feed=(a.stream||[]).filter(e=>e.kind!=="request").slice(0,-1).reverse().slice(0,6);
  if(feed.length){const h=el("div",{class:"history"});h.append(el("div",{},"Earlier"));
    const label={said:"said",decision:"decided",finished:"finished"};
    feed.forEach(e=>h.append(el("div",{},`${(e.t||"").slice(11)}  ${label[e.kind]||e.kind}: ${firstLine(plain(e.text))}`)));box.append(h)}}
function firstLine(t){t=String(t||"").trim();const i=t.indexOf("\n");return(i>0?t.slice(0,i):t).slice(0,240)}
// Agent text is markdown; shown as plain text (never as HTML): no heading marks, emphasis, code ticks or table rules.
function plain(t){return String(t||"").split("\n").map(l=>l.trim()).filter(l=>l&&!/^\|?[\s:|-]+\|?$/.test(l))
  .map(l=>(l.startsWith("|")?l.replace(/^\||\|$/g,"").split("|").map(c=>c.trim()).join(" · "):l)
    .replace(/^#{1,6}\s+/,"").replace(/^[-*+]\s+/,"• ").replace(/\*\*|__|`/g,"")).join("\n")}

function statusRow(st){const box=document.getElementById("status");box.replaceChildren();
  const chip=(k,v,title,cls)=>{const c=el("span",{class:"chip2 "+(cls||""),title:title||""});c.append(el("span",{class:"k"},k),el("span",{class:"v"},v));return c};
  const ws=st.workspace||"";box.append(chip("Task folder",ws.split("/").pop()||"–",ws));
  const cfg=st.config;let cv="none yet";
  if(cfg)cv=cfg.status==="invalid"?"invalid":`${(cfg.devices||[]).map(d=>d.model+" · "+d.backend).join(", ")||"no device"} · ${cfg.status}`+(cfg.status==="confirmed"&&cfg.confirmation?" by "+cfg.confirmation.by:"");
  box.append(chip("Config",cv,cfg?cfg.reason:""));
  const L=st.layout;box.append(chip("Layout",L?(L.name||"(unsaved)")+(L.needs_person&&!L.confirmed_by?" · not confirmed yet":""):"none loaded"));
  const g=st.gate;
  if(st.estop)box.append(chip("Motion gate","blocked · emergency stop",`pressed ${(st.estop.at||"").replace("T"," ")} (${st.estop.by})`,"gate-failed"));
  else if(g&&g.needs_person&&g.state==="pending")box.append(chip("Motion gate","pending · waiting for you to confirm the layout",g.reason,"gate-pending"));
  else if(g&&g.state==="pending"&&((st.agent||{}).checks_waiting||[]).some(c=>c.check==="deck_matches_layout"))box.append(chip("Motion gate","pending · waiting for your deck check",g.reason,"gate-pending"));
  else box.append(chip("Motion gate",g?g.state:"no layout",g?g.reason:"",g?"gate-"+g.state:""));
  const now=(st.events||[]).at(-1);box.append(chip("Last hardware event",now?`${now.op} ${now.detail||""}`.trim():"none yet",now?now.t:""))}

function where(layout,name){if(!layout)return"";
  for(const c of layout.carriers||[])for(const[k,v]of Object.entries(c.sites||{}))if(v.name===name)return`track ${c.track} · ${c.type} site ${k}`;
  for(const[k,v]of Object.entries(layout.slots||{}))if(v.name===name)return`slot ${k}`;return""}

function configCard(st){const card=document.getElementById("configcard");card.replaceChildren();card.append(el("h2",{},"Configuration"));
  const cfg=st.config;
  if(!cfg)card.append(el("div",{class:"dev"},"No .ahc/config.yaml in this task folder yet."));
  else if(cfg.status==="invalid")card.append(el("div",{class:"msg"},"config.yaml is invalid: "+cfg.reason));
  else{const kv=el("div",{class:"kv"});const row=(k,v)=>kv.append(el("span",{class:"k"},k),el("span",{},v));
    (cfg.devices||[]).forEach(d=>row("Device "+d.id,`${d.model} · ${d.backend}`));
    const lim=Object.entries(cfg.limits||{}).flatMap(([a,ps])=>Object.entries(ps).map(([p,b])=>`${a} ${p} ${Object.entries(b).map(([x,y])=>x+" "+y).join(", ")}`));
    row("Task limits",lim.length?lim.join("; "):"none (the device's own limits apply)");
    row("Confirmed",cfg.confirmation?`${cfg.status} · by ${cfg.confirmation.by} at ${(cfg.confirmation.at||"").replace("T"," ")}`:`${cfg.status} · ${cfg.reason}`);
    if(cfg.sim&&Object.keys(cfg.sim).length)row("Simulator",Object.entries(cfg.sim).map(([k,v])=>`${k} ${String(v).slice(0,7)}`).join(", "));
    card.append(kv)}
  const L=st.layout;if(!L)return;
  const how=L.confirmed_by?` · confirmed by ${L.confirmed_by}`:L.needs_person?" · waiting for your confirmation, saved once you confirm":"";
  card.append(el("h3",{id:"layoutsec"},`Layout ${L.name||""} · loaded ${(L.t||"").slice(11)}${how}`));
  const tb=el("table"),hr=el("tr");["Labware","Type","Where"].forEach(x=>hr.append(el("th",{},x)));tb.append(hr);
  Object.entries(L.labware||{}).forEach(([n,t])=>{const r=el("tr");r.append(el("td",{},n),el("td",{},t),el("td",{},where(L.layout,n)));tb.append(r)});
  const wrap=el("div",{class:"tablewrap"});wrap.append(tb);card.append(wrap);
  const liq=(L.layout||{}).liquids;if(liq&&Object.keys(liq).length)card.append(el("div",{class:"legend"},"Starting liquids: "+Object.entries(liq).map(([k,v])=>`${k} ${fmt(v)} uL`).join(", ")))}

// Cameras: the reference image with its boxes. The control page drags them (a corner resizes, the body moves) and saves;
// the config is then pending until the person confirms it again. Never rebuilt while a box is unsaved.
const camWork={};
function cameraCard(cfg){const card=document.getElementById("cameracard");
  const cams=((cfg||{}).cameras||[]).filter(c=>c.slot_map);
  if(!cams.length){card.style.display="none";card.replaceChildren();return}
  if(Object.values(camWork).some(w=>w.dirty))return;
  card.style.display="";card.replaceChildren(el("h2",{},"Cameras"));
  const vis=cfg.physical_world||{},seen=(vis.vision_context||{}).cameras||{};
  cams.forEach(c=>{const m=c.slot_map,[W,H]=m.size,work=JSON.parse(JSON.stringify(m.slots));camWork[c.name]={dirty:false};
    const head=el("div",{class:"camhead"});head.append(el("b",{},c.name),el("span",{class:"dev"},`${c.device} · sees ${c.sees}`));
    const cam=seen[c.name];if(cam&&cam.status&&cam.status!=="ok")head.append(el("span",{class:"tag"},cam.status.replace(/_/g," ")));
    const msg=el("span",{class:"camnote"}),save=el("button",{},"Save boxes"),undo=el("button",{},"Undo");save.disabled=undo.disabled=true;
    if(!VIEW)head.append(save,undo);head.append(msg);card.append(head);
    const pad=Math.round(Math.max(W,H)*0.12),svg=sv("svg",{viewBox:`${-pad} ${-pad} ${W+2*pad} ${H+2*pad}`,role:"img","aria-label":`${c.name} reference with slot boxes`});
    svg.style.width="100%";svg.style.maxHeight="540px";svg.style.touchAction="none";
    svg.append(sv("image",{href:`/camera/${encodeURIComponent(c.name)}/reference.jpg`,x:0,y:0,width:W,height:H}));
    svg.append(sv("rect",{x:0,y:0,width:W,height:H,fill:"none",stroke:"var(--axis)","stroke-width":1}));
    const r=Math.max(5,Math.round(W/110)),parts={},pt=ev=>{const p=svg.createSVGPoint();p.x=ev.clientX;p.y=ev.clientY;const q=p.matrixTransform(svg.getScreenCTM().inverse());return[q.x,q.y]};
    const draw=()=>Object.entries(work).forEach(([k,b])=>{const p=parts[k];p.rect.setAttribute("x",b[0]);p.rect.setAttribute("y",b[1]);
      p.rect.setAttribute("width",b[2]-b[0]);p.rect.setAttribute("height",b[3]-b[1]);p.label.setAttribute("x",Math.max(b[0],0)+6);p.label.setAttribute("y",Math.max(b[1],0)+20);
      p.handles.forEach((h,i)=>{h.setAttribute("cx",i%3?b[2]:b[0]);h.setAttribute("cy",i<2?b[1]:b[3])})});
    const touched=()=>{camWork[c.name].dirty=true;save.disabled=undo.disabled=false;msg.textContent="Unsaved: the config will wait for the person's confirmation.";draw()};
    Object.keys(work).forEach(k=>{const tips=k.startsWith("tips:"),g=sv("g"),rect=sv("rect",{class:"box"+(tips?" tips":"")}),
      label=sv("text",{});label.textContent=tips?k.slice(5):"slot "+k;g.append(rect,label);const handles=[];
      if(!VIEW){rect.addEventListener("pointerdown",ev=>{ev.preventDefault();rect.setPointerCapture(ev.pointerId);const o=pt(ev),b0=work[k].slice();
          rect.onpointermove=e=>{const q=pt(e);work[k]=[b0[0]+q[0]-o[0],b0[1]+q[1]-o[1],b0[2]+q[0]-o[0],b0[3]+q[1]-o[1]];touched()};
          rect.onpointerup=()=>{rect.onpointermove=rect.onpointerup=null}});
        [[0,1],[2,1],[2,3],[0,3]].forEach(([ix,iy],i)=>{const h=sv("circle",{class:"handle",r});handles.push(h);g.append(h);
          h.addEventListener("pointerdown",ev=>{ev.preventDefault();ev.stopPropagation();h.setPointerCapture(ev.pointerId);
            h.onpointermove=e=>{const q=pt(e),b=work[k].slice();b[ix]=q[0];b[iy]=q[1];
              if(b[2]-b[0]>=12&&b[3]-b[1]>=12){work[k]=b;touched()}};h.onpointerup=()=>{h.onpointermove=h.onpointerup=null}})})}
      parts[k]={rect,label,handles};svg.append(g)});
    draw();card.append(svg);
    undo.onclick=()=>{Object.keys(work).forEach(k=>work[k]=m.slots[k].slice());camWork[c.name].dirty=false;save.disabled=undo.disabled=true;msg.textContent="";draw()};
    save.onclick=async()=>{save.disabled=true;msg.textContent="Saving…";
      try{const r=await fetch(`/cameras/${encodeURIComponent(c.name)}/slots`,{method:"POST",headers:{"X-AHC-Control":"1","X-AHC-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify({slots:work})});
        const j=await r.json();if(!r.ok){msg.textContent=`Not saved (${r.status}): ${j.error||""}`;save.disabled=false;return}
        camWork[c.name].dirty=false;undo.disabled=true;
        msg.textContent=j.saved?"Saved. The config is pending until the person confirms it again (confirm_config).":"Nothing changed."}
      catch(e){msg.textContent="Not saved: the dashboard did not answer.";save.disabled=false}}});
  const pts=vis.points||[];card.append(el("h3",{},"Physical world context"+(vis.checked_at?` · checked ${vis.checked_at.slice(11,19)}`:"")));
  if(!pts.length){card.append(el("div",{class:"dev"},"Nothing checked yet: the agent runs physical_world_context once the plan is reported and a layout is loaded."));return}
  // The sources: what each observes and covers; then the conclusion per check type; then every point with the sources that cover it.
  const conf=vis.physical_world_confidence||{},bySource=conf.by_source||{},inst=vis.instrument_context||{};
  const srcs=el("div",{class:"legend"});
  Object.entries(bySource).forEach(([n,v])=>srcs.append(el("div",{},`${n}: covers ${v.covers} of ${v.observes} points it observes`)));
  srcs.append(el("div",{},`instrument:${inst.device||"?"}: ${inst.note||"no sensors"}`));card.append(srcs);
  const cls={covered:"pass",uncovered:"fail",unclear:"pending"};
  const checks=el("div",{class:"autorow"});
  Object.entries(conf.by_check||{}).forEach(([c,v])=>{const p=v.points||{};
    checks.append(el("span",{class:"st "+(cls[v.status]||"audit"),title:(v.sources||[]).map(x=>`${x.source} (${x.level})`).join(", ")},`${c.replace(/_/g," ")}: ${v.status}`),
      el("span",{class:"dev"},`${p.covered||0} covered, ${p.unclear||0} unclear, ${p.uncovered||0} uncovered`))});card.append(checks);
  const tb=el("table"),hr=el("tr");["Point","Check","Level","Status","Sources"].forEach(x=>hr.append(el("th",{},x)));tb.append(hr);
  pts.forEach(p=>{const row=el("tr"),st=el("td"),s=el("span",{class:"st "+(cls[p.status]||"audit"),title:p.why||""},p.status),
      cov=(p.sources||[]).filter(e=>e.covers).map(e=>`${e.source} (${e.level})`).join(", ");
    st.append(s);row.append(el("td",{title:p.why||""},p.what),el("td",{},p.check),el("td",{},p.level),st,el("td",{},cov||"–"));tb.append(row)});
  const wrap=el("div",{class:"tablewrap"});wrap.append(tb);card.append(wrap);
  if((vis.suggestions||[]).length)card.append(el("div",{class:"legend"},"To cover the rest: "+vis.suggestions.join("; ")))}

// Auto-verify: AUTO or why it is off per authorised check, the pause (control page only), and the agent's verdicts to review.
function autoCard(a){const card=document.getElementById("autocard");
  if(!a){card.style.display="none";card.replaceChildren();return}
  card.style.display="";const h=el("h2",{},"Auto-verify"+(a.paused?" · paused":"")),msg=el("span",{class:"camnote"});card.replaceChildren(h);
  if(!VIEW){const b=el("button",{id:"autopause"},a.paused?"Resume auto":"Pause auto");
    b.onclick=async()=>{b.disabled=true;
      try{const r=await fetch(a.paused?"/auto/resume":"/auto/pause",{method:"POST",headers:{"X-AHC-Control":"1","X-AHC-Token":TOKEN,"Content-Type":"application/json"},body:"{}"});
        if(!r.ok){let j={};try{j=await r.json()}catch(e){}msg.textContent=`Not changed (${r.status}): ${j.error||""}`;b.disabled=false}else tick.key=null}
      catch(e){msg.textContent="Not changed: the dashboard did not answer.";b.disabled=false}};h.append(b)}
  card.append(msg);
  if(a.run)card.append(el("div",{class:"autorow"},el("span",{class:"autobadge "+(a.run.state==="on"?"auto":"off")},a.run.state==="on"?"RUN ON":"RUN OFF"),
    el("b",{},"Run level"),el("span",{class:"dev"},a.run.state==="on"?"every point of the listed check types is confident":a.run.why)));
  Object.entries(a.checks||{}).forEach(([c,s])=>{const row=el("div",{class:"autorow"});
    row.append(el("span",{class:"autobadge "+s.state},s.state==="auto"?"AUTO":"OFF"),el("b",{},c),
      el("span",{class:"dev"},s.state==="auto"?`streak ${s.streak} of ${a.min_streak} agreements`:s.why));card.append(row)});
  card.append(el("h3",{},`Auto verdicts to review · ${a.review.length}`));
  if(!a.review.length)card.append(el("div",{class:"dev"},"None yet. Every check the agent passes by itself is listed here with its images."));
  a.review.forEach(r=>{const item=el("div",{class:"review"});
    item.append(el("div",{},`${(r.t||"").replace("T"," ")} · ${r.check}`+(r.note?` · ${r.note}`:"")));
    const row=el("div",{class:"thumbs"});
    (r.images||[]).forEach(p=>{const href="/run-image/"+p.split("/").map(encodeURIComponent).join("/"),link=el("a",{href,target:"_blank",rel:"noopener"});
      link.append(el("img",{src:href,loading:"lazy",alt:p.split("/").pop()}));row.append(link)});
    item.append(row);
    (r.per_point||[]).forEach(pp=>item.append(el("div",{class:"dev"},`${pp.what}: ${pp.verdict}`+(pp.note?` · ${pp.note}`:""))));card.append(item)})}

const TOKEN=document.querySelector('meta[name="ahc-token"]').content,VIEW=document.querySelector('meta[name="ahc-view"]').content==="1";
const ELSEWHERE="Use the robot's own stop or its power switch, or ahc-estop in the task folder.";
// A press that did not arrive is said at once and stays until a stop shows engaged; never silent.
function note(text){const n=document.getElementById("stopnote");n.textContent=text;clearTimeout(note.t);
  if(text&&!text.startsWith("STOP NOT SENT"))note.t=setTimeout(()=>{n.textContent=""},20000)}
async function control(path,body){const stop=path==="/estop";
  const headers={"X-AHC-Control":"1","Content-Type":"application/json"};if(!stop)headers["X-AHC-Token"]=TOKEN;
  let r;try{r=await fetch(path,{method:"POST",headers,body:JSON.stringify(body||{})})}
  catch(err){note(stop?"STOP NOT SENT: the dashboard did not answer. "+ELSEWHERE:"Release not sent: the dashboard did not answer.");return false}
  if(r.ok){note("");return true}
  let why="";try{why=(await r.json()).error||""}catch(err){}
  note(stop?`STOP NOT SENT (${r.status} ${why}). `+ELSEWHERE:`Not released (${r.status}): ${why}`);return false}
const stopBtn=document.getElementById("stopbtn"),releaseBtn=document.getElementById("releasebtn");  // absent on the view-only page
if(stopBtn)stopBtn.onclick=async()=>{await control("/estop",{reason:"pressed on the dashboard"});pollStop()};
if(releaseBtn)releaseBtn.onclick=async()=>{
  if(!confirm("Release the emergency stop?\n\nOnly when the deck is safe. The agent must check the deck again before anything moves."))return;
  await control("/estop/release",{confirm:true});pollStop()};
const IDLE=VIEW?"View only: the stop and its release are on the person's own page.":"Stops the hardware commands of this task folder at once; only a person releases it.";
async function pollStop(){const bar=document.getElementById("stopbar"),state=document.getElementById("stopstate"),list=document.getElementById("stopservers");
  try{const r=await fetch("/estop",{cache:"no-store"});const e=await r.json();
  if(e.engaged){bar.className="stopbar engaged";
    let text=`■ EMERGENCY STOP ENGAGED · pressed ${(e.at||"").replace("T"," ")} (${e.by})`;
    if(e.unreadable)text+=` · the stop file is not one AHC wrote (${e.unreadable}), so it counts as pressed`;
    // One line per AHC server that took the stop (its own acknowledgement), then what the person may rely on.
    text+=` · ${e.verdict?e.verdict.text:"no verdict"}`;
    state.textContent=text+" · nothing runs until a person releases it";
    list.replaceChildren(...(e.servers||[]).map(s=>el("div",{},s.line)));
    if(document.getElementById("stopnote").textContent.startsWith("STOP NOT SENT"))note("")}
  else{bar.className="stopbar";list.replaceChildren();state.textContent=e.released_at?`Released ${(e.released_at||"").replace("T"," ")} by ${e.released_by}; the deck must be checked again before anything moves.`:IDLE}}
  catch(err){state.textContent="The dashboard does not answer"+(VIEW?".":": this button cannot stop anything now. "+ELSEWHERE)}
  clearTimeout(pollStop.t);pollStop.t=setTimeout(pollStop,500)}
pollStop();

// Live clocks: the server says what time it was when it read the log (as_of); the page adds the time since.
const dur=s=>{s=Math.max(0,Math.round(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),r=s%60;
  return h?`${h}h ${String(m).padStart(2,"0")}m ${String(r).padStart(2,"0")}s`:m?`${m}m ${String(r).padStart(2,"0")}s`:`${r}s`};
let TIM={t:null,at:0,live:false},othersOpen=false;
function clockOf(name){const t=TIM.t;if(!t||!t.run)return null;
  const since=iso=>(Date.parse(t.as_of)-Date.parse(iso))/1000+(performance.now()-TIM.at)/1000;
  if(name==="run")return TIM.live?since(t.run.start):t.run.total_s;
  const s=(t.steps||[]).find(x=>"step:"+x.step===name);if(!s)return null;
  return TIM.live?since(s.start):s.duration_s}
function clocks(){document.querySelectorAll("[data-clock]").forEach(n=>{const v=clockOf(n.dataset.clock);n.textContent=v===null?"":dur(v)})}
setInterval(clocks,1000);
let last=null;
function render(st){last=st;const sub=document.getElementById("sub");
  sub.textContent=st.run.run_id?`${st.run.device} · ${st.run.backend==="sim"?"simulated":st.run.backend} · run ${st.run.run_id}`
    :"no run log yet: the agent has started, nothing has moved";
  kpis(st.summary);
  if(!st.plates.length){const card=document.getElementById("platecard");card.replaceChildren(el("div",{class:"empty"},"No plate in the loaded layout yet."))}
  else{const p=st.plates[0];
    const cs=Object.values(p.wells).filter(d=>d.v>0&&d.c>0).map(d=>d.c);LO=Math.min(0.01,...cs);plate(p)}  // floor keeps the scale steady while a series grows
  reservoirs(st.containers);racks(st.tip_racks);timeline(st.events);agentPanel(st.agent,st.timing);statusRow(st);configCard(st);cameraCard(st.config);autoCard(st.auto)}
async function tick(){try{const r=await fetch("/state",{cache:"no-store"});if(r.status===204){document.getElementById("livetext").textContent="waiting for a run";}
  else{const st=await r.json();const tm=st.timing||{};
    TIM={t:tm,at:performance.now(),live:!!tm.run&&!tm.run.done&&["working","waiting"].includes((st.agent||{}).state)};
    const key=JSON.stringify([st.summary,st.events.length,(st.events.at(-1)||{}).i,st.agent,st.config,st.auto,st.gate,st.estop,tm.steps,tm.split,tm.run&&tm.run.done]);
    if(key!==tick.key){tick.key=key;render(st)}clocks();document.getElementById("dot").className="dot on";document.getElementById("livetext").textContent="live"}}
  catch(e){document.getElementById("dot").className="dot";document.getElementById("livetext").textContent="disconnected"}
  setTimeout(tick,500)}
tick();
let resizeTimer;addEventListener("resize",()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>last&&render(last),150)});
</script></body></html>"""


def stop_status(ws: Workspace) -> dict[str, Any]:
  """What the stop bar shows: the folder's stop as every reader sees it (estop.read), and for an engaged
  stop each AHC server's own acknowledgement (with a line to show) and the verdict on them."""
  record = estop.read(ws)
  out: dict[str, Any] = {**(record or {"engaged": False}), "folder": str(ws.root)}
  if record and record.get("engaged"):
    try:
      out["pressed_s"] = max(0, round(time.time() - estop.path(ws).stat().st_mtime))
    except OSError:
      out["pressed_s"] = None
    acks = estop.acknowledgements(ws, record)
    out["servers"] = [{**a, "line": estop.describe_server(a)} for a in acks]
    out["verdict"] = estop.verdict(acks, out["pressed_s"] or 0)
  return out


STOP_BUTTON = ('<button class="stopbtn" id="stopbtn" title="Stops every command of this task folder\'s server at once">'
               'EMERGENCY STOP</button>')
RELEASE_BUTTON = '<button class="releasebtn" id="releasebtn">Release…</button>'
IDLE_TEXT = "Stops the hardware commands of this task folder at once; only a person releases it."
VIEW_ONLY_TEXT = "View only: the stop and its release are on the person's own page."


def render_page(ws: Workspace, token: str, view_only: bool = False) -> str:
  """The page: with the stop and release buttons, or (view_only) without them and without the release token."""
  return (PAGE.replace("<title>AHC · __AHC_FOLDER__</title>", "<title>AHC · __AHC_FOLDER__ · view only</title>" if view_only
                       else "<title>AHC · __AHC_FOLDER__</title>")
          .replace("__AHC_STOPBTN__", "" if view_only else STOP_BUTTON)
          .replace("__AHC_RELEASEBTN__", "" if view_only else RELEASE_BUTTON)
          .replace("__AHC_IDLE__", VIEW_ONLY_TEXT if view_only else IDLE_TEXT)
          .replace("__AHC_VIEW__", "1" if view_only else "0")
          .replace("__AHC_FOLDER__", html.escape(str(ws.root)))
          .replace("__AHC_TOKEN__", "" if view_only else token))


def serve(ws: Workspace, log: Path | Callable[[], Path | None] | None, port: int,
          open_browser: bool, view_only: bool = False) -> http.server.ThreadingHTTPServer:
  """The dashboard of one task folder on 127.0.0.1:<port> (0: a free port; the one bound is
  httpd.server_address[1]). Its stop button writes that folder's estop.json. `log` pins a run log, or a
  function that gives one once it exists; without it the page follows the folder's newest active log.
  view_only: the same page without the stop and release buttons, and every POST refused."""
  token = secrets.token_hex(16)  # per process: the release comes from this page, not a stray request
  hosts: set[str] = set()
  own_origins: set[str] = set()

  class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal for the demo narration
      pass

    def _send(self, status: int, body: bytes = b"", ctype: str | None = None) -> None:
      self.send_response(status)
      if ctype:
        self.send_header("Content-Type", ctype)
      self.send_header("Cache-Control", "no-store")
      self.send_header("X-Frame-Options", "DENY")  # never inside another page: no clicks steered onto the buttons
      self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
      self.send_header("Cross-Origin-Resource-Policy", "same-origin")
      if status != 204:
        self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
      self._send(status, json.dumps(data).encode(), "application/json")

    def _ours(self) -> bool:
      """Asked for by our own name and port (no DNS rebinding), and from our own page or no page at all."""
      origin = self.headers.get("Origin")
      return self.headers.get("Host") in hosts and (origin is None or origin in own_origins)

    def do_POST(self):  # noqa: N802 - the emergency stop and its release, the boxes, the auto-verify pause
      if view_only:  # nothing here writes anything: no stop, no release, whoever asks
        self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536))
        return self._json(403, {"error": "this dashboard is view-only: the stop and its release are on the person's own page"})
      # Only this page's own fetch: a custom header and our origin. Another site cannot set the
      # header without a preflight, which this server never answers.
      if not self._ours() or self.headers.get("X-AHC-Control") != "1":
        return self._json(403, {"error": "forbidden"})
      length = int(self.headers.get("Content-Length") or 0)
      try:
        payload = json.loads(self.rfile.read(length) or b"{}")
      except ValueError:
        payload = {}
      if not isinstance(payload, dict):
        payload = {}
      edit = re.fullmatch(r"/cameras/([a-z][a-z0-9_]{0,31})/slots", self.path)
      if edit:  # the person's dragged boxes: from this page, with its token, like a release
        if self.headers.get("Origin") not in own_origins or \
            not hmac.compare_digest(self.headers.get("X-AHC-Token") or "", token):
          return self._json(403, {"error": "save from the dashboard page"})
        try:
          status, body = save_slots(ws, edit.group(1), payload.get("slots"))
        except OSError as exc:
          return self._json(500, {"error": f"could not write the config: {exc}"})
        return self._json(status, body)
      if self.path in ("/auto/pause", "/auto/resume"):  # the person's switch, from this page with its token, like a release
        if self.headers.get("Origin") not in own_origins or \
            not hmac.compare_digest(self.headers.get("X-AHC-Token") or "", token):
          return self._json(403, {"error": "pause or resume auto-verify on the dashboard page"})
        try:
          if self.path == "/auto/pause":
            return self._json(200, {"paused": True, **auto_verify.pause(ws)})
          return self._json(200, {"paused": False, "changed": auto_verify.resume(ws)})
        except OSError as exc:
          return self._json(500, {"error": f"could not write the pause: {exc}"})
      if self.path == "/estop":
        try:
          return self._json(200, {**estop.engage(ws, by="dashboard", reason=str(payload.get("reason") or "")),
                                  "folder": str(ws.root)})
        except OSError as exc:
          return self._json(500, {"error": f"could not write the stop: {exc}"})
      if self.path == "/estop/release":
        # Only from this page: its origin and its token. Any local process could forge both, but
        # not by accident, and a release is the person's (estop.py).
        if self.headers.get("Origin") not in own_origins or \
            not hmac.compare_digest(self.headers.get("X-AHC-Token") or "", token):
          return self._json(403, {"error": "release on the dashboard page, or with ahc-estop --release at a terminal"})
        if payload.get("confirm") is not True:
          return self._json(400, {"error": "confirm the release"})
        try:
          released = estop.release(ws, by="dashboard")
        except OSError as exc:
          return self._json(500, {"error": f"could not write the release: {exc}"})
        if released is None:
          return self._json(409, {"error": "no emergency stop is engaged: nothing to release", **stop_status(ws)})
        return self._json(200, released)
      return self._json(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
      if not self._ours():
        return self._json(403, {"error": "forbidden"})
      route = self.path.split("?", 1)[0]
      if route == "/whoami":
        return self._json(200, {"app": "ahc-dashboard", "folder": str(ws.root), "pid": os.getpid(), "view_only": view_only})
      if route == "/estop":
        return self._json(200, stop_status(ws))
      if route == "/state":
        state = current_state(ws, log)
        if state is None:
          return self._send(204)
        return self._json(200, state)
      shown = re.fullmatch(r"/camera/([a-z][a-z0-9_]{0,31})/reference\.jpg", route)
      if shown:
        data = reference_image(ws, shown.group(1))
        return self._send(200, data, "image/jpeg") if data else self._send(404)
      image = re.fullmatch(r"/run-image/(.+)", route)
      if image:
        data = run_image(ws, image.group(1))
        return self._send(200, data, "image/jpeg") if data else self._send(404)
      if route in ("/", "/index.html"):
        return self._send(200, render_page(ws, token, view_only).encode(), "text/html; charset=utf-8")
      return self._send(404)

  httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
  bound = httpd.server_address[1]
  hosts.update({f"127.0.0.1:{bound}", f"localhost:{bound}"})
  own_origins.update({f"http://127.0.0.1:{bound}", f"http://localhost:{bound}"})
  httpd.thread = threading.Thread(target=httpd.serve_forever, daemon=True)  # for whoever shuts it down to join
  httpd.thread.start()
  if open_browser:
    webbrowser.open(f"http://127.0.0.1:{bound}/")
  return httpd


def whoami(port: int, timeout: float = 1.0) -> dict[str, Any] | None:
  """What serves 127.0.0.1:<port>: an AHC dashboard answers with its task folder; None for anything else."""
  try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/whoami", timeout=timeout) as response:
      data = json.load(response)
  except (OSError, ValueError):
    return None
  return data if isinstance(data, dict) and data.get("app") == "ahc-dashboard" else None


def open_dashboard(ws: Workspace, log: Path | Callable[[], Path | None] | None, port: int,
                   open_browser: bool, reuse: bool = True,
                   view_only: bool = False) -> tuple[http.server.ThreadingHTTPServer | None, str]:
  """For ahc-smoke, ahc-demo and ahc-dashboard: this folder's dashboard, never another folder's. A
  dashboard for this folder already on `port` follows the run too (unless `reuse` is off); anything
  else there is left alone, and the next free port serves this folder. A view-only dashboard is its
  own kind: it never reuses a control page, nor a control page a view-only one. Returns the server
  started (None when one was already there) and a line for the person."""
  try:
    httpd = serve(ws, log, port, open_browser, view_only)
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"
  except OSError:
    pass
  other = whoami(port)
  same_kind = other is not None and bool(other.get("view_only")) == view_only
  if reuse and other is not None and same_kind and _same_folder(other.get("folder"), ws.root):
    return None, f"http://127.0.0.1:{port}/ (the ahc-dashboard already running for this folder)"
  taken = (f"port {port} serves the {'view-only ' if other.get('view_only') else ''}dashboard of {other.get('folder')}"
           + ("" if _same_folder(other.get("folder"), ws.root) else "; its stop button does NOT stop this run")
           if other is not None else f"port {port} is taken by something that is not this folder's dashboard")
  for candidate in [*range(port + 1, port + 21), 0]:
    try:
      httpd = serve(ws, log, candidate, open_browser, view_only)
    except OSError:
      continue
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/ ({taken})"
  raise OSError("no free port for the dashboard")


def log_folder(log: Path) -> str | None:
  """The task folder a run log belongs to: the workspace its header names, else <task>/.ahc/runs/X.jsonl."""
  try:
    with log.open(encoding="utf-8", errors="replace") as f:
      header = json.loads(f.readline() or "{}")
  except (OSError, ValueError):
    header = {}
  if isinstance(header, dict) and header.get("type") == "run" and header.get("workspace"):
    return str(header["workspace"])
  if log.parent.name == "runs" and log.parent.parent.name == ".ahc":
    return str(log.parent.parent.parent)
  return None


def _same_folder(a: Any, b: Path) -> bool:
  try:
    return a is not None and Path(a).resolve() == Path(b).resolve()
  except (OSError, TypeError, ValueError):
    return False


def main() -> None:
  parser = argparse.ArgumentParser(description="Live dashboard for AHC run logs.")
  parser.add_argument("--workspace", default=None, help="task folder (default: AHC_WORKSPACE, else the current folder)")
  parser.add_argument("--log", default=None, help="a specific run log; default follows the newest active one in the workspace")
  parser.add_argument("--port", type=int, default=8770)
  parser.add_argument("--no-browser", action="store_true")
  parser.add_argument("--view-only", action="store_true",
                      help="a page for an audience: no stop or release controls, every POST refused (run it next to the "
                           "control one; it takes the next free port)")
  args = parser.parse_args()
  log = Path(args.log).expanduser().resolve() if args.log else None
  folder = args.workspace or (log_folder(log) if log else None)
  if log is not None and folder is None:
    sys.exit(f"ahc-dashboard: {log} is not in a task folder's .ahc/runs and names no workspace: pass --workspace, "
             "the folder whose server the stop button stops.")
  ws = Workspace(resolve_workspace(folder))
  try:
    ws.require_task_folder()  # AHC's own data folder: no server works there, so its stop button would stop nothing
  except LabError as exc:
    sys.exit(f"ahc-dashboard: {exc.message} {exc.hint}")
  try:  # a port held by another folder's dashboard is never taken for this one's
    httpd, where = open_dashboard(ws, log, args.port, not args.no_browser, reuse=log is None, view_only=args.view_only)
  except OSError as exc:
    sys.exit(f"ahc-dashboard: {exc}")
  if httpd is None:
    if not args.no_browser:
      webbrowser.open(f"http://127.0.0.1:{args.port}/")
    print(f"ahc-dashboard: {where}")
    return
  print(f"dashboard on {where} for {ws.root}; "
        + ("view only: no stop or release" if args.view_only else "its stop button stops this folder's server")
        + "  (Ctrl-C to stop)")
  try:
    threading.Event().wait()
  except KeyboardInterrupt:
    pass


if __name__ == "__main__":
  main()
