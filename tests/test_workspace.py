"""Task workspaces, startup and the device config (steps 2 and 3 of the workspace design)."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from ahc.devices.opentrons.sim import robot_server as ot2_sim
from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT
from ahc.server import create_server
from ahc.workspace import Workspace, estop, resolve_workspace, simulation_config
from ahc.workspace.config import HEADER
from conftest import give_reference, load_verified

pytestmark = pytest.mark.anyio

STARLET_SIM = [{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}]


def config_of(folder):
  return yaml.safe_load((folder / ".ahc" / "config.yaml").read_text())


def run_entries(folder):
  return [json.loads(line) for log in sorted((folder / ".ahc" / "runs").glob("*.jsonl"))
          for line in log.read_text().splitlines()]


async def confirm(lab):
  """The person approves confirm_config with the summary the agent showed them."""
  summary = (await lab.call("lab_overview"))["config"]["confirm_summary"]
  return await lab.call("confirm_config", summary=summary)


# -- where the workspace is ---------------------------------------------------------------------

def test_workspace_resolution_order(tmp_path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  monkeypatch.delenv("AHC_WORKSPACE", raising=False)
  assert resolve_workspace() == tmp_path.resolve()
  monkeypatch.setenv("AHC_WORKSPACE", str(tmp_path / "from-env"))
  assert resolve_workspace() == (tmp_path / "from-env").resolve()
  assert resolve_workspace(tmp_path / "from-arg") == (tmp_path / "from-arg").resolve()


# -- nothing is written until a tool needs to -----------------------------------------------------

async def test_starting_and_reading_leaves_the_folder_untouched(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    overview = await lab.call("lab_overview")
    assert overview["config"]["status"] == "none" and overview["device"] == "hamilton.starlet"
    await lab.call("get_params", device="starlet.pip", op="aspirate")
    await lab.call("describe_device", component="starlet.pip")
    await lab.call("get_run")
  assert list(tmp_path.iterdir()) == []


async def test_forced_simulation_writes_its_config_on_first_use(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await lab.call("load_layout", layout=STARLET_LAYOUT)
    config = config_of(tmp_path)
    assert config["devices"] == STARLET_SIM and config["confirmation"]["by"] == "simulation"
    assert sorted(p.name for p in (tmp_path / ".ahc").iterdir()) == ["config.yaml", "layouts", "protocols", "runs", "sim", "state"]
    assert (await lab.call("lab_overview"))["config"]["status"] == "confirmed"
  assert run_entries(tmp_path)[0]["workspace"] == str(tmp_path.resolve())


async def test_two_workspaces_keep_separate_runs(tmp_path, open_lab):
  a, b = tmp_path / "task-a", tmp_path / "task-b"
  a.mkdir()
  b.mkdir()
  async with open_lab(a, "sim") as lab_a, open_lab(b, "sim", "hamilton.starlet") as lab_b:
    await lab_a.call("load_layout", layout=STARLET_LAYOUT)
    await load_verified(lab_b, layout=STARLET_LAYOUT)
    await lab_b.call("pick_up_tips", device="starlet.pip")
  assert len(list((a / ".ahc" / "runs").glob("*.jsonl"))) == 1
  assert [e["op"] for e in run_entries(a) if "op" in e] == ["load_layout"]
  assert [e["op"] for e in run_entries(b) if "op" in e] == ["load_layout", "pick_up_tips"]


# -- config: template, choice, confirmation -------------------------------------------------------

async def test_without_simulation_the_person_must_confirm(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    overview = await lab.call("lab_overview")
    assert overview["config"]["status"] == "none" and "hamilton.starlet" in overview["available_models"]
    assert list(tmp_path.iterdir()) == []
    # The first write copies the template; nothing may move while it is pending.
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "config_pending"
    assert config_of(tmp_path)["devices"] == []
    configured = await lab.call("configure_devices", devices=STARLET_SIM)
    assert configured["config"]["status"] == "pending" and "confirm_config" in configured["next"]
    assert await lab.refused("pick_up_tips", device="starlet.pip") == "config_pending"
    confirmed = await confirm(lab)
    assert confirmed["config"]["status"] == "confirmed" and confirmed["config"]["confirmed_by"] == "human"
    await lab.call("load_layout", layout=STARLET_LAYOUT)
  assert config_of(tmp_path)["confirmation"]["by"] == "human"


async def test_the_person_confirms_the_summary_they_were_shown(tmp_path, open_lab):
  limits = {"starlet.pip": {"volume_ul": {"max": 200}, "flow_rate_ul_s": {"min": 5, "max": 100}}}
  async with open_lab(tmp_path, backend=None) as lab:
    shown = (await lab.call("configure_devices", devices=STARLET_SIM, limits=limits))["config"]["confirm_summary"]
    assert shown == ("starlet = hamilton.starlet, backend sim; task limits: starlet.pip flow_rate_ul_s min 5 max 100, "
                     "starlet.pip volume_ul max 200")
    assert await lab.refused("confirm_config") == "summary_mismatch"  # no summary: the person would see nothing
    edit_limits(tmp_path, {"starlet.pip": {"volume_ul": {"max": 500}}})  # changed after the person saw it
    text = await lab.call_text("confirm_config", summary=shown)
    assert "[summary_mismatch]" in text and "starlet.pip volume_ul max 500" in text
    assert "confirmation" not in config_of(tmp_path)
    current = (await lab.call("lab_overview"))["config"]["confirm_summary"]
    assert (await lab.call("confirm_config", summary=current))["config"]["status"] == "confirmed"
  confirmed = [e for e in run_entries(tmp_path) if e.get("op") == "confirm_config"]
  assert [(e["status"], e["basic"]["summary"]) for e in confirmed][-1] == ("ok", current)  # what was confirmed, in the log


async def test_forced_simulation_confirms_a_simulation_only_choice(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    configured = await lab.call("configure_devices",
                                devices=[{"id": "robot1", "model": "hamilton.starlet", "backend": "sim"}])
    assert configured["config"]["status"] == "confirmed" and configured["config"]["confirmed_by"] == "simulation"
    overview = await lab.call("lab_overview")
    assert "robot1.pip" in overview["components"]  # addresses follow the config's device id
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device="robot1.pip")


def test_only_simulation_can_be_forced(tmp_path):
  with pytest.raises(ValueError, match="only 'sim'"):
    create_server(tmp_path, backend="usb")


def test_config_mode_follows_the_folder(tmp_path, monkeypatch):
  monkeypatch.delenv("AHC_BACKEND", raising=False)
  assert create_server(tmp_path, "config").lab.forced_sim is False
  assert create_server(tmp_path, "sim").lab.forced_sim is True
  monkeypatch.setenv("AHC_BACKEND", "sim")  # forcing simulation wins from either source
  assert create_server(tmp_path, "config").lab.forced_sim is True
  monkeypatch.setenv("AHC_BACKEND", "usb")
  with pytest.raises(ValueError, match="only 'sim'"):
    create_server(tmp_path, "config")


def test_the_plugin_follows_the_folder_unless_simulation_is_forced():
  plugin = json.loads((Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json").read_text())
  args = plugin["mcpServers"]["ahc"]["args"]
  # Claude Code expands it: unset = config (the folder's record decides); AHC_PLUGIN_BACKEND=sim still forces simulation.
  assert args[args.index("--backend") + 1] == "${AHC_PLUGIN_BACKEND:-config}"


async def test_only_the_person_only_tools_need_the_person_in_claude_code(tmp_path, open_lab):
  async with open_lab(tmp_path) as lab:
    tools = (await lab.client.list_tools()).tools
  marked = {t.name for t in tools if (t.meta or {}).get("anthropic/requiresUserInteraction") is True}
  assert marked == {"confirm_config", "record_verdict"}


async def test_the_home_folder_is_no_task(tmp_path, monkeypatch, open_lab):
  monkeypatch.setenv("AHC_HOME", str(tmp_path / ".ahc"))
  (tmp_path / ".ahc" / "sim").mkdir(parents=True)  # what every task shares, as ~/.ahc holds it
  async with open_lab(tmp_path, auto_plan=False) as lab:
    await lab.call("lab_overview")  # reading is fine
    assert await lab.refused("report_decision", decision="Plan", plan=["Load"]) == "not_a_task_folder"
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT, plan_step=1) == "not_a_task_folder"
    assert await lab.refused("configure_devices", devices=STARLET_SIM) == "not_a_task_folder"
  assert sorted(p.name for p in (tmp_path / ".ahc").iterdir()) == ["sim"]


async def test_a_real_backend_needs_a_description_file_backend(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    bad = [{"id": "starlet", "model": "hamilton.starlet", "backend": "usb"}]
    assert await lab.refused("configure_devices", devices=bad) == "config_invalid"
    assert await lab.refused("configure_devices", devices=STARLET_SIM * 2) == "config_invalid"


async def test_editing_a_confirmed_config_by_hand_sends_it_back_to_pending(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    await lab.call("configure_devices", devices=STARLET_SIM)
    await confirm(lab)
    await lab.call("load_layout", layout=STARLET_LAYOUT)
    config = config_of(tmp_path)
    config["limits"] = {"starlet.pip": {"volume_ul": {"max": 50}}}  # an edit the person never confirmed
    (tmp_path / ".ahc" / "config.yaml").write_text(yaml.safe_dump(config))
    assert await lab.refused("pick_up_tips", device="starlet.pip") == "config_pending"
    assert (await lab.call("lab_overview"))["config"]["reason"].startswith("devices or limits changed")
  audits = [e for e in run_entries(tmp_path) if e.get("type") == "audit"]
  assert [a["event"] for a in audits] == ["config_edited_outside_server"]


async def confirmed_and_loaded(lab, devices=STARLET_SIM):
  await lab.call("configure_devices", devices=devices)
  await confirm(lab)
  give_reference(lab)
  await load_verified(lab, layout=STARLET_LAYOUT)
  return lab.server.lab.adapter


def edit_limits(folder, limits):
  config = config_of(folder)
  config["limits"] = limits  # an edit the person makes by hand
  (folder / ".ahc" / "config.yaml").write_text(yaml.safe_dump(config))


async def test_a_config_edit_keeps_the_device_and_its_tips(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    adapter = await confirmed_and_loaded(lab)
    await lab.call("pick_up_tips", device="starlet.pip")
    edit_limits(tmp_path, {"starlet.pip": {"volume_ul": {"max": 50}}})
    for tool in ("lab_overview", "get_state", "find_layout_references", "list_checks", "get_run"):
      await lab.call(tool)  # reading never stops or drops the device
    state = await lab.call("get_state")
    assert lab.server.lab.adapter is adapter and state["tips_mounted"] == {"starlet.pip": 8}
    aspirate = dict(device="starlet.pip", targets=["diluent"], volumes=20)
    assert await lab.refused("aspirate", **aspirate) == "config_pending"
    await confirm(lab)  # the person confirms the tighter limit
    assert lab.server.lab.adapter is adapter  # the same device, its tips still on
    limits = (await lab.call("get_params", device="starlet.pip", op="aspirate"))["basic"]["volumes"]["limits"]
    assert limits["max"] == 50 and limits["task_limit"]
    assert await lab.refused("aspirate", **aspirate) == "gate_pending"  # confirmed again: the deck is checked again
    await lab.call("verify", check="deck_matches_layout")
    assert await lab.refused("aspirate", **{**aspirate, "volumes": 60}) == "out_of_range"
    await lab.call("aspirate", **aspirate)
  gates = [e["reason"] for e in run_entries(tmp_path) if e.get("type") == "gate"]
  assert gates[-2:] == ["the config was confirmed again: check the deck again", "deck check pass"]


async def test_writing_the_same_config_again_keeps_its_confirmation(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    adapter = await confirmed_and_loaded(lab)
    again = await lab.call("configure_devices", devices=STARLET_SIM)
    assert again["config"]["status"] == "confirmed" and lab.server.lab.adapter is adapter
    await lab.call("pick_up_tips", device="starlet.pip")  # nothing changed: the gate stays open
    await lab.call("drop_tips", device="starlet.pip", mode="return")
    other = [{"id": "robot1", "model": "hamilton.starlet", "backend": "sim"}]
    assert (await lab.call("configure_devices", devices=other))["config"]["status"] == "pending"
    assert lab.server.lab.adapter is adapter  # pending: the old device stays bound, and nothing moves
    assert await lab.refused("pick_up_tips", device="starlet.pip") == "config_pending"
    await confirm(lab)
    assert lab.server.lab.adapter is not adapter and "robot1.pip" in (await lab.call("lab_overview"))["components"]


async def test_reading_a_hand_edited_folder_writes_no_run_log(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    await lab.call("configure_devices", devices=STARLET_SIM)
    await confirm(lab)
  shutil.rmtree(tmp_path / ".ahc" / "runs")
  edit_limits(tmp_path, {"starlet.pip": {"volume_ul": {"max": 50}}})
  async with open_lab(tmp_path, backend=None) as lab:  # a new session in that folder
    for tool in ("get_run", "lab_overview", "get_state", "find_layout_references", "list_checks", "get_run"):
      await lab.call(tool)
    assert not (tmp_path / ".ahc" / "runs").exists()
    await lab.call("report_decision", decision="Ask the person to confirm the edited limits", waiting_for="person")
  entries = run_entries(tmp_path)
  assert [e.get("type") for e in entries] == ["run", "audit", "wait", "agent"]  # wait: this session awaits the go-ahead
  assert entries[1]["event"] == "config_edited_outside_server"


async def test_a_device_that_cannot_stop_never_wedges_the_server(tmp_path, open_lab, monkeypatch):
  async def robot_off():
    raise ConnectionRefusedError("[Errno 61] Connection refused")

  async with open_lab(tmp_path, backend=None) as lab:
    old = await confirmed_and_loaded(lab)
    monkeypatch.setattr(old, "stop", robot_off)
    await lab.call("configure_devices", devices=[{"id": "robot1", "model": "hamilton.starlet", "backend": "sim"}])
    for tool in ("lab_overview", "get_state", "get_run"):
      await lab.call(tool)
    confirmed = await confirm(lab)  # the old device cannot stop: dropped anyway
    assert confirmed["config"]["status"] == "confirmed" and lab.server.lab.adapter is not old
    await lab.call("report_decision", decision="Going on with the new device")
    monkeypatch.setattr(lab.server.lab.adapter, "stop", robot_off)  # and at shutdown: the server still closes
  failed = [e for e in run_entries(tmp_path) if e.get("event") == "device_stop_failed"]
  assert len(failed) == 2 and "Connection refused" in failed[0]["detail"] and "shut down" in failed[1]["detail"]


async def test_a_broken_config_keeps_the_server_up(tmp_path, open_lab):
  (tmp_path / ".ahc").mkdir()
  (tmp_path / ".ahc" / "config.yaml").write_text("devices: [\n")
  async with open_lab(tmp_path, "sim") as lab:
    overview = await lab.call("lab_overview")
    assert overview["config"]["status"] == "invalid" and overview["config"]["error"] == "config_invalid"
    assert "tell the person" in overview["next"] and "Tell the person" in overview["config"]["hint"]
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "config_invalid"
    # Rewriting it is the way out, but its limits are unknown: even forced simulation waits for the person.
    rewritten = await lab.call("configure_devices", devices=STARLET_SIM)
    assert rewritten["config"]["status"] == "pending" and "could not be read" in rewritten["next"]
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "config_pending"
    await confirm(lab)
    await lab.call("load_layout", layout=STARLET_LAYOUT)


async def test_a_config_that_does_not_parse_never_drops_the_persons_limits_silently(tmp_path, open_lab):
  (tmp_path / ".ahc").mkdir()
  (tmp_path / ".ahc" / "config.yaml").write_text(yaml.safe_dump({
    "devices": STARLET_SIM, "limits": {"starlet.pip": {"volume_ul": {"max": 50}}}, "note": "the person's"}))
  async with open_lab(tmp_path, "sim") as lab:
    assert (await lab.call("lab_overview"))["config"]["status"] == "invalid"  # an unknown key
    rewritten = await lab.call("configure_devices", devices=STARLET_SIM)  # without the limit
    assert rewritten["config"]["status"] == "pending" and rewritten["config"]["confirmed_by"] is None
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "config_pending"


async def test_device_flag_conflicting_with_the_config_is_refused(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await lab.call("load_layout", layout=STARLET_LAYOUT)
  async with open_lab(tmp_path, "sim", "opentrons.ot2") as lab:
    assert (await lab.call("lab_overview"))["config"]["error"] == "device_conflict"
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "device_conflict"


async def test_an_engaged_stop_refuses_before_the_folder_is_touched(tmp_path, open_lab):
  ws = Workspace(tmp_path)
  estop.engage(ws, by="test")  # pressed before this session's first call
  async with open_lab(tmp_path, "sim", auto_plan=False) as lab:
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT, plan_step=1) == "emergency_stop"
    assert lab.server.lab.adapter is None  # no device started
  assert not ws.config_path.exists()  # and no config written for it


# -- writes a reader never sees half of ---------------------------------------------------------

def disk_full_after_half(monkeypatch):
  """Every Path.write_text writes half its text, then the disk is full."""
  real = Path.write_text

  def disk_full(self, text, *args, **kwargs):
    real(self, text[: len(text) // 2], *args, **kwargs)
    raise OSError(28, "No space left on device")

  monkeypatch.setattr(Path, "write_text", disk_full)


@pytest.mark.parametrize("what", ["config", "layout", "simulator settings"])
def test_a_write_that_fails_leaves_the_old_file_whole(tmp_path, monkeypatch, what):
  ws = Workspace(tmp_path)
  if what == "config":
    path, write = ws.config_path, lambda data: ws.write_config(data, HEADER)
    old, new = simulation_config("hamilton.starlet").data(), {**simulation_config("hamilton.starlet").data(),
                                                              "limits": {"starlet.pip": {"volume_ul": {"max": 200}}}}
  elif what == "layout":
    path, write = ws.layout_path("deck"), lambda data: ws.save_layout("deck", data, "hamilton.starlet")
    old, new = STARLET_LAYOUT, {**STARLET_LAYOUT, "liquids": {}}
  else:  # ahc-sim's, from which the server reads the simulator's port on every call
    monkeypatch.setattr(ot2_sim, "choose_port", lambda ws: 31999)  # no port scan
    base = ot2_sim.Base(tmp_path / "robot-server", "abc1234", "setup")
    path, write = ot2_sim.settings_path(ws), lambda data: ot2_sim.prepare_instance(ws, base)
    old = new = None
  write(old)
  before = path.read_text()
  disk_full_after_half(monkeypatch)
  with pytest.raises(OSError):
    write(new)
  monkeypatch.undo()
  assert path.read_text() == before and not list(ws.dir.rglob("*.tmp"))


async def test_the_first_config_is_never_half_written(tmp_path, open_lab, monkeypatch):
  async with open_lab(tmp_path, backend=None) as lab:  # the first write creates the config from the template
    disk_full_after_half(monkeypatch)
    failed = await lab.client.call_tool("load_layout", {"layout": STARLET_LAYOUT, "plan_step": 1})
    monkeypatch.undo()
    assert failed.is_error
  ws = Workspace(tmp_path)
  assert not ws.config_path.exists() and not list(ws.dir.rglob("*.tmp"))


# -- references for a new layout ----------------------------------------------------------------

def earlier_run(ws, device, *entries):
  """A run log as an earlier session in this folder left it."""
  ws.runs_dir.mkdir(parents=True, exist_ok=True)
  log = ws.runs_dir / f"2026100{len(list(ws.runs_dir.iterdir())) + 1}T120000-abcdef.jsonl"
  lines = [{"type": "run", "device": device, "backend": "sim"}, *entries]
  log.write_text("".join(json.dumps(e) + "\n" for e in lines))


def loaded(name, layout):
  return {"op": "load_layout", "status": "ok", "basic": {"layout": layout, "name": None}, "effective": {"saved_as": name}}


def gate(state, name):
  return {"type": "gate", "state": state, "layout": name, "reason": f"deck check {state}"}


def test_only_what_fits_this_device_counts_as_a_reference(tmp_path):
  ws = Workspace(tmp_path)
  ws.protocols_dir.mkdir(parents=True)
  (ws.protocols_dir / ".DS_Store").write_bytes(b"\0\0\0\1Bud1")  # Finder's, not the person's
  (ws.protocols_dir / "notes.md").write_text(" \n")  # empty
  (ws.protocols_dir / ".drafts").mkdir()
  (ws.protocols_dir / ".drafts" / "deck.md").write_text("# A hidden draft\n")
  assert not ws.has_reference("hamilton.starlet") and ws.references()["protocols"] == []
  ws.save_layout("ot2-deck", OT2_LAYOUT, "opentrons.ot2")  # saved for the other device
  assert not ws.has_reference("hamilton.starlet") and ws.has_reference("opentrons.ot2")
  assert [l["model"] for l in ws.references(model="opentrons.ot2")["layouts"]] == ["opentrons.ot2"]
  # Earlier runs: a layout counts once its deck check passed, and only for the device it was used on.
  earlier_run(ws, "hamilton.starlet", loaded("rejected", STARLET_LAYOUT), gate("pending", "rejected"),
              gate("failed", "rejected"))
  earlier_run(ws, "hamilton.starlet", loaded("unchecked", STARLET_LAYOUT), gate("pending", "unchecked"))
  earlier_run(ws, "hamilton.starlet", loaded("first", STARLET_LAYOUT), loaded("second", STARLET_LAYOUT),
              gate("passed", "first"))  # a pass for a layout the device no longer held
  assert not ws.has_reference("hamilton.starlet")
  earlier_run(ws, "opentrons.ot2", loaded("ot2-run", OT2_LAYOUT), gate("pending", "ot2-run"), gate("passed", "ot2-run"))
  assert not ws.has_reference("hamilton.starlet")
  assert [r["device"] for r in ws.references(model="opentrons.ot2")["run_layouts"]] == ["opentrons.ot2"]
  earlier_run(ws, "hamilton.starlet", loaded("deck", STARLET_LAYOUT), gate("passed", "deck"))
  assert [r["saved_as"] for r in ws.references(model="hamilton.starlet")["run_layouts"]] == ["deck"]
  (ws.protocols_dir / "dilution.md").write_text("# Serial dilution\n")
  assert [p["name"] for p in ws.references()["protocols"]] == ["dilution.md"]


async def test_another_devices_layouts_leave_the_person_to_confirm(tmp_path, open_lab):
  ws = Workspace(tmp_path)
  ws.save_layout("ot2-deck", OT2_LAYOUT, "opentrons.ot2")  # the folder was used with the OT-2 before
  (ws.protocols_dir / ".DS_Store").write_bytes(b"\0\0\0\1Bud1")
  async with open_lab(tmp_path, "sim") as lab:
    refs = await lab.call("find_layout_references")
    assert refs["protocols"] == refs["layouts"] == refs["run_layouts"] == []
    assert (await lab.call("load_layout", layout=STARLET_LAYOUT))["needs_person"]
