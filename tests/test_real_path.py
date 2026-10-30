"""What may reach a real robot: forcing simulation never drives one, server-level overrides never redirect the
robot the person confirmed, and a port named at server level must be an AHC simulator. A fake robot-server
(tests/fake_robot.py) stands in for the robot; nothing here leaves this machine."""

import ipaddress
import json
import socket
import urllib.request

import pytest

from ahc.core.errors import LabError
from ahc.devices.opentrons.ot2 import OT2Adapter
from ahc.examples import OT2_LAYOUT
from ahc.server import REPO
from ahc.workspace import Config, DeviceEntry, Workspace
from ahc.workspace.config import HEADER
from conftest import OT2_HOST, OT2_PORT, load_verified, robot_server_up
from fake_robot import FakeRobot

pytestmark = pytest.mark.anyio

DEV = "ot2.right"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
  """A fresh AHC_HOME: no simulator base is installed, whatever this machine has in ~/.ahc."""
  monkeypatch.setenv("AHC_HOME", str(tmp_path / "ahc-home"))
  monkeypatch.delenv("AHC_BACKEND", raising=False)


@pytest.fixture
def connections(monkeypatch):
  """Every connection this process opens; one that would leave this machine is refused at once."""
  made = []
  real = socket.socket.connect

  def connect(sock, address):
    made.append(address)
    host = address[0] if isinstance(address, tuple) else None
    if host is not None and not ipaddress.ip_address(host).is_loopback:
      raise ConnectionRefusedError(f"test: {address} is off this machine")
    return real(sock, address)

  monkeypatch.setattr(socket.socket, "connect", connect)
  return made


@pytest.fixture
def folder(tmp_path):
  def make(name):
    path = tmp_path / name
    path.mkdir()
    return path
  return make


def confirmed_config(folder, backend="robot", **options):
  """A config the person confirmed earlier, as ahc-smoke leaves it in a task folder."""
  config = Config(devices=[DeviceEntry("ot2", "opentrons.ot2", backend, options)], limits={})
  config.confirm("human")
  Workspace(folder).write_config(config.data(), HEADER)


def requests(bot):
  return [(method, path) for _, method, path, _ in bot.log if method in ("GET", "POST", "DELETE")]


def run_entries(folder):
  return [json.loads(line) for log in sorted((folder / ".ahc" / "runs").glob("*.jsonl"))
          for line in log.read_text().splitlines()]


def robot(**options):
  return [{"id": "ot2", "model": "opentrons.ot2", "backend": "robot", "options": options}]


def stub_probe(monkeypatch, **override):
  """OT2Adapter.probe_identity as contract C1 has it (devices/base.py, defined with the OT-2 adapter):
  GET /health and /instruments, nothing else. `override` replaces fields of its answer."""
  def probe(cls, backend, options):
    if backend == "sim":
      return {}
    base = f"http://{options['host']}:{options.get('port') or 31950}"

    def get(path):
      request = urllib.request.Request(base + path, headers={"Opentrons-Version": "*"})
      with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)

    try:
      health = get("/health")
    except OSError as exc:
      raise LabError("backend_unreachable", f"no robot-server at {base} ({exc}).",
                     "Check that the robot is on and reachable.") from exc
    mounted = {i["mount"]: {"name": i["instrumentName"], "model": i["instrumentModel"], "serial": i["serialNumber"]}
               for i in get("/instruments")["data"]}
    out = {k: health.get(k) for k in ("name", "robot_serial", "robot_model", "api_version", "fw_version", "system_version")}
    out.update(pipettes={m: mounted.get(m) for m in ("left", "right")}, mounts={"ok": True, "problems": []})
    return {**out, **override}

  monkeypatch.setattr(OT2Adapter, "probe_identity", classmethod(probe), raising=False)


async def confirm(lab):
  """The person approves confirm_config with the summary the agent showed them."""
  summary = (await lab.call("lab_overview"))["config"]["confirm_summary"]
  return await lab.call("confirm_config", summary=summary)


# -- forced simulation ------------------------------------------------------------------------------

