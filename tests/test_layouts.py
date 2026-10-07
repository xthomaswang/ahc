"""Layouts: the agent writes them from this workspace's references and they are saved in .ahc/layouts/."""

import copy

import pytest
import yaml

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
