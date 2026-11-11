"""The OT-2's built-in camera as a capture source (`source: robot`): config, confirmation, capture through the robot
server (the fake one: nothing leaves this machine), and the rules for when a picture may be taken."""

import asyncio
import json
import socket
import time

import anyio
import pytest

import ahc.server as server_module
from ahc import cameras as cams
from ahc.core.errors import LabError
from ahc.examples import OT2_LAYOUT
from ahc.server import confirm_summary
from ahc.workspace import CameraEntry, Config, DeviceEntry, estop, parse_config
from conftest import confirm_as_person, load_verified, robot_entry
from fake_robot import make_jpeg
from test_cameras import SMALL, FakeFfmpeg, ffmpeg, home  # noqa: F401 - fixtures

pytestmark = pytest.mark.anyio

ROBOT_CAM = {"name": "deck", "source": "robot", "device": "ot2"}
OT2_ROBOT = DeviceEntry("ot2", "opentrons.ot2", "robot", {"host": "127.0.0.1"})


async def robot_cam_lab(open_lab, folder, bot, cameras=(ROBOT_CAM,)):
  """A confirmed real-OT-2 config (the fake robot) with cameras, the person's go-ahead given."""
  lab_cm = open_lab(folder, backend=None)
  lab = await lab_cm.__aenter__()
  await lab.call("configure_devices", devices=robot_entry("127.0.0.1", bot.port, robot_name=bot.name), cameras=list(cameras))
  await confirm_as_person(lab)
  return lab_cm, lab


# -- the config key and the confirmation ----------------------------------------------------------

def test_a_robot_camera_parses_and_the_mac_one_keeps_its_shape():
  config = parse_config("devices: [{id: ot2, model: opentrons.ot2, backend: robot, options: {host: 10.0.0.5}}]\n"
                        "cameras:\n  - {name: deck, source: robot, device: ot2}\n  - {name: top, device: 'USB Camera'}\n"
                        "  - {name: mac, source: mac, device: 'Other'}\n")
  assert config.cameras == [CameraEntry("deck", "ot2", None, "robot"), CameraEntry("top", "USB Camera"), CameraEntry("mac", "Other")]
  assert config.data()["cameras"] == [{"name": "deck", "device": "ot2", "source": "robot"},
                                      {"name": "top", "device": "USB Camera"}, {"name": "mac", "device": "Other"}]  # mac: no source key


OT2_DEVICES = "[{id: ot2, model: opentrons.ot2, backend: robot, options: {host: h}}]"
STAR_DEVICES = "[{id: starlet, model: hamilton.starlet, backend: sim}]"


@pytest.mark.parametrize("devices, cameras, why", [
  (OT2_DEVICES, "[{name: deck, source: robot, device: 'USB Camera'}]", "id of the OT-2"),  # a Mac device name is no device id
  (OT2_DEVICES, "[{name: deck, source: robot, device: nothere}]", "configured OT-2"),
  (STAR_DEVICES, "[{name: deck, source: robot, device: starlet}]", "configured OT-2"),  # not an OT-2
  (OT2_DEVICES, "[{name: deck, source: usb, device: ot2}]", "must be one of"),
  (OT2_DEVICES, "[{name: deck, source: robot}]", "id of the OT-2"),
])
def test_bad_robot_cameras_are_refused(devices, cameras, why):
  with pytest.raises(LabError) as caught:
    parse_config(f"devices: {devices}\ncameras: {cameras}\n")
  assert caught.value.code == "config_invalid" and why in str(caught.value)


def test_a_robot_camera_needs_the_real_robot():
  with pytest.raises(LabError) as caught:
    parse_config("devices: [{id: ot2, model: opentrons.ot2, backend: sim}]\ncameras: [{name: deck, source: robot, device: ot2}]\n")
  assert caught.value.code == "config_invalid" and "real robot" in str(caught.value)


