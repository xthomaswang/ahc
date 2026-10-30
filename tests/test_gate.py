"""The motion gate: after load_layout nothing moves until deck_matches_layout has passed (server-enforced).

In simulation the simulator judges the deck check when the task folder holds a reference for the
layout; without one the person confirms the layout first, as on real hardware.
"""

import copy

import pytest

from pylabrobot.resources import Coordinate, Resource

from ahc.devices.base import Adapter
from ahc.examples import STARLET_LAYOUT
from ahc.workspace import estop
from conftest import give_reference

pytestmark = pytest.mark.anyio

DEV = "starlet.pip"
ACTIONS = [
  ("pick_up_tips", dict(device=DEV)),
  ("aspirate", dict(device=DEV, targets=["diluent"], volumes=50)),
  ("dispense", dict(device=DEV, targets=["plate:A1:H1"], volumes=50)),
  ("mix", dict(device=DEV, targets=["plate:A1:H1"], volume=20, repetitions=2)),
  ("drop_tips", dict(device=DEV)),
  ("invoke", dict(device=DEV, op="sense_tip_presence")),
]


def detach(lab, name):
  """Fault injection: take a labware off the simulated deck, as if someone removed it."""
  res = lab.server.lab.adapter.layout.labware[name]
  parent, location = res.parent, res.location
  parent.unassign_child_resource(res)
  return lambda: parent.assign_child_resource(res, location=location)


async def deck_check(lab):
  return await lab.call("verify", check="deck_matches_layout")


@pytest.mark.parametrize("tool,args", ACTIONS, ids=[t for t, _ in ACTIONS])
async def test_every_action_waits_for_the_deck_check(starlet, tool, args):
  loaded = await starlet.call("load_layout", layout=STARLET_LAYOUT)
  assert loaded["gate"]["state"] == "pending"
  assert await starlet.refused(tool, **args) == "gate_pending"


async def test_passing_opens_the_gate_and_a_new_layout_closes_it(starlet):
  give_reference(starlet)
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  verdict = await deck_check(starlet)
  assert verdict["verdict"] == "pass" and verdict["simulated"] and verdict["gate"]["state"] == "passed"
  await starlet.call("pick_up_tips", device=DEV)
  await starlet.call("drop_tips", device=DEV, mode="return")
  await starlet.call("load_layout", layout=STARLET_LAYOUT)  # a new layout is a new gate
  assert await starlet.refused("pick_up_tips", device=DEV) == "gate_pending"
  assert (await starlet.call("lab_overview"))["gate"]["state"] == "pending"


async def test_a_failed_deck_check_blocks_everything_until_the_deck_is_fixed(starlet):
  give_reference(starlet)
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  await deck_check(starlet)
  await starlet.call("pick_up_tips", device=DEV)
  reattach = detach(starlet, "plate")  # mid-run: tips are mounted when the deck stops matching
  verdict = await deck_check(starlet)
  assert verdict["verdict"] == "fail" and verdict["gate"]["state"] == "failed"
  assert not verdict["evidence"]["digital_twin"]["labware"]["plate"]["matches"]
  assert verdict["evidence"]["digital_twin"]["labware"]["tips"]["matches"]
  assert await starlet.refused("aspirate", device=DEV, targets=["diluent"], volumes=50) == "gate_failed"
  assert await starlet.refused("drop_tips", device=DEV) == "gate_failed"  # tips stay until the deck is fixed
  reattach()
  assert (await deck_check(starlet))["gate"]["state"] == "passed"
  await starlet.call("drop_tips", device=DEV)


async def test_other_checks_leave_the_gate_alone(starlet):
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  await starlet.call("verify", check="liquid_present", targets=["diluent"], min_volume_ul=1)
  assert (await starlet.call("get_state"))["gate"]["state"] == "pending"


