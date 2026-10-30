---
name: lab-hardware
description: Run liquid-handling protocols on lab robots through the AHC (Agentic Hardware Control) MCP server: any work a device can do with tips, aspirate, dispense and mix (reagent addition, plate-to-plate transfers, normalization, dilution series, assay setup), on the Hamilton STARlet (simulated) and the Opentrons OT-2 (simulated, or a real robot with a person on site). Use when planning or executing lab work in a task folder, choosing its device or layout, or when an AHC tool reports that a simulator is missing.
---

# Lab hardware (AHC)

The AHC MCP server drives one device for the task folder it runs in. Everything about the task stays
in that folder's `.ahc/`: `config.yaml`, `layouts/`, `protocols/`, `runs/`, `sim/` and `state/deck.json`
(the deck the last run ended with, written by AHC after every call that changes it). Every call is
checked against the device's description file before anything moves, and refusals say what to change.

To plan the work itself (many wells, a table of volumes, replicates, a series, a mix, tip changes),
read `patterns.md` in this skill's folder.

## Flow

1. `lab_overview`: the workspace, its config, the device and its components, the layout format and
   the next step. Read what you need (`find_layout_references`, `describe_device`, `get_params`).
   Call `open_dashboard` at the start of a task: it serves the run page from this server and opens
   it in the person's browser, so they can watch the run and use its stop button. The person types
   nothing; `lab_overview.dashboard` has the URL. For an audience, `open_dashboard(view_only=true)`
   opens a second page with no stop or release button; the person keeps their own.
