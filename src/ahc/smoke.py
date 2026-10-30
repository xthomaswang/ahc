"""ahc-smoke: the first runs on a real OT-2, scripted, with the person at the terminal.

No agent and no LLM: a fixed sequence of AHC tool calls through the same MCP server an agent uses,
so the run log, the dashboard and every server-side check are the real ones. The person answers
every check the robot cannot make itself, at a terminal: it refuses to run under an agent. Each
stage adds to the one before:

    connect    the robot's identity; the person confirms this task's config. Nothing moves.
    init       + the plan, the layout and the person's deck check (the robot homes first, after asking)
    tips       + pick up one column of tips (column 1) and put them back
    water      + 100 uL of water into plate column 12 (tips column 2)
    dilution   + a 3-column dye dilution in plate columns 1-3 (tips columns 3-5), instead of the water

    ahc-smoke --host 192.168.1.20 --stage connect   # first time in a task folder: the robot's address
    ahc-smoke --stage tips                          # later runs take it from .ahc/config.yaml

The config is never rewritten behind the person's back: task limits they added stay, and a config
they edited is shown and confirmed as it is. A limit that would refuse a stage's values stops the
stage before the robot homes, not half-way through it. Every stage is a new server session, which
moves nothing before the person's go-ahead: asked at the terminal (confirming the config gives it).
Tips an earlier stage left on the pipettes, which the robot forgot, hold the next run back until the
person says at the terminal that both pipettes are empty.

Rehearse first against an OT-2 simulator: `ahc-sim ot2` in some folder, then in the task folder
`ahc-smoke --host 127.0.0.1 --port <the simulator's port> --stage dilution`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

from mcp import Client

from ahc.server import create_server
from ahc.viz.dashboard import open_dashboard
from ahc.workspace.estop import running_agent

STAGES = ["connect", "init", "tips", "water", "dilution"]
DEV = "ot2.right"  # the 8-channel P300; the left P20 is not used here

LAYOUT = {
  "slots": {"1": {"name": "tips", "type": "opentrons_96_tiprack_300ul"},
            "2": {"name": "reagents", "type": "cor_96_wellplate_2mL_Vb"},
            "3": {"name": "plate", "type": "Cor_96_wellplate_360ul_Fb"}},
  "liquids": {"reagents:A1:H1": 1000, "reagents:A2:H2": 1000},
  "aliases": {"stock": "reagents:A1:H1", "water": "reagents:A2:H2"},  # stock: the dye, concentration 1
}

# Liquid heights, mm above the well bottom, given on every call instead of a default: aspirating in the
# deep-well plate stays clear of its V bottom; in the flat 360 uL plate everything happens at 1 mm.
ASPIRATE_MM = {"reagents": 1.5, "plate": 1.0}
DISPENSE_MM = 1.0
MIX_MM = 1.0
MIX_UL, MIX_TIMES = 100, 3

# The liquid moves of the stages that move liquid: (source, uL, mix in the target after). run_stage makes
# them; liquid_calls() lists the values they carry, which are checked against the limits before the stage.
MOVES = {"water": [("water", 100, False)],
         "dilution": [("water", 100, False), ("water", 100, False), ("stock", 200, False),
                      ("plate:A1:H1", 100, True), ("plate:A2:H2", 100, True)]}

# Refusals after which the robot may have carried out (part of) the command: C2's completion_unknown,
# and a stop the robot did not confirm.
OUTCOME_UNKNOWN = ("completion_unknown", "device_halted")

# What each stage takes from the deck: tip rack columns (column 1 always goes back) and plate columns.
TIP_COLUMNS = {"connect": [], "init": [], "tips": [1], "water": [1, 2], "dilution": [1, 3, 4, 5]}
PLATE_COLUMNS = {"connect": [], "init": [], "tips": [], "water": [12], "dilution": [1, 2, 3]}

HOMING = "The robot homes now: pipettes rise and the gantry moves. Hands out and deck set? [yes/no] "

EXPECTED_PIPETTES = {"right": "p300_multi_gen2", "left": "p20_single_gen2"}


class Refused(Exception):
  def __init__(self, text: str):
    super().__init__(text)
    match = re.search(r"\[([a-z_]+)\]", text)
    self.code = match.group(1) if match else None


class Stop(Exception):
  """The person answered no: nothing more moves."""


def columns(cols: list[int]) -> str:
  """[1, 3, 4, 5] -> 'columns 1 and 3-5'; [12] -> 'column 12'."""
  runs: list[list[int]] = []
  for c in sorted(set(cols)):
    if runs and c == runs[-1][-1] + 1:
      runs[-1].append(c)
    else:
      runs.append([c])
  parts = [f"{r[0]}-{r[-1]}" if len(r) > 1 else str(r[0]) for r in runs]
  text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
  return ("column " if len(cols) == 1 else "columns ") + text


def deck_text(stage: str, heading: str) -> str:
  """The deck as this stage needs it: the twin starts every stage with a full rack and an empty plate,
  so the columns this stage uses must really be so."""
  tips, plate = TIP_COLUMNS[stage], PLATE_COLUMNS[stage]
  rack = f"full tips in {columns(tips)}" if tips else "no tips are used in this stage"
  wells = f"{columns(plate)} empty" if plate else "nothing is dispensed in this stage"
  return (f"{heading} (lids off; A1 at the back left of each slot):\n"
          f"  slot 1   Opentrons 96 tip rack 300 uL: {rack}\n"
          "  slot 2   Corning 96 deep-well plate 2 mL: column 1 dye (the stock), column 2 water, about 1 mL per well "
          "(at least 0.5 mL)\n"
          f"  slot 3   Corning 96-well plate 360 uL, flat bottom: {wells}\n"
          "  slot 12  the trash, in place and not full\n"
          "  Nothing else on the deck, and no tips on either pipette.")


def plan_for(stage: str) -> list[str]:
  steps = ["Load the layout", "The person checks the deck"]
  if STAGES.index(stage) >= STAGES.index("tips"):
    steps.append("Tips: pick up column 1, the person checks them, put them back")
  if stage == "water":
    steps += ["Water: 100 uL into plate column 12 (tips column 2)", "The person checks plate column 12"]
  if stage == "dilution":
    steps += ["Water: 100 uL into plate columns 2 and 3 (tips column 3)",
              "Dye: 200 uL into column 1, then 100 uL from column 1 to column 2 and mix (tips column 4)",
              "100 uL from column 2 to column 3 and mix (tips column 5)",
              "The person checks plate columns 1-3"]
  return steps


def describe_limits(limits: dict[str, Any] | None) -> str:
  parts = [f"{comp} {param} " + ", ".join(f"{k} {v}" for k, v in bounds.items())
           for comp, params in (limits or {}).items() for param, bounds in (params or {}).items()
           if isinstance(bounds, dict)]
  return "; ".join(parts) or "none"


def describe_config(config: dict[str, Any]) -> str:
  """One line for the person, like the server's confirm_summary, for a server that gives none."""
  entries = []
  for d in config.get("devices") or []:
    options = d.get("options") or {}
    where = f", robot '{options.get('robot_name')}'" if options.get("robot_name") else ""
    if options.get("host"):
      where += f" at {options['host']}:{options.get('port', 31950)}"
    entries.append(f"{d.get('id')} = {d.get('model')}, backend {d.get('backend')}{where}")
  return "; ".join(entries) + f"; task limits: {describe_limits(config.get('limits'))}"