async def test_on_real_hardware_the_person_opens_the_gate(starlet, monkeypatch):
  monkeypatch.setattr(Adapter, "simulated", property(lambda self: False))  # judged as on real hardware
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  first = await deck_check(starlet)
  assert first["verdict"] == "pending" and first["gate"]["state"] == "pending"
  failed = await starlet.call("record_verdict", check_id=first["check_id"], verdict="fail")
  assert failed["gate"]["state"] == "failed" and "corrected layout" in failed["next"]
  assert await starlet.refused("pick_up_tips", device=DEV) == "gate_failed"
  second = await deck_check(starlet)
  # The first check was already answered; only the newest deck check can open the gate.
  assert await starlet.refused("record_verdict", check_id=first["check_id"], verdict="pass") == "unknown_check_id"
  tips = await starlet.call("verify", check="tips_mounted", device=DEV, expect="none")
  await starlet.call("record_verdict", check_id=tips["check_id"], verdict="pass")  # not the deck check
  assert (await starlet.call("get_state"))["gate"]["state"] == "pending"
  # While the person's deck check waits, every pointer names that one check, and verify returns it again.
  again = await deck_check(starlet)
  assert again["check_id"] == second["check_id"] and again["verdict"] == "pending" and "repeat" in again
  assert f"record_verdict(check_id='{second['check_id']}', verdict='pass', note=" in await starlet.call_text("pick_up_tips", device=DEV)
  assert f"Deck check {second['check_id']} waits for the person" in (await starlet.call("lab_overview"))["next"]
  await starlet.call("load_layout", layout=STARLET_LAYOUT)  # reloading starts a new gate
  third = await deck_check(starlet)
  assert await starlet.refused("record_verdict", check_id=second["check_id"], verdict="pass") == "check_superseded"
  assert await starlet.refused("pick_up_tips", device=DEV) == "gate_pending"  # a stale verdict opened nothing
  opened = await starlet.call("record_verdict", check_id=third["check_id"], verdict="pass")
  assert opened["gate"]["state"] == "passed" and opened["next"] == "Liquid handling is open; describe_device(component) for limits."
  await starlet.call("pick_up_tips", device=DEV)
  refused = [e["error"] for e in starlet.server.lab.runlog.entries if e.get("op") == "record_verdict"]
  assert refused == ["unknown_check_id", "check_superseded"]  # refused verdicts are in the run log too


async def test_gate_changes_are_in_the_run_log(starlet):
  give_reference(starlet)
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  await deck_check(starlet)
  run = starlet.server.lab.runlog
  assert [(e["state"], e["reason"]) for e in run.entries if e.get("type") == "gate"] == \
    [("pending", "layout loaded"), ("passed", "deck check pass")]


async def test_gate_refusals_are_logged(starlet):
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  await starlet.refused("pick_up_tips", device=DEV)
  assert (await starlet.call("get_run"))["refused"] == {"gate_pending": 1}


async def test_every_refusal_is_in_the_run_log_once(starlet):
  give_reference(starlet)
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  await deck_check(starlet)
  refusals = [("pick_up_tips", dict(device="starlet.nope"), "unknown_component"),
              ("pick_up_tips", dict(device="starlet.head96"), "not_supported"),
              ("aspirate", dict(device=DEV, targets=["diluent"], volumes=50), "no_tips"),
              ("verify", dict(check="nope"), "unknown_check"),
              ("verify", dict(check="liquid_present", targets=["nowhere"]), "unknown_target"),
              ("record_verdict", dict(check_id="nope", verdict="pass"), "unknown_check_id")]
  for tool, args, code in refusals:
    assert await starlet.refused(tool, **args) == code
  logged = [(e["op"], e["error"]) for e in starlet.server.lab.runlog.entries if e.get("status") == "refused"]
  assert logged == [(tool, code) for tool, _, code in refusals]


async def test_a_live_view_wrapping_the_device_does_not_fail_the_deck_check(starlet):
  # The 3D viewer puts the device under its own root; the labware has not moved.
  give_reference(starlet)
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  wrapper = Resource(name="lab", size_x=2000, size_y=1000, size_z=1000)
  wrapper.assign_child_resource(starlet.server.lab.adapter.view_root(), location=Coordinate(100, 50, 0))
  assert (await deck_check(starlet))["verdict"] == "pass"


def other_layout():
  layout = copy.deepcopy(STARLET_LAYOUT)
  layout["liquids"]["stock_trough"] = 5000
  return layout


