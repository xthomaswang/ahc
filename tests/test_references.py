"""A layout whose deck differs from this folder's references needs the person, in simulation too. The deck is each
labware type and where it sits: an OT-2 slot; a STARlet carrier's type and track, and the site on it. Labware
names, liquids, volumes and aliases do not count. References are the layouts saved here for the device and the
ones earlier runs used once their deck check passed; a protocol is none. The person is told how the layout differs
from the closest reference (the fewest differences)."""

import copy

import pytest
import yaml

from ahc.core.layout import deck_differences
from ahc.devices.hamilton.brand import HamiltonAdapter
from ahc.devices.opentrons.brand import OpentronsAdapter
from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT
from conftest import give_reference, load_verified, robot_lab

pytestmark = pytest.mark.anyio

DEV = "starlet.pip"
REFERENCE = "saved layout 'reference'"


def starlet_variant(change) -> dict:
  layout = copy.deepcopy(STARLET_LAYOUT)
  change(layout, {c["name"]: c for c in layout["carriers"]})
  return layout


def renamed(layout, carriers):
  """The same deck under other names, with other liquids and aliases."""
  for carrier in layout["carriers"]:
    carrier["name"] = "my_" + carrier["name"]
    for item in carrier["sites"].values():
      item["name"] = "my_" + item["name"]
  layout["liquids"] = {"my_diluent_trough": 50000, "my_plate:col1": 100}
  layout["aliases"] = {"buffer": "my_diluent_trough", "sample": "my_stock_trough"}


def another_plate_type(layout, carriers):
  carriers["plate_car"]["sites"]["0"]["type"] = "cor_96_wellplate_2mL_Vb"


def an_extra_plate(layout, carriers):
  carriers["plate_car"]["sites"]["1"] = {"name": "plate2", "type": "Cor_96_wellplate_360ul_Fb"}


def no_waste_trough(layout, carriers):
  del carriers["trough_car"]["sites"]["2"]
  del layout["aliases"]["waste"]


def the_plate_one_site_on(layout, carriers):
  carriers["plate_car"]["sites"] = {"1": carriers["plate_car"]["sites"]["0"]}


def the_troughs_on_track_19(layout, carriers):
  carriers["trough_car"]["track"] = 19


CHANGES = {
  "a labware type": (another_plate_type,
                     ["track 7 site 0: the reference has Cor_96_wellplate_360ul_Fb, this layout cor_96_wellplate_2mL_Vb"]),
  "an extra labware": (an_extra_plate, ["track 7 site 1: not in the reference (this layout has Cor_96_wellplate_360ul_Fb)"]),
  "a missing labware": (no_waste_trough,
                        ["track 13 site 2: not in this layout (the reference has Hamilton_1_trough_200ml_Vb)"]),
  "the same labware on another site": (the_plate_one_site_on,
                                       ["track 7 site 0: not in this layout (the reference has Cor_96_wellplate_360ul_Fb)",
                                        "track 7 site 1: not in the reference (this layout has Cor_96_wellplate_360ul_Fb)"]),
  "a carrier on another track": (the_troughs_on_track_19,
                                 ["track 13: not in this layout (the reference has Trough_CAR_4R200_A00)",
                                  "track 13 site 0: not in this layout (the reference has Hamilton_1_trough_200ml_Vb)",
                                  "track 13 site 1: not in this layout (the reference has Hamilton_1_trough_200ml_Vb)",
                                  "track 13 site 2: not in this layout (the reference has Hamilton_1_trough_200ml_Vb)",
                                  "track 19: not in the reference (this layout has Trough_CAR_4R200_A00)",
                                  "track 19 site 0: not in the reference (this layout has Hamilton_1_trough_200ml_Vb)",
                                  "track 19 site 1: not in the reference (this layout has Hamilton_1_trough_200ml_Vb)",
                                  "track 19 site 2: not in the reference (this layout has Hamilton_1_trough_200ml_Vb)"]),
}


# -- the deck, position by position ------------------------------------------------------------------

