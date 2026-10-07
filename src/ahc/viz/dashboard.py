"""Live run dashboard: replays a task's newest run log into a plate heatmap and a call timeline.

It reads only the run log the MCP server writes, so it works the same whether a script or an agent
is driving. Concentrations are the digital twin's estimate (liquid named by a `stock` alias is 1,
everything else 0, complete mixing assumed); volumes match the server's trackers.

    ahc-dashboard                       # in a task folder: follows the newest log in .ahc/runs
    ahc-dashboard --workspace <folder>
    ahc-dashboard --log <folder>/.ahc/runs/X.jsonl
"""

from __future__ import annotations

import argparse
import http.server
import json
import re
import threading
import webbrowser
from collections import Counter
from pathlib import Path
from typing import Any

from ahc.devices.spec import LIQUID_HANDLING_OPS
from ahc.plan import PlanProgress
from ahc.workspace import estop
from ahc.workspace.store import Workspace, resolve_workspace

ROWS = "ABCDEFGH"


def latest_log(runs_dir: Path) -> Path | None:
  logs = sorted(runs_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
  return logs[-1] if logs else None


def _expand(target: str) -> list[str]:
  """`plate:A1:H1` -> plate:A1 .. plate:H1 (column by column); `trough` -> trough."""
  name, _, wells = target.partition(":")
  if not wells:
    return [name]
  first, _, last = wells.partition(":")
  last = last or first
  rows = ROWS[ROWS.index(first[0]): ROWS.index(last[0]) + 1]
  return [f"{name}:{r}{c}" for c in range(int(first[1:]), int(last[1:]) + 1) for r in rows]


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
  last_pick: dict[str, list[str]] = {}
  tips_in_hand: dict[tuple[str, int], tuple[float, float]] = {}  # (device, channel) -> (uL, stock uL)
  events: list[dict[str, Any]] = []
  decisions: list[dict[str, Any]] = []  # what the agent said it decided (report_decision)
  progress = PlanProgress()  # rebuilt from the log exactly as the server keeps it
  gate: dict[str, Any] | None = None
  layout_info: dict[str, Any] | None = None
  stopped: dict[str, Any] | None = None  # the engaged emergency stop, as the server logged it

  for i, e in enumerate(entries[1:], 1):
    progress.apply(e)
    if e.get("type") == "agent":
      decisions.append({k: e[k] for k in ("t", "decision", "why", "current_step", "waiting_for", "plan_done") if k in e})
      continue
    if e.get("type") == "verification":
      events.append({"i": i, "t": e.get("t"), "op": "verify", "status": e.get("verdict"),
                     "detail": f"{e.get('check')} → {e.get('verdict')} ({e.get('judged_by')})"})
      continue
    if e.get("type") == "gate":
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
      layout_info = {"name": eff.get("saved_as"), "kind": eff.get("kind"), "labware": eff.get("labware", {}),
                     "layout": (e.get("basic") or {}).get("layout"), "t": e.get("t"),
                     "needs_person": bool(eff.get("needs_person"))}
      geometry, aliases = eff.get("geometry", {}), eff.get("aliases", {})
      names = _Names(geometry)
      vol, amt, used_tips, last_pick, tips_in_hand = {}, {}, set(), {}, {}
      stock_keys = {k for alias, target in aliases.items() if "stock" in alias or alias in (stock or [])
                    for k in _expand(target)}
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
    elif e.get("status") == "refused" and e.get("partial"):
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
      last_pick[e.get("device", "")] = spots
    elif ok and op == "drop_tips":
      tips_in_hand = {k: v for k, v in tips_in_hand.items() if k[0] != e.get("device")}
      if eff.get("mode") == "return":
        used_tips.difference_update(last_pick.pop(e.get("device", ""), []))

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
      wells = _expand(target)
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
      racks.append({"name": name, "rows": g["rows"], "cols": g["cols"], "used": used})

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
          "agent": agent_view(decisions, progress, gate, ahc_dir / "agent-feed.jsonl"),
          "config": workspace_config(ahc_dir / "config.yaml")}


def current_state(ws: Workspace, log: Path | None) -> dict[str, Any] | None:
  """What /state serves: the followed run log, else the agent alone once it has started, else nothing."""
  path = log or latest_log(ws.runs_dir)
  if path is not None and path.is_file():
    return replay(path)
  if (ws.dir / "agent-feed.jsonl").is_file():
    return replay(None, ahc_dir=ws.dir)
  return None


