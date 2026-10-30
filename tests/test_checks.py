"""Checks run with verify: volumes_match, and what the person is asked on real hardware."""

import pytest

from ahc.devices.base import Adapter
from ahc.examples import STARLET_LAYOUT
from ahc.verification.checks import TWIN_JUDGE
from conftest import give_reference

pytestmark = pytest.mark.anyio

DEV = "starlet.pip"


def as_on_real_hardware(monkeypatch):
  monkeypatch.setattr(Adapter, "simulated", property(lambda self: False))


async def fill_column_2(lab):
  """100 uL of diluent into each well of plate column 2 (8 channels)."""
  give_reference(lab)
  await lab.call("load_layout", layout=STARLET_LAYOUT)
  await lab.call("verify", check="deck_matches_layout")
  await lab.call("pick_up_tips", device=DEV)
  await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  await lab.call("dispense", device=DEV, targets=["plate:A2:H2"], volumes=100)


async def test_volumes_match_compares_each_container_with_what_was_sent(starlet):
  await fill_column_2(starlet)
  ok = await starlet.call("verify", check="volumes_match",
                          expected={"plate:A2:H2": 100, "diluent": 100000 - 800, "plate:A3": 0})
  assert ok["verdict"] == "pass" and ok["judged_by"] == TWIN_JUDGE and "mismatches" not in ok
  rows = ok["evidence"]["digital_twin"]["containers"]
  assert len(rows) == 10 and rows["plate_well_H2"] == {"expected": 100, "twin": 100, "matches": True}
  off = await starlet.call("verify", check="volumes_match", expected={"plate:A2:B2": 99, "plate:A3": 0})
  assert off["verdict"] == "fail" and off["mismatches"] == [
    {"container": "plate_well_A2", "expected": 99, "twin": 100}, {"container": "plate_well_B2", "expected": 99, "twin": 100}]
  within = await starlet.call("verify", check="volumes_match", expected={"plate:A2:B2": 99}, tolerance_ul=1)
  assert within["verdict"] == "pass"
  logged = [(e["verdict"], e["judged_by"]) for e in starlet.server.lab.runlog.entries
            if e.get("type") == "verification" and e["check"] == "volumes_match"]
  assert logged == [("pass", TWIN_JUDGE), ("fail", TWIN_JUDGE), ("pass", TWIN_JUDGE)]


@pytest.mark.parametrize("args,code", [
  ({}, "expected_required"),
  ({"expected": {}}, "expected_required"),
  ({"expected": {"diluent": -1}}, "bad_expected"),
  ({"expected": {"plate:A1:H1": 10, "plate:B1": 20}}, "bad_expected"),  # B1 twice, with two volumes
  ({"expected": {"diluent": 10}, "tolerance_ul": -0.1}, "bad_tolerance"),
  ({"expected": {"nowhere": 10}}, "unknown_target"),
])
async def test_volumes_match_says_what_is_wrong_with_its_input(starlet, args, code):
  assert await starlet.refused("verify", check="volumes_match", expected={"diluent": 1}) == "no_layout"
  await starlet.call("load_layout", layout=STARLET_LAYOUT)  # the deck check does not matter: nothing moves
  assert await starlet.refused("verify", check="volumes_match", **args) == code


async def test_on_real_hardware_volumes_are_still_the_twins_answer(starlet, monkeypatch):
  await fill_column_2(starlet)
  as_on_real_hardware(monkeypatch)
  result = await starlet.call("verify", check="volumes_match", expected={"plate:A2:H2": 100})
  assert result["verdict"] == "pass" and result["judged_by"] == TWIN_JUDGE and not result["simulated"]
  assert "not a measurement" in result["note"] and not starlet.server.lab.verification.pending


async def test_on_real_hardware_the_person_is_asked_about_the_check_at_hand(starlet, monkeypatch):
  as_on_real_hardware(monkeypatch)
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  deck = await starlet.call("verify", check="deck_matches_layout")
  assert "every slot's labware as listed" in deck["next"] and "no tips on either pipette" in deck["next"]
  tips = await starlet.call("verify", check="tips_mounted", device=DEV, expect="none")
  assert tips["next"].startswith("The person on site checks starlet.pip: no tip on any channel.")
  liquid = await starlet.call("verify", check="liquid_present", targets=["diluent"], min_volume_ul=50)
  assert liquid["next"].startswith("The person on site checks that each container of diluent holds at least 50 uL.")
  assert all("never decide it yourself" in c["next"] for c in (deck, tips, liquid))
  answered = await starlet.call("record_verdict", check_id=liquid["check_id"], verdict="fail")
  assert answered["next"].startswith("The person found liquid_present not as expected: stop, ask them")
  answered = await starlet.call("record_verdict", check_id=tips["check_id"], verdict="pass")
  assert "no tip on any channel" not in answered["next"]  # what comes next, not what the person was asked
