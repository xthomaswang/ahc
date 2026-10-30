"""The plugin's hooks as files: they run before every tool call in every Claude Code session on the
machine, from the plugin's folder, so a broken one must fail here and never in a session."""

import json
import os
import re
import subprocess

import pytest

from hookenv import HAS_SYSTEM_PYTHON, HOOKS, SYSTEM_PYTHON

SCRIPTS = sorted(HOOKS.glob("*.sh"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_hook_script_parses_and_is_executable(script):
  out = subprocess.run(["/bin/sh", "-n", str(script)], capture_output=True, text=True)
  assert out.returncode == 0, out.stderr  # a syntax error exits 2, which blocks the tool call
  assert os.access(script, os.X_OK)


def test_the_feed_runs_on_the_system_python():
  if not HAS_SYSTEM_PYTHON:
    pytest.skip(f"no working {SYSTEM_PYTHON}")
  source = HOOKS / "agent_feed.py"
  out = subprocess.run([SYSTEM_PYTHON, "-I", "-c", "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')",
                        str(source)], capture_output=True, text=True)
  assert out.returncode == 0, out.stderr


def test_hooks_json_names_the_hooks_that_exist():
  config = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
  commands = [h["command"] for groups in config.values() for group in groups for h in group["hooks"]]
  for command in commands:
    script = re.match(r'"\$\{CLAUDE_PLUGIN_ROOT\}/hooks/([\w.-]+)"', command).group(1)
    assert (HOOKS / script).is_file(), command
  every_tool, ahc_tools = config["PreToolUse"]
  assert every_tool["matcher"] == "*" and "estop-guard.sh" in every_tool["hooks"][0]["command"]
  assert every_tool["hooks"][1]["command"].endswith(" pre")
  pattern = re.compile(ahc_tools["matcher"])  # Claude Code reads a matcher with other characters as a regex
  assert pattern.fullmatch("mcp__plugin_ahc_ahc__lab_overview") and not pattern.search("Bash")
  assert [h["command"].split()[-1] for h in ahc_tools["hooks"]] == ["start"]
  assert all(h.get("timeout") for groups in config.values() for group in groups for h in group["hooks"])