def test_an_ot2_deck_is_compared_slot_by_slot():
  reference = OpentronsAdapter.placement(OT2_LAYOUT)
  assert reference == {"slot 1": "opentrons_96_tiprack_300ul", "slot 4": "opentrons_96_tiprack_300ul",
                       "slot 2": "cor_96_wellplate_2mL_Vb", "slot 3": "Cor_96_wellplate_360ul_Fb"}
  same = copy.deepcopy(OT2_LAYOUT)
  same["slots"] = {int(slot): {**item, "name": f"my_{item['name']}"} for slot, item in same["slots"].items()}
  same["slots"]["01"] = same["slots"].pop(1)  # however the slot is written
  same["liquids"], same["aliases"] = {"my_plate:A1": 5}, {}
  assert deck_differences(OpentronsAdapter.placement(same), reference) == []
  other = copy.deepcopy(OT2_LAYOUT)
  other["slots"]["2"] = {"name": "reagents", "type": "nest_12_reservoir_15ml"}
  other["slots"]["5"] = {"name": "more", "type": "Cor_96_wellplate_360ul_Fb"}
  del other["slots"]["4"]
  other["slots"]["10"] = other["slots"].pop("3")  # the same plate, in another slot
  assert deck_differences(OpentronsAdapter.placement(other), reference) == [
    "slot 2: the reference has cor_96_wellplate_2mL_Vb, this layout nest_12_reservoir_15ml",
    "slot 3: not in this layout (the reference has Cor_96_wellplate_360ul_Fb)",
    "slot 4: not in this layout (the reference has opentrons_96_tiprack_300ul)",
    "slot 5: not in the reference (this layout has Cor_96_wellplate_360ul_Fb)",
    "slot 10: not in the reference (this layout has Cor_96_wellplate_360ul_Fb)"]


def test_a_starlet_deck_is_compared_by_carrier_type_track_and_site():
  reference = HamiltonAdapter.placement(STARLET_LAYOUT)
  assert reference["track 1"] == "TIP_CAR_480_A00" and reference["track 7 site 0"] == "Cor_96_wellplate_360ul_Fb"
  assert deck_differences(HamiltonAdapter.placement(starlet_variant(renamed)), reference) == []
  other = starlet_variant(lambda layout, carriers: carriers["plate_car"].update(type="TIP_CAR_480_A00"))
  assert deck_differences(HamiltonAdapter.placement(other), reference) == [
    "track 7: the reference has PLT_CAR_L5AC_A00, this layout TIP_CAR_480_A00"]
  for change, (make, expected) in CHANGES.items():
    assert deck_differences(HamiltonAdapter.placement(starlet_variant(make)), reference) == expected, change


def test_a_layout_that_is_no_layout_never_breaks_the_comparison():
  for broken in (None, [], {"slots": [1, 2]}, {"slots": {"1": "a tip rack"}}, {"carriers": {"x": 1}},
                 {"carriers": [None, {"track": 3}]}):
    assert isinstance(OpentronsAdapter.placement(broken), dict) and isinstance(HamiltonAdapter.placement(broken), dict)


# -- in simulation ---------------------------------------------------------------------------------------

