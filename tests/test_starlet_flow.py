"""A real STARlet (backend `usb`) through the MCP tools, end to end: configure, confirm, load, liquid handling, the
emergency stop and the recoveries. The instrument is PyLabRobot's simulator behind the real guard and the real bridge
(`tests/star_shadow.py`). Nothing here reaches ssh, USB or a network. The last tests pin the OT-2's texts."""

import asyncio
import json
import time

import anyio
import pytest

from ahc.core.errors import LabError
from ahc.devices.hamilton import starlet
from ahc.devices.hamilton.link import BridgeIO
from ahc.devices.base import Adapter
from ahc.devices.hamilton.starlet import STARletAdapter
from ahc.devices.opentrons.ot2 import OT2Adapter
from ahc.examples import STARLET_LAYOUT
from ahc.server import confirm_summary
from ahc.verification.ask import HOMING, before_load, confirm_now, pass_now
from ahc.workspace import Config, DeviceEntry, Workspace, estop
from ahc.workspace.config import HEADER
from conftest import confirm_as_person, confirm_pipettes_empty, load_verified
from star_shadow import shadow_instrument

pytestmark = pytest.mark.anyio

DEV = "starlet.pip"
ENTRY = {"id": "starlet", "model": "hamilton.starlet", "backend": "usb", "options": {"bridge": "usb-host"}}
SUMMARY = ("starlet = hamilton.starlet, backend usb, instrument 'T0001' through the USB host 'usb-host', no tip waste "
           "(tips go back to their rack); to stop motion at once, use the instrument's power switch (AHC's stop lets a "
           "command already running finish); task limits: none")
INITIALIZES = "initializes the instrument (its channels, 96-head, iSWAP and autoload all move)"
BEFORE_LOAD = before_load(INITIALIZES, STARletAdapter.LOAD_NEEDS)  # what the person is told before a load


def entry(**options) -> list[dict]:
  return [{**ENTRY, "options": {**ENTRY["options"], **options}}]


class Rig:
  def __init__(self, shadow, arms):
    self.shadow, self.arms = shadow, arms


@pytest.fixture
def make_rig(tmp_path, monkeypatch):
  rigs: list[Rig] = []
  real_arm = BridgeIO.arm

  def make(**instrument) -> Rig:
    folder = tmp_path / f"instrument-{len(rigs)}"
    folder.mkdir()
    shadow = shadow_instrument(folder, monkeypatch, **instrument)
    arms: list[str] = []

    async def arm(self):
      arms.append("arm")
      return await real_arm(self)

    monkeypatch.setattr(BridgeIO, "arm", arm)
    rigs.append(Rig(shadow, arms))
    return rigs[-1]

  yield make
  for rig in rigs:
    rig.shadow.close()


@pytest.fixture
def rig(make_rig):
  return make_rig()


async def until(predicate, timeout: float = 15.0) -> None:
  deadline = time.monotonic() + timeout
  while not predicate():
    assert time.monotonic() < deadline, "timed out waiting"
    await asyncio.sleep(0.02)


@pytest.fixture
async def late_reply_race():
  """PyLabRobot's reply router sets the result of a waiting command's future when its reply arrives, also when the
  caller cancelled that future meanwhile (the stop cancels a load whose command is still in flight): the callback raises
  InvalidStateError in the loop, which is noise (the reply belongs to nobody) and which the test runner would count as a
  failure. Only that one is let through; anything else still reaches the runner's own handler."""
  loop = asyncio.get_running_loop()
  previous = loop.get_exception_handler()

  def handler(loop, context):
    exc, message = context.get("exception"), str(context.get("message"))
    if isinstance(exc, asyncio.InvalidStateError) and "Future.set_result" in message:
      return
    (previous or (lambda loop, context: loop.default_exception_handler(context)))(loop, context)

  loop.set_exception_handler(handler)
  yield
  loop.set_exception_handler(previous)


def folder_of(tmp_path, name: str = "task"):
  path = tmp_path / name
  path.mkdir()
  return path


