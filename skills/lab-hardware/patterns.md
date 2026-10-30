# Planning patterns

General patterns for liquid-handling work on any AHC device. Fit each one to the component facts
that `lab_overview` and `get_params` give. Numbers marked "e.g." are examples from one device, not
rules. On the OT-2 each mount carries the robot's own GEN2 pipette. Without anything else (and in the
simulator) `ot2.right` is a P300 8-channel head and `ot2.left` a P20 single channel. On the STARlet,
`starlet.pip` is 8 independent channels.

Each pattern says when to use it, the calls, and the pitfalls with the refusal they produce.

## Read the components first

| Fact | What it means for the plan |
|---|---|
| `channels` | The most tips one pick-up takes, and the most wells one call serves (one per mounted tip). |
| `equal_volumes: true` | One volume for every channel in a call (`equal_volumes_required`). |
| `shared_container: false` | Each channel needs its own well: a liquid for an 8-channel head sits in a column of 8 wells (`distinct_wells_required`). |
| `volume_ul` min and max (`get_params`; `task_limit: true` marks the task's own) | Every transfer and mix volume. A volume below the minimum needs another component. |
| The mounted tip's size | What the tips can hold at once (`exceeds_tip`). |
| `liquid_height_mm` default | Where the tips go when a call gives no height. |

Each pipette takes its own tip size: a P20 10 or 20 uL, a P300 200 or 300 uL, a P1000 1000 uL. The
OT-2's tip racks are `opentrons_96_tiprack_10ul`, `_20ul`, `_300ul` and `_1000ul`, and its filter racks
`opentrons_96_filtertiprack_10ul`, `_20ul`, `_200ul` and `_1000ul`. A plan that uses a pipette needs a
rack of its tips on the deck (ask the person; the built-in OT-2 example layout has racks for the
default pair). An omitted pick-up takes the next fresh tips, in layout order, from the racks whose
tips fit that pipette; name the tips to take them from another rack.

Count the sources too. A draw may not take more than a container holds (`insufficient_liquid`), and
it must leave the liquid surface above the tips (`above_liquid`). Plan a reserve in every source
well, and ask the person how much is really there.

## 1. One reagent into many wells

When: the same volume of one liquid goes into many wells or columns.

Calls:
- A component that may share a container (`shared_container: true`, e.g. the STARlet with a
  trough): `aspirate(targets=["buffer"], volumes=V)` draws with every mounted tip; then `dispense`
  into one well per tip.
- A head that may not (e.g. an 8-channel head such as the default `ot2.right`): keep the reagent in
  one column of a deep-well plate and alias it (`"buffer": "reagents:col1"`).
  `aspirate(targets=["buffer"], volumes=V)` draws one well per channel;
  `dispense(targets=["plate:col2"], volumes=V)`, then `plate:col3`, and so on.
- `pick_up_tips` once, the aspirate/dispense pairs, then `drop_tips`.

Pitfalls:
- The source needs every draw plus a reserve: N passes of V take N × V from each well of a source
  column, and N × V per tip from a shared trough.
- One aspirate may feed several dispenses: draw a little extra and dispense the rest into a waste
  container the layout names. On the STARlet the last dispense otherwise runs out of piston travel
  (`piston_travel`).
- The same tips serve every dispense only while they touch nothing but this reagent (pattern 8).
- A reservoir well serves one channel at a time: an 8-channel head draws from a column of 8 wells,
  a single channel from any well. The OT-2's 1-well reservoir (`nest_1_troughplate_195000uL_Vb`)
  therefore serves a single channel only; its 12-well one is `nest_12_troughplate_15000uL_Vb`.

## 2. Per-well volumes from a table

When: each well gets its own volume, e.g. a normalization or a plate map in `.ahc/protocols/`.

Calls:
- Independent channels (`equal_volumes: false`, e.g. the STARlet): one call serves one well per
  mounted tip, one volume per target: `dispense(targets=["plate:A1", "plate:B1"], volumes=[12.5, 30])`
  with two tips. An aspirate from a shared trough takes the same per-channel volumes.
- A head with equal volumes plus a single channel (e.g. the OT-2's default pair): the single channel,
  one well per call, with its own tips (see "Read the components first"). Or the head first adds a
  common base to a whole column (that column's smallest volume, if it is at least the head's minimum),
  and the single channel adds each well's difference.

Pitfalls:
- Check every volume in the table against each component's min and max before the first call. A
  volume below every minimum cannot be pipetted as asked: ask the person.
- A volume above the single channel's maximum: equal parts (pattern 7).
- Well names are a row letter and a column number (`B3` is row B, column 3).

## 3. Copy columns or a region (stamping, replicates, pooling)

When: liquids go from source columns into columns of another plate, or one column into several.

Calls:
- A multi-channel head: `aspirate(targets=["source:col1"])`, `dispense(targets=["plate:col1"])`,
  one pass per column. A region is one pass per full column it covers.
- k replicates of one column: one pass per replicate, or one aspirate of k × V that fits the tip and
  k dispenses (pattern 1's pitfall).
- Pooling many wells into one: a single channel well by well; independent channels into one shared
  container; an 8-channel head column by column into the same 8 wells.

Pitfalls:
- A head whose channels move together (e.g. an 8-channel head such as the default `ot2.right`) serves
  one full column per call, top to bottom: part of a column, or a row (`plate:rowB`, 12 wells), is
  refused. Fill those wells one by one with a single channel, or turn the plan so the work runs along
  columns.
- Fresh tips for each source column whose liquids differ.
- Tip budget: a whole 96-well plate, column by column, takes 12 columns of tips.

## 4. Dilution series

When: each well holds the previous well's concentration divided by a factor F.

Volumes for a final volume Vf in every well: transfer T = Vf / (F − 1); diluent Vf in every well of
the series but the first; stock Vf + T in the first. E.g. F = 2 and Vf = 100 uL: T = 100 uL and
200 uL of stock.

Calls, across columns with a multi-channel head (one series per row, all rows at once):
1. Diluent into columns 2 to n (pattern 1).
2. Stock into column 1.
3. For each column k: fresh tips, aspirate T from column k, dispense it into column k + 1, mix
   there, drop the tips.
4. If every well must end at Vf: draw T from the last column into a waste container the layout names
   (a trash takes tips only).

A series down a column (A to H), or in a single row, runs one well at a time with a single channel.

Pitfalls:
- Fresh tips for every transfer: carry-over shifts every later concentration.
- Mix before drawing from a well (pattern 8).
- Check T against the min and max of the component that moves it.

## 5. Assemble a mix, then aliquot it

When: several components make one mix, which then goes into many wells.

Volumes: each mix well needs every aliquot that will be drawn from it plus a reserve. For an 8-channel
head, make the mix in all 8 wells of one deep-well column, each holding what its channel will
dispense.

Calls: each component into the mix wells, fresh tips per component, the largest volume first; `mix`
the mix wells; then pattern 1 from them.

Pitfalls:
- The mix wells must hold the total (`overfill`).
- A mix moves at most one tip's volume per cycle: a large volume needs more repetitions, within
  `mix_repetitions`.
- A component volume below the head's minimum goes in well by well with the single channel.

## 6. Standards and samples in replicates

When: a standard curve and samples, each in duplicate or triplicate, from a plate map.

Calls:
- Standards made as a series across columns with a multi-channel head (pattern 4): every row runs the
  same series, so the rows are the replicates.
- Standards or samples the person prepared down one source column (one per row, a blank included):
  copy that column into as many plate columns as there are replicates (pattern 3).
- A single channel, or independent channels: well by well from the plate map.

Pitfalls:
- Write the plate map into the plan (which wells hold which standard or sample); when it was not
  given, show it to the person before the first call.
- Fresh tips between different standards or samples. Replicates of one liquid may share tips while
  the wells they go into are empty.

## 7. A transfer above a cap

When: a volume is above `volume_ul`'s max, a task limit on it, or the tip's size.

Calls: n = ceil(V / cap) equal aspirate/dispense pairs of V / n. E.g. 250 uL under a 100 uL cap:
3 × 83.3 uL.

Pitfalls:
- Equal parts, not the cap plus a remainder: every part stays inside the limits.
- Each part must still reach the minimum; if it would not, use another component or ask the person.
- The target must hold the whole volume (`overfill`).

## 8. Mixing and tip changes

Mix when two liquids meet in a well and the next step draws from it (a series, a master mix), or when
the protocol says so.
- Volume: about half of what the well holds, and at most the tip. The tips must stay under the
  surface after each draw (`above_liquid`). E.g. 100 uL in a flat 360 uL well at the OT-2's 1 mm
  default: about 50 uL.
- On the STARlet a separate `mix` is translated; right after a dispense, mix with the dispense's
  `post_mix_volume_ul` and `post_mix_repetitions` instead (`get_params`).

Change tips:
- before a different liquid;
- after the tips touched another liquid: a dispense at the default height goes into whatever the
  well already holds, and a mix always does;
- between the steps of a series, and between samples.

Keep tips for the same liquid into empty wells, and for replicates of one liquid. To add a reagent to
filled wells and keep the tips clean, dispense above their liquid (a `liquid_height` over the
surface and below the well's top).

`drop_tips(mode="return")` puts tips back in their spots. They are never picked as fresh again; reuse
them only by name, and only for the same liquid.

## 9. Tip budget

Count before the first call: a pick-up by a head whose channels move together takes a full column;
a single channel takes one tip; independent channels one tip per channel in use. Compare the total
with `get_state.tips_left`, the fresh tips in each rack (`tips_returned` counts returned tips apart).
Running out stops the work half done (`out_of_tips`). Another rack changes the deck: ask the person.

## 10. Check the result

Before `report_decision(plan_done=true)`, call `verify(check="volumes_match", expected={...})` with
the final volume of every well you changed, in any target form, e.g.
`{"plate:col1": 100, "plate:A12": 40}`. The expected volumes come from your plan, so a wrong call
shows as a mismatch. On real hardware it compares what was sent, not what is in the wells: the
person checks the plate (`liquid_present`).
