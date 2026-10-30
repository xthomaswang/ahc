"""Finding OT-2 robots on this computer's networks: an mDNS browse, then GET /health on each candidate.

Read-only: nothing is sent to a robot but GET /health (and, for a robot, the GETs that name its pipettes).
OT-2s advertise `_http._tcp` with the robot's name as the instance name and port 31950; printers and other
web servers advertise it too, so a service counts only when its /health answers like an Opentrons robot.
Blocking (async callers use a thread). Every subprocess is bounded, started without a shell, and killed
when its time is up.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import signal
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ahc.core.errors import LabError
from ahc.devices.opentrons.brand import MOUNTS, _request, read_identity

SERVICE = "_http._tcp"
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 1.0, 15.0
RESOLVE_S = 1.5  # dns-sd -L / -G answer from the cache or the first reply, then run on until killed
HEALTH_S = 2.0
MAX_SERVICES = 32  # a network with more web servers than this is searched in its first 32
ROBOT_FIELDS = ("robot_model", "api_version", "fw_version")
SIMULATOR_FIRMWARE = "Virtual Smoothie"  # what ahc-sim ot2 reports (see sim/robot_server.py)

_BROWSE_LINE = re.compile(r"^\S+\s+(Add|Rmv)\s+\S+\s+\S+\s+\S+\s+" + re.escape(SERVICE) + r"\.?\s+(.+?)\s*$")
_LOOKUP_LINE = re.compile(r"can be reached at (\S+?):(\d+)\s+\(interface")
_ADDRESS_LINE = re.compile(r"^\S+\s+Add\s+\S+\s+\S+\s+\S+\s+(\d{1,3}(?:\.\d{1,3}){3})\s")


def _run(argv: list[str], seconds: float) -> str:
  """Run a command for `seconds`, then kill it, and return what it printed (dns-sd never exits on its own)."""
  proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                          errors="replace", start_new_session=True)
  try:
    out, _ = proc.communicate(timeout=seconds)
  except subprocess.TimeoutExpired:
    try:
      os.killpg(proc.pid, signal.SIGKILL)  # the whole group: a child holding the pipe would keep communicate waiting
    except OSError:
      proc.kill()
    out, _ = proc.communicate()
  return out or ""


def _probe(host: str, port: int) -> dict[str, Any] | None:
  """The robot at host:port from its /health (and its pipettes), or None when it is not an Opentrons robot."""
  try:
    health = _request(host, port, "/health", HEALTH_S)
  except (OSError, ValueError):  # nothing answers, not HTTP, not JSON
    return None
  if not isinstance(health, dict) or not isinstance(health.get("name"), str) or not health["name"]:
    return None
  if not any(health.get(k) for k in ROBOT_FIELDS):
    return None
  out = {k: health.get(k) for k in ("name", "robot_serial", *ROBOT_FIELDS)}
  try:
    pipettes = read_identity(host, port, HEALTH_S)["pipettes"]
    out["pipettes"] = {m: ({"name": p.get("name"), "model": p.get("model")} if p else None)
                       for m, p in ((m, pipettes.get(m)) for m in MOUNTS)}
  except (OSError, ValueError, KeyError, AttributeError):  # the pipettes are a nicety: configure_devices reads them
    out["pipettes"] = None
  return out


# -- macOS: mDNSResponder through dns-sd -----------------------------------------------------------

def parse_dns_sd_browse(text: str) -> list[str]:
  """Instance names still advertised in `dns-sd -B` output (added, not removed), in first-seen order."""
  names: dict[str, None] = {}
  for line in text.splitlines():
    match = _BROWSE_LINE.match(line)
    if not match:
      continue
    if match.group(1) == "Add":
      names.setdefault(match.group(2), None)
    else:
      names.pop(match.group(2), None)
  return list(names)


def parse_dns_sd_lookup(text: str) -> tuple[str, int] | None:
  """(host, port) from `dns-sd -L` output."""
  match = _LOOKUP_LINE.search(text)
  return (match.group(1).rstrip("."), int(match.group(2))) if match else None


def parse_dns_sd_address(text: str) -> str | None:
  """The first IPv4 address in `dns-sd -G v4` output."""
  for line in text.splitlines():
    match = _ADDRESS_LINE.match(line)
    if match:
      return match.group(1)
  return None


def _resolve_dns_sd(instance: str) -> tuple[str, int] | None:
  found = parse_dns_sd_lookup(_run(["dns-sd", "-L", instance, SERVICE, "local."], RESOLVE_S))
  if found is None:
    return None
  host, port = found
  try:
    ipaddress.IPv4Address(host)
    return host, port
  except ValueError:
    pass
  address = parse_dns_sd_address(_run(["dns-sd", "-G", "v4", host], RESOLVE_S))
  return (address, port) if address else None


def _browse_dns_sd(timeout_s: float) -> list[tuple[str, int]]:
  instances = parse_dns_sd_browse(_run(["dns-sd", "-B", SERVICE, "local."], timeout_s))[:MAX_SERVICES]
  with ThreadPoolExecutor(max_workers=8) as pool:
    return [r for r in pool.map(_resolve_dns_sd, instances) if r]


# -- Linux: avahi-browse ----------------------------------------------------------------------------

def _unescape_avahi(text: str) -> str:
  return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), text)


def parse_avahi_browse(text: str) -> list[tuple[str, int]]:
  """(IPv4 address, port) of each resolved service in `avahi-browse -rpt` output ('=' lines)."""
  out: dict[tuple[str, int], None] = {}
  for line in text.splitlines():
    fields = line.split(";")
    if len(fields) < 9 or fields[0] != "=" or fields[2] != "IPv4":
      continue
    try:
      ipaddress.IPv4Address(fields[7])
      out.setdefault((fields[7], int(fields[8])), None)
    except ValueError:
      continue
  return list(out)


def _browse_avahi(timeout_s: float) -> list[tuple[str, int]]:
  # -t makes avahi-browse exit once the cache is dumped; the bound is for a daemon that never answers
  return parse_avahi_browse(_run(["avahi-browse", "-rpt", SERVICE], timeout_s + RESOLVE_S))[:MAX_SERVICES]


# -- the search -------------------------------------------------------------------------------------

def find_robots(timeout_s: float = 4.0) -> list[dict[str, Any]]:
  """OT-2 robots on this computer's networks: name, host (IPv4), port, serial, model, software, pipettes, and
  `simulator` (an AHC simulator advertising itself). Takes about timeout_s plus a few seconds."""
  if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not MIN_TIMEOUT_S <= timeout_s <= MAX_TIMEOUT_S:
    raise LabError("bad_option", f"timeout_s {timeout_s!r} is not between {MIN_TIMEOUT_S:g} and {MAX_TIMEOUT_S:g} seconds.",
                   "Leave it out (4 seconds), or give a number of seconds in that range.")
  if shutil.which("dns-sd"):
    candidates = _browse_dns_sd(float(timeout_s))
  elif shutil.which("avahi-browse"):
    candidates = _browse_avahi(float(timeout_s))
  else:
    raise LabError("discovery_unavailable", "neither dns-sd (macOS) nor avahi-browse (Linux) is on this computer.",
                   "Ask the person for the robot's address.")
  unique = list(dict.fromkeys(candidates))
  with ThreadPoolExecutor(max_workers=8) as pool:
    probed = list(pool.map(lambda c: _probe(*c), unique))
  robots: dict[tuple[str, str], dict[str, Any]] = {}
  for (host, port), identity in zip(unique, probed):
    if identity is None:
      continue
    simulator = identity["name"].startswith("ahc-") and identity.get("fw_version") == SIMULATOR_FIRMWARE
    robots.setdefault((identity["name"], host), {
      "name": identity["name"], "host": host, "port": port, "robot_serial": identity.get("robot_serial"),
      "robot_model": identity.get("robot_model"), "api_version": identity.get("api_version"),
      "fw_version": identity.get("fw_version"), "pipettes": identity.get("pipettes"), "simulator": simulator})
  return list(robots.values())

