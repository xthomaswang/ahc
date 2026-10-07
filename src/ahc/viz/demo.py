"""Scripted visual demo: a serial dilution through the MCP server, paced so you can watch it.

Two browser tabs follow the run: the 3D device view (PyLabRobot's Viewer3D: tips leave the rack,
wells fill) and the run dashboard (concentration heatmap and every call from the run log). The
script is an MCP client, so the server sees exactly what an agent would send.

Everything goes to the task folder it runs in (or --workspace): .ahc/config.yaml, layouts/, runs/.
It reports its plan first and names the plan step on every call, as an agent must. In a folder with
no reference for its example layout, the person confirms the layout: asked on the terminal, or, with
no terminal (a background run), the demo answers for them and says so.

    ahc-demo --backend sim                          # STARlet simulation in the current folder
    ahc-demo --backend sim --native-mix             # STAR's own post-dispense mix, not a translated mix
    ahc-demo --backend sim --device opentrons.ot2   # needs the simulated robot-server (README)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from typing import Any

from mcp import Client

from ahc.viz.dashboard import serve
from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT, serial_dilution
from ahc.server import DEFAULT_SIM_MODEL, create_server

# The tools that name their plan step.
PLAN_TOOLS = {"load_layout", "verify", "pick_up_tips", "aspirate", "dispense", "mix", "drop_tips", "invoke"}

# The component the demo pipettes with, and its example layout.
DEVICES = {
  "hamilton.starlet": ("pip", STARLET_LAYOUT),
  "opentrons.ot2": ("right", OT2_LAYOUT),
}


class Refused(Exception):
  def __init__(self, text: str):
    super().__init__(text)
    match = re.search(r"\[([a-z_]+)\]", text)
    self.code = match.group(1) if match else None


PLAN = ["Load the deck layout", "Check the deck against the layout (motion gate)",
        "Show requests the layer refuses", "Put 100 uL of diluent in columns 2-12",
        "Put the stock in column 1", "Transfer down the series, mixing each column", "Verify and summarize"]


class PacedLab:
  """Calls tools like an agent would, one at a time with a pause, and narrates each result.

  Like an agent following the skill, it reports its decisions with report_decision, so the
  dashboard's agent panel fills in the offline demo too.
  """

  def __init__(self, client: Client, delay: float):
    self.client = client
    self.delay = delay
    self.phases: list = []  # armed by dilution_phases() when the dilution starts
    self.step = 1  # the plan step the next calls belong to (decide() moves it)

  def dilution_phases(self) -> None:
    """Announce each phase of the dilution when the first call that belongs to it comes up."""
    self.phases = [
      (lambda tool, a: tool == "aspirate" and a.get("targets") == ["diluent"], 4,
       "Distribute 100 uL of diluent into columns 2-12 with one column of tips",
       "Every well must end at 100 uL, so each column starts with 100 uL before the transfers arrive"),
      (lambda tool, a: tool == "aspirate" and a.get("targets") == ["stock"], 5,
       "Put 300 uL of stock in column 1", "Column 1 gives 200 uL to column 2 and keeps 100 uL"),
      (lambda tool, a: tool == "aspirate" and str(a.get("targets", [""])[0]).startswith("plate:"), 6,
       "Move 200 uL from each column to the next and mix there",
       "200 uL into 100 uL is a 1.5-fold step; fresh tips for every transfer"),
    ]

  async def decide(self, decision: str, why: str | None = None, step: int | None = None, **extra) -> None:
    if step is not None:
      self.step = step
    args = {"decision": decision, "current_step": step, **extra}
    if why:
      args["why"] = why
    await self.call("report_decision", quiet=True, **{k: v for k, v in args.items() if v is not None})
    print(f"  agent    {decision}")

  async def call(self, tool: str, quiet: bool = False, **args) -> dict[str, Any]:
    for i, (starts, step, decision, why) in enumerate(self.phases):
      if starts(tool, args):
        del self.phases[i]
        await self.decide(decision, why, step)
        break
    if tool in PLAN_TOOLS:
      args = {"plan_step": self.step, **args}
    await asyncio.sleep(self.delay)
    result = await self.client.call_tool(tool, args)
    if result.is_error:
      raise Refused(result.content[0].text)
    out = result.structured_content
    if not quiet:
      print(f"  ok       {tool:<13}{_brief(tool, args, out)}")
    return out

  async def refused(self, tool: str, **args) -> None:
    try:
      await self.call(tool, quiet=True, **args)
    except Refused as exc:
      text = re.sub(r"^Error executing tool \w+: ", "", str(exc))
      print(f"  refused  {tool:<13}{_brief(tool, args, None)}\n           {text}")
      return
    raise RuntimeError(f"{tool}({args}) was expected to be refused")


def _brief(tool: str, args: dict[str, Any], out: dict[str, Any] | None) -> str:
  parts = []
  if "targets" in args:
    parts.append(", ".join(args["targets"]))
  if "volumes" in args:
    parts.append(f"{args['volumes']:g} uL")
  if tool == "mix":
    parts.append(f"{args['repetitions']} x {args['volume']:g} uL")
  if args.get("specialized"):
    parts.append("specialized " + json.dumps(args["specialized"]))
  if out:
    if out.get("translated"):
      parts.append("[translated]")
    if out.get("commands_sent"):
      parts.append(f"{out['commands_sent']} cmds")
  return "  ".join(parts)


async def confirm_layout(lab: PacedLab, check: dict[str, Any]) -> dict[str, Any]:
  """The person confirms a layout this folder has no reference for: asked on the terminal, or, with no
  terminal to ask, the demo answers for them and says so (simulation only)."""
  await lab.decide("Wait for the person to confirm the layout", "This folder has no reference for it",
                   waiting_for="person")
  if sys.stdin.isatty():
    answer = await asyncio.to_thread(input, "           Confirm this layout (it is on the dashboard)? Type 'yes' to go on: ")
    verdict = "pass" if answer.strip().lower() in ("y", "yes") else "fail"
  else:
    print("           no terminal to ask: the demo confirms it on the person's behalf (simulation only)")
    verdict = "pass"
  result = await lab.call("record_verdict", quiet=True, check_id=check["check_id"], verdict=verdict)
  if verdict == "fail":
    raise Refused("[demo] the person did not confirm the layout, so nothing moved.")
  return result


async def script(lab: PacedLab, server, args: argparse.Namespace) -> None:
  view = server.lab.view
  overview = await lab.call("lab_overview", quiet=True)
  config = overview["config"]
  print(f"workspace  {overview['workspace']}")
  print(f"config     {config['status']}: {config['reason']}")
  model = config["devices"][0]["model"] if config.get("devices") else (args.device or DEFAULT_SIM_MODEL)
  if model not in DEVICES:
    raise Refused(f"[demo] the demo has no example layout for {model}.")
  component, layout = DEVICES[model]
  if args.native_mix and model != "hamilton.starlet":
    raise Refused("[demo] --native-mix uses STAR-only parameters.")

  await lab.decide(f"Run a 1.5-fold serial dilution on the {model}", "Columns 1-11 hold the series and column 12 the blank; "
                   "every well ends at 100 uL", step=1, plan=PLAN)
  print("\n1. load the deck layout")
  loaded = await lab.call("load_layout", quiet=True, layout=layout)
  address = f"{server.lab.spec.prefix}.{component}"
  print(f"device     {server.lab.spec.title} ({'simulated' if server.lab.adapter.simulated else 'REAL'})")
  if "live_view_url" in loaded:
    print(f"3D view    {loaded['live_view_url']}")
    if not args.no_browser:
      print("           waiting for the 3D view to connect…")
      if not await view.wait_for_browser(60):
        print("           (no browser yet; carrying on)")
  print(f"           labware {', '.join(loaded['labware'])}; aliases {loaded['aliases']}")
  if loaded.get("needs_person"):
    print(f"           no reference for it in this folder: the person confirms it, then it is saved as "
          f".ahc/layouts/{loaded['saved_as']}.yaml; motion gate {loaded['gate']['state']}")
  else:
    print(f"           saved as .ahc/layouts/{loaded['saved_as']}.yaml; motion gate {loaded['gate']['state']}")
  await asyncio.sleep(args.pause)

  print("\n2. the motion gate: nothing moves until the deck matches the layout")
  await lab.decide("Check the deck against the layout before anything moves",
                   "The server refuses every motion until deck_matches_layout passes", step=2)
  await lab.refused("pick_up_tips", device=address)
  deck = await lab.call("verify", quiet=True, check="deck_matches_layout")
  if deck["verdict"] == "pending":
    print(f"  pending  deck_matches_layout  (judged by {deck['judged_by']}); the layout is on the dashboard")
    deck = await confirm_layout(lab, deck)
  print(f"  {deck['verdict']:<8} deck_matches_layout  (judged by {deck['judged_by']}) -> gate {deck['gate']['state']}")
  await asyncio.sleep(args.pause)

  print("\n3. requests the layer refuses before anything moves")
  await lab.decide("Show two requests the server refuses", "2000 uL is above the channel limit; an empty well has nothing "
                   "to draw", step=3)
  await lab.call("pick_up_tips", device=address)
  await lab.refused("aspirate", device=address, targets=["diluent"], volumes=2000)
  await lab.refused("aspirate", device=address, targets=["plate:A1:H1"], volumes=100)
  await lab.call("drop_tips", device=address, mode="return")
  await asyncio.sleep(args.pause)

  print(f"\n4. serial dilution (x1.5, 11 columns + blank){' with native STAR mixing' if args.native_mix else ''}")
  lab.dilution_phases()
  await serial_dilution(lab, address, native_mix=args.native_mix)
  await asyncio.sleep(args.pause)

  print("\n5. verification (judged by the simulator here; by the person on site on hardware)")
  await lab.decide("Verify the plate and summarize the run", "Every well should hold liquid and no tips stay mounted", step=7)
  for check, extra in (("liquid_present", {"targets": ["plate:A1:H12"], "min_volume_ul": 99}),
                       ("tips_mounted", {"device": address, "expect": "none"})):
    result = await lab.call("verify", quiet=True, check=check, **extra)
    print(f"  {result['verdict']:<8} {check}  (judged by {result['judged_by']})")
  await lab.decide("Done: columns 1-11 hold the 1.5-fold series, column 12 the blank, every well 100 uL",
                   "The plate checks passed and no tips are left on", plan_done=True)

  run_summary = await lab.call("get_run", quiet=True)
  print("\n6. run summary")
  for key in ("liquid_handling_calls", "basic_only", "with_specialized", "translated", "refused",
              "commands_sent"):
    print(f"  {key:<22}{run_summary[key]}")
  print(f"  log                   {run_summary['log']}")
  print("\n7. what this task folder now holds")
  root = server.lab.ws.dir
  for path in sorted(root.rglob("*")):
    if path.is_file():
      print(f"  .ahc/{path.relative_to(root)}")


async def run(args: argparse.Namespace) -> None:
  logging.getLogger("mcp.server.mcpserver.server").setLevel(logging.WARNING)  # refusals are narrated
  server = create_server(args.workspace, "sim", args.device, {"host": args.ot2_host, "port": args.ot2_port},
                         view=not args.no_3d)
  view = server.lab.view
  if view is not None:
    view.open_browser = not args.no_browser
    view.port = args.view_port
  try:
    serve(server.lab.ws.runs_dir, None, args.dashboard_port, not args.no_browser)
    print(f"dashboard  http://127.0.0.1:{args.dashboard_port}/")
  except OSError:
    print(f"dashboard  port {args.dashboard_port} is taken; a running ahc-dashboard follows this run too")

  stopped = None
  async with Client(server) as client:
    try:
      await script(PacedLab(client, args.step_delay), server, args)
    except Refused as exc:  # caught here: past the client's task group it arrives wrapped
      stopped = exc
      if exc.code == "emergency_stop" and not args.exit_when_done:
        print("\nEMERGENCY STOP: the demo stopped. Both views stay up; release it on the dashboard, Ctrl-C to quit.")
        try:
          await asyncio.Event().wait()
        except asyncio.CancelledError:
          pass
    else:
      if not args.exit_when_done:
        print("\nDone. Both views stay up for inspection; Ctrl-C to stop.")
        try:
          await asyncio.Event().wait()
        except asyncio.CancelledError:
          pass
  if stopped is not None:
    raise SystemExit("stopped: " + re.sub(r"^Error executing tool \w+: ", "", str(stopped)))


def main() -> None:
  parser = argparse.ArgumentParser(description="Paced, visual serial-dilution demo (simulation only).")
  parser.add_argument("--workspace", default=None, help="task folder (default: AHC_WORKSPACE, else the current folder)")
  parser.add_argument("--backend", choices=["sim"], default="sim", help="the demo only simulates")
  parser.add_argument("--device", choices=sorted(DEVICES), default=None,
                      help=f"model when the folder has no config yet (default {DEFAULT_SIM_MODEL})")
  parser.add_argument("--native-mix", action="store_true",
                      help="STARlet: mix with the dispense's post_mix_* parameters (no translation)")
  parser.add_argument("--step-delay", type=float, default=0.4, help="seconds before each call")
  parser.add_argument("--pause", type=float, default=2.0, help="seconds between demo sections")
  parser.add_argument("--no-browser", action="store_true", help="print the links instead of opening them")
  parser.add_argument("--no-3d", action="store_true", help="dashboard only")
  parser.add_argument("--dashboard-port", type=int, default=8770)
  parser.add_argument("--view-port", type=int, default=1338, help="3D viewer page; its websocket uses the next port")
  parser.add_argument("--ot2-host", default=None, help="OT-2 robot-server host override")
  parser.add_argument("--ot2-port", type=int, default=None, help="OT-2 robot-server port override")
  parser.add_argument("--exit-when-done", action="store_true")
  args = parser.parse_args()
  sys.stdout.reconfigure(line_buffering=True)  # narration shows up even when run in a background shell
  try:
    asyncio.run(run(args))
  except KeyboardInterrupt:
    pass


if __name__ == "__main__":
  main()
