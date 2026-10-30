"""Robot discovery: mDNS output parsing, the /health filter and the find_robots tool. No network: the
subprocess runner and the HTTP probe are replaced."""

import pytest

from ahc import discovery
from ahc.core.errors import LabError

pytestmark = pytest.mark.anyio

BROWSE = """Browsing for _http._tcp.local.
DATE: ---Thu 29 Oct 2026---
18:20:29.935  ...STARTING...
Timestamp     A/R    Flags  if Domain               Service Type         Instance Name
18:20:29.936  Add        3  27 local.               _http._tcp.          opentrons
18:20:29.936  Add        3  15 local.               _http._tcp.          OT2CEP20200101R01
18:20:29.936  Add        2  15 local.               _http._tcp.          Office Printer
18:20:29.937  Add        2  14 local.               _http._tcp.          opentrons
18:20:29.938  Add        2  14 local.               _http._tcp.          gone printer
18:20:29.939  Rmv        2  14 local.               _http._tcp.          gone printer
"""
LOOKUP = """Lookup opentrons._http._tcp.local.
18:20:40.400  opentrons._http._tcp.local. can be reached at robot1.local.:31950 (interface 27)
 robotModel=OT-2\\ Standard
"""
ADDRESS = """Timestamp     A/R  Flags         IF  Hostname                               Address                                      TTL
18:20:48.961  Add  40000002      27  robot1.local.                          169.254.10.20                                120
"""
AVAHI = """+;en0;IPv4;opentrons;_http._tcp;local
=;en0;IPv4;opentrons;_http._tcp;local;robot1.local;169.254.10.20;31950;"robotModel=OT-2 Standard"
=;en0;IPv6;opentrons;_http._tcp;local;robot1.local;fe80::1;31950;"x"
=;en0;IPv4;HP\\032LaserJet;_http._tcp;local;hp.local;192.168.1.9;80;"y"
"""


def identity(name="opentrons", fw="v2.1.0", **extra):
  return {"name": name, "robot_serial": "OT2X", "robot_model": "OT-2 Standard", "api_version": "8.2.0", "fw_version": fw,
          "pipettes": {"left": {"name": "p300_single_gen2", "model": "p300_single_v2.1"}, "right": None}, **extra}


def test_dns_sd_browse_lists_each_instance_once_and_drops_removed_ones():
  assert discovery.parse_dns_sd_browse(BROWSE) == ["opentrons", "OT2CEP20200101R01", "Office Printer"]


def test_dns_sd_lookup_and_address():
  assert discovery.parse_dns_sd_lookup(LOOKUP) == ("robot1.local", 31950)
  assert discovery.parse_dns_sd_lookup("Lookup x\n...STARTING...\n") is None
  assert discovery.parse_dns_sd_address(ADDRESS) == "169.254.10.20"
  assert discovery.parse_dns_sd_address("nothing") is None


def test_avahi_browse_keeps_resolved_ipv4_services():
  assert discovery.parse_avahi_browse(AVAHI) == [("169.254.10.20", 31950), ("192.168.1.9", 80)]


@pytest.fixture
def macos(monkeypatch):
  """dns-sd answers from canned output; only the robots' hosts answer /health."""
  calls = []

  def run(argv, seconds):
    calls.append((argv, seconds))
    if argv[:2] == ["dns-sd", "-B"]:
      return BROWSE
    if argv[:2] == ["dns-sd", "-L"]:
      host = {"opentrons": "robot1.local", "OT2CEP20200101R01": "ot2.local"}.get(argv[2], "printer.local")
      return f"x {argv[2]}._http._tcp.local. can be reached at {host}.:{80 if host == 'printer.local' else 31950} (interface 1)\n"
    if argv[:3] == ["dns-sd", "-G", "v4"]:
      address = {"robot1.local": "169.254.10.20", "ot2.local": "192.168.1.20", "printer.local": "192.168.1.9"}[argv[3]]
      return f"18:20:48.961  Add  40000002      27  {argv[3]}.   {address}    120\n"
    raise AssertionError(argv)

  monkeypatch.setattr(discovery.shutil, "which", lambda name: "/usr/bin/dns-sd" if name == "dns-sd" else None)
  monkeypatch.setattr(discovery, "_run", run)
  monkeypatch.setattr(discovery, "_probe", lambda host, port: {
    "169.254.10.20": identity(), "192.168.1.20": identity("OT2CEP20200101R01")}.get(host))
  return calls


def test_robots_are_found_and_non_robot_services_dropped(macos):
  robots = discovery.find_robots(2)
  assert [(r["name"], r["host"], r["port"], r["simulator"]) for r in robots] == [
    ("opentrons", "169.254.10.20", 31950, False), ("OT2CEP20200101R01", "192.168.1.20", 31950, False)]
  assert robots[0]["pipettes"]["left"]["name"] == "p300_single_gen2" and robots[0]["robot_serial"] == "OT2X"
  assert macos[0] == (["dns-sd", "-B", "_http._tcp", "local."], 2.0)  # the browse listens for timeout_s, then is killed


