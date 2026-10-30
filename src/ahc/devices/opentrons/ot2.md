---
model: opentrons.ot2
brand: opentrons
title: Opentrons OT-2 with the GEN2 pipettes its mounts carry
backends:
  sim: "Opentrons robot-server (opentrons-ot2 repo) with a virtual Smoothie, reached over its HTTP API. The simulator carries the mounts the config names in options.mounts (ahc-sim sets them each time it starts), else the default pair."
  robot: "A real OT-2: its own robot-server on the lab network, over the same HTTP API. Set options.host (options.port if not 31950) in the config; configure_devices reads the robot's name into options.robot_name and its pipettes into options.mounts, and confirm_config requires the name (robot_name_required), so another robot is refused. The person confirms the robot and its mounts together."
layout:
  kind: slots
  slots: {min: 1, max: 11}
  labware:
    tip_racks: [opentrons_96_tiprack_20ul, opentrons_96_filtertiprack_20ul, opentrons_96_tiprack_10ul, opentrons_96_filtertiprack_10ul, opentrons_96_tiprack_300ul, opentrons_96_filtertiprack_200ul, opentrons_96_tiprack_1000ul, opentrons_96_filtertiprack_1000ul]
    plates: [Cor_96_wellplate_360ul_Fb, cor_96_wellplate_2mL_Vb]
    troughs: [nest_12_troughplate_15000uL_Vb, nest_1_troughplate_195000uL_Vb]
  note: "A reservoir well serves one channel at a time, so an 8-channel head draws from a column of 8 wells, and single-channel pipettes draw from any well."
