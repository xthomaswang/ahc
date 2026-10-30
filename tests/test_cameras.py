"""Camera captures: the config key, the confirmation, the run folder, the tool's image blocks and photos as
evidence. ffmpeg is faked (cameras._run); the robot is the fake robot-server, so nothing leaves this machine."""

import base64
import hashlib
import json
import types

import pytest

from ahc import cameras as cams
from ahc.core.errors import LabError
from ahc.server import confirm_summary
from ahc.workspace import CameraEntry, Config, DeviceEntry, estop, parse_config
from conftest import confirm_as_person, robot_entry

pytestmark = pytest.mark.anyio

DEVICES = ["Intel(R) RealSense(TM) Depth Camera 435 with RGB Module RGB", "USB Camera", "MacBook Pro Camera",
           "Capture screen 0"]
TOP = {"name": "top", "device": "USB Camera"}
SIDE = {"name": "side", "device": "Intel(R) RealSense(TM) Depth Camera 435 with RGB Module RGB"}
FULL = b"\xff\xd8" + b"full-size frame" * 50 + b"\xff\xd9"
SMALL = b"\xff\xd8small\xff\xd9"
STARLET = [{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}]


class FakeFfmpeg:
  """Stands in for cameras._run: lists DEVICES, writes FULL for a capture and SMALL for a downscale."""

  def __init__(self):
    self.calls: list[list[str]] = []
    self.fail: dict[str, str] = {}  # device -> the error ffmpeg prints

  def __call__(self, args, timeout):
    self.calls.append(args)
    if "-list_devices" in args:
      lines = ["[AVFoundation indev @ 0x1] AVFoundation video devices:"]
      lines += [f"[AVFoundation indev @ 0x1] [{i}] {d}" for i, d in enumerate(DEVICES)]
      lines += ["[AVFoundation indev @ 0x1] AVFoundation audio devices:", "[AVFoundation indev @ 0x1] [0] Microphone"]
      return 1, "\n".join(lines)
    out = cams.Path(args[-1])
    if "-vf" in args:
      out.write_bytes(SMALL)
      return 0, ""
    device = args[args.index("-i") + 1].removesuffix(":none")
    if device in self.fail:
      return 1, f"warning\n{self.fail[device]}"
    out.write_bytes(FULL)
    return 0, ""

  def captures(self):
    return [a for a in self.calls if "-list_devices" not in a and "-vf" not in a]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
  monkeypatch.setenv("AHC_HOME", str(tmp_path / "ahc-home"))
  monkeypatch.delenv("AHC_BACKEND", raising=False)


@pytest.fixture
def ffmpeg(monkeypatch):
  fake = FakeFfmpeg()
  monkeypatch.setattr(cams, "_run", fake)
  monkeypatch.setattr(cams, "ffmpeg", lambda: "/fake/ffmpeg")
  monkeypatch.setattr(cams, "sys", types.SimpleNamespace(platform="darwin"))
  return fake


def digest_of(devices, limits):
  """What Config.digest was before cameras existed."""
  body = json.dumps({"devices": devices, "limits": limits}, sort_keys=True)
  return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


async def real_lab(open_lab, folder, bot, cameras=(TOP, SIDE)):
  """A confirmed real-robot config (the fake robot) with cameras, the person's go-ahead given."""
  lab_cm = open_lab(folder, backend=None)
  lab = await lab_cm.__aenter__()
  await lab.call("configure_devices", devices=robot_entry("127.0.0.1", bot.port, robot_name=bot.name),
                 **({"cameras": list(cameras)} if cameras is not None else {}))
  await confirm_as_person(lab)
  return lab_cm, lab


def run_log(folder):
  return [json.loads(line) for log in sorted((folder / ".ahc" / "runs").glob("*.jsonl")) for line in log.read_text().splitlines()]


# -- the config key and the confirmation ----------------------------------------------------------

def test_config_parses_cameras():
  config = parse_config("devices: []\ncameras:\n  - {name: top, device: 'USB Camera'}\n  - {name: side, device: 'X'}\n")
  assert config.cameras == [CameraEntry("top", "USB Camera"), CameraEntry("side", "X")]
  assert config.data()["cameras"] == [{"name": "top", "device": "USB Camera"}, {"name": "side", "device": "X"}]
  assert parse_config("devices: []\n").cameras == [] and "cameras" not in parse_config("devices: []\n").data()