2. Report your plan with `report_decision(plan=[...])` before `load_layout` or any action (refused
   otherwise: `plan_required`): short concrete steps, e.g. "Load the layout", "The person checks the
   deck", "Add 50 uL buffer to plate columns 1-3", "Check the volumes", "Report the result".
   - Every `load_layout`, `verify` and action call names its step with `plan_step`. The person's
     dashboard ticks a step once you move on to a later one; a step you jump over shows as skipped.
   - A step without a call of its own (waiting for the person's approval): `report_decision` with
     `current_step` and `waiting_for="person"`, just before the call they approve.
   - A changed plan, or a refusal that changes your course: `report_decision` again. Not for every
     call.
3. The device config and this session's go-ahead:
   - When the server forces simulation (`lab_overview.forced_simulation`, e.g. a headless run with
     `AHC_BACKEND=sim`), a STARlet simulation config is created on first use. `configure_devices`
     picks another model.
   - Otherwise, with no config, ask the person whether to simulate or use a real robot, and which
     device (`available_models`); write the choice with `configure_devices` (backend `sim` to
     simulate). Then call `confirm_config(summary=...)` right away, with `config.confirm_summary` exactly
     as `lab_overview` or `configure_devices` gave it (any other text is refused:
     `summary_mismatch`, whose hint gives the current one). Claude Code asks the person to approve
     the call and shows them the summary: their approval is the confirmation, a denial is not (stop
     and ask what they want changed). Never approve for them. Nothing moves before that. A client
     that shows no approval dialog on each call: ask in chat first.
   - The go-ahead (`lab_overview.session` says whether this session needs it and why: on a real
     robot, or in a folder that has never run an experiment): nothing moves (`session_unconfirmed`)
     until the person approves `confirm_config(summary=...)` in this server session, even for a
     config they confirmed before. Call it right away.
   - Switch between simulation and a robot only when the person asks: write the other entry with
     `configure_devices` (a `sim` entry of the same model, or the robot entry again), and they
     confirm it.
   - Under forced simulation too, a config that drops or loosens the person's task limits needs their
     approval of `confirm_config` the same way.
4. The layout: call `find_layout_references` first; it searches this folder only. Base the layout on
   what it returns. With no references, start from `layout_format.example` in simulation; on real
   hardware ask the person on site to describe the deck first. `load_layout(name=...)` loads a saved
   layout.
   - To continue on the same deck, `find_layout_references` returns `last_deck`: the deck the last run
     here ended with, as a layout (liquids from the volumes the twin holds, plus `tips_empty` and
     `tips_returned`). `load_layout` its `layout` as is, or edit it first (e.g. add a fresh plate).
     Never write that state by hand, and AHC never loads it by itself. Wells or racks under
     `last_deck.unknown` are left out: ask the person for those volumes or tip states.
   - `tips_empty` and `tips_returned` are optional layout keys for tip spots that are not fresh, in the
     tip target form (`["tips:B1:H1", "tips:A2:F2"]`, `["tips:A1"]`). Empty spots hold no tip, so an
     omitted `pick_up_tips` skips them and naming one is refused (`tip_spot_empty`). Returned spots hold
     a used tip: never taken as the next fresh one, picked up only when named.
5. The motion gate: after every `load_layout`, call `verify(check="deck_matches_layout")`. Nothing
   moves until it passes. The simulator judges it in simulation; on real hardware the person on site.
   - In simulation a layout needs the person too when its deck placement (the labware type at each
     position, carriers included) matches no layout saved in this folder or passed by its deck check
     here; names, liquids and volumes do not count, and a saved layout loaded by name never needs
     them. `verify` returns `pending` with `ask_person` (the labware, positions and liquids) and lists
     what differs from the closest earlier layout (the layout is also on the run dashboard).
   - A carried deck is confirmed the same way: on real hardware always, since anything may have been
     touched since; `ask_person` lists the volumes and the empty and returned tip spots for the person to
     check. In simulation `last_deck`'s placement matches this folder's reference, so the simulator
     judges it. With cameras configured, `capture` can show the person the plate at the deck check; the
     tips are not counted from images.
   - The person confirms by approving the call: report `waiting_for="person"`, then
     `record_verdict(check_id, verdict="pass", note=<ask_person>)` right away. Claude Code shows them
     the dialog, and their approval is the confirmation. Never approve for them or invent their
     words. Once they approve, the layout is saved and becomes this folder's reference. If they deny
     it, stop and ask what differs, record `verdict="fail"` with their words as the note (that call
     asks them too), load the corrected layout; they confirm that one too. A client that shows no
     approval dialog on each call: ask in chat first.
6. `pick_up_tips` → `aspirate` / `dispense` / `mix` → `drop_tips`, with basic parameters (below).
   `get_params(device, op)` only when you need device-specific behaviour; pass what it lists in
   `specialized`. Unknown keys are refused.
7. Before reporting: `verify(check="volumes_match", expected={target: uL, ...})` for every well you
   changed, e.g. `{"plate:col2": 100, "plate:col3": 100}`. A fail lists each mismatch. Then
   `report_decision(plan_done=true)` with the result. That report and `get_run` return `timing` (run total, per-step durations, robot / agent / others split, where others is waiting for the person plus stops and their recovery; the agent and others parts are estimates), and the dashboard shows it live.

## Targets and basic parameters

- A component is `<device id>.<component>`, e.g. `starlet.pip`, `ot2.right`. On the OT-2 the components
  are the mounts, `ot2.left` and `ot2.right`, each with the GEN2 pipette the robot carries; an empty
  mount, or one with a pipette AHC does not describe, has none. Without anything else (and in the
  simulator) `ot2.right` is a P300 8-channel and `ot2.left` a P20 single channel. `lab_overview` and
  `describe_device` show each component's pipette, channels, limits and tip sizes.
- A target is `plate:A1`, `plate:A1:H1` (a range), `plate:col2` (A2 to H2, top to bottom),
  `plate:rowB` (B1 to B12, left to right), the name alone of a labware that is one container (a
  trough, e.g. `buffer`), or an alias from the layout. Tip spots: `tips:A1`, `tips:A1:H1`,
  `tips:col1`.
- "Columns 2 and 3" means `plate:col2` and `plate:col3`, one pass each with an 8-channel head. Never
  rows B and C. A head whose channels move together (more than one channel and `equal_volumes` true,
  e.g. an 8-channel head such as the default `ot2.right`) serves one full column per call, top to bottom.
- Every action's result echoes `effective.where`, e.g. `plate: column 2 (A2-H2)`. Check that it is
  what you meant.
- One target per mounted tip, or one container all channels share (only where `shared_container` is
  true). `volumes` are per target; one number is broadcast.
- Tips: a head whose channels move together picks up exactly one full column; a single channel
  exactly one tip; independent channels 1 to n. A refusal (`tip_count`) names the count. Each pipette
  takes its own tip size: a P20 10 or 20 uL, a P300 200 or 300 uL, a P1000 1000 uL.
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
- The robot: find it with `find_robots` (mDNS, then each robot's `/health`) or ask the person for its
  address (and port, if not 31950). Show what was found (name, address, pipettes) and let the person say
  which robot this task uses; never probe addresses yourself. Write it with `configure_devices`, e.g.
  `[{"id": "ot2", "model": "opentrons.ot2", "backend": "robot", "options": {"host": "<address>"}}]`.
  It reads the robot's name and what each mount carries (`options.mounts`): show the person both, with
  the summary listing the mounts, before they confirm. `mount_mismatch` (the pipettes changed after
  confirmation): write the config again with `configure_devices` and have them confirm it.
  `no_usable_pipette`, `wrong_robot` (another robot answers at that address): stop and tell the person.
- Tell the person how to stop this server: `lab_overview.emergency_stop.how` (the dashboard's stop
  button, or `uv run --project <repo> ahc-estop` in a terminal).
- To let go of the robot while the session and the dashboard stay up, call `disconnect` (no emergency
  stop): it ends the run (`run_ended_confirmed`, the robot's own answer) and notes tips left on a pipette.
  Then nothing moves (`disconnected`) until the person's `confirm_config` and a `load_layout`.
  `robot_in_use` (another session drives this robot): tell the person; that session must disconnect or end,
  and they decide which one drives.
- `load_layout` starts a run on the robot and homes it: hands and deck must be clear and the
  pipettes carry no tips. It has no dialog of its own, so fold that into the last one before it (the
  `tips_mounted` note or `confirm_config`): say in chat, in the same message before that call, that
  approving also means hands and deck are clear and the robot homes next. With no such dialog, ask
  in chat.
- `tips_unconfirmed` (tips may be on that the robot no longer knows of: from an earlier run, or
  after a stop or an unknown outcome): tell the person what it says (which pipette, how many tips,
  when and why), tell them to remove the tips by hand (or that there are none), then
  `verify(check="tips_mounted", expect="none")` without `device`, one check for every pipette, and
  `record_verdict` with its `ask_person` right away (the homing line above goes in your message).
  When it says this run still holds its tips, `drop_tips` first: the check of every pipette is
  refused while the run holds tips it knows.
- Every `verify` waits for the person (`pending`), except `volumes_match`, which the digital twin
  answers, and a check with `auto: true` (see Auto-verify below), which you judge. The result carries `ask_person`, one sentence of what they confirm (the deck's slots,
  labware and liquids; "No tip on either pipette (ot2.left, ot2.right)"; "8 tips seated straight on
  ot2.right"; the liquid targets and minimum volume). Report `waiting_for="person"`, then call
  `record_verdict(check_id, verdict="pass", note=<ask_person>)` right away: the dialog Claude Code
  shows is the question, and the person's approval is the answer. Move nothing meanwhile (while a
  tips or liquid check waits, every motion tool is refused: `check_pending`). After a repeated
  `verify`, record the verdict on the newest check id.
- The deck check: `ask_person` lists every slot's labware and each liquid with its volume, from the
  layout, and the points to look at. Starting volumes come from the layout: ask the person how much
  is really there before you load it.
- First runs go one stage at a time, each with the person's check: tips only (pick up,
  `verify(check="tips_mounted")`, return them), water before the real liquids, and after each liquid
  step `verify(check="liquid_present", targets=[...])`.
- Positions come from the labware definitions, without Labware Position Check offsets: the first tip
  pick-up is the first contact, so say when it comes.
- A denied `record_verdict` is a fail: stop, ask the person what they see in chat, record
  `verdict="fail"` with their words as the note (that call asks them too), and go on only as they
  say. A client that shows no approval dialog on each call: ask in chat first. After a
  failed tip check only `drop_tips` moves (`check_failed`) until a `drop_tips` on that pipette goes
  through or a later tip check of it passes; after a failed liquid check only `drop_tips` and
  `load_layout` move until a later check of those wells passes or a new layout is loaded.

## Cameras (only when `lab_overview.config.cameras` lists some)

- `capture(label, plan_step)` takes a still from every camera (`cameras=["top"]` for some) and returns
  the images: look at them. Capture before a tip pick-up, an aspirate or a dispense you want to check,
  and once with `final=true` when the result is on the deck. Not in simulation; no motion, so it works
  after an emergency stop too. The pipette head can hide labware from a camera: capture when the head is
  elsewhere (e.g. after dropping tips in the trash), and say so when an image shows the head instead.
- Photos of a check go in `verify(images=[paths from capture])`. They are evidence only: on real
  hardware the person still gives the verdict. Cameras are the person's to declare (`configure_devices`
  `cameras`, confirmed by them); never invent a device name, ask which camera is which.

### Slot maps, verification points and shadow judgments (cameras with a slot map)

- `map_camera(camera)` takes a reference image (deck clear, head parked away, nobody in front of the lens) and
  gives it back with the deck guide: from above, slots 1-3 are the front row left to right, 4-6 and 7-9 the
  rows behind, then 10, 11 and the trash 12 in the back row. Propose a box per slot you can see with
  `map_camera(camera, slots={"2": [x0, y0, x1, y1], ...})`, in the pixels of that image, hugging each
  labware's footprint (128 x 85 mm, long side left to right) by its guide lines and engraved number. A slot cut
  off at the frame's edge gets its whole footprint (the box reaches past the edge); AHC knows which wells are
  visible. Leave out slots the camera does not see. A side camera: `view="side"`. A close-up of a pipette's
  tips: `tips:<component>`. The drawn boxes come back for you to check; the config is then pending: the person
  confirms it (`confirm_config`, the deck is checked again) and can drag boxes on the dashboard.
- A capture of a mapped camera says per slot the `level` AHC measured: `unclear` (too blurry, covered, too
  small or mostly out of frame: judge nothing), `slot` (labware present, its type and liquid colour) or `well`
  (single wells). Judge only at that level, and look at the crops (`slots[].crop`). `map` is `unclear` when
  the frame is out of focus or covered (capture again with the head parked elsewhere) and `needs_new_map`
  when the camera no longer lines up with its reference (the camera moved: `map_camera` again).
- With the plan, declare what your checks need the cameras to see:
  `report_decision(plan=[...], vision_points=[{"what": "plate:B1:B8", "level": "well", "check": "liquid_present"}])`
  (`what`: a target, `slot:<n>` or `tips:<component>`; `level`: `slot`, `well` or `tip`). AHC adds the points
  it derives from the loaded layout (every slot at `slot` for the deck check, `tips:<pipette>` for tips); you
  cannot remove those.
- `physical_world_context()` is the umbrella: what AHC can observe of the real deck, from independent sources.
  It captures (unless a capture newer than the last motion exists) and returns `vision_context` (the cameras:
  slot maps, alignment, quality gate), `instrument_context` (the device's own sensors; the OT-2 says "no tip or
  liquid sensors", and the STARlet's tip presence, liquid-level detection and pressure monitoring are not read as
  evidence yet), `points`, `physical_world_confidence` and `suggestions`. Each point lists its `sources`
  (`{source: "vision:usb", level, fresh, covers}`; a source that cannot use the point says `state` and `why`) and a
  `status`: `covered` (at least one source covers it at its level), `unclear` (blurred, covered or not captured:
  capture again; `state: needs_new_map` on a source: the camera moved, `map_camera` again) or `uncovered`.
  `physical_world_confidence.by_check` gives the same per check type with the sources and levels that cover
  it, `by_source` what each source observes and covers, `all_confident` whether every point is covered. There is no
  numeric score. A point at level `well` also needs those wells inside the frame (a plate cut off at the top shows
  rows D-H only). Use it to know what can be verified before you rely on an image; it gives no verdict.
- The digital twin (tracked volumes and tips) is not a source: it is the expectation that observations are
  compared with, and it never adds confidence.
- Shadow mode, on real hardware: while a check waits for the person, look at the crops and call
  `propose_verdict(check_id, verdict="pass"|"fail"|"unclear", images=[...], per_point=[...])` before
  `record_verdict`. Images are required. It passes nothing: the person's verdict decides. AHC records whether
  you agreed with them (per check type; a disagreement or `unclear` resets the streak, a changed slot map
  resets all), which later decides how much the cameras may judge on their own. Say `unclear` when the
  images cannot tell; never guess. The recovery checks after a stop, an unknown outcome or a restart stay
  with the person (`recovery_check`).

### Auto-verify (only when `lab_overview.auto_verify` is there)

The person can let the cameras answer `deck_matches_layout`, `liquid_present` or `tips_mounted` checks
(`auto_verify` in the confirmed config, set with `configure_devices(auto_verify={"checks": [...], "min_streak": 5})`).
AHC, not you, decides when a check is auto: the check type is listed, auto is not paused (dashboard), no
stop is engaged, it is not the recovery check, the run is confident, the check has points of its own, and your
`propose_verdict` judgments agreed with the person's `min_streak` times in a row (per check type).
"The run is confident" is decided for the whole run: every point of every check type listed in `auto_verify.checks`
must be `covered` in `physical_world_confidence` (fresh captures newer than the last motion, sharp, aligned). One
uncovered or unclear point of a listed type turns auto off for every check, with `auto_off: "low vision confidence:
<the points not covered and why>"`; points of check types that are not listed do not count. So a listed `tips_mounted`
keeps auto off wherever nothing observes the tips. `lab_overview.auto_verify` says the run level and, per check
type, AUTO or why not.

- Capture first (`physical_world_context`; after any motion the old pictures are stale), then `verify`. Auto:
  the result has `auto: true`, `points` with their `crops`, `images` and `ask_agent`; no `ask_person`. Not
  auto: it is the person's check as always, with `auto_off` saying why (fix what it names if you can, e.g.
  capture again with the head parked away; otherwise carry on with the person).
