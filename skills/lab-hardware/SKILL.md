---
name: lab-hardware
description: Run liquid-handling protocols on lab robots through the AHC (Agentic Hardware Control) MCP server: any work a device can do with tips, aspirate, dispense and mix (reagent addition, plate-to-plate transfers, normalization, dilution series, assay setup), on the Hamilton STARlet (simulated) and the Opentrons OT-2 (simulated, or a real robot with a person on site). Use when planning or executing lab work in a task folder, choosing its device or layout, or when an AHC tool reports that a simulator is missing.
---

# Lab hardware (AHC)

The AHC MCP server drives one device for the task folder it runs in. Everything about the task stays
in that folder's `.ahc/`: `config.yaml`, `layouts/`, `protocols/`, `runs/` and `sim/`. Every call is
checked against the device's description file before anything moves, and refusals say what to change.

To plan the work itself (many wells, a table of volumes, replicates, a series, a mix, tip changes),
read `patterns.md` in this skill's folder.

## Flow

1. `lab_overview`: the workspace, its config, the device and its components, the layout format and
   the next step. Read what you need (`find_layout_references`, `describe_device`, `get_params`).
2. Report your plan with `report_decision(plan=[...])` before `load_layout` or any action (refused
   otherwise: `plan_required`): short concrete steps, e.g. "Load the layout", "The person checks the
   deck", "Add 50 uL buffer to plate columns 1-3", "Check the volumes", "Report the result".
   - Every `load_layout`, `verify` and action call names its step with `plan_step`. The person's
     dashboard ticks a step once you move on to a later one; a step you jump over shows as skipped.
   - A step without a call of its own (waiting for the person): `report_decision` with
     `current_step` and `waiting_for="person"`.
   - A changed plan, or a refusal that changes your course: `report_decision` again. Not for every
     call.
3. The device config and this session's go-ahead:
   - When the server forces simulation (`lab_overview.forced_simulation`, e.g. a headless run with
     `AHC_BACKEND=sim`), a STARlet simulation config is created on first use. `configure_devices`
     picks another model.
   - Otherwise, with no config, ask the person whether to simulate or use a real robot, and which
     device (`available_models`); write the choice with `configure_devices` (backend `sim` to
     simulate). Show them `config.confirm_summary` and ask them to confirm it. Only when they say so,
     call `confirm_config(summary=...)` with that summary exactly as `lab_overview` or
     `configure_devices` gave it (any other text is refused: `summary_mismatch`, whose hint gives the
     current one). Claude Code then asks the person to approve the call and shows them the summary.
     Never confirm on your own. Nothing moves before that.
   - The go-ahead (`lab_overview.session` says whether this session needs it and why: on a real
     robot, or in a folder that has never run an experiment): nothing moves (`session_unconfirmed`)
     until the person approves `confirm_config(summary=...)` in this server session, even for a
     config they confirmed before.
   - Switch between simulation and a robot only when the person asks: write the other entry with
     `configure_devices` (a `sim` entry of the same model, or the robot entry again), and they
     confirm it.
   - Under forced simulation too, a config that drops or loosens the person's task limits waits for
     their confirmation the same way.
4. The layout: call `find_layout_references` first; it searches this folder only. Base the layout on
   what it returns. With no references, start from `layout_format.example` in simulation; on real
   hardware ask the person on site to describe the deck first. `load_layout(name=...)` loads a saved
   layout.
5. The motion gate: after every `load_layout`, call `verify(check="deck_matches_layout")`. Nothing
   moves until it passes. The simulator judges it in simulation; on real hardware the person on site.
   - In simulation a layout needs the person too when its deck placement (the labware type at each
     position, carriers included) matches no layout saved in this folder or passed by its deck check
     here; names, liquids and volumes do not count, and a saved layout loaded by name never needs
     them. `verify` returns `pending` and lists what differs from the closest earlier layout. Show
     the person the layout (also on the run dashboard) and those differences, ask them to confirm,
     report `waiting_for="person"`, and stop.
   - Record their answer with `record_verdict` only when they give it; Claude Code asks them to
     approve the call. Never decide a verdict yourself. Once they confirm, the layout is saved and
     becomes this folder's reference. If they reject it, ask what to change and load the corrected
     layout; they confirm that one too.
