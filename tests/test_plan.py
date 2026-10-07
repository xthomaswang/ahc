"""The plan first, then every call names its step: what the person sees ticked, and what is refused."""

import pytest

from ahc.core.errors import LabError
from ahc.plan import PlanProgress

STEPS = ["Load the layout", "Check the deck", "Pick up tips", "Dilute", "Drop tips", "Report the volumes"]


def test_steps_tick_as_the_agent_moves_on():
  p = PlanProgress()
  p.report(plan=STEPS)
  assert p.statuses() == ["next"] * 6
  for step in (1, 2, 3, 3, 5):  # step 4 passed over without a call
    p.enter(step)
  assert p.statuses() == ["done", "done", "done", "skipped", "now", "next"]
  p.report(plan_done=True)  # the final report is the last step
  assert p.statuses() == ["done", "done", "done", "skipped", "done", "done"] and p.done
  p.enter(5)  # it went on working after all
  assert not p.done and p.statuses()[4] == "now"
  p.report(plan=["Start over"])  # a new plan replaces the old one and its progress
  assert p.statuses() == ["next"] and p.current is None


def test_calls_before_the_plan_or_outside_it_are_refused():
  p = PlanProgress()
  with pytest.raises(LabError) as exc:
    p.check(1)
  assert exc.value.code == "plan_required"
  p.report(plan=STEPS)
  for step in (0, 7):
    with pytest.raises(LabError) as exc:
      p.check(step)
    assert exc.value.code == "bad_plan_step"
  p.check(6)


def test_the_run_log_replays_to_the_same_progress():
  log = [{"type": "agent", "t": "2026-10-02T12:00:00", "decision": "Dilute", "plan": STEPS},
         {"op": "load_layout", "status": "ok", "plan_step": 1},
         {"type": "verification", "check": "deck_matches_layout", "plan_step": 2},
         {"op": "pick_up_tips", "status": "ok", "plan_step": 3},
         {"op": "aspirate", "status": "refused", "error": "gate_pending"},  # refused before its step began
         {"type": "agent", "decision": "Waiting for the person", "current_step": 4}]
  p = PlanProgress()
  for entry in log:
    p.apply(entry)
  assert p.statuses() == ["done", "done", "done", "now", "next", "next"] and p.at == "2026-10-02T12:00:00"


# -- through the server ------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from ahc.examples import STARLET_LAYOUT  # noqa: E402
from ahc.viz.dashboard import replay  # noqa: E402
from conftest import give_reference  # noqa: E402

DEV = "starlet.pip"
DILUTION = ["Load the example layout", "Check the deck", "Pick up tips", "Add diluent to column 2",
            "Drop tips", "Report the volumes"]


@pytest.mark.anyio
async def test_nothing_is_loaded_or_moved_before_the_plan(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", auto_plan=False) as lab:
    await lab.call("lab_overview")  # reading comes first: the plan is made from it
    await lab.call("find_layout_references")
    assert "report your plan" in (await lab.call("lab_overview"))["next"].lower()
    for tool, args in (("load_layout", {"layout": STARLET_LAYOUT}), ("pick_up_tips", {"device": DEV})):
      assert await lab.refused(tool, plan_step=1, **args) == "plan_required"
    assert await lab.refused("report_decision", decision="Done", plan_done=True) == "no_plan"
    assert await lab.refused("report_decision", decision="Plan", plan=[]) == "bad_plan"
    first = await lab.call("report_decision", decision="Dilute column 2", plan=DILUTION)
    assert "plan_step" in first["next"] and "plan_done=true" in first["next"]
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT, plan_step=7) == "bad_plan_step"
    assert await lab.refused("report_decision", decision="Step", current_step=9) == "bad_step"
    await lab.call("load_layout", layout=STARLET_LAYOUT, plan_step=1)


@pytest.mark.anyio
async def test_steps_tick_from_the_calls_and_the_dashboard_agrees(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", auto_plan=False) as lab:
    give_reference(lab)  # the simulator judges the deck here
    await lab.call("report_decision", decision="Dilute column 2", plan=DILUTION)
    await lab.call("load_layout", layout=STARLET_LAYOUT, plan_step=1)
    assert await lab.refused("pick_up_tips", device=DEV, plan_step=3) == "gate_pending"  # refused: step 3 not begun
    await lab.call("verify", check="deck_matches_layout", plan_step=2)
    assert lab.server.lab.plan.statuses() == ["done", "now", "next", "next", "next", "next"]
    await lab.call("pick_up_tips", device=DEV, plan_step=3)
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100, plan_step=4)
    await lab.call("dispense", device=DEV, targets=["plate:A2:H2"], volumes=100, plan_step=4)
    await lab.call("report_decision", decision="Asking the person to check the plate", current_step=4,
                   waiting_for="person")
    last = await lab.call("drop_tips", device=DEV, plan_step=6)  # step 5 passed over, straight to the last
    assert "plan_done=true" in last["plan"]
    assert lab.server.lab.plan.statuses() == ["done", "done", "done", "done", "skipped", "now"]
    await lab.call("report_decision", decision="Column 2 holds 100 uL per well", plan_done=True)
    server = lab.server.lab.plan.public()
    entries = lab.server.lab.runlog.entries
  assert server["status"] == ["done", "done", "done", "done", "skipped", "done"] and server["done"]
  assert [e.get("plan_step") for e in entries if e.get("op") == "aspirate"] == [4]
  refused = [e for e in entries if e.get("error") == "gate_pending"][0]
  assert "plan_step" not in refused  # a refused call never started its step
  shown = replay(Path(lab.server.lab.runlog.path))["agent"]
  assert shown["plan"] == DILUTION and shown["steps"] == server["status"] and shown["plan_done"]


@pytest.mark.anyio
async def test_the_last_step_reminder_comes_once(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", auto_plan=False) as lab:
    give_reference(lab)
    await lab.call("report_decision", decision="Two steps", plan=["Load and check", "Tips on and off"])
    await lab.call("load_layout", layout=STARLET_LAYOUT, plan_step=1)
    await lab.call("verify", check="deck_matches_layout", plan_step=1)
    assert "plan" in await lab.call("pick_up_tips", device=DEV, plan_step=2)
    assert "plan" not in await lab.call("drop_tips", device=DEV, plan_step=2)


@pytest.mark.anyio
async def test_a_new_run_log_carries_the_plan(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", auto_plan=False) as lab:
    give_reference(lab)
    await lab.call("report_decision", decision="Dilute", plan=DILUTION)
    await lab.call("load_layout", layout=STARLET_LAYOUT, plan_step=1)
    await lab.call("verify", check="deck_matches_layout", plan_step=2)
    first = Path(lab.server.lab.runlog.path)
    lab.server.lab.runlog = None  # what a device change does: the next entry starts a new log
    await lab.call("get_state")
    await lab.call("pick_up_tips", device=DEV, plan_step=3)
    second = Path(lab.server.lab.runlog.path)
  assert first != second
  assert replay(second)["agent"]["steps"] == ["done", "done", "now", "next", "next", "next"]
