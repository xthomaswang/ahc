"""The run log's header names the code that ran: AHC's own checkout and PyLabRobot's commit."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from ahc.records import RunLog, environment

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
  return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_the_run_log_names_the_code_that_ran(tmp_path):
  log = RunLog(tmp_path / "runs", "hamilton.starlet", "sim")
  log.record({"type": "agent", "decision": "Start"})
  header = json.loads(log.path.read_text().splitlines()[0])
  assert header["type"] == "run" and header["environment"].keys() >= {"pylabrobot", "pylabrobot_commit", "ahc"}
  pin = re.search(r"pylabrobot@([0-9a-f]{40})", (ROOT / "pyproject.toml").read_text()).group(1)
  assert environment()["pylabrobot_commit"] == pin  # the pin PyLabRobot resolved to, not just its version
  if not (ROOT / ".git").exists():
    pytest.skip("AHC runs from an installed package here, not a checkout")
  checkout = header["environment"]["ahc_checkout"]
  assert checkout["path"] == str(ROOT) and checkout["commit"] == git("rev-parse", "HEAD")
  assert checkout["dirty"] == bool(git("status", "--porcelain", "--untracked-files=no"))
