import json
import re
import urllib.request
from contextlib import asynccontextmanager

import pytest
from mcp import Client

from ahc.server import create_server

import os

# OT-2 tests run only against a simulator named on purpose: whatever answers on 31950 may be a
# task's own simulator, and these tests must not borrow it.
OT2_HOST = "127.0.0.1"
OT2_PORT = int(os.environ["AHC_TEST_OT2_PORT"]) if os.environ.get("AHC_TEST_OT2_PORT") else None


# Test modules set `pytestmark = pytest.mark.anyio`: anyio's plugin runs async fixtures and the
# test in one task, which MCP's task groups need.
@pytest.fixture
def anyio_backend():
  return "asyncio"


class Refused(Exception):
  def __init__(self, text: str):
    super().__init__(text)
    match = re.search(r"\[([a-z_]+)\]", text)
    self.code = match.group(1) if match else None


# The tools that name their plan step (the server refuses them before a plan is reported).
PLAN_TOOLS = {"load_layout", "verify", "pick_up_tips", "aspirate", "dispense", "mix", "drop_tips", "invoke"}


class LabClient:
  """An MCP client connected in-process, as an agent would see the server.

  Tests that are not about the plan get one: before the first call that needs it, a one-step plan is
  reported, and calls that name no step name step 1. auto_plan=False turns this off.
  """

  def __init__(self, client: Client, server, auto_plan: bool = True):
    self.client = client
    self.server = server
    self.auto_plan = auto_plan

  async def _args(self, tool: str, args: dict) -> dict:
    if not self.auto_plan or tool not in PLAN_TOOLS or "plan_step" in args:
      return args
    if self.server.lab.plan.steps is None:
      result = await self.client.call_tool("report_decision", {"decision": "Test run", "plan": ["Test"]})
      assert not result.is_error, result.content[0].text
    return {**args, "plan_step": 1}

  async def call(self, tool: str, **args):
    result = await self.client.call_tool(tool, await self._args(tool, args))
    if result.is_error:
      raise Refused(result.content[0].text)
    return result.structured_content

  async def call_text(self, tool: str, **args) -> str:
    """The refusal text of a call that must be refused."""
    result = await self.client.call_tool(tool, await self._args(tool, args))
    assert result.is_error, f"{tool}({args}) was not refused"
    return result.content[0].text

  async def refused(self, tool: str, **args) -> str:
    """Call a tool that must be refused; return the refusal code."""
    try:
      await self.call(tool, **args)
    except Refused as exc:
      assert exc.code, f"refusal without a code: {exc}"
      return exc.code
    raise AssertionError(f"{tool}({args}) was not refused")


def robot_server_up() -> bool:
  if OT2_PORT is None:
    return False
  try:
    request = urllib.request.Request(f"http://{OT2_HOST}:{OT2_PORT}/health",
                                     headers={"Opentrons-Version": "*"})
    with urllib.request.urlopen(request, timeout=2) as response:
      return json.load(response).get("robot_model") == "OT-2 Standard"
  except OSError:
    return False


def task_folder(tmp_path, name: str):
  folder = tmp_path / name
  folder.mkdir()
  return folder


@pytest.fixture
async def starlet(tmp_path):
  server = create_server(task_folder(tmp_path, "starlet-task"), "sim", "hamilton.starlet")
  async with Client(server) as client:
    yield LabClient(client, server)


@pytest.fixture
async def ot2(tmp_path):
  if not robot_server_up():
    pytest.skip("set AHC_TEST_OT2_PORT to a running OT-2 simulator's port (ahc-sim ot2 in a test folder)")
  server = create_server(task_folder(tmp_path, "ot2-task"), "sim", "opentrons.ot2",
                         {"host": OT2_HOST, "port": OT2_PORT})
  async with Client(server) as client:
    yield LabClient(client, server)


@pytest.fixture
def open_lab():
  """`async with open_lab(folder, backend, device) as lab:` a client on a server for that task folder."""
  @asynccontextmanager
  async def opener(folder, backend="sim", device=None, options=None, auto_plan=True):
    server = create_server(folder, backend, device, options)
    async with Client(server) as client:
      yield LabClient(client, server, auto_plan)
  return opener


def give_reference(lab, text: str = "# Deck\nThe example deck, as the person set it up.\n") -> None:
  """A reference the person provides (a protocol note): new layouts are then judged by the simulator."""
  protocols = lab.server.lab.ws.protocols_dir
  protocols.mkdir(parents=True, exist_ok=True)
  (protocols / "deck.md").write_text(text)


async def load_verified(lab, **args):
  """load_layout, then the deck check that opens the motion gate. The simulator judges it, except for
  a layout with no reference in the folder: then the person confirms it, which the test does here."""
  loaded = await lab.call("load_layout", **args)
  step = {"plan_step": args["plan_step"]} if "plan_step" in args else {}
  verdict = await lab.call("verify", check="deck_matches_layout", **step)
  if verdict["verdict"] == "pending" and verdict["gate"]["needs_person"]:
    verdict = await lab.call("record_verdict", check_id=verdict["check_id"], verdict="pass")
  assert verdict["verdict"] == "pass" and verdict["gate"]["state"] == "passed"
  return loaded