6. `pick_up_tips` → `aspirate` / `dispense` / `mix` → `drop_tips`, with basic parameters (below).
   `get_params(device, op)` only when you need device-specific behaviour; pass what it lists in
   `specialized`. Unknown keys are refused.
7. Before reporting: `verify(check="volumes_match", expected={target: uL, ...})` for every well you
   changed, e.g. `{"plate:col2": 100, "plate:col3": 100}`. A fail lists each mismatch. Then
   `report_decision(plan_done=true)` with the result.

## Targets and basic parameters

- A component is `<device id>.<component>`, e.g. `starlet.pip`, `ot2.right`.
- A target is `plate:A1`, `plate:A1:H1` (a range), `plate:col2` (A2 to H2, top to bottom),
  `plate:rowB` (B1 to B12, left to right), the name alone of a labware that is one container (a
  trough, e.g. `buffer`), or an alias from the layout. Tip spots: `tips:A1`, `tips:A1:H1`,
  `tips:col1`.
- "Columns 2 and 3" means `plate:col2` and `plate:col3`, one pass each with an 8-channel head. Never
  rows B and C. A head whose channels move together (more than one channel and `equal_volumes` true,
  e.g. `ot2.right`) serves one full column per call, top to bottom.
- Every action's result echoes `effective.where`, e.g. `plate: column 2 (A2-H2)`. Check that it is
  what you meant.
- One target per mounted tip, or one container all channels share (only where `shared_container` is
  true). `volumes` are per target; one number is broadcast.
- Tips: a head whose channels move together picks up exactly one full column; a single channel
  exactly one tip; independent channels 1 to n. A refusal (`tip_count`) names the count. Each pipette
  takes its own tip size (on the OT-2, 300 uL tips for `ot2.right`, 20 uL tips for `ot2.left`).
  Omitted: the next fresh column (or tip), in layout order, from the racks whose tips fit that
  pipette. Returned tips are never picked as fresh again; name them to use them again.
- `liquid_height`, mm above the cavity bottom: omitted means the description file's default (1 mm on
  the OT-2), recorded in the result. An aspirate or mix whose tips would end above the liquid is
  refused (`above_liquid`): draw less, or lower the height as the hint says.
- `flow_rate` omitted: the device's default. With a task limit on a parameter, pass it explicitly
  unless the description file's default lies inside the limit (`limit_requires_value` otherwise).

## Planning rules

- The layout describes what is physically on the deck. Never change labware, positions or starting
  volumes to make a request fit, in simulation too: ask the person.
- Fit the plan to each component before the first call: `lab_overview.components` gives `channels`,
  `equal_volumes` and `shared_container`; `get_params` gives every limit (task limits show
  `task_limit: true`). A step a component cannot do as asked may work another way (another pipette,
  a source column prepared first): say how in the plan, or ask the person.
- Plan every volume before the first aspirate: the final volume in each well, each transfer and mix
  against `volume_ul` and the tip size, each source against what it holds. Split a transfer above a
  cap into equal parts under it.
- Read a refusal's hint and change the call; never retry the same call unchanged.
- Report honestly. Say the work is done only when every planned step's calls succeeded and
  `volumes_match` passed; otherwise say what was refused, what was done partly and what is left.
- Results marked `simulated` are not measurements. On real hardware the run log and `volumes_match`
  say what the robot was told to do, not what it measured.

## Real hardware (a person on site)

A real OT-2 is the `robot` backend; the server follows this folder's config. If `forced_simulation`
is true, nothing reaches a robot and you cannot change that. Everything below is the person's to
decide: you prepare it, show it and wait.

- Reach the robot only through the AHC tools: no curl or other HTTP to it, no `ahc-smoke`, no
  `ahc-estop`. Never release a stop.
