"""recover_tips at the server: the gates, the defaults, the layout it works from, what it tells the agent and what it
leaves standing. The adapter's own recover_tips/recovers_tips are stubbed here (their tests live with the adapter);
the instrument behind the server is PyLabRobot's simulator behind the real guard and bridge (tests/star_shadow.py),
so a stop, a release and a load run as they do on the real path. Nothing here reaches ssh, USB or a network."""

import asyncio
import copy
import json

import anyio
import pytest

from ahc.core.errors import LabError
from ahc.devices.hamilton.starlet import STARletAdapter
from ahc.examples import STARLET_LAYOUT
from ahc.workspace import Config, DeviceEntry, Workspace, estop
from ahc.workspace.config import HEADER
from conftest import confirm_as_person, confirm_pipettes_empty, load_verified
from test_starlet_flow import (DEV, acknowledged, configured, folder_of, late_reply_race, make_rig, moves,  # noqa: F401
                               press, ready, released, rig, run_entries, until)

pytestmark = pytest.mark.anyio

TIPS = "tips:A1:H1"
# The deck as it stands after the stop: the racks where they are, the column the mounted tips came from listed as empty.
STANDING = {**STARLET_LAYOUT, "tips_empty": [TIPS]}
RETURNED = {"moved": True, "mode": "return", "sensed_before": [1] * 8, "sensed_after": [0] * 8, "spots": [TIPS],
            "commands": 3}
DISCARDED = {"moved": True, "mode": "discard", "sensed_before": [1] * 8, "sensed_after": [0] * 8, "spots": [TIPS],
             "commands": 3}


class Adapter:
  """What the stub saw, and what it answers."""

  def __init__(self):
    self.supported = True
    self.result = dict(RETURNED)
    self.raises: Exception | None = None
    self.calls: list[dict] = []


@pytest.fixture
def adapter(monkeypatch):
  stub = Adapter()

  def recovers(self) -> bool:
    return stub.supported

  async def recover(self, comp, layout, mode, target):
    stub.calls.append({"comp": comp, "layout": layout, "mode": mode, "target": target})
    if stub.raises is not None:
      raise stub.raises
    return {**stub.result, "mode": mode} if stub.result.get("moved") else stub.result

  monkeypatch.setattr(STARletAdapter, "recovers_tips", recovers, raising=False)
  monkeypatch.setattr(STARletAdapter, "recover_tips", recover, raising=False)
  return stub


async def stopped_with_tips(lab, rig, **options):
  """Configured, loaded and the deck passed (so the folder holds the deck as a reference), a column picked up, the
  emergency stop pressed and released: tips may be on the channels, and the run cannot vouch for them."""
  await ready(lab) if not options else await _ready_with(lab, **options)
  record = await press(lab)
  await acknowledged(lab, record, "stopped")
  await released(lab)


async def _ready_with(lab, **options):
  await configured(lab, **options)
  await load_verified(lab, layout=STARLET_LAYOUT)
  await lab.call("pick_up_tips", device=DEV)


async def stopped_during_pickup(lab, rig):
  """The case that matters: the stop is pressed while the pick-up's firmware command (C0TP) is in flight; it finishes, so
  the tips are on the channels, and the call ends interrupted. The stop is then released."""
  await configured(lab)
  await load_verified(lab, layout=STARLET_LAYOUT)
  rig.shadow.hold("C0TP")
  outcome: dict = {}

  async def pick_up():
    outcome["text"] = await lab.call_text("pick_up_tips", device=DEV, tips=["tips:col1"])

  async with anyio.create_task_group() as tg:
    tg.start_soon(pick_up)
    await until(lambda: "C0TP" in rig.shadow.codes())
    record = await press(lab)
    await asyncio.sleep(0.3)
    rig.shadow.release("C0TP")
  assert "[emergency_stop]" in outcome["text"]
  await acknowledged(lab, record, "stopped")
  await released(lab)


def layouts_saved(lab) -> list[str]:
  folder = lab.server.lab.ws.layouts_dir
  return sorted(p.name for p in folder.glob("*")) if folder.is_dir() else []


# -- the gates, in the house order ---------------------------------------------------------------