async def test_forced_simulation_never_reaches_the_confirmed_robot(tmp_path, open_lab, fake_robot, connections):
  confirmed_config(tmp_path, host="127.0.0.1", port=fake_robot.port, robot_name=fake_robot.name)
  Workspace(tmp_path).save_layout("smoke-deck", OT2_LAYOUT, "opentrons.ot2")  # a reference: the simulator would judge it
  async with open_lab(tmp_path, "sim") as lab:  # the plugin's server, opened in the folder ahc-smoke left behind
    overview = await lab.call("lab_overview")
    assert overview["forced_simulation"] and overview["simulated"] and overview["backend"] == "sim"
    assert "never on the robot" in overview["config"]["note"] and overview["next"].startswith("Simulation only")
    assert overview["endpoint"] != f"127.0.0.1:{fake_robot.port}"
    assert await lab.refused("load_layout", name="smoke-deck") == "sim_not_installed"
    assert await lab.refused("pick_up_tips", device=DEV) in ("no_layout", "gate_pending")
    await lab.call("get_state")
  assert fake_robot.log == []  # not one request, not even a read
  assert not [a for a in connections if a[1] == fake_robot.port]


async def test_forced_simulation_never_reaches_a_remote_robot(tmp_path, open_lab, connections):
  confirmed_config(tmp_path, host="192.0.2.10", port=31950, robot_name="lab-ot2")
  async with open_lab(tmp_path, "sim") as lab:
    await lab.call("lab_overview")
    assert await lab.refused("load_layout", layout=OT2_LAYOUT) == "sim_not_installed"
  assert not [a for a in connections if a[0] == "192.0.2.10"]


async def test_a_sim_entry_names_no_endpoint(tmp_path, open_lab):
  entry = {"id": "ot2", "model": "opentrons.ot2", "backend": "sim", "options": {"host": "192.0.2.10", "port": 31950}}
  async with open_lab(tmp_path, "sim") as lab:
    text = await lab.call_text("configure_devices", devices=[entry])
  assert "[config_invalid]" in text and "takes no host, port or robot_name" in text
  assert not (tmp_path / ".ahc" / "config.yaml").exists()
  confirmed_config(tmp_path, backend="sim", host="192.0.2.10", port=31950)  # written by hand: invalid, nothing binds
  async with open_lab(tmp_path, "sim") as lab:
    overview = await lab.call("lab_overview")
    assert overview["config"]["status"] == "invalid" and "device" not in overview


# -- a simulator named at server level (tests, ahc-demo) --------------------------------------------

async def test_a_server_level_port_must_be_an_ahc_simulator(folder, open_lab, fake_robot):
  impostor = FakeRobot(name="ahc-impostor")  # named like one, but a robot's firmware and serial
  try:
    for bot in (fake_robot, impostor):
      async with open_lab(folder(bot.name), "sim", "opentrons.ot2", {"host": "127.0.0.1", "port": bot.port}) as lab:
        text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
      assert "[not_a_simulator]" in text and bot.name in text and "'v1.0'" in text
      assert requests(bot) == [("GET", "/health")]  # one read, then refused
  finally:
    impostor.close()


@pytest.mark.skipif(not robot_server_up(), reason="set AHC_TEST_OT2_PORT to a running OT-2 simulator's port")
async def test_a_server_level_port_accepts_a_running_ahc_simulator(folder, open_lab):
  async with open_lab(folder("task"), "sim", "opentrons.ot2", {"host": OT2_HOST, "port": OT2_PORT}) as lab:
    loaded = await lab.call("load_layout", layout=OT2_LAYOUT)
    overview = await lab.call("lab_overview")
  assert loaded["gate"]["state"] == "pending" and overview["endpoint"] == f"{OT2_HOST}:{OT2_PORT}"


async def test_a_server_level_host_must_be_this_machine(tmp_path, open_lab, connections):
  async with open_lab(tmp_path, "sim", "opentrons.ot2", {"host": "192.0.2.10", "port": 31950}) as lab:
    assert await lab.refused("load_layout", layout=OT2_LAYOUT) == "sim_endpoint_not_local"
    assert (await lab.call("lab_overview"))["config"]["error"] == "sim_endpoint_not_local"
  assert not [a for a in connections if a[0] == "192.0.2.10"]


# -- a real robot: only where the person confirmed it -----------------------------------------------

