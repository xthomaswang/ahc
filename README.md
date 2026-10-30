# Agentic Hardware Control (AHC)

AHC lets an AI agent drive liquid-handling robots through one MCP server. The agent asks for general
operations (pick up tips, aspirate, dispense, mix, drop tips) on a device component; the server
checks every call against that device's limits before anything moves, keeps a run log, and shows
the run live on a dashboard. Because the rules live in the server, they hold for any MCP client; on
real hardware the agent must have no other way to the robot (see
[An agent on the real OT-2](#an-agent-on-the-real-ot-2)).

The repository is also a Claude Code plugin: install it, open Claude Code in a folder for a task,
and ask for the lab work, any liquid handling the device can do.

**Status: simulation only.** Two devices are supported, both simulated:

- a Hamilton STARlet, through PyLabRobot's firmware-level simulator (a real one is planned over USB
  from the Mac, as the optional extra `ahc[usb]`, after the OT-2);
- an Opentrons OT-2, through Opentrons' own robot-server with a virtual motor controller.

Nothing here has run on a physical instrument yet. The path to a real OT-2 is in place and rehearsed
against the simulator (see [A real OT-2](#a-real-ot-2-bring-up)); it is the first to be tried on
hardware.

## How it works

- **One capability layer.** Operations are addressed to a component (`starlet.pip`, `ot2.right`)
  with basic parameters: targets, volumes, flow rate, liquid height. Device-specific parameters are
  there on request (`get_params`), and an operation a device does differently is translated and
  reported as such.
- **Description files.** Each device model has a Markdown file
  (`src/ahc/devices/<brand>/<model>.md`) that lists its components, layout rules and every
  parameter's limits next to how the parameter is used. The server enforces them on every call.
- **Task folders.** Everything about a task stays in its folder's `.ahc/`: device config, layouts,
  protocols, run logs and simulator state. Tasks never affect each other.
- **Safety, enforced by the server:**
  - A real backend runs only from a config the person confirmed, after seeing a one-line summary of
    the device, where it is and the task limits. Task limits can only tighten a device's limits.
  - Unless simulation is forced, a server session moves nothing before the person's go-ahead: on a
    real robot in every session, in simulation until the folder has run its first experiment.
  - The agent reports a plan before it loads or moves anything, and every call names its plan step.
  - After every layout load nothing moves until the deck check passes. In simulation the simulator
    judges it; on real hardware the person on site does. In simulation the person also confirms a
    layout whose deck placement matches none the task folder already has.
  - On real hardware nothing moves while a tip or liquid check waits for the person, and no new run
    starts while tips may still be on the pipettes.
  - The emergency stop (dashboard button or `ahc-estop`) makes the server send nothing more and
    refuse every action until a person releases it. On an OT-2 it also stops AHC's run, but the
    robot first finishes the run command it is executing: the robot's power switch is the
    immediate stop (see [Emergency stop](#emergency-stop)).

## Install (Claude Code)

```
/plugin marketplace add xthomaswang/ahc
/plugin install ahc@agentic-hardware-control
```

Needs [uv](https://docs.astral.sh/uv/) on `PATH`. The first session builds the server's Python
environment (a few seconds on a fast connection; later starts take under a second).

## Use

Open Claude Code in a folder for the task and ask for the work in your own words, for example:

- "Add 50 uL of buffer to plate columns 2 to 4."
- "Normalize the samples in plate column 1 to 100 uL with water, using the volumes in
  `.ahc/protocols/volumes.csv`."
- "Set up the standards and the samples in triplicate as in `.ahc/protocols/plate-map.md`."

The agent follows the plugin's `ahc:lab-hardware` skill, whose planning patterns cover distributing
a reagent, per-well volumes from a table, replicates, dilution series, mixes, tip changes and tip
budgets:

1. `lab_overview` shows the task folder, its device config and the next step.
2. In a new folder the agent asks you whether to simulate or use a real robot, and which device. It
   writes your choice with `configure_devices`, and you confirm it (see
   [Simulation or a real robot](#simulation-or-a-real-robot)).
3. `find_layout_references` looks for protocols and earlier layouts in this folder: put plate maps,
   volume tables and protocol notes in `.ahc/protocols/`. It also returns `last_deck`, the deck the
   last run ended with (volumes, used tip spots) as a layout, so the next run continues from it
   instead of the agent writing that state by hand. You confirm it at the deck check as any layout
   (`ask_person` lists the volumes and the empty and returned tip spots); with a top camera
   configured, `capture` can show you the plate there.
4. The agent reports its plan as concrete steps. The server refuses `load_layout` and every action
   until then; after that, every call names its plan step and the dashboard ticks each step as the
   agent moves on.
5. `load_layout`, then `verify(check="deck_matches_layout")`: the motion gate. On a real robot you
   check every deck. In simulation the simulator checks it, but you confirm a layout whose deck
   placement (the labware type at each position, carriers included) matches none of the folder's
   layouts, such as a first run from the built-in example or a changed deck. The folder's layouts
   are those saved in `.ahc/layouts/` and those whose deck check passed in an earlier run; protocol
   files guide a layout but are never matched. The agent calls `record_verdict` with a note listing
   the labware, positions and liquids (it is on the dashboard too), and Claude Code shows you the
   approval dialog with what differs from the closest earlier layout: your approval is the
   confirmation, a denial is not (the agent then asks what you see and records a fail with your
   words). Once confirmed, the layout is saved, and later layouts with the same placement are
   checked by the simulator. Labware names, liquids and volumes do not count, and a saved layout
   loaded by name never needs you.
6. Liquid handling. Targets name wells, ranges, columns (`plate:col2`, A2 to H2) and rows
   (`plate:rowB`, B1 to B12), and every result says where it went (`plate: column 2 (A2-H2)`).
7. `verify(check="volumes_match", expected={...})` compares the final volumes the plan expects with
   the digital twin; then `report_decision(plan_done=true)` with the result. That report and `get_run` return `timing` (run total, per-step durations, robot / agent / others split, where others is waiting for the person plus stops and their recovery; the agent and others parts are estimates), and the dashboard shows it live.

The task folder then holds:

```
.ahc/config.yaml      the device, task limits, confirmation, simulator version
.ahc/layouts/         layouts the agent wrote (one that needed you: once you confirmed it)
.ahc/protocols/       protocols you put here for the agent to consult
.ahc/runs/            run logs
.ahc/sim/             this task's OT-2 simulator
.ahc/state/deck.json  the twin's deck after the last call that changed it (volumes, tip spots, unknown wells)
.ahc/tips-left.json   on a real robot: tips that may still be on the pipettes
```

### Simulation or a real robot

The server follows the task folder's config. Without one nothing moves: the agent asks the person
whether to simulate or use a real robot, and which device, writes the choice with
`configure_devices` and shows them `config.confirm_summary`, one line naming the device, where a
real one is, what each OT-2 mount carries and the task limits. The agent then calls `confirm_config`
with that summary right away; Claude Code asks the person to approve the call and shows it. Their
approval is the confirmation (a denial is not), and a summary that no longer matches the config is
refused. Only then can anything move. Task limits in the config tighten the
device's limits (for example `starlet.pip: {volume_ul: {max: 200}}`) and can never loosen them.

Where it is needed, the person also gives each server session a go-ahead before anything moves:
their approval of `confirm_config` in that session, even for a config they confirmed before (a
confirmation of a new config earlier in the session counts). Claude Code starts a new server for
every session, on `--resume` and on a `/mcp` reconnect.

- On a real robot it is needed in every session, because `load_layout` homes the robot.
- In simulation it is needed only while the folder has never run an experiment (no layout has
  loaded in it yet).
- Under forced simulation (below) it is never needed.
- Until it is given, `load_layout` and every action are refused (`session_unconfirmed`);
  `lab_overview` says whether this session needs it.

To switch, tell the agent: it writes the other device entry with `configure_devices` (a `sim` entry
of the same model to simulate; the robot entry again to go back, which reads the robot's identity
again), and you confirm it.

`AHC_BACKEND=sim` forces simulation, for headless runs, evals (`claude -p`) and CI: put it in
Claude Code's environment (`AHC_BACKEND=sim claude -p ...`), or start `ahc-mcp --backend sim`. A
STARlet simulation config is then created on first use without asking (`configure_devices` picks
another model), a robot the config names is simulated on this task's simulator instead, and no
go-ahead is needed. Without it, `claude -p` moves nothing in a folder that needs the go-ahead:
Claude Code refuses `confirm_config` where nobody can answer.

In a client that shows no approval dialog on each call, the agent asks in chat first and calls the
tool with what you said; AHC cannot tell the difference, so such a client's own approval settings
are all that stand between the agent and those calls.

## Commands for the person

Besides the MCP server, AHC has commands a person runs at a terminal: `ahc-dashboard`, `ahc-estop`,
`ahc-sim`, `ahc-smoke` and `ahc-demo`. Under the plugin they are not on `PATH`. Run them as
`uv run --project <plugin checkout> <command>` (the folder Claude Code installed the plugin into),
from a clone as `uv run --project <clone> <command>`, or without either as
`uvx --from git+https://github.com/xthomaswang/ahc <command>`. The server's hints give the absolute
path of the plugin's `ahc-sim`; the other commands sit in the same folder.

The dashboard needs no command: the agent calls the `open_dashboard` tool, which serves it from the MCP
server (port 8770, or the next free one on 127.0.0.1) and opens it in your browser. It ends with the server.

## The OT-2 simulator

The OT-2 runs against Opentrons' robot-server, installed once for all tasks and run once per task
folder. The agent is guided through this; by hand:

```bash
ahc-sim ot2 --setup --dry-run   # what it downloads (~270 MB on disk, into ~/.ahc; AHC_HOME moves it)
ahc-sim ot2 --setup             # install once
ahc-sim ot2                     # in the task folder: run this task's simulator (keeps running)
ahc-sim ot2 --status
```

The simulator starts with the default pair (a P300 8-channel on `ot2.right`, a P20 single channel on
`ot2.left`) unless the config's `options.mounts` names other pipettes. To rehearse with the robot's
pipettes, copy its `options.mounts` into the `sim` entry; `ahc-sim ot2` gives the simulator those
pipettes each time it starts, so restart it after changing them.

## A real OT-2 (bring-up)

The OT-2's real backend, `robot`, talks to the robot's own robot-server over the lab network: the
same HTTP API the simulator serves. A task folder's config names the robot, and only the person's
confirmation lets it run:

```yaml
devices:
  - id: ot2
    model: opentrons.ot2
    backend: robot
    options: {host: 192.168.1.20, port: 31950, robot_name: <the name /health reports>}
```

The agent can look for the robot with the `find_robots` tool (an mDNS browse, then a `GET /health` on
each candidate; read-only, and the only way an agent without a shell looks): you say which robot the
task uses.

Before writing the config, `configure_devices` reads the robot's name and what each mount carries (no run, no
motion) and fills in `options.robot_name` and `options.mounts`. A mount with a pipette AHC does not
describe stays unused; a robot with none AHC drives is refused (`no_usable_pipette`), as is an entry
that names a robot when another one answers (`wrong_robot`). The person sees that identity and the
mounts before they confirm. With `robot_name`
set, the server refuses any other robot at that address. If the robot's pipettes change after the
person confirmed, `load_layout` is refused (`mount_mismatch`): write the config again with
`configure_devices` and have the person confirm it.

On a real robot:

- Every server session starts with the person's go-ahead (see
  [Simulation or a real robot](#simulation-or-a-real-robot)).
- `load_layout` starts a new run and homes the robot, before the deck check: the person keeps hands
  and the deck clear. It has no dialog of its own, so the agent says in chat, in the same message
  before the dialog right ahead of it (the tips note or `confirm_config`), that approving also means
  hands and deck are clear and the robot homes next; with no such dialog it asks in chat.
- A new run does not know the tips an earlier run left on the pipettes, and its first pick-up would
  drive them into a rack. So AHC notes the tips in `.ahc/tips-left.json` from each pick-up until
  they are dropped, and after a stop or an unknown outcome (the note outlives a crash), and
  `load_layout` is refused while tips may be on (`tips_unconfirmed`). Tips the current run still
  holds are dropped with `drop_tips` first. Otherwise the refusal says which pipette, how many tips,
  when and why; the person removes them by hand (or sees there are none), and the agent asks for
  their verdict on one check for both pipettes, `verify(check="tips_mounted", expect="none")`
  without a device. Their pass clears the note.
- Every deck check, tip check and liquid check waits for the person's verdict, and nothing moves
  while a tip or liquid check waits (`check_pending`). `verify` returns `ask_person` with it, one
  sentence of what the person confirms (each slot's labware and liquids; "No tip on either pipette
  (ot2.left, ot2.right)"; "8 tips seated straight on ot2.right"; the wells and minimum volume), and
  the agent passes it as the note of `record_verdict(verdict="pass")`. Claude Code's approval
  dialog for that call is the question: approving passes the check, denying does not. After a
  denial the agent stops, asks what you see and records a fail with your words, which asks you
  again. After the person fails a tip check, only `drop_tips` moves until the tips are dropped or a
  later tip check on that pipette passes; after a failed liquid check, only `drop_tips` and
  `load_layout` move until a later check of those wells passes or a new layout is loaded
  (`check_failed`). `volumes_match` is the exception: it compares the expected volumes with the
  digital twin, which holds what was sent, not what was measured.
- An omitted liquid height is the description file's default, 1 mm above the cavity bottom on the
  OT-2, and every result records the height used. An aspirate or mix that would leave the tips above
  the liquid is refused.
- Positions come from the labware definitions; Labware Position Check offsets are not applied.
- AHC stops only its own run: nobody runs the Opentrons App on the robot during an AHC run.

The first runs use `ahc-smoke`, a scripted operator tool with a fixed protocol (water, then a
3-column dye dilution), not the agent's path. It goes through the same server as an agent, one stage
at a time, and asks the person at the terminal for every confirmation:

- It refuses to run under an agent (`CLAUDECODE` or `AI_AGENT` set) or without a terminal.
- When the config needs the person's confirmation, it shows the config summary and, once they say
  yes, confirms exactly that with `confirm_config`; it never confirms on its own.
- Each stage is a new server session. A stage that moves asks the person for that session's
  go-ahead (the same summary, recorded with `confirm_config`), unless they confirmed the config in
  that stage; `--stage connect` moves nothing and asks for none.
- Before the robot homes, it checks the stage's volumes, heights and mix repetitions (and any
  flow-rate task limit) against the limits the server applies, so a limit stops the stage before
  anything moves rather than half-way through it.
- It asks before `load_layout` homes the robot. When `load_layout` is refused because tips an
  earlier stage left may still be on (`tips_unconfirmed`), it shows why, asks the person to take any
  off both pipettes, records their answer on the check of every pipette, and asks again before the
  robot homes.
- After a refusal, a stop or Ctrl-C it says whether tips may still be on the pipette and what the
  run used.

```bash
ahc-smoke --host <robot address> --stage connect   # identity; the person confirms the config. Nothing moves.
ahc-smoke --stage init         # + plan, layout (the robot homes, after asking) and the person's deck check
ahc-smoke --stage tips         # + pick up tip column 1, the person checks it, put it back
ahc-smoke --stage water        # + 100 uL of water into plate column 12 (tip column 2)
ahc-smoke --stage dilution     # + a 3-column dye dilution, not the water: plate columns 1-3 (tip columns 3-5)
```

The deck: slot 1 an Opentrons 300 uL tip rack, slot 2 a Corning 2 mL deep-well plate with dye in
column 1 and water in column 2 (about 1 mL per well), slot 3 an empty Corning 360 uL flat-bottom
plate, and the trash in slot 12. Each stage prints which tip columns must be full and which plate
columns empty. Rehearse against a simulator first: run `ahc-sim ot2` in another folder, then
`ahc-smoke --host 127.0.0.1 --port <its port> --stage dilution` in the task folder. The connect and
init stages work with any pipettes. The tips, water and dilution stages are written for a P300
8-channel on the right mount; on a robot with other pipettes they stop before anything moves.

### An agent on the real OT-2

The plugin's server follows the task folder's config, so any Claude Code session in a folder whose
config names a robot can reach it once the person gives that session's go-ahead. Start every session
in such a folder like this:

```bash
claude --permission-mode default --disallowedTools Bash WebFetch
```

- `--permission-mode default` (shown as Manual): Claude Code asks you before tool calls instead of
  approving them itself. Recent versions start a terminal session in auto mode otherwise. Never use
  auto, bypass or accept-edits mode in a session that can reach a robot, and don't switch to them.
- `--disallowedTools Bash WebFetch`, plus any browser, computer-use or code-execution tools your
  setup adds (deny rules such as `"mcp__<server>__*"`; `/mcp` lists the names): the robot-server has
  no authentication, so a shell or a web tool could reach the robot around every AHC rule. Never deny
  `"mcp__*"`, which removes AHC's own tools too.
- The agent never runs `ahc-smoke`, `ahc-estop` or `ahc-sim` itself; you run them at a terminal.

The agent writes the robot you name with `configure_devices` and calls `confirm_config` at once. In
every later session (also after `--resume` or a `/mcp` reconnect) it calls it again before anything
moves, and your approval of it is that session's go-ahead. Claude Code asks you to approve
`confirm_config` and `record_verdict` on every call, in every permission mode and despite allow
rules: that dialog is the confirmation, so the agent does not wait for you to type "confirm" first,
and each deck, tip and liquid check reaches you as a `record_verdict` dialog whose note says what you
confirm. Deny it and the check is not passed. Without a person to answer, as in `claude -p`, Claude
Code refuses them, so a headless session moves nothing on the robot.
Run the `ahc-smoke` stages first, so the robot, the deck and your checks are known to work before an
agent chooses the calls. Other MCP clients reach the robot the same way: `ahc-mcp` without
`--backend sim`, or with `--backend config`.

## Cameras (opt-in)

A task can declare cameras in `.ahc/config.yaml`; with none declared nothing of this is active. They are
part of the config the person confirms (a change sends it back to pending):

```yaml
cameras:
  - {name: top, device: "USB Camera"}       # name: lowercase id; device: the exact macOS camera name
  - {name: side, device: "Intel(R) RealSense(TM) Depth Camera 435 with RGB Module RGB"}
```

The agent writes them with `configure_devices(cameras=[...])` (omitted: the config's cameras stay; `[]`
removes them) after the person says which camera is which. Each name is checked against the AVFoundation
devices (`ffmpeg -f avfoundation -list_devices true -i ""`): `camera_not_found` lists the available ones,
and without macOS or ffmpeg (`brew install ffmpeg`) it is `camera_unavailable`.

The agent decides when to look: `capture(label, plan_step=None, cameras=None, final=False)` takes one
frame from each named camera (all when omitted), one after another, and returns the images inline (at
most 1280 px wide) with their paths. Full-size JPEGs stay next to the run log:

```
.ahc/runs/<run id>.jsonl
.ahc/runs/<run id>/0_preset/                  before any plan step
.ahc/runs/<run id>/<k>_<step text>/           plan step k
.ahc/runs/<run id>/<N+1>_results/             final=true, N = the plan's step count
    <camera>_<HHMMSS>.jpg, captures.jsonl     one line per image: time, camera, device, file, label, plan_step, final
```

`verify(..., images=[paths from capture])` keeps photos as the check's `evidence.camera`, also listed by
`list_checks` and in the run log. Nothing changes about who judges: on real hardware the person still
records the verdict. Notes:

- Captures are refused in simulation (`camera_not_in_simulation`): a camera shows the real deck.
- A capture is no motion, so it still runs while the emergency stop is engaged: the Claude Code hook
  (`hooks/estop-guard.sh`) lets `capture` (and the ToolSearch that loads it) through and denies every
  other call, which ends the agent's turn.
- Images stay on this computer, unmasked and never uploaded by AHC; the client shows them to its model.
- macOS asks once whether the terminal (or Claude Code) may use the camera.

## Dashboard and demo

The agent opens the dashboard itself with `open_dashboard` (its URL is in `lab_overview.dashboard`).
To run it, or the demo, by hand, use a task folder (see [Commands for the person](#commands-for-the-person)):

```bash
uv run --project <repo> ahc-dashboard           # follows this folder's newest active run log
ahc-demo --backend sim                          # paced serial dilution, 3D view and dashboard
ahc-demo --backend sim --device opentrons.ot2   # needs this folder's OT-2 simulator running
```

`ahc-demo` is a scripted demo with a fixed protocol, a 1.5-fold serial dilution
(`src/ahc/examples.py`): it shows the server at work, not how an agent plans.

The dashboard belongs to one task folder and names it, as an absolute path, in its title and its
stop bar. If its port (8770) is taken by something else, it binds a free one and says so. It shows,
from top to bottom:

- the emergency stop button and, once pressed, whether the server took the stop and what the device
  reported;
- the agent: what it was asked, what it is doing, its plan with each step's progress, and the checks
  waiting for you;
- the task folder, config, layout, motion gate and last hardware event;
- the plate, reservoirs and tips (tips returned to a rack are marked as returned, never as fresh);
- the task's configuration and layout;
- every hardware call, newest first.

Concentrations are the digital twin's estimate (complete mixing; liquid behind a `stock` alias
= 1), not measurements. `AHC_VIEW=1 claude` also opens a live 3D view of the device when the agent
loads a layout.

In Claude Code the agent panel updates live. The plugin's hooks add what the agent was asked, what
it says, which tool it is calling and its final answer, from its first AHC call on. They copy that
text into `.ahc/agent-feed.jsonl` in the task folder; for other tools only the tool's name is
recorded, never shell commands or file contents. `AHC_FEED=0 claude` turns this off.

## Emergency stop

Press the red button on the dashboard (the agent opens it with `open_dashboard`), or run
`uv run --project <repo> ahc-estop` in the task folder (add `--workspace <folder>` from anywhere;
`lab_overview.emergency_stop.how` has the exact command and the folder of the server the agent uses).
`ahc-estop` says whether a server took the stop and what the device reported.

- The server sends the device nothing more and refuses every action, configuration change and check
  until a person releases it; reading state and `report_decision` still work. In Claude Code the
  plugin's hook also stops the agent at its next tool call.
- On an OT-2 the stop also ends AHC's run on the robot, on a connection of its own, so it reaches a
  run that is still being set up. The robot halts only once the run's command in flight has run to
  its end: a move, an aspirate or dispense, a tip pick-up. The home at the start of `load_layout` is
  not a run command; by Opentrons' code a stop during it halts the robot at once, which has not been
  tried on a robot yet. The robot's power switch is the immediate stop; the door switch and the front
  button do not affect AHC's run.
- The stop reaches only runs AHC started: when another client's run (the Opentrons App's) has
  replaced AHC's as the robot's current run, it is logged as `device_halt_failed`
  (`other_run_active`).
- A call the stop cut short on real hardware has an unknown outcome: the person checks the tips and
  the wells.
- On the simulated STARlet the stop ends the call in the simulator.

Releasing is the person's: the dashboard's Release, after a confirmation, or
`uv run --project <repo> ahc-estop --release`
at a terminal, which refuses to run under an agent (`CLAUDECODE` or `AI_AGENT` set) or without a
terminal. Only a release of the stop the server holds clears it: if the stop file is replaced by a
release of another stop, the server writes its stop back and the person releases once more. After a
release the deck is checked again before anything moves. On an OT-2 the layout is loaded again (a new
run, which homes the robot); the robot no longer knows about tips left on the pipettes, so the person
takes them off by hand first and confirms it with the tips check (see
[A real OT-2](#a-real-ot-2-bring-up)): until then `load_layout` is refused (`tips_unconfirmed`).

## Other MCP clients

Everything lives in the server, so any MCP client (Codex, pi, ...) works the same way: run it in the
task folder.

```bash
uvx --from git+https://github.com/xthomaswang/ahc ahc-mcp --backend sim
```

`--backend sim` forces simulation; without it the server follows the folder's config, as the
plugin's does. `confirm_config` and `record_verdict` carry a flag that makes Claude Code ask the
person on every call. Other clients ignore it: only their own approval settings stand between the
agent and those two calls, the session's go-ahead included. AHC's instructions tell the agent to ask
in chat first in a client that shows no such dialog.

## Development

```bash
uv sync
uv run pytest                    # the real-OT-2 path runs against a fake robot-server
claude --plugin-dir .            # this checkout as the plugin
claude plugin validate .
```

`AHC_TEST_OT2_PORT=<port of an ahc-sim ot2 started for testing>` also runs the tests against an
OT-2 simulator; when nothing answers there, the run fails instead of skipping them.
`AHC_TEST_OT2_CHECKOUT=<opentrons-ot2 checkout>` also runs the test that starts two task simulators.

| Path | What it is |
|---|---|
| `src/ahc/server.py` | the MCP server and its tools |
| `src/ahc/core/` | capability layer, layouts, errors |
| `src/ahc/devices/` | adapter interface, description files, one folder per brand |
| `src/ahc/devices/opentrons/sim/` | `ahc-sim ot2` |
| `src/ahc/workspace/` | task folders: config, limits, layouts, references, emergency stop |
| `src/ahc/verification/` | checks and the motion gate |
| `src/ahc/plan.py` | the agent's plan and its progress |
| `src/ahc/smoke.py` | `ahc-smoke`: scripted first runs on a real OT-2 |
| `src/ahc/examples.py` | the demo's fixed protocol and example layouts |
| `src/ahc/viz/` | dashboard, 3D view, demo |
| `.claude-plugin/`, `skills/`, `hooks/` | the Claude Code plugin: manifest, skill and its planning patterns, hooks |

## Known limits

- **Not yet run on a physical instrument.** The OT-2's real path is rehearsed against the simulator
  only. The STARlet is simulated only: its real backend, USB from the Mac as the optional extra
  `ahc[usb]`, comes after the OT-2's bring-up and is not implemented yet.
- **The OT-2 stop does not interrupt a run command in flight** (see above); the power switch does.
- **`load_layout` homes the OT-2 before the person checks the deck.**
- **Plans and checks live in the server's memory.** A new server session (a new Claude Code session,
  `--resume`, a `/mcp` reconnect) forgets the plan, the deck check, any check waiting for the person
  and what a check they failed holds back: the agent loads the layout again and the deck is checked
  again. What is on disk carries over: the config, layouts, run logs and the tips note (tips the
  person found in a failed tip check included).
- **The digital twin holds what was sent, not what happened.** After a stop or an unknown outcome on
  real hardware, the person checks the tips and the wells the call touched; the robot's next run does
  not know about tips left on the pipettes, so `load_layout` waits until the person confirms the
  pipettes are empty.
- **No authentication on the robot.** AHC's checks hold only for calls that go through AHC; see
  [An agent on the real OT-2](#an-agent-on-the-real-ot-2) for keeping the agent to them.
- **A slow robot slows the calls, not the stop.** The robot's HTTP calls run off the server's event
  loop, so the emergency stop's watcher keeps running. A call still waits for the robot: AHC's own
  requests up to 10 s each (5 s for `configure_devices`' identity check), PyLabRobot's commands up to
  30 s or, for a liquid move, its volume divided by the flow rate plus 30 s, and a stop up to 30 s for
  the robot to report its run stopped. Other tool calls wait behind it.
- **First-start timeout.** Claude Code waits 30 s for an MCP server to start. On a slow network the
  first start after an install or update can take longer while uv downloads; reconnect with `/mcp`.
- **One device per server.**
- **`AHC_DEVICE` conflicts.** If `AHC_DEVICE` is set and a folder's config names another model, the
  server reports `device_conflict` rather than guessing.
- **Live agent text is Claude Code only.** Other clients show their plan and steps, their reported
  decisions and the hardware calls. A step is ticked when the agent moves on to the next one, a few
  seconds after it actually ends.

## License

MIT; see [LICENSE](LICENSE).
