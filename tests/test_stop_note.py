"""The dashboard's stop bar says what the stop cannot do on real hardware: `stop_status` carries the `emergency_stop`
sentence of a confirmed real device's description file, and nothing at all for a folder without one."""

import pytest

from ahc.devices.hamilton.starlet import STARletAdapter
from ahc.devices.spec import load_device
from ahc.viz import stopbar
from ahc.viz.dashboard import render_page, stop_note, stop_status
from ahc.workspace import Config, DeviceEntry, Workspace, estop
from ahc.workspace.config import HEADER

STARLET = DeviceEntry("starlet", "hamilton.starlet", "usb", {"bridge": "usb-host", "serial": "T0001"})
STARLET_SIM = DeviceEntry("starlet", "hamilton.starlet", "sim", {})
OT2 = DeviceEntry("ot2", "opentrons.ot2", "robot", {"host": "127.0.0.1", "robot_name": "lab-ot2"})
OT2_SIM = DeviceEntry("ot2", "opentrons.ot2", "sim", {})


def folder(tmp_path, *devices: DeviceEntry, confirmed: bool = True) -> Workspace:
  ws = Workspace(tmp_path)
  if devices:
    config = Config(devices=list(devices), limits={})
    if confirmed:
      config.confirm("human")
    ws.write_config(config.data(), HEADER)
  return ws


def test_a_confirmed_real_starlet_carries_the_sentence_of_its_description_file(tmp_path):
  ws = folder(tmp_path, STARLET)
  sentence = load_device("hamilton.starlet").raw["emergency_stop"]["usb"]
  assert stop_note(ws) == sentence == STARletAdapter.STOP_NOTE
  assert stop_status(ws) == {"engaged": False, "folder": str(ws.root), "note": sentence}


def test_the_note_stays_while_a_stop_is_engaged(tmp_path):
  ws = folder(tmp_path, STARLET)
  estop.engage(ws, by="test")
  status = stop_status(ws)
  assert status["engaged"] is True and status["note"] == STARletAdapter.STOP_NOTE


@pytest.mark.parametrize("devices", [(), (OT2,), (STARLET_SIM,), (OT2_SIM,)], ids=["no config", "ot2", "starlet sim", "ot2 sim"])
def test_every_other_folder_has_exactly_the_status_it_had_without_a_note(tmp_path, devices):
  ws = folder(tmp_path, *devices)
  assert stop_note(ws) is None
  assert stop_status(ws) == {"engaged": False, "folder": str(ws.root)}  # no `note` key at all


def test_a_config_that_is_not_confirmed_or_cannot_be_read_gives_no_note_and_no_error(tmp_path):
  ws = folder(tmp_path, STARLET, confirmed=False)
  assert stop_note(ws) is None and "note" not in stop_status(ws)
  ws.config_path.write_text("devices: [not: valid: yaml")
  assert stop_note(ws) is None and stop_status(ws) == {"engaged": False, "folder": str(ws.root)}
  ws.config_path.write_text("devices:\n  - id: x\n    model: nobody.nothing\n    backend: usb\n")
  assert stop_note(ws) is None


def test_the_stop_bar_shows_the_note_as_one_line_when_the_status_has_one(tmp_path):
  assert 'id="stoplimit"' in stopbar.HTML and ".stopbar .limit:empty{display:none}" in stopbar.CSS
  assert 'document.getElementById("stoplimit").textContent=e.note||""' in stopbar.JS
  page = render_page(folder(tmp_path), "token")
  assert 'id="stoplimit"></span>' in page  # empty until the status has a note: nothing shows on the page