async def configured(lab, **options):
  written = await lab.call("configure_devices", devices=entry(**options))
  await confirm_as_person(lab)
  return written


async def ready(lab):
  """Configured, confirmed, loaded, the deck passed, tips picked up: the instrument is mid-run."""
  await configured(lab)
  await load_verified(lab, layout=STARLET_LAYOUT)
  await lab.call("pick_up_tips", device=DEV)


def moves(rig: Rig) -> list[str]:
  return rig.shadow.moved()


def run_entries(lab) -> list[dict]:
  return list(lab.server.lab.runlog.entries)


async def press(lab, by: str = "test") -> dict:
  """The stop button: the stop file, then the server has taken it."""
  record = estop.engage(lab.server.lab.ws, by=by)
  await until(lambda: lab.server.lab.estop is not None or lab.server.lab.poll_estop() or lab.server.lab.estop is not None)
  return record


async def acknowledged(lab, record: dict, halt: str | None = None) -> dict:
  """This server's acknowledgement of the stop, once it says `halt` (any answer but pending when none is named)."""
  ws = lab.server.lab.ws

  def answers():
    return [a for a in estop.acknowledgements(ws, record) if (a["halt"] == halt if halt else a["halt"] != "pending")]

  await until(lambda: answers())
  return answers()[0]


def person_releases(lab) -> None:
  estop.release(lab.server.lab.ws, by="person")


async def released(lab) -> None:
  person_releases(lab)
  await until(lambda: lab.server.lab.estop is None)


async def disarmed(rig: Rig) -> bool:
  """Whether the bridge says it is not armed, asked over the link the adapter holds."""
  return (await rig.shadow.bridge.status())["armed"] is False


# -- configure and confirm ------------------------------------------------------------------------

