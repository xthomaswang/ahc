---
model: hamilton.starlet
brand: hamilton
title: Hamilton STARlet with 8 independent 1000 uL channels
backends:
  sim: "PyLabRobot 1.0 STARSimulationDriver: firmware-level simulation built from a recorded 2021 STARlet. Nothing reaches a device."
layout:
  kind: tracks
  tracks: {min: 1, max: 30}
  carriers: [TIP_CAR_480_A00, PLT_CAR_L5AC_A00, Trough_CAR_4R200_A00]
  labware:
    tip_racks: [hamilton_96_tiprack_300uL_filter, hamilton_96_tiprack_1000uL_filter]
    plates: [Cor_96_wellplate_360ul_Fb, cor_96_wellplate_2mL_Vb]
    troughs: [Hamilton_1_trough_200ml_Vb]
components:
  pip:
    title: 8 independent pipetting channels
    capabilities: [liquid_handling]
    channels: 8
    shared_container: true
    equal_volumes: false
    limits:
      volume_ul: {min: 0.1, max: 1000, source: "channel drive 0-1250 uL in 0.1 uL steps (simulated frame config); the mounted tip caps it further"}
      flow_rate_ul_s: {min: 0.4, max: 500, source: "pipetting speed range 4-5000 x 0.1 uL/s (simulated frame config)"}
      liquid_height_mm: {min: 0, max: 40, source: "prototype bound above the cavity bottom; also capped by the container depth"}
      mix_repetitions: {min: 1, max: 99, source: "mix cycles range 0-99 (simulated frame config)"}
    specialized:
      aspirate:
        jet: {type: boolean, default: false, doc: "Pick the liquid class for a later jet dispense."}
        blow_out: {type: boolean, default: false, doc: "Pick the liquid class for a later blow-out dispense."}
        pre_wetting_volume_ul: {type: number, min: 0, max: 99.9, follows: volume_ul, doc: "Drawn and returned before the draw. Liquid-class value when omitted.", source: "pre-wetting range 0-999 x 0.1 uL"}
        settling_time_s: {type: number, min: 0, max: 9.9, doc: "Wait in the liquid after the draw. Liquid-class value when omitted.", source: "settling range 0-99 x 0.1 s"}
        transport_air_volume_ul: {type: number, min: 0, max: 50, doc: "Air drawn after the liquid. Liquid-class value when omitted.", source: "transport air range 0-500 x 0.1 uL"}
        swap_speed_mm_s: {type: number, min: 0.3, max: 160, doc: "Speed of leaving the liquid. Liquid-class value when omitted.", source: "swap speed range 3-1600 x 0.1 mm/s"}
      dispense:
        jet: {type: boolean, default: false, doc: "Jet dispense (from above the liquid)."}
        blow_out: {type: boolean, default: false, doc: "Blow out the tip after dispensing."}
        settling_time_s: {type: number, min: 0, max: 9.9, doc: "Wait in the liquid after dispensing. Liquid-class value when omitted.", source: "settling range 0-99 x 0.1 s"}
        transport_air_volume_ul: {type: number, min: 0, max: 50, doc: "Transport air. Liquid-class value when omitted.", source: "transport air range 0-500 x 0.1 uL"}
        swap_speed_mm_s: {type: number, min: 0.3, max: 160, doc: "Speed of leaving the liquid. Liquid-class value when omitted.", source: "swap speed range 3-1600 x 0.1 mm/s"}
        post_mix_volume_ul: {type: number, min: 1, max: 1000, follows: volume_ul, doc: "Native mix right after this dispense, in the same firmware command. Needs post_mix_repetitions. Prefer it over a separate mix call."}
        post_mix_repetitions: {type: integer, min: 1, max: 99, doc: "Cycles for the native post-dispense mix. Needs post_mix_volume_ul.", source: "mix cycles range 0-99"}
    translations:
      mix: "STAR channels have no standalone mix. A separate mix call runs as one aspirate of 0 uL whose pre-mix does the cycles (one firmware command); post_mix_* on the dispense mixes right after a dispense."
  head96:
    title: 96-channel head (fitted in the simulated frame)
    capabilities: []
    note: "Not wired in this prototype; liquid-handling calls on it are refused as not supported."
---

# Hamilton STARlet

Eight independent channels on one arm. Each channel can take its own volume, and several channels may
draw from one container (a trough), so `targets: ["diluent"]` with eight tips mounted sends all eight
channels into the trough.

## Layout

Carriers sit on tracks 1–30. A layout lists carriers with their track and the labware on each carrier
site, for example a `TIP_CAR_480_A00` on track 1 holding a tip rack at site 0. Plates go on a
`PLT_CAR_L5AC_A00`, troughs on a `Trough_CAR_4R200_A00`.

## Liquid handling

- `volumes` are per target; one number is broadcast to every target.
- Liquid classes are looked up from the tip, `jet` and `blow_out`; the other specialized parameters
  override single liquid-class values.
- Mixing: pass `post_mix_volume_ul` and `post_mix_repetitions` on the dispense to mix right after
  it. A separate `mix` call runs as a 0 uL aspirate with a pre-mix, also a single command.
- One aspiration feeding several dispenses: each dispense also pushes out the tip's transport air
  and a liquid-class volume correction, so the last one can run out of piston travel (refused as
  `piston_travel`). Aspirate about 10 uL more than the dispenses add up to and dispense the rest to
  waste, or aspirate once per dispense.

## Sensors usable for verification

Tip presence per channel (`invoke` op `sense_tip_presence`). Liquid-level detection and TADM pressure
curves exist on the device but are not wired in this prototype.

## Cannot

- Use the 96-head through this server yet.
- Move plates (iSWAP and CoRe grippers are not wired in this prototype).