async def test_a_stop_still_engaged_refuses_it_before_anything_else(tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await ready(lab)
    await press(lab)
    assert await lab.refused("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING) == "emergency_stop"
  assert adapter.calls == []


async def test_an_unconfirmed_config_refuses_it(tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await lab.call("configure_devices", devices=[{"id": "starlet", "model": "hamilton.starlet", "backend": "usb",
                                                  "options": {"bridge": "usb-host"}}])
    assert await lab.refused("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING) == "config_pending"
  assert adapter.calls == [] and moves(rig) == []


async def test_a_session_without_the_persons_go_ahead_refuses_it(tmp_path, open_lab, rig, adapter):
  folder = folder_of(tmp_path)
  config = Config(devices=[DeviceEntry("starlet", "hamilton.starlet", "usb", {"bridge": "usb-host", "serial": "T0001"})],
                  limits={})
  config.confirm("human")  # confirmed in an earlier session: this session still needs its own go-ahead
  Workspace(folder).write_config(config.data(), HEADER)
  async with open_lab(folder, backend=None) as lab:
    assert await lab.refused("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING) == "session_unconfirmed"
    await confirm_as_person(lab)
  assert adapter.calls == [] and moves(rig) == []


async def test_no_reported_plan_refuses_it(tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None, auto_plan=False) as lab:
    await configured(lab)
    assert await lab.refused("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING,
                             plan_step=1) == "plan_required"
  assert adapter.calls == []


async def test_a_pending_tips_check_holds_it_like_other_motion(tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    check = await lab.call("verify", check="tips_mounted", expect="none")
    assert check["verdict"] == "pending"
    assert await lab.refused("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING) == "check_pending"
    # the person fails the check (tips are on): recover_tips is the way out, as drop_tips is
    await lab.call("record_verdict", check_id=check["check_id"], verdict="fail", note="tips are still on")
    assert await lab.refused("pick_up_tips", device=DEV) == "check_failed"
    await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
  assert len(adapter.calls) == 1


# -- who may use it, and when ---------------------------------------------------------------------

async def test_a_device_that_cannot_recover_is_refused_with_the_by_hand_way(tmp_path, open_lab, rig, adapter):
  adapter.supported = False
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    text = await lab.call_text("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
    assert "[recovery_not_supported]" in text and "removes the tips by hand" in text
    # the texts of a device without it do not mention it
    refused = await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    assert "[tips_unconfirmed]" in refused and "recover_tips" not in refused
    assert "recover_tips" not in lab.server.lab.next_step()
  assert adapter.calls == []


async def test_a_simulated_device_is_refused(starlet, adapter):
  adapter.supported = True  # whatever the adapter class says, a simulation has nothing to recover
  assert await starlet.refused("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING) \
      == "recovery_not_supported"
  assert adapter.calls == []


async def test_a_run_that_knows_its_tips_needs_no_recovery(tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await ready(lab)  # tips on, and the run knows them
    text = await lab.call_text("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
    assert "[no_recovery_needed]" in text and "nothing is in doubt" in text and "Use drop_tips." in text
  assert adapter.calls == []


async def test_with_nothing_loaded_and_no_note_the_adapters_sensor_read_decides(tmp_path, open_lab, rig, adapter):
  folder = folder_of(tmp_path)
  async with open_lab(folder, backend=None) as lab:
    await configured(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)  # the folder knows this deck
  async with open_lab(folder, backend=None) as lab:  # a new session: nothing loaded, no note that tips may be on
    await configured(lab)
    assert lab.server.lab.tips_note() is None
    result = await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
    assert result["moved"] is True
  assert [call["target"] for call in adapter.calls] == [TIPS]  # not a dead end: a load refuses while tips are sensed


# -- the mode -------------------------------------------------------------------------------------

async def test_an_omitted_mode_discards_where_the_config_names_a_tip_waste(tmp_path, open_lab, rig, adapter):
  adapter.result = dict(DISCARDED)
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig, tip_waste=True)
    result = await lab.call("recover_tips", device=DEV, tips=TIPS, layout=STANDING)
  assert adapter.calls[0]["mode"] == "discard" and result["mode"] == "discard"


async def test_an_omitted_mode_without_a_tip_waste_asks_for_the_rack(tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    text = await lab.call_text("recover_tips", device=DEV, tips=TIPS, layout=STANDING)
    assert "[no_tip_waste]" in text and "names no tip waste, so the only choice is back into the rack" in text
    assert "Ask the person; then call recover_tips again with mode='return'." in text
    assert adapter.calls == []
    await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)  # the person chose the rack
  assert [c["mode"] for c in adapter.calls] == ["return"]


async def test_a_named_mode_is_passed_on_with_the_target(tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig, tip_waste=True)
    await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
  call = adapter.calls[0]
  assert call["mode"] == "return" and call["target"] == TIPS and call["comp"].name == "pip"


# -- the layout it works from ----------------------------------------------------------------------

async def test_the_layout_given_wins_over_a_name_and_the_last_deck_and_passes_through_untouched(
    tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    lab.server.lab.ws.save_layout("saved", STARLET_LAYOUT, "hamilton.starlet")
    given = {**STARLET_LAYOUT, "tips_empty": [TIPS, "tips2:A1"], "tips_returned": ["tips:A2:H2"]}
    before = copy.deepcopy(given)
    await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return", layout=given, name="saved")
    assert adapter.calls[-1]["layout"] == before and given == before  # both keys survive, nothing rewritten
    await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return", name="saved")
    assert adapter.calls[-1]["layout"] == STARLET_LAYOUT  # a saved layout's name, when no layout is given


async def test_with_neither_the_last_deck_is_used_when_it_is_complete(tmp_path, open_lab, rig, adapter, monkeypatch):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    complete = {"layout": {**STARLET_LAYOUT, "tips_empty": [TIPS]}, "unknown": []}
    monkeypatch.setattr(lab.server.lab, "last_deck", lambda: complete)
    await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return")
  assert adapter.calls[0]["layout"] == complete["layout"]


async def test_after_a_stop_in_a_pick_up_the_last_deck_leaves_the_racks_out_and_the_refusal_says_what_to_pass(
    tmp_path, open_lab, rig, adapter, late_reply_race):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_during_pickup(lab, rig)
    last = lab.server.lab.last_deck()
    assert last is not None and last["unknown"]  # what the stop made unknown is listed, the rack is not in its layout
    sent = list(moves(rig))
    text = await lab.call_text("recover_tips", device=DEV, tips=TIPS, mode="return")
    assert "[layout_incomplete]" in text and "leaves out what the stop made unknown" in text and "tips" in text
    assert ("Pass a layout that has the racks where they physically are, with the tip states as they are now (the spots "
            "the mounted tips came from as tips_empty)") in text
    assert moves(rig) == sent
  assert adapter.calls == []


async def test_without_any_deck_a_layout_is_required(tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    lab.server.lab.ws.deck_path.unlink()  # no deck state at all
    text = await lab.call_text("recover_tips", device=DEV, tips=TIPS, mode="return")
    assert "[layout_required]" in text and "tips_empty" in text
    assert await lab.refused("recover_tips", device=DEV, tips=TIPS, mode="return", name="nothing") == "unknown_layout"
  assert adapter.calls == []


async def test_a_layout_that_matches_no_deck_this_folder_ran_is_refused(tmp_path, open_lab, rig, adapter):
  other = copy.deepcopy(STANDING)
  for carrier in other["carriers"]:
    carrier["track"] = {"plate_car": 13, "trough_car": 7}.get(carrier["name"], carrier["track"])
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    sent = list(moves(rig))
    text = await lab.call_text("recover_tips", device=DEV, tips=TIPS, mode="return", layout=other)
    assert "[layout_unknown]" in text and "match no layout saved or run in this task folder" in text
    assert "track 7" in text  # how it differs, from the closest reference
    assert "Recover on the deck this task folder already ran" in text and "removes the tips another way" in text
    assert moves(rig) == sent
  assert adapter.calls == []


async def test_a_folder_with_no_reference_at_all_refuses_every_layout(tmp_path, open_lab, rig, adapter):
  folder = folder_of(tmp_path)
  config = Config(devices=[DeviceEntry("starlet", "hamilton.starlet", "usb", {"bridge": "usb-host", "serial": "T0001"})],
                  limits={})
  config.confirm("human")
  Workspace(folder).write_config(config.data(), HEADER)
  async with open_lab(folder, backend=None) as lab:
    await confirm_as_person(lab)
    lab.server.lab.ws.mark_tips_left({"run": "earlier", "mounted": {DEV: 8}, "why": "picked up",
                                      "at": "2026-11-11T10:00:00"})  # a restart with tips on: the note of an earlier run
    text = await lab.call_text("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
    assert "[layout_unknown]" in text and "No layout saved or run in this task folder" in text
  assert adapter.calls == [] and moves(rig) == []


# -- what the adapter refuses, what the agent is told ------------------------------------------------

@pytest.mark.parametrize("code", ["recovery_needs_initialized", "recovery_mismatch", "channel_spacing_refused",
                                  "tips_unreadable", "recovery_target_required", "bad_param"])
async def test_an_adapter_refusal_reaches_the_agent_unchanged_and_the_doubt_stays(tmp_path, open_lab, rig, adapter, code):
  adapter.raises = LabError(code, "the adapter's sentence.", "the adapter's hint.")
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    text = await lab.call_text("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
    assert text.endswith(f"[{code}] the adapter's sentence. Hint: the adapter's hint.")
    assert lab.server.lab.tips_note() is not None
    refused = [e for e in run_entries(lab) if e.get("op") == "recover_tips"]
    assert refused[-1]["status"] == "refused" and refused[-1]["error"] == code


async def test_what_the_agent_is_told_after_a_return(tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    result = await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
  assert {k: v for k, v in result.items() if k != "next"} == RETURNED
  assert f"The tips were put back into {TIPS}: those spots now hold used tips" in result["next"]
  assert "any liquid the tips held is still in them" in result["next"]
  assert "verify(check='tips_mounted', expect='none') without device and record_verdict" in result["next"]
  assert f"load_layout, declaring {TIPS} as tips_returned" in result["next"]


async def test_what_the_agent_is_told_after_a_discard(tmp_path, open_lab, rig, adapter):
  adapter.result = dict(DISCARDED)
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig, tip_waste=True)
    result = await lab.call("recover_tips", device=DEV, tips=TIPS, layout=STANDING)
  assert "The tips went into the tip waste, with any liquid they held." in result["next"]
  assert f"load_layout, declaring {TIPS} as tips_empty" in result["next"] and "tips_returned" not in result["next"]


async def test_a_tip_still_sensed_is_said_plainly_and_no_retry_is_offered(tmp_path, open_lab, rig, adapter):
  adapter.result = {**RETURNED, "sensed_after": [0, 0, 1, 0, 0, 0, 0, 0]}
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    result = await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
  assert "still senses a tip on channel(s) 3" in result["next"] and "must look at the instrument" in result["next"]
  assert "Do not call recover_tips again" in result["next"] and "load_layout" not in result["next"]


async def test_no_tip_sensed_moves_nothing_and_says_so(tmp_path, open_lab, rig, adapter):
  adapter.result = {"moved": False, "sensed_before": [0] * 8, "note": "no tip is sensed on any channel"}
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    result = await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
  assert result["moved"] is False and "nothing moved" in result["next"] and "no tip is sensed" in result["next"]


# -- what stands afterwards ------------------------------------------------------------------------

async def test_the_doubt_stands_after_a_recovery_until_the_persons_check_passes(tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    before = layouts_saved(lab)
    await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
    inner = lab.server.lab
    assert inner.tips_note() is not None  # not cleared by the recovery
    text = await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    assert "[tips_unconfirmed]" in text and "recover_tips" in text  # the way out is named while the doubt stands
    assert layouts_saved(lab) == before  # recover_tips saved nothing
    assert inner.adapter.layout is not None  # and did not start a run that forgets tips, or change the layout
    await confirm_pipettes_empty(lab)
    assert inner.tips_note() is None
    returned = {**STARLET_LAYOUT, "tips_returned": [TIPS]}
    await load_verified(lab, layout=returned)


async def test_the_call_is_in_the_run_log_like_other_motion(tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    await lab.call("recover_tips", device=DEV, tips=TIPS, mode="return", layout=STANDING)
    entry = [e for e in run_entries(lab) if e.get("op") == "recover_tips" and e.get("status") == "ok"][-1]
    assert entry["basic"]["tips"] == TIPS and entry["basic"]["mode"] == "return" and entry["basic"]["device"] == DEV
    assert entry["effective"]["spots"] == [TIPS] and entry["effective"]["commands"] == 3


async def test_every_text_that_sends_the_person_to_remove_tips_names_recover_tips_where_it_exists(
    tmp_path, open_lab, rig, adapter):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await stopped_with_tips(lab, rig)
    inner = lab.server.lab
    assert "recover_tips" in inner.next_step()
    assert "recover_tips" in (await lab.call("lab_overview"))["next"]
    text = await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    assert "remove any tips by hand (or there are none)." in text and "recover_tips" in text
    assert "ask the person which first" in text
  async with open_lab(folder_of(tmp_path, "real"), backend=None) as lab:
    assert "recover_tips" in lab.client.instructions
