"""The STARlet adapter's real backend (`usb`): a real instrument through the bridge on its USB host, here PyLabRobot's
simulator behind the real guard (`tests/star_shadow.py`). `sim_instrument` has a fake bridge handle; `shadow_instrument`
also puts every command through the real transport, a spawned bridge and its fake instrument. Nothing here reaches ssh,
USB or a network."""

import asyncio
import copy
import re
import subprocess
import sys
import time
from dataclasses import replace
from itertools import pairwise

import pytest
from pylabrobot.resources import Coordinate, set_tip_tracking, set_volume_tracking

from ahc.core.capability import LiquidHandling
from ahc.core.errors import LabError
from ahc.devices.hamilton import link, starlet
from ahc.devices.hamilton.link import BridgeLost, BridgeRefused, GuardRefused
from ahc.devices.hamilton.starlet import STARletAdapter
from ahc.devices.spec import load_device
from ahc.examples import STARLET_LAYOUT
from ahc.records import RunLog
from star_shadow import shadow_instrument, sim_instrument

pytestmark = pytest.mark.anyio

DEV = "starlet.pip"
OPTIONS = {"bridge": "usb-host", "serial": "T0001"}


def other_layout() -> dict:
  """STARLET_LAYOUT with the troughs and the plate carrier swapped on the deck: the same labware, other tracks."""
  layout = copy.deepcopy(STARLET_LAYOUT)
  for carrier in layout["carriers"]:
    carrier["track"] = {"plate_car": 13, "trough_car": 7}.get(carrier["name"], carrier["track"])
  return layout


class Rig:
  def __init__(self, instrument, adapter, lh):
    self.instrument, self.adapter, self.lh = instrument, adapter, lh

  async def load(self, layout=None):
    return await self.adapter.load_layout(layout or STARLET_LAYOUT)

  async def ready(self, tips: bool = True):
    await self.load()
    if tips:
      await self.lh.pick_up_tips(DEV, None)

  async def refused(self, op: str, *args, **kwargs) -> str:
    with pytest.raises(LabError) as raised:
      await getattr(self.lh, op)(*args, **kwargs)
    return raised.value.code


@pytest.fixture(autouse=True)
def tracking():
  set_tip_tracking(True)  # as the server does: the twin's volumes follow what PyLabRobot does
  set_volume_tracking(True)


@pytest.fixture
async def make(tmp_path, monkeypatch):
  """`make("sim" | "shadow", options=..., **instrument)` -> a Rig. Everything is closed afterwards."""
  rigs: list[Rig] = []

  def build(kind: str = "sim", options: dict | None = None, **instrument) -> Rig:
    if kind == "shadow":
      inst = shadow_instrument(tmp_path, monkeypatch, **instrument)
    else:
      inst = sim_instrument(**instrument)
      monkeypatch.setattr(starlet, "open_instrument", inst)
    spec = replace(load_device("hamilton.starlet"), id="starlet")
    adapter = STARletAdapter(spec, "usb", {**OPTIONS, **(options or {})})
    rig = Rig(inst, adapter, LiquidHandling(spec, adapter, RunLog(tmp_path / "runs", "starlet", "usb")))
    rigs.append(rig)
    return rig

  yield build
  for rig in rigs:
    try:
      await asyncio.wait_for(rig.adapter.stop(), 20)
    finally:
      if hasattr(rig.instrument, "close"):
        rig.instrument.close()


async def started(rig: Rig) -> Rig:
  await rig.adapter.start()
  return rig


async def until(predicate, timeout: float = 10.0) -> None:
  deadline = time.monotonic() + timeout
  while not predicate():
    assert time.monotonic() < deadline, "timed out waiting"
    await asyncio.sleep(0.01)


async def ended_by_the_stop(call) -> None:
  """A call cancelled by the emergency stop ends as a cancellation, also when PyLabRobot's own cleanup (a retract after
  the interrupted command) was refused by the closed guard: that refusal must not replace the cancellation."""
  with pytest.raises(asyncio.CancelledError):
    await call


# -- the options of a usb entry -----------------------------------------------------------------

@pytest.mark.parametrize("options", [
  {"bridge": "usb-host"}, {"bridge": "pi@usb-host.local", "serial": "T0001"},
  {"bridge": "usb-host", "bridge_python": "~/venv/bin/python", "bridge_script": "~/star_bridge.py", "tip_waste": True},
  {"bridge": "usb-host", "tip_waste": False},
])
def test_entries_the_usb_backend_runs(options):
  STARletAdapter.check_options("usb", options)


@pytest.mark.parametrize("options, code", [
  ({}, "bridge_required"), ({"serial": "T0001"}, "bridge_required"), ({"bridge": ""}, "bridge_required"),
  ({"bridge": "-oProxyCommand=x"}, "bad_option"), ({"bridge": "host name"}, "bad_option"),
  ({"bridge": 5}, "bad_option"),
  ({"bridge": "usb-host", "bridge_python": "python3; ls"}, "bad_option"),
  ({"bridge": "usb-host", "bridge_script": "-s.py"}, "bad_option"),
  ({"bridge": "usb-host", "serial": ""}, "bad_option"), ({"bridge": "usb-host", "serial": 1}, "bad_option"),
  ({"bridge": "usb-host", "tip_waste": "yes"}, "bad_option"), ({"bridge": "usb-host", "tip_waste": 1}, "bad_option"),
  ({"bridge": "usb-host", "host": "10.0.0.5"}, "bad_option"),
])
def test_entries_the_usb_backend_refuses(options, code):
  with pytest.raises(LabError) as refused:
    STARletAdapter.check_options("usb", options)
  assert refused.value.code == code


def test_a_backend_that_does_not_exist_is_still_not_implemented_and_sim_takes_no_options():
  with pytest.raises(LabError) as refused:
    STARletAdapter.check_options("serial", {})
  assert refused.value.code == "backend_not_implemented"
  with pytest.raises(LabError) as refused:
    STARletAdapter.check_options("sim", {"bridge": "usb-host"})
  assert refused.value.code == "bad_option"
  STARletAdapter.check_options("sim", {})


def test_describe_entry_in_its_three_forms_and_it_never_raises():
  describe = STARletAdapter.describe_entry
  power_switch = "; to stop motion at once, use the instrument's power switch (AHC's stop lets a command already running finish)"
  assert describe({"bridge": "usb-host", "serial": "T0001"}) == (
    ", instrument 'T0001' through the USB host 'usb-host', no tip waste (tips go back to their rack)" + power_switch)
  assert describe({"bridge": "usb-host", "serial": "T0001", "tip_waste": True}) == (
    ", instrument 'T0001' through the USB host 'usb-host', tip waste fitted" + power_switch)
  assert describe({"bridge": "usb-host"}) == (
    ", instrument (serial not read) through the USB host 'usb-host', no tip waste (tips go back to their rack)"
    + power_switch)
  assert describe(None).startswith(", instrument") and describe({"serial": ["x"]}).startswith(", instrument")
  assert describe(None).endswith(power_switch) and describe({"serial": ["x"]}).endswith(power_switch)


def test_the_stop_note_is_the_sentence_of_the_description_file():
  spec = load_device("hamilton.starlet")
  assert STARletAdapter.STOP_NOTE == spec.raw["emergency_stop"]["usb"]
  assert "power switch" in STARletAdapter.STOP_NOTE and "USB host" not in STARletAdapter.STOP_NOTE
  assert set(spec.raw["emergency_stop"]) == {"usb"}
  assert "emergency_stop" not in load_device("opentrons.ot2").raw  # no other description file has the key


def test_the_stars_texts_say_nothing_of_a_stop_on_the_usb_host():
  for text in (STARletAdapter.HALTED, STARletAdapter.STOPPED, STARletAdapter.STOP_UNCONFIRMED,
               STARletAdapter.STOP_NOTE, STARletAdapter.IDENTITY_SAYS):
    assert "stop on" not in text and "holds the stop" not in text and "host's stop" not in text
  assert STARletAdapter.HALTED == "nothing more is sent to the instrument and no command is in flight"
  assert STARletAdapter.LOAD_NEEDS == "hands and deck are clear and they are within reach of the instrument's power switch"
  assert STARletAdapter.pinned({"serial": "T0001"}) == "T0001" and STARletAdapter.pinned({}) is None


async def test_the_usb_backend_starts_nothing(make):
  rig = await started(make("sim"))
  assert rig.instrument.opened == 0 and rig.adapter.device is None and rig.adapter.identity() == {}
  assert rig.adapter.command_count() == 0 and rig.adapter.robot_record(3) is None and rig.adapter.mounted_tips(
    rig.adapter.spec.components["pip"]) == []
  assert await rig.adapter.sense(rig.adapter.spec.components["pip"], "tips_mounted") is None


# -- probe_identity -----------------------------------------------------------------------------

