"""Layouts: the agent writes them from this workspace's references and they are saved in .ahc/layouts/."""

import copy

import pytest
import yaml

from ahc.core.errors import LabError
from ahc.examples import STARLET_LAYOUT
from conftest import load_verified

pytestmark = pytest.mark.anyio


async def test_a_fresh_folder_has_no_references_and_stays_untouched(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    refs = await lab.call("find_layout_references")
    assert refs["protocols"] == refs["layouts"] == refs["run_layouts"] == []
    assert "layout_format.example" in refs["next"]  # simulation: no physical deck to ask about
  assert list(tmp_path.iterdir()) == []


async def test_without_simulation_no_references_means_asking_the_person(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    refs = await lab.call("find_layout_references")
    assert "ask the person on site" in refs["next"]


async def test_a_loaded_layout_is_saved_and_can_be_loaded_by_name(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    first = await load_verified(lab, layout=STARLET_LAYOUT)  # no reference yet: saved once the person confirms it
    assert first["saved_as"] == "layout-1"
    saved = yaml.safe_load((tmp_path / ".ahc" / "layouts" / "layout-1.yaml").read_text())
    assert saved == STARLET_LAYOUT
    named = await lab.call("load_layout", layout=STARLET_LAYOUT, name="dilution-deck")  # saved at once now
    assert named["saved_as"] == "dilution-deck" and "needs_person" not in named
    again = await lab.call("load_layout", name="dilution-deck")
    assert again["labware"] == first["labware"]
    other = copy.deepcopy(STARLET_LAYOUT)
    other["liquids"]["stock_trough"] = 5000
    assert await lab.refused("load_layout", layout=other, name="dilution-deck") == "layout_exists"
    assert await lab.refused("load_layout", name="missing") == "unknown_layout"
    assert await lab.refused("load_layout") == "layout_required"
  assert sorted(p.name for p in (tmp_path / ".ahc" / "layouts").iterdir()) == ["dilution-deck.yaml", "layout-1.yaml"]


async def test_references_come_from_this_workspace_only(tmp_path, open_lab):
  mine, other = tmp_path / "mine", tmp_path / "other"
  mine.mkdir()
  other.mkdir()
  async with open_lab(other, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT, name="elsewhere")
  async with open_lab(mine, "sim") as lab:  # rejected by the person: neither saved nor a reference
    await lab.call("load_layout", layout=copy.deepcopy(STARLET_LAYOUT) | {"aliases": {}}, name="rejected")
    check = await lab.call("verify", check="deck_matches_layout")
    await lab.call("record_verdict", check_id=check["check_id"], verdict="fail")
  for session in range(2):  # two runs here load the same layout: the person confirms it in the first
    async with open_lab(mine, "sim") as lab:
      await load_verified(lab, layout=STARLET_LAYOUT, name="deck")
  (mine / ".ahc" / "protocols" / "dilution.md").write_text("# Serial dilution\n1.5-fold, 11 columns.\n")
  async with open_lab(mine, "sim") as lab:
    refs = await lab.call("find_layout_references")
  assert [p["name"] for p in refs["protocols"]] == ["dilution.md"] and "1.5-fold" in refs["protocols"][0]["text"]
  assert [l["name"] for l in refs["layouts"]] == ["deck"]
  assert len(refs["run_layouts"]) == 1 and len(refs["run_layouts"][0]["runs"]) == 2
  assert refs["run_layouts"][0]["device"] == "hamilton.starlet"


# -- addressing ---------------------------------------------------------------------------------

def ids(items) -> list[str]:
  return [i.parent.get_child_identifier(i) for i in items]


async def test_columns_and_rows_can_be_named(starlet):
  await load_verified(starlet, layout=STARLET_LAYOUT)
  layout = starlet.server.lab.adapter.layout
  assert ids(layout.resolve_targets(["plate:col2"])) == [f"{r}2" for r in "ABCDEFGH"]  # top to bottom
  assert ids(layout.resolve_targets(["plate:rowB"])) == [f"B{c}" for c in range(1, 13)]  # left to right
  assert ids(layout.resolve_targets(["plate:COL12"])) == ids(layout.resolve_targets(["plate:A12:H12"]))
  assert ids(layout.resolve_targets(["plate:Rowh"])) == ids(layout.resolve_targets(["plate:H1:H12"]))
  assert ids(layout.resolve_tips(["tips:col1"])) == ids(layout.resolve_tips(["tips:A1:H1"]))
  for bad in ("plate:col0", "plate:col13", "plate:col 2", "plate:rowI", "plate:column2", "plate:A13"):
    with pytest.raises(LabError) as refused:
      layout.resolve_targets([bad])
    assert refused.value.code == "bad_wells" and "'labware:col2'" in refused.value.hint  # every form, listed
  with pytest.raises(LabError) as refused:
    layout.resolve_tips(["tips:col13"])
  assert refused.value.code == "bad_tips" and "tips:col1" in refused.value.hint


async def test_results_say_where_the_liquid_went(starlet):
  layout = copy.deepcopy(STARLET_LAYOUT)
  layout["aliases"]["buffer"] = "plate:col3"  # an alias may name a column
  layout["liquids"]["plate:col3"] = 200
  await load_verified(starlet, layout=layout)
  picked = await starlet.call("pick_up_tips", device="starlet.pip")
  assert picked["effective"]["where"] == "tips: column 1 (A1-H1)"
  drawn = await starlet.call("aspirate", device="starlet.pip", targets=["buffer"], volumes=50)
  assert drawn["effective"]["where"] == "plate: column 3 (A3-H3)"
  shared = await starlet.call("dispense", device="starlet.pip", targets=["waste"], volumes=50)
  assert shared["effective"]["where"] == "waste_trough"  # one container for all eight channels
  mixed = await starlet.call("mix", device="starlet.pip", targets=["plate:col3"], volume=50, repetitions=2)
  assert mixed["effective"]["where"] == "plate: column 3 (A3-H3)"
  logged = [e for e in starlet.server.lab.runlog.entries if e.get("op") == "aspirate"][-1]
  assert logged["effective"]["where"] == "plate: column 3 (A3-H3)"
  live = starlet.server.lab.adapter.layout
  assert live.where(live.resolve_targets(["plate:rowB"])) == "plate: row B (B1-B12)"
  assert live.where(live.resolve_targets(["plate:A1", "plate:A3", "plate:B2"])) == "plate: A1, A3, B2"
  assert live.where(live.resolve_targets(["diluent", "plate:A1"])) == "diluent_trough; plate: A1"


async def test_returned_tips_are_never_the_next_fresh_ones(starlet):
  await load_verified(starlet, layout=STARLET_LAYOUT)
  first = await starlet.call("pick_up_tips", device="starlet.pip")
  await starlet.call("aspirate", device="starlet.pip", targets=["stock"], volumes=50)
  await starlet.call("dispense", device="starlet.pip", targets=["waste"], volumes=50)
  await starlet.call("drop_tips", device="starlet.pip", mode="return")  # back in column 1, wet with stock
  second = await starlet.call("pick_up_tips", device="starlet.pip")
  assert first["effective"]["where"] == "tips: column 1 (A1-H1)"
  assert second["effective"]["where"] == "tips: column 2 (A2-H2)"
  await starlet.call("drop_tips", device="starlet.pip")
  again = await starlet.call("pick_up_tips", device="starlet.pip", tips=["tips:col1"])  # named: the agent's choice
  assert again["effective"]["tips"] == first["effective"]["tips"]