async def test_the_same_deck_under_other_names_and_liquids_is_the_simulators_to_judge(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    give_reference(lab)
    loaded = await lab.call("load_layout", layout=starlet_variant(renamed))
    assert "needs_person" not in loaded and loaded["matches_reference"] == REFERENCE and "differences" not in loaded
    check = await lab.call("verify", check="deck_matches_layout")
    assert check["verdict"] == "pass" and check["judged_by"] == "simulation"
    assert lab.server.lab.ws.layout_path(loaded["saved_as"]).is_file()  # saved at once, a reference itself


@pytest.mark.parametrize("change", list(CHANGES))
async def test_a_deck_that_differs_from_the_reference_needs_the_person(tmp_path, open_lab, change):
  make, expected = CHANGES[change]
  listed = "; ".join(expected)
  async with open_lab(tmp_path, "sim") as lab:
    give_reference(lab)
    loaded = await lab.call("load_layout", layout=starlet_variant(make))
    assert loaded["needs_person"] and not loaded["saved"] and loaded["gate"]["needs_person"]
    assert loaded["closest_reference"] == REFERENCE and loaded["differences"] == expected
    assert f"It differs from the closest reference in this task folder, {REFERENCE}: {listed}." in loaded["next"]
    text = await lab.call_text("pick_up_tips", device=DEV)  # the gate's refusal says it too
    assert "[gate_pending]" in text and "has not confirmed this layout" in text and listed in text
    check = await lab.call("verify", check="deck_matches_layout")  # and what the person is asked
    assert check["verdict"] == "pending" and check["judged_by"].startswith("the person") and listed in check["next"]
    passed = await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    assert passed["saved_as"] == loaded["saved_as"]  # confirmed: a reference from now on
    again = await lab.call("load_layout", layout=starlet_variant(make))
    assert "needs_person" not in again and again["matches_reference"] == f"saved layout {loaded['saved_as']!r}"
    assert (await lab.call("verify", check="deck_matches_layout"))["judged_by"] == "simulation"


async def test_the_closest_reference_is_the_one_with_the_fewest_differences(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    give_reference(lab)
    lab.server.lab.ws.save_layout("deep-wells", starlet_variant(another_plate_type), "hamilton.starlet")
    both = starlet_variant(lambda layout, carriers: (another_plate_type(layout, carriers),
                                                     an_extra_plate(layout, carriers)))
    loaded = await lab.call("load_layout", layout=both)
  assert loaded["closest_reference"] == "saved layout 'deep-wells'" and loaded["differences"] == CHANGES["an extra labware"][1]


async def test_a_protocol_is_no_reference_a_layout_can_match(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    ws = lab.server.lab.ws
    ws.protocols_dir.mkdir(parents=True)
    (ws.protocols_dir / "dilution.md").write_text("# Serial dilution\nThe example deck, as the person set it up.\n")
    refs = await lab.call("find_layout_references")
    assert [p["name"] for p in refs["protocols"]] == ["dilution.md"]
    assert "No layout here to match it against yet, so the person confirms the first one" in refs["next"]
    loaded = await lab.call("load_layout", layout=STARLET_LAYOUT)
    assert loaded["needs_person"] and "closest_reference" not in loaded
    assert loaded["next"].startswith("No layout saved or run in this task folder to compare it with.")
    assert not ws.has_reference("hamilton.starlet")


async def test_a_saved_layout_loaded_by_name_never_needs_the_person(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    ws = lab.server.lab.ws
    ws.ensure()
    ws.layout_path("by-hand").write_text(yaml.safe_dump(starlet_variant(another_plate_type)))  # no AHC header
    assert not ws.has_reference("hamilton.starlet")  # so no reference for any device
    loaded = await lab.call("load_layout", name="by-hand")
    assert "needs_person" not in loaded and "differences" not in loaded
    assert (await lab.call("verify", check="deck_matches_layout"))["judged_by"] == "simulation"


async def test_a_layout_an_earlier_run_used_is_a_reference(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    first = await load_verified(lab, layout=STARLET_LAYOUT)  # the person confirmed it
    lab.server.lab.ws.layout_path(first["saved_as"]).unlink()  # the file goes; the run log keeps it
  async with open_lab(tmp_path, "sim") as lab:
    loaded = await lab.call("load_layout", layout=starlet_variant(renamed))
  assert "needs_person" not in loaded and loaded["matches_reference"].startswith("layout 'layout-1' of run ")


async def test_an_ot2_layout_in_simulation_is_matched_slot_by_slot(ot2):
  give_reference(ot2, OT2_LAYOUT, "opentrons.ot2")
  same = copy.deepcopy(OT2_LAYOUT)
  same["liquids"] = {"reagents:A1:H1": 1000}
  assert "needs_person" not in await ot2.call("load_layout", layout=same)
  moved = copy.deepcopy(OT2_LAYOUT)
  moved["slots"]["6"] = moved["slots"].pop("3")
  loaded = await ot2.call("load_layout", layout=moved)
  assert loaded["needs_person"] and loaded["differences"] == [
    "slot 3: not in this layout (the reference has Cor_96_wellplate_360ul_Fb)",
    "slot 6: not in the reference (this layout has Cor_96_wellplate_360ul_Fb)"]


# -- on real hardware ------------------------------------------------------------------------------------

async def test_on_real_hardware_the_deck_check_lists_what_differs_too(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    give_reference(lab, OT2_LAYOUT, "opentrons.ot2")
    changed = copy.deepcopy(OT2_LAYOUT)
    changed["slots"]["2"] = {"name": "reagents", "type": "Cor_96_wellplate_360ul_Fb"}
    changed["liquids"] = {"reagents:A1:H1": 300, "reagents:A2:H2": 300}
    loaded = await lab.call("load_layout", layout=changed)
    assert loaded["saved"] is False and "needs_person" not in loaded  # the person checks every deck here anyway
    listed = "slot 2: the reference has cor_96_wellplate_2mL_Vb, this layout Cor_96_wellplate_360ul_Fb"
    assert loaded["differences"] == [listed]
    check = await lab.call("verify", check="deck_matches_layout")
    assert check["verdict"] == "pending" and listed in check["next"] and "no tips on either pipette" in check["next"]
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    loaded = await lab.call("load_layout", layout=OT2_LAYOUT)  # the reference itself: nothing to list
    assert loaded["matches_reference"] == REFERENCE
    check = await lab.call("verify", check="deck_matches_layout")
    assert check["verdict"] == "pending" and "differs" not in check["next"]
