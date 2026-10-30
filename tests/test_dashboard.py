"""What the run dashboard shows: the agent's reported decisions, the task's config and layout, the gate."""

import http.client
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from ahc.examples import STARLET_LAYOUT
from ahc.viz.dashboard import (_expand, current_state, latest_log, log_folder, open_dashboard, replay, serve,
                               whoami)
from ahc.workspace import estop
from ahc.workspace.store import Workspace
from ackenv import ROBOT, dead_pid, put_ack
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


async def test_returned_tips_show_as_returned_never_as_fresh(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device="starlet.pip")
    await lab.call("drop_tips", device="starlet.pip", mode="return")
    picked = await lab.call("pick_up_tips", device="starlet.pip")  # next fresh: the server skips returned tips
    twin = await lab.call("get_state")
    state = replay(log_of(lab))
  assert twin["tips_left"]["tips"] == 80 and twin["tips_returned"] == {"tips": 8}  # what the agent reads agrees
  assert picked["effective"]["where"] == "tips: column 2 (A2-H2)"
  rack = next(r for r in state["tip_racks"] if r["name"] == "tips")
  first, second = [f"{r}1" for r in "ABCDEFGH"], [f"{r}2" for r in "ABCDEFGH"]
  assert sorted(rack["used"]) == sorted(first + second)  # 80 of 96 left, as the server would pick them
  assert rack["returned"] == first  # back in their spots after use: shown as returned, never as fresh


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


# -- the page's server: which folder it stops, and only for its own page ------------------------------

def _http(port: int, path: str, method: str = "GET", body=None, **headers):
  """status, headers, body of one request; `Host` can be set to what a rebinding page would send."""
  conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
  try:
    conn.request(method, path, body=None if body is None else json.dumps(body).encode(),
                 headers={"Content-Type": "application/json", **{k.replace("_", "-"): v for k, v in headers.items()}})
    response = conn.getresponse()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
  finally:
    conn.close()


@pytest.fixture
def dashboard(tmp_path):
  """A dashboard for a task folder on a free port: (workspace, port, page token)."""
  ws = Workspace(tmp_path / "task")
  ws.root.mkdir()
  httpd = serve(ws, None, 0, open_browser=False)
  port = httpd.server_address[1]
  page = _http(port, "/")[2].decode()
  token = re.search(r'name="ahc-token" content="([0-9a-f]+)"', page).group(1)
  try:
    yield ws, port, token
  finally:
    httpd.shutdown()


def test_the_page_names_the_folder_it_stops(dashboard):
  ws, port, token = dashboard
  status, _, body = _http(port, "/whoami")
  assert status == 200 and json.loads(body) == {"app": "ahc-dashboard", "folder": str(ws.root), "pid": os.getpid(),
                                                "view_only": False}
  page = _http(port, "/")[2].decode()
  assert f"<title>AHC · {ws.root}</title>" in page  # the absolute folder, in the tab and in the stop bar
  assert f"Task folder <code>{ws.root}</code>" in page and len(token) == 32
  assert whoami(port)["folder"] == str(ws.root)
  stop = json.loads(_http(port, "/estop")[2])
  assert stop == {"engaged": False, "folder": str(ws.root)}


def test_the_page_answers_only_its_own_name_and_is_never_framed(dashboard):
  ws, port, _ = dashboard
  for path in ("/", "/state", "/estop", "/whoami"):
    assert _http(port, path, Host=f"rebind.attacker.example:{port}")[0] == 403, path  # DNS rebinding
    assert _http(port, path, Origin="http://evil.example")[0] == 403, path
    assert _http(port, path, Host=f"localhost:{port}")[0] in (200, 204), path
  for path in ("/", "/state", "/estop", "/nothing"):
    _, headers, _ = _http(port, path)
    assert headers["x-frame-options"] == "DENY" and headers["content-security-policy"] == "frame-ancestors 'none'"
    assert headers["cross-origin-resource-policy"] == "same-origin"


