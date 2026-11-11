"""The deck setup preview: report_decision(layout=...) builds the layout on a scratch deck (no device), the dashboard
draws it from the run log's deck map until the deck check passes, and load_layout without a layout loads it."""

import copy
import http.client
import json
import socket
import urllib.request
from pathlib import Path

import pytest

from ahc.devices.opentrons.brand import _readable, definition_cache
from ahc.server import INSTRUCTIONS, instructions
from ahc.viz.dashboard import current_state, replay
from ahc.workspace import Config, DeviceEntry, Workspace, estop
from ahc.workspace.config import HEADER

pytestmark = pytest.mark.anyio

# The lab's first real test deck: an empty target plate in front, a water plate behind it, one tip rack.
STAR_DECK = {
  "carriers": [
    {"type": "PLT_CAR_L5_DWP", "track": 1, "name": "plates",
     "sites": {"0": {"type": "Cor_96_wellplate_360ul_Fb", "name": "target"},
               "1": {"type": "Cor_96_wellplate_360ul_Fb", "name": "water"}}},
    {"type": "TIP_CAR_480_A00", "track": 13, "name": "tipcar",
     "sites": {"0": {"type": "hamilton_96_tiprack_300uL_filter", "name": "tips"}}},
  ],
  "liquids": {"water:A1:H1": 300},
  "aliases": {"diluent": "water:A1:H1"},
  "tips_empty": ["tips:A1:H1"],
  "tips_returned": ["tips:A2"],
}
OT2_PLATES = {  # no tip rack: its definition may need a download
  "slots": {"2": {"name": "reagents", "type": "cor_96_wellplate_2mL_Vb"},
            "3": {"name": "plate", "type": "Cor_96_wellplate_360ul_Fb"}},
  "liquids": {"reagents:A1:H1": 1800, "reagents:A2:H2": 500},
  "aliases": {"diluent": "reagents:A1:H1"},
}
needs_tip_definition = pytest.mark.skipif(not _readable(definition_cache("opentrons_96_tiprack_300ul")),
                                          reason="the opentrons_96_tiprack_300ul definition is not cached here")


def log_of(lab) -> Path:
  return Path(lab.server.lab.runlog.path)


def deck_entries(lab) -> list[dict]:
  return [e for e in lab.server.lab.runlog.entries if e.get("type") == "deck"]


async def preview(lab, layout, **more):
  return await lab.call("report_decision", decision="Plan the dilution", plan=["Set up the deck", "Load", "Check"],
                        layout=layout, **more)


def labware(deck_map: dict, name: str) -> dict:
  return next(lw for lw in deck_map["labware"] if lw["name"] == name)


# -- the preview never touches the device ------------------------------------------------------------

async def test_a_preview_on_a_real_robot_config_sends_nothing(tmp_path, open_lab, monkeypatch):
  config = Config(devices=[DeviceEntry("ot2", "opentrons.ot2", "robot",
                                       {"host": "192.0.2.10", "port": 31950, "robot_name": "lab-ot2"})], limits={})
  config.confirm("human")
  Workspace(tmp_path).write_config(config.data(), HEADER)
  attempts = []

  def refuse(*args, **kwargs):
    attempts.append(args)
    raise AssertionError("the preview reached for the network")

  monkeypatch.setattr(urllib.request, "urlopen", refuse)
  monkeypatch.setattr(socket.socket, "connect", refuse)
  async with open_lab(tmp_path, backend=None) as lab:
    result = await preview(lab, OT2_PLATES)
    adapter = lab.server.lab.adapter
    assert not adapter.simulated and adapter.layout is None and adapter.command_count() == 0
    assert result["deck"].startswith("slot 2: reagents (cor_96_wellplate_2mL_Vb)")
    assert "Deck setup" in result["layout_shown"] and "open_dashboard" in result["next"]
    assert lab.server.lab.planned_layout == OT2_PLATES and lab.server.lab.preview["map"]["kind"] == "slots"
    assert [s["slot"] for s in deck_entries(lab)[0]["map"]["slots"]] == list(range(1, 13))
  assert attempts == []


