"""A refusal's "Use at most X" gives an X the same check accepts: bounds are rounded down at the precision shown,
never to the nearest (a 1.19 mm bound shown as 1.2 would be refused again)."""

import copy
import re
import warnings

import pytest
import pylabrobot.resources as plr
from pylabrobot.resources import Container

from ahc.core.errors import LabError, at_most
from ahc.core.layout import Layout
from ahc.devices.spec import load_device
from ahc.examples import STARLET_LAYOUT
from conftest import give_reference, load_verified

pytestmark = pytest.mark.anyio

DEV = "starlet.pip"
SUGGESTED = re.compile(r"Use at most ([0-9.]+) (?:mm|uL)")


def suggested(text: str) -> float:
  return float(SUGGESTED.search(text.partition("Hint:")[2]).group(1))


def test_a_bound_is_rounded_down_at_the_precision_shown():
  assert [at_most(1.19, 1), at_most(10.67, 1), at_most(2.3, 1), at_most(0.04, 1)] == ["1.1", "10.6", "2.3", "0"]
  assert [at_most(355.0, 2), at_most(354.9999995, 2), at_most(1234.5678, 2)] == ["355", "354.99", "1234.56"]
  for value in (0.3, 0.7, 1.46, 10.67, 59.9999995, 2393.04, 1234.5678, 1 / 3):
    for places in (1, 2):
      assert float(at_most(value, places)) <= value


async def ready(starlet, layout=STARLET_LAYOUT):
  give_reference(starlet)
  await load_verified(starlet, layout=layout)
  await starlet.call("pick_up_tips", device=DEV)


async def test_the_height_an_above_liquid_hint_gives_is_accepted(starlet):
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    well = plr.Cor_96_wellplate_360ul_Fb(name="probe").get_item("A1")
  # A volume left whose bound (0.5 mm under the surface, at least 0.1 mm above the floor) a rounding to the nearest
  # tenth would show too high.
  bound = lambda v: well.compute_height_from_volume(v) - 0.5
  left = next(v for v in range(20, 300) if bound(v) >= 0.1 and round(bound(v), 1) > bound(v))
  layout = copy.deepcopy(STARLET_LAYOUT)
  layout["liquids"]["plate:col1"] = left + 50
  await ready(starlet, layout)
  text = await starlet.call_text("aspirate", device=DEV, targets=["plate:col1"], volumes=50, liquid_height=5)
  assert "[above_liquid]" in text and f"{left} uL is left" in text
  height = suggested(text)
  assert height <= bound(left)
  drawn = await starlet.call("aspirate", device=DEV, targets=["plate:col1"], volumes=50, liquid_height=height)
  assert drawn["effective"]["liquid_height_mm"] == height


async def test_the_height_an_exceeds_container_hint_gives_is_accepted(starlet):
  await ready(starlet)
  await starlet.call("aspirate", device=DEV, targets=["diluent"], volumes=50)
  text = await starlet.call_text("dispense", device=DEV, targets=["plate:col1"], volumes=50, liquid_height=30)
  assert "[exceeds_container]" in text and "above the 10.67 mm cavity" in text  # rounded to the nearest: 10.7
  assert suggested(text) == 10.6
  await starlet.call("dispense", device=DEV, targets=["plate:col1"], volumes=50, liquid_height=suggested(text))


async def test_the_volume_an_exceeds_tip_hint_gives_is_accepted(starlet):
  await ready(starlet)  # 300 uL filter tips: 360 uL to the filter
  await starlet.call("aspirate", device=DEV, targets=["diluent"], volumes=100.0000005)
  mix = lambda volume: {"post_mix_volume_ul": volume, "post_mix_repetitions": 1}
  # 0.0000005 uL stay in the tips and the liquid class draws 5 uL of transport air: 354.9999995 uL is the room left.
  text = await starlet.call_text("dispense", device=DEV, targets=["diluent"], volumes=100, specialized=mix(356))
  assert "[exceeds_tip]" in text and suggested(text) == 354.99  # to the nearest it would be 355, refused again
  await starlet.call("dispense", device=DEV, targets=["diluent"], volumes=100, specialized=mix(suggested(text)))


def test_the_volume_an_overfill_hint_gives_is_accepted():
  layout = Layout(load_device("hamilton.starlet"), "tracks")
  layout.add("odd", Container(name="odd", size_x=10, size_y=10, size_z=10, max_volume=1234.5678), "odd")
  with pytest.raises(LabError) as refused:
    layout.set_liquids({"odd": 2000})
  assert refused.value.code == "overfill" and refused.value.hint == "Use at most 1234.56 uL."  # :g showed 1234.57
  layout.set_liquids({"odd": 1234.56})