async def test_server_level_overrides_never_redirect_a_confirmed_robot(tmp_path, open_lab, fake_robot):
  other = FakeRobot(name="other-ot2")
  try:
    confirmed_config(tmp_path, host="127.0.0.1", port=fake_robot.port)  # no robot_name to catch a redirect
    for options in ({"port": other.port}, {"host": "localhost"}, {"host": "127.0.0.1", "port": other.port}):
      async with open_lab(tmp_path, backend=None, options=options) as lab:  # e.g. AHC_OT2_PORT left from a rehearsal
        assert (await lab.call("lab_overview"))["config"]["error"] == "endpoint_override"
        text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
      assert "[endpoint_override]" in text and f"127.0.0.1:{fake_robot.port}" in text
    assert fake_robot.log == [] and other.log == []
    async with open_lab(tmp_path, backend=None, options={"host": "127.0.0.1", "port": fake_robot.port}) as lab:
      # The confirmed address itself: nothing to override. The session's go-ahead is the person's confirm_config,
      # which takes only an entry that names its robot: written again, which reads the name from the robot.
      await lab.call("configure_devices", devices=robot(host="127.0.0.1", port=fake_robot.port))
      await confirm(lab)
      await load_verified(lab, layout=OT2_LAYOUT)
      await lab.call("pick_up_tips", device=DEV)
      overview = await lab.call("lab_overview")
    assert overview["simulated"] is False and overview["endpoint"] == f"127.0.0.1:{fake_robot.port}"
    assert other.log == [] and ("POST", "/runs") in requests(fake_robot)
  finally:
    other.close()


# -- the robot the person confirms ----------------------------------------------------------------

async def test_configure_devices_reads_the_robot_before_the_person_confirms(tmp_path, open_lab, fake_robot, monkeypatch):
  stub_probe(monkeypatch)
  async with open_lab(tmp_path, backend=None) as lab:
    written = await lab.call("configure_devices", devices=robot(host="127.0.0.1", port=fake_robot.port))
    config, identity = written["config"], written["device_identity"]
    assert config["status"] == "pending" and config["devices"][0]["options"]["robot_name"] == "fake-ot2"
    assert identity["robot_serial"] == "OT2FAKE" and identity["pipettes"]["right"]["name"] == "p300_multi_gen2"
    assert config["confirm_summary"] == f"ot2 = opentrons.ot2, backend robot, robot 'fake-ot2' at 127.0.0.1:{fake_robot.port}, mounts left p20_single_gen2, right p300_multi_gen2; task limits: none"
    assert config["devices"][0]["options"]["mounts"] == {"left": "p20_single_gen2", "right": "p300_multi_gen2"}
    assert "identity" in written["next"] and "confirm_summary" in written["next"]
    assert requests(fake_robot) == [("GET", "/health"), ("GET", "/instruments")]  # read-only: no run, no motion
    confirmed = await lab.call("confirm_config", summary=config["confirm_summary"])
    assert confirmed["config"]["status"] == "confirmed" and confirmed["device_identity"]["name"] == "fake-ot2"
  entries = run_entries(tmp_path)
  assert entries[0]["type"] == "run" and entries[0]["backend"] == "robot"  # the header names the backend it drives
  device = [e for e in entries if e.get("type") == "device"]
  assert len(device) == 1 and device[0]["name"] == "fake-ot2" and device[0]["robot_serial"] == "OT2FAKE"
  assert device[0]["backend"] == "robot" and device[0]["endpoint"] == f"127.0.0.1:{fake_robot.port}"
  assert [m for m, _ in requests(fake_robot)].count("POST") == 0  # confirming moves nothing either


async def test_configure_devices_refuses_another_robot_and_mounts_it_does_not_carry(tmp_path, open_lab, fake_robot, monkeypatch):
  stub_probe(monkeypatch)
  async with open_lab(tmp_path, backend=None) as lab:
    text = await lab.call_text("configure_devices", devices=robot(host="127.0.0.1", port=fake_robot.port,
                                                                  robot_name="lab-ot2-7"))
    assert "[wrong_robot]" in text and "'fake-ot2', not 'lab-ot2-7'" in text
    text = await lab.call_text("configure_devices", devices=robot(host="127.0.0.1", port=fake_robot.port,
                                                                  mounts={"left": "p20_single_gen2", "right": "p1000_single_gen2"}))
    assert "[mount_mismatch]" in text and "p300_multi_gen2" in text and "nothing was written" in text
    assert "Leave mounts out" in text
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    text = await lab.call_text("configure_devices", devices=robot(host="127.0.0.1", port=port))
    assert "[backend_unreachable]" in text and "must be on and reachable" in text
  assert not (tmp_path / ".ahc" / "config.yaml").exists()
  assert [m for m, _ in requests(fake_robot)].count("POST") == 0


