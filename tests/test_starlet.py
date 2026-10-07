import copy

import pytest
from ahc.examples import STARLET_LAYOUT, plate_volumes, serial_dilution
from conftest import load_verified

pytestmark = pytest.mark.anyio

DEV = "starlet.pip"


async def loaded(lab, with_tips: bool = False):
  await load_verified(lab, layout=STARLET_LAYOUT)
  if with_tips:
    await lab.call("pick_up_tips", device=DEV)


# -- end to end ---------------------------------------------------------------------------------

async def test_serial_dilution_with_basic_parameters(starlet):
  await loaded(starlet)
  await serial_dilution(starlet, DEV)
  plate = plate_volumes(await starlet.call("get_state", device=DEV))
  assert len(plate) == 96 and all(v == pytest.approx(100.0) for v in plate.values())
  run = await starlet.call("get_run")
  # STAR channels have no standalone mix: each separate mix was translated and logged as such.
  assert run["translated"] == {"calls": 10, "ops": {"mix": 10}}
  assert run["with_specialized"]["calls"] == 0
  assert run["refused"] == {}
  assert run["basic_only"] == run["liquid_handling_calls"] - 10
  assert run["layout_kind"] == "tracks"


async def test_serial_dilution_with_native_star_mixing(starlet):
  await loaded(starlet)
  await serial_dilution(starlet, DEV, native_mix=True)
  plate = plate_volumes(await starlet.call("get_state", device=DEV))
  assert all(v == pytest.approx(100.0) for v in plate.values())
  run = await starlet.call("get_run")
  assert run["translated"]["calls"] == 0
  assert run["with_specialized"] == {"calls": 10, "params": {"post_mix_volume_ul": 10, "post_mix_repetitions": 10}}


async def test_native_mix_sends_fewer_commands_than_translated_mix(starlet):
  await loaded(starlet, with_tips=True)
  await starlet.call("aspirate", device=DEV, targets=["diluent"], volumes=200)
  native = await starlet.call("dispense", device=DEV, targets=["plate:A1:H1"], volumes=200,
                              specialized={"post_mix_volume_ul": 150, "post_mix_repetitions": 3})
  await starlet.call("aspirate", device=DEV, targets=["diluent"], volumes=200)
  plain = await starlet.call("dispense", device=DEV, targets=["plate:A2:H2"], volumes=200)
  translated = await starlet.call("mix", device=DEV, targets=["plate:A2:H2"], volume=150, repetitions=3)
  assert translated["translated"] and "no standalone mix" in translated["note"]
  assert native["commands_sent"] < plain["commands_sent"] + translated["commands_sent"]


# -- every assert can refuse --------------------------------------------------------------------

REFUSALS = [
  ("out_of_range", "aspirate", dict(targets=["diluent"], volumes=2000)),
  ("exceeds_tip", "aspirate", dict(targets=["diluent"], volumes=500)),
  ("out_of_range", "aspirate", dict(targets=["diluent"], volumes=100, flow_rate=900)),
  ("exceeds_container", "dispense", dict(targets=["plate:A1:H1"], volumes=10, liquid_height=30)),
  ("unknown_param", "aspirate", dict(targets=["diluent"], volumes=50, specialized={"lld": "on"})),
  ("out_of_range", "aspirate", dict(targets=["diluent"], volumes=50, specialized={"settling_time_s": 50})),
  ("bad_type", "aspirate", dict(targets=["diluent"], volumes=50, specialized={"jet": "yes"})),
  ("insufficient_liquid", "aspirate", dict(targets=["plate:A1:H1"], volumes=50)),
  ("target_count", "aspirate", dict(targets=["plate:A1:D1"], volumes=50)),
  ("volume_count", "aspirate", dict(targets=["diluent"], volumes=[50, 50])),
  ("unknown_target", "aspirate", dict(targets=["buffer"], volumes=50)),
  ("wells_required", "aspirate", dict(targets=["plate"], volumes=50)),
]