def test_the_source_is_in_the_digest_and_the_summary():
  base = Config(devices=[OT2_ROBOT], limits={})
  robot = Config(devices=[OT2_ROBOT], limits={}, cameras=[CameraEntry("deck", "ot2", None, "robot")])
  mac = Config(devices=[OT2_ROBOT], limits={}, cameras=[CameraEntry("deck", "ot2")])
  assert len({base.digest(), robot.digest(), mac.digest()}) == 3  # the same name and device, another source: another digest
  assert confirm_summary(robot).endswith("; cameras: deck 'ot2' (the built-in camera of the robot ot2)")
  robot.confirm("human")
  robot.cameras = [CameraEntry("deck", "ot2")]  # the source changed after the confirmation: pending again
  assert robot.status(False)[0] == "pending"


async def test_configure_validates_the_robot_camera_and_needs_no_ffmpeg(tmp_path, open_lab, fake_robot, monkeypatch):
  monkeypatch.setattr(cams, "ffmpeg", lambda: None)  # a robot camera needs none
  async with open_lab(tmp_path, backend=None) as lab:
    devices = robot_entry("127.0.0.1", fake_robot.port, robot_name=fake_robot.name)
    assert await lab.refused("configure_devices", devices=devices, cameras=[{**ROBOT_CAM, "device": "nothere"}]) == "config_invalid"
    assert await lab.refused("configure_devices", devices=[{"id": "ot2", "model": "opentrons.ot2", "backend": "sim"}],
                             cameras=[ROBOT_CAM]) == "config_invalid"
    assert not (tmp_path / ".ahc" / "config.yaml").exists()
    written = await lab.call("configure_devices", devices=devices, cameras=[ROBOT_CAM])
    assert written["config"]["cameras"] == [{"name": "deck", "device": "ot2", "source": "robot"}]
    assert written["config"]["confirm_summary"].endswith("cameras: deck 'ot2' (the built-in camera of the robot ot2)")
    assert "source: robot" in (tmp_path / ".ahc" / "config.yaml").read_text()
    # devices replaced by a simulated one while the camera stays: refused, not a config that cannot be read
    assert await lab.refused("configure_devices", devices=[{"id": "ot2", "model": "opentrons.ot2", "backend": "sim"}]) == "config_invalid"


# -- capture ---------------------------------------------------------------------------------------

async def test_capture_takes_the_robots_picture_and_keeps_it(tmp_path, open_lab, fake_robot, monkeypatch):
  monkeypatch.setattr(cams, "ffmpeg", lambda: None)  # no ffmpeg: the 640 px picture needs no downscale
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot)
  try:
    await lab.call("report_decision", decision="Plan", plan=["Look at the deck"])
    raw = await lab.client.call_tool("capture", {"label": "the empty deck"})
    assert not raw.is_error and [c.type for c in raw.content] == ["text", "text", "image"]
    result = raw.structured_content
    assert result["failures"] == [] and [i["camera"] for i in result["images"]] == ["deck"]
    path = cams.Path(result["images"][0]["path"])
    assert path.read_bytes() == fake_robot.picture and path.parent == tmp_path / ".ahc" / "runs" / result["run_id"] / "0_preset"
    assert path.name.startswith("deck_") and path.suffix == ".jpg"
    import base64
    assert base64.b64decode(raw.content[2].data) == fake_robot.picture  # returned inline as it is
    row = json.loads((path.parent / "captures.jsonl").read_text())
    assert (row["camera"], row["device"], row["label"]) == ("deck", "ot2", "the empty deck")
    asked = [(m, p) for _, m, p, _ in fake_robot.log if p.startswith("/camera")]
    assert asked == [("GET", "/camera"), ("POST", "/camera/picture")]  # the setting first, then one picture
    logged = [e for e in lab.server.lab.runlog.entries if e.get("op") == "capture"]
    assert logged[0]["status"] == "ok"
  finally:
    await cm.__aexit__(None, None, None)