def test_the_dashboard_button_needs_its_own_page(dashboard):
  ws, port, token = dashboard
  mine = {"X_AHC_Control": "1", "Origin": f"http://127.0.0.1:{port}"}  # built from the port bound (here: 0 asked)
  assert _http(port, "/estop", "POST", {})[0] == 403  # no header: a cross-site form
  assert _http(port, "/estop", "POST", {}, **{**mine, "Origin": "http://evil.example"})[0] == 403
  assert _http(port, "/estop", "POST", {}, **mine, Host=f"rebind.attacker.example:{port}")[0] == 403
  status, _, body = _http(port, "/estop", "POST", {"reason": "test"}, **mine)
  data = json.loads(body)
  assert status == 200 and data["engaged"] and data["by"] == "dashboard" and data["folder"] == str(ws.root)
  assert _http(port, "/estop", "POST", {}, X_AHC_Control="1")[0] == 200  # pressing needs no page: a script may stop
  # Releasing does: this page's origin and its token, then the person's confirmation.
  assert _http(port, "/estop/release", "POST", {"confirm": True}, X_AHC_Control="1")[0] == 403
  assert _http(port, "/estop/release", "POST", {"confirm": True}, **mine)[0] == 403
  assert _http(port, "/estop/release", "POST", {"confirm": True}, **mine, X_AHC_Token="0" * 32)[0] == 403
  assert _http(port, "/estop/release", "POST", {}, **mine, X_AHC_Token=token)[0] == 400  # not confirmed
  assert estop.read(ws)["engaged"]
  status, _, body = _http(port, "/estop/release", "POST", {"confirm": True}, **mine, X_AHC_Token=token)
  assert status == 200 and json.loads(body)["engaged"] is False and json.loads(body)["released_by"] == "dashboard"
  status, _, body = _http(port, "/estop/release", "POST", {"confirm": True}, **mine, X_AHC_Token=token)
  assert status == 409 and "nothing to release" in json.loads(body)["error"]


def test_the_stop_bar_says_what_each_server_did_with_the_stop(dashboard):
  ws, port, _ = dashboard
  pressed = estop.engage(ws, by="terminal")
  stop = json.loads(_http(port, "/estop")[2])
  assert stop["engaged"] and stop["servers"] == [] and stop["pressed_s"] >= 0  # said plainly on the page
  assert stop["verdict"]["state"] == "not_confirmed" and "no AHC server has acknowledged" in stop["verdict"]["text"]
  put_ack(ws, pressed, os.getpid(), "stopped", ROBOT, run_active=True)
  put_ack(ws, pressed, os.getpid(), "no_device", None, server="def456")
  put_ack(ws, pressed, dead_pid(), "pending", ROBOT, server="0a0a0a")
  stop = json.loads(_http(port, "/estop")[2])
  assert [s["halt"] for s in stop["servers"]] == ["stopped", "no_device", "pending"]
  assert [s["stale"] for s in stop["servers"]] == [False, False, True]
  assert "OT2-LAB" in stop["servers"][0]["line"] and "STALE" in stop["servers"][2]["line"]
  assert stop["verdict"]["state"] == "not_confirmed"  # a server that died while stopping leaves the question open
  estop.path(ws).write_text("stop")
  stop = json.loads(_http(port, "/estop")[2])
  assert stop["engaged"] and stop["unreadable"] == "not JSON" and stop["servers"] == []


def test_smoke_and_demo_never_claim_another_folders_dashboard(tmp_path, dashboard):
  other, port, _ = dashboard  # an ahc-dashboard left running for another folder holds the port
  ws = Workspace(tmp_path / "this-task")
  ws.root.mkdir()
  httpd, where = open_dashboard(ws, None, port, open_browser=False)
  try:
    assert httpd is not None and httpd.server_address[1] != port
    assert f"port {port} serves the dashboard of {other.root}" in where and "does NOT stop this run" in where
    assert whoami(httpd.server_address[1])["folder"] == str(ws.root)
  finally:
    httpd.shutdown()
  again, where = open_dashboard(other, None, port, open_browser=False)  # this folder's own: it follows the run
  assert again is None and "already running for this folder" in where


def test_a_log_names_its_folder(tmp_path):
  runs = tmp_path / "task" / ".ahc" / "runs"
  runs.mkdir(parents=True)
  (runs / "a.jsonl").write_text(json.dumps({"type": "run", "run_id": "a"}) + "\n")
  assert log_folder(runs / "a.jsonl") == str(tmp_path / "task")
  copied = tmp_path / "b.jsonl"
  copied.write_text(json.dumps({"type": "run", "run_id": "b", "workspace": "/lab/task"}) + "\n")
  assert log_folder(copied) == "/lab/task"
  (tmp_path / "c.jsonl").write_text("{}\n")
  assert log_folder(tmp_path / "c.jsonl") is None