mounts:
  slots: [left, right]
  default: {left: p20_single_gen2, right: p300_multi_gen2}
  limits:
    liquid_height_mm: {min: 0, max: 40, default: 1.0, doc: "Omitted: 1 mm above the cavity bottom, the clearance Opentrons uses for aspirate and dispense.", source: "prototype bound above the cavity bottom; also capped by the container depth and, for aspirate and mix, the liquid surface"}
    mix_repetitions: {min: 1, max: 20, source: "prototype bound"}
  specialized:
    aspirate:
      air_gap_ul: {type: number, min: 0, default: 0, doc: "Air drawn at the well top after the liquid, so the tip does not drip on the move. The tip must hold the liquid and the air. The next dispense pushes the air out first: the pipette moves volume plus air, the twin counts only the liquid."}
    dispense:
      blow_out: {type: boolean, default: false, doc: "After the dispense, move to the top of the same well and blow out in place. The tip must hold nothing else: a blow-out pushes out everything in it. A later aspirate or mix in the same tip first has the plunger prepared above the well."}
      blow_out_flow_rate_ul_s: {type: number, doc: "Flow rate of the blow-out; needs blow_out. Omitted: the pipette's own blow-out rate."}
      blow_out_height_mm: {type: number, min: -5, max: 5, default: 0, doc: "Height of the blow-out above the well top (negative: into the well); needs blow_out. Stay above the liquid: a blow-out under the surface is refused.", source: "prototype bound around the well top"}
      touch_tip: {type: boolean, default: false, doc: "After the dispense (and the blow-out), touch the tip to the four sides of the same well to knock off a drop. Emulated with moves at 60 mm/s (Opentrons' own touch-tip speed): the robot-server's touchTip needs loaded labware, and AHC sends plates as coordinates."}
      touch_tip_v_offset_mm: {type: number, min: -10, max: 0, default: -1, doc: "Depth of the touch below the well top; needs touch_tip. Stay above the liquid: a touch under the surface is refused.", source: "Opentrons touch_tip v_offset (default -1 mm); prototype bound"}
      touch_tip_radius: {type: number, min: 0.5, max: 1.0, default: 1.0, doc: "How far the tip travels toward each wall, as a fraction of the well radius; needs touch_tip.", source: "Opentrons touch_tip radius (default 1.0); lower bound is a prototype bound"}
    mix:
      blow_out: {type: boolean, default: false, doc: "Blow out at the top of the well after the last cycle, at the pipette's own blow-out rate. The tip must hold nothing else: a blow-out pushes out everything in it."}
  instruments:
    p20_single_gen2:
      title: P20 single-channel GEN2
      capabilities: [liquid_handling]
      channels: 1
      shared_container: true
      equal_volumes: true
      limits:
        volume_ul: {min: 1, max: 20, source: "Opentrons pipetteNameSpecs p20_single_gen2"}
        flow_rate_ul_s: {min: 0.08, max: 24, default: 7.56, source: "Opentrons pipetteNameSpecs p20_single_gen2"}
      specialized:
        aspirate:
          air_gap_ul: {max: 20, source: "Opentrons pipetteNameSpecs p20_single_gen2 maximum volume; the mounted tip caps it further"}
        dispense:
          blow_out_flow_rate_ul_s: {min: 0.08, max: 24, default: 7.56, source: "Opentrons pipetteNameSpecs p20_single_gen2 defaultBlowOutFlowRate (API level 2.6 value)"}
    p20_multi_gen2:
      title: P20 8-channel GEN2
      capabilities: [liquid_handling]
      channels: 8
      shared_container: false
      equal_volumes: true
      limits:
        volume_ul: {min: 1, max: 20, source: "Opentrons pipetteNameSpecs p20_multi_gen2"}
        flow_rate_ul_s: {min: 0.08, max: 24, default: 7.6, source: "Opentrons pipetteNameSpecs p20_multi_gen2"}
      specialized:
        aspirate:
          air_gap_ul: {max: 20, source: "Opentrons pipetteNameSpecs p20_multi_gen2 maximum volume; the mounted tip caps it further"}
        dispense:
          blow_out_flow_rate_ul_s: {min: 0.08, max: 24, default: 7.6, source: "Opentrons pipetteNameSpecs p20_multi_gen2 defaultBlowOutFlowRate (API level 2.0 value)"}
    p300_single_gen2:
      title: P300 single-channel GEN2
      capabilities: [liquid_handling]
      channels: 1
      shared_container: true
      equal_volumes: true
      limits:
        volume_ul: {min: 20, max: 300, source: "Opentrons pipetteNameSpecs p300_single_gen2"}
        flow_rate_ul_s: {min: 1, max: 275, default: 92.86, source: "Opentrons pipetteNameSpecs p300_single_gen2"}
      specialized:
        aspirate:
          air_gap_ul: {max: 300, source: "Opentrons pipetteNameSpecs p300_single_gen2 maximum volume; the mounted tip caps it further"}
        dispense:
          blow_out_flow_rate_ul_s: {min: 1, max: 275, default: 92.86, source: "Opentrons pipetteNameSpecs p300_single_gen2 defaultBlowOutFlowRate (API level 2.6 value)"}
    p300_multi_gen2:
      title: P300 8-channel GEN2
      capabilities: [liquid_handling]
      channels: 8
      shared_container: false
      equal_volumes: true
      limits:
        volume_ul: {min: 20, max: 300, source: "Opentrons pipetteNameSpecs p300_multi_gen2"}
        flow_rate_ul_s: {min: 1, max: 275, default: 94, source: "Opentrons pipetteNameSpecs p300_multi_gen2"}
      specialized:
        aspirate:
          air_gap_ul: {max: 300, source: "Opentrons pipetteNameSpecs p300_multi_gen2 maximum volume; the mounted tip caps it further"}
        dispense:
          blow_out_flow_rate_ul_s: {min: 1, max: 275, default: 94, source: "Opentrons pipetteNameSpecs p300_multi_gen2 defaultBlowOutFlowRate (API level 2.0 value)"}
    p1000_single_gen2:
      title: P1000 single-channel GEN2
      capabilities: [liquid_handling]
      channels: 1
      shared_container: true
      equal_volumes: true
      limits:
        volume_ul: {min: 100, max: 1000, source: "Opentrons pipetteNameSpecs p1000_single_gen2"}
        flow_rate_ul_s: {min: 3, max: 812, default: 274.7, source: "Opentrons pipetteNameSpecs p1000_single_gen2"}
      specialized:
        aspirate:
          air_gap_ul: {max: 1000, source: "Opentrons pipetteNameSpecs p1000_single_gen2 maximum volume; the mounted tip caps it further"}
        dispense:
          blow_out_flow_rate_ul_s: {min: 3, max: 812, default: 274.7, source: "Opentrons pipetteNameSpecs p1000_single_gen2 defaultBlowOutFlowRate (API level 2.6 value)"}
---

# Opentrons OT-2

The components are the two mounts, `left` and `right`, each built from the GEN2 pipette it carries:
AHC does not dictate the pipettes. `configure_devices` reads them from the robot into
`options.mounts` and the person confirms them together with the robot's identity; a simulator
carries the mounts the config names. `lab_overview` shows what each mount carries, and
`describe_device` each component's channels, limits and tips. An empty mount, or one with a pipette
this file does not describe, has no component (`lab_overview` lists the latter under `mounts`), and
addressing it is refused (`unknown_component`).

After the person confirmed the config, a change of pipettes is a different robot setup:
`load_layout` is refused (`mount_mismatch`) until `configure_devices` reads them again and the
person confirms.