def robot_health(host: str, port: int) -> tuple[dict[str, Any], dict[str, str]]:
  """The robot's /health and what its mounts carry, read before anything is configured."""
  def get(path: str) -> dict[str, Any]:
    request = urllib.request.Request(f"http://{host}:{port}{path}", headers={"Opentrons-Version": "*"})
    with urllib.request.urlopen(request, timeout=10) as response:
      return json.load(response)
  health = get("/health")
  try:
    mounts = {i.get("mount"): i.get("instrumentName") for i in get("/instruments").get("data") or []}
  except urllib.error.HTTPError:  # older robot software
    mounts = {m: p.get("name") for m, p in get("/pipettes").items() if isinstance(p, dict)}
  return health, mounts


class Smoke:
  def __init__(self, client: Client, ask: Callable[[str], bool], out: Callable[[str], None]):
    self.client = client
    self.ask = ask
    self.out = out
    self.tips: str | None = None  # "on" after a pick-up, "maybe" while one runs, None once they are off
    self.tips_taken: list[int] = []  # rack columns picked up and not put back
    self.plate_used: list[int] = []  # plate columns liquid went to

  async def call(self, tool: str, **args) -> dict[str, Any]:
    result = await self.client.call_tool(tool, args)
    if result.is_error:
      raise Refused(re.sub(r"^Error executing tool \w+: ", "", result.content[0].text))
    return result.structured_content

  async def person_checks(self, step: int, check: str, question: str, **args) -> None:
    result = await self.call("verify", check=check, plan_step=step, **args)
    if result["verdict"] == "pending":  # real hardware: the person's verdict
      yes = self.ask(question)
      await self.call("record_verdict", check_id=result["check_id"], verdict="pass" if yes else "fail",
                      note="answered at the terminal (ahc-smoke)")
      if not yes:
        raise Stop(f"{check}: the person answered no")
    elif result["verdict"] != "pass":
      raise Stop(f"{check}: {result['verdict']} ({result['judged_by']})")
    self.out(f"  pass     {check}")

  async def pipettes_empty(self, step: int, refusal: Refused) -> None:
    """load_layout waits for the person's word that no tip is on either pipette (tips the robot no longer knows
    of, from an earlier run): asked here and recorded as their verdict on the check of every pipette."""
    told = re.sub(r"^\[\w+\] ", "", str(refusal)).split(" Hint: ")[0]  # what the person needs, not the agent's hint
    self.out(f"\nTIPS      {told}")
    await self.person_checks(step, "tips_mounted", "Take any tips off both pipettes by hand. Are both pipettes empty "
                             "now? [yes/no] ", expect="none")

  async def tips_on(self, step: int, column: int) -> None:
    self.tips = "maybe"  # until the pick-up returns, tips may be on
    self.tips_taken.append(column)
    await self.call("pick_up_tips", device=DEV, tips=[f"tips:A{column}:H{column}"], plan_step=step)
    self.tips = "on"
    self.out(f"  ok       pick_up_tips  tips column {column}")
    await self.person_checks(step, "tips_mounted", f"8 tips on the right pipette (from rack column {column}), "
                             "seated straight? [yes/no] ", device=DEV, expect="mounted")

  async def put_back(self, step: int, column: int) -> None:
    await self.call("drop_tips", device=DEV, mode="return", plan_step=step)
    self.tips = None
    self.tips_taken.remove(column)
    self.out(f"  ok       drop_tips  back to rack column {column}")

  async def move(self, step: int, source: str, target: str, volume: float, mix: bool = False) -> None:
    await self.call("aspirate", device=DEV, targets=[source], volumes=volume, liquid_height=aspirate_mm(source),
                    plan_step=step)
    self.plate_used += plate_columns(target)
    await self.call("dispense", device=DEV, targets=[target], volumes=volume, liquid_height=DISPENSE_MM,
                    plan_step=step)
    self.out(f"  ok       {volume:g} uL {source} -> {target}")
    if mix:
      await self.call("mix", device=DEV, targets=[target], volume=MIX_UL, repetitions=MIX_TIMES, liquid_height=MIX_MM,
                      plan_step=step)
      self.out(f"  ok       mix {target} {MIX_TIMES} x {MIX_UL} uL")

  async def discard(self, step: int) -> None:
    await self.call("drop_tips", device=DEV, mode="discard", plan_step=step)
    self.tips = None
    self.out("  ok       drop_tips into the trash")

  def left_on_deck(self) -> list[str]:
    """After a stop or a refusal: what the person must know before anything runs again."""
    lines = []
    if self.tips == "on":
      lines.append("Tips are on the right pipette. Take them off by hand before the next run: the robot forgets them "
                   "once its run stops, and its next pick-up would drive them into the rack.")
    elif self.tips == "maybe":
      lines.append("Tips may be on the right pipette (the pick-up did not finish). Check, and take any off by hand "
                   "before the next run: the robot forgets them once its run stops.")
    used = []
    if self.tips_taken:
      used.append(f"tips from rack {columns(self.tips_taken)}")
    if self.plate_used:
      used.append(f"plate {columns(self.plate_used)}")
    if used:
      lines.append(f"This run used {' and '.join(used)}: before running a stage again, set the deck as that stage "
                   "lists it (a fresh rack or plate where needed).")
    return lines

  async def configure(self, host: str | None, port: int) -> None:
    """This task's config, never rewritten behind the person's back: a confirmed one for this robot is
    kept; a pending one for it (the person edited it, e.g. tightened a limit) is confirmed as it is; a new
    address writes a config that keeps the folder's task limits."""
    config = (await self.call("lab_overview"))["config"]
    if config["status"] == "invalid":
      raise Stop(f"this task folder's .ahc/config.yaml is invalid ({config.get('reason')}). Fix it by hand (it may "
                 "hold task limits you set), then run this stage again.")
    device = (config.get("devices") or [{}])[0]
    options = device.get("options") or {}
    limits = config.get("limits") or {}
    same_robot = device.get("backend") == "robot" and (
      host is None or (host == options.get("host") and port == options.get("port", 31950)))
    if config["status"] == "confirmed" and same_robot:
      self.out(f"config     confirmed earlier: {options.get('host')}:{options.get('port', 31950)} "
               f"({options.get('robot_name')}); task limits: {describe_limits(limits)}")
      return
    if config["status"] == "pending" and same_robot:
      self.out(f"config     pending: {config.get('reason')}")
      await self.confirm(config)
      return
    if host is None:
      raise Stop("no robot configured in this task folder yet: pass --host (and --port if not 31950)")
    try:
      health, mounts = robot_health(host, port)
    except OSError as exc:  # a wrong address, or the robot is off or on another network
      raise Stop(f"no robot-server at {host}:{port} ({exc}). Is the robot on, and on this network?") from exc
    self.out(f"robot      {health.get('name')} at {host}:{port} · serial {health.get('robot_serial')} · "
             f"software {health.get('api_version')} · firmware {health.get('fw_version')}")
    for mount, expected in EXPECTED_PIPETTES.items():
      flag = "" if mounts.get(mount) == expected else f"   <- the description file expects {expected}"
      self.out(f"           {mount:<5} {mounts.get(mount)}{flag}")
    written = await self.call("configure_devices", devices=[{"id": "ot2", "model": "opentrons.ot2", "backend": "robot",
                                                             "options": {"host": host, "port": port,
                                                                         "robot_name": health.get("name")}}],
                              limits=limits or None)  # the person's task limits stay
    if written.get("next"):
      self.out(f"           next: {written['next']}")
    await self.confirm(written["config"])

  async def check_limits(self, stage: str) -> None:
    """Before anything moves: every value this stage sends lies inside the limits the server applies (task
    limits included), so a limit refuses the stage here, not half-way through it with tips on."""
    basic: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for op, values in liquid_calls(stage):
      if op not in basic:
        basic[op] = (await self.call("get_params", device=DEV, op=op)).get("basic") or {}
      for name, value in values.items():
        bounds = (basic[op].get(name) or {}).get("limits") or {}
        low, high = bounds.get("min"), bounds.get("max")
        if (low is not None and value < low) or (high is not None and value > high):
          problems.append(f"{op} {name} {value:g} (allowed {low}..{high})")
    for op, params in basic.items():  # smoke gives no flow rate: a task limit on it must cover the default
      flow = (params.get("flow_rate") or {}).get("limits") or {}
      default = flow.get("default")
      if flow.get("task_limit") and not (isinstance(default, (int, float)) and
                                         (flow.get("min") is None or default >= flow["min"]) and
                                         (flow.get("max") is None or default <= flow["max"])):
        problems.append(f"{op} flow_rate: smoke gives none, and the task limit {flow.get('min')}..{flow.get('max')} "
                        f"leaves out the default {default}")
    if problems:
      raise Stop(f"stage {stage} sends values outside the limits for {DEV}: " + "; ".join(dict.fromkeys(problems))
                 + ". Nothing moved. Change the task limits in .ahc/config.yaml (and confirm them), or run another stage.")

  async def go_ahead(self, stage: str) -> None:
    """Every stage runs in a new server session, which moves nothing on the robot before the person's go-ahead:
    asked here, never given silently. A config the person confirmed in this stage gave it already."""
    overview = await self.call("lab_overview")
    if (overview.get("session") or {}).get("go_ahead") != "needed":
      return
    summary = overview["config"].get("confirm_summary")
    if not self.ask(f"Go ahead with stage {stage} on this robot? This new session moves nothing before you say so.\n"
                    f"  {summary}\n[yes/no] "):
      raise Stop("the person did not give the go-ahead for this stage")
    await self.call("confirm_config", summary=summary)
    self.out("go-ahead   given by the person")

  async def confirm(self, config: dict[str, Any]) -> None:
    """The person confirms the config as the server holds it, every line of it shown."""
    summary = config.get("confirm_summary")
    if not self.ask(f"Confirm this robot and config for this task folder?\n  {summary or describe_config(config)}\n"
                    "[yes/no] "):
      raise Stop("the person did not confirm the config")
    if summary:  # the server checks that what the person saw is what it holds
      await self.call("confirm_config", summary=summary)
    else:
      await self.call("confirm_config")
    self.out("config     confirmed by the person")