def test_ahc_dashboard_refuses_ahcs_own_data_folder(tmp_path):
  home = tmp_path / "home"
  (home / ".ahc" / "sim").mkdir(parents=True)
  cli = Path(sys.executable).parent / "ahc-dashboard"
  out = subprocess.run([str(cli), "--workspace", str(home), "--no-browser", "--port", "0"], capture_output=True,
                       text=True, timeout=60, env={**os.environ, "AHC_HOME": str(home / ".ahc")})
  assert out.returncode != 0 and "AHC's own data folder" in out.stderr


def test_ahc_dashboard_never_takes_another_folders_port(tmp_path, dashboard):
  other, port, _ = dashboard  # a dashboard left running for another folder holds the port asked for
  task = tmp_path / "this-task"
  task.mkdir()
  cli = Path(sys.executable).parent / "ahc-dashboard"
  env = {**os.environ, "PYTHONUNBUFFERED": "1"}
  proc = subprocess.Popen([str(cli), "--workspace", str(task), "--no-browser", "--port", str(port)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
  try:
    line = proc.stdout.readline()
  finally:
    proc.kill()
    proc.communicate(timeout=30)
  assert f"port {port} serves the dashboard of {other.root}; its stop button does NOT stop this run" in line, line
  assert f"for {task}" in line and f"127.0.0.1:{port}/" not in line.split(" (")[0]
  same = subprocess.run([str(cli), "--workspace", str(other.root), "--no-browser", "--port", str(port)],
                        capture_output=True, text=True, timeout=60, env=env)  # this folder's own: shown, not doubled
  assert same.returncode == 0 and "already running for this folder" in same.stdout


def test_the_page_survives_robot_outcomes_nobody_knows_and_every_target_form(tmp_path):
  geometry = {"reagents": {"kind": "plate", "rows": 8, "cols": 12, "well_max_ul": 2000},
              "plate": {"kind": "plate", "rows": 8, "cols": 12, "well_max_ul": 360}}
  assert _expand("plate:col2", geometry) == [f"plate:{r}2" for r in "ABCDEFGH"]
  assert _expand("plate:ROWb", geometry) == [f"plate:B{c}" for c in range(1, 13)]
  assert _expand("plate:A1:B2") == ["plate:A1", "plate:B1", "plate:A2", "plate:B2"]
  assert _expand("plate:middle") == ["plate:middle"] and _expand("trough") == ["trough"]  # never raises
  liquids = {f"reagents_well_{r}2": 1000 for r in "ABCDEFGH"}
  log = _log(tmp_path / ".ahc" / "runs" / "r.jsonl",
             {"op": "load_layout", "status": "ok", "effective": {
               "saved_as": "deck", "kind": "slots", "labware": {}, "geometry": geometry, "liquids_ul": liquids,
               "aliases": {"water": "reagents:col2", "stock": "reagents:rowA", "odd": "reagents:middle"}}},
             {"op": "aspirate", "device": "ot2.right", "status": "refused", "error": "completion_unknown",
              "partial": "unknown", "basic": {"targets": ["water"], "volumes": 100},
              "message": "[completion_unknown] aspirate on ot2.right failed after 1 command(s) went to the robot"})
  state = replay(log)  # an unknown outcome on the robot (W2, and the stop cutting a call short) and C7 aliases
  water = next(c for c in state["containers"] if c["alias"] == "water")
  assert water["v"] == 8000 and water["max"] == 16000
  event = state["events"][-1]
  assert event["unknown"] and event["status"] == "refused" and state["summary"]["refused"] == 1


def test_the_view_only_page_has_no_controls_and_refuses_every_post(tmp_path):
  ws = Workspace(tmp_path / "task")
  ws.root.mkdir()
  control, viewer = serve(ws, None, 0, open_browser=False), serve(ws, None, 0, open_browser=False, view_only=True)
  try:
    cport, vport = control.server_address[1], viewer.server_address[1]
    assert cport != vport
    own = _http(cport, "/")[2].decode()
    page = _http(vport, "/")[2].decode()
    assert 'id="stopbtn"' in own and 'id="releasebtn"' in own
    for control_html in ('id="stopbtn"', 'id="releasebtn"', "EMERGENCY STOP</button>", "Release…</button>"):
      assert control_html not in page, control_html
    assert 'name="ahc-view" content="1"' in page and "view only</title>" in page
    token = re.search(r'name="ahc-token" content="([0-9a-f]*)"', page).group(1)
    assert token == ""  # no release token on the audience's page
    assert json.loads(_http(vport, "/whoami")[2])["view_only"] is True and whoami(vport)["view_only"] is True
    assert json.loads(_http(vport, "/estop")[2]) == {"engaged": False, "folder": str(ws.root)}  # it shows the state
    mine = {"Origin": f"http://127.0.0.1:{vport}", "X_AHC_Control": "1", "Content-Type": "application/json"}
    for path in ("/estop", "/estop/release", "/nothing"):
      for headers in (mine, {}):  # with every protection the control page's own fetch carries, and without
        assert _http(vport, path, "POST", {"confirm": True, "reason": "x"}, **headers)[0] in (403, 405), path
    assert not estop.path(ws).exists()
    # The same requests to the control page still stop and release (its protections are unchanged).
    pressed = estop.engage(ws, by="test")
    assert _http(vport, "/estop/release", "POST", {"confirm": True}, **mine, X_AHC_Token=token)[0] in (403, 405)
    assert estop.read(ws)["engaged"] and estop.read(ws)["id"] == pressed["id"]
    assert json.loads(_http(vport, "/estop")[2])["engaged"] is True  # the audience sees a stop
  finally:
    control.shutdown()
    viewer.shutdown()


def test_a_view_only_dashboard_is_its_own_second_server_next_to_the_control_one(tmp_path):
  ws = Workspace(tmp_path / "task")
  ws.root.mkdir()
  control, where = open_dashboard(ws, None, 0, open_browser=False)
  port = control.server_address[1]
  try:
    viewer, line = open_dashboard(ws, None, port, open_browser=False, view_only=True)  # that port holds the control page
    assert viewer is not None and viewer.server_address[1] != port, line
    assert whoami(viewer.server_address[1])["view_only"] is True and whoami(port)["view_only"] is False
    again, line = open_dashboard(ws, None, viewer.server_address[1], open_browser=False, view_only=True)
    assert again is None and "already running" in line  # its own kind is reused
    asked, line = open_dashboard(ws, None, viewer.server_address[1], open_browser=False)  # a control page is no view-only one
    assert asked is not None and asked.server_address[1] != viewer.server_address[1]
    asked.shutdown()
    viewer.shutdown()
  finally:
    control.shutdown()


async def test_open_dashboard_view_only_starts_a_second_dashboard(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    control = await lab.call("open_dashboard", open_browser=False)
    viewer = await lab.call("open_dashboard", open_browser=False, view_only=True)
    again = await lab.call("open_dashboard", open_browser=False, view_only=True)
    assert viewer["view_only"] is True and "how_to_stop" not in viewer and "how_to_stop" in control
    assert viewer["url"] != control["url"] and viewer["started"] and again["url"] == viewer["url"] and not again["started"]
    status, body = await anyio.to_thread.run_sync(_get, viewer["url"], "/")
    assert status == 200 and b'id="stopbtn"' not in body
    status, body = await anyio.to_thread.run_sync(_get, control["url"], "/")
    assert status == 200 and b'id="stopbtn"' in body


# -- which run the page follows, and when it waits for the person ------------------------------------

def _log(path: Path, *entries: dict) -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text("".join(json.dumps(e) + "\n" for e in [{"type": "run", "run_id": path.stem}, *entries]))
  return path


def test_the_page_follows_the_run_with_activity_not_the_newest_file(tmp_path):
  runs = tmp_path / ".ahc" / "runs"
  active = _log(runs / "a.jsonl", {"type": "agent", "decision": "Dilute", "plan": PLAN},
                {"op": "load_layout", "status": "ok", "t": "2026-10-22T10:00:00"})
  idle = _log(runs / "b.jsonl", {"type": "estop", "state": "engaged", "id": "x"},
              {"type": "estop", "state": "released", "id": "x"})  # another server on the folder, which only took the stop
  os.utime(active, (1_000_000, 1_000_000))
  assert latest_log(runs) == active
  assert current_state(Workspace(tmp_path), None)["log"] == str(active)
  os.utime(active, (1_000_000, 1_000_000))
  os.utime(idle, (500_000, 500_000))
  assert latest_log(runs) == active
  only_idle = tmp_path / "other" / ".ahc" / "runs"
  _log(only_idle / "c.jsonl", {"type": "estop", "state": "engaged", "id": "y"})
  assert latest_log(only_idle) == only_idle / "c.jsonl"  # nothing active: the newest still shows


async def test_an_idle_server_on_the_folder_does_not_take_the_page_over(tmp_path, open_lab):
  ws = Workspace(tmp_path)
  async with open_lab(tmp_path, "sim") as lab, open_lab(tmp_path, "sim"):  # a second session open in the folder
    await load_verified(lab, layout=STARLET_LAYOUT)
    active = Path(lab.server.lab.runlog.path)
    estop.engage(ws, by="test")
    await anyio.sleep(0.6)  # both servers' watchers take the stop
    assert await lab.refused("pick_up_tips", device="starlet.pip") == "emergency_stop"
    estop.release(ws, by="person")
    await anyio.sleep(0.6)
    assert await lab.refused("pick_up_tips", device="starlet.pip") == "gate_pending"
  for log in ws.runs_dir.glob("*.jsonl"):  # whatever the idle server logged, it logged last
    if log != active:
      os.utime(log)
  assert latest_log(ws.runs_dir) == active and current_state(ws, None)["layout"]["name"]


def test_waiting_for_you_comes_from_every_check_the_person_owes(tmp_path):
  def verification(check_id, check, verdict, **twin):
    return {"type": "verification", "check_id": check_id, "check": check, "verdict": verdict, "t": "2026-10-22T10:00:0" + check_id[-1],
            "judged_by": "human (placeholder)" if verdict == "pending" else "human",
            "evidence": {"digital_twin": twin}}

  log = tmp_path / ".ahc" / "runs" / "r.jsonl"
  base = [{"type": "agent", "t": "2026-10-22T09:59:00", "decision": "Dilute", "plan": PLAN},
          {"op": "load_layout", "status": "ok", "t": "2026-10-22T10:00:00", "effective": {"saved_as": "deck"}},
          {"type": "gate", "layout": "deck", "state": "pending", "check_id": None}]
  _log(log, *base, verification("c1", "tips_mounted", "pending", expect="none", tips_mounted=0))
  agent = replay(log)["agent"]
  assert agent["state"] == "waiting" and [c["check"] for c in agent["checks_waiting"]] == ["tips_mounted"]
  assert agent["checks_waiting"][0]["what"] == "no tips are left on the pipette"
  _log(log, *base, verification("c1", "tips_mounted", "pending"), verification("c1", "tips_mounted", "pass"))
  assert replay(log)["agent"]["checks_waiting"] == [] and replay(log)["agent"]["state"] == "working"
  deck = [verification("c2", "deck_matches_layout", "pending"),
          {"type": "gate", "layout": "deck", "state": "pending", "check_id": "c2"}]
  _log(log, *base, *deck)
  assert [c["what"] for c in replay(log)["agent"]["checks_waiting"]] == ["the deck matches the loaded layout"]
  released = {"type": "gate", "layout": "deck", "state": "pending", "check_id": None, "reason": "emergency stop released"}
  _log(log, *base, *deck, released)  # the stop was released: that deck check opens nothing any more
  assert replay(log)["agent"]["checks_waiting"] == []
  liquid = verification("c3", "liquid_present", "pending", min_volume_ul=90,
                        volumes_ul={f"plate_well_{r}12": 100 for r in "ABCDEFGH"})
  _log(log, *base, liquid, {"op": "load_layout", "status": "ok", "effective": {"saved_as": "deck"}})
  assert replay(log)["agent"]["checks_waiting"] == []  # a new layout: earlier checks are moot
  _log(log, *base, liquid)
  assert replay(log)["agent"]["checks_waiting"][0]["what"].startswith("liquid in plate_well_A12")
  every = verification("c4", "tips_mounted", "pending", expect="none", pipettes={"ot2.right": "unknown", "ot2.left": 0})
  _log(log, *base, every)  # the check of every pipette at once
  assert replay(log)["agent"]["checks_waiting"][0]["what"] == "every pipette is empty (ot2.right, ot2.left)"


def test_a_check_a_newer_one_replaced_is_shown_as_replaced_not_as_done(tmp_path):
  from ahc.viz.dashboard import PAGE

  log = _log(tmp_path / ".ahc" / "runs" / "r.jsonl",
             {"type": "verification", "check_id": "c1", "check": "tips_mounted", "verdict": "superseded",
              "judged_by": "nobody: check c2 replaced it"})
  assert replay(log)["events"][-1]["status"] == "superseded"
  shown = re.search(r'const st=\[([^\]]*)\]', PAGE).group(1)  # the statuses the timeline styles; others show as ok
  assert '"superseded"' in shown and ".st.superseded::before" in PAGE


async def test_the_page_waits_for_the_person_during_a_robot_check(tmp_path, fake_robot):
  from ahc.smoke import smoke

  seen = []

  def ask(question):  # while the person is asked, the dashboard says so
    state = current_state(Workspace(tmp_path), None)
    if state is not None:
      seen.append((question.splitlines()[0], state["agent"]["state"],
                   [c["check"] for c in state["agent"]["checks_waiting"]]))
    return True

  assert await smoke(tmp_path, "tips", "127.0.0.1", fake_robot.port, ask, out=lambda line: None, scripted=True) == 0
  waits = {question: (agent_state, checks) for question, agent_state, checks in seen}
  assert waits["Check the deck against the layout (lids off; A1 at the back left of each slot):"] == (
    "waiting", ["deck_matches_layout"])
  assert waits["Tips back in rack column 1, none left on the pipette? [yes/no] "] == ("waiting", ["tips_mounted"])
  final = current_state(Workspace(tmp_path), None)["agent"]
  assert final["checks_waiting"] == [] and final["state"] == "finished"


def test_the_panel_follows_the_session_making_ahc_calls(tmp_path):
  runs = tmp_path / ".ahc" / "runs"
  log = _log(runs / "r.jsonl")
  (tmp_path / ".ahc" / "agent-feed.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
    {"kind": "request", "t": "2026-10-22T10:00:00", "text": "Dilute", "session": "lab", "id": "u1"},
    {"kind": "doing", "t": "2026-10-22T10:00:05", "tool": "aspirate", "ahc": True, "session": "lab"},
    {"kind": "request", "t": "2026-10-22T10:01:00", "text": "Read the logs", "session": "reader", "id": "u2"},
    {"kind": "doing", "t": "2026-10-22T10:01:05", "tool": "Bash", "ahc": False, "session": "reader"},
    {"kind": "denied", "t": "2026-10-22T10:01:06", "tool": "dispense", "ahc": True, "session": "lab"}]))
  agent = replay(log)["agent"]
  assert agent["request"]["text"] == "Dilute"  # another session in the folder does not take the panel over
  assert agent["doing"]["tool"] == "dispense" and agent["doing"]["denied"]  # shown as refused, not as calling


# -- open_dashboard: the agent starts the page from inside the MCP server -------------------------------

def _get(url: str, path: str = "") -> tuple[int, bytes]:
  import urllib.request
  with urllib.request.urlopen(url.rstrip("/") + (path or "/"), timeout=10) as response:
    return response.status, response.read()


def _dashboard_threads() -> list:
  import threading
  return [t for t in threading.enumerate() if getattr(getattr(t, "_target", None), "__name__", "") == "serve_forever"]


@pytest.fixture
def opened(monkeypatch):
  """webbrowser.open is replaced: the URLs it was asked for."""
  urls: list[str] = []
  monkeypatch.setattr("ahc.server.webbrowser.open", lambda url: urls.append(url) or True)
  return urls


async def test_open_dashboard_serves_this_folders_page_and_follows_the_run(tmp_path, open_lab, opened):
  async with open_lab(tmp_path, "sim") as lab:
    tools = {t.name: t for t in (await lab.client.list_tools()).tools}
    assert tools["open_dashboard"].annotations.read_only_hint is False
    assert tools["open_dashboard"].annotations.destructive_hint is False
    got = await lab.call("open_dashboard", open_browser=False)
    assert got["started"] is True and got["browser_opened"] is False and opened == []
    assert got["how_to_stop"] == "the stop button on this page"
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", got["url"])
    status, body = await anyio.to_thread.run_sync(_get, got["url"], "/whoami")
    assert status == 200 and json.loads(body)["folder"] == str(lab.server.lab.ws.root)
    status, page = await anyio.to_thread.run_sync(_get, got["url"])
    assert status == 200 and b"<title>AHC" in page
    await load_verified(lab, layout=STARLET_LAYOUT, name="deck")  # a run starts after the page is up
    status, body = await anyio.to_thread.run_sync(_get, got["url"], "/state")
    state = json.loads(body)
    assert state["run"]["run_id"] == lab.server.lab.runlog.run_id and state["layout"]["name"] == "deck"
    again = await lab.call("open_dashboard", open_browser=False)
    assert again["url"] == got["url"] and again["started"] is False  # the same one
    assert (await lab.call("lab_overview"))["dashboard"] == {"url": got["url"]}


async def test_open_dashboard_opens_the_browser_and_survives_a_browser_that_fails(tmp_path, open_lab, opened, monkeypatch):
  async with open_lab(tmp_path, "sim") as lab:
    got = await lab.call("open_dashboard")  # open_browser defaults to true
    assert got["browser_opened"] is True and opened == [got["url"]]
    def broken(url):
      raise RuntimeError("no browser here")
    monkeypatch.setattr("ahc.server.webbrowser.open", broken)
    again = await lab.call("open_dashboard")
    assert again["url"] == got["url"] and again["browser_opened"] is False


async def test_the_servers_shutdown_stops_its_dashboard(tmp_path, open_lab, opened):
  before = set(_dashboard_threads())
  async with open_lab(tmp_path, "sim") as lab:
    got = await lab.call("open_dashboard", open_browser=False)
    ours = set(_dashboard_threads()) - before
    assert len(ours) == 1 and all(t.is_alive() for t in ours)
  assert not any(t.is_alive() for t in ours)  # joined, not left behind
  with pytest.raises(OSError):
    _get(got["url"], "/whoami")


async def test_open_dashboard_takes_the_next_free_port_and_keeps_the_stop_protections(tmp_path, open_lab, opened):
  other = Workspace(tmp_path / "other")
  other.root.mkdir()
  try:  # another folder's page on 8770, unless something already holds it
    held = serve(other, None, 8770, open_browser=False)
  except OSError:
    held = None
  try:
    async with open_lab(tmp_path / "task", "sim") as lab:
      got = await lab.call("open_dashboard", open_browser=False)
      port = int(got["url"].rsplit(":", 1)[1].strip("/"))
      assert whoami(port)["folder"] == str(lab.server.lab.ws.root)
      if held is not None:
        assert port != 8770
      conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)  # a POST without the page's header is refused
      conn.request("POST", "/estop", body="{}")
      assert conn.getresponse().status == 403
      conn.close()
      assert not estop.path(lab.server.lab.ws).exists()
  finally:
    if held is not None:
      held.shutdown()
      held.server_close()


