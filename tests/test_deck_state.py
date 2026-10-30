"""The deck carried from one run to the next: .ahc/state/deck.json after every call that changes the deck, the layout
keys tips_empty / tips_returned for tip spots that are not fresh, and last_deck, the saved state offered as a layout.
The STARlet runs in-process; the OT-2 paths run on the fake robot (tests/fake_robot.py) or build the deck alone."""

import copy
import json

import pytest
from pylabrobot.resources import OTDeck

from ahc.core.deck_state import last_deck
from ahc.devices.hamilton.brand import HamiltonAdapter
from ahc.devices.opentrons.brand import OpentronsAdapter, ensure_tip_rack_definitions
from ahc.devices.opentrons.ot2 import OT2Adapter
from ahc.devices.spec import load_device
from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT
from conftest import give_reference, load_verified, robot_lab

pytestmark = pytest.mark.anyio

DEV = "ot2.right"

# The first real OT-2 run's end state: plate row D at 100 uL (D12 at 200), the rack in slot 10 with B1-H1 and A2-F2
# gone, A1 picked up and returned, the troughs at 69.8 and 88.9 mL.
REAL_END_STATE = {
  "slots": {
    "10": {"name": "tips", "type": "opentrons_96_filtertiprack_200ul"},
    "1": {"name": "plate", "type": "Cor_96_wellplate_360ul_Fb"},
    "2": {"name": "red", "type": "nest_1_troughplate_195000uL_Vb"},
    "3": {"name": "water", "type": "nest_1_troughplate_195000uL_Vb"},
  },
  "liquids": {"plate:D1:D11": 100, "plate:D12": 200, "red:A1": 69800, "water:A1": 88900},
  "tips_empty": ["tips:B1:H1", "tips:A2:F2"],
  "tips_returned": ["tips:A1"],
}


def deck_json(lab) -> dict:
  return json.loads(lab.server.lab.ws.deck_path.read_text())


def used(layout: dict, **keys) -> dict:
  return {**copy.deepcopy(layout), **keys}


def ot2_deck(layout: dict):
  ensure_tip_rack_definitions({item["type"] for item in layout["slots"].values()})
  return OT2Adapter(load_device("opentrons.ot2"), "sim", {})._build_deck(OTDeck(), layout)


# -- .ahc/state/deck.json ----------------------------------------------------------------------------------

async def test_deck_json_follows_every_call_that_changes_the_deck(starlet):
  assert not starlet.server.lab.ws.deck_path.exists()
  await load_verified(starlet, layout=STARLET_LAYOUT)
  lab = starlet.server.lab
  deck = deck_json(starlet)
  assert deck["run_id"] == lab._run_id and deck["at"]
  assert deck["device"] == {"model": "hamilton.starlet", "id": "starlet", "backend": "sim"}
  assert deck["layout"] == STARLET_LAYOUT  # the layout as loaded
  assert deck["liquids_ul"] == {"diluent_trough": 100000.0, "stock_trough": 20000.0, "waste_trough": 0.0}
  assert deck["tips"] == {} and deck["tips_mounted"] == {"starlet.pip": 0} and "unknown" not in deck

  await starlet.call("pick_up_tips", device="starlet.pip")
  deck = deck_json(starlet)  # picked and not returned: empty
  assert deck["tips"] == {"tips": {f"{row}1": "empty" for row in "ABCDEFGH"}}
  assert deck["tips_mounted"] == {"starlet.pip": 8}
  await starlet.call("aspirate", device="starlet.pip", targets=["diluent"], volumes=100)
  assert deck_json(starlet)["liquids_ul"]["diluent_trough"] == 99200.0
  await starlet.call("dispense", device="starlet.pip", targets=["plate:A1:H1"], volumes=100)
  assert deck_json(starlet)["liquids_ul"]["plate"] == {f"{row}1": 100.0 for row in "ABCDEFGH"}
  await starlet.call("mix", device="starlet.pip", targets=["plate:A1:H1"], volume=50, repetitions=2)
  assert deck_json(starlet)["liquids_ul"]["plate"] == {f"{row}1": 100.0 for row in "ABCDEFGH"}
  await starlet.call("drop_tips", device="starlet.pip", mode="return")
  deck = deck_json(starlet)
  assert deck["tips"] == {"tips": {f"{row}1": "returned" for row in "ABCDEFGH"}}
  assert deck["tips_mounted"] == {"starlet.pip": 0}
  state = await starlet.call("get_state")  # the same numbers get_state reports
  assert state["liquids_ul"]["plate"] == deck["liquids_ul"]["plate"] and state["tips_returned"] == {"tips": 8}
  assert not list(lab.ws.deck_path.parent.glob(".*.tmp"))  # written atomically