def agent_view(decisions: list[dict[str, Any]], progress: PlanProgress, gate: dict[str, Any] | None,
               feed_path: Path) -> dict[str, Any]:
  """The agent, live: its decisions and plan (any client: report_decision, and the plan step every call
  names) and, in Claude Code, what it was asked, what it says, what it is calling and when its turn
  ended (from the plugin's hooks). `state`: waiting (for the person), finished (the plan is done),
  working, ended (the turn ended before the plan was done) or idle."""
  feed = [e for e in _read(feed_path) if isinstance(e, dict)] if feed_path.is_file() else []
  if feed:
    session = feed[-1].get("session")
    feed = [e for e in feed if e.get("session") == session]
  said = [e for e in feed if e.get("kind") in ("said", "finished")]
  stream = sorted([{"kind": e["kind"], "t": e.get("t"), "text": e.get("text")} for e in said + [
                     e for e in feed if e.get("kind") == "request"]]
                  + [{"kind": "decision", "t": d.get("t"), "text": d.get("decision"), "why": d.get("why"),
                      "waiting_for": d.get("waiting_for")} for d in decisions], key=lambda e: e.get("t") or "")
  doing = [e for e in feed if e.get("kind") == "doing"]
  last_doing = doing[-1] if doing else None
  now = next((e for e in reversed(stream) if e["kind"] != "request"), None)
  finished = bool(now and now["kind"] == "finished" and (not last_doing or (last_doing.get("t") or "") <= (now.get("t") or "")))
  requests = [e for e in stream if e["kind"] == "request"]
  plan = progress.public() or {}
  layout_waits = bool(gate and gate.get("needs_person") and gate.get("state") == "pending")
  last = decisions[-1] if decisions else None
  asked = bool(last and last.get("waiting_for") and not (last_doing and (last_doing.get("t") or "") > (last.get("t") or "")))
  if layout_waits or asked:
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
          "history": decisions[-8:], "request": requests[-1] if requests else None, "now": now,
          "doing": {k: last_doing.get(k) for k in ("t", "tool", "brief", "ahc")} if last_doing else None,
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
  return {"status": status, "reason": why, "path": str(path),
          "devices": [{"id": d.id, "model": d.model, "backend": d.backend} for d in config.devices],
          "limits": config.limits, "confirmation": config.confirmation, "sim": config.sim}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AHC live run</title>
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
.st.audit::before{content:"⚠ ";color:var(--warning)}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}.dev{color:var(--muted);font-size:12px}
.det{color:var(--ink2);font-size:12px;margin-top:2px}.msg{color:var(--ink2);font-size:12px;margin-top:3px}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:8px;background:var(--chip);color:var(--ink2);margin-left:6px}
.tag.tr::before{content:"↻ ";color:var(--warning)}
.racks{display:flex;gap:18px;flex-wrap:wrap}.racks svg circle.free{fill:var(--axis)}
.racks svg circle.used{fill:none;stroke:var(--axis);stroke-width:1}
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
.agent .calling{margin-top:6px;font-size:13px}
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
</style></head><body>
<div class="stopbar" id="stopbar"><button class="stopbtn" id="stopbtn" title="Stops every command of this task at once">EMERGENCY STOP</button>
<span class="state" id="stopstate">Stops the hardware commands at once; only a person releases it.</span>
<button class="releasebtn" id="releasebtn">Release…</button></div>
<header><h1>AHC · live run</h1><span class="sub" id="sub">waiting for a run log…</span>
<span class="live"><span class="dot" id="dot"></span><span id="livetext">connecting</span></span></header>
<main>
<section class="card agent" id="agent"></section>
<section class="status" id="status"></section>
<section class="kpis" id="kpis"></section>
<div class="col"><section class="card" id="platecard"><div class="empty">No layout loaded yet.</div></section>
<section class="card"><h2>Reservoirs</h2><div id="containers"></div></section>
<section class="card"><h2>Tips <span class="sub">· hollow = used</span></h2><div class="racks" id="racks"></div></section>
<section class="card" id="configcard"></section></div>
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
  rs.forEach(r=>{const used=new Set(r.used),d=8,W=r.cols*d+4,H=r.rows*d+4;const w=el("div");
    w.append(el("div",{class:"dev"},`${r.name} · ${r.rows*r.cols-used.size} / ${r.rows*r.cols} left`));
    const s=sv("svg",{width:W*1.5,height:H*1.5,viewBox:`0 0 ${W} ${H}`});
    for(let c=1;c<=r.cols;c++)"ABCDEFGH".slice(0,r.rows).split("").forEach((row,ri)=>{
      s.append(sv("circle",{cx:2+(c-1)*d+d/2,cy:2+ri*d+d/2,r:2.6,class:used.has(row+c)?"used":"free"}))});
    w.append(s);box.append(w)})}