@pytest.mark.parametrize("raw", ["cameras: top", "cameras: [top]", "cameras: [{name: Top, device: X}]",
                                 "cameras: [{name: top}]", "cameras: [{name: top, device: ''}]",
                                 "cameras: [{name: top, device: X, index: 0}]",
                                 "cameras: [{name: top, device: X}, {name: top, device: Y}]"])
def test_bad_cameras_are_refused(raw):
  with pytest.raises(LabError) as caught:
    parse_config("devices: []\n" + raw)
  assert caught.value.code == "config_invalid"


def test_a_config_without_cameras_keeps_its_digest():
  entry = DeviceEntry("starlet", "hamilton.starlet", "sim")
  limits = {"starlet.pip": {"volume_ul": {"max": 200}}}
  config = Config(devices=[entry], limits=limits)
  assert config.digest() == digest_of([{"id": "starlet", "model": "hamilton.starlet", "backend": "sim", "options": {}}], limits)
  config.confirm("human")
  assert config.status(False) == ("confirmed", "confirmed by the person")
  with_camera = Config(devices=[entry], limits=limits, cameras=[CameraEntry("top", "USB Camera")])
  assert with_camera.digest() != config.digest()
  config.cameras = [CameraEntry("top", "USB Camera")]  # cameras added after the confirmation: pending again
  assert config.status(False)[0] == "pending" and "cameras" in config.status(False)[1]


def test_summary_names_cameras_only_when_there_are_some():
  config = Config(devices=[DeviceEntry("starlet", "hamilton.starlet", "sim")], limits={})
  assert "cameras" not in confirm_summary(config)
  config.cameras = [CameraEntry("top", "USB Camera"), CameraEntry("side", "Depth Cam")]
  assert confirm_summary(config).endswith("; task limits: none; cameras: top 'USB Camera', side 'Depth Cam'")


async def test_configure_keeps_or_removes_cameras(tmp_path, open_lab, fake_robot, ffmpeg):
  cm, lab = await real_lab(open_lab, tmp_path, fake_robot)
  try:
    status = (await lab.call("lab_overview"))["config"]
    assert status["status"] == "confirmed" and [c["name"] for c in status["cameras"]] == ["top", "side"]
    assert status["confirm_summary"].endswith("cameras: top 'USB Camera', side " + repr(SIDE["device"]))
    devices = robot_entry("127.0.0.1", fake_robot.port, robot_name=fake_robot.name)
    kept = await lab.call("configure_devices", devices=devices)  # cameras omitted: they stay, and so does the confirmation
    assert kept["config"]["status"] == "confirmed" and len(kept["config"]["cameras"]) == 2
    one = await lab.call("configure_devices", devices=devices, cameras=[TOP])
    assert one["config"]["status"] == "pending" and [c["name"] for c in one["config"]["cameras"]] == ["top"]
    await confirm_as_person(lab)
    none = await lab.call("configure_devices", devices=devices, cameras=[])
    assert none["config"]["status"] == "pending" and "cameras" not in none["config"]
    assert "cameras" not in (tmp_path / ".ahc" / "config.yaml").read_text().split("confirmation")[0]
  finally:
    await cm.__aexit__(None, None, None)


