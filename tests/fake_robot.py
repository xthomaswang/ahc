"""A fake OT-2 robot-server for tests: no simulator, no robot, standard library only.

It models what AHC and PyLabRobot use, following the robot-server's own behaviour:
- runs are created idle and never played (PyLabRobot sends setup-intent commands only);
- commands run one at a time in a worker thread, each taking a configurable time (`durations`);
- a stop action on a never-played run lets the command in flight finish, then the run is 'stopped';
- after a stop the hardware forgets mounted tips (as a real robot's reset does);
- a run that is no longer current answers 409 to commands and actions;
- failures can be injected: `fail_commands`, `stop_status`, `poll_status`, `instruments_status`,
  `count_get_fail_at`, `count_get_delay`, `reboot()`; `pipettes` sets what each mount carries;
- another client (the Opentrons App) can start a run of its own: `other_client_run()`.
Every request is logged in `log` with a monotonic timestamp; `dump()` prints it.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeRobot:
  def __init__(self, name: str = "fake-ot2", api_version: str = "8.2.0"):
    self.name = name
    self.api_version = api_version
    self.log: list[tuple[float, str, str, Any]] = []  # (t, method, path, body)
    self.runs: dict[str, dict[str, Any]] = {}
    self.current: str | None = None
    self.durations: dict[str, float] = {}  # commandType -> seconds
    self.home_seconds = 0.0
    self.health_seconds = 0.0  # a slow robot-server: each GET /health takes this long
    self.count_get_delay = 0.0  # delay for GET /runs/{id}/commands?pageLength=1
    self.count_get_delay_once_at: int | None = None  # only the n-th such GET (1-based)
    self.count_get_fail_at: int | None = None  # the n-th such GET answers 500
    self._count_gets = 0
    self.fail_commands: dict[str, str] = {}  # commandType -> error detail (command fails)
    self.stop_status: int | None = None  # force this HTTP status on POST actions
    self.poll_status: int | None = None  # force this HTTP status on GET /runs/{id}/commands/{cid}
    self.instruments_status: int | None = None  # force this HTTP status on GET /instruments (older software)
    self.instruments_listed = True  # False: GET /instruments answers with no pipettes (it is partial on an OT-2)
    self.pipettes: dict[str, dict[str, str] | None] = {  # what each mount carries (None: empty)
      "left": {"name": "p20_single_gen2", "model": "p20_single_v2.2", "id": "L1"},
      "right": {"name": "p300_multi_gen2", "model": "p300_multi_v2.1", "id": "R1"}}
    self.tips_on = {"left": False, "right": False}  # the hardware's idea of tips
    self.position = {"x": 200.0, "y": 150.0, "z": 200.0}
    self.moves: list[dict[str, Any]] = []
    self._lock = threading.Lock()
    self.t0 = time.monotonic()
    self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
    self.port = self.server.server_address[1]
    self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
    self.thread.start()

  def t(self) -> float:
    return round(time.monotonic() - self.t0, 3)

  def close(self):
    self.server.shutdown()

  # -- runs ---------------------------------------------------------------------------------

  def _new_run(self) -> dict[str, Any]:
    with self._lock:
      if self.current is not None:  # run_data_manager.create clears an idle current run
        prev = self.runs[self.current]
        if prev["status"] not in ("stopped",):
          prev["status"] = "stopped"
          self._forget_tips("new run cleared the previous run")
      rid = uuid.uuid4().hex[:8]
      run = {"id": rid, "status": "idle", "commands": [], "queue": queue.Queue(), "stop": False,
             "inflight": None}
      self.runs[rid] = run
      self.current = rid
    threading.Thread(target=self._worker, args=(run,), daemon=True).start()
    return run

  def _forget_tips(self, why: str):
    self.tips_on = {"left": False, "right": False}
    self.log.append((self.t(), "EVENT", "hardware reset: tips forgotten", why))

  def _worker(self, run):
    while True:
      cmd = run["queue"].get()
      if cmd is None:
        return
      if run["stop"]:
        cmd["status"] = "failed"
        cmd["error"] = {"errorType": "RunStoppedError", "detail": "run stopped"}
        continue
      run["inflight"] = cmd
      cmd["status"] = "running"
      self.log.append((self.t(), "EXEC-START", cmd["commandType"], cmd["params"]))
      time.sleep(self.durations.get(cmd["commandType"], 0.0))
      if cmd["commandType"] == "pickUpTip" and self.tips_on[cmd["params"]["pipetteId"].split("-")[1]]:
        cmd["status"] = "failed"
        cmd["error"] = {"errorType": "UnexpectedTipAttachError", "detail": "Cannot perform pick_up_tip with a tip attached"}
      elif cmd["commandType"] in self.fail_commands:
        cmd["status"] = "failed"
        cmd["error"] = {"errorType": "FakeError", "detail": self.fail_commands[cmd["commandType"]]}
      else:
        cmd["result"] = self._result(cmd)
        cmd["status"] = "succeeded"
      self.log.append((self.t(), "EXEC-END", cmd["commandType"], cmd["status"]))
      run["inflight"] = None
      if run["stop"]:
        self._finish_stop(run)

  def _finish_stop(self, run):
    if run["status"] != "stopped":
      # the queue worker joined; now halt + stop_and_recover: hardware reset forgets tips
      run["status"] = "stopped"
      self._forget_tips("run stop -> hardware_api.stop -> reset")
      self.log.append((self.t(), "EVENT", "run stopped", run["id"]))

  def _result(self, cmd) -> dict[str, Any]:
    ct, p = cmd["commandType"], cmd["params"]
    if ct == "loadPipette":
      return {"pipetteId": f"pip-{p['mount']}"}
    if ct == "loadLabware":
      return {"labwareId": p.get("labwareId") or uuid.uuid4().hex}
    if ct == "savePosition":
      return {"positionId": "p", "position": dict(self.position)}
    if ct == "moveToCoordinates":
      self.position = dict(p["coordinates"])
      self.moves.append({"t": self.t(), **p["coordinates"]})
      return {"position": dict(self.position)}
    if ct == "pickUpTip":
      mount = p["pipetteId"].split("-")[1]
      if self.tips_on[mount]:
        raise RuntimeError("unreachable")
      self.tips_on[mount] = True
      self.position = {**self.position, "z": 60.0}
      return {"tipVolume": 300, "tipLength": 51.0, "tipDiameter": 5.0}
    if ct in ("dropTip", "dropTipInPlace"):
      mount = p["pipetteId"].split("-")[1]
      self.tips_on[mount] = False
      self.position = {**self.position, "z": 60.0}
      return {}
    return {}

  # -- HTTP ---------------------------------------------------------------------------------

  def _handler(self):
    bot = self

    class H(BaseHTTPRequestHandler):
      def log_message(self, *a):
        pass

      def _send(self, code: int, body: dict[str, Any]):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

      def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

      def do_GET(self):
        bot.log.append((bot.t(), "GET", self.path, None))
        path = self.path
        if path == "/health":
          time.sleep(bot.health_seconds)
          return self._send(200, {"name": bot.name, "robot_model": "OT-2 Standard", "api_version": bot.api_version,
                                  "fw_version": "v1.0", "robot_serial": "OT2FAKE", "system_version": "1.0"})
        if path == "/pipettes":
          empty = {"name": None, "model": None, "id": None}
          return self._send(200, {m: dict(p) if p else empty for m, p in bot.pipettes.items()})
        if path == "/instruments":
          if bot.instruments_status is not None:
            return self._send(bot.instruments_status, {"errors": [{"detail": "injected"}]})
          return self._send(200, {"data": [
            {"mount": m, "instrumentType": "pipette", "instrumentName": p["name"], "instrumentModel": p["model"],
             "serialNumber": p["id"]} for m, p in bot.pipettes.items() if p and bot.instruments_listed]})
        if re.fullmatch(r"/runs(\?pageLength=\d+)?", path):  # every run, oldest first, and which one is current
          listed = [{"id": r["id"], "status": r["status"], "current": r["id"] == bot.current} for r in bot.runs.values()]
          length = re.search(r"pageLength=(\d+)", path)
          return self._send(200, {"data": listed[-int(length.group(1)):] if length else listed,
                                  "links": {"current": {"href": f"/runs/{bot.current}"} if bot.current else None},
                                  "meta": {"cursor": 0, "totalLength": len(listed)}})
        m = re.fullmatch(r"/runs/(\w+)", path)
        if m:
          run = bot.runs.get(m.group(1))
          if run is None:
            return self._send(404, {"errors": [{"id": "RunNotFound"}]})
          return self._send(200, {"data": {"id": run["id"], "status": run["status"],
                                           "current": run["id"] == bot.current}})
        m = re.fullmatch(r"/runs/(\w+)/commands\?pageLength=1", path)
        if m:
          bot._count_gets += 1
          n = bot._count_gets
          if bot.count_get_fail_at == n:
            return self._send(500, {"errors": [{"detail": "injected failure"}]})
          if bot.count_get_delay and (bot.count_get_delay_once_at in (None, n)):
            bot.log.append((bot.t(), "SLOW", f"count GET #{n} delayed", bot.count_get_delay))
            time.sleep(bot.count_get_delay)
          run = bot.runs[m.group(1)]
          return self._send(200, {"data": run["commands"][-1:], "meta": {"totalLength": len(run["commands"])}})
        m = re.fullmatch(r"/runs/(\w+)/commands/(\w+)", path)
        if m:
          if bot.poll_status is not None:
            return self._send(bot.poll_status, {"errors": [{"detail": "injected"}]})
          run = bot.runs[m.group(1)]
          cmd = next(c for c in run["commands"] if c["id"] == m.group(2))
          return self._send(200, {"data": {"id": cmd["id"], "status": cmd["status"], "result": cmd.get("result"),
                                           "error": cmd.get("error")}})
        return self._send(404, {"errors": [{"detail": f"no route {path}"}]})

      def do_POST(self):
        body = self._body()
        bot.log.append((bot.t(), "POST", self.path, body))
        path = self.path
        if path == "/runs":
          run = bot._new_run()
          return self._send(201, {"data": {"id": run["id"], "status": "idle"}})
        if path == "/robot/home":
          time.sleep(bot.home_seconds)
          bot.log.append((bot.t(), "EVENT", "home finished", None))
          return self._send(200, {"message": "Homing robot."})
        m = re.fullmatch(r"/runs/(\w+)/commands", path)
        if m:
          run = bot.runs.get(m.group(1))
          if run is None:
            return self._send(404, {"errors": [{"id": "RunNotFound"}]})
          if run["id"] != bot.current or run["status"] == "stopped" or run["stop"]:
            return self._send(409, {"errors": [{"id": "RunStopped", "detail": "Run is not the current run"}]})
          cmd = {"id": uuid.uuid4().hex[:10], "commandType": body["data"]["commandType"],
                 "params": body["data"]["params"], "status": "queued"}
          run["commands"].append(cmd)
          run["queue"].put(cmd)
          return self._send(201, {"data": {"id": cmd["id"], "status": "queued"}})
        m = re.fullmatch(r"/runs/(\w+)/actions", path)
        if m:
          run = bot.runs.get(m.group(1))
          if run is None:
            return self._send(404, {"errors": [{"id": "RunNotFound"}]})
          if bot.stop_status is not None:
            return self._send(bot.stop_status, {"errors": [{"id": "RunStopped", "detail": "injected"}]})
          if run["id"] != bot.current:
            return self._send(409, {"errors": [{"id": "RunStopped", "detail": f"Run {run['id']} is not the current run"}]})
          run["stop"] = True
          if run["inflight"] is None:
            bot._finish_stop(run)
          return self._send(201, {"data": {"id": uuid.uuid4().hex[:6], "actionType": "stop"}})
        return self._send(404, {"errors": [{"detail": f"no route {path}"}]})

      def do_DELETE(self):
        bot.log.append((bot.t(), "DELETE", self.path, None))
        return self._send(405, {})

    return H

  # -- helpers for experiments ---------------------------------------------------------------

  def other_client_run(self, status: str = "running") -> str:
    """Another client (the Opentrons App) creates a run and plays it: AHC's idle run is cleared, as the
    robot-server does, and the new run is the current one."""
    run = self._new_run()
    run["status"] = status
    self.log.append((self.t(), "EVENT", "another client's run", run["id"]))
    return run["id"]

  def reboot(self):
    """The robot was power-cycled: no current run, the hardware knows no tips."""
    with self._lock:
      self.current = None
      self._forget_tips("reboot")
      self.log.append((self.t(), "EVENT", "reboot", None))

  def dump(self, since: float = 0.0, skip_polls: bool = True):
    for t, method, path, body in self.log:
      if t < since:
        continue
      if skip_polls and method == "GET" and re.fullmatch(r"/runs/\w+/commands/\w+", path):
        continue
      short = body
      if isinstance(body, dict) and "data" in body and isinstance(body["data"], dict):
        d = body["data"]
        short = d.get("commandType") or d.get("actionType") or d
        if d.get("commandType") == "moveToCoordinates":
          short = f"moveToCoordinates z={d['params']['coordinates']['z']:.2f}"
      print(f"  {t:7.3f}  {method:10s} {path:45s} {short if short is not None else ''}")