async def test_the_probe_reads_who_answers_and_sends_only_reads(make, monkeypatch):
  rig = make("shadow")
  found = await asyncio.to_thread(STARletAdapter.probe_identity, "usb", dict(OPTIONS))
  assert found["serial"] == "T0001" and found["channels"] == 8 and found["usb_host"] == "usb-host"
  assert found["firmware"].startswith("7.6S") and found["firmware_date"] == "2021-11-05"
  assert found["fitted"] == {"head96": True, "iswap": True, "autoload": True}
  assert found["initialized"] is False and found["tips_sensed"] == [0] * 8 and not any("stop" in key for key in found)
  assert rig.instrument.codes() and rig.instrument.moved() == [] and rig.instrument.state.moved == 0
  assert rig.instrument.state.mode == "read_only"  # never opened
  assert rig.instrument.procs[0].wait(10) == 0  # the link closed
  assert STARletAdapter.probe_identity("sim", {}) == {}


async def test_the_probe_never_arms_and_closes_the_link_also_when_discovery_fails(make):
  rig = make("sim", tips_mounted=[True] + [False] * 7, initialized=True)
  found = await asyncio.to_thread(STARletAdapter.probe_identity, "usb", dict(OPTIONS))
  assert found["initialized"] is True and found["tips_sensed"] == [1] + [0] * 7
  assert ("arm",) not in rig.instrument.bridge.calls and rig.instrument.bridge.closed
  assert rig.instrument.moved() == []

  rig.instrument.fail("C0RM")  # discovery's first read fails
  with pytest.raises(LabError) as raised:
    await asyncio.to_thread(STARletAdapter.probe_identity, "usb", dict(OPTIONS))
  assert raised.value.code == "backend_unreachable" and "RuntimeError" in str(raised.value)
  assert rig.instrument.opened == 2 and rig.instrument.bridge.closed and rig.instrument.moved() == []


