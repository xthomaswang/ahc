"""What the run dashboard shows: the agent's reported decisions, the task's config and layout, the gate."""

import json
from pathlib import Path

import pytest

from ahc.examples import STARLET_LAYOUT
from ahc.viz.dashboard import current_state, replay
from ahc.workspace.store import Workspace
from conftest import load_verified

pytestmark = pytest.mark.anyio

PLAN = ["Load the deck", "Check the deck", "Dilute"]


def log_of(lab) -> Path:
  return Path(lab.server.lab.runlog.path)


async def test_reported_decisions_reach_the_dashboard(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    first = await lab.call("report_decision", decision="Run a 1.5-fold dilution", why="the person asked", plan=PLAN,
                           current_step=1)
    assert "plan_step" in first["next"]  # from now on every call names its step
    assert sorted(p.name for p in (tmp_path / ".ahc").iterdir()) == ["runs"]  # a note writes only the run log
    await load_verified(lab, layout=STARLET_LAYOUT, name="deck")
    await lab.call("report_decision", decision="Waiting for the person to check the plate", current_step=2,
                   waiting_for="person")
    assert "next" not in await lab.call("report_decision", decision="Dilute", current_step=3)  # the last step
    entry = [e for e in lab.server.lab.runlog.entries if e.get("type") == "agent"][0]
    assert entry == {"type": "agent", "decision": "Run a 1.5-fold dilution", "why": "the person asked",
                     "plan": PLAN, "current_step": 1}
    state = replay(log_of(lab))
  agent = state["agent"]
  assert agent["history"][-2]["decision"].startswith("Waiting") and agent["history"][-2]["waiting_for"] == "person"
  assert agent["plan"] == PLAN and agent["current_step"] == 3 and len(agent["history"]) == 3
  assert agent["steps"] == ["done", "done", "now"] and agent["state"] == "working"
  assert state["config"]["status"] == "confirmed" and state["config"]["devices"][0]["model"] == "hamilton.starlet"
  assert state["layout"]["name"] == "deck" and state["layout"]["labware"]["plate"] == "Cor_96_wellplate_360ul_Fb"
  assert state["layout"]["needs_person"] and state["layout"]["confirmed_by"] == "the person"  # a fresh folder
  assert state["gate"]["state"] == "passed"


async def test_an_empty_decision_is_refused(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    assert await lab.refused("report_decision", decision="  ") == "bad_decision"
    assert await lab.refused("report_decision", decision="x", plan=["a", ""]) == "bad_plan"


def test_a_run_without_config_or_decisions_still_replays(tmp_path):
  runs = tmp_path / ".ahc" / "runs"
  runs.mkdir(parents=True)
  log = runs / "r.jsonl"
  log.write_text(json.dumps({"type": "run", "run_id": "r", "device": None, "backend": None}) + "\n")
  state = replay(log)
  assert state["config"] is None and state["layout"] is None and state["gate"] is None
  agent = state["agent"]
  assert agent["current"] is None and agent["plan"] is None and agent["history"] == [] and not agent["live"]
  assert agent["state"] == "idle"
  (tmp_path / ".ahc" / "config.yaml").write_text("devices: [\n")  # broken by hand
  assert replay(log)["config"]["status"] == "invalid"


def test_the_live_feed_and_reported_decisions_merge(tmp_path):
  runs = tmp_path / ".ahc" / "runs"
  runs.mkdir(parents=True)
  log = runs / "r.jsonl"
  log.write_text("".join(json.dumps(e) + "\n" for e in [
    {"type": "run", "run_id": "r"},
    {"type": "agent", "t": "2026-10-02T11:01:32", "decision": "Plan the dilution", "plan": PLAN},
    {"op": "pick_up_tips", "device": "starlet.pip", "status": "ok", "t": "2026-10-02T11:01:32", "plan_step": 2},
    {"op": "aspirate", "device": "starlet.pip", "status": "ok", "t": "2026-10-02T11:01:41", "plan_step": 3,
     "basic": {"targets": ["stock"], "volumes": 200}, "effective": {"containers": ["stock"], "volumes_ul": [200]}}]))
  feed = tmp_path / ".ahc" / "agent-feed.jsonl"
  entries = [
    {"kind": "request", "t": "2026-10-02T11:01:00", "text": "Run a 2-fold dilution", "session": "s"},
    {"kind": "said", "t": "2026-10-02T11:01:40", "text": "Diluent is in; adding the stock next.", "session": "s"},
    {"kind": "doing", "t": "2026-10-02T11:01:41", "tool": "aspirate", "brief": "stock · 200", "session": "s"}]
  feed.write_text("".join(json.dumps(e) + "\n" for e in entries))
  agent = replay(log)["agent"]
  assert agent["live"] and agent["request"]["text"] == "Run a 2-fold dilution"
  assert agent["now"]["text"].startswith("Diluent is in") and agent["doing"]["tool"] == "aspirate"
  assert not agent["finished"] and agent["plan_at"] == "2026-10-02T11:01:32" and agent["state"] == "working"
  assert agent["current_step"] == 3 and agent["steps"] == ["skipped", "done", "now"]  # step 1 had no call
  with feed.open("a") as f:
    f.write(json.dumps({"kind": "finished", "t": "2026-10-02T11:02:10", "text": "Done.", "session": "s"}) + "\n")
  agent = replay(log)["agent"]
  assert agent["finished"] and agent["state"] == "ended"  # the turn ended, the plan was not reported done
  with log.open("a") as f:
    f.write(json.dumps({"type": "agent", "t": "2026-10-02T11:02:20", "decision": "Diluted", "plan_done": True}) + "\n")
  agent = replay(log)["agent"]
  assert agent["state"] == "finished" and agent["plan_done"] and agent["steps"] == ["skipped", "done", "done"]


def test_a_layout_waiting_for_the_person_shows_as_waiting(tmp_path):
  runs = tmp_path / ".ahc" / "runs"
  runs.mkdir(parents=True)
  log = runs / "r.jsonl"
  log.write_text("".join(json.dumps(e) + "\n" for e in [
    {"type": "run", "run_id": "r"},
    {"type": "agent", "t": "2026-10-02T11:00:00", "decision": "Dilute", "plan": PLAN},
    {"op": "load_layout", "status": "ok", "t": "2026-10-02T11:00:05", "plan_step": 1,
     "basic": {"layout": {"carriers": []}}, "effective": {"saved_as": "layout-1", "needs_person": True}},
    {"type": "gate", "t": "2026-10-02T11:00:05", "layout": "layout-1", "state": "pending", "needs_person": True,
     "reason": "deck check pending"}]))
  state = replay(log)
  assert state["agent"]["state"] == "waiting" and state["agent"]["layout_waits"]
  assert state["gate"]["needs_person"] and state["layout"]["needs_person"] and "confirmed_by" not in state["layout"]


def test_the_agent_shows_before_any_run(tmp_path):
  ws = Workspace(tmp_path)
  assert current_state(ws, None) is None  # nothing yet: the page waits
  ws.dir.mkdir()
  (ws.dir / "agent-feed.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
    {"kind": "request", "t": "2026-10-02T11:00:00", "text": "Dilute on the STARlet", "session": "s"},
    {"kind": "doing", "t": "2026-10-02T11:00:30", "tool": "lab_overview", "brief": "", "session": "s"}]))
  state = current_state(ws, None)  # the hook wrote the feed at the first AHC call; the server has written nothing
  assert state["run"]["run_id"] is None and state["log"] is None and state["workspace"] == str(ws.root)
  assert state["agent"]["request"]["text"] == "Dilute on the STARlet" and state["agent"]["doing"]["tool"] == "lab_overview"
  assert state["agent"]["state"] == "working"
  assert state["config"] is None and state["events"] == [] and state["plates"] == []
