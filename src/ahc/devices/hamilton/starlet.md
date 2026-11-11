---
model: hamilton.starlet
brand: hamilton
title: Hamilton STARlet with 8 independent 1000 uL channels
backends:
  sim: "PyLabRobot 1.0 STARSimulationDriver: firmware-level simulation built from a recorded 2021 STARlet. Nothing reaches a device."
  usb: "A real STARlet through star_bridge.py on its USB host (PyLabRobot firmware commands over an ssh link); a person on site."
emergency_stop:
  usb: "On a real STARlet the stop keeps AHC from sending anything more; a command the instrument is already running finishes. To stop motion at once, switch the instrument off at its power switch."
layout:
  kind: tracks
  tracks: {min: 1, max: 30}
  carriers: [TIP_CAR_480_A00, PLT_CAR_L5AC_A00, PLT_CAR_L5_DWP, Trough_CAR_4R200_A00]
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
      liquid_height_mm: {min: 0, max: 40, default: 0, doc: "Omitted: the cavity bottom, PyLabRobot's STAR default (liquid-level detection off).", source: "prototype bound above the cavity bottom; also capped by the container depth and, for aspirate and mix, the liquid surface"}
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
        post_mix_repetitions: {type: integer, min: 1, max: 99, follows: mix_repetitions, doc: "Cycles for the native post-dispense mix. Needs post_mix_volume_ul.", source: "mix cycles range 0-99"}
    translations:
      mix: "STAR channels have no standalone mix. A separate mix call runs as one aspirate of 0 uL whose pre-mix does the cycles (one firmware command); post_mix_* on the dispense mixes right after a dispense."
  head96:
    title: 96-channel head (fitted in the simulated frame)
    capabilities: []
    note: "Not wired in this prototype; liquid-handling calls on it are refused as not supported."
---

# Hamilton STARlet

Eight independent channels on one arm. Each channel can take its own volume, and several channels may
draw from one container (a trough), so `targets: ["buffer"]` with eight tips mounted sends all eight
channels into the trough.

Simulated here: PyLabRobot's firmware-level simulator of a recorded 2021 STARlet. The lab's STARlet,
by its own firmware report, has 8 independent 1000 uL channels and a CO-RE 96 head as that frame does,
plus an iSWAP and an autoload (1D barcode reader); it has no 5 mL channels, CO-RE 384 head,
labware-handling channels, washer or pump station. The volume, flow rate and mix limits still come from
the simulated frame's firmware ranges, not from Hamilton's pipetting specification: 0.1 uL is the drive's step, not a volume a tip
delivers. A real STARlet is driven through the `usb` backend (see "A real STARlet" below), with the same limits.

## A real STARlet (backend `usb`)

The instrument is plugged into a USB host: a small Linux machine with PyLabRobot and `star_bridge.py` on it. This
Mac reaches the host over ssh with key login and never opens the instrument itself (macOS cannot enumerate it).

- The entry: `options.bridge` is the host's ssh name; the person gives it, never guess one. Optional:
  `bridge_python` and `bridge_script` (where the bridge is on the host), `tip_waste` (true only when the person
  says a tip waste is fitted), `channel_spacing_mm` (see below). `configure_devices` reads the instrument's serial (the four characters its firmware
  answers) with reads only, and writes it as `options.serial`; the person confirms that serial, and a confirmed
  entry without one is refused (`serial_required`). Another instrument answering on that host is refused
  (`wrong_robot`).
- `load_layout`: the first load of a session, and the first after a stop, an error or a disconnect, initializes the
  instrument: its channels, 96-head, iSWAP and autoload all move, so hands and deck must be clear before the
  person approves it. The initialization takes the 96 head to its waste position (X -42.0, Y 120.3) at its travel
  height and does not lower it there; that position is the STARlet's CO-RE 96 slide waste as VENUS places it. A
  later load on the same link only swaps the deck in the model and moves nothing. Tips sensed on a channel before
  initialization refuse the load (`tips_on_channels`): they come off with `recover_tips` (below) while the
  instrument still reports itself initialized, otherwise outside AHC (they did not come off by hand on the lab's
  instrument).
