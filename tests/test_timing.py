"""How long a run took and where the time went: the summary of a run log, and where it shows."""

import json
import os
from pathlib import Path

import pytest

from ahc.examples import STARLET_LAYOUT
from ahc.timing import run_timing
from ahc.viz.dashboard import PAGE, replay
from conftest import give_reference, robot_lab

PLAN = ["Load the layout", "Check the deck", "Move liquid", "Report"]


def at(second: float) -> str:
  minute, second = divmod(second, 60)
  return f"2026-10-29T10:{int(minute):02d}:{int(second):02d}" + (f".{round(second % 1 * 1000):03d}" if second % 1 else "")


def call(op, end, duration=None, step=None, **more):
  """A tool call entry as the server logs it: t is when the call ended, t_start and duration_s since timing."""
  entry = {"t": at(end), "op": op, "status": "ok", **more}
  if step:
    entry["plan_step"] = step
  if duration is not None:
    entry["t_start"] = at(end - duration)
    entry["duration_s"] = duration
  return entry


def check(check_id, verdict, end, step=None, duration=None):
  entry = {"t": at(end), "type": "verification", "check_id": check_id, "check": "tips_mounted", "verdict": verdict}
  if step:
    entry["plan_step"] = step
  if duration is not None:
    entry.update(t_start=at(end - duration), duration_s=duration)
  return entry


def run(*entries):
  return [{"t": at(0), "type": "run"}, *entries]


def test_steps_and_the_three_way_split_of_a_timed_run():
  log = run({"t": at(1), "type": "agent", "decision": "Plan", "plan": PLAN},
            call("load_layout", 11, duration=8, step=1),
            check("c1", "pending", 14, step=2, duration=0.5),  # the person is asked at 14
            check("c1", "pass", 44, duration=0.1),  # their verdict reaches the server at 43.9; it names no step
            call("pick_up_tips", 50, duration=2.5, step=3),
            call("aspirate", 53, duration=2, step=3),
            {"t": at(60), "type": "agent", "decision": "Done", "plan_done": True})
  t = run_timing(log)
  assert t["run"] == {"start": at(0), "end": at(60), "total_s": 60.0, "done": True}
  steps = {s["step"]: s for s in t["steps"]}
  assert [s["step"] for s in t["steps"]] == [1, 2, 3, 4]
  assert steps[1]["text"] == "Load the layout" and steps[1]["hardware_calls"] == 1
  assert (steps[1]["start"], steps[1]["end"], steps[1]["duration_s"]) == (at(3), at(11), 8.0)  # from the call's start
  assert (steps[2]["start"], steps[2]["end"], steps[2]["duration_s"]) == (at(13.5), at(44), 30.5)  # the verdict counts
  assert steps[3]["hardware_calls"] == 2 and steps[3]["duration_s"] == 5.5  # 47.5 to 53
  assert (steps[4]["start"], steps[4]["duration_s"]) == (at(60), 0.0)  # the final report is the last step
  split = t["split"]
  assert split["robot_s"] == 12.5 and split["robot_measured"]  # 8 + 2.5 + 2
  assert split["others"]["person_checks_s"] == pytest.approx(29.9, abs=0.001)  # pending at 14 until the verdict began at 43.9
  assert split["others_s"] == pytest.approx(29.9, abs=0.001)
  assert split["agent_s"] == pytest.approx(60 - 12.5 - 29.9, abs=0.001)
  assert t["counts"] == {"calls": 3, "refused": 0, "checks": 1, "estops": 0} and t["precise"]


def test_an_old_log_without_durations_still_summarizes_from_t():
  log = run({"t": at(1), "type": "agent", "decision": "Plan", "plan": PLAN},
            call("load_layout", 10, step=1),
            check("c1", "pending", 12, step=2),
            check("c1", "pass", 40),
            call("aspirate", 50, step=3),
            call("dispense", 58, step=3),
            {"t": at(61), "type": "agent", "decision": "Done", "plan_done": True})
  t = run_timing(log)
  assert not t["precise"] and t["run"]["total_s"] == 61.0
  assert [(s["step"], s["duration_s"]) for s in t["steps"]] == [(1, 0.0), (2, 28.0), (3, 8.0), (4, 0.0)]
  assert t["split"]["robot_s"] is None and not t["split"]["robot_measured"]  # nothing was timed: not a guess
  assert t["split"]["others_s"] == 28.0 and t["split"]["agent_s"] == 33.0  # the robot's time is in the agent's, unmeasured


def test_waits_for_the_person_are_counted_once_and_never_beyond_the_run():
  log = run({"t": at(1), "type": "agent", "decision": "Plan", "plan": PLAN},
            check("a", "pending", 10, step=2), check("b", "pending", 12, step=2),  # two checks open together
            check("a", "superseded", 12), check("b", "pass", 30),
            check("c", "pending", 40, step=3),  # still waiting when the log ends
            {"t": at(50), "type": "estop", "state": "engaged"})
  t = run_timing(log)
  assert t["run"]["done"] is False and t["run"]["end"] == at(50)
  assert t["split"]["others"]["person_checks_s"] == 20.0 + 10.0  # 10 to 30 (one wait, not two), then 40 to the end
  assert t["split"]["others_s"] == 30.0 and t["split"]["agent_s"] == 20.0 and t["counts"]["estops"] == 1 and t["counts"]["checks"] == 3