async def test_configure_reads_the_instrument_before_the_person_confirms(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    written = await lab.call("configure_devices", devices=entry())
    config, identity = written["config"], written["device_identity"]
    assert config["status"] == "pending" and config["devices"][0]["options"] == {"bridge": "usb-host", "serial": "T0001"}
    assert identity["serial"] == "T0001" and identity["firmware"] and identity["channels"] == 8
    assert identity["fitted"] == {"head96": True, "iswap": True, "autoload": True}
    assert identity["tips_sensed"] == [0] * 8 and identity["usb_host"] == "usb-host" and identity["initialized"] is False
    assert not any("stop" in key for key in identity)
    assert config["confirm_summary"] == SUMMARY
    assert "serial, firmware, channels and fitted parts" in written["next"]
    assert INITIALIZES in written["next"] and "homes the robot" not in written["next"]
    assert rig.shadow.moved() == [] and rig.arms == [] and rig.shadow.opened == 1  # read-only, never armed
    confirmed = await lab.call("confirm_config", summary=SUMMARY)
    assert confirmed["config"]["status"] == "confirmed" and confirmed["device_identity"] == {}  # no link yet
  on_disk = Workspace(tmp_path / "task").read_config_text()
  assert "serial: T0001" in on_disk and "bridge: usb-host" in on_disk
  assert rig.shadow.moved() == [] and rig.arms == []  # confirming moves nothing either


async def test_configure_refuses_another_serial_and_an_unreachable_host(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    text = await lab.call_text("configure_devices", devices=entry(serial="T0002"))
    assert "[wrong_robot]" in text and "answers the serial 'T0001', not 'T0002'" in text and "nothing was written" in text
    assert not (tmp_path / "task" / ".ahc" / "config.yaml").exists()

    rig.shadow.fail("C0RM")  # discovery's first read fails: the instrument does not answer
    text = await lab.call_text("configure_devices", devices=entry())
    assert "[backend_unreachable]" in text and "Nothing was written." in text
    assert "must be on and its USB host reachable over ssh" in text and "must be on and reachable" not in text
    assert not (tmp_path / "task" / ".ahc" / "config.yaml").exists()
  assert rig.shadow.moved() == [] and rig.arms == []


async def test_confirm_needs_the_exact_summary_and_a_serial(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await lab.call("configure_devices", devices=entry())
    assert await lab.refused("confirm_config", summary="starlet = hamilton.starlet") == "summary_mismatch"
    assert await lab.refused("confirm_config") == "summary_mismatch"
    await lab.call("confirm_config", summary=SUMMARY)
  by_hand = folder_of(tmp_path, "by-hand")  # an entry written by hand: nothing read the instrument's serial
  Workspace(by_hand).write_config(Config(devices=[DeviceEntry("starlet", "hamilton.starlet", "usb",
                                                              {"bridge": "usb-host"})], limits={}).data(), HEADER)
  async with open_lab(by_hand, backend=None) as lab:
    summary = (await lab.call("lab_overview"))["config"]["confirm_summary"]
    assert "instrument (serial not read) through the USB host 'usb-host'" in summary
    text = await lab.call_text("confirm_config", summary=summary)
    assert "[serial_required]" in text and "names no serial" in text and "configure_devices" in text
    assert (await lab.call("lab_overview"))["config"]["status"] == "pending"
  assert rig.shadow.moved() == []


# -- the session's go-ahead, in the instrument's words --------------------------------------------

async def test_the_go_ahead_and_every_text_before_the_load_say_initializes(tmp_path, open_lab, rig):
  folder = folder_of(tmp_path)
  config = Config(devices=[DeviceEntry("starlet", "hamilton.starlet", "usb", {"bridge": "usb-host", "serial": "T0001"})],
                  limits={})
  config.confirm("human")  # confirmed in an earlier session
  Workspace(folder).write_config(config.data(), HEADER)
  async with open_lab(folder, backend=None) as lab:
    overview = await lab.call("lab_overview")
    assert overview["session"] == {"go_ahead": "needed", "why": "load_layout initializes the real instrument 'T0001' "
                                   "through the USB host 'usb-host': every arm moves"}
    assert BEFORE_LOAD in overview["next"] and "homes the robot" not in overview["next"]
    assert overview["config"]["confirm_summary"] == SUMMARY
    refused = await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    assert "[session_unconfirmed]" in refused and BEFORE_LOAD in refused
    assert "homes the robot" not in refused and "OT-2" not in refused
    assert "hands and deck are clear and they are within reach of the instrument's power switch" in refused
    await confirm_as_person(lab)
    assert (await lab.call("lab_overview"))["session"]["go_ahead"] == "given"
  assert rig.shadow.codes() == [] and rig.arms == []  # nothing was even read


# -- load and liquid handling ---------------------------------------------------------------------

async def test_the_whole_run_from_load_to_the_volumes_check(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await configured(lab)
    before = len(rig.shadow.codes())
    loaded = await lab.call("load_layout", layout=STARLET_LAYOUT)
    assert rig.arms == ["arm"] and len(moves(rig)) > 20  # initialization moved: channels, 96-head, iSWAP, autoload
    assert loaded["gate"]["state"] == "pending" and loaded["saved"] is False and len(rig.shadow.codes()) > before
    assert await lab.refused("pick_up_tips", device=DEV) == "gate_pending"  # the deck check waits for the person
    sent = len(moves(rig))
    check = await lab.call("verify", check="deck_matches_layout")
    assert check["verdict"] == "pending" and "ask_person" in check
    assert len(moves(rig)) == sent
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    await lab.call("pick_up_tips", device=DEV)
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=120)
    await lab.call("dispense", device=DEV, targets=["plate:A2:H2"], volumes=60)
    await lab.call("mix", device=DEV, targets=["plate:A2:H2"], volume=30, repetitions=2)
    await lab.call("dispense", device=DEV, targets=["plate:A3:H3"], volumes=40)
    assert await lab.refused("drop_tips", device=DEV, mode="discard") == "no_tip_waste"
    await lab.call("drop_tips", device=DEV, mode="return")
    assert (await lab.call("lab_overview"))["emergency_stop"]["how"].endswith(STARletAdapter.STOP_NOTE)
    state = await lab.call("get_state")
    assert state["tips_mounted"] == {DEV: 0} and state["tips_returned"]
    volumes = await lab.call("verify", check="volumes_match", expected={"plate:A2:H2": 60, "plate:A3:H3": 40})
    if volumes["verdict"] == "pending":
      volumes = await lab.call("record_verdict", check_id=volumes["check_id"], verdict="pass")
    assert volumes["verdict"] == "pass"
    entries = run_entries(lab)
    assert lab.server.lab.adapter.command_count() == len(moves(rig))
  device = [e for e in entries if e.get("type") == "device"]
  assert device and device[0]["serial"] == "T0001" and device[0]["backend"] == "usb" and device[0]["usb_host"] == "usb-host"
  assert [e["op"] for e in entries if e.get("op") in ("pick_up_tips", "aspirate", "dispense", "mix", "drop_tips")
          and e.get("status") == "ok"] == ["pick_up_tips", "aspirate", "dispense", "mix", "dispense", "drop_tips"]


async def test_a_second_layout_on_the_same_link_moves_nothing(tmp_path, open_lab, rig):
  other = json.loads(json.dumps(STARLET_LAYOUT))
  for carrier in other["carriers"]:
    carrier["track"] = {"plate_car": 13, "trough_car": 7}.get(carrier["name"], carrier["track"])
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await configured(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)
    sent, opened = list(moves(rig)), rig.shadow.opened
    await load_verified(lab, layout=other)
    assert moves(rig) == sent and rig.shadow.opened == opened and rig.arms == ["arm"]
    await lab.call("pick_up_tips", device=DEV)  # and the instrument works on the new deck


async def test_tips_go_back_to_their_rack_unless_the_confirmed_config_says_a_waste_is_fitted(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await ready(lab)
    sent = list(moves(rig))
    text = await lab.call_text("drop_tips", device=DEV, mode="discard")
    assert "[no_tip_waste]" in text and "mode='return'" in text and moves(rig) == sent
    await lab.call("drop_tips", device=DEV, mode="return")
    assert len(moves(rig)) > len(sent)
  async with open_lab(folder_of(tmp_path, "waste"), backend=None) as lab:
    written = await lab.call("configure_devices", devices=entry(tip_waste=True))
    assert written["config"]["confirm_summary"] == SUMMARY.replace("no tip waste (tips go back to their rack)",
                                                                  "tip waste fitted")
    await confirm_as_person(lab)
    await lab.call("load_layout", layout=STARLET_LAYOUT)
    check = await lab.call("verify", check="deck_matches_layout")
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    await lab.call("pick_up_tips", device=DEV)
    discarded = len(moves(rig))
    await lab.call("drop_tips", device=DEV, mode="discard")
    assert len(moves(rig)) > discarded


# -- refusals at the load, nothing moved ----------------------------------------------------------

async def test_tips_sensed_before_initialization_refuse_the_load(tmp_path, open_lab, make_rig):
  rig = make_rig(tips_mounted=[True] + [False] * 7)
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    written = await configured(lab)
    assert written["device_identity"]["tips_sensed"] == [1] + [0] * 7
    text = await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    assert "[tips_on_channels]" in text and "channel(s) 1" in text
    assert moves(rig) == [] and rig.arms == []


async def test_another_serial_at_the_load_than_the_confirmed_one_moves_nothing(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await configured(lab)
    rig.shadow.serial = "T0002"  # another instrument is now plugged into that host
    text = await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    assert "[wrong_robot]" in text and "'T0002'" in text and "'T0001'" in text
    assert moves(rig) == [] and rig.arms == []
    assert (await lab.call("lab_overview"))["gate"] is None


async def test_two_servers_on_one_serial_never_drive_it_together(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path, "first"), backend=None) as first, \
      open_lab(folder_of(tmp_path, "second"), backend=None) as second:
    await configured(first)
    await configured(second)  # both read it before either holds a link
    await first.call("load_layout", layout=STARLET_LAYOUT)
    sent = list(moves(rig))
    text = await second.call_text("load_layout", layout=STARLET_LAYOUT)
    assert "[robot_in_use]" in text and "'T0001'" in text
    assert moves(rig) == sent and rig.shadow.opened == 3  # two reads and the first server's link


async def test_configure_asks_the_link_the_server_holds_instead_of_opening_a_second(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await configured(lab)
    await lab.call("load_layout", layout=STARLET_LAYOUT)
    opened = rig.shadow.opened
    written = await lab.call("configure_devices", devices=entry(tip_waste=True))
    assert rig.shadow.opened == opened  # no second connection
    assert written["device_identity"]["serial"] == "T0001" and written["config"]["devices"][0]["options"]["serial"] == "T0001"
    text = await lab.call_text("configure_devices", devices=entry(serial="T0002"))
    assert "[wrong_robot]" in text and rig.shadow.opened == opened


async def test_forced_simulation_runs_on_the_simulator_and_never_opens_the_instrument(tmp_path, open_lab, rig, monkeypatch):
  folder = folder_of(tmp_path)
  config = Config(devices=[DeviceEntry("starlet", "hamilton.starlet", "usb", {"bridge": "usb-host", "serial": "T0001"})],
                  limits={})
  config.confirm("human")
  Workspace(folder).write_config(config.data(), HEADER)

  def never(options, deck):
    raise AssertionError("the instrument was opened")

  monkeypatch.setattr(starlet, "open_instrument", never)
  Workspace(folder).save_layout("reference", STARLET_LAYOUT, "hamilton.starlet")
  async with open_lab(folder, backend="sim") as lab:
    overview = await lab.call("lab_overview")
    assert overview["forced_simulation"] and overview["simulated"] and overview["backend"] == "sim"
    await lab.call("load_layout", layout=STARLET_LAYOUT)
    check = await lab.call("verify", check="deck_matches_layout")
    assert check["verdict"] == "pass"
    await lab.call("pick_up_tips", device=DEV)
  assert rig.shadow.codes() == [] and rig.shadow.opened == 0


# -- the emergency stop ---------------------------------------------------------------------------

async def test_a_stop_during_a_call_ends_it_interrupted_and_the_recovery_initializes_again(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await ready(lab)
    rig.shadow.hold("C0AS")
    outcome: dict = {}

    async def aspirate():
      outcome["text"] = await lab.call_text("aspirate", device=DEV, targets=["diluent"], volumes=50)

    async with anyio.create_task_group() as tg:
      tg.start_soon(aspirate)
      await until(lambda: "C0AS" in rig.shadow.codes())
      record = await press(lab)
      await asyncio.sleep(0.3)
      rig.shadow.release("C0AS")  # the command in flight is answered; nothing after it is sent
    assert "[emergency_stop]" in outcome["text"] and "outcome on the robot is unknown" in outcome["text"]
    sent = list(moves(rig))
    assert sent[-1] == "C0AS"
    ack = await acknowledged(lab, record, "stopped")
    assert ack["device"]["model"] == "hamilton.starlet" and ack["device"]["robot"] == "T0001"
    assert ack["detail"] == "nothing more is sent to the instrument and no command is in flight"
    assert await disarmed(rig)  # AHC's stop closed the bridge over the open link
    assert await lab.refused("dispense", device=DEV, targets=["plate:A2:H2"], volumes=10) == "emergency_stop"
    assert moves(rig) == sent  # nothing that moves went out afterwards

    await released(lab)
    text = await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    assert "[tips_unconfirmed]" in text and moves(rig) == sent
    assert BEFORE_LOAD in text and "homes the robot" not in text
    await confirm_pipettes_empty(lab)
    opened = rig.shadow.opened
    await load_verified(lab, layout=STARLET_LAYOUT)  # a new load: a fresh link, armed and initialized again
    assert rig.shadow.opened == opened + 1 and (await rig.shadow.bridge.status())["armed"] is True
    assert len(moves(rig)) > len(sent) + 20 and rig.arms == ["arm", "arm"]
    await lab.call("pick_up_tips", device=DEV)  # and the experiment goes on


async def test_a_stop_during_initialization_refuses_the_load_and_the_recovery_works(tmp_path, open_lab, rig,
                                                                                 late_reply_race):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await configured(lab)
    rig.shadow.hold("C0DI")
    outcome: dict = {}

    async def loading():
      outcome["text"] = await lab.call_text("load_layout", layout=STARLET_LAYOUT)

    async with anyio.create_task_group() as tg:
      tg.start_soon(loading)
      await until(lambda: "C0DI" in rig.shadow.codes())
      record = await press(lab)
      await asyncio.sleep(0.3)
      rig.shadow.release("C0DI")
    assert "[emergency_stop]" in outcome["text"]
    sent = list(moves(rig))
    await acknowledged(lab, record)
    assert moves(rig) == sent and (await lab.call("lab_overview"))["gate"] is None
    assert await disarmed(rig)
    await released(lab)
    assert "[tips_unconfirmed]" in await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    await confirm_pipettes_empty(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)
    assert len(moves(rig)) > len(sent) and (await rig.shadow.bridge.status())["armed"] is True


async def test_a_stop_with_nothing_loaded_reaches_no_instrument_and_the_load_goes_on(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await configured(lab)
    opened = rig.shadow.opened
    record = await press(lab)
    ack = await acknowledged(lab, record)
    assert ack["halt"] == "no_run" and ack["run_active"] is False  # no link: nothing running
    assert rig.shadow.opened == opened and moves(rig) == [] and rig.arms == []  # the stop opened nothing
    await released(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)
    assert len(moves(rig)) > 20 and rig.arms == ["arm"]


async def test_two_stops_in_a_row_need_only_the_tips_check_and_a_load(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await ready(lab)
    first = await press(lab)
    await acknowledged(lab, first, "stopped")
    assert await disarmed(rig)
    await released(lab)
    second = await press(lab)  # no load in between
    await acknowledged(lab, second, "stopped")
    assert first["id"] != second["id"] and await disarmed(rig)
    await released(lab)
    sent = list(moves(rig))
    assert "[tips_unconfirmed]" in await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    await confirm_pipettes_empty(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)
    assert len(moves(rig)) > len(sent) + 20 and rig.arms == ["arm", "arm"]


async def test_a_stop_in_a_pick_up_leaves_tips_on_and_recover_tips_puts_them_back_without_initializing(
    tmp_path, open_lab, make_rig, late_reply_race):
  rig = make_rig(carry_tips=True)  # the instrument outlives a link: its channels keep the tips, and its initialization
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await configured(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)
    rig.shadow.hold("C0TP")
    outcome: dict = {}

    async def pick_up():
      outcome["text"] = await lab.call_text("pick_up_tips", device=DEV, tips=["tips:col3"])

    async with anyio.create_task_group() as tg:
      tg.start_soon(pick_up)
      await until(lambda: "C0TP" in rig.shadow.codes())
      record = await press(lab)
      await asyncio.sleep(0.3)
      rig.shadow.release("C0TP")  # the command in flight finishes: the tips are on the channels
    assert "[emergency_stop]" in outcome["text"]
    await acknowledged(lab, record, "stopped")
    await released(lab)
    # The firmware command in flight finished, so the channels hold the tips; the shadow's simulator learns of a command only
    # when its reply comes back to a caller that the stop had cancelled, so the test says what the instrument holds.
    rig.shadow.tips_mounted = [True] * 8
    text = await lab.call_text("load_layout", layout=STARLET_LAYOUT)  # no way out by hand: the text names recover_tips
    assert "[tips_unconfirmed]" in text and "recover_tips" in text
    standing = {**STARLET_LAYOUT, "tips_empty": ["tips:A3:H3"]}  # the racks as they are, the spots the tips came from
    assert await lab.refused("recover_tips", device=DEV, tips="tips:col3", mode="discard", layout=standing) == "no_tip_waste"
    opened, sent, armed = rig.shadow.opened, len(moves(rig)), len(rig.arms)
    result = await lab.call("recover_tips", device=DEV, tips="tips:col3", mode="return", layout=standing)
    assert result["moved"] is True and result["mode"] == "return" and not any(result["sensed_after"])
    assert any(result["sensed_before"]) and result["commands"] == len(moves(rig)) - sent
    recovery = moves(rig)[sent:]  # the safety raise, then the returns; no initialization and no pick-up
    assert recovery[:2] == ["C0ZA", "C0EV"] and set(recovery[2:]) == {"C0TR"}
    assert len(rig.arms) == armed + 1  # the recovery's own link armed once, for the move
    assert "tips_returned" in result["next"] and "verify(check='tips_mounted', expect='none')" in result["next"]
    assert rig.shadow.opened == opened + 1  # one link of its own, and none left open for the server's run
    assert "[tips_unconfirmed]" in await lab.call_text("load_layout", layout=STARLET_LAYOUT)  # the doubt stands
    await confirm_pipettes_empty(lab)
    await load_verified(lab, layout={**STARLET_LAYOUT, "tips_returned": ["tips:A3:H3"]})
    assert len(rig.arms) == armed + 2  # the new load armed again (and initialized: the person's check came first)
    picked = await lab.call("pick_up_tips", device=DEV, tips=["tips:col4"])
    assert len(rig.shadow.sent("C0TP")) == 2 and (await lab.call("get_state"))["tips_mounted"] == {DEV: 8}
    assert picked  # the experiment goes on, with the next column


# -- errors and a lost link ----------------------------------------------------------------------

async def test_a_command_that_fails_in_an_aspirate_closes_motion_until_the_person_checks_and_a_load_initializes(
    tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await ready(lab)
    sent = len(moves(rig))
    rig.shadow.fail("C0AS")
    text = await lab.call_text("aspirate", device=DEV, targets=["diluent"], volumes=50)
    assert "[completion_unknown]" in text and "C0AS" in text
    logged = [e for e in run_entries(lab) if e.get("op") == "aspirate" and e.get("status") != "ok"][-1]
    assert {"command": "C0AS", "status": "error er01/00"} in logged["robot_record"]
    after = list(moves(rig))
    assert len(after) > sent
    assert await lab.refused("dispense", device=DEV, targets=["plate:A2:H2"], volumes=10) == "completion_unknown"
    assert moves(rig) == after
    assert "[tips_unconfirmed]" in await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    await confirm_pipettes_empty(lab)
    opened = rig.shadow.opened
    rig.shadow.fail("C0AS", "er00/00")
    await load_verified(lab, layout=STARLET_LAYOUT)
    assert rig.shadow.opened == opened + 1 and len(moves(rig)) > len(after) + 20


async def test_a_link_lost_while_idle_is_refused_at_the_next_call_and_recovered_by_a_load(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await ready(lab)
    rig.shadow.kill()
    await until(lambda: lab.server.lab.adapter.outcome_unknown)
    says = lab.server.lab.adapter.outcome_unknown["says"]
    assert "the link to the USB host was lost" in says
    sent = list(moves(rig))
    code = await lab.refused("aspirate", device=DEV, targets=["diluent"], volumes=50)
    assert code in ("completion_unknown", "link_lost") and moves(rig) == sent
    overview = await lab.call("lab_overview")  # told in its own words, not as the outcome of one call
    assert says[0].upper() + says[1:] in overview["next"] and "check the instrument" in overview["next"]
    assert "the outcome of" not in overview["next"].lower().split(says.lower())[0]
    text = await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    assert "[tips_unconfirmed]" in text and says in text
    await confirm_pipettes_empty(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)
    assert rig.shadow.opened == 3 and len(moves(rig)) > len(sent) + 20  # the read, the lost link, the new one


# -- disconnect ----------------------------------------------------------------------------------

async def test_disconnect_lets_go_without_moving_and_the_next_load_needs_the_go_ahead(tmp_path, open_lab, rig):
  async with open_lab(folder_of(tmp_path), backend=None) as lab:
    await configured(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)
    sent = list(moves(rig))
    answer = await lab.call("disconnect")
    assert answer["disconnected"] is True and answer["run_ended_confirmed"] is None
    assert answer["next"].startswith("This server let go of the instrument: its link is closed and nothing was moved")
    assert moves(rig) == sent and rig.shadow.procs[-1].wait(10) == 0
    assert await lab.refused("pick_up_tips", device=DEV) == "disconnected"
    assert (await lab.call("lab_overview"))["session"]["go_ahead"] == "needed"
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "session_unconfirmed"
    await confirm_as_person(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)  # a fresh link, initialized again
    assert rig.shadow.opened == 3 and len(moves(rig)) > len(sent) + 20


# -- the OT-2's texts, as they were before the adapters said them ---------------------------------

def test_the_ot2s_texts_are_the_ones_the_real_robot_accepted():
  def summary(**options) -> str:
    return confirm_summary(Config(devices=[DeviceEntry("ot2", "opentrons.ot2", "robot", options)], limits={}))

  mounts = {"left": "p20_single_gen2", "right": "p300_multi_gen2"}
  assert summary(host="127.0.0.1", port=1234, robot_name="lab-ot2", mounts=mounts) == (
    "ot2 = opentrons.ot2, backend robot, robot 'lab-ot2' at 127.0.0.1:1234, mounts left p20_single_gen2, right "
    "p300_multi_gen2; task limits: none")
  assert summary(host="127.0.0.1") == (
    "ot2 = opentrons.ot2, backend robot, robot (no name) at 127.0.0.1:31950, mounts left p20_single_gen2, right "
    "p300_multi_gen2; task limits: none")
  assert summary(host="127.0.0.1", robot_name="x") == (
    "ot2 = opentrons.ot2, backend robot, robot 'x' at 127.0.0.1:31950, mounts left p20_single_gen2, right "
    "p300_multi_gen2; task limits: none")
  assert summary() == ("ot2 = opentrons.ot2, backend robot, robot (no name), mounts left p20_single_gen2, right "
                       "p300_multi_gen2; task limits: none")
  assert HOMING == ("load_layout homes the robot next, so in that same message, before the call, tell the person that "
                    "approving also means hands and deck are clear.")
  asked = ("Report waiting_for='person', then call record_verdict(check_id='c1', verdict='pass', "
           "note=<ask_person>) now: the person's approval in the dialog is their confirmation; never approve for them.")
  assert pass_now("c1") == asked and pass_now("c1", homes=True) == f"{asked} {HOMING}"
  config = ("Report waiting_for='person', then call confirm_config(summary=config.confirm_summary) now: the person's "
            "approval in the dialog is their confirmation (denied: not confirmed; stop and ask what they want). Never "
            "approve for them. A client that shows no approval dialog on each call: ask in chat first.")
  assert confirm_now() == config and confirm_now(homes=True) == f"{config} {HOMING}"
  assert before_load("homes the robot") == HOMING == before_load("homes the robot", "hands and deck are clear")
  assert Adapter.LOAD_NEEDS == OT2Adapter.LOAD_NEEDS == "hands and deck are clear"
  assert pass_now("c1", homes=True, moves=OT2Adapter.LOAD_MOVES, needs=OT2Adapter.LOAD_NEEDS) == f"{asked} {HOMING}"
  assert confirm_now(homes=True, moves=OT2Adapter.LOAD_MOVES, needs=OT2Adapter.LOAD_NEEDS) == f"{config} {HOMING}"
  assert STARletAdapter.LOAD_MOVES == INITIALIZES
  assert BEFORE_LOAD == ("load_layout initializes the instrument (its channels, 96-head, iSWAP and autoload all move) "
                         "next, so in that same message, before the call, tell the person that approving also means "
                         "hands and deck are clear and they are within reach of the instrument's power switch.")