async def test_a_refused_call_leaves_the_deck_state_as_it_was(starlet):
  await load_verified(starlet, layout=STARLET_LAYOUT)
  await starlet.call("pick_up_tips", device="starlet.pip")
  before = starlet.server.lab.ws.deck_path.read_text()
  assert await starlet.refused("aspirate", device="starlet.pip", targets=["plate:A1:H1"], volumes=10) == "insufficient_liquid"
  assert starlet.server.lab.ws.deck_path.read_text() == before


async def test_a_call_with_an_unknown_outcome_writes_the_wells_and_tips_as_unknown(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device=DEV, tips=["tips:col1"])
    fake_robot.fail_commands["aspirateInPlace"] = "pressure fault"
    assert await lab.refused("aspirate", device=DEV, targets=["diluent"], volumes=100) == "completion_unknown"
    deck = deck_json(lab)
    assert deck["tips_mounted"] == {"ot2.right": "unknown", "ot2.left": "unknown"}  # never "0" as if known
    assert deck["unknown"]["wells"] == ["reagents:A1:H1"] and deck["unknown"]["racks"] == []
    assert "aspirate on the robot is unknown" in deck["unknown"]["why"]
    assert "reagents" in deck["liquids_ul"] and "A1" not in deck["liquids_ul"]["reagents"]
    assert deck["liquids_ul"]["reagents"]["A2"] == 500.0
    last = (await lab.call("find_layout_references"))["last_deck"]  # what is unknown is left out of the layout
    assert last["unknown"] == ["reagents:A1:H1"] and last["layout"]["liquids"] == {"reagents:A2:H2": 500.0}
    assert "reagents:A1:H1" in last["note"] and "person gives those volumes" in last["note"]


async def test_a_pick_up_with_an_unknown_outcome_leaves_the_rack_unknown_and_out_of_the_last_deck(tmp_path, open_lab,
                                                                                                    fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    fake_robot.fail_commands["pickUpTip"] = "motor stall"
    assert await lab.refused("pick_up_tips", device=DEV, tips=["tips:col1"]) == "completion_unknown"
    deck = deck_json(lab)
    assert deck["unknown"]["racks"] == ["tips", "tips2"]  # as get_state: after a tip call no rack is known
    assert deck["tips"]["tips"] == {f"{row}1": "unknown" for row in "ABCDEFGH"}  # the spots the call named
    last = (await lab.call("find_layout_references"))["last_deck"]
    assert last["unknown"] == ["tips (tip states)", "tips2 (tip states)"]
    kept = {item["name"] for item in last["layout"]["slots"].values()}  # never a rack taken for full
    assert kept == {"reagents", "plate"}
    assert "tips_empty" not in last["layout"] and "tip states" in last["note"]


# -- tips_empty and tips_returned ------------------------------------------------------------------------------

async def test_the_starlet_skips_empty_spots_and_takes_returned_ones_only_by_name(starlet):
  layout = used(STARLET_LAYOUT, tips_empty=["tips:A1:H1", "tips:A2"], tips_returned=["tips:A3:H3"])
  loaded = await load_verified(starlet, layout=layout)
  assert loaded["tip_states"] == {"tips": {"empty": ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1", "A2"],
                                          "returned": ["A3", "B3", "C3", "D3", "E3", "F3", "G3", "H3"]}}
  assert (await starlet.call("get_state"))["tips_returned"] == {"tips": 8}
  # Column 1 is gone, column 2 lacks A2, column 3 holds returned tips: the next full fresh column is column 4.
  picked = await starlet.call("pick_up_tips", device="starlet.pip")
  assert picked["effective"]["where"] == "tips: column 4 (A4-H4)"
  await starlet.call("drop_tips", device="starlet.pip")
  text = await starlet.call_text("pick_up_tips", device="starlet.pip", tips=["tips:A1:H1"])
  assert "[tip_spot_empty]" in text and "tips: column 1 (A1-H1) holds no tip" in text and "tips_empty" in text
  assert "Name spots that hold a tip" in text.partition("Hint:")[2]
  named = await starlet.call("pick_up_tips", device="starlet.pip", tips=["tips:A3:H3"])  # a returned tip, by name
  assert named["effective"]["where"] == "tips: column 3 (A3-H3)"


async def test_the_ot2_skips_empty_spots_and_takes_returned_ones_only_by_name(tmp_path, open_lab, fake_robot):
  layout = used(OT2_LAYOUT, tips_empty=["tips:col1"], tips_returned=["tips:col2"])
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=layout)
    picked = await lab.call("pick_up_tips", device=DEV)
    assert picked["effective"]["where"] == "tips: column 3 (A3-H3)"
    await lab.call("drop_tips", device=DEV)
    assert await lab.refused("pick_up_tips", device=DEV, tips=["tips:col1"]) == "tip_spot_empty"
    named = await lab.call("pick_up_tips", device=DEV, tips=["tips:col2"])
    assert named["effective"]["where"] == "tips: column 2 (A2-H2)"


