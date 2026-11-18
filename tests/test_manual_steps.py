"""Manual steps: work the device cannot do (centrifuge, incubate off deck, vortex, move labware) that the person does
by hand. request_manual_step asks for it; it is always the person's, in simulation too, and holds motion like their
tips and liquid checks: nothing moves while it waits (check_pending), the person's approval of record_verdict says it
is done, and once they report they could not do it only drop_tips moves until a later manual step passes."""

from pathlib import Path

import pytest

from ahc.examples import STARLET_LAYOUT
from ahc.viz.dashboard import replay
from conftest import give_reference, load_verified

pytestmark = pytest.mark.anyio

PIP = "starlet.pip"
SPIN = "Centrifuge plate at 6000 x g for 1 min, then put it back in its slot"


async def ready(lab):
  give_reference(lab)
  await load_verified(lab, layout=STARLET_LAYOUT)
  await lab.call("pick_up_tips", device=PIP)


async def test_a_manual_step_waits_for_the_person_in_simulation_and_holds_motion(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await ready(lab)
    step = await lab.call("request_manual_step", instruction=SPIN, why="no centrifuge on the deck",
                          labware=["plate"], duration_min=1)
    assert step["verdict"] == "pending" and step["check"] == "manual_step" and step["simulated"] is True
    assert step["ask_person"] == f"Done by hand: {SPIN} (about 1 min); labware: plate (plate_car site 0)."
    assert f"record_verdict(check_id='{step['check_id']}', verdict='pass'" in step["next"]
    for tool, args in [("aspirate", dict(device=PIP, targets=["diluent"], volumes=50)),
                       ("drop_tips", dict(device=PIP))]:
      text = await lab.call_text(tool, **args)
      assert "[check_pending]" in text and f"manual step {step['check_id']} (by hand: {SPIN})" in text
      assert "A new verify" not in text
    done = await lab.call("record_verdict", check_id=step["check_id"], verdict="pass", note=step["ask_person"])
    assert done["verdict"] == "pass" and done["judged_by"] == "human" and done["instruction"] == SPIN
    await lab.call("aspirate", device=PIP, targets=["diluent"], volumes=50)  # held no longer
    assert not lab.server.lab.verification.holds


async def test_after_the_person_could_not_do_it_only_drop_tips_moves_until_a_later_one_passes(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await ready(lab)
    step = await lab.call("request_manual_step", instruction=SPIN)
    failed = await lab.call("record_verdict", check_id=step["check_id"], verdict="fail", note="the centrifuge is in use")
    assert "could not do the manual step" in failed["next"] and "Only drop_tips moves" in failed["next"]
    assert await lab.refused("aspirate", device=PIP, targets=["diluent"], volumes=50) == "check_failed"
    await lab.call("drop_tips", device=PIP)  # still allowed
    assert await lab.refused("pick_up_tips", device=PIP) == "check_failed"
    again = await lab.call("request_manual_step", instruction=SPIN + " (second centrifuge)")
    await lab.call("record_verdict", check_id=again["check_id"], verdict="pass", note=again["ask_person"])
    await lab.call("pick_up_tips", device=PIP)  # the later pass cleared the failed one
    assert not lab.server.lab.verification.holds


async def test_a_manual_step_is_refused_without_an_instruction_or_with_labware_not_on_the_deck(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await ready(lab)
    assert await lab.refused("request_manual_step", instruction="  ") == "bad_instruction"
    assert await lab.refused("request_manual_step", instruction=SPIN, labware=["centrifuge"]) == "unknown_labware"
    assert await lab.refused("request_manual_step", instruction=SPIN, duration_min=-1) == "bad_duration"
    assert not lab.server.lab.verification.holds  # nothing refused holds anything
    await lab.call("aspirate", device=PIP, targets=["diluent"], volumes=50)


async def test_a_manual_step_names_its_plan_step_and_an_alias_resolves_to_its_labware(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", auto_plan=False) as lab:
    assert (await lab.client.call_tool("report_decision", {"decision": "run", "plan": ["Load", "Spin"]})).is_error is False
    give_reference(lab)
    await load_verified(lab, layout=STARLET_LAYOUT, plan_step=1)
    no_step = await lab.client.call_tool("request_manual_step", {"instruction": SPIN})
    assert no_step.is_error
    step = await lab.call("request_manual_step", instruction="Top up the diluent trough by hand", labware=["diluent"],
                          plan_step=2)
    assert step["labware"] == ["diluent (trough_car site 0)"]
    entry = [e for e in lab.server.lab.runlog.entries if e.get("check") == "manual_step"][-1]
    assert entry["plan_step"] == 2 and entry["instruction"] == "Top up the diluent trough by hand"


async def test_the_dashboard_shows_a_waiting_manual_step_with_its_instruction(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await ready(lab)
    step = await lab.call("request_manual_step", instruction=SPIN, labware=["plate"], duration_min=1)
    shown = replay(Path(lab.server.lab.runlog.path))["agent"]["checks_waiting"]
    assert [(c["check_id"], c["check"]) for c in shown] == [(step["check_id"], "manual_step")]
    assert shown[0]["what"] == f"by hand: {SPIN} (about 1 min) · labware: plate (plate_car site 0)"
    await lab.call("record_verdict", check_id=step["check_id"], verdict="pass", note=step["ask_person"])
    assert replay(Path(lab.server.lab.runlog.path))["agent"]["checks_waiting"] == []


async def test_a_manual_step_that_discards_liquid_empties_those_wells_in_the_twin_once_done(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await ready(lab)
    await lab.call("aspirate", device=PIP, targets=["diluent"], volumes=100)
    await lab.call("dispense", device=PIP, targets=["plate:A1:H1"], volumes=100)
    assert await lab.refused("request_manual_step", instruction=SPIN, empties=["nowhere:A1"]) == "unknown_target"
    step = await lab.call("request_manual_step", instruction="Spin the plate and discard the flow-through",
                          empties=["plate:A1:H1"])
    assert step["empties_containers"][:2] == ["plate_well_A1", "plate_well_B1"] and len(step["empties_containers"]) == 8
    still = await lab.call("verify", check="volumes_match", expected={"plate:A1:H1": 100})
    assert still["verdict"] == "pass"  # nothing changes before the person has done it
    await lab.call("record_verdict", check_id=step["check_id"], verdict="pass", note=step["ask_person"])
    emptied = await lab.call("verify", check="volumes_match", expected={"plate:A1:H1": 0})
    assert emptied["verdict"] == "pass"
    logged = [e for e in lab.server.lab.runlog.entries if e.get("type") == "twin"]
    assert logged[-1]["event"] == "emptied" and logged[-1]["removed_ul"]["plate_well_A1"] == 100


async def test_a_failed_manual_step_empties_nothing(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await ready(lab)
    await lab.call("aspirate", device=PIP, targets=["diluent"], volumes=100)
    await lab.call("dispense", device=PIP, targets=["plate:A1:H1"], volumes=100)
    step = await lab.call("request_manual_step", instruction="Spin and discard", empties=["plate:A1:H1"])
    await lab.call("record_verdict", check_id=step["check_id"], verdict="fail", note="could not")
    kept = await lab.call("verify", check="volumes_match", expected={"plate:A1:H1": 100})
    assert kept["verdict"] == "pass"
