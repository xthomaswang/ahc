---
model: opentrons.ot2
brand: opentrons
title: Opentrons OT-2 with a P300 8-channel GEN2 (right) and a P20 single-channel GEN2 (left)
backends:
  sim: "Opentrons robot-server (opentrons-ot2 repo) with a virtual Smoothie, reached over its HTTP API."
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
      liquid_height_mm: {min: 0, max: 40, source: "prototype bound above the cavity bottom; also capped by the container depth"}
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
      liquid_height_mm: {min: 0, max: 40, source: "prototype bound above the cavity bottom; also capped by the container depth"}
      mix_repetitions: {min: 1, max: 20, source: "prototype bound"}
    specialized: {}
    translations: {}
---

# Opentrons OT-2

Two mounts. The right P300 8-channel head is rigid: its eight nozzles sit 9 mm apart in one column
and share one plunger, so every nozzle moves the same volume and each nozzle needs its own well.

## Layout

Labware sits in slots 1–11; slot 12 is the fixed trash. Shared liquids for the 8-channel head go in
one column of a deep-well plate (`cor_96_wellplate_2mL_Vb`), for example `reagents:A1:H1`, and an
alias such as `diluent` can name that column.

## Liquid handling

- `volumes` are per target, but the 8-channel head needs them all equal.
- Tips are picked up as one full column, top to bottom.
- `mix` is native: the head cycles in place.
- Positions come from PyLabRobot's deck model and are sent as coordinates; run Labware Position
  Check on a real robot before trusting them.

## Sensors usable for verification

None: OT-2 GEN2 pipettes report no tip presence. Verification of this device needs cameras.

## Cannot

- Draw with the 8-channel head from a single-well reservoir column through this adapter.
- Use modules (thermocycler, heater-shaker) through this server yet.
