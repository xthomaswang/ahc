---
model: opentrons.ot2
brand: opentrons
title: Opentrons OT-2 with a P300 8-channel GEN2 (right) and a P20 single-channel GEN2 (left)
backends:
  sim: "Opentrons robot-server (opentrons-ot2 repo) with a virtual Smoothie, reached over its HTTP API."
  robot: "A real OT-2: its own robot-server on the lab network, over the same HTTP API. Set options.host (options.port if not 31950) in the config; configure_devices reads the robot's name into options.robot_name, which confirm_config requires (robot_name_required), so another robot is refused. The person confirms it."
layout:
  kind: slots
  slots: {min: 1, max: 11}
  labware:
    tip_racks: [opentrons_96_tiprack_300ul, opentrons_96_tiprack_20ul]
    plates: [Cor_96_wellplate_360ul_Fb, cor_96_wellplate_2mL_Vb]
    troughs: []
  note: "A reservoir with one well per column cannot feed the 8-channel head through this adapter; keep shared liquids in a column of a deep-well plate."
components:
  right:
    title: P300 8-channel GEN2
    capabilities: [liquid_handling]
    channels: 8
    shared_container: false
    equal_volumes: true
    limits:
      volume_ul: {min: 20, max: 300, source: "Opentrons pipetteNameSpecs p300_multi_gen2"}
      flow_rate_ul_s: {min: 1, max: 275, default: 94, source: "Opentrons pipetteNameSpecs p300_multi_gen2"}
      liquid_height_mm: {min: 0, max: 40, default: 1.0, doc: "Omitted: 1 mm above the cavity bottom, the clearance Opentrons uses for aspirate and dispense.", source: "prototype bound above the cavity bottom; also capped by the container depth and, for aspirate and mix, the liquid surface"}
      mix_repetitions: {min: 1, max: 20, source: "prototype bound"}
    specialized: {}
    translations: {}
  left:
    title: P20 single-channel GEN2
    capabilities: [liquid_handling]
    channels: 1
    shared_container: true
    equal_volumes: true
    limits:
      volume_ul: {min: 1, max: 20, source: "Opentrons pipetteNameSpecs p20_single_gen2"}
      flow_rate_ul_s: {min: 0.08, max: 24, default: 7.56, source: "Opentrons pipetteNameSpecs p20_single_gen2"}
      liquid_height_mm: {min: 0, max: 40, default: 1.0, doc: "Omitted: 1 mm above the cavity bottom, the clearance Opentrons uses for aspirate and dispense.", source: "prototype bound above the cavity bottom; also capped by the container depth and, for aspirate and mix, the liquid surface"}
      mix_repetitions: {min: 1, max: 20, source: "prototype bound"}
    specialized: {}
    translations: {}
---

# Opentrons OT-2

Two mounts. The right P300 8-channel head is rigid: its eight nozzles sit 9 mm apart in one column
and share one plunger, so every nozzle moves the same volume and each nozzle needs its own well. The
left P20 takes one tip and serves one well per call. Each takes its own tips: the head 300 uL tips
(`opentrons_96_tiprack_300ul`), the P20 20 uL tips (`opentrons_96_tiprack_20ul`).

## Layout

Labware sits in slots 1–11; slot 12 is the fixed trash. Shared liquids for the 8-channel head go in
one column of a deep-well plate (`cor_96_wellplate_2mL_Vb`), for example `reagents:col1` (the same
wells as `reagents:A1:H1`), and an alias such as `buffer` can name that column.

On a real robot every `load_layout` starts a new run and homes the robot, before the deck is
checked: the person keeps hands and the deck clear. The new run does not know tips an earlier run
left on the pipettes, so while tips may be on, `load_layout` is refused (`tips_unconfirmed`). Tips
the current run still holds are dropped with `drop_tips` first. Any others the person removes by
hand and confirms with one check for both pipettes: `verify(check="tips_mounted", expect="none")`
without a device.

## Liquid handling

- `volumes` are per target, but the 8-channel head needs them all equal. Volumes below its minimum,
  or that differ from well to well, go to the P20: one well per call, in parts within its limits,
  with a 20 uL tip rack on the deck (the built-in example layout has one in slot 4).
- The 8-channel head picks up exactly one full column, top to bottom (`tips:col1` or `tips:A1:H1`);
  the P20 picks up exactly one tip from its 20 uL rack (e.g. `tips20:A1`). An omitted pick-up takes
  the next fresh tips, in layout order, from the racks whose tips fit that pipette.
- `liquid_height` is in mm above the cavity bottom, 1 mm when omitted (Opentrons' own clearance); the
  result records the height used. An aspirate or mix whose tips would end above the liquid, by the
  twin's volume and the labware geometry, is refused (`above_liquid`): draw less, or pass the lower
  height the hint gives.
- `mix` is native: the head cycles in place.
- Returned tips go back to their spots but are never picked as the next fresh tips; name them to use
  them again.
- Positions: tip racks use Opentrons' official definitions with the robot's own calibration (deck,
  pipette offset, tip length); plate positions come from PyLabRobot's labware geometry and are sent
  as coordinates. Labware Position Check offsets are not applied: running it in the Opentrons App
  changes nothing here. Check positions and heights with a dry run, tips only and then water, before
  anything that matters.

## Stopping

The door switch and the front button do not pause or stop AHC's runs: AHC drives the robot through
setup commands, which they ignore. AHC's emergency stop sends nothing more and stops AHC's run, but
the robot halts only once the command it is executing (a move, an aspirate or dispense, a tip
pick-up) has run to its end: the power switch is the immediate stop. The stop reaches only a run AHC
started: when another client's run (the Opentrons App's) has replaced AHC's as the current run, it
is logged as `device_halt_failed` (`other_run_active`), so nobody runs the Opentrons App on the
robot during an AHC run. After a stop, or a call whose outcome is unknown, AHC no longer knows which
tips are on the pipettes: the person removes them by hand and confirms it (see Layout) before the
layout is loaded again.

## Sensors usable for verification

None: OT-2 GEN2 pipettes report no tip presence. On real hardware the person checks tips and wells
(cameras later), and nothing moves while their check waits.

## Cannot

- Draw with the 8-channel head from a single-well reservoir column through this adapter.
- Use modules (thermocycler, heater-shaker) through this server yet.