async def test_a_preview_works_while_the_emergency_stop_is_engaged(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    estop.engage(Workspace(tmp_path), by="test")
    result = await preview(lab, STAR_DECK)
    assert "plates site 1: water" in result["deck"]
    assert lab.server.lab.adapter.layout is None and lab.server.lab.adapter.command_count() == 0
    assert await lab.refused("load_layout", plan_step=1) == "emergency_stop"  # the load is still refused


async def test_a_saved_layout_can_be_previewed_by_name(tmp_path, open_lab):
  Workspace(tmp_path).save_layout("lab-deck", STAR_DECK, "hamilton.starlet")
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    result = await preview(lab, "lab-deck")
    assert "tipcar site 0: tips" in result["deck"] and deck_entries(lab)[0]["name"] == "lab-deck"
    assert await lab.refused("report_decision", decision="x", layout="no-such-layout") == "unknown_layout"
    loaded = await lab.call("load_layout", plan_step=1)  # no layout: the saved one, as if loaded by name
    assert loaded["saved_as"] == "lab-deck" and "needs_person" not in loaded


async def test_no_dashboard_open_suggests_opening_one(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    assert "open_dashboard" in (await preview(lab, STAR_DECK))["next"]
    lab.server.lab.dashboard = {"url": "http://127.0.0.1:1/", "httpd": None}
    assert "open_dashboard" not in (await preview(lab, STAR_DECK))["next"]
    alone = await lab.call("report_decision", decision="A changed deck", layout=STAR_DECK)  # without a plan
    assert alone["deck"] and "layout_shown" in alone


# -- a bad layout refuses the whole call --------------------------------------------------------------

async def test_a_layout_that_cannot_be_built_refuses_and_records_nothing(tmp_path, open_lab):
  bad_carrier = copy.deepcopy(STAR_DECK)
  bad_carrier["carriers"][0]["type"] = "TIP_CAR_384_A00"
  bad_track = copy.deepcopy(STAR_DECK)
  bad_track["carriers"][0]["track"] = 31
  bad_site = copy.deepcopy(STAR_DECK)
  bad_site["carriers"][0]["sites"]["9"] = {"type": "Cor_96_wellplate_360ul_Fb", "name": "extra"}
  bad_key = {"slots": {}}
  missing = {"carriers": [{"type": "PLT_CAR_L5_DWP", "track": 1, "name": "plates", "sites": {"0": {"name": "x"}}}]}
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    for layout, code in ((bad_carrier, "carrier_not_allowed"), (bad_track, "bad_track"), (bad_site, "bad_site"),
                         (bad_key, "bad_layout"), (missing, "bad_layout")):
      assert await lab.refused("report_decision", decision="Plan", plan=["Load"], layout=layout) == code
    lab_ = lab.server.lab
    assert lab_.plan.steps is None and lab_.planned_layout is None and lab_.preview is None
    assert lab_.runlog is None and not (tmp_path / ".ahc" / "runs").exists()  # nothing recorded, not even the plan


# -- the deck map ------------------------------------------------------------------------------------

async def test_the_ot2_deck_map_has_every_slot_and_the_trash(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", "opentrons.ot2") as lab:
    await preview(lab, OT2_PLATES)
    deck = deck_entries(lab)[0]["map"]
  assert deck["kind"] == "slots" and deck["size"] == {"x": 624.3, "y": 565.2}
  slots = {s["slot"]: s for s in deck["slots"]}
  assert sorted(slots) == list(range(1, 13)) and slots[12]["trash"] and not any(slots[n]["trash"] for n in range(1, 12))
  assert {n: s["labware"] for n, s in slots.items() if s["labware"]} == {2: "reagents", 3: "plate"}
  assert slots[1]["y"] < slots[10]["y"] and slots[1]["x"] < slots[3]["x"]  # slot 1 is front left (y grows to the back)
  reagents = labware(deck, "reagents")
  assert reagents["slot"] == 2 and reagents["alias"] is None and reagents["aliases"] == [{"alias": "diluent", "wells": "A1:H1"}]
  assert reagents["liquids"] == [{"wells": "A1-H1", "ul": 1800, "ids": [f"{r}1" for r in "ABCDEFGH"]},
                                 {"wells": "A2-H2", "ul": 500, "ids": [f"{r}2" for r in "ABCDEFGH"]}]
  assert reagents["a1"][0] < reagents["x"] + reagents["w"] / 2 and reagents["a1"][1] > reagents["y"] + reagents["h"] / 2
  assert labware(deck, "plate")["liquids"] == []


@needs_tip_definition
async def test_an_ot2_tip_rack_carries_its_tip_states(tmp_path, open_lab):
  layout = copy.deepcopy(OT2_PLATES)
  layout["slots"]["1"] = {"name": "tips", "type": "opentrons_96_tiprack_300ul"}
  layout["tips_empty"], layout["tips_returned"] = ["tips:A1:H3", "tips:A4"], ["tips:B4:C4"]
  async with open_lab(tmp_path, "sim", "opentrons.ot2") as lab:
    await preview(lab, layout)
    tips = labware(deck_entries(lab)[0]["map"], "tips")
  assert tips["kind"] == "tip_rack" and tips["rows"] == 8 and tips["cols"] == 12
  assert len(tips["tips"]["empty"]) == 25 and tips["tips"]["returned"] == ["B4", "C4"]
  assert tips["tips_text"] == "empty A1-H1, A2-H2, A3-H3, A4; returned B4-C4"


async def test_the_starlet_deck_map_has_tracks_carriers_sites_and_waste(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await preview(lab, STAR_DECK)
    deck = deck_entries(lab)[0]["map"]
  assert deck["kind"] == "tracks"
  ticks = deck["tracks"]["ticks"]
  assert [t["track"] for t in ticks] == list(range(1, 31)) and deck["tracks"]["width"] == 22.5
  assert ticks[0]["x"] == 100.0 and ticks[12]["x"] == 370.0  # read from the deck's rails
  plates, tipcar = deck["carriers"]
  assert (plates["name"], plates["type"], plates["track"], plates["track_end"]) == ("plates", "PLT_CAR_L5_DWP", 1, 6)
  assert (tipcar["name"], tipcar["track"], tipcar["track_end"]) == ("tipcar", 13, 18)
  assert plates["x"] == ticks[0]["x"] and tipcar["x"] == ticks[12]["x"]
  sites = plates["sites"]
  assert [s["site"] for s in sites] == [0, 1, 2, 3, 4]
  assert min(sites, key=lambda s: s["y"])["site"] == 0 and sites[0]["front"] and not any(s["front"] for s in sites[1:])
  assert [s["y"] for s in sites] == sorted(s["y"] for s in sites)  # site 0 is the front-most: the lowest y
  assert [s["venus_position"] for s in sites] == [5, 4, 3, 2, 1]  # VENUS counts from the back
  assert [s["labware"] for s in sites] == ["target", "water", None, None, None]
  assert {f["label"] for f in deck["fixed"]} == {"CO-RE 96 waste", "waste block"}  # the arm a device adds is not
  water, tips = labware(deck, "water"), labware(deck, "tips")
  assert (water["carrier"], water["site"], water["alias"]) == ("plates", 1, None)
  assert water["aliases"] == [{"alias": "diluent", "wells": "A1:H1"}]
  assert water["liquids"] == [{"wells": "A1-H1", "ul": 300, "ids": [f"{r}1" for r in "ABCDEFGH"]}]
  assert labware(deck, "target")["liquids"] == []
  assert tips["kind"] == "tip_rack" and tips["tips"]["empty"] == [f"{r}1" for r in "ABCDEFGH"]
  assert tips["tips"]["returned"] == ["A2"] and tips["tips_text"] == "empty A1-H1; returned A2"
  assert water["y"] > labware(deck, "target")["y"]  # the water plate is behind the target plate


async def test_a_four_site_carrier_counts_venus_positions_from_four(tmp_path, open_lab):
  layout = {"carriers": [{"type": "Trough_CAR_4R200_A00", "track": 5, "name": "troughs",
                          "sites": {"0": {"type": "Hamilton_1_trough_200ml_Vb", "name": "buffer"}}}]}
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await preview(lab, layout)
    carrier = deck_entries(lab)[0]["map"]["carriers"][0]
  assert [s["venus_position"] for s in carrier["sites"]] == [4, 3, 2, 1] and carrier["sites"][0]["front"]


# -- the dashboard -----------------------------------------------------------------------------------

async def test_the_dashboard_pins_the_deck_until_the_deck_check_passes(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await preview(lab, STAR_DECK)
    planned = replay(log_of(lab))["deck"]
    assert planned["mode"] == "plan" and planned["pinned"] and not planned["checked"] and planned["differs"] is None
    assert planned["map"]["kind"] == "tracks" and "plates site 0: target" in planned["text"]
    assert current_state(Workspace(tmp_path), log_of(lab))["deck"]["pinned"]

    loaded = await lab.call("load_layout", plan_step=2)  # the previewed deck
    assert loaded["labware"] == {"target": "Cor_96_wellplate_360ul_Fb", "water": "Cor_96_wellplate_360ul_Fb",
                                 "tips": "hamilton_96_tiprack_300uL_filter"} and "differs_from_preview" not in loaded
    shown = replay(log_of(lab))["deck"]
    assert shown["mode"] == "loaded" and shown["pinned"] and shown["differs"] is None  # not checked yet
    assert {f["name"] for f in shown["map"]["fixed"]} == {"trash_core96", "waste_block"}  # no arm drawn

    check = await lab.call("verify", check="deck_matches_layout", plan_step=3)
    assert check["verdict"] == "pending" and "'Deck setup' card" in check["next"]  # a layout no reference matches
    assert replay(log_of(lab))["deck"]["pinned"]
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    folded = replay(log_of(lab))["deck"]
    assert folded["mode"] == "loaded" and folded["checked"] and not folded["pinned"]

    assert await lab.refused("load_layout", plan_step=2) == "layout_required"  # the preview was used once
    await preview(lab, STAR_DECK)  # a reload: the deck is reported again, then loaded; it needs a new deck check
    await lab.call("load_layout", plan_step=2)
    assert replay(log_of(lab))["deck"]["pinned"]
    check = await lab.call("verify", check="deck_matches_layout", plan_step=3)  # now a reference: the simulator judges
    assert check["verdict"] == "pass" and not replay(log_of(lab))["deck"]["pinned"]
    await preview(lab, STAR_DECK)  # a changed layout before loading
    assert replay(log_of(lab))["deck"]["mode"] == "plan" and replay(log_of(lab))["deck"]["pinned"]


async def test_the_dashboard_says_when_the_loaded_deck_differs_from_the_plan(tmp_path, open_lab):
  other = copy.deepcopy(STAR_DECK)
  other["carriers"][0]["sites"] = {"0": other["carriers"][0]["sites"]["0"], "2": other["carriers"][0]["sites"]["1"]}
  other["liquids"] = {"water:A1:H1": 250}
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await preview(lab, STAR_DECK)
    loaded = await lab.call("load_layout", plan_step=2, layout=other)
    assert loaded["differs_from_preview"] == [
      "water (placed): planned plates site 1 (Cor_96_wellplate_360ul_Fb), loaded plates site 2 (Cor_96_wellplate_360ul_Fb)",
      "water (liquids): planned A1-H1 300 uL, loaded A1-H1 250 uL"]
    shown = replay(log_of(lab))["deck"]
    assert shown["mode"] == "loaded" and shown["differs"] == loaded["differs_from_preview"]
    assert [s["labware"] for s in shown["map"]["carriers"][0]["sites"][:3]] == ["target", None, "water"]


async def test_both_dashboard_pages_carry_the_deck_setup_card_and_serve_its_state(tmp_path, open_lab):
  from ahc.viz.dashboard import serve

  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await preview(lab, STAR_DECK)
    ws = Workspace(tmp_path)
    control, viewer = serve(ws, None, 0, open_browser=False), serve(ws, None, 0, open_browser=False, view_only=True)
    try:
      for httpd in (control, viewer):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
        conn.request("GET", "/")
        page = conn.getresponse().read().decode()
        assert 'id="deckcard"' in page and "function deckCard" in page and "st.deck" in page
        conn.request("GET", "/state")
        state = json.loads(conn.getresponse().read())
        assert state["deck"]["pinned"] and state["deck"]["map"]["carriers"][0]["name"] == "plates"
        conn.close()
    finally:
      control.shutdown()
      viewer.shutdown()


def test_a_log_without_a_deck_has_none(tmp_path):
  runs = tmp_path / ".ahc" / "runs"
  runs.mkdir(parents=True)
  log = runs / "r.jsonl"
  log.write_text(json.dumps({"type": "run", "run_id": "r", "device": None, "backend": None}) + "\n")
  assert replay(log)["deck"] is None


# -- load_layout reuses the preview ------------------------------------------------------------------

async def test_load_layout_without_a_layout_loads_the_preview(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await preview(lab, STAR_DECK)
    loaded = await lab.call("load_layout", plan_step=2)
    assert loaded["labware"]["water"] == "Cor_96_wellplate_360ul_Fb" and loaded["liquids_ul"]["water_well_A1"] == 300
    assert loaded["needs_person"] and loaded["saved"] is False and loaded["saved_as"] == "layout-1"  # as if passed as layout
    assert loaded["tip_states"] == {"tips": {"empty": [f"{r}1" for r in "ABCDEFGH"], "returned": ["A2"]}}
    entry = next(e for e in lab.server.lab.runlog.entries if e.get("op") == "load_layout")
    assert entry["basic"]["layout"] == STAR_DECK
    check = await lab.call("verify", check="deck_matches_layout", plan_step=3)
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    assert (tmp_path / ".ahc" / "layouts" / "layout-1.yaml").is_file()  # saved once the person confirmed it
    assert lab.server.lab.planned_layout is None and lab.server.lab.preview is None  # spent by that load
    refused = await lab.call_text("load_layout", plan_step=2)  # a second load with no layout: not the plan's original deck
    assert "[layout_required]" in refused and "report_decision(layout=...)" in refused and "last_deck.layout" in refused
    await preview(lab, STAR_DECK)  # reported again (e.g. last_deck.layout), it loads once more
    again = await lab.call("load_layout", plan_step=2)
    assert again["matches_reference"] and "needs_person" not in again
    assert await lab.refused("load_layout", plan_step=2) == "layout_required"


async def test_load_layout_with_a_different_layout_reports_the_difference(tmp_path, open_lab):
  other = copy.deepcopy(STAR_DECK)
  other["liquids"] = {"water:A1:H1": 200}
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await preview(lab, STAR_DECK)
    loaded = await lab.call("load_layout", plan_step=2, layout=other)
    assert loaded["differs_from_preview"] == ["water (liquids): planned A1-H1 300 uL, loaded A1-H1 200 uL"]
    assert lab.server.lab.planned_layout is None  # any successful load spends the preview
    await preview(lab, STAR_DECK)
    same = await lab.call("load_layout", plan_step=2, layout=STAR_DECK)  # the previewed deck passed explicitly
    assert "differs_from_preview" not in same and lab.server.lab.planned_layout is None
    await preview(lab, STAR_DECK)
    named = await lab.call("load_layout", plan_step=2, layout=other, name="other-deck")  # by name too
    assert named["differs_from_preview"] and lab.server.lab.planned_layout is None


async def test_a_failed_load_keeps_the_preview(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await preview(lab, STAR_DECK)
    assert await lab.refused("load_layout", plan_step=9) == "bad_plan_step"  # refused before anything loads
    assert await lab.refused("load_layout", plan_step=2, name="missing") == "unknown_layout"
    assert lab.server.lab.planned_layout == STAR_DECK and lab.server.lab.preview is not None
    estop.engage(Workspace(tmp_path), by="test")
    assert await lab.refused("load_layout", plan_step=2) == "emergency_stop"
    assert lab.server.lab.planned_layout == STAR_DECK
    estop.release(Workspace(tmp_path), by="test")
    loaded = await lab.call("load_layout", plan_step=2)  # the preview was still there
    assert loaded["labware"]["water"] == "Cor_96_wellplate_360ul_Fb" and lab.server.lab.planned_layout is None


async def test_load_layout_without_a_layout_or_a_preview_is_refused(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    await lab.call("report_decision", decision="Plan", plan=["Load"])  # a plan, but no layout reported
    text = await lab.call_text("load_layout", plan_step=1)
    assert "[layout_required]" in text and "report_decision(layout=...)" in text
    assert await lab.refused("load_layout", name="missing") == "unknown_layout"  # a name is still a choice


# -- what the agent is told --------------------------------------------------------------------------

async def test_the_instructions_and_tool_descriptions_tell_the_agent_about_the_preview(tmp_path, open_lab):
  assert "report_decision(plan=[...], layout=...) (the person sets the deck up from it before load_layout)" in INSTRUCTIONS
  assert len(instructions(False)) < 4500
  async with open_lab(tmp_path, "sim", "hamilton.starlet") as lab:
    tools = {t.name: t for t in (await lab.client.list_tools()).tools}
  assert "Deck setup card" in tools["report_decision"].description and "layout" in tools["report_decision"].input_schema["properties"]
  assert "report_decision(layout=...)" in tools["load_layout"].description
  assert "differs_from_preview" in tools["load_layout"].description


# -- the 3D view -------------------------------------------------------------------------------------

async def test_the_live_view_root_grows_to_fit_the_device_that_replaces_a_preview(monkeypatch):
  import pylabrobot.resources as plr
  from pylabrobot.resources import Resource

  from ahc.devices.hamilton.starlet import STARletAdapter
  from ahc.viz import viewer

  class FakeViewer:
    def __init__(self, root, **kwargs):
      self.url = "http://viewer/"

    async def start(self):
      pass

  monkeypatch.setattr(viewer, "Viewer3D", FakeViewer)
  view = viewer.LiveView("test", open_browser=False)
  scratch = plr.STARLetDeck()
  adapter = STARletAdapter.__new__(STARletAdapter)  # only preview_root is used: no device, no spec
  await view.show(adapter.preview_root(scratch))
  assert view.current is scratch and view.root.get_size_z() == 334.7 + 140.0  # sized as view_root() sizes the device
  device = Resource(name="device", size_x=1130.0, size_y=785.8, size_z=903.0)  # what a STARlet reports (it is larger)
  await view.show(device)
  assert view.current is device and scratch.parent is None
  assert (view.root.get_size_x(), view.root.get_size_y(), view.root.get_size_z()) == (1130.0, 785.8, 903.0)
