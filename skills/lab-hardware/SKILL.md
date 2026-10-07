---
name: lab-hardware
description: Run liquid-handling work (transfers, mixing, serial dilutions) on lab robots through the AHC (Agentic Hardware Control) MCP server, for the Hamilton STARlet and Opentrons OT-2 (simulated for now). Use when planning or executing lab work in a task folder, choosing its device or layout, or when an AHC tool reports that a simulator is missing.
---

# Lab hardware (AHC)

The AHC MCP server drives one device for the task folder it runs in. Everything about the task stays
in that folder's `.ahc/`: `config.yaml`, `layouts/`, `protocols/`, `runs/` and `sim/`. Every call is
checked against the device's description file before anything moves, and refusals say what to change.

## Flow

1. `lab_overview`: the workspace, its config status, the device and its components, the layout
   format and the next step. Read what you need (`find_layout_references`, `describe_device`), then
   report your plan with `report_decision` before `load_layout` or any action: a concrete
   overview, `plan` as short steps (e.g. "Load the example layout", "Wait for the person to confirm
   it", "Add 100 uL diluent to column 2", "Report the volumes"). The server refuses
   `load_layout` and every action until then (`plan_required`).
   - Every `load_layout`, `verify` and action call names its step with `plan_step`. The person's
     dashboard ticks a step once you move on to a later one; a step you jump over shows as skipped.
   - A step without a call of its own (waiting for the person): `report_decision` with
     `current_step` and `waiting_for="person"`.
   - A changed plan, or a refusal that changes your course: `report_decision` again. Not for every
     call.
   - When the last step is done, report the result with `report_decision(plan_done=true)`.
2. The device config:
   - When the server forces simulation (the plugin does), a STARlet simulation config is created on
     first use. `configure_devices` picks another model.
   - Otherwise list `available_models` to the person, let them choose, write the choice with
     `configure_devices`, then ask the person to confirm it with `confirm_config`. Never call
     `confirm_config` yourself and never approve it on the person's behalf.
3. The layout: call `find_layout_references` first; it searches this folder only. Base the layout
   on what it returns. With no references, start from `layout_format.example` in simulation; on
   real hardware ask the person on site to describe or check the deck first. `load_layout` saves the
   layout in `.ahc/layouts/`; `load_layout(name=...)` loads a saved one.
4. The motion gate: after every `load_layout`, call `verify(check="deck_matches_layout")`. Nothing
   moves until it passes. The simulator judges it in simulation; on real hardware the person on site
   records the verdict.
   - A layout with no reference in this folder (the example, or your own) needs the person in
     simulation too: `verify` returns `pending`. Show the person the labware, positions and
     liquids (they are also on the run dashboard), ask them to confirm, report
     `waiting_for="person"`, and stop until they answer.
   - Record their answer with `record_verdict` only when they give it; never decide it yourself.
     Once they confirm, the layout is saved and becomes this folder's reference. If they reject it,
     ask what to change and load the corrected layout; they confirm that one too.
5. `pick_up_tips` → `aspirate` / `dispense` / `mix` → `drop_tips`, with basic parameters: targets,
   volumes (per target; one number is broadcast), flow rate and liquid height.
6. `get_params(device, op)` only when you need device-specific behaviour; pass what it lists in
   `specialized`. Unknown keys are refused.
7. `verify` the physical state when it matters; `get_run` for what took effect.

## The OT-2 simulator

The OT-2 runs against Opentrons' robot-server: installed once for all tasks, one instance per task
folder (`ahc-sim ot2`). `lab_overview.simulator` says whether it is installed and running.

- `sim_not_installed`: tell the person what the hint says setup downloads (source, size, where it
  goes) and wait for their go-ahead. Then run the setup command from the hint; it is an absolute
  path, so use it as given.
- `sim_not_running`: run the start command from the hint in a background shell (it keeps running
  until stopped), then load the layout again.

## Emergency stop

If a call is refused with `emergency_stop`, the person pressed the emergency stop. Stop at once: do
not retry, do not work around it, never release it yourself. Tell the person what was running and
report `waiting_for="person"` with `report_decision`. After they release it, check the deck again
(`verify(check="deck_matches_layout")`) before anything moves.

## Rules

- Plan the volumes before the first aspirate: the final volume in every well, and every transfer and
  mix volume against `volume_ul` (task limits show `task_limit: true` in `get_params`) and the tip
  size. If a cap is below a transfer, split it into equal aspirate/dispense pairs under the cap.
  Before reporting, check the final volumes with `get_state`.
- Read a refusal's hint and change the call; never retry the same call unchanged.
- Address components as `<device id>.<component>`, e.g. `starlet.pip`. A target is `labware`,
  `labware:A1`, `labware:A1:H1` or an alias.
- Task limits in `.ahc/config.yaml` can only tighten the device's limits. When one applies to a
  parameter, pass that parameter explicitly.
- Results marked `simulated` are not measurements.
- Verdicts that wait for the person (real hardware, or a layout without a reference) are theirs:
  call `record_verdict` only with the verdict they gave you, never on your own.