@pytest.mark.parametrize("code,tool,args", REFUSALS, ids=[f"{c}-{t}" for c, t, _ in REFUSALS])
async def test_refusals_with_tips_mounted(starlet, code, tool, args):
  await loaded(starlet, with_tips=True)
  assert await starlet.refused(tool, device=DEV, **args) == code


async def test_paired_specialized_parameters(starlet):
  await loaded(starlet, with_tips=True)
  await starlet.call("aspirate", device=DEV, targets=["diluent"], volumes=50)
  assert await starlet.refused("dispense", device=DEV, targets=["plate:A1:H1"], volumes=50,
                               specialized={"post_mix_volume_ul": 40}) == "paired_params"


async def test_state_refusals(starlet):
  assert await starlet.refused("aspirate", device=DEV, targets=["diluent"], volumes=10) == "no_layout"
  await loaded(starlet)
  assert await starlet.refused("aspirate", device=DEV, targets=["diluent"], volumes=10) == "no_tips"
  await starlet.call("pick_up_tips", device=DEV)
  assert await starlet.refused("pick_up_tips", device=DEV) == "tips_already_mounted"
  assert await starlet.refused("load_layout", layout=STARLET_LAYOUT) == "tips_mounted"
  await starlet.call("drop_tips", device=DEV)
  assert await starlet.refused("drop_tips", device=DEV) == "no_tips"


async def test_component_and_layout_refusals(starlet):
  assert await starlet.refused("pick_up_tips", device="starlet.head96") == "not_supported"
  assert await starlet.refused("pick_up_tips", device="starlet.arm") == "unknown_component"
  bad = copy.deepcopy(STARLET_LAYOUT)
  bad["carriers"][0]["track"] = 99
  assert await starlet.refused("load_layout", layout=bad) == "bad_track"
  bad = copy.deepcopy(STARLET_LAYOUT)
  bad["carriers"][1]["sites"]["0"]["type"] = "opentrons_96_tiprack_300ul"
  assert await starlet.refused("load_layout", layout=bad) == "labware_not_allowed"
  bad = copy.deepcopy(STARLET_LAYOUT)
  bad["liquids"] = {"diluent_trough": 10**9}
  assert await starlet.refused("load_layout", layout=bad) == "overfill"


async def test_refusals_are_logged(starlet):
  await loaded(starlet, with_tips=True)
  await starlet.refused("aspirate", device=DEV, targets=["diluent"], volumes=2000)
  run = await starlet.call("get_run")
  assert run["refused"] == {"out_of_range": 1}


# -- verification -------------------------------------------------------------------------------

async def test_tip_check_passes_and_can_fail(starlet, monkeypatch):
  await loaded(starlet, with_tips=True)
  verdict = await starlet.call("verify", check="tips_mounted", device=DEV)
  assert verdict["verdict"] == "pass" and verdict["simulated"]
  assert verdict["evidence"]["sensors"]["agrees_with_twin"]

  async def sensor_sees_no_tips(comp, check):
    return {"tip_presence_per_channel": [0] * 8}

  monkeypatch.setattr(starlet.server.lab.adapter, "sense", sensor_sees_no_tips)
  verdict = await starlet.call("verify", check="tips_mounted", device=DEV)
  assert verdict["verdict"] == "fail"


async def test_liquid_check_and_layout_check(starlet):
  await loaded(starlet)
  empty = await starlet.call("verify", check="liquid_present", targets=["plate:A1:H1"])
  assert empty["verdict"] == "fail"
  full = await starlet.call("verify", check="liquid_present", targets=["diluent"], min_volume_ul=1000)
  assert full["verdict"] == "pass"
  deck = await starlet.call("verify", check="deck_matches_layout")
  assert deck["verdict"] == "pass" and "camera" in deck["note"]
  assert await starlet.refused("record_verdict", check_id="x", verdict="pass") == "unknown_check_id"


# -- discovery ----------------------------------------------------------------------------------