def test_the_same_robot_on_two_interfaces_is_listed_once(macos, monkeypatch):
  monkeypatch.setattr(discovery, "_probe", lambda host, port: identity())
  assert len(discovery.find_robots(1)) == 3  # one name on three hosts: kept apart by name and host
  monkeypatch.setattr(discovery, "_browse_dns_sd", lambda t: [("10.0.0.5", 31950), ("10.0.0.5", 31950)])
  assert len(discovery.find_robots(1)) == 1


def test_an_ahc_simulator_is_marked(macos, monkeypatch):
  monkeypatch.setattr(discovery, "_probe", lambda host, port: identity("ahc-sim", fw="Virtual Smoothie") if host.startswith("169") else None)
  [robot] = discovery.find_robots(1)
  assert robot["simulator"] is True
  monkeypatch.setattr(discovery, "_probe", lambda host, port: identity("ahc-sim", fw="v2.1.0") if host.startswith("169") else None)
  assert discovery.find_robots(1)[0]["simulator"] is False  # a real robot is not a simulator by its name alone


def test_avahi_is_used_where_dns_sd_is_missing(monkeypatch):
  monkeypatch.setattr(discovery.shutil, "which", lambda name: "/usr/bin/avahi-browse" if name == "avahi-browse" else None)
  seen = []
  monkeypatch.setattr(discovery, "_run", lambda argv, seconds: seen.append(argv) or AVAHI)
  monkeypatch.setattr(discovery, "_probe", lambda host, port: identity() if port == 31950 else None)
  assert [r["host"] for r in discovery.find_robots(1)] == ["169.254.10.20"]
  assert seen == [["avahi-browse", "-rpt", "_http._tcp"]]


def test_without_dns_sd_or_avahi_discovery_is_unavailable(monkeypatch):
  monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
  with pytest.raises(LabError) as raised:
    discovery.find_robots(1)
  assert raised.value.code == "discovery_unavailable" and "address" in raised.value.hint


@pytest.mark.parametrize("bad", [0, 0.5, 60, "4", True])
def test_a_timeout_out_of_range_is_refused(bad):
  with pytest.raises(LabError) as raised:
    discovery.find_robots(bad)
  assert raised.value.code == "bad_option"


def test_a_command_that_outlives_its_time_is_killed():
  import time

  start = time.monotonic()
  out = discovery._run(["sh", "-c", "echo seen; sleep 30"], 0.5)
  assert time.monotonic() - start < 5 and out == "seen\n"


def test_a_process_is_killed_on_timeout(monkeypatch):
  class Proc:
    killed = False

    def communicate(self, timeout=None):
      if not self.killed:
        raise discovery.subprocess.TimeoutExpired("dns-sd", timeout)
      return "partial", None

    pid = 4242

    def kill(self):
      self.killed = True

  proc = Proc()
  seen = {}
  monkeypatch.setattr(discovery.subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv, kw=kw) or proc)
  assert discovery._run(["dns-sd", "-B"], 1) == "partial" and proc.killed
  assert seen["kw"].get("shell") is None


def test_probe_keeps_only_opentrons_health(monkeypatch):
  def request(host, port, path, timeout, body=None):
    return {"/health": answer}[path]

  monkeypatch.setattr(discovery, "_request", request)
  monkeypatch.setattr(discovery, "read_identity", lambda host, port, timeout: {"pipettes": {"left": None, "right": None}})
  for answer in ({"status": "ok"}, {"name": "printer"}, ["x"], {"name": "", "api_version": "1"}):
    assert discovery._probe("h", 1) is None
  answer = {"name": "r", "robot_serial": "S", "robot_model": "OT-2 Standard", "api_version": "8", "fw_version": "v2"}
  assert discovery._probe("h", 1)["pipettes"] == {"left": None, "right": None}

  def refuses(host, port, path, timeout, body=None):
    raise OSError("refused")

  monkeypatch.setattr(discovery, "_request", refuses)
  assert discovery._probe("h", 1) is None


# -- the MCP tool -------------------------------------------------------------------------------------

async def test_the_tool_is_listed_and_read_only(tmp_path, open_lab):
  async with open_lab(tmp_path) as lab:
    tools = {t.name: t for t in (await lab.client.list_tools()).tools}
  tool = tools["find_robots"]
  assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
  assert "mDNS" in tool.description and "person picks" in tool.description


async def test_the_tool_returns_the_robots_and_works_during_an_emergency_stop(tmp_path, open_lab, monkeypatch):
  monkeypatch.setattr(discovery, "find_robots", lambda timeout_s: [{"name": "opentrons", "host": "169.254.10.20", "port": 31950}])
  async with open_lab(tmp_path) as lab:
    found = await lab.call("find_robots", timeout_s=2)
    assert found["robots"][0]["host"] == "169.254.10.20" and "which one" in found["next"]
    lab.server.lab.estop = {"id": "x", "at": "2026-10-29T00:00:00", "by": "test"}
    assert (await lab.call("find_robots"))["robots"]


async def test_the_tool_refuses_with_the_hint_when_discovery_is_unavailable(tmp_path, open_lab, monkeypatch):
  monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
  async with open_lab(tmp_path) as lab:
    assert await lab.refused("find_robots") == "discovery_unavailable"
