"""A pick-up that names no tips takes the next fresh ones from a rack whose tips that pipette takes, in layout order,
by PyLabRobot's own rule: on the OT-2 the P20 (left) takes 10 and 20 uL tips, the P300 8-channel head (right) 200 and
300 uL ones. The built-in example layout carries a rack for each. On the OT-2 simulator (AHC_TEST_OT2_PORT) and on
the robot backend against tests/fake_robot.py."""

import copy

import pytest

from ahc.examples import OT2_LAYOUT
from conftest import load_verified, robot_lab

pytestmark = pytest.mark.anyio

HEAD, P20 = "ot2.right", "ot2.left"
ORDERS = ["20 uL rack first", "20 uL rack last"]


def with_a_20ul_rack(first: bool) -> dict:
  layout = copy.deepcopy(OT2_LAYOUT)
  rack = {"5": {"name": "tips20", "type": "opentrons_96_tiprack_20ul"}}
  layout["slots"] = {**rack, **layout["slots"]} if first else {**layout["slots"], **rack}
  return layout


async def each_takes_its_own(lab, layout):
  await load_verified(lab, layout=layout)
  small = await lab.call("pick_up_tips", device=P20)
  head = await lab.call("pick_up_tips", device=HEAD)
  assert small["effective"]["where"] == "tips20: A1" and head["effective"]["where"] == "tips: column 1 (A1-H1)"
  await lab.call("drop_tips", device=P20)
  await lab.call("drop_tips", device=HEAD)


async def no_tips_for_the_p20(lab):
  await load_verified(lab, layout=OT2_LAYOUT)  # 300 uL racks only
  text = await lab.call_text("pick_up_tips", device=P20)
  assert "[out_of_tips]" in text and "no fresh 10 or 20 uL tips for 1 channel(s) in the layout" in text
  assert "Add a rack of 10 or 20 uL tips" in text.partition("Hint:")[2]


# -- on the OT-2 simulator -----------------------------------------------------------------------------

@pytest.mark.parametrize("order", ORDERS)
async def test_each_pipette_takes_its_own_tips_on_the_simulator(ot2, order):
  await each_takes_its_own(ot2, with_a_20ul_rack(order == ORDERS[0]))


async def test_out_of_tips_names_the_tips_the_pipette_takes_on_the_simulator(ot2):
  await no_tips_for_the_p20(ot2)


async def test_the_built_in_example_serves_both_pipettes_on_the_simulator(ot2):
  example = (await ot2.call("lab_overview"))["layout_format"]["example"]
  assert example["slots"]["4"] == {"name": "tips20", "type": "opentrons_96_tiprack_20ul"}
  await each_takes_its_own(ot2, example)


# -- on the robot backend ---------------------------------------------------------------------------------

@pytest.mark.parametrize("order", ORDERS)
async def test_each_pipette_takes_its_own_tips_on_the_robot_backend(tmp_path, open_lab, fake_robot, order):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await each_takes_its_own(lab, with_a_20ul_rack(order == ORDERS[0]))


async def test_out_of_tips_names_the_tips_the_pipette_takes_on_the_robot_backend(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await no_tips_for_the_p20(lab)


async def test_the_built_in_example_serves_both_pipettes_on_the_robot_backend(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await each_takes_its_own(lab, (await lab.call("lab_overview"))["layout_format"]["example"])


async def test_the_starlets_built_in_example_loads_and_serves_its_pipette(starlet):
  example = (await starlet.call("lab_overview"))["layout_format"]["example"]
  await load_verified(starlet, layout=example)
  assert (await starlet.call("pick_up_tips", device="starlet.pip"))["effective"]["where"] == "tips: column 1 (A1-H1)"
  await starlet.call("drop_tips", device="starlet.pip")