@pytest.mark.parametrize("keys, code, text", [
  ({"tips_empty": "tips:A1"}, "bad_layout", "must be a list"),
  ({"tips_empty": ["tips"]}, "bad_tips", "tips_empty: 'tips' must name tip spots"),
  ({"tips_returned": ["plate:A1"]}, "bad_tips", "tips_returned: 'plate:A1' must name tip spots"),
  ({"tips_empty": ["tips:A13"]}, "bad_tips", "tips_empty"),
  ({"tips_empty": ["tips:A1"], "tips_returned": ["tips:col1"]}, "bad_layout", "in both tips_empty and tips_returned"),
])
async def test_bad_tip_state_keys_are_refused_with_a_hint(starlet, keys, code, text):
  refusal = await starlet.call_text("load_layout", layout=used(STARLET_LAYOUT, **keys))
  assert f"[{code}]" in refusal and text in refusal


def test_the_real_end_state_as_a_layout_an_omitted_pick_up_takes_g2():
  deck = ot2_deck(REAL_END_STATE)
  assert [s.get_identifier() for s in deck.next_tips(1)] == ["G2"]  # a single channel: G2, then H2, then column 3
  assert [s.get_identifier() for s in deck.next_tips(8)] == [f"{row}3" for row in "ABCDEFGH"]  # a full column
  assert deck.liquids["plate_well_D1"] == deck.liquids["plate_well_D11"] == 100 and deck.liquids["plate_well_D12"] == 200
  assert deck.liquids["red_well_A1"] == 69800 and deck.liquids["water_well_A1"] == 88900
  rack = deck.labware["tips"]
  assert [s.get_identifier() for s in rack.get_all_items() if not s.has_tip()] == \
         [f"{row}1" for row in "BCDEFGH"] + [f"{row}2" for row in "ABCDEF"]
  assert deck.returned == {"tips_A1"}


def test_a_returned_spot_is_not_next_fresh_and_a_named_empty_one_is_refused_by_the_layout():
  deck = ot2_deck(REAL_END_STATE)
  assert not any(s.name == "tips_A1" for s in deck.next_tips(1))
  assert deck.resolve_tips(["tips:A1"])[0].has_tip()  # named, a returned tip can be picked up again
  assert not deck.resolve_tips(["tips:B1"])[0].has_tip()


# -- the deck check lists them ---------------------------------------------------------------------------------