- The robot: ask the person for its address (and port, if not 31950); never guess, scan or discover
  one. Write it with `configure_devices`, e.g.
  `[{"id": "ot2", "model": "opentrons.ot2", "backend": "robot", "options": {"host": "<address>"}}]`.
  It reads the robot's name and pipettes: show the person that identity and the summary before they
  confirm. `mount_mismatch`: the pipettes differ from the description file. `wrong_robot`: another
  robot answers at that address. Stop and tell the person.
- Tell the person how to stop this server: `lab_overview.emergency_stop.how`.
- `load_layout` starts a run on the robot and homes it. Tell the person first and wait until they say
  hands and deck are clear and the pipettes carry no tips.
- `tips_unconfirmed` (tips may be on that the robot no longer knows of: from an earlier run, or
  after a stop or an unknown outcome): tell the person what it says (which pipette, how many tips,
  when and why), ask them to remove the tips by hand or confirm there are none, then
  `verify(check="tips_mounted", expect="none")` without `device`, one check for every pipette, and
  wait for their verdict. When it says this run still holds its tips, `drop_tips` first: the check
  of every pipette is refused while the run holds tips it knows.
- Every `verify` waits for the person (`pending`), except `volumes_match`, which the digital twin
  answers. Report `waiting_for="person"` and stop until they answer; move nothing meanwhile (while a
  tips or liquid check waits, every motion tool is refused: `check_pending`). After a repeated
  `verify`, record the verdict on the newest check id.
- The deck check: list every slot's labware, the tip columns in use and each liquid with its volume.
  Starting volumes come from the layout: ask the person how much is really there.
- First runs go one stage at a time, each with the person's check: tips only (pick up,
  `verify(check="tips_mounted")`, return them), water before the real liquids, and after each liquid
  step `verify(check="liquid_present", targets=[...])`.
- Positions come from the labware definitions, without Labware Position Check offsets: the first tip
  pick-up is the first contact, so say when it comes.
- A `fail` from the person on any check: stop, ask what they see, and go on only as they say. After a
  failed tip check only `drop_tips` moves (`check_failed`) until a `drop_tips` on that pipette goes
  through or a later tip check of it passes; after a failed liquid check only `drop_tips` and
  `load_layout` move until a later check of those wells passes or a new layout is loaded.

## When something goes wrong

- `completion_unknown`: a command may or may not have run on the robot. Stop and tell the person;
  they check the tips and wells; then the tips check (above) and `load_layout` again with the
  volumes they report. Never retry. Until then `get_state` shows the tips and the wells that call
  touched as unknown, `tips_mounted` and `liquid_present` say unknown, and `volumes_match` fails on
  those wells.
- `device_refused`: the device model or the robot refused the command. `backend_unreachable`: the
  robot is off or not on this network. On real hardware stop and ask the person, and never retry a
  motion call; in simulation, change the call as the message says.
- `emergency_stop`: the person pressed the stop. Stop at once: do not retry, do not work around it,
  never release it. Tell the person what was running and report `waiting_for="person"`. AHC sends
  nothing more (on an OT-2 it also stops its run), but the robot may first finish the command it is
  executing (a move, an aspirate, a pick-up): the robot's power switch is the immediate stop. When
  `next` or `get_state.unknown` says the robot has not confirmed that its run stopped, it may still be
  moving: tell the person so. A call the stop cut short on real hardware has an unknown outcome, as
  above.
- After the person releases it: on an OT-2 motion stays refused (`device_halted`) until the layout is
  loaded again. The person removes any tips left on the pipettes, and you record it with the tips
  check (above); then `load_layout` (it homes the robot) and the deck check. On other devices,
  `verify(check="deck_matches_layout")` again before anything moves.

## The OT-2 simulator (simulation only)

The simulated OT-2 runs against Opentrons' robot-server: installed once for all tasks, one instance
per task folder (`ahc-sim ot2`). `lab_overview.simulator` says whether it is installed and running.

- `sim_not_installed`: tell the person what the hint says setup downloads (source, size, where it
  goes) and wait for their go-ahead. Then run the setup command from the hint; it is an absolute
  path, so use it as given.
- `sim_not_running`: run the start command from the hint in a background shell (it keeps running
  until stopped), then load the layout again.