async def test_open_dashboard_returns_this_folders_dashboard_a_person_already_runs(tmp_path, open_lab, opened, monkeypatch):
  theirs = serve(Workspace(tmp_path), None, 0, open_browser=False)
  port = theirs.server_address[1]
  monkeypatch.setattr("ahc.server.DASHBOARD_PORT", port)  # where the person's ahc-dashboard sits
  try:
    async with open_lab(tmp_path, "sim") as lab:
      got = await lab.call("open_dashboard", open_browser=False)
      assert got["url"] == f"http://127.0.0.1:{port}/" and got["started"] is False
      assert (await lab.call("lab_overview"))["dashboard"] == {"url": got["url"]}
  finally:
    theirs.shutdown()
    theirs.server_close()


async def test_overview_hints_at_the_dashboard_and_names_the_stop_commands(tmp_path, open_lab, opened):
  import ahc.server as server_module
  async with open_lab(tmp_path, "sim") as lab:
    before = await lab.call("lab_overview")
    assert before["dashboard"]["url"] is None and "open_dashboard" in before["dashboard"]["hint"]
    how = before["emergency_stop"]["how"]
    assert f"uv run --project {server_module.REPO} ahc-estop --workspace" in how and "open_dashboard" in how
    got = await lab.call("open_dashboard", open_browser=False)
    after = (await lab.call("lab_overview"))["emergency_stop"]["how"]
    assert got["url"] in after and f"uv run --project {server_module.REPO} ahc-estop" in after


async def test_open_dashboard_runs_while_the_emergency_stop_is_engaged(tmp_path, open_lab, opened):
  async with open_lab(tmp_path, "sim") as lab:
    estop.engage(lab.server.lab.ws, by="test")
    got = await lab.call("open_dashboard", open_browser=False)
    status, body = await anyio.to_thread.run_sync(_get, got["url"], "/estop")
    assert status == 200 and json.loads(body)["engaged"] is True