def test_the_go_ahead_wait_runs_from_the_marker_to_confirm_config():
  log = run({"t": at(2), "type": "wait", "for": "person", "what": "confirm_config"},
            call("confirm_config", 32, duration=0.2),  # the call began at 31.8
            {"t": at(35), "type": "agent", "decision": "Plan", "plan": PLAN})
  t = run_timing(log)
  assert t["run"]["start"] == at(0) and t["split"]["others"]["go_ahead_s"] == 29.8 and t["split"]["others_s"] == 29.8
  assert t["split"]["robot_s"] is None  # confirm_config is not motion


def test_a_refused_call_is_not_robot_time_and_a_half_done_one_is():
  log = run({"t": at(1), "type": "agent", "decision": "Plan", "plan": PLAN},
            call("aspirate", 10, duration=0.01, step=3, status="refused", error="gate_pending"),
            call("dispense", 20, duration=4, step=3, status="refused", error="emergency_stop", partial={"done": ["a"]}))
  t = run_timing(log)
  assert t["split"]["robot_s"] == 4.0 and t["counts"] == {"calls": 1, "refused": 2, "checks": 0, "estops": 0}


def test_a_new_plan_starts_the_step_table_again_and_nothing_breaks_on_an_empty_log():
  log = run({"t": at(1), "type": "agent", "decision": "Plan", "plan": PLAN}, call("load_layout", 5, step=1),
            {"t": at(6), "type": "agent", "decision": "Again", "plan": ["Only step"]}, call("mix", 9, step=1))
  assert [(s["step"], s["text"], s["start"]) for s in run_timing(log)["steps"]] == [(1, "Only step", at(9))]
  assert run_timing([])["run"] is None and run_timing([{"type": "x"}])["steps"] == []


# -- through the server --------------------------------------------------------------------------

DEV = "starlet.pip"