function timeline(evs){const ul=document.getElementById("timeline");ul.replaceChildren();
  [...evs].reverse().forEach((e,k)=>{const li=el("li",{class:(e.status==="refused"?"refused ":"")+(k===0?"newest":"")});
    const st=["ok","refused","pass","fail","pending","passed","failed","audit","stop","released"].includes(e.status)?e.status:"ok";
    const top=el("div");top.append(el("span",{class:`st ${st}`},st),el("code",{},e.op));if(e.device)top.append(el("span",{class:"dev"},"  "+e.device));
    if(e.translated)top.append(el("span",{class:"tag tr"},"translated"));
    if(e.specialized&&e.specialized.length)top.append(el("span",{class:"tag"},"specialized: "+e.specialized.join(", ")));
    if(e.commands)top.append(el("span",{class:"tag"},`${e.commands} cmds`));li.append(top);
    if(e.detail)li.append(el("div",{class:"det"},e.detail));
    if(e.status==="refused"&&e.message)li.append(el("div",{class:"msg"},e.message.replace(/^Error executing tool \w+: /,"")));
    else if(e.note)li.append(el("div",{class:"msg"},e.note));ul.append(li)})}

function agentPanel(a){const box=document.getElementById("agent");box.replaceChildren();
  const head=el("div",{class:"label"});head.append(el("span",{},"Agent"));box.append(head);
  const nothing=!a||(!a.now&&!a.current&&!a.request&&!a.doing);
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
  if(a.doing&&!a.finished&&a.state!=="finished"){const c=el("div",{class:"calling"});c.append(el("span",{class:"k"},"Calling "),el("code",{},a.doing.tool),
    el("span",{class:"dev"},a.doing.brief?"  "+a.doing.brief:""),el("span",{class:"dev"},"  "+(a.doing.t||"").slice(11)));box.append(c)}
  if(a.layout_waits){const b=el("div",{class:"banner"});b.append(el("span",{},"The agent proposes a layout this folder has no reference for. Check its labware, positions and liquids in "),
    el("a",{href:"#layoutsec"},"Configuration › Layout"),el("span",{},", then tell the agent to go on, or what to change. Nothing moves until you do."));box.append(b)}
  if(a.plan&&a.plan.length){const ol=el("ol",{class:"plan"}),n=a.plan.length,st=a.steps||[];
    // Each call names its step: a step is ticked once the agent moves on, and the plan ends when it reports it done.
    const where=a.plan_done?"finished":a.current_step?`step ${a.current_step} of ${n}`:`${n} steps, not started`;
    box.append(el("h3",{},`Plan · ${where}`+(a.plan_at?` · reported ${a.plan_at.slice(11)}`:"")));
    a.plan.forEach((step,k)=>{const s=st[k]||"next";ol.append(el("li",{class:s},`${k+1}. ${step}`+(s==="skipped"?" · skipped":"")))});box.append(ol)}
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

async function control(path,body){return fetch(path,{method:"POST",headers:{"X-AHC-Control":"1","Content-Type":"application/json"},body:JSON.stringify(body||{})})}
document.getElementById("stopbtn").onclick=async()=>{await control("/estop",{reason:"pressed on the dashboard"});pollStop()};
document.getElementById("releasebtn").onclick=async()=>{
  if(!confirm("Release the emergency stop?\n\nOnly when the deck is safe. The agent must check the deck again before anything moves."))return;
  await control("/estop/release",{confirm:true});pollStop()};
async function pollStop(){try{const r=await fetch("/estop",{cache:"no-store"});const e=await r.json();
  const bar=document.getElementById("stopbar"),state=document.getElementById("stopstate");
  if(e.engaged){bar.className="stopbar engaged";state.textContent=`■ EMERGENCY STOP ENGAGED · ${(e.at||"").replace("T"," ")} · ${e.by} · nothing runs until a person releases it`}
  else{bar.className="stopbar";state.textContent=e.released_at?`Released ${(e.released_at||"").replace("T"," ")} by ${e.released_by}; the deck must be checked again before anything moves.`:"Stops the hardware commands at once; only a person releases it."}}catch(err){}
  clearTimeout(pollStop.t);pollStop.t=setTimeout(pollStop,500)}
pollStop();

let last=null;
function render(st){last=st;const sub=document.getElementById("sub");
  sub.textContent=st.run.run_id?`${st.run.device} · ${st.run.backend==="sim"?"simulated":st.run.backend} · run ${st.run.run_id}`
    :"no run log yet: the agent has started, nothing has moved";
  kpis(st.summary);
  if(!st.plates.length){const card=document.getElementById("platecard");card.replaceChildren(el("div",{class:"empty"},"No plate in the loaded layout yet."))}
  else{const p=st.plates[0];
    const cs=Object.values(p.wells).filter(d=>d.v>0&&d.c>0).map(d=>d.c);LO=Math.min(0.01,...cs);plate(p)}  // floor keeps the scale steady while a series grows
  reservoirs(st.containers);racks(st.tip_racks);timeline(st.events);agentPanel(st.agent);statusRow(st);configCard(st)}
async function tick(){try{const r=await fetch("/state",{cache:"no-store"});if(r.status===204){document.getElementById("livetext").textContent="waiting for a run";}
  else{const st=await r.json();const key=JSON.stringify([st.summary,st.events.length,(st.events.at(-1)||{}).i,st.agent,st.config,st.gate,st.estop]);
    if(key!==tick.key){tick.key=key;render(st)}document.getElementById("dot").className="dot on";document.getElementById("livetext").textContent="live"}}
  catch(e){document.getElementById("dot").className="dot";document.getElementById("livetext").textContent="disconnected"}
  setTimeout(tick,500)}
tick();
let resizeTimer;addEventListener("resize",()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>last&&render(last),150)});
</script></body></html>"""


def serve(runs_dir: Path, log: Path | None, port: int, open_browser: bool) -> http.server.ThreadingHTTPServer:
  ws = Workspace((log.parent if log else runs_dir).parent.parent)  # <task>/.ahc/runs -> <task>
  own_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

  class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal for the demo narration
      pass

    def _json(self, status: int, data: Any) -> None:
      body = json.dumps(data).encode()
      self.send_response(status)
      self.send_header("Content-Type", "application/json")
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - the emergency stop and its release
      # Only this page's own fetch: a custom header and our origin. Another site cannot set the
      # header without a preflight, which this server never answers.
      origin = self.headers.get("Origin")
      if self.headers.get("X-AHC-Control") != "1" or (origin is not None and origin not in own_origins):
        return self._json(403, {"error": "forbidden"})
      length = int(self.headers.get("Content-Length") or 0)
      try:
        payload = json.loads(self.rfile.read(length) or b"{}")
      except ValueError:
        payload = {}
      if self.path == "/estop":
        return self._json(200, estop.engage(ws, by="dashboard", reason=str(payload.get("reason") or "")))
      if self.path == "/estop/release":
        if payload.get("confirm") is not True:
          return self._json(400, {"error": "confirm the release"})
        return self._json(200, estop.release(ws, by="dashboard") or {"engaged": False})
      return self._json(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
      if self.path.startswith("/estop"):
        return self._json(200, estop.read(ws) or {"engaged": False})
      if self.path.startswith("/state"):
        state = current_state(ws, log)
        if state is None:
          self.send_response(204)
          self.end_headers()
          return
        body = json.dumps(state).encode()
        ctype = "application/json"
      elif self.path in ("/", "/index.html"):
        body, ctype = PAGE.encode(), "text/html; charset=utf-8"
      else:
        self.send_response(404)
        self.end_headers()
        return
      self.send_response(200)
      self.send_header("Content-Type", ctype)
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)

  httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
  threading.Thread(target=httpd.serve_forever, daemon=True).start()
  if open_browser:
    webbrowser.open(f"http://127.0.0.1:{port}/")
  return httpd


def main() -> None:
  parser = argparse.ArgumentParser(description="Live dashboard for AHC run logs.")
  parser.add_argument("--workspace", default=None, help="task folder (default: AHC_WORKSPACE, else the current folder)")
  parser.add_argument("--log", default=None, help="a specific run log; default follows the newest in the workspace")
  parser.add_argument("--port", type=int, default=8770)
  parser.add_argument("--no-browser", action="store_true")
  args = parser.parse_args()
  runs = Workspace(resolve_workspace(args.workspace)).runs_dir
  serve(runs, Path(args.log) if args.log else None, args.port, not args.no_browser)
  print(f"dashboard on http://127.0.0.1:{args.port}/ for {runs}  (Ctrl-C to stop)")
  try:
    threading.Event().wait()
  except KeyboardInterrupt:
    pass


if __name__ == "__main__":
  main()