async def test_configure_checks_the_camera_exists(tmp_path, open_lab, fake_robot, ffmpeg, monkeypatch):
  async with open_lab(tmp_path, backend=None) as lab:
    devices = robot_entry("127.0.0.1", fake_robot.port, robot_name=fake_robot.name)
    text = await lab.call_text("configure_devices", devices=devices, cameras=[{"name": "top", "device": "Webcam"}])
    assert "[camera_not_found]" in text and "'USB Camera'" in text and "MacBook Pro Camera" in text
    assert "Capture screen" not in text  # a screen is no camera
    assert not (tmp_path / ".ahc" / "config.yaml").exists()
    monkeypatch.setattr(cams, "ffmpeg", lambda: None)
    text = await lab.call_text("configure_devices", devices=devices, cameras=[TOP])
    assert "[camera_unavailable]" in text and "brew install ffmpeg" in text
    monkeypatch.setattr(cams, "ffmpeg", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(cams, "sys", types.SimpleNamespace(platform="linux"))
    assert "[camera_unavailable]" in await lab.call_text("configure_devices", devices=devices, cameras=[TOP])
    assert await lab.refused("configure_devices", devices=devices, cameras=[{"name": "Top", "device": "x"}]) == "config_invalid"


# -- folder naming ----------------------------------------------------------------------------------

def test_step_folders():
  steps = ["Pick up tips", "Aspirate 100 uL of buffer from the reservoir's well A1 into column 2", "Report the result"]
  assert cams.step_folder(None, None, False) == "0_preset"
  assert cams.step_folder(steps, None, False) == "0_preset"
  assert cams.step_folder(steps, 1, False) == "1_pick-up-tips"
  second = cams.step_folder(steps, 2, False)
  assert second == "2_aspirate-100-ul-of-buffer-from-the" and len(second.split("_", 1)[1]) <= 40
  assert cams.step_folder(steps, 3, True) == "4_results" and cams.step_folder(steps, None, True) == "4_results"
  assert cams.step_folder(None, None, True) == "1_results"
  assert cams.slug("  !!  ") == "step" and cams.slug("A" * 100) == "a" * 40


def test_a_clash_gets_a_number(tmp_path):
  first = cams.unique_path(tmp_path, "top", 1000.0)
  first.write_bytes(b"x")
  second = cams.unique_path(tmp_path, "top", 1000.0)
  second.write_bytes(b"x")
  third = cams.unique_path(tmp_path, "top", 1000.0)
  assert [p.name.split("_", 1)[1] for p in (first, second, third)] == [first.name.split("_", 1)[1],
                                                                       first.name.split("_", 1)[1].replace(".jpg", "-2.jpg"),
                                                                       first.name.split("_", 1)[1].replace(".jpg", "-3.jpg")]


# -- the capture tool -------------------------------------------------------------------------------

async def test_capture_returns_images_and_keeps_the_files(tmp_path, open_lab, fake_robot, ffmpeg):
  cm, lab = await real_lab(open_lab, tmp_path, fake_robot)
  try:
    await lab.call("report_decision", decision="Plan", plan=["Pick up tips", "Aspirate 100 uL from A1"])
    raw = await lab.client.call_tool("capture", {"label": "before the pick-up"})
    assert not raw.is_error
    kinds = [c.type for c in raw.content]
    assert kinds == ["text", "text", "image", "text", "image"]  # the summary, then a caption and an image per camera
    images = [c for c in raw.content if c.type == "image"]
    assert all(i.mime_type == "image/jpeg" and base64.b64decode(i.data) == SMALL for i in images)
    result = raw.structured_content
    assert result["failures"] == [] and result["final"] is False and result["plan_step"] is None
    folder = tmp_path / ".ahc" / "runs" / result["run_id"] / "0_preset"
    assert result["folder"] == str(folder)
    assert [i["camera"] for i in result["images"]] == ["top", "side"]
    for image in result["images"]:
      path = cams.Path(image["path"])
      assert path.parent == folder and path.name.startswith(image["camera"] + "_") and path.read_bytes() == FULL
    lines = [json.loads(line) for line in (folder / "captures.jsonl").read_text().splitlines()]
    assert [(l["camera"], l["device"], l["label"], l["plan_step"], l["final"], l["run_id"]) for l in lines] == [
      ("top", "USB Camera", "before the pick-up", None, False, result["run_id"]),
      ("side", SIDE["device"], "before the pick-up", None, False, result["run_id"])]
    assert [l["file"] for l in lines] == [cams.Path(i["path"]).name for i in result["images"]] and lines[0]["time"]
    # the full-resolution capture ran as the brief says, one camera after another, by name
    first, second = ffmpeg.captures()
    assert first[first.index("-i") + 1] == "USB Camera:none" and second[second.index("-i") + 1] == SIDE["device"] + ":none"
    assert "nv12" in first and "-y" in first and first[first.index("-ss") + 1] == "1.5"
    assert any(a.startswith("scale='min(1280,iw)'") for call in ffmpeg.calls for a in call)
    # the call is in the run log, with the paths
    logged = [e for e in run_log(tmp_path) if e.get("op") == "capture"]
    assert len(logged) == 1 and logged[0]["status"] == "ok"
    assert [i["path"] for i in logged[0]["effective"]["images"]] == [i["path"] for i in result["images"]]
    # a run folder next to the log breaks neither the run summary nor the run listing
    assert (await lab.call("get_run"))["run_id"] == result["run_id"]
    assert [p.name for p in (tmp_path / ".ahc" / "runs").glob("*.jsonl")] == [result["run_id"] + ".jsonl"]
  finally:
    await cm.__aexit__(None, None, None)


async def test_capture_folders_follow_the_plan(tmp_path, open_lab, fake_robot, ffmpeg):
  cm, lab = await real_lab(open_lab, tmp_path, fake_robot)
  try:
    await lab.call("report_decision", decision="Plan", plan=["Pick up tips", "Aspirate 100 uL from A1", "Report"])
    run = (await lab.call("capture", label="start", cameras=["top"]))["run_id"]
    base = tmp_path / ".ahc" / "runs" / run
    second = await lab.call("capture", label="look", plan_step=2, cameras=["top"])
    assert second["folder"] == str(base / "2_aspirate-100-ul-from-a1") and second["plan_step"] == 2
    await lab.call("report_decision", decision="Now on step 1", current_step=1)
    assert (await lab.call("capture", label="on the step", cameras=["top"]))["folder"] == str(base / "1_pick-up-tips")
    final = await lab.call("capture", label="the result", final=True, plan_step=3)
    assert final["folder"] == str(base / "4_results") and final["final"] is True
    again = await lab.call("capture", label="again", plan_step=2, cameras=["top"])
    assert {cams.Path(i["path"]).parent.name for i in (second["images"][0], again["images"][0])} == {"2_aspirate-100-ul-from-a1"}
    assert len(list((base / "2_aspirate-100-ul-from-a1").glob("top_*.jpg"))) == 2  # a clash is numbered, nothing overwritten
    assert await lab.refused("capture", label="x", plan_step=9) == "bad_plan_step"
  finally:
    await cm.__aexit__(None, None, None)


async def test_capture_before_a_plan_goes_to_preset(tmp_path, open_lab, fake_robot, ffmpeg):
  cm, lab = await real_lab(open_lab, tmp_path, fake_robot)
  try:
    result = await lab.call("capture", label="the empty deck", cameras=["side"])
    assert result["folder"].endswith("0_preset") and [i["camera"] for i in result["images"]] == ["side"]
    assert await lab.refused("capture", label="x", plan_step=1) == "plan_required"
  finally:
    await cm.__aexit__(None, None, None)


async def test_a_failing_camera_fails_alone(tmp_path, open_lab, fake_robot, ffmpeg):
  cm, lab = await real_lab(open_lab, tmp_path, fake_robot)
  try:
    ffmpeg.fail[SIDE["device"]] = "Selected video device not found"
    raw = await lab.client.call_tool("capture", {"label": "both"})
    assert not raw.is_error and [c.type for c in raw.content].count("image") == 1
    result = raw.structured_content
    assert [i["camera"] for i in result["images"]] == ["top"]
    assert result["failures"] == [{"camera": "side", "device": SIDE["device"], "error": "Selected video device not found"}]
    folder = cams.Path(result["folder"])
    assert [p.name.split("_")[0] for p in folder.glob("*.jpg")] == ["top"]  # no partial file for the failed one
    assert len((folder / "captures.jsonl").read_text().splitlines()) == 1
    ffmpeg.fail["USB Camera"] = "Input/output error"
    text = await lab.call_text("capture", label="none works")
    assert "[capture_failed]" in text and "side: Selected video device not found" in text and "top: Input/output error" in text
    assert [e["error"] for e in run_log(tmp_path) if e.get("op") == "capture" and e["status"] == "refused"] == ["capture_failed"]
  finally:
    await cm.__aexit__(None, None, None)


async def test_capture_refusals(tmp_path, open_lab, fake_robot, ffmpeg):
  cm, lab = await real_lab(open_lab, tmp_path, fake_robot)
  try:
    assert await lab.refused("capture", label="x", cameras=["nope"]) == "unknown_camera"
    assert await lab.refused("capture", label="x", cameras=[]) == "unknown_camera"
    assert await lab.refused("capture", label="  ") == "bad_label"
    assert ffmpeg.captures() == []
  finally:
    await cm.__aexit__(None, None, None)
  other = tmp_path / "nocameras"
  other.mkdir()
  cm, lab = await real_lab(open_lab, other, fake_robot, cameras=None)
  try:
    assert await lab.refused("capture", label="x") == "no_cameras"
  finally:
    await cm.__aexit__(None, None, None)


async def test_no_capture_in_simulation(tmp_path, open_lab, ffmpeg):
  async with open_lab(tmp_path, "sim") as lab:  # forced simulation
    await lab.call("configure_devices", devices=STARLET, cameras=[TOP])
    assert (await lab.call("lab_overview"))["config"]["status"] == "confirmed"
    assert await lab.refused("capture", label="x") == "camera_not_in_simulation"
  assert ffmpeg.captures() == []
  folder = tmp_path / "plain"
  folder.mkdir()
  async with open_lab(folder, backend=None) as lab:  # a sim backend the person confirmed
    await lab.call("configure_devices", devices=STARLET, cameras=[TOP])
    await confirm_as_person(lab)
    assert await lab.refused("capture", label="x") == "camera_not_in_simulation"
  assert ffmpeg.captures() == [] and not list((folder / ".ahc" / "runs").glob("*/"))


async def test_capture_works_during_an_emergency_stop(tmp_path, open_lab, fake_robot, ffmpeg):
  cm, lab = await real_lab(open_lab, tmp_path, fake_robot)
  try:
    estop.engage(lab.server.lab.ws, by="test")
    assert await lab.refused("verify", check="tips_mounted", expect="none") == "emergency_stop"
    result = await lab.call("capture", label="the deck after the stop", cameras=["top"])
    assert len(result["images"]) == 1 and "emergency stop is engaged" in result["note"]
  finally:
    await cm.__aexit__(None, None, None)


# -- photos as evidence -----------------------------------------------------------------------------

async def test_verify_stores_images_as_evidence(tmp_path, open_lab, fake_robot, ffmpeg):
  cm, lab = await real_lab(open_lab, tmp_path, fake_robot)
  try:
    taken = await lab.call("capture", label="the pipettes", cameras=["top"])
    path = taken["images"][0]["path"]
    check = await lab.call("verify", check="tips_mounted", expect="none", images=[path])
    assert check["verdict"] == "pending" and check["judged_by"] == "human (placeholder)"  # the person still judges
    assert check["evidence"]["camera"]["images"] == [path] and "person" in check["evidence"]["camera"]["note"]
    listed = await lab.call("list_checks")
    assert listed["checks_with_images"] == [{"check_id": check["check_id"], "check": "tips_mounted", "images": [path],
                                             "verdict": "pending", "judged_by": "human (placeholder)"}]
    verdict = await lab.call("record_verdict", check_id=check["check_id"], verdict="pass", note="the person looked")
    assert verdict["evidence"]["camera"]["images"] == [path]
    assert (await lab.call("list_checks"))["checks_with_images"][0]["verdict"] == "pass"
    entries = [e for e in run_log(tmp_path) if e.get("type") == "verification" and e.get("evidence")]
    assert entries[0]["evidence"]["camera"]["images"] == [path]
    plain = await lab.call("verify", check="tips_mounted", expect="none")  # no photos: the old placeholder text
    assert isinstance(plain["evidence"]["camera"], str) and "no camera verifier" in plain["evidence"]["camera"]
  finally:
    await cm.__aexit__(None, None, None)


async def test_verify_refuses_images_outside_this_run(tmp_path, open_lab, fake_robot, ffmpeg):
  cm, lab = await real_lab(open_lab, tmp_path, fake_robot)
  try:
    stray = tmp_path / "stray.jpg"
    stray.write_bytes(FULL)
    assert await lab.refused("verify", check="tips_mounted", expect="none", images=[str(stray)]) == "image_outside_run"
    taken = await lab.call("capture", label="one", cameras=["top"])
    older = tmp_path / ".ahc" / "runs" / "20200101T000000-abcdef" / "0_preset" / "top_000000.jpg"
    older.parent.mkdir(parents=True)
    older.write_bytes(FULL)
    for bad in (str(older), str(tmp_path / ".ahc" / "runs" / taken["run_id"] / ".." / "other.jpg"), "../stray.jpg"):
      assert await lab.refused("verify", check="tips_mounted", expect="none", images=[bad]) == "image_outside_run"
    inside = cams.Path(taken["folder"])
    assert await lab.refused("verify", check="tips_mounted", expect="none", images=[str(inside / "ghost.jpg")]) == "image_not_found"
    assert await lab.refused("verify", check="tips_mounted", expect="none",
                             images=[str(inside / "captures.jsonl")]) == "image_not_found"
    refused = [e for e in run_log(tmp_path) if e.get("op") == "verify" and e.get("status") == "refused"]
    assert refused and refused[0]["basic"]["images"] == [str(stray)]
    ok = await lab.call("verify", check="tips_mounted", expect="none", images=[taken["images"][0]["path"]])
    assert ok["evidence"]["camera"]["images"]
  finally:
    await cm.__aexit__(None, None, None)


async def test_images_are_refused_before_any_capture(tmp_path, open_lab, ffmpeg):
  async with open_lab(tmp_path, "sim") as lab:
    await lab.call("configure_devices", devices=STARLET)
    assert await lab.refused("verify", check="tips_mounted", device="starlet.pip", expect="none",
                             images=[str(tmp_path / "x.jpg")]) == "image_outside_run"

