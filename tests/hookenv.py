"""Running the plugin's hooks as Claude Code does: with /bin/sh, and with the system python3 first on
PATH (`/usr/bin/env python3` in the hooks), the oldest Python they must run on (3.9 on macOS), not
this project's environment."""

import os
import subprocess
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
SYSTEM_PYTHON = "/usr/bin/python3"


def _system_python_works() -> bool:
  try:
    return subprocess.run([SYSTEM_PYTHON, "-c", "pass"], capture_output=True, timeout=30).returncode == 0
  except (OSError, subprocess.SubprocessError):
    return False


HAS_SYSTEM_PYTHON = _system_python_works()


def hook_env(**env: str) -> dict[str, str]:
  """This environment without the variables that choose a task folder, the system PATH, and `env`."""
  base = {k: v for k, v in os.environ.items()
          if k not in ("AHC_WORKSPACE", "AHC_HOME", "AHC_FEED", "CLAUDE_PROJECT_DIR")}
  if HAS_SYSTEM_PYTHON:
    base["PATH"] = "/usr/bin:/bin"
  return {**base, **env}


def run_hook(name: str, *args: str, stdin: str = "", **env: str) -> subprocess.CompletedProcess:
  return subprocess.run(["/bin/sh", str(HOOKS / name), *args], input=stdin, capture_output=True, text=True,
                        env=hook_env(**env), timeout=30)
