"""Cameras: still captures of the real deck, kept next to the run log and shown to the agent.

A task declares its cameras in the workspace config (`cameras: [{name: top, device: "USB Camera"}]`, part of
what the person confirms). capture takes one frame from each, with ffmpeg's AVFoundation input (macOS), and
the server hands the images to the agent. Full-resolution files stay on disk:

    .ahc/runs/<run_id>/<step folder>/<camera>_<HHMMSS>.jpg
    .ahc/runs/<run_id>/<step folder>/captures.jsonl     one line per image

The step folder is `0_preset` before any plan step, `<k>_<slug of step k>` for plan step k, and
`<N+1>_results` for the final capture of an N-step plan. AVFoundation indices swap between runs, so a
camera is always selected by its name. Nothing is masked and nothing leaves this machine.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ahc.core.errors import LabError

TIMEOUT_S = 20  # per capture; the process is killed after it
SKIP_S = 1.5  # the first frames are dark while the camera's exposure settles
MAX_WIDTH = 1280  # what the agent sees; the file on disk keeps the camera's resolution
SLUG_LIMIT = 40
INSTALL_HINT = "brew install ffmpeg"
DEVICE_LINE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")


def ffmpeg() -> str | None:
  return shutil.which("ffmpeg")


def require_ffmpeg() -> str:
  """The ffmpeg to use; a refusal where cameras cannot work (not macOS, or no ffmpeg)."""
  if sys.platform != "darwin":
    raise LabError("camera_unavailable", "cameras are captured through macOS's AVFoundation, and this is not macOS.",
                   f"Remove the cameras from the config (configure_devices cameras=[]), or run the server on the Mac "
                   f"the cameras are plugged into (it needs ffmpeg: {INSTALL_HINT}).")
  found = ffmpeg()
  if found is None:
    raise LabError("camera_unavailable", "ffmpeg is not installed, so no camera can be captured.", INSTALL_HINT)
  return found


def _run(args: list[str], timeout: float) -> tuple[int, str]:
  """Run ffmpeg; (return code, its stderr). A run past `timeout` is killed and reported as code -1. Tests replace this."""
  try:
    done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
  except subprocess.TimeoutExpired:
    return -1, f"timed out after {timeout:g} s"
  except OSError as exc:
    return -1, f"{type(exc).__name__}: {exc}"
  return done.returncode, done.stderr


def list_devices() -> list[str]:
  """The names of the AVFoundation video devices (screens excluded: they are no cameras)."""
  _, text = _run([require_ffmpeg(), "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""], 10)
  names: list[str] = []
  in_video = False
  for line in text.splitlines():
    if "AVFoundation video devices" in line:
      in_video = True
    elif "AVFoundation audio devices" in line:
      in_video = False
    elif in_video:
      match = DEVICE_LINE.search(line)
      if match and not match.group(2).startswith("Capture screen"):
        names.append(match.group(2))
  return names


def require_devices(devices: list[str]) -> None:
  """Each device name exists now; a refusal naming the ones that do."""
  available = list_devices()
  missing = [d for d in devices if d not in available]
  if missing:
    raise LabError("camera_not_found",
                   f"no camera named {', '.join(repr(m) for m in missing)} on this computer. Cameras available: "
                   f"{', '.join(repr(a) for a in available) or 'none'}.",
                   "Use one of the available names exactly as written (a camera must be plugged in and allowed for "
                   "this app), or ask the person which camera is which.")


def capture_frame(device: str, dest: Path) -> str | None:
  """One frame from `device` into `dest` (JPEG, the camera's resolution); None when it worked, else why not."""
  dest.parent.mkdir(parents=True, exist_ok=True)
  code, text = _run([require_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "avfoundation", "-framerate", "30", "-pixel_format", "nv12", "-i", f"{device}:none",
                     "-ss", str(SKIP_S), "-frames:v", "1", "-q:v", "2", str(dest)], TIMEOUT_S)
  if code == 0 and dest.is_file() and dest.stat().st_size > 0:
    return None
  dest.unlink(missing_ok=True)
  lines = [line.strip() for line in text.splitlines() if line.strip()]
  return (lines[-1] if lines else f"ffmpeg exited with code {code} and wrote no image")[:300]


def downscaled(path: Path, max_width: int = MAX_WIDTH) -> bytes:
  """The image as JPEG bytes no wider than `max_width` (its own size when narrower). Raises OSError if it cannot."""
  with tempfile.TemporaryDirectory(prefix="ahc-cam-") as tmp:
    small = Path(tmp) / "small.jpg"
    code, text = _run([require_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                       "-vf", f"scale='min({max_width},iw)':-2", "-frames:v", "1", "-q:v", "3", str(small)], TIMEOUT_S)
    if code != 0 or not small.is_file():
      raise OSError(text.strip().splitlines()[-1] if text.strip() else f"ffmpeg exited with code {code}")
    return small.read_bytes()


def slug(text: str, limit: int = SLUG_LIMIT) -> str:
  """Lowercase, hyphen-separated, at most `limit` characters (cut at a word where there is one)."""
  words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
  if len(words) > limit:
    cut = words[:limit]
    if words[limit] != "-" and "-" in cut:  # in the middle of a word: drop it
      cut = cut.rsplit("-", 1)[0]
    words = cut
  return words.strip("-") or "step"


def step_folder(steps: list[str] | None, plan_step: int | None, final: bool) -> str:
  """The name of the folder a capture goes to: see the module docstring."""
  if final:
    return f"{len(steps or []) + 1}_results"
  if plan_step is None or not steps or not 1 <= plan_step <= len(steps):
    return "0_preset"
  return f"{plan_step}_{slug(steps[plan_step - 1])}"


def unique_path(folder: Path, camera: str, when: float | None = None) -> Path:
  """<camera>_<HHMMSS>.jpg in `folder`; -2, -3 ... on a clash."""
  stamp = time.strftime("%H%M%S", time.localtime(when))
  path = folder / f"{camera}_{stamp}.jpg"
  n = 2
  while path.exists():
    path = folder / f"{camera}_{stamp}-{n}.jpg"
    n += 1
  return path


def append_captures(folder: Path, rows: list[dict[str, Any]]) -> None:
  """One line per image in the folder's captures.jsonl."""
  with (folder / "captures.jsonl").open("a") as f:
    for row in rows:
      f.write(json.dumps(row) + "\n")