def test_the_deck_description_lists_empty_and_returned_spots():
  deck = ot2_deck(REAL_END_STATE)
  deck.record_placements()
  text = deck.describe()
  assert "slot 10: tips (opentrons_96_filtertiprack_200ul), empty B1-H1, A2-F2, returned A1" in text
  assert "slot 1: plate (Cor_96_wellplate_360ul_Fb) holding D1-D11 100 uL, D12 200 uL" in text
  assert "red (nest_1_troughplate_195000uL_Vb) holding A1 69800 uL" in text


async def test_ask_person_lists_the_carried_state_on_real_hardware(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await lab.call("load_layout", layout=REAL_END_STATE)
    check = await lab.call("verify", check="deck_matches_layout")
    assert check["verdict"] == "pending"  # on a real deck the person always checks, whatever the folder remembers
    asked = check["ask_person"]
    assert asked.startswith("The deck matches the loaded layout: ")
    assert "slot 10: tips (opentrons_96_filtertiprack_200ul), empty B1-H1, A2-F2, returned A1" in asked
    assert "D1-D11 100 uL, D12 200 uL" in asked and "A1 69800 uL" in asked and "A1 88900 uL" in asked
    assert asked.endswith("and no tips on either pipette.")


async def test_ask_person_lists_them_in_simulation_too(starlet):
  layout = used(STARLET_LAYOUT, tips_empty=["tips:A1:H1"], tips_returned=["tips:A2"])
  await starlet.call("load_layout", layout=layout)
  check = await starlet.call("verify", check="deck_matches_layout")
  assert check["verdict"] == "pending"  # no reference in this folder yet: the person confirms it
  assert "tips (hamilton_96_tiprack_300uL_filter), empty A1-H1, returned A2" in check["ask_person"]


def test_the_placement_comparison_ignores_the_tip_keys():
  ot2 = used(OT2_LAYOUT, tips_empty=["tips:col1"], tips_returned=["tips:A2"])
  star = used(STARLET_LAYOUT, tips_empty=["tips:col1"], tips_returned=["tips:A2"])
  assert OpentronsAdapter.placement(ot2) == OpentronsAdapter.placement(OT2_LAYOUT)
  assert HamiltonAdapter.placement(star) == HamiltonAdapter.placement(STARLET_LAYOUT)


async def test_a_layout_with_the_tip_keys_matches_a_reference_without_them(starlet):
  give_reference(starlet)
  layout = used(STARLET_LAYOUT, tips_empty=["tips:col1"], tips_returned=["tips:A2"])
  loaded = await starlet.call("load_layout", layout=layout)
  assert loaded["matches_reference"] and "needs_person" not in loaded
  assert (await starlet.call("verify", check="deck_matches_layout"))["verdict"] == "pass"  # the simulator judges it


# -- last_deck ---------------------------------------------------------------------------------------------------

async def test_the_last_deck_is_offered_as_a_layout_and_loading_it_reproduces_the_state(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device="starlet.pip")
    await lab.call("aspirate", device="starlet.pip", targets=["diluent"], volumes=100)
    await lab.call("dispense", device="starlet.pip", targets=["plate:A1:H1"], volumes=100)
    await lab.call("drop_tips", device="starlet.pip", mode="return")
    await lab.call("pick_up_tips", device="starlet.pip")  # column 2: picked, not returned
    await lab.call("aspirate", device="starlet.pip", targets=["stock"], volumes=50)
    await lab.call("dispense", device="starlet.pip", targets=["plate:A2:H2"], volumes=50)
    await lab.call("drop_tips", device="starlet.pip")
    ended = await lab.call("get_state")
    run = lab.server.lab._run_id
    overview = await lab.call("lab_overview")
    assert overview["last_deck"]["run_id"] == run and overview["last_deck"]["unknown"] == 0
  async with open_lab(tmp_path, "sim") as lab:  # a new session in the same folder
    assert (await lab.call("get_state"))["layout"] is None  # AHC never loads the old state by itself
    refs = await lab.call("find_layout_references")
    last = refs["last_deck"]
    assert last["run_id"] == run and last["at"] and last["unknown"] == []
    assert "as is or edited" in refs["next"] and "person" in last["note"]
    layout = last["layout"]
    assert layout["carriers"] == STARLET_LAYOUT["carriers"] and layout["aliases"] == STARLET_LAYOUT["aliases"]
    assert layout["liquids"] == {"diluent_trough": 99200.0, "stock_trough": 19600.0, "plate:A1:H1": 100.0,
                                 "plate:A2:H2": 50.0}
    assert layout["tips_empty"] == ["tips:A2:H2"] and layout["tips_returned"] == ["tips:A1:H1"]
    # It counts as a reference (it was run here), so in simulation its placement passes the deck check by itself.
    loaded = await lab.call("load_layout", layout=layout)
    assert loaded["matches_reference"] and "needs_person" not in loaded
    assert (await lab.call("verify", check="deck_matches_layout"))["verdict"] == "pass"
    state = await lab.call("get_state")
    assert state["liquids_ul"] == ended["liquids_ul"]
    assert state["tips_left"] == ended["tips_left"] and state["tips_returned"] == ended["tips_returned"]
    again = await lab.call("pick_up_tips", device="starlet.pip")  # the next fresh column is column 3
    assert again["effective"]["where"] == "tips: column 3 (A3-H3)"


async def test_the_last_deck_as_a_layout_can_be_edited_before_loading(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device="starlet.pip")
    await lab.call("drop_tips", device="starlet.pip")
  async with open_lab(tmp_path, "sim") as lab:
    layout = (await lab.call("find_layout_references"))["last_deck"]["layout"]
    layout["liquids"]["plate:A1:H1"] = 200  # e.g. the person filled a column since
    loaded = await lab.call("load_layout", layout=layout)
    assert loaded["liquids_ul"]["plate_well_A1"] == 200


async def test_the_ot2_end_state_comes_back_as_the_same_layout(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=REAL_END_STATE)
    last = (await lab.call("find_layout_references"))["last_deck"]
  assert last["layout"]["slots"] == REAL_END_STATE["slots"]
  assert last["layout"]["liquids"] == {"plate:D1:D11": 100.0, "plate:D12": 200.0, "red:A1": 69800.0,
                                         "water:A1": 88900.0}
  assert last["layout"]["tips_empty"] == ["tips:B1:H1", "tips:A2:F2"] and last["layout"]["tips_returned"] == ["tips:A1"]
  assert last["unknown"] == []
  again = ot2_deck(last["layout"])
  assert [s.get_identifier() for s in again.next_tips(1)] == ["G2"]


async def test_a_deck_of_another_device_model_is_not_offered(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    path = lab.server.lab.ws.deck_path
  deck = json.loads(path.read_text())
  deck["device"]["model"] = "opentrons.ot2"
  path.write_text(json.dumps(deck))
  async with open_lab(tmp_path, "sim") as lab:
    assert "last_deck" not in await lab.call("find_layout_references")
    assert "last_deck" not in await lab.call("lab_overview")
  path.write_text("{not json")  # unreadable: no last deck, no failure
  async with open_lab(tmp_path, "sim") as lab:
    assert "last_deck" not in await lab.call("find_layout_references")


def test_last_deck_takes_unknown_wells_and_racks_out():
  deck = {"run_id": "r1", "at": "2026-10-29T10:00:00", "layout": {"slots": {
    "1": {"name": "tips", "type": "opentrons_96_filtertiprack_200ul"}, "2": {"name": "plate", "type": "Cor_96_wellplate_360ul_Fb"}},
    "liquids": {"plate:A1": 50}}, "liquids_ul": {"plate": {"B1": 80.0}},
    "tips": {"tips": {"A1": "empty"}}, "unknown": {"why": "stop", "wells": ["plate:A1"], "racks": ["tips"]}}
  out = last_deck(deck)
  assert out["layout"]["slots"] == {"2": {"name": "plate", "type": "Cor_96_wellplate_360ul_Fb"}}
  assert out["layout"]["liquids"] == {"plate:B1": 80.0} and out["unknown"] == ["plate:A1", "tips (tip states)"]
