"""Example layouts and a serial dilution written once against the capability layer.

The protocol names liquids by alias (`diluent`, `stock`, `waste`); each layout says where they
physically are. Everything device-specific lives in the layout.
"""

import json

STARLET_LAYOUT = {
  "carriers": [
    {"name": "tip_car", "type": "TIP_CAR_480_A00", "track": 1,
     "sites": {"0": {"name": "tips", "type": "hamilton_96_tiprack_300uL_filter"},
               "1": {"name": "tips2", "type": "hamilton_96_tiprack_300uL_filter"}}},
    {"name": "plate_car", "type": "PLT_CAR_L5AC_A00", "track": 7,
     "sites": {"0": {"name": "plate", "type": "Cor_96_wellplate_360ul_Fb"}}},
    {"name": "trough_car", "type": "Trough_CAR_4R200_A00", "track": 13,
     "sites": {"0": {"name": "diluent_trough", "type": "Hamilton_1_trough_200ml_Vb"},
               "1": {"name": "stock_trough", "type": "Hamilton_1_trough_200ml_Vb"},
               "2": {"name": "waste_trough", "type": "Hamilton_1_trough_200ml_Vb"}}},
  ],
  "liquids": {"diluent_trough": 100000, "stock_trough": 20000},
  "aliases": {"diluent": "diluent_trough", "stock": "stock_trough", "waste": "waste_trough"},
}

OT2_LAYOUT = {
  "slots": {
    "1": {"name": "tips", "type": "opentrons_96_tiprack_300ul"},
    "4": {"name": "tips2", "type": "opentrons_96_tiprack_300ul"},
    "2": {"name": "reagents", "type": "cor_96_wellplate_2mL_Vb"},
    "3": {"name": "plate", "type": "Cor_96_wellplate_360ul_Fb"},
  },
  "liquids": {"reagents:A1:H1": 1800, "reagents:A2:H2": 500},
  "aliases": {"diluent": "reagents:A1:H1", "stock": "reagents:A2:H2", "waste": "reagents:A12:H12"},
}


async def serial_dilution(lab, device: str, *, dilution_factor: float = 1.5, final_ul: float = 100.0,
                          n_series: int = 11, native_mix: bool = False) -> list[tuple[str, str]]:
  """Columns 1..n_series hold the series, column 12 the blank; every well ends at final_ul.

  Returns the calls made, without the device, so two devices' runs can be compared.
  """
  transfer = final_ul / (dilution_factor - 1)
  stock = final_ul + transfer
  mix_ul = 0.75 * (final_ul + transfer)
  calls: list[tuple[str, str]] = []

  async def call(op: str, **args):
    calls.append((op, json.dumps(args, sort_keys=True)))
    return await lab.call(op, device=device, **args)

  def col(c: int) -> list[str]:
    return [f"plate:A{c}:H{c}"]

  await call("pick_up_tips")
  for c in range(2, 13):
    await call("aspirate", targets=["diluent"], volumes=final_ul)
    await call("dispense", targets=col(c), volumes=final_ul)
  await call("drop_tips")

  await call("pick_up_tips")
  await call("aspirate", targets=["stock"], volumes=stock)
  await call("dispense", targets=col(1), volumes=stock)
  await call("drop_tips")

  for k in range(1, n_series):
    await call("pick_up_tips")
    await call("aspirate", targets=col(k), volumes=transfer)
    if native_mix:
      await call("dispense", targets=col(k + 1), volumes=transfer,
                 specialized={"post_mix_volume_ul": mix_ul, "post_mix_repetitions": 3})
    else:
      await call("dispense", targets=col(k + 1), volumes=transfer)
      await call("mix", targets=col(k + 1), volume=mix_ul, repetitions=3)
    if k + 1 == n_series:
      await call("aspirate", targets=col(k + 1), volumes=transfer)
      await call("dispense", targets=["waste"], volumes=transfer)
    await call("drop_tips")
  return calls


def plate_volumes(state: dict) -> dict[str, float]:
  return state["liquids_ul"].get("plate", {})