async def test_the_probe_passes_a_link_error_through_and_says_when_no_serial_answered(make, monkeypatch):
  def in_use(argv):
    return subprocess.Popen([sys.executable, "-c", "import sys; print('{\"op\":\"fatal\",\"code\":\"in_use\","
                             "\"detail\":\"made up\"}'); sys.exit(2)"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)

  monkeypatch.setattr(link, "spawn", in_use)
  with pytest.raises(LabError) as raised:
    await asyncio.to_thread(STARletAdapter.probe_identity, "usb", dict(OPTIONS))
  assert raised.value.code == "bridge_in_use"
  make("sim", serial="")
  with pytest.raises(LabError) as raised:
    await asyncio.to_thread(STARletAdapter.probe_identity, "usb", dict(OPTIONS))
  assert raised.value.code == "identity_unreadable" and "nothing was written" in str(raised.value)
  with pytest.raises(LabError) as raised:
    await asyncio.to_thread(STARletAdapter.probe_identity, "usb", {})
  assert raised.value.code == "bridge_required"


# -- the first load -----------------------------------------------------------------------------

async def test_the_first_load_connects_then_initializes(make):
  rig = await started(make("shadow"))
  adapter, shadow = rig.adapter, rig.instrument
  layout = await rig.load()
  assert shadow.opened == 1 and shadow.state.moved > 0 and shadow.state.mode == "open"
  assert (await shadow.bridge.status())["armed"] is True  # the bridge on the host was armed
  assert adapter.layout is layout and adapter.device is shadow.device and adapter.view_root() is shadow.device
  assert adapter.identity() == {"serial": "T0001", "firmware": "7.6S 25 2021_11_05 (GRU C0)", "channels": 8,
                                "fitted": {"head96": True, "iswap": True, "autoload": True}, "usb_host": "usb-host",
                                "firmware_date": "2021-11-05"}
  assert adapter.command_count() == shadow.state.moved == len(shadow.moved())  # reads do not count
  assert adapter.robot_record(2) == [{"command": c, "status": "answered"} for c in shadow.moved()[-2:]]
  assert layout.labware["tips"] is not None and shadow.driver.deck is not None
  assert adapter.has_stop and adapter.outcome_unknown is None and not adapter.halted


async def test_the_96_head_is_sent_to_the_slide_waste_at_its_travel_height_and_never_lower(make):
  rig = await started(make("shadow"))
  await rig.load()
  ejects = rig.instrument.sent("C0EI")
  assert len(ejects) == 1  # the one command that ejects on the 96 head
  for field in ("xs00420", "xd1", "yh1203", "za2450", "ze2450"):
    assert field in ejects[0]
  assert "xs00465" not in ejects[0] and "yh1788" not in ejects[0]  # PyLabRobot's own waste is not used
  head = rig.instrument.driver.arms[0].head96
  assert head.configuration.tip_discard_location == Coordinate(-42.0, 120.3, head.configuration.traversal_z_position)
  assert (starlet.HEAD96_EJECT_X, starlet.HEAD96_EJECT_Y) == (-42.0, 120.3)


async def test_a_96_head_without_a_travel_height_refuses_the_load_before_the_bridge_is_armed(make, monkeypatch):
  rig = await started(make("shadow"))
  read = starlet._read_identity

  def without_height(driver, options):
    for arm in driver.arms:
      if arm.head96 is not None:
        arm.head96.configuration.traversal_z_position = None
    return read(driver, options)

  monkeypatch.setattr(starlet, "_read_identity", without_height)
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "head96_unreadable" and "nothing was moved" in str(raised.value)
  assert "Tell the person" in raised.value.hint
  shadow = rig.instrument
  assert shadow.sent("C0EI") == [] and shadow.moved() == [] and shadow.state.moved == 0
  assert (await shadow.bridge.status())["armed"] is False  # never armed
  assert rig.adapter.layout is None and rig.adapter.device is None


async def test_an_instrument_that_is_already_initialized_is_sent_no_eject(make):
  rig = await started(make("shadow", initialized=True))
  await rig.load()
  assert rig.instrument.state.moved > 0 and "C0EI" not in rig.instrument.moved()  # it still moves, but ejects nothing
  assert rig.instrument.sent("C0EI") == []  # PyLabRobot initializes the 96 head only when it reports itself uninitialized


async def test_the_serial_is_checked_before_anything_moves(make):
  rig = await started(make("sim", options={"serial": "T9999"}))
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "wrong_robot" and "'T0001'" in str(raised.value) and "nothing was moved" in str(raised.value)
  assert rig.instrument.state.moved == 0 and rig.instrument.moved() == []
  assert ("arm",) not in rig.instrument.bridge.calls and rig.instrument.bridge.closed
  assert rig.adapter.device is None and rig.adapter.layout is None and rig.adapter._link is None


async def test_a_description_with_another_channel_count_is_refused_before_anything_moves(make):
  rig = make("sim")
  pip = rig.adapter.spec.components["pip"]
  rig.adapter.spec = replace(rig.adapter.spec, components={"pip": replace(pip, channels=4)})
  await rig.adapter.start()
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "device_mismatch" and rig.instrument.state.moved == 0
  assert rig.instrument.bridge.closed


async def test_a_bad_layout_is_refused_before_the_link_is_opened(make):
  rig = await started(make("shadow"))
  bad = copy.deepcopy(STARLET_LAYOUT)
  bad["carriers"][0]["type"] = "NO_SUCH_CARRIER"
  with pytest.raises(LabError) as raised:
    await rig.load(bad)
  assert raised.value.code == "carrier_not_allowed"
  assert rig.instrument.opened == 0 and rig.instrument.procs == []  # no spawn, no connect
  overlap = copy.deepcopy(STARLET_LAYOUT)
  overlap["carriers"][1]["track"] = 2
  with pytest.raises(LabError):
    await rig.load(overlap)
  assert rig.instrument.opened == 0


async def test_tips_sensed_before_initialization_refuse_the_load(make):
  rig = await started(make("shadow", tips_mounted=[True, False, True] + [False] * 5))
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "tips_on_channels" and "channel(s) 1, 3" in str(raised.value)
  assert "remove the tips by hand" in raised.value.hint
  assert rig.instrument.moved() == [] and rig.instrument.state.moved == 0
  assert (await rig.instrument.bridge.status())["armed"] is False  # never armed
  assert rig.adapter.layout is None and rig.adapter.device is None


async def test_a_tip_sensor_that_cannot_be_read_refuses_too(make):
  rig = await started(make("sim"))
  rig.instrument.fail("C0RT")
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "tips_unreadable"
  assert rig.instrument.moved() == [] and ("arm",) not in rig.instrument.bridge.calls


# -- a second load, and liquid handling on the link ---------------------------------------------

async def test_a_second_load_swaps_carriers_without_a_command_that_moves_and_liquid_handling_works(make):
  rig = await started(make("shadow"))
  await rig.load()
  shadow = rig.instrument
  sent, moved = list(shadow.codes()), list(shadow.moved())
  first = rig.adapter.layout
  second = await rig.load(other_layout())
  assert second is not first and rig.adapter.layout is second
  assert shadow.codes() == sent and shadow.moved() == moved  # nothing at all went to the instrument
  assert shadow.opened == 1 and shadow.procs and len(shadow.procs) == 1
  deck = shadow.device.deck
  assert first.labware["plate"].get_root() is not shadow.device and second.labware["plate"].get_root() is shadow.device
  assert deck.get_resource("plate_car").location.x > deck.get_resource("trough_car").location.x  # the swapped tracks
  assert {c.name for c in deck.children} >= {"tip_car", "plate_car", "trough_car"}  # and the driver's own stay

  await rig.lh.pick_up_tips(DEV, None)
  drawn = await rig.lh.aspirate(DEV, ["diluent"], 50)
  assert drawn["ok"] and drawn["commands_sent"] == 3
  trough = second.labware["diluent_trough"]
  assert trough.tracker.get_used_volume() == pytest.approx(100000 - 8 * 50)
  await rig.lh.dispense(DEV, ["plate:A1:H1"], 50)
  plate = second.labware["plate"]
  assert plate.get_item("A1").tracker.get_used_volume() == pytest.approx(50)
  assert trough.tracker.get_used_volume() == pytest.approx(100000 - 400)
  await rig.lh.mix(DEV, ["plate:A1:H1"], 20, 2)  # empty tips: draws from the plate's 50 uL
  await rig.lh.drop_tips(DEV, "return")
  assert rig.adapter.mounted_tips(rig.adapter.spec.components["pip"]) == []
  assert len(shadow.moved()) == len(moved) + 2 + 3 + 3 + 3 + 1
  assert rig.adapter.command_count() == shadow.state.moved == len(shadow.moved())


async def test_a_bridge_that_refuses_to_arm_refuses_the_load_with_its_reason_and_nothing_is_sent(make, monkeypatch):
  rig = await started(make("sim"))
  stand_in = starlet.open_instrument

  def opened(options, deck):
    device, bridge, state = stand_in(options, deck)

    async def refuse() -> None:
      raise BridgeRefused("not_armed")

    bridge.arm = refuse
    return device, bridge, state

  monkeypatch.setattr(starlet, "open_instrument", opened)
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "backend_unreachable"
  assert "arming the bridge failed" in str(raised.value) and "not_armed" in str(raised.value)
  assert rig.instrument.moved() == [] and rig.instrument.state.moved == 0 and rig.adapter.layout is None


async def test_a_refused_load_keeps_a_read_only_link_the_next_load_reuses(make):
  rig = await started(make("sim", tips_mounted=[True] + [False] * 7))
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "tips_on_channels" and rig.instrument.moved() == []
  rig.instrument.driver.tips_mounted = [False] * 8  # the person removed the tip by hand
  await rig.load()  # the same link: connected once, initialized now
  assert rig.instrument.opened == 1 and rig.instrument.state.moved > 0 and rig.instrument.state.mode == "open"


# -- drop_tips ----------------------------------------------------------------------------------

async def test_discarding_needs_a_confirmed_tip_waste(make):
  rig = await started(make("shadow"))
  await rig.ready()
  moved = list(rig.instrument.moved())
  assert await rig.refused("drop_tips", DEV, "discard") == "no_tip_waste"
  assert rig.instrument.moved() == moved  # nothing was sent
  await rig.lh.drop_tips(DEV, "return")
  assert rig.instrument.moved()[len(moved):] == ["C0TR"]


async def test_discarding_goes_through_with_a_tip_waste(make):
  rig = await started(make("shadow", options={"tip_waste": True}))
  await rig.ready()
  moved = len(rig.instrument.moved())
  await rig.lh.drop_tips(DEV, "discard")
  assert len(rig.instrument.moved()) > moved and rig.adapter.mounted_tips(rig.adapter.spec.components["pip"]) == []


# -- halt ---------------------------------------------------------------------------------------

async def test_halt_latches_before_its_first_await_and_a_running_call_sends_nothing_more(make):
  rig = await started(make("sim"))
  await rig.ready()
  sim, adapter = rig.instrument, rig.adapter
  moved = list(sim.moved())
  sim.hang("X0XP")  # the first command of an aspirate (C0JY goes out beside it; C0AS follows both)
  call = asyncio.ensure_future(rig.lh.aspirate(DEV, ["diluent"], 50))
  await until(lambda: "X0XP" in sim.moved()[len(moved):])
  at_halt = list(sim.moved())
  halting = asyncio.ensure_future(adapter.halt({"id": "stop-1"}))
  await asyncio.sleep(0)  # the task started: its first slice ran
  assert sim.state.mode == "read_only" and sim.state.why == "emergency stop" and adapter.halt_requested
  sim.release("X0XP")  # the call goes on to its next command
  with pytest.raises(Exception):
    await call
  assert await halting is True
  assert sim.moved() == at_halt and "C0AS" not in sim.moved()  # not one more command went to the simulator
  assert [e["status"] for e in sim.state.log if e["command"] == "C0AS"] == ["refused"]


async def test_a_stop_with_nothing_in_flight_disarms_the_bridge(make):
  rig = await started(make("shadow"))
  await rig.ready()
  adapter, shadow = rig.adapter, rig.instrument
  moved = list(shadow.moved())
  assert (await shadow.bridge.status())["armed"] is True
  assert await adapter.halt({"id": "stop-1", "engaged": True}) is True
  assert (await shadow.bridge.status())["armed"] is False  # asked of the bridge itself, over the same link
  assert adapter.halted and adapter.halt_requested and adapter.halted_text is None
  assert adapter.HALTED == "nothing more is sent to the instrument and no command is in flight"
  assert shadow.moved() == moved and shadow.state.mode == "read_only"  # the stop wrote nothing
  assert adapter.mounted_tips(adapter.spec.components["pip"]) == []  # the tips are the person's to check


async def test_halt_with_nothing_connected_does_nothing(make):
  rig = await started(make("sim"))
  assert await rig.adapter.halt({"id": "x"}) is False and not rig.adapter.halt_requested


async def test_a_stop_when_the_link_is_already_lost_says_the_bridge_did_not_confirm_and_writes_nothing(make):
  rig = await started(make("shadow"))
  await rig.ready()
  shadow, adapter = rig.instrument, rig.adapter
  moved = list(shadow.moved())
  shadow.kill()
  await until(lambda: shadow.bridge.lost)
  codes = list(shadow.codes())
  assert await adapter.halt({"id": "stop-1"}) is True  # does not raise
  assert shadow.state.latched and adapter.halt_requested and adapter.halted
  assert adapter.halted_text.startswith(adapter.HALTED + "; the bridge on the USB host did not confirm closing to motion")
  assert "BridgeLost" in adapter.halted_text and "AHC's own guard is closed either way" in adapter.halted_text
  assert shadow.moved() == moved and shadow.codes() == codes  # nothing was written


async def test_a_bridge_that_does_not_answer_the_disarm_is_said_and_the_guard_still_stands(make):
  rig = await started(make("sim"))
  await rig.ready()

  async def down() -> None:
    raise BridgeLost("the bridge did not answer")

  rig.instrument.bridge.disarm = down
  assert await rig.adapter.halt({"id": "stop-1"}) is True
  assert rig.adapter.halted and rig.instrument.state.mode == "read_only"
  assert "did not confirm closing to motion (BridgeLost" in rig.adapter.halted_text
  assert rig.adapter.halted_text.startswith("nothing more is sent to the instrument and no command is in flight")
  assert rig.adapter.HALTED == "nothing more is sent to the instrument and no command is in flight"  # the class text is untouched


async def test_a_stop_during_an_aspirate_writes_nothing_more_and_disarms_the_bridge(make):
  rig = await started(make("shadow"))
  await rig.ready()
  shadow, adapter = rig.instrument, rig.adapter
  shadow.hold("C0AS")
  call = asyncio.ensure_future(rig.lh.aspirate(DEV, ["diluent"], 50))
  await until(lambda: shadow.codes()[-1:] == ["C0AS"])
  assert shadow.state.in_flight() == ["C0AS"]
  at_stop = list(shadow.moved())
  halting = asyncio.ensure_future(adapter.halt({"id": "stop-1"}))
  await asyncio.sleep(0)
  call.cancel()  # the server cancels the running call right after it scheduled the halt
  await ended_by_the_stop(call)
  await asyncio.sleep(0.3)
  assert not halting.done()  # waiting for the reply of the command in flight
  shadow.release("C0AS")
  assert await asyncio.wait_for(halting, 15) is True
  assert shadow.moved() == at_stop and shadow.moved()[-1] == "C0AS"  # no command that moves was written after the stop
  assert (await shadow.bridge.status())["armed"] is False
  assert adapter.halted and adapter.halt_requested and shadow.state.in_flight() == [] and shadow.state.mode == "read_only"
  assert adapter.outcome_unknown is None  # the stop's own state: the server asks the person to check the tips


async def test_halt_raises_when_the_reply_never_comes(make, monkeypatch):
  monkeypatch.setattr(starlet, "HALT_WAIT_S", 0.5)
  rig = await started(make("shadow"))
  await rig.ready()
  shadow, adapter = rig.instrument, rig.adapter
  shadow.hold("C0AS")
  call = asyncio.ensure_future(rig.lh.aspirate(DEV, ["diluent"], 50))
  await until(lambda: shadow.codes()[-1:] == ["C0AS"])
  halting = asyncio.ensure_future(adapter.halt({"id": "stop-1"}))
  await asyncio.sleep(0)
  call.cancel()
  await ended_by_the_stop(call)
  with pytest.raises(LabError) as raised:
    await asyncio.wait_for(halting, 15)
  assert raised.value.code == "halt_unconfirmed" and "C0AS" in str(raised.value) and "power switch" in raised.value.hint
  assert adapter.halt_requested and not adapter.halted
  assert await rig.refused("aspirate", DEV, ["diluent"], 50) == "device_halted"
  assert (await shadow.bridge.status())["armed"] is False  # disarmed all the same


async def test_after_a_stop_the_guard_and_the_bridge_each_keep_motion_closed_by_themselves(make):
  rig = await started(make("shadow"))
  await rig.ready()
  shadow, adapter = rig.instrument, rig.adapter
  assert await adapter.halt({"id": "stop-1"}) is True
  moved = list(shadow.moved())
  # AHC's guard alone: the adapter refuses before anything is assembled, and so does the driver under it.
  assert await rig.refused("aspirate", DEV, ["diluent"], 50) == "device_halted"
  with pytest.raises(GuardRefused):
    await shadow.driver.send_raw_command("C0ASid0098")
  assert shadow.moved() == moved
  # The bridge alone: a command that moves pushed through its handle, past the adapter and the guard, is refused there.
  with pytest.raises(BridgeRefused) as refused:
    await shadow.bridge.write(b"C0ASid0099")
  assert refused.value.code == "not_armed" and refused.value.command == "C0AS"
  assert shadow.moved() == moved  # the instrument's own log: nothing reached it either way


async def test_a_reload_after_a_stop_arms_the_bridge_again_and_initializes(make):
  rig = await started(make("sim"))
  await rig.ready()
  sim, adapter = rig.instrument, rig.adapter
  assert await adapter.halt({"id": "stop-1"}) is True
  assert not sim.bridge.armed
  moved = len(sim.moved())
  await rig.load()  # nothing else is asked of anybody first
  assert sim.opened == 2 and len(sim.moved()) > moved and sim.state.mode == "open" and sim.bridge.armed
  assert sim.bridges[0].closed  # the link that was stopped is let go
  assert {call[0] for call in sim.bridge.calls} <= {"arm", "disarm", "status", "stop"}  # nothing but arming and closing
  assert not adapter.halted and not adapter.halt_requested and adapter.halted_text is None and adapter.layout is not None


async def test_a_bridge_found_disarmed_before_a_motion_call_closes_the_adapter_and_writes_nothing(make):
  rig = await started(make("shadow"))
  await rig.ready()
  shadow, adapter = rig.instrument, rig.adapter
  moved = list(shadow.moved())
  await shadow.bridge.disarm()  # the bridge no longer allows motion, and nothing told the adapter
  with pytest.raises(LabError) as raised:
    await rig.lh.aspirate(DEV, ["diluent"], 50)
  assert raised.value.code == "instrument_read_only" and "not armed" in str(raised.value) and "nothing was sent" in str(raised.value)
  assert shadow.moved() == moved and adapter.command_count() == len(moved)  # nothing was written
  assert shadow.state.mode == "read_only" and "the bridge on the USB host is not armed" in shadow.state.why
  assert "the instrument takes no more motion commands" in adapter.outcome_unknown["says"]
  assert adapter.mounted_tips(adapter.spec.components["pip"]) == []
  assert await rig.refused("aspirate", DEV, ["diluent"], 50) == "completion_unknown"
  await rig.load()  # a fresh link, armed again
  assert shadow.opened == 2 and shadow.state.mode == "open" and (await shadow.bridge.status())["armed"] is True


# -- a stop during initialization ---------------------------------------------------------------

async def test_a_stop_during_initialization_writes_no_more_of_it_and_disarms_the_bridge(make):
  rig = await started(make("shadow"))
  shadow, adapter = rig.instrument, rig.adapter
  shadow.hold("C0DI")
  loading = asyncio.ensure_future(rig.load())
  await until(lambda: "C0DI" in shadow.codes())
  at_stop = list(shadow.moved())
  halting = asyncio.ensure_future(adapter.halt({"id": "stop-1"}))
  await asyncio.sleep(0.2)
  assert not halting.done() and shadow.state.latched
  shadow.release("C0DI")
  assert await asyncio.wait_for(halting, 15) is True
  with pytest.raises(LabError) as raised:
    await asyncio.wait_for(loading, 15)
  assert raised.value.code == "device_halted" and "interrupted the instrument's initialization" in str(raised.value)
  assert "released" not in raised.value.hint
  assert shadow.state.latched and shadow.state.why == "emergency stop" and adapter.layout is None
  assert shadow.moved() == at_stop  # the rest of the initialization was not written, PyLabRobot's cleanup included
  assert (await shadow.bridge.status())["armed"] is False and adapter.halted


async def test_cancelling_the_load_during_initialization_is_not_converted(make):
  rig = await started(make("shadow"))
  shadow = rig.instrument
  shadow.hold("C0DI")
  loading = asyncio.ensure_future(rig.load())
  await until(lambda: "C0DI" in shadow.codes())
  halting = asyncio.ensure_future(rig.adapter.halt({"id": "stop-1"}))
  await asyncio.sleep(0)
  loading.cancel()
  with pytest.raises(asyncio.CancelledError):
    await loading
  shadow.release("C0DI")
  assert await asyncio.wait_for(halting, 15) is True
  assert shadow.state.latched and rig.adapter.layout is None and rig.adapter.device is None


async def test_an_initialization_that_fails_is_said_and_the_next_load_replaces_the_link(make):
  rig = await started(make("shadow"))
  shadow = rig.instrument
  shadow.fail("C0II")
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "initialization_failed" and "command(s) that move were sent" in str(raised.value)
  assert "look at the instrument" in raised.value.hint and shadow.state.latched and rig.adapter.layout is None
  sent = len(shadow.moved())
  shadow.fail("C0II", "er00/00")  # works again
  await rig.load()
  assert shadow.opened == 2 and len(shadow.moved()) > sent and shadow.state.mode == "open"


# -- a command that fails ------------------------------------------------------------------------

async def test_a_firmware_error_in_the_middle_of_an_aspirate_closes_the_guard_and_the_adapter(make):
  rig = await started(make("shadow"))
  await rig.ready()
  shadow, adapter = rig.instrument, rig.adapter
  before = adapter.command_count()
  shadow.fail("C0AS")
  with pytest.raises(LabError) as raised:
    await rig.lh.aspirate(DEV, ["diluent"], 50)
  assert raised.value.code == "completion_unknown" and "C0AS" in str(raised.value)
  assert shadow.state.mode == "read_only" and "C0AS" in shadow.state.why
  assert adapter.command_count() == before + 3 and adapter.outcome_unknown
  record = adapter.robot_record(8)
  assert {"command": "C0AS", "status": "error er01/00"} in record
  assert all(e["status"] == "refused" for e in record[record.index({"command": "C0AS", "status": "error er01/00"}) + 1:])
  moved = list(shadow.moved())
  assert await rig.refused("dispense", DEV, ["plate:A1:H1"], 10) == "completion_unknown"
  assert await rig.refused("drop_tips", DEV, "return") == "completion_unknown"
  assert shadow.moved() == moved and adapter.mounted_tips(adapter.spec.components["pip"]) == []


async def test_a_command_that_raises_closes_the_guard_and_the_adapter(make):
  rig = await started(make("sim"))
  await rig.ready()
  sim, adapter = rig.instrument, rig.adapter
  before = adapter.command_count()
  sim.fail("C0AS", TimeoutError("no answer from the instrument"))
  assert await rig.refused("aspirate", DEV, ["diluent"], 50) == "completion_unknown"
  assert sim.state.mode == "read_only" and sim.state.why == "C0AS did not complete"
  assert adapter.command_count() == before + 3 and adapter.outcome_unknown
  moved = list(sim.moved())
  assert await rig.refused("dispense", DEV, ["plate:A1:H1"], 10) == "completion_unknown"
  assert sim.moved() == moved


async def test_a_call_refused_before_anything_is_sent_leaves_the_instrument_open(make):
  rig = await started(make("sim"))
  await rig.ready()
  sim = rig.instrument
  moved = list(sim.moved())
  assert await rig.refused("aspirate", DEV, ["diluent"], 5000) == "out_of_range"
  assert sim.state.mode == "open" and sim.moved() == moved and rig.adapter.outcome_unknown is None


async def test_a_command_the_bridge_refuses_after_it_said_armed_is_an_unknown_outcome(make):
  rig = await started(make("sim"))
  await rig.ready()
  sim = rig.instrument
  stated = sim.bridge.status

  async def says_armed() -> dict:
    return {**await stated(), "armed": True}

  sim.bridge.status = says_armed
  sim.bridge.armed = False  # but a command that moves meets a bridge that is not armed
  assert await rig.refused("aspirate", DEV, ["diluent"], 50) == "completion_unknown"
  assert sim.state.mode == "read_only" and rig.adapter.outcome_unknown


# -- reads and refusals before the call ---------------------------------------------------------

async def test_motion_is_refused_before_a_layout_and_when_the_guard_is_closed(make):
  rig = await started(make("sim"))
  assert await rig.refused("pick_up_tips", DEV, None) == "no_layout"
  await rig.ready(tips=False)
  rig.instrument.state.latch("test")
  moved = list(rig.instrument.moved())
  with pytest.raises(LabError) as raised:
    await rig.adapter.pick_up_tips(rig.adapter.spec.components["pip"], rig.adapter.layout.labware["tips"]["A1:H1"])
  assert raised.value.code == "instrument_read_only" and "test" in str(raised.value) and "load_layout" in raised.value.hint
  assert rig.instrument.moved() == moved


async def test_a_link_that_is_lost_refuses_motion_with_nothing_sent(make):
  rig = await started(make("sim"))
  await rig.ready(tips=False)
  rig.instrument.bridge.lost = "the bridge process ended"  # lost, and on_lost not yet heard
  moved = list(rig.instrument.moved())
  assert await rig.refused("pick_up_tips", DEV, None) == "link_lost"
  assert rig.instrument.moved() == moved


async def test_the_tip_sensor_reads_through_the_link_and_gives_none_when_it_cannot(make):
  rig = await started(make("shadow"))
  comp = rig.adapter.spec.components["pip"]
  assert await rig.adapter.sense(comp, "tips_mounted") is None  # no link yet
  await rig.ready()
  assert await rig.adapter.sense(comp, "tips_mounted") == {"tip_presence_per_channel": [1] * 8}
  assert await rig.adapter.sense(comp, "liquid_present") is None
  assert (await rig.adapter.invoke(comp, "sense_tip_presence", {}))["tip_presence_per_channel"] == [1] * 8
  rig.instrument.state.latch("test")  # a read passes a closed guard
  assert await rig.adapter.sense(comp, "tips_mounted") == {"tip_presence_per_channel": [1] * 8}
  rig.instrument.kill()
  await until(lambda: rig.instrument.bridge.lost)
  assert await rig.adapter.sense(comp, "tips_mounted") is None
  with pytest.raises(LabError) as raised:
    await rig.adapter.invoke(comp, "sense_tip_presence", {})
  assert raised.value.code == "link_lost"


# -- a lost link --------------------------------------------------------------------------------

async def test_a_link_lost_while_idle_is_an_unknown_outcome_and_the_next_load_reconnects(make):
  rig = await started(make("sim"))
  await rig.ready()
  sim, adapter = rig.instrument, rig.adapter
  sim.bridge.lose("the bridge process ended")
  assert "the link to the USB host was lost" in adapter.outcome_unknown["says"]
  assert sim.state.mode == "read_only" and not adapter.halted
  assert await rig.refused("aspirate", DEV, ["diluent"], 50) == "completion_unknown"
  assert adapter.mounted_tips(adapter.spec.components["pip"]) == []
  await rig.load()
  assert sim.opened == 2 and adapter.outcome_unknown is None and sim.state.mode == "open" and sim.bridge.armed
  assert sim.states[0].moved > 0 and adapter.command_count() == sim.states[0].moved + sim.states[1].moved


async def test_a_real_link_lost_while_idle_is_heard_on_the_reader_thread(make):
  rig = await started(make("shadow"))
  await rig.ready()
  shadow, adapter = rig.instrument, rig.adapter
  shadow.kill()
  await until(lambda: adapter.outcome_unknown)
  assert "lost" in adapter.outcome_unknown["says"] and shadow.state.mode == "read_only"
  assert await rig.refused("aspirate", DEV, ["diluent"], 50) in ("completion_unknown", "link_lost")
  await rig.load()
  assert shadow.opened == 2 and adapter.outcome_unknown is None and shadow.state.mode == "open"


async def test_a_link_lost_before_anything_moved_is_not_an_unknown_outcome(make):
  rig = await started(make("sim", tips_mounted=[True] + [False] * 7))
  with pytest.raises(LabError):
    await rig.load()  # tips_on_channels: the link stays, read-only
  rig.instrument.bridge.lose("gone")
  assert rig.adapter.outcome_unknown is None and rig.instrument.state.latched


# -- stop() -------------------------------------------------------------------------------------

async def test_stop_lets_go_without_moving_and_a_later_load_connects_again(make):
  rig = await started(make("shadow"))
  await rig.ready(tips=False)
  shadow, adapter = rig.instrument, rig.adapter
  moved, count = list(shadow.moved()), adapter.command_count()
  await adapter.stop()
  assert shadow.moved() == moved and adapter.command_count() == count and adapter.device is None
  assert shadow.procs[0].wait(10) == 0  # the bridge closed on its own
  assert adapter.identity() == {} and adapter.robot_record(1) is None
  await adapter.stop()  # again: nothing to do
  await rig.load()
  assert shadow.opened == 2 and len(shadow.moved()) > len(moved) and adapter.command_count() > count
  assert adapter.command_count() == len(moved) + shadow.state.moved  # the dropped link's commands stay counted


# -- what the capability layer reads ------------------------------------------------------------

async def test_mounted_tips_follow_the_twin_until_a_stop_or_an_unknown_outcome(make):
  rig = await started(make("sim"))
  await rig.ready()
  comp = rig.adapter.spec.components["pip"]
  assert len(rig.adapter.mounted_tips(comp)) == 8
  rig.adapter.mark_outcome_unknown("aspirate", "TimeoutError: x")
  assert rig.adapter.mounted_tips(comp) == [] and rig.instrument.state.latched
  rig.adapter.outcome_unknown = None
  assert len(rig.adapter.mounted_tips(comp)) == 8
  await rig.adapter.halt({"id": "s"})
  assert rig.adapter.mounted_tips(comp) == []


async def test_a_load_with_tips_mounted_is_refused(make):
  rig = await started(make("sim"))
  await rig.ready()
  with pytest.raises(LabError) as raised:
    await rig.load(other_layout())
  assert raised.value.code == "tips_mounted" and rig.instrument.opened == 1


async def test_the_commands_counted_are_the_ones_that_move(make):
  rig = await started(make("sim"))
  await rig.load()
  count = rig.adapter.command_count()
  assert count == rig.instrument.state.moved == len(rig.instrument.moved()) and len(rig.instrument.reached) > count
  await rig.adapter.sense(rig.adapter.spec.components["pip"], "tips_mounted")
  assert rig.adapter.command_count() == count  # a read is not counted


# -- sim is unchanged ---------------------------------------------------------------------------

async def test_the_simulated_backend_has_no_stop_and_keeps_its_own_counter():
  spec = replace(load_device("hamilton.starlet"), id="starlet")
  adapter = STARletAdapter(spec, "sim", {})
  assert adapter.has_stop is False and await adapter.halt({"id": "x"}) is False
  assert STARletAdapter.IDENTITY_OPTION == "serial" and STARletAdapter.IDENTITY_FIELD == "serial"
  assert "initializes the instrument" in STARletAdapter.LOAD_MOVES
  await adapter.start()
  await adapter.load_layout(STARLET_LAYOUT)
  assert adapter.command_count() > 50 and adapter.device is not None and adapter._link is None
  await adapter.stop()
  assert adapter.device is None


# -- whatever closes the guard after motion was open leaves the channels to the person ------------

async def test_a_failed_initialization_leaves_the_instrument_to_the_person_until_the_next_load(make):
  rig = await started(make("shadow"))
  shadow, adapter = rig.instrument, rig.adapter
  shadow.fail("C0II")
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "initialization_failed"
  unknown = adapter.outcome_unknown  # the server holds the next load for the person's check on this
  assert unknown["op"] == "load_layout" and "initialization failed part-way" in unknown["says"]
  assert "unconfirmed" in unknown["says"]
  shadow.fail("C0II", "er00/00")  # works again
  await rig.load()
  assert adapter.outcome_unknown is None and shadow.state.mode == "open"


async def test_a_stop_during_initialization_is_the_stops_and_not_an_unknown_outcome_of_its_own(make):
  rig = await started(make("shadow"))
  shadow, adapter = rig.instrument, rig.adapter
  shadow.hold("C0DI")
  loading = asyncio.ensure_future(rig.load())
  await until(lambda: "C0DI" in shadow.codes())
  halting = asyncio.ensure_future(adapter.halt({"id": "stop-1"}))
  await asyncio.sleep(0.1)
  shadow.release("C0DI")
  assert await asyncio.wait_for(halting, 15) is True
  with pytest.raises(LabError) as raised:
    await asyncio.wait_for(loading, 15)
  assert raised.value.code == "device_halted" and adapter.halted and adapter.outcome_unknown is None


async def test_a_read_that_gets_no_answer_closes_motion_and_leaves_the_tips_to_the_person(make):
  rig = await started(make("sim"))
  await rig.ready()  # tips are on
  sim, adapter, comp = rig.instrument, rig.adapter, rig.adapter.spec.components["pip"]
  assert len(adapter.mounted_tips(comp)) == 8
  sim.fail("C0RT")  # the tip sensor's read gets no answer: the guard closes
  with pytest.raises(Exception):
    await adapter.invoke(comp, "sense_tip_presence", {})
  assert sim.state.latched and adapter.outcome_unknown is None  # nobody asked for motion yet
  moved = list(sim.moved())
  with pytest.raises(LabError) as raised:
    await adapter.drop_tips(comp, "return")
  assert raised.value.code == "instrument_read_only" and sim.moved() == moved
  # The drop the server would otherwise ask for can no longer run, so the tips become the person's to check.
  assert adapter.outcome_unknown is not None and "unconfirmed" in adapter.outcome_unknown["says"]
  assert adapter.mounted_tips(comp) == []
  assert await rig.refused("drop_tips", DEV, "return") == "completion_unknown"


async def test_a_usb_host_that_does_not_answer_before_a_call_closes_motion(make):
  rig = await started(make("sim"))
  await rig.ready()
  sim, adapter = rig.instrument, rig.adapter

  async def silent():
    raise TimeoutError("no answer")

  sim.bridge.status = silent
  moved = list(sim.moved())
  assert await rig.refused("aspirate", DEV, ["diluent"], 50) == "link_lost"
  assert sim.moved() == moved and sim.state.latched and adapter.outcome_unknown is not None


async def test_a_call_the_stop_cancelled_ends_cancelled_even_when_the_guard_refused_the_cleanup(make):
  rig = await started(make("sim"))
  await rig.ready()
  sim, adapter = rig.instrument, rig.adapter
  sim.hang("C0AS")
  call = asyncio.ensure_future(rig.lh.aspirate(DEV, ["diluent"], 50))
  await until(lambda: "C0AS" in sim.moved())
  halting = asyncio.ensure_future(adapter.halt({"id": "stop-1"}))
  await asyncio.sleep(0)
  call.cancel()  # as the server does right after scheduling the halt
  with pytest.raises(asyncio.CancelledError):
    await call
  assert call.cancelled()
  refused = [e["command"] for e in sim.state.log if e["status"] == "refused"]
  sim.release("C0AS")
  await asyncio.wait_for(halting, 15)
  assert adapter.halt_requested and sim.state.latched, refused


# -- the channel spacing the person states --------------------------------------------------------

LAB_CHANNEL_WIDTH = 17.97  # what the lab's channels answer to `VY` (388 increments, converted by PyLabRobot)
SPACING_OPTIONS = {"channel_spacing_mm": 9.0}


def _used(text: str) -> list[int]:
  """The channels a command was written for, as its `tm` mask says (the others carry 0 in every list)."""
  return [i for i, on in enumerate(_field(text, "tm")) if on]


def _field(text: str, name: str) -> list[int]:
  """The per-channel values of one parameter of the command text the instrument was written (`yp1458 0000`)."""
  found = re.search(rf"{name}((?:-?\d+ ?)+)", text)
  assert found, f"no {name} in {text!r}"
  return [int(value) for value in found.group(1).split()]


async def _column(rig: Rig) -> None:
  """A full column of tips, an aspirate from the trough, a dispense into plate column 2 and the tips back."""
  await rig.lh.pick_up_tips(DEV, None)
  await rig.lh.aspirate(DEV, ["diluent"], 50)
  await rig.lh.dispense(DEV, ["plate:A2:H2"], 50)
  await rig.lh.drop_tips(DEV, "return")


async def test_channels_reporting_17_97_mm_take_a_column_one_channel_at_a_time(make):
  rig = await started(make("shadow", channel_width=LAB_CHANNEL_WIDTH))
  await rig.load()
  assert {c.width for c in rig.instrument.driver.arms[0].pipettes.configuration.channels} == {17.97}
  await _column(rig)
  shadow = rig.instrument
  assert len(shadow.sent("C0TP")) == 8 and [_used(t) for t in shadow.sent("C0TP")] == [[i] for i in range(8)]
  assert len(shadow.sent("C0DS")) == 8 and len(shadow.sent("C0TR")) == 8  # the plate column and the rack: 1 at a time
  assert len(shadow.sent("C0AS")) > 1  # the trough: split into batches, where 9 mm takes one command
  assert "channel_spacing_mm" not in rig.adapter.identity()


async def test_a_stated_spacing_takes_a_full_column_in_one_command_of_each_kind(make):
  rig = await started(make("shadow", options=SPACING_OPTIONS, channel_width=LAB_CHANNEL_WIDTH))
  await rig.load()
  await _column(rig)
  shadow = rig.instrument
  for code in ("C0TP", "C0AS", "C0DS", "C0TR"):
    sent = shadow.sent(code)
    assert len(sent) == 1, (code, sent)
    assert _used(sent[0]) == list(range(8))
  positions = _field(shadow.sent("C0TP")[0], "yp")
  assert len(positions) == 8 and all(a - b == 90 for a, b in pairwise(positions))  # tenths of a mm
  rows = _field(shadow.sent("C0DS")[0], "yp")
  assert all(a - b == 90 for a, b in pairwise(rows)), rows  # plate column 2: its rows are 9 mm apart
  spread = _field(shadow.sent("C0AS")[0], "yp")  # the trough: the channels spread over it, never closer than 9 mm
  assert all(a - b >= 90 for a, b in pairwise(spread)), spread


async def test_a_stated_spacing_is_refused_below_the_pitch_the_master_reports(make):
  rig = await started(make("shadow", options=SPACING_OPTIONS, channel_width=LAB_CHANNEL_WIDTH, raster_pitch=18.0))
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "channel_spacing_refused" and "nothing was moved" in str(raised.value)
  assert "9 mm" in str(raised.value) and "18 mm" in str(raised.value)
  assert raised.value.hint.startswith("Tell the person:") and "written again" in raised.value.hint
  shadow = rig.instrument
  assert shadow.moved() == [] and shadow.state.moved == 0
  assert (await shadow.bridge.status())["armed"] is False  # never armed
  assert rig.adapter.layout is None and rig.adapter.device is None


async def test_a_stated_spacing_is_refused_when_the_master_reports_no_pitch(make):
  rig = await started(make("shadow", options=SPACING_OPTIONS, no_raster_pitch=True))
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "channel_spacing_refused" and "nothing was moved" in str(raised.value)
  assert "reported no minimum pitch" in str(raised.value) and raised.value.hint.startswith("Tell the person:")
  shadow = rig.instrument
  assert shadow.moved() == [] and shadow.state.moved == 0
  assert (await shadow.bridge.status())["armed"] is False
  assert rig.adapter.layout is None and rig.adapter.device is None


async def test_a_stated_spacing_equal_to_the_reported_pitch_is_accepted(make):
  rig = await started(make("shadow", options={"channel_spacing_mm": 12.5}, raster_pitch=12.5))
  await rig.load()
  assert rig.adapter.identity()["channel_spacing_mm"]["planned"] == 12.5


async def test_the_stated_spacing_replaces_every_width_after_setup_and_is_on_the_identity(make):
  rig = await started(make("shadow", options=SPACING_OPTIONS, channel_width=LAB_CHANNEL_WIDTH))
  await rig.load()
  channels = rig.instrument.driver.arms[0].pipettes.configuration.channels
  assert len(channels) == 8 and {c.width for c in channels} == {9.0}
  assert rig.adapter.identity()["channel_spacing_mm"] == {"reported": [17.97], "planned": 9.0}
  assert rig.adapter.identity()["serial"] == "T0001"  # the rest of the identity is as it was


async def test_without_the_option_the_widths_and_the_identity_are_as_reported(make):
  rig = await started(make("shadow", channel_width=LAB_CHANNEL_WIDTH))
  await rig.load()
  assert {c.width for c in rig.instrument.driver.arms[0].pipettes.configuration.channels} == {17.97}
  assert "channel_spacing_mm" not in rig.adapter.identity()


@pytest.mark.parametrize("value", [9, 9.0, 18, 12.5])
def test_a_channel_spacing_that_is_a_number_of_at_least_9_is_accepted(value):
  STARletAdapter.check_options("usb", {"bridge": "usb-host", "channel_spacing_mm": value})


@pytest.mark.parametrize("value", ["9", True, False, 8.9, 0, -9.0, None, [9], float("nan"), float("inf"),
                                   float("-inf")])
def test_a_channel_spacing_that_is_not_a_number_of_at_least_9_is_refused(value):
  with pytest.raises(LabError) as refused:
    STARletAdapter.check_options("usb", {"bridge": "usb-host", "channel_spacing_mm": value})
  assert refused.value.code == "bad_option" and "channel_spacing_mm" in str(refused.value)


def test_the_sim_backend_still_refuses_a_channel_spacing():
  with pytest.raises(LabError) as refused:
    STARletAdapter.check_options("sim", {"channel_spacing_mm": 9.0})
  assert refused.value.code == "bad_option"


def test_describe_entry_says_what_spacing_the_person_states_and_only_then():
  describe = STARletAdapter.describe_entry
  clause = ", channels planned 9 mm apart as the person states (not the width the channels report)"
  base = {"bridge": "usb-host", "serial": "T0001"}
  assert describe({**base, "channel_spacing_mm": 9.0}).startswith(
    ", instrument 'T0001' through the USB host 'usb-host', no tip waste (tips go back to their rack)" + clause + "; ")
  assert ", channels planned 12.5 mm apart" in describe({**base, "tip_waste": True, "channel_spacing_mm": 12.5})
  assert "tip waste fitted" in describe({**base, "tip_waste": True, "channel_spacing_mm": 9})
  assert "planned" not in describe(base) and "channels planned" not in describe({"bridge": "usb-host"})
  assert describe({**base, "channel_spacing_mm": "x"}).startswith(", instrument")  # a description never raises


# -- tip recovery: tips left on the channels go back to their rack, or into the waste ----------------

RECOVERY_TARGET = "tips:col3"
ALL_TIPS = [True] * 8
FORBIDDEN_IN_A_RECOVERY = ("C0DI", "C0EI", "C0PG", "I0XP", "C0II", "C0TP", "C0VI")


def recovery_layout(declared: str = "empty", target: str = RECOVERY_TARGET) -> dict:
  """STARLET_LAYOUT as the session that recovers the tips passes it: the target spots hold no tip (they are on the
  channels), or are left as full racks (`declared="full"`)."""
  layout = copy.deepcopy(STARLET_LAYOUT)
  if declared == "empty":
    layout["tips_empty"] = [target]
  layout["tips_returned"] = ["tips:col1"]
  return layout


async def recover(rig: Rig, mode: str = "return", target: str | None = RECOVERY_TARGET, layout: dict | None = None):
  return await rig.adapter.recover_tips(rig.adapter.spec.components["pip"], layout or recovery_layout(), mode, target)


async def refused_recovery(rig: Rig, *args, **kwargs) -> LabError:
  with pytest.raises(LabError) as raised:
    await recover(rig, *args, **kwargs)
  return raised.value


def left_no_link(rig: Rig) -> None:
  assert rig.adapter._link is None and rig.adapter.device is None and rig.adapter.layout is None
  assert rig.instrument.state.mode == "read_only" and rig.instrument.state.latched  # closed to motion, link let go


@pytest.mark.parametrize("declared", ["empty", "full"])
async def test_recovery_returns_the_tips_without_initializing_and_lets_go_of_the_link(make, declared):
  rig = await started(make("shadow", options=SPACING_OPTIONS, tips_mounted=ALL_TIPS, initialized=True,
                           channel_width=LAB_CHANNEL_WIDTH))
  shadow = rig.instrument
  result = await recover(rig, layout=recovery_layout(declared))
  assert shadow.moved() == ["C0ZA", "C0EV", "C0TR"]  # the safety raise, and one return of 8 tips
  for code in FORBIDDEN_IN_A_RECOVERY:
    assert code not in shadow.codes(), code
  (text,) = shadow.sent("C0TR")
  assert _used(text) == list(range(8))
  rows = _field(text, "yp")
  assert len(rows) == 8 and all(a - b == 90 for a, b in pairwise(rows)), rows  # 9 mm apart, as a normal return
  assert result["moved"] is True and result["mode"] == "return" and result["commands"] == 3
  assert result["sensed_before"] == [1] * 8 and result["sensed_after"] == [0] * 8
  assert len(result["spots"]) == 8 and all(name.endswith(f"{row}3") for name, row in zip(result["spots"], "ABCDEFGH"))
  left_no_link(rig)
  assert shadow.procs[-1].wait(10) == 0  # the bridge process ended: nothing is armed
  assert rig.adapter.command_count() == shadow.state.moved == 3
  deck = shadow.device.deck  # the model of the link just let go: the spots hold their tips again
  spots = [deck.get_resource(name) for name in result["spots"]]
  assert all(spot.tip is not None and spot.tip.name.startswith(spot.name + "#") for spot in spots)
  xs = {round(spot.get_location_wrt(deck, x="c", y="c", z="b").x * 10) for spot in spots}
  assert len(xs) == 1
  assert abs(_field(text, "xp")[0] - xs.pop()) <= 5  # the named column's x (tenths of a mm)


async def test_recovery_without_the_spacing_option_returns_one_channel_at_a_time(make):
  rig = await started(make("shadow", tips_mounted=ALL_TIPS, initialized=True, channel_width=LAB_CHANNEL_WIDTH))
  shadow = rig.instrument
  result = await recover(rig)
  assert shadow.moved() == ["C0ZA", "C0EV"] + ["C0TR"] * 8
  assert [_used(t) for t in shadow.sent("C0TR")] == [[i] for i in range(8)]
  for code in FORBIDDEN_IN_A_RECOVERY:
    assert code not in shadow.codes(), code
  assert result["moved"] is True and result["sensed_after"] == [0] * 8 and result["commands"] == 10
  left_no_link(rig)


async def test_a_load_and_a_pick_up_work_after_a_recovery(make):
  rig = await started(make("shadow", options=SPACING_OPTIONS, tips_mounted=ALL_TIPS, initialized=True,
                           carry_tips=True))
  shadow = rig.instrument
  await recover(rig)
  assert shadow.opened == 1
  moved = len(shadow.moved())
  await rig.load()  # a fresh link and the normal bring-up
  assert shadow.opened == 2 and shadow.state.mode == "open" and len(shadow.moved()) > moved
  assert rig.adapter.layout is not None
  await rig.lh.pick_up_tips(DEV, [RECOVERY_TARGET])
  assert len(shadow.sent("C0TP")) == 1 and len(rig.adapter.mounted_tips(rig.adapter.spec.components["pip"])) == 8
  assert rig.adapter.command_count() == sum(state.moved for state in shadow.states)


async def test_a_recovery_drop_refused_before_it_is_sent_leaves_no_link_a_load_could_reuse(make, monkeypatch):
  from pylabrobot.hamilton.star.driver.features.pipettes import Pipettes

  async def refuses(self, *args, **kwargs):
    raise ValueError("refused before anything was sent")

  rig = await started(make("shadow", options=SPACING_OPTIONS, tips_mounted=ALL_TIPS, initialized=True,
                           carry_tips=True))
  shadow = rig.instrument
  with monkeypatch.context() as patch:  # its own context: the rig's stand-ins stay in place
    patch.setattr(Pipettes, "return_tips", refuses)
    with pytest.raises(ValueError):
      await recover(rig)
  assert shadow.moved() == ["C0ZA", "C0EV"]  # the safety raise only: the tips were not touched
  left_no_link(rig)  # not an open, armed link set up without the channels' initialization
  assert shadow.procs[-1].wait(10) == 0  # the bridge process ended: nothing is armed
  with pytest.raises(LabError) as raised:  # the tips are still on, and a load still says so
    await rig.load()
  assert raised.value.code == "tips_on_channels" and shadow.opened == 2
  assert shadow.moved() == ["C0ZA", "C0EV"]


async def test_a_recovery_of_an_instrument_that_is_not_initialized_is_refused_before_anything_moves(make):
  rig = await started(make("sim", tips_mounted=ALL_TIPS, initialized=False))
  sim = rig.instrument
  refused = await refused_recovery(rig)
  assert refused.code == "recovery_needs_initialized" and "Nothing was moved" in str(refused)
  assert "ejects mounted tips" in str(refused) and refused.hint.startswith("Tell the person:")
  assert "Do not retry" in refused.hint
  assert sim.moved() == [] and sim.state.moved == 0 and ("arm",) not in sim.bridge.calls
  assert sim.bridge.closed and rig.adapter._link is None and rig.adapter.device is None


async def test_a_recovery_with_no_tip_sensed_moves_nothing_and_says_so(make):
  rig = await started(make("sim", tips_mounted=[False] * 8, initialized=True))
  sim = rig.instrument
  result = await recover(rig)
  assert result == {"moved": False, "sensed_before": [0] * 8,
                    "note": "no tip is sensed on any channel; nothing was moved"}
  assert sim.moved() == [] and sim.state.moved == 0 and ("arm",) not in sim.bridge.calls
  assert sim.bridge.closed and rig.adapter._link is None and rig.adapter.command_count() == 0


@pytest.mark.parametrize("sensed, target", [
  ([True] * 4 + [False] * 4, RECOVERY_TARGET),  # tips on channels 1-4, 8 spots
  ([False, False] + [True] * 6, "tips:A3:F3"),  # right count, not channels 1-6
  ([True, False, True] + [False] * 5, "tips:A3:B3"),  # a gap
  ([True] * 8, "tips:A3:D3"),  # more tips than spots
])
async def test_tips_that_are_not_the_ones_the_target_names_are_refused_and_nothing_moves(make, sensed, target):
  rig = await started(make("sim", tips_mounted=sensed, initialized=True))
  sim = rig.instrument
  refused = await refused_recovery(rig, target=target)
  assert refused.code == "recovery_mismatch" and "Nothing was moved" in str(refused)
  assert "channel(s) " + ", ".join(str(i + 1) for i, t in enumerate(sensed) if t) in str(refused)
  assert sim.moved() == [] and sim.state.moved == 0 and ("arm",) not in sim.bridge.calls
  assert rig.adapter._link is None and sim.bridge.closed


async def test_a_tip_sensor_that_cannot_be_read_refuses_the_recovery(make):
  rig = await started(make("sim", tips_mounted=ALL_TIPS, initialized=True))
  rig.instrument.fail("C0RT")
  assert (await refused_recovery(rig)).code == "tips_unreadable"
  assert rig.instrument.moved() == [] and ("arm",) not in rig.instrument.bridge.calls and rig.adapter._link is None


async def test_a_channel_spacing_the_master_refuses_refuses_the_recovery_before_the_bridge_is_armed(make):
  rig = await started(make("sim", options=SPACING_OPTIONS, tips_mounted=ALL_TIPS, initialized=True, raster_pitch=18.0))
  assert (await refused_recovery(rig)).code == "channel_spacing_refused"
  assert rig.instrument.moved() == [] and ("arm",) not in rig.instrument.bridge.calls and rig.adapter._link is None


async def test_discarding_in_a_recovery_needs_a_tip_waste_and_is_refused_before_a_link_is_opened(make):
  rig = await started(make("sim", tips_mounted=ALL_TIPS, initialized=True))
  assert (await refused_recovery(rig, "discard")).code == "no_tip_waste"
  assert rig.instrument.opened == 0 and rig.instrument.moved() == []


async def test_discarding_in_a_recovery_with_a_tip_waste_sends_a_safety_raise_and_the_drop_only(make):
  rig = await started(make("shadow", options={**SPACING_OPTIONS, "tip_waste": True}, tips_mounted=ALL_TIPS,
                           initialized=True))
  shadow = rig.instrument
  result = await recover(rig, "discard", layout=recovery_layout("full"))
  moved = shadow.moved()
  assert moved[:2] == ["C0ZA", "C0EV"] and len(moved) > 2 and set(moved[2:]) == {"C0TR"}
  for code in FORBIDDEN_IN_A_RECOVERY:
    assert code not in shadow.codes(), code
  assert result["moved"] is True and result["mode"] == "discard" and result["sensed_after"] == [0] * 8
  assert result["commands"] == len(moved)
  deck = shadow.device.deck
  assert all(deck.get_resource(name).tip is None for name in result["spots"])  # discarded: the spots stay empty
  left_no_link(rig)


@pytest.mark.parametrize("mode, target, code", [
  ("return", None, "recovery_target_required"), ("return", "", "recovery_target_required"),
  ("discard", None, "recovery_target_required"), ("wash", RECOVERY_TARGET, "bad_param"),
  ("return", "tips", "bad_tips"), ("return", "plate:A1:H1", "bad_tips"), ("return", "tips:col13", "bad_tips"),
])
async def test_a_recovery_that_is_asked_wrongly_is_refused_before_a_link_is_opened(make, mode, target, code):
  rig = await started(make("sim", options={"tip_waste": True}, tips_mounted=ALL_TIPS, initialized=True))
  assert (await refused_recovery(rig, mode, target)).code == code
  assert rig.instrument.opened == 0 and rig.instrument.moved() == []


async def test_a_bad_layout_is_refused_before_a_link_is_opened(make):
  rig = await started(make("sim", tips_mounted=ALL_TIPS, initialized=True))
  bad = copy.deepcopy(STARLET_LAYOUT)
  bad["carriers"][0]["type"] = "NOT_A_CARRIER"
  assert (await refused_recovery(rig, layout=bad)).code == "carrier_not_allowed"
  assert rig.instrument.opened == 0


async def test_a_stop_during_the_recovery_drop_is_the_same_as_a_stop_during_a_drop(make):
  rig = await started(make("shadow", options=SPACING_OPTIONS, tips_mounted=ALL_TIPS, initialized=True))
  shadow, adapter = rig.instrument, rig.adapter
  shadow.hold("C0TR")
  call = asyncio.ensure_future(recover(rig))
  await until(lambda: shadow.codes()[-1:] == ["C0TR"])
  assert shadow.state.in_flight() == ["C0TR"]
  at_stop = list(shadow.moved())
  assert at_stop == ["C0ZA", "C0EV", "C0TR"]
  halting = asyncio.ensure_future(adapter.halt({"id": "stop-1"}))
  await asyncio.sleep(0)
  call.cancel()  # the server cancels the running call right after it scheduled the halt
  await ended_by_the_stop(call)
  await asyncio.sleep(0.3)
  assert not halting.done()  # waiting for the reply of the command in flight
  shadow.release("C0TR")
  assert await asyncio.wait_for(halting, 15) is True
  assert shadow.moved() == at_stop  # nothing that moves was written after the stop
  assert (await shadow.bridge.status())["armed"] is False
  assert adapter.halted and adapter.halt_requested and shadow.state.in_flight() == []
  assert shadow.state.mode == "read_only"
  assert adapter.outcome_unknown is None  # the stop's own state: the person checks the tips
  assert adapter.command_count() == 3


async def test_a_firmware_error_on_the_recovery_drop_closes_the_guard_and_the_adapter_as_a_failed_drop_does(make):
  rig = await started(make("shadow", options=SPACING_OPTIONS, tips_mounted=ALL_TIPS, initialized=True))
  shadow, adapter = rig.instrument, rig.adapter
  shadow.fail("C0TR")
  with pytest.raises(Exception) as raised:
    await recover(rig)
  assert not isinstance(raised.value, asyncio.CancelledError) and "C0TR" in str(raised.value)
  assert shadow.state.mode == "read_only" and "C0TR" in shadow.state.why
  assert adapter.command_count() == 3 and adapter.outcome_unknown["op"] == "recover_tips"
  record = adapter.robot_record(64)
  assert {"command": "C0TR", "status": "error er01/00"} in record  # then the cleanup, refused by the closed guard
  moved = list(shadow.moved())
  with pytest.raises(LabError) as again:
    adapter.require_motion()
  assert again.value.code == "completion_unknown" and shadow.moved() == moved
  assert adapter._link is not None  # as after a failed drop: the next load lets go of it


async def test_the_whole_story_load_pick_up_stop_recover_load_pick_up(make):
  rig = await started(make("shadow", options=SPACING_OPTIONS, carry_tips=True))
  shadow, adapter = rig.instrument, rig.adapter
  await rig.load()
  shadow.hold("C0TP")
  picking = asyncio.ensure_future(rig.lh.pick_up_tips(DEV, [RECOVERY_TARGET]))
  await until(lambda: shadow.codes()[-1:] == ["C0TP"])
  halting = asyncio.ensure_future(adapter.halt({"id": "stop-1"}))
  await asyncio.sleep(0)
  picking.cancel()
  await ended_by_the_stop(picking)
  shadow.release("C0TP")
  assert await asyncio.wait_for(halting, 15) is True
  assert shadow.sent("C0TP") and adapter.halted
  shadow.tips_mounted = ALL_TIPS[:]  # the instrument finished the pick-up that was in flight: the 8 tips are on
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "tips_on_channels"  # a load still refuses: its initialization would eject them
  shadow_moves = len(shadow.moved())
  result = await recover(rig, layout=recovery_layout("empty"))
  assert result["moved"] is True and result["sensed_before"] == [1] * 8 and result["sensed_after"] == [0] * 8
  assert shadow.moved()[shadow_moves:] == ["C0ZA", "C0EV", "C0TR"]
  assert not adapter.halted and adapter._link is None
  await rig.load()
  await rig.lh.pick_up_tips(DEV, [RECOVERY_TARGET])  # the instrument took them from the same column again
  assert len(adapter.mounted_tips(adapter.spec.components["pip"])) == 8
  assert adapter.command_count() == sum(state.moved for state in shadow.states)


async def test_a_load_with_tips_sensed_still_answers_tips_on_channels(make):
  rig = await started(make("sim", tips_mounted=ALL_TIPS, initialized=True))
  with pytest.raises(LabError) as raised:
    await rig.load()
  assert raised.value.code == "tips_on_channels" and rig.instrument.moved() == []


async def test_the_sim_backend_has_no_tip_recovery_and_the_usb_backend_has(make, tmp_path):
  spec = replace(load_device("hamilton.starlet"), id="starlet")
  sim = STARletAdapter(spec, "sim", {})
  assert sim.recovers_tips() is False
  with pytest.raises(LabError) as raised:
    await sim.recover_tips(spec.components["pip"], STARLET_LAYOUT, "return", RECOVERY_TARGET)
  assert raised.value.code == "recovery_not_supported" and "no tip recovery" in str(raised.value)
  assert raised.value.hint.startswith("The person removes the tips by hand, then verify(check='tips_mounted'")
  assert make("sim").adapter.recovers_tips() is True