def aspirate_mm(source: str) -> float:
  return ASPIRATE_MM[LAYOUT["aliases"].get(source, source).partition(":")[0]]


def liquid_calls(stage: str) -> list[tuple[str, dict[str, float]]]:
  """What the stage's liquid calls carry that a limit can refuse, call by call, as run_stage sends them."""
  calls: list[tuple[str, dict[str, float]]] = []
  for source, volume, mix in MOVES.get(stage, []):
    calls.append(("aspirate", {"volumes": volume, "liquid_height": aspirate_mm(source)}))
    calls.append(("dispense", {"volumes": volume, "liquid_height": DISPENSE_MM}))
    if mix:
      calls.append(("mix", {"volume": MIX_UL, "repetitions": MIX_TIMES, "liquid_height": MIX_MM}))
  return calls


def refusal_text(exc: Refused) -> str:
  """What the person reads after a refusal: what the robot may have done, and what to do now."""
  if exc.code == "emergency_stop":
    return (f"\nSTOPPED   {exc}\nRelease the emergency stop first (the dashboard's Release, or ahc-estop --release "
            "at a terminal), check the deck and the pipettes, then run this stage again.")
  if exc.code in OUTCOME_UNKNOWN:
    return (f"\nREFUSED   {exc}\nThe robot may have carried out what it was sent last, or part of it, and may still "
            "be finishing it: wait until it is still, then check the tips and the wells. Smoke sends nothing more; run "
            "a stage again only with the deck set as that stage lists it.")
  return f"\nREFUSED   {exc}\nNothing more was sent. Fix what it says, then run this stage again."