- Judge the crops yourself, point by point, and call `agent_verdict(check_id, verdict, images, per_point, note)`.
  `pass` only when every point shows what the check asks; hidden, blurred, off or in doubt is `unclear`, a
  mismatch is `fail`. Nothing moves until the verdict.
- `pass` passes the check (`judged_by: agent (auto, cameras)`, images kept) and opens the gate after a deck
  check; the person reviews every auto verdict on the dashboard. `fail`, `unclear`, or auto no longer holding
  (a motion, a stop, a blurred capture, a pause since `verify`): nothing is recorded as a verdict, the check
  is the person's (`ask_person`, `auto_off`), motion stays held, and you go on with `record_verdict` as usual.
  Never an auto fail. Auto verdicts do not count toward the streak; keep calling `propose_verdict` on the
  person's checks.
- Always the person's, whatever the config says: the recovery check of every pipette after a stop, an unknown
  outcome or a restart (tips removed by hand), the config and the go-ahead, the homing go-ahead, and the stop
  release.

## When something goes wrong

- `completion_unknown`: a command may or may not have run on the robot. Stop and tell the person;
  they check the tips and wells; then the tips check (above) and `load_layout` again with the
  volumes they report. Never retry. Until then `get_state` shows the tips and the wells that call
  touched as unknown, `tips_mounted` and `liquid_present` say unknown, and `volumes_match` fails on
  those wells.
- `device_refused`: the device model or the robot refused the command. `backend_unreachable`: the
  robot is off or not on this network (or its address changed: when the message names where the robot
  answers now, `configure_devices` with that `options.host` and the person confirms; never switch it
  yourself). On real hardware stop and ask the person, and never retry a
  motion call; in simulation, change the call as the message says.
- `emergency_stop`: the person pressed the stop. Stop at once: do not retry, do not work around it,
  never release it. Read-only calls, `capture` and `report_decision` still run; nothing else does (the
  Claude Code hook denies every other call, Bash and edits included, and ends your turn). With cameras
  configured, `capture` the deck first. Tell the person what was running and report
  `waiting_for="person"`. AHC sends
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
- Rehearsing with the robot's pipettes: configure the `sim` backend with `options.mounts` copied from
  the robot's config. `ahc-sim ot2` gives the simulator those pipettes each time it starts, so restart
  it after changing them.