async def test_a_disabled_camera_is_a_capture_error_with_a_hint_and_nothing_is_written(tmp_path, open_lab, fake_robot):
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot)
  try:
    fake_robot.camera_enabled = False
    text = await lab.call_text("capture", label="x")
    assert "[camera_disabled]" in text and "Opentrons App" in text and "never changes the robot's settings" in text
    assert [(m, p) for _, m, p, _ in fake_robot.log if p.startswith("/camera")] == [("GET", "/camera")]  # no picture asked for
    assert all(m == "GET" for _, m, p, _ in fake_robot.log if "setting" in p)  # nothing written to the robot
    fake_robot.camera_enabled, fake_robot.picture_status = True, 422  # the robot says so only when asked for the picture
    assert await lab.refused("capture", label="x") == "camera_disabled"
    refused = [e for e in lab.server.lab.runlog.entries if e.get("op") == "capture" and e["status"] == "refused"]
    assert [e["error"] for e in refused] == ["camera_disabled", "camera_disabled"]
    fake_robot.picture_status = None  # the person turned it on
    assert len((await lab.call("capture", label="x"))["images"]) == 1
  finally:
    await cm.__aexit__(None, None, None)


async def test_a_failing_robot_camera_never_becomes_a_device_error(tmp_path, open_lab, fake_robot):
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot)
  try:
    server = lab.server.lab
    await load_verified(lab, layout=OT2_LAYOUT)
    fake_robot.picture_status = 500
    text = await lab.call_text("capture", label="x")
    assert "[camera_failed]" in text and "500" in text
    fake_robot.picture_status = None
    fake_robot.picture = b"not a jpeg"
    assert await lab.refused("capture", label="x") == "camera_failed"
    fake_robot.picture = make_jpeg()
    # nothing about the device changed: no unknown outcome, the deck is still checked, motion still works
    assert server.adapter.outcome_unknown is None and server.interrupted is None and server.gate.public()["state"] == "passed"
    await lab.call("pick_up_tips", device="ot2.right", tips=["tips:A1:H1"])
    with socket.socket() as spare:  # the robot's server stops answering: nothing listens at its address any more
      spare.bind(("127.0.0.1", 0))
      server.adapter.port = spare.getsockname()[1]
    assert await lab.refused("capture", label="x") == "camera_unreachable"
    assert server.adapter.outcome_unknown is None and server.interrupted is None
  finally:
    await cm.__aexit__(None, None, None)


async def test_a_robot_failure_next_to_a_mac_camera_fails_alone(tmp_path, open_lab, fake_robot, ffmpeg):
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot, cameras=(ROBOT_CAM, {"name": "top", "device": "USB Camera"}))
  try:
    fake_robot.camera_enabled = False
    result = await lab.call("capture", label="both")
    assert [i["camera"] for i in result["images"]] == ["top"]
    [failure] = result["failures"]
    assert failure["camera"] == "deck" and failure["code"] == "camera_disabled" and "Opentrons App" in failure["hint"]
  finally:
    await cm.__aexit__(None, None, None)


async def test_no_picture_while_a_motion_call_runs_and_the_capture_waits_for_it(tmp_path, open_lab, fake_robot):
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot)
  try:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device="ot2.right", tips=["tips:A1:H1"])
    fake_robot.durations["aspirateInPlace"] = 1.0
    moved: list[str] = []

    async def aspirate():
      await lab.call("aspirate", device="ot2.right", targets=["diluent"], volumes=100)
      moved.append("done")

    async with anyio.create_task_group() as tg:
      tg.start_soon(aspirate)
      with anyio.fail_after(10):
        while not any(m == "EXEC-START" and p == "aspirateInPlace" for _, m, p, _ in fake_robot.log):
          await anyio.sleep(0.02)
        result = await lab.call("capture", label="after the aspirate")  # waits for the call, then takes the picture
    assert moved == ["done"] and len(result["images"]) == 1
    ended = max(t for t, m, p, _ in fake_robot.log if m in ("EXEC-END", "EXEC-DONE") and p == "aspirateInPlace") \
        if any(m in ("EXEC-END", "EXEC-DONE") for _, m, p, _ in fake_robot.log) else 0
    asked = min(t for t, m, p, _ in fake_robot.log if p == "/camera/picture")
    assert asked >= ended  # the picture was asked for after the motion command finished
  finally:
    await cm.__aexit__(None, None, None)