def plate_columns(target: str) -> list[int]:
  """plate:A2:H2 -> [2]; plate:A1:H3 -> [1, 2, 3]; anything else -> []."""
  match = re.fullmatch(r"plate:[A-H](\d+)(?::[A-H](\d+))?", target)
  if not match:
    return []
  first = int(match.group(1))
  return list(range(first, int(match.group(2) or first) + 1))


async def run_stage(lab: Smoke, stage: str, host: str | None, port: int) -> None:
  lab.out(f"\n1. connect")
  await lab.configure(host, port)
  overview = await lab.call("lab_overview")  # every stage starts by reaching the robot, without moving it
  identity = overview.get("device_identity") or {}
  if not identity or "error" in identity:
    raise Stop(f"the configured robot does not answer: {identity.get('error', 'no identity')}. "
               "Is it on, and on this network?")
  lab.out(f"reached    {identity.get('name')} · software {identity.get('api_version')}")
  if stage == "connect":
    lab.out(f"           next: {overview['next']}")
    return
  await lab.check_limits(stage)  # a task limit that refuses this stage does so now, before the robot homes
  await lab.go_ahead(stage)

  steps = plan_for(stage)
  await lab.call("report_decision", decision=f"Smoke test, stage {stage}, on the real OT-2",
                 why="first runs on the robot, one stage at a time", plan=steps)
  lab.out("\n2. layout and deck check")
  lab.out(deck_text(stage, "Set the deck for this stage"))
  if not lab.ask(HOMING):  # load_layout starts a run on the robot, and that homes it
    raise Stop("the person was not ready for the robot to home")
  try:
    loaded = await lab.call("load_layout", layout=LAYOUT, name="smoke-deck", plan_step=1)
  except Refused as exc:
    if exc.code != "tips_unconfirmed":
      raise
    await lab.pipettes_empty(1, exc)
    if not lab.ask(HOMING):  # their hands were at the pipettes: they say again when it may home
      raise Stop("the person was not ready for the robot to home")
    loaded = await lab.call("load_layout", layout=LAYOUT, name="smoke-deck", plan_step=1)
  lab.out(f"  ok       load_layout  {', '.join(loaded['labware'])}")
  await lab.person_checks(2, "deck_matches_layout",
                          deck_text(stage, "Check the deck against the layout") + "\nDoes the deck match? [yes/no] ")

  step = 3
  if STAGES.index(stage) >= STAGES.index("tips"):
    lab.out("\n3. tips")
    await lab.tips_on(step, 1)
    await lab.put_back(step, 1)
    await lab.person_checks(step, "tips_mounted", "Tips back in rack column 1, none left on the pipette? [yes/no] ",
                            device=DEV, expect="none")
    step += 1

  if stage == "water":
    lab.out("\n4. water")
    await lab.tips_on(step, 2)
    await lab.move(step, "water", "plate:A12:H12", 100)
    await lab.discard(step)
    await lab.person_checks(step + 1, "liquid_present", "About 100 uL in each well of plate column 12, and no drops "
                            "anywhere else? [yes/no] ", targets=["plate:A12:H12"], min_volume_ul=90)
  elif stage == "dilution":
    lab.out("\n4. dilution")
    await lab.tips_on(step, 3)
    await lab.move(step, "water", "plate:A2:H2", 100)
    await lab.move(step, "water", "plate:A3:H3", 100)
    await lab.discard(step)
    await lab.tips_on(step + 1, 4)
    await lab.move(step + 1, "stock", "plate:A1:H1", 200)
    await lab.move(step + 1, "plate:A1:H1", "plate:A2:H2", 100, mix=True)
    await lab.discard(step + 1)
    await lab.tips_on(step + 2, 5)
    await lab.move(step + 2, "plate:A2:H2", "plate:A3:H3", 100, mix=True)
    await lab.discard(step + 2)
    await lab.person_checks(step + 3, "liquid_present", "Plate columns 1-3 hold liquid, the colour fading from "
                            "column 1 to column 3? [yes/no] ", targets=["plate:A1:H3"], min_volume_ul=90)
  await lab.call("report_decision", decision=f"Smoke test stage {stage} passed", plan_done=True)


