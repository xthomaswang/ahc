"""ahc-smoke: the first runs on a real OT-2, scripted, with the person at the terminal.

No agent and no LLM: a fixed sequence of AHC tool calls through the same MCP server an agent uses,
so the run log, the dashboard and every server-side check are the real ones. The person answers
every check the robot cannot make itself. Each stage adds to the one before:

    connect    the robot's identity; the person confirms this task's config. Nothing moves.
    init       + the plan, the layout and the person's deck check (the robot may home)
    tips       + pick up one column of tips (column 1) and put them back
    water      + 100 uL of water into plate column 12 (tips column 2)
    dilution   + a 3-column dye dilution in plate columns 1-3 (tips columns 3-5), instead of the water

    ahc-smoke --host 192.168.1.20 --stage connect   # first time in a task folder: the robot's address
    ahc-smoke --stage tips                          # later runs take it from .ahc/config.yaml

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
from ahc.viz.dashboard import serve

STAGES = ["connect", "init", "tips", "water", "dilution"]
DEV = "ot2.right"  # the 8-channel P300; the left P20 is not used here

LAYOUT = {
  "slots": {"1": {"name": "tips", "type": "opentrons_96_tiprack_300ul"},
            "2": {"name": "reagents", "type": "cor_96_wellplate_2mL_Vb"},
            "3": {"name": "plate", "type": "Cor_96_wellplate_360ul_Fb"}},
  "liquids": {"reagents:A1:H1": 1000, "reagents:A2:H2": 1000},
  "aliases": {"stock": "reagents:A1:H1", "water": "reagents:A2:H2"},  # stock: the dye, concentration 1
}

DECK = """Check the deck against the layout:
  slot 1   Opentrons 96 tip rack 300 uL, columns 1-5 full
  slot 2   Corning 96 deep-well plate 2 mL: column 1 dye (the stock), column 2 water, about 1 mL per well
  slot 3   Corning 96-well plate 360 uL, flat bottom, empty
  slot 12  the trash, in place and not full
  Nothing else on the deck, and no tips on the pipettes."""

EXPECTED_PIPETTES = {"right": "p300_multi_gen2", "left": "p20_single_gen2"}


class Refused(Exception):
  def __init__(self, text: str):
    super().__init__(text)
    match = re.search(r"\[([a-z_]+)\]", text)
    self.code = match.group(1) if match else None


class Stop(Exception):
  """The person answered no: nothing more moves."""


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

  async def tips_on(self, step: int, column: int) -> None:
    await self.call("pick_up_tips", device=DEV, tips=[f"tips:A{column}:H{column}"], plan_step=step)
    self.out(f"  ok       pick_up_tips  tips column {column}")
    await self.person_checks(step, "tips_mounted", f"8 tips on the right pipette (from rack column {column}), "
                             "seated straight? [yes/no] ", device=DEV, expect="mounted")

  async def move(self, step: int, source: str, target: str, volume: float, mix: bool = False) -> None:
    await self.call("aspirate", device=DEV, targets=[source], volumes=volume, plan_step=step)
    await self.call("dispense", device=DEV, targets=[target], volumes=volume, plan_step=step)
    self.out(f"  ok       {volume:g} uL {source} -> {target}")
    if mix:
      await self.call("mix", device=DEV, targets=[target], volume=100, repetitions=3, plan_step=step)
      self.out(f"  ok       mix {target} 3 x 100 uL")

  async def discard(self, step: int) -> None:
    await self.call("drop_tips", device=DEV, mode="discard", plan_step=step)
    self.out("  ok       drop_tips into the trash")

  async def configure(self, host: str | None, port: int) -> None:
    """This task's config: kept if the person already confirmed this robot here, else written and confirmed."""
    config = (await self.call("lab_overview"))["config"]
    device = (config.get("devices") or [{}])[0]
    options = device.get("options") or {}
    same_robot = host is None or (host == options.get("host") and port == options.get("port", 31950))
    if config["status"] == "confirmed" and device.get("backend") == "robot" and same_robot:
      self.out(f"config     confirmed earlier: {options.get('host')}:{options.get('port', 31950)} "
               f"({options.get('robot_name')})")
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
    await self.call("configure_devices", devices=[{"id": "ot2", "model": "opentrons.ot2", "backend": "robot",
                                                   "options": {"host": host, "port": port,
                                                               "robot_name": health.get("name")}}])
    if not self.ask(f"Confirm this robot ({health.get('name')}) for this task folder? [yes/no] "):
      raise Stop("the person did not confirm the config")
    await self.call("confirm_config")
    self.out("config     confirmed by the person")


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

  steps = plan_for(stage)
  await lab.call("report_decision", decision=f"Smoke test, stage {stage}, on the real OT-2",
                 why="first runs on the robot, one stage at a time", plan=steps)
  lab.out("\n2. layout and deck check")
  loaded = await lab.call("load_layout", layout=LAYOUT, name="smoke-deck", plan_step=1)
  lab.out(f"  ok       load_layout  {', '.join(loaded['labware'])}")
  await lab.person_checks(2, "deck_matches_layout", DECK + "\nDoes the deck match? [yes/no] ")

  step = 3
  if STAGES.index(stage) >= STAGES.index("tips"):
    lab.out("\n3. tips")
    await lab.tips_on(step, 1)
    await lab.call("drop_tips", device=DEV, mode="return", plan_step=step)
    lab.out("  ok       drop_tips  back to rack column 1")
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
                open_browser: bool = False) -> int:
  """Run one stage; 0 if it passed, 1 if something was refused or the person said no."""
  server = create_server(workspace, None, "opentrons.ot2", {})  # no forced simulation: the config decides
  if server.lab.forced_sim:  # AHC_BACKEND=sim in this shell would quietly aim it at a simulator instead
    out("ahc-smoke: AHC_BACKEND=sim is set in this shell, which forces simulation. Unset it to reach the robot.")
    return 2
  if dashboard_port:
    try:
      serve(server.lab.ws.runs_dir, None, dashboard_port, open_browser)
      out(f"dashboard  http://127.0.0.1:{dashboard_port}/")
    except OSError:
      out(f"dashboard  port {dashboard_port} is taken; a running ahc-dashboard in this folder follows the run too")
  out(f"workspace  {server.lab.ws.root}\nstage      {stage}")
  async with Client(server) as client:
    lab = Smoke(client, ask, out)
    try:
      await run_stage(lab, stage, host, port)
    except Refused as exc:
      if exc.code == "emergency_stop":
        out(f"\nSTOPPED   {exc}\nRelease the emergency stop first (the dashboard's Release, or ahc-estop --release "
            "at a terminal), check the deck and the pipettes, then run this stage again.")
      else:
        out(f"\nREFUSED   {exc}\nNothing more was sent. Fix what it says, then run this stage again.")
      return 1
    except Stop as exc:
      out(f"\nSTOPPED   {exc}\nNothing more was sent.")
      return 1
  out(f"\nstage {stage} passed. Run log: {server.lab.runlog.path if server.lab.runlog else '-'}")
  return 0


def ask_terminal(question: str) -> bool:
  while True:
    answer = input(question).strip().lower()
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
  if not sys.stdin.isatty():
    sys.exit("ahc-smoke: the person answers every check at a terminal; run it in one, not from an agent or a pipe.")
  logging.getLogger("mcp.server.mcpserver.server").setLevel(logging.WARNING)  # refusals are printed here
  sys.stdout.reconfigure(line_buffering=True)
  code = asyncio.run(smoke(args.workspace, args.stage, args.host, args.port, ask_terminal,
                           dashboard_port=args.dashboard_port, open_browser=not args.no_browser))
  sys.exit(code)


if __name__ == "__main__":
  main()
