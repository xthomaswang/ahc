"""The emergency stop: pressed anywhere, the server stops at once and refuses everything until a person
releases it; the Claude Code hook ends the agent's turn."""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import anyio
import pytest

from ahc.examples import STARLET_LAYOUT
from ahc.viz.dashboard import serve
from ahc.workspace import estop
from ahc.workspace.store import Workspace
from conftest import load_verified

pytestmark = pytest.mark.anyio

DEV = "starlet.pip"
HOOK = Path(__file__).resolve().parents[1] / "hooks" / "estop-guard.sh"


async def test_a_pressed_stop_refuses_every_action_but_not_reading(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    estop.engage(Workspace(tmp_path), by="test")
    for tool, args in (("pick_up_tips", {"device": DEV}), ("load_layout", {"layout": STARLET_LAYOUT}),
                       ("verify", {"check": "deck_matches_layout"}),
                       ("configure_devices", {"devices": [{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}]})):
      assert await lab.refused(tool, **args) == "emergency_stop", tool
    overview = await lab.call("lab_overview")
    assert overview["emergency_stop"]["engaged"] and "emergency stop" in overview["next"]
    await lab.call("get_state")
    await lab.call("report_decision", decision="Stopped; waiting for the person", waiting_for="person")


async def test_the_stop_interrupts_the_running_command(tmp_path, open_lab, monkeypatch):
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device=DEV)

    async def slow_aspirate(*args, **kwargs):  # a long move on the device
      await asyncio.sleep(5)

    monkeypatch.setattr(lab.server.lab.adapter, "aspirate", slow_aspirate)
    started = time.monotonic()
    async with anyio.create_task_group() as tg:
      async def press_soon():
        await anyio.sleep(0.3)
        estop.engage(Workspace(tmp_path), by="test")
      tg.start_soon(press_soon)
      text = await lab.call_text("aspirate", device=DEV, targets=["diluent"], volumes=50)
    assert "[emergency_stop]" in text and "interrupted aspirate" in text
    assert time.monotonic() - started < 2  # not the 5 s the command would have taken
    entries = lab.server.lab.runlog.entries
    assert [e for e in entries if e.get("type") == "estop"][0]["interrupted"] is True
    assert [e for e in entries if e.get("op") == "aspirate"][-1]["error"] == "emergency_stop"


async def test_only_a_release_record_releases_it(tmp_path, open_lab):
  ws = Workspace(tmp_path)
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device=DEV)  # tips are on when the stop comes
    estop.engage(ws, by="test")
    assert await lab.refused("drop_tips", device=DEV) == "emergency_stop"
    estop.path(ws).unlink()  # deleting the file is not a release: the server puts it back
    assert await lab.refused("drop_tips", device=DEV) == "emergency_stop"
    assert estop.read(ws)["engaged"]
    estop.release(ws, by="person")
    # Released: the deck is checked again first; with tips on, that is the way to go on, not a reload.
    assert await lab.refused("drop_tips", device=DEV) == "gate_pending"
    await lab.call("verify", check="deck_matches_layout")
    await lab.call("drop_tips", device=DEV)
  states = [e["state"] for e in lab.server.lab.runlog.entries if e.get("type") == "estop"]
  assert states == ["engaged", "released"]


def _post(port, path, body, headers):
  request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json", **headers}, method="POST")
  try:
    with urllib.request.urlopen(request, timeout=5) as response:
      return response.status, json.load(response)
  except urllib.error.HTTPError as exc:
    return exc.code, None


def test_the_dashboard_button_needs_its_own_page(tmp_path):
  runs = tmp_path / ".ahc" / "runs"
  runs.mkdir(parents=True)
  port = 18770 + os.getpid() % 1000
  httpd = serve(runs, None, port, open_browser=False)
  try:
    mine = {"X-AHC-Control": "1", "Origin": f"http://127.0.0.1:{port}"}
    assert _post(port, "/estop", {}, {})[0] == 403  # no header: a cross-site form
    assert _post(port, "/estop", {}, {**mine, "Origin": "http://evil.example"})[0] == 403
    status, data = _post(port, "/estop", {"reason": "test"}, mine)
    assert status == 200 and data["engaged"] and data["by"] == "dashboard"
    assert _post(port, "/estop/release", {}, mine)[0] == 400  # not confirmed
    status, data = _post(port, "/estop/release", {"confirm": True}, mine)
    assert status == 200 and data["engaged"] is False and data["released_by"] == "dashboard"
  finally:
    httpd.shutdown()


def test_the_claude_code_hook_ends_the_turn_only_when_engaged(tmp_path):
  run = lambda: subprocess.run(["sh", str(HOOK)], capture_output=True, text=True,
                               env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)})
  assert run().stdout == ""  # no task here: a no-op
  estop.engage(Workspace(tmp_path), by="test")
  out = json.loads(run().stdout)
  assert out["continue"] is False and "emergency stop" in out["stopReason"]
  assert out["hookSpecificOutput"]["permissionDecision"] == "deny"  # parallel calls in the batch do not run either
  estop.release(Workspace(tmp_path), by="person")
  assert run().stdout == ""


def test_releasing_from_a_shell_without_a_person_is_refused(tmp_path):
  estop.engage(Workspace(tmp_path), by="test")
  cli = Path(sys.executable).parent / "ahc-estop"
  out = subprocess.run([str(cli), "--release", "--workspace", str(tmp_path)], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)
  assert out.returncode != 0 and "person at a terminal" in out.stderr
  assert estop.read(Workspace(tmp_path))["engaged"]