async def smoke(workspace, stage: str, host: str | None, port: int, ask: Callable[[str], bool],
                out: Callable[[str], None] = print, dashboard_port: int | None = None,
                open_browser: bool = False, *, scripted: bool = False) -> int:
  """Run one stage; 0 if it passed, 1 if something was refused or the person said no, 2 if it may not run
  here. `scripted`: a test plays the person through `ask`; otherwise an agent's environment is refused."""
  agent = running_agent()
  if agent and not scripted:  # the person answers every check; an agent reaches a robot only through the tools
    out(f"ahc-smoke: {agent} is set: an agent is running this. The person runs ahc-smoke at a terminal.")
    return 2
  server = create_server(workspace, None, "opentrons.ot2", {})  # no forced simulation: the config decides
  if server.lab.forced_sim:  # AHC_BACKEND=sim in this shell would quietly aim it at a simulator instead
    out("ahc-smoke: AHC_BACKEND=sim is set in this shell, which forces simulation. Unset it to reach the robot.")
    return 2
  if dashboard_port:
    lab_state = server.lab  # the page follows this run's own log once it exists
    try:
      _, where = open_dashboard(lab_state.ws, lambda: lab_state.runlog.path if lab_state.runlog else None,
                                dashboard_port, open_browser)
      out(f"dashboard  {where}")
    except OSError as exc:
      out(f"dashboard  not started ({exc}); ahc-dashboard --workspace {lab_state.ws.root} shows the run")
  out(f"workspace  {server.lab.ws.root}\nstage      {stage}")
  async with Client(server) as client:
    lab = Smoke(client, ask, out)
    try:
      await run_stage(lab, stage, host, port)
    except Refused as exc:
      out(refusal_text(exc))
      for line in lab.left_on_deck():
        out(line)
      return 1
    except Stop as exc:
      out(f"\nSTOPPED   {exc}\nNothing more was sent.")
      for line in lab.left_on_deck():
        out(line)
      return 1
    except BaseException as exc:  # Ctrl-C, a closed terminal, a fault: the person still learns what is on the deck
      out(f"\nINTERRUPTED ({type(exc).__name__}). A command already sent may still be running on the robot: wait "
          "until it is still.")
      for line in lab.left_on_deck():
        out(line)
      raise
  out(f"\nstage {stage} passed. Run log: {server.lab.runlog.path if server.lab.runlog else '-'}")
  return 0


