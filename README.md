# Agentic Hardware Control (AHC)

AHC lets an AI agent drive liquid-handling robots through one MCP server. The agent asks for general
operations (pick up tips, aspirate, dispense, mix, drop tips) on a device component; the server
checks every call against that device's limits before anything moves, keeps a run log, and shows
the run live on a dashboard. Because the rules live in the server, they hold for any MCP client.

The repository is also a Claude Code plugin: install it, open Claude Code in a folder for a task,
and ask for the lab work.

**Status: simulation only.** Two devices are supported, both simulated:

- a Hamilton STARlet, through PyLabRobot's firmware-level simulator;
- an Opentrons OT-2, through Opentrons' own robot-server with a virtual motor controller.

Nothing here has run on a physical instrument yet; bringing up a real OT-2 comes first.

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
  - A real backend runs only from a config the person confirmed. Task limits can only tighten a
    device's limits.
  - The agent reports a plan before it loads or moves anything, and every call names its plan step.
  - After every layout load nothing moves until the deck check passes. In simulation the simulator
    judges it; on real hardware the person on site does. In simulation the person also confirms a
    layout the task folder has no reference for.
  - An emergency stop (dashboard button or `ahc-estop`) halts the server's work at once until a
    person releases it.

## Install (Claude Code)

```
/plugin marketplace add xthomaswang/ahc
/plugin install ahc@agentic-hardware-control
```

Needs [uv](https://docs.astral.sh/uv/) on `PATH`. The first session builds the server's Python
environment (a few seconds on a fast connection; later starts take under a second).

## Use

Open Claude Code in a folder for the task and ask for the work, for example "run a 1.5-fold serial
dilution across a plate". The agent follows the plugin's `ahc:lab-hardware` skill:

1. `lab_overview` shows the task folder, its device config and the next step.
2. The plugin forces simulation, so a STARlet simulation config is created on first use;
   `configure_devices` picks the OT-2 instead.
3. `find_layout_references` looks for protocols and earlier layouts in this folder.
4. The agent reports its plan as concrete steps. The server refuses `load_layout` and every action
   until then; after that, every call names its plan step and the dashboard ticks each step as the
   agent moves on.
5. `load_layout`, then `verify(check="deck_matches_layout")`: the motion gate. If the folder has no
   reference for the layout (a first run from the built-in example), you confirm it. The agent shows
   it to you (it is on the dashboard too) and waits; answer in the chat, and the agent records your
   answer with `record_verdict`, a call Claude Code asks you to approve. Once confirmed, the layout
   is saved and later layouts in the folder are checked by the simulator.
6. Liquid handling, then `report_decision(plan_done=true)` with the result.

The task folder then holds:

```
.ahc/config.yaml      the device, task limits, confirmation, simulator version
.ahc/layouts/         layouts the agent wrote (one without a reference: once you confirmed it)
.ahc/protocols/       protocols you put here for the agent to consult
.ahc/runs/            run logs
.ahc/sim/             this task's OT-2 simulator
```

Outside simulation the config starts pending: the agent writes the device the person chose with
`configure_devices`, and only the person's `confirm_config` lets anything move. Task limits in the
config tighten the device's limits (for example `starlet.pip: {volume_ul: {max: 200}}`) and can
never loosen them.

## The OT-2 simulator

The OT-2 runs against Opentrons' robot-server, installed once for all tasks and run once per task
folder. The agent is guided through this; by hand:

```bash
ahc-sim ot2 --setup --dry-run   # what it downloads (~270 MB on disk, into ~/.ahc; AHC_HOME moves it)
ahc-sim ot2 --setup             # install once
ahc-sim ot2                     # in the task folder: run this task's simulator (keeps running)
ahc-sim ot2 --status
```

Under the plugin, `ahc-sim` is not on `PATH`; the server's hints give its full path.

## Dashboard and demo

Run these in a task folder. From a clone, prefix them with `uv run --project <clone>`; otherwise use
`uvx --from git+https://github.com/xthomaswang/ahc <command>`.

```bash
ahc-dashboard                                   # follows this folder's newest run log
ahc-demo --backend sim                          # paced serial dilution, 3D view and dashboard
ahc-demo --backend sim --device opentrons.ot2   # needs this folder's OT-2 simulator running
```

The dashboard shows, from top to bottom:

- the emergency stop button;
- the agent: what it was asked, what it is doing, its plan with each step's progress;
- the task folder, config, layout, motion gate and last hardware event;
- the plate, reservoirs and tips;
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

Press the red button on the dashboard, or run `ahc-estop` in the task folder. The server stops the
running command at once and refuses everything until a person releases it: the dashboard's
Release, after a confirmation, or `ahc-estop --release` at a terminal. In Claude Code the plugin's
hook also stops the agent itself. After a release, the deck is checked again before anything moves.
The stop does not reach an instrument yet; on real hardware the instrument's own stop stays the
primary safety control.

## Other MCP clients

Everything lives in the server, so any MCP client (Codex, pi, ...) works the same way: run it in the
task folder.

```bash
uvx --from git+https://github.com/xthomaswang/ahc ahc-mcp --backend sim
```

## Development

```bash
uv sync
uv run pytest                    # OT-2 tests: AHC_TEST_OT2_PORT=<port of an ahc-sim ot2 started for testing>
claude --plugin-dir .            # this checkout as the plugin
claude plugin validate .
```

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
| `src/ahc/viz/` | dashboard, 3D view, demo |
| `.claude-plugin/`, `skills/`, `hooks/` | the Claude Code plugin: manifest, skill, hooks |

## Known limits

- **Simulation only.** Nothing here has run on a physical instrument.
- **First-start timeout.** Claude Code waits 30 s for an MCP server to start. On a slow network the
  first start after an install or update can take longer while uv downloads; reconnect with `/mcp`.
- **One device per server.**
- **`AHC_DEVICE` conflicts.** If `AHC_DEVICE` is set and a folder's config names another model, the
  server reports `device_conflict` rather than guessing.
- **The emergency stop does not reach an instrument yet** (see above).
- **Live agent text is Claude Code only.** Other clients show their plan and steps, their reported
  decisions and the hardware calls. A step is ticked when the agent moves on to the next one, a few
  seconds after it actually ends.
