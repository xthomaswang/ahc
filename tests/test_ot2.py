import copy

import pytest
from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT, plate_volumes, serial_dilution
from conftest import load_verified

pytestmark = pytest.mark.anyio

DEV = "ot2.right"


async def test_same_protocol_calls_as_the_starlet(ot2, starlet):
  await load_verified(ot2, layout=OT2_LAYOUT)
  ot2_calls = await serial_dilution(ot2, DEV)
  await load_verified(starlet, layout=STARLET_LAYOUT)
  star_calls = await serial_dilution(starlet, "starlet.pip")
  # The capability calls are identical; everything device-specific lives in the two layouts.
  assert ot2_calls == star_calls

  plate = plate_volumes(await ot2.call("get_state", device=DEV))
  assert len(plate) == 96 and all(v == pytest.approx(100.0) for v in plate.values())
  run = await ot2.call("get_run")
  assert run["basic_only"] == run["liquid_handling_calls"] == 80
  assert run["translated"]["calls"] == 0 and run["with_specialized"]["calls"] == 0
  assert run["refused"] == {} and run["layout_kind"] == "slots"
  assert run["commands_sent"] > 0  # counted by the robot-server, not by this client


async def test_eight_channel_constraints(ot2):
  await load_verified(ot2, layout=OT2_LAYOUT)
  await ot2.call("pick_up_tips", device=DEV)
  assert await ot2.refused("aspirate", device=DEV, targets=["diluent"],
                           volumes=[100] * 7 + [50]) == "equal_volumes_required"
  assert await ot2.refused("aspirate", device=DEV, targets=["reagents:A1"], volumes=100) == "distinct_wells_required"
  assert await ot2.refused("aspirate", device=DEV, targets=["diluent"], volumes=10) == "out_of_range"
  assert await ot2.refused("aspirate", device=DEV, targets=["diluent"], volumes=100,
                           specialized={"jet": True}) == "unknown_param"
  params = await ot2.call("get_params", device=DEV, op="aspirate")
  assert params["specialized"] == {} and "every channel moves the same volume" in params["constraints"]


async def test_layout_refusals(ot2):
  bad = copy.deepcopy(OT2_LAYOUT)
  bad["slots"]["5"] = {"name": "trough", "type": "Hamilton_1_trough_200ml_Vb"}
  assert await ot2.refused("load_layout", layout=bad) == "labware_not_allowed"
  bad = copy.deepcopy(OT2_LAYOUT)
  bad["slots"]["12"] = bad["slots"].pop("4")
  assert await ot2.refused("load_layout", layout=bad) == "bad_slot"


async def test_no_tip_sensor_means_a_camera_is_needed(ot2):
  await load_verified(ot2, layout=OT2_LAYOUT)
  await ot2.call("pick_up_tips", device=DEV)
  verdict = await ot2.call("verify", check="tips_mounted", device=DEV)
  assert verdict["evidence"]["sensors"] is None and "camera" in verdict["note"]