- Channel spacing: AHC plans with the width each channel reports. On an instrument whose channels report more than
  the 9 mm between a plate's rows, a column of tips, aspirations or dispenses goes one channel at a time.
  `channel_spacing_mm` (a number, at least 9) is set only when the person on site states that the channels can sit
  that close; the person confirms it with the config ("channels planned N mm apart as the person states"). AHC
  refuses a value below the minimum pitch the instrument's master reports, or when it reports none
  (`channel_spacing_refused`, before anything moves), and after initialization plans every channel with the stated
  value; `identity.channel_spacing_mm` then shows the widths the channels reported and the spacing planned.
- Tips go back to their rack (`drop_tips` with `mode='return'`). `mode='discard'` is refused (`no_tip_waste`) unless
  the confirmed config says `tip_waste: true`.
- The emergency stop (the task folder's, from the dashboard or `ahc-estop`): nothing more is sent to the
  instrument, and AHC tells the bridge to disarm over the link that is already open. No firmware abort exists: the
  command the instrument is already running finishes. The only thing that stops motion at once is the instrument's
  power switch: switch it off. The link is then lost and the outcome is unknown. The person sees to it that no tips
  are on the channels and confirms it (`verify(check='tips_mounted', expect='none')`), and only then does a new
  `load_layout` run; it refuses to initialize while the tip sensor reports tips (`tips_on_channels`). CO-RE tips did
  not come off by hand on the lab's instrument, so there is `recover_tips` (below): the emergency stop does not
  cancel the pick-up command in flight, and the tips it picked up stay on the channels. After a power-off
  `recover_tips` cannot run (`recovery_needs_initialized`): the tips come off outside AHC, for example with the
  vendor's software.
- After a stop, a lost link or a command that failed, the person checks the channels for tips and the instrument
  before the next load: `load_layout` waits for their pass on `verify(check='tips_mounted', expect='none')`
  (`tips_unconfirmed`). That load arms the bridge again; there is nothing to release.
- Tips that stay on the channels (`recover_tips`): the agent asks the person first, in chat: back into the rack spots
  the tips came from (`mode='return'`), or into the tip waste (`mode='discard'`, the default, and only where the
  confirmed config says `tip_waste: true`; otherwise `no_tip_waste` and the only choice is the rack). The person's
  approval of the call is their go-ahead for the move. It opens a link of its own and never initializes anything
  (initializing the channels ejects the tips at the waste position): it reads the tip sensor, requires the
  instrument to still report itself initialized, raises the channels to Z safety and retracts the 96 head, then
  sends one return or one discard, and lets go of the link. `tips` names the spots the mounted tips were picked up
  from (for example `tips:col3`, from the interrupted call in `get_run`) and is required for both modes. The
  `layout` is the deck as it stands (racks where they physically are, those spots as `tips_empty`), or a saved
  layout's `name`, else the last deck; it must match a layout this task folder saved or ran, because no deck check
  runs first. It saves nothing and clears no doubt: afterwards the person passes
  `verify(check='tips_mounted', expect='none')`, and `load_layout` declares the returned spots as `tips_returned`
  (after a discard, the emptied spots as `tips_empty`). Liquid that was in the tips stays in them in the rack, or
  goes into the waste with them. A result whose `sensed_after` still shows a tip: the person looks at the
  instrument; do not retry.
- Refusals to tell the person, never to retry: `tips_on_channels`, `head96_unreadable` (the 96 head did not report
  its travel height; nothing moved), `channel_spacing_refused` (the stated spacing is below the minimum pitch the
  instrument reports; nothing moved), `initialization_failed` (it may have stopped part-way: they look at the
  instrument), `instrument_read_only` and `link_lost` (the link or the guard closed), `no_tip_waste`,
  `halt_unconfirmed` (the stop was sent but a command sent before it was not answered: the power switch).
  From `recover_tips`: `recovery_needs_initialized` (the instrument was switched off or reset since the pick-up, so
  AHC cannot put the tips back: the person decides how they come off), `recovery_mismatch` (the tips sensed are not
  the ones the target names; nothing moved: they look at the instrument), `layout_unknown` (the deck matches none this
  task folder ran), `layout_incomplete` and `layout_required` (pass the deck as it stands).
- Status: the `usb` backend has had its first runs on one instrument (2026-11-11, with a person on site): the
  read-only connect, the initialization, a pick-up, a 50 uL transfer and the return of the tips one channel at a
  time and with 8 channels at once (`channel_spacing_mm: 9`), the stop during a column's pick-up, `recover_tips`
  with `mode='return'` and the load after it. Not yet on an instrument: an initialization with the 96 head's waste
  position as AHC now sets it (with PyLabRobot's default the head touched the edge of the waste box at the first
  initialization, so the person watches the head at the next one after a power-on), `mix`, a tip waste
  (`mode='discard'` in `drop_tips` and `recover_tips`), a stop with liquid in the tips, and the power switch during
  a command. Everything else is checked against PyLabRobot's simulator and a stand-in USB host. Runs on an
  instrument need a person at the power switch.

## Layout

Carriers sit on tracks 1–30. A layout lists carriers with their track and the labware on each carrier
site, for example a `PLT_CAR_L5_DWP` on track 1 and a `TIP_CAR_480_A00` on track 13 holding a tip rack
at site 0. Plates go on a `PLT_CAR_L5AC_A00` or a `PLT_CAR_L5_DWP` (deep-well plates, Hamilton 93522-01),
troughs on a `Trough_CAR_4R200_A00`. The tip and plate carriers take 6 tracks each, the trough carrier 2.
Site 0 is a carrier's front-most site, nearest the person. VENUS numbers positions the other way, from
the back: on a 5-site carrier, VENUS position 5 is site 0 and position 1 is site 4.

A rack that is not full: the optional layout keys `tips_empty` and `tips_returned` list tip spots
without a tip and spots holding a used tip (`tips_empty: ["tips:A1:H1"]`); see `last_deck` in
`find_layout_references` for continuing from the last run's deck.

## Liquid handling

- `volumes` are per target; one number is broadcast to every target.
- Waste: the waste positions exist on the deck model (the 8 channels eject to the waste block at the right
  end of the deck, the CO-RE 96 head to a slide waste left of track 1), but a real instrument may have no tip
  waste fitted. That is why discarding is off by default on `usb`: see `drop_tips` below.
- `pick_up_tips` takes 1 to 8 tips, one per channel in use (`tips:col1`, `tips:A1:D1`); omitted, the
  next fresh column.
- `liquid_height` is in mm above the cavity bottom; omitted, the cavity bottom (PyLabRobot's STAR
  default, liquid-level detection off), and the result records it. An aspirate or mix whose tips
  would end above the liquid is refused (`above_liquid`).
- Liquid classes are looked up from the tip, `jet` and `blow_out`; the other specialized parameters
  override single liquid-class values.
- Mixing: pass `post_mix_volume_ul` and `post_mix_repetitions` on the dispense to mix right after
  it. A separate `mix` call runs as a 0 uL aspirate with a pre-mix, also a single command. Either
  runs at 100 uL/s when the call gives no flow rate, and the result says so.
- One aspiration feeding several dispenses: each dispense also pushes out the tip's transport air
  and a liquid-class volume correction, so the last one can run out of piston travel (refused as
  `piston_travel`). Aspirate about 10 uL more than the dispenses add up to and leave that surplus in
  the tip (it goes with the tip), or aspirate once per dispense.

## Sensors usable for verification

Tip presence per channel (`invoke` op `sense_tip_presence`; `verify(check="tips_mounted")` reads it
too). Liquid-level detection and TADM pressure curves exist on the device but are not wired in this
prototype.

## Cannot

- Use the 96-head through this server yet.
- Move plates or use the autoload: the iSWAP, the CoRe grippers and the autoload are not wired in this prototype.
