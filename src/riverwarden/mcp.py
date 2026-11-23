"""MCP server (stdio) so an AI assistant can watch and operate the fleet.

    riverwarden -c config.yaml mcp

Two modes:

* **remote** (when ``api.url`` is set in the configuration): every tool calls the
  REST API. Use this when a scheduler and the API already run on a server, so
  there is exactly one process that writes to the miners.
* **local** (no ``api.url``, or ``RIVERWARDEN_MCP_LOCAL=1``): the tools talk to the
  miners directly from this process.

Tools that change something only act when called with ``confirm: true``;
without it they answer with what they would do.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, service
from .api import load_token
from .cli import format_snapshot
from .fleet import FleetError, load_config
from .iceriver import IceRiverError


# -- backends ----------------------------------------------------------------

class Local:
    def __init__(self, config_path: str):
        self.config_path = config_path

    def cfg(self):
        return load_config(self.config_path)

    def snapshot(self) -> dict:
        return service.full_snapshot(self.cfg())

    def thermostat(self) -> dict | None:
        cfg = self.cfg()
        return service.thermostat_view(cfg) if service.has_thermostat(cfg) else None

    def history(self, hours: int, unit: str | None) -> dict:
        return service.history(self.cfg(), hours, unit)

    def chips(self, unit: str) -> dict:
        return service.chips(self.cfg(), unit)

    def fan(self, unit: str, percent: int) -> dict:
        return service.set_fan(self.cfg(), unit, percent)

    def sleep(self, unit: str, hold) -> dict:
        return service.sleep(self.cfg(), unit, hold)

    def wake(self, unit: str, hold) -> str:
        service.wake_async(self.cfg(), unit, hold)
        return f"waking {unit} in the background; this takes a few minutes"

    def thermostat_action(self, action: str, **kw) -> dict:
        return service.thermostat_action(self.cfg(), action, **kw)


class Remote:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 120):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except ValueError:
                return e.code, {"error": str(e)}
        except (urllib.error.URLError, OSError) as e:
            raise FleetError(f"the API at {self.url} is unreachable: {getattr(e, 'reason', e)}") from None

    def _ok(self, result):
        status, doc = result
        if status >= 400:
            raise FleetError(doc.get("error") or f"HTTP {status}")
        return doc

    def snapshot(self) -> dict:
        return self._ok(self._call("GET", "/api/v1/status"))

    def thermostat(self) -> dict | None:
        status, doc = self._call("GET", "/api/v1/thermostat")
        return None if status == 404 else self._ok((status, doc))

    def history(self, hours: int, unit: str | None) -> dict:
        q = urllib.parse.urlencode({k: v for k, v in (("hours", hours), ("unit", unit)) if v})
        return self._ok(self._call("GET", f"/api/v1/history?{q}"))

    def chips(self, unit: str) -> dict:
        return self._ok(self._call("GET", f"/api/v1/units/{urllib.parse.quote(unit)}/chips"))

    def fan(self, unit: str, percent: int) -> dict:
        return self._ok(self._call("POST", f"/api/v1/units/{urllib.parse.quote(unit)}/fan", {"percent": percent}))["unit"]

    def sleep(self, unit: str, hold) -> dict:
        return self._ok(self._call("POST", f"/api/v1/units/{urllib.parse.quote(unit)}/sleep", {"hold_minutes": hold}))["unit"]

    def wake(self, unit: str, hold) -> str:
        return self._ok(self._call("POST", f"/api/v1/units/{urllib.parse.quote(unit)}/wake", {"hold_minutes": hold}))["message"]

    def thermostat_action(self, action: str, **kw) -> dict:
        return self._ok(self._call("POST", "/api/v1/thermostat", {"action": action, **kw}))["thermostat"]


# -- text --------------------------------------------------------------------

def thermostat_text(view: dict | None) -> str:
    if view is None:
        return "no thermostat is configured"
    p = view["params"]
    lines = [f"thermostat {'enabled' if p['enabled'] else 'disabled'}: {p['low_c']:g}..{p['high_c']:g} C, "
             f"{p['min_running']}..{p['max_running']} running, sleep order {' > '.join(p['sleep_order'])}"]
    if view.get("hold_until"):
        lines.append(f"on hold until {view['hold_until']}")
    if view.get("last_change_at"):
        lines.append(f"last change at {view['last_change_at']}")
    lines.append(f"right now it would: {view['would_do_now']['reason']}")
    return "\n".join(lines)


def history_text(h: dict) -> str:
    rows = h["rows"]
    if not rows:
        return f"nothing recorded in the last {h['hours']} h (a scheduler has to run 'riverwarden tick')"
    step = max(1, len(rows) // 24)
    lines = [f"last {h['hours']} h, {len(rows)} snapshots (every {step} shown)",
             "time  | room C | running | avg GH/s | board C per unit (z = asleep)"]
    for r in rows[::step]:
        temps = " ".join(f"{n}:--" if u is None else f"{n}:{'z' if u['mode'] == 'sleep' else ''}{(u.get('board_temp_max_c') or 0):.0f}"
                         for n, u in r["units"].items())
        room = "--" if r["room_c"] is None else r["room_c"]
        lines.append(f"{r['ts'][11:16]} | {room} | {r['running']} | {r['avg_ghs']:.0f} | {temps}")
    if h.get("changes"):
        lines.append("changes:")
        lines += [f"  {c['ts'][11:16]} {c['action']} {c.get('unit')}: {c.get('reason')}" for c in h["changes"]]
    return "\n".join(lines)


def unit_text(u: dict) -> str:
    rpm = "/".join(str(x) for x in u.get("fans_rpm") or [])
    return (f"{u['name']}: {u['mode']}, fan {u.get('fan_percent')} % ({rpm} rpm), board {u.get('board_temp_max_c')} C, "
            f"{u.get('avg_ghs')} GH/s average, accepted {u.get('accepted')}")


# -- tools -------------------------------------------------------------------

UNIT = {"type": "string", "description": "Unit name from the configuration, its address, or the last octet of the address"}
CONFIRM = {"type": "boolean", "description": "true to act; omit to see what would happen"}
HOLD = {"type": "integer", "description": "Minutes to pause the thermostat afterwards so it does not undo the change (default 120, 0 = no pause)"}

TOOLS = [
    {"name": "fleet_status", "description": "Current state of every miner (mode, hashrate, board temperature, fan, accepted shares), the room temperature and the thermostat. Read-only.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fleet_history", "description": "Trend over the last N hours: room temperature, units running, hashrate, board temperatures and thermostat changes. Read-only.",
     "inputSchema": {"type": "object", "properties": {"hours": {"type": "integer", "description": "1..72, default 6"}, "unit": UNIT}}},
    {"name": "unit_chips", "description": "Per-chip temperature and voltage of one miner. Read-only.",
     "inputSchema": {"type": "object", "properties": {"unit": UNIT}, "required": ["unit"]}},
    {"name": "unit_set_fan", "description": "Set a manual fan percentage on one miner. It is lost when the miner reboots.",
     "inputSchema": {"type": "object", "properties": {"unit": UNIT, "percent": {"type": "integer"}, "confirm": CONFIRM}, "required": ["unit", "percent"]}},
    {"name": "unit_sleep", "description": "Put one miner to sleep: hashing stops and it produces almost no heat.",
     "inputSchema": {"type": "object", "properties": {"unit": UNIT, "confirm": CONFIRM, "hold_minutes": HOLD}, "required": ["unit"]}},
    {"name": "unit_wake", "description": "Wake a sleeping miner. Runs in the background for a few minutes (it reboots the miner and waits for accepted shares). Refused when the thermostat's max_running is reached.",
     "inputSchema": {"type": "object", "properties": {"unit": UNIT, "confirm": CONFIRM, "hold_minutes": HOLD}, "required": ["unit"]}},
    {"name": "thermostat", "description": "Room-temperature thermostat. action: status (default), enable, disable, hold (minutes), resume, set (low_c, high_c, min_running, max_running), reset (drop run-time overrides).",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["status", "enable", "disable", "hold", "resume", "set", "reset"]},
         "minutes": {"type": "integer"}, "low_c": {"type": "number"}, "high_c": {"type": "number"},
         "min_running": {"type": "integer"}, "max_running": {"type": "integer"}}}},
]


class Server:
    def __init__(self, backend):
        self.b = backend

    def call(self, name: str, a: dict) -> str:
        b = self.b
        if name == "fleet_status":
            return format_snapshot(b.snapshot()) + "\n" + thermostat_text(b.thermostat())
        if name == "fleet_history":
            return history_text(b.history(int(a.get("hours") or 6), a.get("unit")))
        if name == "unit_chips":
            d = b.chips(str(a["unit"]))
            temps = [c.get("temp") for c in d["chips"] if c.get("temp") is not None]
            if not temps:
                return f"{d['unit']}: no chip data"
            return (f"{d['unit']}: {len(temps)} chips, {min(temps)}..{max(temps)} C (mean {sum(temps) / len(temps):.1f})\n"
                    + " ".join(f"{c.get('no')}:{c.get('temp')}C/{c.get('voltage')}V" for c in d["chips"]))
        if name == "thermostat":
            action = a.get("action") or "status"
            if action == "status":
                return thermostat_text(b.thermostat())
            kw = {k: a[k] for k in ("minutes", "low_c", "high_c", "min_running", "max_running") if a.get(k) is not None}
            return thermostat_text(b.thermostat_action(action, **kw))
        if name in ("unit_set_fan", "unit_sleep", "unit_wake"):
            unit = str(a["unit"])
            what = {"unit_set_fan": f"set the fan of {unit} to {a.get('percent')} %", "unit_sleep": f"put {unit} to sleep",
                    "unit_wake": f"wake {unit}"}[name]
            if a.get("confirm") is not True:
                return f"[not done] would {what}. Call again with confirm=true to do it."
            hold = a.get("hold_minutes", 120)
            if name == "unit_set_fan":
                return unit_text(b.fan(unit, int(a["percent"])))
            if name == "unit_sleep":
                return unit_text(b.sleep(unit, hold))
            return b.wake(unit, hold)
        raise KeyError(name)

    def handle(self, req: dict):
        method, rid = req.get("method", ""), req.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": (req.get("params") or {}).get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}}, "serverInfo": {"name": "riverwarden", "version": __version__}}}
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            if name not in {t["name"] for t in TOOLS}:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool: {name}"}}
            try:
                text, is_error = self.call(name, params.get("arguments") or {}), False
            except (FleetError, IceRiverError, KeyError, ValueError, TypeError, OSError) as e:
                text, is_error = f"error: {e}", True
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}
        if rid is not None:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return None


def make_backend(config_path: str):
    cfg = load_config(config_path)
    url = (cfg.raw.get("api") or {}).get("url")
    if url and os.environ.get("RIVERWARDEN_MCP_LOCAL") != "1":
        return Remote(str(url), load_token(cfg, create=False))
    return Local(config_path)


def serve(config_path: str) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    out = sys.stdout
    sys.stdout = sys.stderr            # nothing but protocol messages may reach the real stdout
    backend = make_backend(config_path)
    print(f"[riverwarden-mcp] started ({type(backend).__name__.lower()} mode)", file=sys.stderr, flush=True)
    server = Server(backend)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        resp = server.handle(req)
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()
    return 0