@pytest.mark.anyio
async def test_calls_are_logged_with_a_start_and_a_length_and_the_reports_carry_timing(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim", auto_plan=False) as lab:
    give_reference(lab)
    await lab.call("report_decision", decision="Plan", plan=["Load", "Check", "Pick up", "Report"])
    await lab.call("load_layout", layout=STARLET_LAYOUT, plan_step=1)
    await lab.call("verify", check="deck_matches_layout", plan_step=2)
    await lab.call("pick_up_tips", device=DEV, plan_step=3)
    live = (await lab.call("get_run"))["timing"]
    assert [s["step"] for s in live["steps"]] == [1, 2, 3] and live["run"]["done"] is False
    done = (await lab.call("report_decision", decision="All done", plan_done=True))["timing"]
    path = Path(lab.server.lab.runlog.path)
  assert done["run"]["done"] and [s["step"] for s in done["steps"]] == [1, 2, 3, 4]
  assert done["split"]["robot_measured"] and done["split"]["robot_s"] > 0 and done["precise"]
  parts = done["split"]["robot_s"] + done["split"]["others_s"] + done["split"]["agent_s"]
  assert parts == pytest.approx(done["run"]["total_s"], abs=1.0)  # `t` is to the second, durations are not
  logged = [json.loads(line) for line in path.read_text().splitlines()]
  calls = [e for e in logged if e.get("op") in ("load_layout", "pick_up_tips")]
  assert len(calls) == 2
  for e in calls:
    assert len(e["t"]) == 19 and e["t_start"][:19] <= e["t"] and e["duration_s"] >= 0  # t is as it was
    assert round(e["duration_s"], 3) == e["duration_s"] and len(e["t_start"]) == 23  # milliseconds
  assert "duration_s" not in [e for e in logged if e.get("type") == "agent"][-1]  # a report is not a timed call
  assert run_timing(logged)["steps"] == done["steps"]  # the log alone gives the server's numbers


@pytest.mark.anyio
async def test_the_go_ahead_wait_of_a_real_robot_is_logged(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    entries = lab.server.lab.runlog.written
    kinds = [(e.get("type") or e.get("op")) for e in entries]
    assert kinds.index("wait") < kinds.index("confirm_config")  # the wait is on record before the person answers
    assert kinds.count("wait") == 1
    assert "others_s" in (await lab.call("get_run"))["timing"]["split"]


# -- the dashboard ---------------------------------------------------------------------------------


def test_the_dashboard_replay_carries_the_timing_and_the_page_shows_it(tmp_path):
  log = tmp_path / ".ahc" / "runs" / "r.jsonl"
  log.parent.mkdir(parents=True)
  entries = run({"t": at(1), "type": "agent", "decision": "Plan", "plan": PLAN},
                call("load_layout", 11, duration=8, step=1, effective={"saved_as": "deck"}),
                {"t": at(12), "type": "agent", "decision": "Checking", "current_step": 2, "waiting_for": "person"})
  log.write_text("".join(json.dumps(e) + "\n" for e in entries))
  state = replay(log)
  timing = state["timing"]
  assert timing["run"]["start"] == at(0) and timing["run"]["done"] is False
  assert [(s["step"], s["duration_s"]) for s in timing["steps"]] == [(1, 8.0), (2, 0.0)]
  assert timing["split"]["robot_s"] == 8.0 and "as_of" in timing
  assert timing["split"]["others"].keys() == {"person_checks_s", "go_ahead_s", "estop_engaged_s", "after_release_s"}
  entries.append({"t": at(30), "type": "agent", "decision": "Done", "plan_done": True})
  log.write_text("".join(json.dumps(e) + "\n" for e in entries))
  finished = replay(log)
  assert finished["agent"]["plan_done"] and finished["timing"]["run"]["done"] and finished["timing"]["run"]["total_s"] == 30.0
  # The page ticks the run and the step in progress from the server's as_of, and sums up a finished run.
  for piece in ('data-clock', 'timesum', 'as_of', 'Robot', 'Agent', 'Others', 'othersOpen', 'Recovery after a stop release'):
    assert piece in PAGE
  assert replay(None, ahc_dir=tmp_path / ".ahc")["timing"]["run"] is None  # no run log yet: nothing to time


def test_an_emergency_stop_and_the_recovery_after_it_are_others_time():
  log = run({"t": at(1), "type": "agent", "decision": "Plan", "plan": PLAN},
            {"t": at(10), "type": "estop", "state": "engaged", "id": "s1"},
            {"t": at(10), "type": "estop", "state": "engaged", "id": "s1"},  # a second server logs the same stop
            check("c", "pending", 12, step=2),  # a check open during the stop: the time counts once
            {"t": at(20), "type": "estop", "state": "released", "id": "s1"},
            check("c", "pass", 25),
            {"t": at(40), "type": "estop", "state": "engaged", "id": "s2"},  # never released: until the log ends
            {"t": at(50), "type": "agent", "decision": "Waiting"})
  t = run_timing(log)
  assert t["split"]["others"] == {"person_checks_s": 13.0, "go_ahead_s": 0.0, "estop_engaged_s": 2.0 + 10.0,
                                  "after_release_s": 0.0}  # 12-25 is the check, 10-12 and 40-50 the stops
  assert t["split"]["others_s"] == 25.0 and t["split"]["agent_s"] == 25.0 and t["counts"]["estops"] == 2


def test_the_recovery_after_a_release_lasts_until_the_next_check_verdict_or_motion_call():
  def stopped(*then):
    return run({"t": at(1), "type": "agent", "decision": "Plan", "plan": PLAN},
               {"t": at(10), "type": "estop", "state": "engaged", "id": "s"},
               {"t": at(15), "type": "estop", "state": "released", "id": "s"},
               {"t": at(15), "type": "gate", "state": "pending", "reason": "emergency stop released: check the deck again"},
               *then)
  moved = run_timing(stopped(call("pick_up_tips", 45, duration=5, step=3)))  # the call began at 40
  assert moved["split"]["others"]["estop_engaged_s"] == 5.0 and moved["split"]["others"]["after_release_s"] == 25.0
  assert moved["split"]["robot_s"] == 5.0 and moved["split"]["agent_s"] == 45.0 - 5.0 - 30.0
  asked = run_timing(stopped(check("c", "pending", 30, step=3), call("aspirate", 60, duration=2, step=3)))
  assert asked["split"]["others"]["after_release_s"] == 15.0  # to the check; then the check itself is waiting
  assert asked["split"]["others"]["person_checks_s"] == 28.0  # pending at 30 until the aspirate began at 58
  unended = run_timing(stopped({"t": at(35), "type": "agent", "decision": "Tips removed"}))
  assert unended["split"]["others"]["after_release_s"] == 20.0  # no check or call since: until the log ends


# The first real run's log names a workspace and a robot, so it stays out of the repo: the test reads it from here.
REAL_LOG = os.environ.get("AHC_TEST_REAL_LOG")


@pytest.mark.skipif(not REAL_LOG or not Path(REAL_LOG).is_file(), reason="AHC_TEST_REAL_LOG names no run log")
def test_the_first_real_runs_split():
  t = run_timing([json.loads(line) for line in Path(REAL_LOG).read_text().splitlines()])  # read only
  assert t["run"]["total_s"] == 1744.0 and t["counts"]["estops"] == 1 and t["counts"]["checks"] == 6
  assert t["split"]["others"] == {"person_checks_s": 218.0, "go_ahead_s": 0.0, "estop_engaged_s": 12.0,
                                  "after_release_s": 219.0}  # 18:30:04 stop, 18:30:16 release, 18:33:55 tips check
  assert t["split"]["others_s"] == 449.0 and t["split"]["robot_s"] is None and t["split"]["agent_s"] == 1744.0 - 449.0