async def test_the_wait_for_a_motion_call_is_bounded(tmp_path, open_lab, fake_robot, monkeypatch):
  monkeypatch.setattr(server_module, "ROBOT_BUSY_WAIT_S", 0.3)
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot)
  try:
    await load_verified(lab, layout=OT2_LAYOUT)
    await lab.call("pick_up_tips", device="ot2.right", tips=["tips:A1:H1"])
    fake_robot.durations["aspirateInPlace"] = 1.5
    texts: list[str] = []

    async def aspirate():
      await lab.call("aspirate", device="ot2.right", targets=["diluent"], volumes=100)

    async with anyio.create_task_group() as tg:
      tg.start_soon(aspirate)
      with anyio.fail_after(10):
        while not any(m == "EXEC-START" and p == "aspirateInPlace" for _, m, p, _ in fake_robot.log):
          await anyio.sleep(0.02)
        texts.append(await lab.call_text("capture", label="too soon"))
    assert "[robot_busy]" in texts[0] and "robot's camera" in texts[0]
    assert fake_robot.pictures_taken == 0
    assert any(e.get("op") == "capture" and e.get("error") == "robot_busy" for e in lab.server.lab.runlog.entries)
    assert len((await lab.call("capture", label="now"))["images"]) == 1  # once it is over
  finally:
    await cm.__aexit__(None, None, None)


async def test_a_motion_that_begins_during_the_picture_makes_it_useless(tmp_path, open_lab, fake_robot):
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot)
  try:
    server = lab.server.lab
    taken: list[int] = []
    real = server.adapter.camera_picture

    def picture(*args):
      data = real(*args)
      taken.append(1)
      if len(taken) == 1:
        server.last_motion = time.time() + 60  # a motion call began while the robot took it
      return data

    server.adapter.camera_picture = picture
    text = await lab.call_text("capture", label="x")
    assert "[robot_busy]" in text and "moved while its picture was taken" in text and len(taken) == 2  # tried once more
  finally:
    await cm.__aexit__(None, None, None)


async def test_the_robot_camera_captures_during_an_emergency_stop(tmp_path, open_lab, fake_robot):
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot)
  try:
    estop.engage(lab.server.lab.ws, by="test")
    result = await lab.call("capture", label="after the stop")
    assert len(result["images"]) == 1 and "emergency stop is engaged" in result["note"]
  finally:
    await cm.__aexit__(None, None, None)


async def test_no_robot_picture_in_simulation(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    assert await lab.refused("configure_devices", devices=[{"id": "ot2", "model": "opentrons.ot2", "backend": "sim"}],
                             cameras=[ROBOT_CAM]) == "config_invalid"
    assert await lab.refused("capture", label="x") in ("no_cameras", "camera_not_in_simulation")


async def test_map_camera_and_vision_work_on_the_robots_picture(tmp_path, open_lab, fake_robot, monkeypatch):
  monkeypatch.setattr(cams, "ffmpeg", lambda: None)
  cm, lab = await robot_cam_lab(open_lab, tmp_path, fake_robot)
  try:
    taken = await lab.call("map_camera", camera="deck")
    assert taken["image_size"] == [640, 480] and cams.Path(taken["saved_as"]).read_bytes() == fake_robot.picture
    boxes = {"1": [20, 20, 160, 150], "2": [170, 20, 310, 150]}
    mapped = await lab.call("map_camera", camera="deck", slots=boxes, view="side")
    assert mapped["slot_map"]["size"] == [640, 480] and mapped["slot_map"]["view"] == "side"
    assert (tmp_path / ".ahc" / "cameras" / "deck" / "reference.jpg").read_bytes() == fake_robot.picture
    pending = (await lab.call("lab_overview"))["config"]
    assert pending["status"] == "pending" and pending["cameras"][0]["source"] == "robot"  # the new map waits for the person
    await confirm_as_person(lab)
    image = (await lab.call("capture", label="mapped"))["images"][0]
    assert image["map"] in ("ok", "unclear", "needs_new_map") and "slots" in image  # analysed against the slot map
  finally:
    await cm.__aexit__(None, None, None)