## Pipettes

- An 8-channel head is rigid: its eight nozzles sit 9 mm apart in one column and share one plunger,
  so every nozzle moves the same volume and each nozzle needs its own well. It serves one full column
  per call, top to bottom (`plate:col2`, or `plate:A2:H2`), and picks up a full column of tips.
- A single-channel pipette takes one tip and serves one well per call, from any well, also of a
  reservoir. A row of a plate is `plate:rowA`, but one well per call.
- Each pipette takes its own tip size, by PyLabRobot's rule for that pipette: a P20 takes 10 and
  20 uL tips, a P300 200 and 300 uL, a P1000 1000 uL. A tip rack the pipette cannot use is refused
  with the sizes it takes. An omitted pick-up takes the next fresh tips, in layout order, from the
  racks whose tips fit that pipette; a deck with two pipettes of different sizes needs a rack for each.
- Volumes are per target and limited by the pipette (`volume_ul` in `describe_device`); an 8-channel
  head needs them all equal. A volume outside a pipette's range is refused: use the other mount, if
  its range holds it, or split the transfer into parts within the range.

## Layout

Labware sits in slots 1–11; slot 12 is the fixed trash. Reservoirs and deep-well plates hold shared
liquids: an 8-channel head draws from one column of a deep-well plate (`cor_96_wellplate_2mL_Vb`),
for example `reagents:col1` (the same wells as `reagents:A1:H1`), and an alias such as `buffer`
can name that column; a single-channel pipette can draw from any well, for example one well of
`nest_12_troughplate_15000uL_Vb`.

A rack that is not full: `tips_empty` (spots with no tip) and `tips_returned` (spots holding a used
tip) are optional layout keys, in the tip target form, e.g. `tips_empty: ["tips:B1:H1", "tips:A2:F2"]`
and `tips_returned: ["tips:A1"]`. An omitted pick-up skips both; naming an empty spot is refused
(`tip_spot_empty`), a returned one is picked up only when named. To continue from the last run's deck,
load `find_layout_references` → `last_deck.layout`; the person confirms it at the deck check
(`ask_person` lists the volumes and these spots).

On a real robot every `load_layout` starts a new run and homes the robot, before the deck is
checked: the person keeps hands and the deck clear (the agent says so in chat, in the same message
before the dialog right ahead of `load_layout`, so approving it also means the robot may home). The new run does not know tips an earlier run
left on the pipettes, so while tips may be on, `load_layout` is refused (`tips_unconfirmed`). Tips
the current run still holds are dropped with `drop_tips` first. Any others the person removes by
hand and confirms with one check for both pipettes: `verify(check="tips_mounted", expect="none")`
without a device. Every such check on the real robot returns `ask_person`, one sentence of what the
person confirms (slots, labware and liquids; the tips on a pipette; the wells and volume), and the
agent passes it as the note of `record_verdict(verdict="pass")`: the person's approval of that call
is the confirmation, a denial is not.

## Liquid handling

- `liquid_height` is in mm above the cavity bottom, 1 mm when omitted (Opentrons' own clearance); the
  result records the height used. An aspirate or mix whose tips would end above the liquid, by the
  twin's volume and the labware geometry, is refused (`above_liquid`): draw less, or pass the lower
  height the hint gives.
- `mix` is native: the pipette cycles in place.
- A little liquid stays in a tip after a plain dispense. `blow_out` (dispense, mix) and `touch_tip`
  (dispense) deal with it at the well top, `air_gap_ul` (aspirate) keeps a drop from falling on the
  move; `get_params` lists their bounds. A blow-out empties the tip, so it is refused while the tip
  still holds liquid, and the plunger is prepared again above the well before the next aspirate or mix
  in that tip. The air gap takes room in the tip, and the next dispense pushes it out first.
- Returned tips go back to their spots but are never picked as the next fresh tips; name them to use
  them again.
- Positions: tip racks use Opentrons' official definitions with the robot's own calibration (deck,
  pipette offset, tip length); other labware positions come from PyLabRobot's labware geometry and
  are sent as coordinates. Labware Position Check offsets are not applied: running it in the
  Opentrons App changes nothing here. Check positions and heights with a dry run, tips only and then
  water, before anything that matters.

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

- Draw with an 8-channel head from a reservoir whose wells do not form a column of 8 (the 12-well
  reservoir is one row); use a single-channel pipette, or keep the liquid in a column of a deep-well plate.
- Drive a pipette this file does not describe (a GEN1 pipette, a P50, a 96-channel head).
- Use modules (thermocycler, heater-shaker) through this server yet.