async def test_a_robot_without_its_name_is_never_confirmed(tmp_path, open_lab, fake_robot, monkeypatch):
  monkeypatch.setattr(OT2Adapter, "probe_identity", classmethod(lambda cls, backend, options: {}), raising=False)
  async with open_lab(tmp_path, backend=None) as lab:  # nothing read the robot's name
    written = await lab.call("configure_devices", devices=robot(host="127.0.0.1", port=fake_robot.port))
    summary = written["config"]["confirm_summary"]
    assert "robot (no name)" in summary
    assert await lab.refused("confirm_config", summary=summary) == "robot_name_required"
    assert (await lab.call("lab_overview"))["config"]["status"] == "pending"
  assert fake_robot.log == []


async def test_only_a_server_that_may_drive_a_robot_carries_its_rules(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    forced = lab.client.instructions
  async with open_lab(tmp_path, backend=None) as lab:
    real = lab.client.instructions
  assert "Real hardware" not in forced and "forces simulation" in forced
  assert len(real) < 5000 and real.startswith(forced.split("\nConfig:")[0])
  for text in (forced, real):  # the person's tools: their approval in the client's dialog is the confirmation
    assert "their approval in the client's dialog is the confirmation, so call them right away; do not wait for a typed answer" in text
    assert "record_verdict(check_id, verdict='pass', note=<ask_person>)" in text and "confirm_config(summary=config.confirm_summary)" in text
    assert ("Denied means not confirmed: stop, ask the person what they see, record_verdict(verdict='fail') "
            "with their words as the note.") in text  # after a denial
    assert "Never approve for them or invent their words." in text
    assert "A client with no approval dialog on each call: ask in chat first." in text  # the fallback
    assert "only when the person says so" not in text and "wait for their verdict" not in text
  for rule in ("find_robots or ask the person for its address", "never probe addresses yourself", "homes the robot: hands and deck must be clear",
               "say in chat, in the same message before that call, that approving also means hands and deck are clear "
               "and the robot homes next. With no such dialog, ask in chat.",
               "it returns pending with ask_person; report waiting_for='person', then record_verdict right away",
               "Every verify on real hardware waits for the person, except volumes_match (the digital twin's answer)",
               "never retry a motion call", "removes any tips",
               "- Never reach the robot except through these tools: no curl or other HTTP to it, no ahc-smoke; only the "
               "person releases a stop (never ahc-estop --release).",
               "plate:col2 then plate:col3, one pass each", "call confirm_config(summary=config.confirm_summary) right away",
               "With no config, ask the person whether to simulate or use a real robot",
               "Each new server session also needs their go-ahead", "(lab_overview.session)"):
    assert rule in real, rule
  assert "AHC_PLUGIN_BACKEND" not in real + forced
  async with open_lab(tmp_path, backend=None) as lab:
    tools = {t.name: t for t in (await lab.client.list_tools()).tools}
  assert "starts a new run and homes the robot" in tools["load_layout"].description
  assert "on real hardware every check but volumes_match" in tools["record_verdict"].description
  for name in ("record_verdict", "confirm_config"):  # the tool descriptions say the same as the instructions
    description = tools[name].description
    assert "in the client's dialog" in description and "ask in chat first" in description, name
    assert "Never approve it for them" in description, name
  assert "verdict='pass' and note=verify's ask_person" in tools["record_verdict"].description
  assert "then record verdict='fail' with their words as the note" in tools["record_verdict"].description
  assert "pending with ask_person" in tools["verify"].description
  for tool in ("aspirate", "dispense", "mix"):
    schema = json.dumps(tools[tool].input_schema)
    assert "plate:col2 then plate:col3" in schema
    assert "A head whose channels move together (an 8-channel mount) serves one full column per call, top to bottom; a single-channel mount serves one well per call." in schema


# -- layouts, deck checks and tips on real hardware --------------------------------------------------

async def test_on_real_hardware_a_layout_is_saved_only_once_the_person_confirms_it(tmp_path, open_lab, fake_robot):
  confirmed_config(tmp_path, host="127.0.0.1", port=fake_robot.port, robot_name=fake_robot.name)
  ws = Workspace(tmp_path)
  async with open_lab(tmp_path, backend=None) as lab:
    await confirm(lab)  # the person's go-ahead for this session
    loaded = await lab.call("load_layout", layout=OT2_LAYOUT)
    assert loaded["saved"] is False and "once they confirm it" in loaded["next"]
    check = await lab.call("verify", check="deck_matches_layout")
    for words in ("no tips on either pipette", "lids off, A1 at the back left of each slot", "waiting_for='person'"):
      assert words in check["next"], words
    await lab.call("record_verdict", check_id=check["check_id"], verdict="fail")  # the person rejects it
    found = await lab.call("find_layout_references")
    assert not ws.layout_path("layout-1").exists() and found["layouts"] == [] and found["run_layouts"] == []
    await lab.call("load_layout", layout=OT2_LAYOUT)  # the corrected deck
    check = await lab.call("verify", check="deck_matches_layout")
    passed = await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    assert passed["saved_as"] == "layout-1" and ws.layout_path("layout-1").is_file()
    found = await lab.call("find_layout_references")
  assert [r["name"] for r in found["layouts"]] == ["layout-1"] and len(found["run_layouts"]) == 1
  assert found["next"].endswith("on real hardware only once the person confirms the deck.")
  assert ws.has_reference("opentrons.ot2") and not ws.has_reference("hamilton.starlet")


DECK_POINTS = ("lids off", "A1 at the back left of each slot", "the liquids and their volumes", "nothing else on the deck",
               "no tips on either pipette")


async def test_every_text_about_a_real_deck_says_what_the_person_checks(tmp_path, open_lab, fake_robot):
  confirmed_config(tmp_path, host="127.0.0.1", port=fake_robot.port, robot_name=fake_robot.name)
  async with open_lab(tmp_path, backend=None) as lab:
    texts = {"the check": (await lab.call("list_checks"))["checks"]["deck_matches_layout"],
             "lab_overview": (await lab.call("lab_overview"))["checks"]["deck_matches_layout"],
             "find_layout_references": (await lab.call("find_layout_references"))["next"]}  # no reference yet
    await confirm(lab)  # the person's go-ahead for this session
    await lab.call("load_layout", layout=OT2_LAYOUT)
    texts["verify"] = (await lab.call("verify", check="deck_matches_layout"))["next"]
  for where, text in texts.items():
    for point in DECK_POINTS:
      assert point in text, (where, point)


async def test_lab_overview_says_how_to_stop_this_server(tmp_path, open_lab, monkeypatch):
  root = tmp_path.resolve()
  monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
  async with open_lab(tmp_path, "sim") as lab:
    overview = await lab.call("lab_overview")
  assert "the stop button on the dashboard (open_dashboard opens it)" in overview["emergency_stop"]["how"]
  assert f"uv run --project {REPO} ahc-estop --workspace {root}" in overview["emergency_stop"]["how"] and "warning" not in overview
  assert "power switch" not in overview["emergency_stop"]["how"]  # a simulator stops at once
  elsewhere = tmp_path / "elsewhere"
  elsewhere.mkdir()
  monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(elsewhere))  # Claude Code started in another folder
  async with open_lab(tmp_path, "sim") as lab:
    warning = (await lab.call("lab_overview"))["warning"]
  assert str(elsewhere) in warning and f"ahc-estop --workspace {root}" in warning and "open_dashboard" in warning


async def test_on_a_real_robot_the_stop_says_what_it_cannot_do(tmp_path, open_lab, fake_robot):
  confirmed_config(tmp_path, host="127.0.0.1", port=fake_robot.port, robot_name=fake_robot.name)
  async with open_lab(tmp_path, backend=None) as lab:
    how = (await lab.call("lab_overview"))["emergency_stop"]["how"]
  assert how.endswith(" On a real OT-2 it ends AHC's run, but the robot first finishes the command it is executing (a "
                      "move, an aspirate, a pick-up); the power switch is the immediate stop.")


def test_the_robot_backend_says_how_another_robot_is_refused():
  from ahc.devices.spec import load_device

  robot = load_device("opentrons.ot2").backends["robot"]
  assert robot == ("A real OT-2: its own robot-server on the lab network, over the same HTTP API. Set options.host "
                   "(options.port if not 31950) in the config; configure_devices reads the robot's name into "
                   "options.robot_name and its pipettes into options.mounts, and confirm_config requires the name "
                   "(robot_name_required), so another robot is refused. The person confirms the robot and its "
                   "mounts together.")