def ask_terminal(question: str) -> bool:
  while True:
    try:
      answer = input(question).strip().lower()
    except EOFError:  # the terminal closed (Ctrl-D): no answer is no
      print()
      return False
    if answer in ("y", "yes"):
      return True
    if answer in ("n", "no"):
      return False


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(prog="ahc-smoke", description="Scripted first runs on a real OT-2, one stage at a time.")
  parser.add_argument("--stage", choices=STAGES, required=True)
  parser.add_argument("--workspace", default=None, help="task folder (default: AHC_WORKSPACE, else the current folder)")
  parser.add_argument("--host", default=None, help="the robot's address (first run in a task folder)")
  parser.add_argument("--port", type=int, default=31950)
  parser.add_argument("--dashboard-port", type=int, default=8770)
  parser.add_argument("--no-browser", action="store_true")
  args = parser.parse_args(argv)
  agent = running_agent()
  if agent:
    sys.exit(f"ahc-smoke: the person answers every check at a terminal; run it yourself, not from an agent "
             f"({agent} is set).")
  if not sys.stdin.isatty():
    sys.exit("ahc-smoke: the person answers every check at a terminal; run it in one, not from an agent or a pipe.")
  logging.getLogger("mcp.server.mcpserver.server").setLevel(logging.WARNING)  # refusals are printed here
  sys.stdout.reconfigure(line_buffering=True)
  try:
    code = asyncio.run(smoke(args.workspace, args.stage, args.host, args.port, ask_terminal,
                             dashboard_port=args.dashboard_port, open_browser=not args.no_browser))
  except KeyboardInterrupt:  # what is on the deck was printed on the way out
    sys.exit("ahc-smoke: interrupted.")
  sys.exit(code)


if __name__ == "__main__":
  main()