async def test_a_layout_without_a_reference_waits_for_the_person(starlet):
  ws = starlet.server.lab.ws
  loaded = await starlet.call("load_layout", layout=STARLET_LAYOUT)  # the built-in example, nothing to base it on
  assert loaded["needs_person"] and not loaded["saved"] and loaded["gate"]["needs_person"]
  assert not ws.layout_path("layout-1").exists()  # not a reference before the person confirms it
  check = await deck_check(starlet)
  assert check["verdict"] == "pending" and check["judged_by"].startswith("the person") and check["simulated"]
  assert check["evidence"]["digital_twin"]["as_expected"]  # the simulator's comparison is still there as evidence
  text = await starlet.call_text("pick_up_tips", device=DEV)
  assert "[gate_pending]" in text and "has not confirmed this layout" in text
  opened = await starlet.call("record_verdict", check_id=check["check_id"], verdict="pass")
  assert opened["gate"]["state"] == "passed" and opened["saved_as"] == "layout-1" and ws.layout_path("layout-1").is_file()
  await starlet.call("pick_up_tips", device=DEV)
  await starlet.call("drop_tips", device=DEV, mode="return")
  again = await starlet.call("load_layout", layout=other_layout())  # the folder has a reference now
  assert "needs_person" not in again and again["saved_as"] == "layout-2"
  assert (await deck_check(starlet))["judged_by"] == "simulation"


async def test_a_rejected_layout_is_no_reference(starlet):
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  check = await deck_check(starlet)
  failed = await starlet.call("record_verdict", check_id=check["check_id"], verdict="fail")
  assert failed["gate"]["state"] == "failed" and "corrected layout" in failed["next"]
  assert await starlet.refused("pick_up_tips", device=DEV) == "gate_failed"
  assert not starlet.server.lab.ws.has_reference()  # not saved, and not counted from the run log
  corrected = await starlet.call("load_layout", layout=other_layout())
  assert corrected["needs_person"]  # the person confirms the corrected layout too
  assert (await deck_check(starlet))["verdict"] == "pending"


@pytest.mark.parametrize("earlier", ["no gate yet", "a passed gate"])
async def test_a_load_that_fails_after_the_device_loaded_keeps_everything_closed(starlet, monkeypatch, earlier):
  give_reference(starlet)
  if earlier == "a passed gate":
    await starlet.call("load_layout", layout=STARLET_LAYOUT)
    assert (await deck_check(starlet))["gate"]["state"] == "passed"

  def disk_full(*args, **kwargs):
    raise OSError("No space left on device")

  monkeypatch.setattr(starlet.server.lab.ws, "save_layout", disk_full)  # fails once the device holds the new deck
  assert await starlet.refused("load_layout", layout=other_layout()) == "device_refused"
  for tool, args in ACTIONS:  # a deck nobody checked: nothing moves
    assert await starlet.refused(tool, **args) == "gate_pending", tool
  monkeypatch.undo()
  assert (await deck_check(starlet))["gate"]["state"] == "passed"
  await starlet.call("pick_up_tips", device=DEV)


async def test_a_gate_belongs_to_the_layout_the_device_holds(starlet):
  give_reference(starlet)
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  await deck_check(starlet)
  lab = starlet.server.lab
  gate, layout = lab.gate, lab.adapter.layout
  lab.gate = None  # a loaded layout without a gate counts as not checked
  assert "never started" in await starlet.call_text("pick_up_tips", device=DEV)
  lab.gate, lab.adapter.layout = gate, copy.copy(layout)  # a gate of another layout than the device holds
  assert "another layout" in await starlet.call_text("pick_up_tips", device=DEV)
  lab.adapter.layout = layout
  await starlet.call("pick_up_tips", device=DEV)


async def test_the_emergency_stop_keeps_a_layout_waiting_for_the_person(starlet):
  ws = starlet.server.lab.ws
  await starlet.call("load_layout", layout=STARLET_LAYOUT)
  estop.engage(ws, by="test")
  assert await starlet.refused("pick_up_tips", device=DEV) == "emergency_stop"
  estop.release(ws, by="person")
  check = await deck_check(starlet)  # a new gate after the release, still the person's to open
  assert check["verdict"] == "pending" and check["gate"]["needs_person"]