async def test_limits_come_with_the_parameters(starlet):
  params = await starlet.call("get_params", device=DEV, op="dispense")
  assert params["basic"]["volumes"]["limits"]["max"] == 1000
  assert params["specialized"]["post_mix_repetitions"]["max"] == 99
  assert "source" in params["specialized"]["settling_time_s"]
  aspirate = await starlet.call("get_params", device=DEV, op="mix")
  assert "no standalone mix" in aspirate["translation"]


async def test_alias_rules(starlet):
  layout = copy.deepcopy(STARLET_LAYOUT)
  layout["aliases"]["diluent_trough"] = "diluent_trough"  # an alias naming its own labware is fine
  await starlet.call("load_layout", layout=layout)
  layout["aliases"] = {"plate": "diluent_trough"}  # one name meaning two things is not
  assert await starlet.refused("load_layout", layout=layout) == "alias_clash"


async def test_one_aspiration_split_over_dispenses_is_explained(starlet):
  # As an agent did: 99.9 uL drawn, then three 33.3 uL dispenses; the third has no piston travel left.
  await loaded(starlet, with_tips=True)
  await starlet.call("aspirate", device=DEV, targets=["diluent"], volumes=99.9, flow_rate=100)
  await starlet.call("dispense", device=DEV, targets=["plate:A2:H2"], volumes=33.3, flow_rate=100)
  await starlet.call("dispense", device=DEV, targets=["plate:A3:H3"], volumes=33.3, flow_rate=100)
  text = await starlet.call_text("dispense", device=DEV, targets=["plate:A4:H4"], volumes=33.3, flow_rate=100)
  assert "[piston_travel]" in text and "aspirate about" in text


async def test_mix_is_one_command_that_cannot_stop_half_way(starlet):
  # An agent's run: 1000 uL tips, 300 uL in the well, then mix 3 x 150 uL. As aspirate/dispense
  # cycles this was refused by 0.1 uL of piston travel with 150 uL left in the tips.
  layout = copy.deepcopy(STARLET_LAYOUT)
  for site in layout["carriers"][0]["sites"].values():
    site["type"] = "hamilton_96_tiprack_1000uL_filter"
  await load_verified(starlet, layout=layout)
  await starlet.call("pick_up_tips", device=DEV)
  await starlet.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  await starlet.call("dispense", device=DEV, targets=["plate:A2:H2"], volumes=100)
  await starlet.call("drop_tips", device=DEV)
  await starlet.call("pick_up_tips", device=DEV)
  await starlet.call("aspirate", device=DEV, targets=["stock"], volumes=200)
  await starlet.call("dispense", device=DEV, targets=["plate:A2:H2"], volumes=200)
  mixed = await starlet.call("mix", device=DEV, targets=["plate:A2:H2"], volume=150, repetitions=3)
  assert mixed["translated"] and "one firmware command" in mixed["note"]
  plate = plate_volumes(await starlet.call("get_state", device=DEV))
  assert plate["A2"] == pytest.approx(300.0)


async def test_a_refusal_after_liquid_moved_says_so(starlet, monkeypatch):
  from pathlib import Path
  from ahc.viz.dashboard import replay
  await loaded(starlet, with_tips=True)
  await starlet.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  adapter = starlet.server.lab.adapter
  original = adapter.dispense

  async def half_then_refused(comp, containers, volumes, *args):
    await original(comp, containers[:4], volumes[:4], *args)  # four channels went, then the device stopped
    raise ValueError("simulated stop after the first group")

  monkeypatch.setattr(adapter, "dispense", half_then_refused)
  text = await starlet.call_text("dispense", device=DEV, targets=["plate:A1:H1"], volumes=100)
  assert "[device_refused]" in text and "Partly done before the refusal" in text and "plate_well_A1 +100" in text
  entry = [e for e in starlet.server.lab.runlog.entries if e.get("status") == "refused"][-1]
  assert entry["partial"] == {f"plate_well_{r}1": 100.0 for r in "ABCD"}
  twin = plate_volumes(await starlet.call("get_state", device=DEV))
  wells = replay(Path(starlet.server.lab.runlog.path))["plates"][0]["wells"]
  assert {w: d["v"] for w, d in wells.items() if d["v"] > 0} == twin  # the dashboard agrees with the device
