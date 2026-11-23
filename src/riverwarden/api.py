"""REST API (JSON) for the fleet.

    riverwarden -c config.yaml api

    api:
      bind: "127.0.0.1"     # change to reach it from other hosts; then firewall the port
      port: 8532
      url: "http://127.0.0.1:8532"   # where clients (the MCP server) find this API

Every endpoint except ``/health`` needs ``Authorization: Bearer <token>`` (or
``X-API-Key``). The token is generated on first start and stored in
``<data_dir>/api_token`` (override with ``RIVERWARDEN_API_TOKEN_FILE``). The
miners themselves have no authentication, so this token is the only lock on
whatever network the API is bound to.

    GET  /health
    GET  /api/v1                          endpoint list
    GET  /api/v1/status[?cached=1]        every unit (cached=1: the last recorded snapshot)
    GET  /api/v1/units/{unit}
    GET  /api/v1/units/{unit}/chips       per-chip temperature and voltage
    GET  /api/v1/history?hours=6&unit=
    GET  /api/v1/thermostat
    POST /api/v1/units/{unit}/fan         {"percent": 45}
    POST /api/v1/units/{unit}/sleep       {"hold_minutes": 120}   (optional)
    POST /api/v1/units/{unit}/wake        runs in the background, answers 202
    POST /api/v1/units/{unit}/reboot
    POST /api/v1/thermostat               {"action": "enable|disable|hold|resume|set|reset", ...}
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import service
from .fleet import Busy, Config, Fleet, FleetError, Timing, load_config, now_iso
from .iceriver import IceRiverError, Unreachable

MAX_BODY = 4096
ENDPOINTS = [
    "GET /health", "GET /api/v1", "GET /api/v1/status", "GET /api/v1/units/{unit}", "GET /api/v1/units/{unit}/chips",
    "GET /api/v1/history?hours=&unit=", "GET /api/v1/thermostat", "POST /api/v1/units/{unit}/fan",
    "POST /api/v1/units/{unit}/sleep", "POST /api/v1/units/{unit}/wake", "POST /api/v1/units/{unit}/reboot",
    "POST /api/v1/thermostat",
]
_log_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def token_path(cfg: Config) -> Path:
    api = cfg.raw.get("api") or {}
    return Path(os.environ.get("RIVERWARDEN_API_TOKEN_FILE") or api.get("token_file") or (Path(cfg.data_dir) / "api_token"))


def load_token(cfg: Config, create: bool = True) -> str:
    path = token_path(cfg)
    if not path.exists():
        if not create:
            raise FileNotFoundError(f"no API token at {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(32), encoding="ascii")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    token = path.read_text(encoding="ascii").strip()
    if len(token) < 24:
        raise ValueError(f"the API token in {path} is too short; delete the file to generate a new one")
    return token


class App:
    """Routing, separated from HTTP so it can be tested and reused."""

    def __init__(self, config_path: str, timing: Timing | None = None):
        self.config_path = config_path
        self.timing = timing

    def cfg(self) -> Config:
        return load_config(self.config_path)      # re-read on every request: edits apply without a restart

    def log(self, line: str) -> None:
        try:
            path = Path(self.cfg().data_dir) / "api.log"
            with _log_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size > 2_000_000:
                    path.replace(path.with_suffix(".log.1"))
                with open(path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(f"{now_iso()} {line}\n")
        except OSError:
            pass

    def _unit(self, cfg: Config, key: str) -> str:
        try:
            return cfg.find(key).name
        except KeyError as e:
            raise ApiError(404, str(e).strip("'\"")) from None

    def _live(self, cfg: Config, name: str) -> dict:
        st = service.unit_status(cfg, name)
        if not st["ok"]:
            raise ApiError(502, f"{name} is unreachable: {st.get('error')}")
        return st

    def get(self, path: str, query: dict) -> tuple[int, dict]:
        cfg = self.cfg()
        parts = [p for p in path.split("/") if p]
        if parts == ["api", "v1"]:
            return 200, {"name": "riverwarden", "endpoints": ENDPOINTS, "units": [u.name for u in cfg.units]}
        if parts == ["api", "v1", "status"]:
            if query.get("cached", ["0"])[0] in ("1", "true"):
                snap = service.read_latest(cfg)
                if snap is None:
                    raise ApiError(404, "nothing recorded yet; run 'riverwarden tick' first")
                return 200, snap
            return 200, service.full_snapshot(cfg)
        if len(parts) == 4 and parts[:3] == ["api", "v1", "units"]:
            return 200, self._live(cfg, self._unit(cfg, parts[3]))
        if len(parts) == 5 and parts[:3] == ["api", "v1", "units"] and parts[4] == "chips":
            return 200, service.chips(cfg, self._unit(cfg, parts[3]))
        if parts == ["api", "v1", "history"]:
            unit = query.get("unit", [None])[0]
            return 200, service.history(cfg, int(query.get("hours", ["6"])[0]), self._unit(cfg, unit) if unit else None)
        if parts == ["api", "v1", "thermostat"]:
            if not service.has_thermostat(cfg):
                raise ApiError(404, "no thermostat is configured")
            return 200, service.thermostat_view(cfg)
        raise ApiError(404, "no such path; GET /api/v1 lists the endpoints")

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        cfg = self.cfg()
        fleet = Fleet(cfg, self.timing)
        parts = [p for p in path.split("/") if p]
        if parts == ["api", "v1", "thermostat"]:
            if not service.has_thermostat(cfg):
                raise ApiError(404, "no thermostat is configured")
            args = {k: v for k, v in body.items() if k in ("minutes", "low_c", "high_c", "min_running", "max_running")}
            return 200, {"ok": True, "thermostat": service.thermostat_action(cfg, str(body.get("action")), **args)}
        if len(parts) == 5 and parts[:3] == ["api", "v1", "units"]:
            name, op = self._unit(cfg, parts[3]), parts[4]
            hold = body.get("hold_minutes", 120)
            if op == "fan":
                if "percent" not in body:
                    raise ApiError(400, 'missing "percent" (for example {"percent": 45})')
                return 200, {"ok": True, "unit": service.set_fan(cfg, name, int(body["percent"]), fleet)}
            if op == "sleep":
                return 200, {"ok": True, "unit": service.sleep(cfg, name, hold, fleet)}
            if op == "wake":
                def finished(result):
                    self.log(f"wake {name}: " + (f"FAILED {result}" if isinstance(result, Exception)
                                                 else f"hashing, rebooted={result.get('rebooted')}"))
                service.wake_async(cfg, name, hold, fleet, on_done=finished)
                return 202, {"ok": True, "message": f"waking {name} in the background; this takes a few minutes"}
            if op == "reboot":
                service.reboot(cfg, name, fleet)
                return 202, {"ok": True, "message": f"reboot sent to {name}; a manual fan setting is lost on reboot"}
        raise ApiError(404, "no such path; GET /api/v1 lists the endpoints")


def make_handler(app: App, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "riverwarden"

        def log_message(self, *args):   # requests go to api.log instead of stderr
            return

        def _send(self, status: int, obj: dict) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            app.log(f"{self.client_address[0]} {self.command} {self.path.split('?')[0]} {status}")

        def _authorized(self) -> bool:
            got = self.headers.get("X-API-Key") or ""
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                got = auth[7:].strip()
            return bool(got) and hmac.compare_digest(got.encode(), token.encode())

        def _handle(self, method: str) -> None:
            url = urlparse(self.path)
            try:
                if url.path == "/health" and method == "GET":
                    return self._send(200, {"ok": True, "ts": now_iso()})
                if not self._authorized():
                    return self._send(401, {"ok": False, "error": "authentication required (Authorization: Bearer <token>)"})
                if method == "GET":
                    status, obj = app.get(url.path, parse_qs(url.query))
                else:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        raise ApiError(413, "request body too large")
                    raw = self.rfile.read(n) if n else b""
                    try:
                        body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                    except ValueError:
                        raise ApiError(400, "the request body must be JSON") from None
                    if not isinstance(body, dict):
                        raise ApiError(400, "the request body must be a JSON object")
                    status, obj = app.post(url.path, body)
                self._send(status, obj)
            except ApiError as e:
                self._send(e.status, {"ok": False, "error": e.message})
            except Busy as e:
                self._send(409, {"ok": False, "error": str(e)})
            except Unreachable as e:
                self._send(502, {"ok": False, "error": str(e)})
            except (FleetError, IceRiverError) as e:
                self._send(409 if isinstance(e, FleetError) else 502, {"ok": False, "error": str(e)})
            except (ValueError, TypeError, KeyError) as e:
                self._send(400, {"ok": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001 - last resort, keep the server alive
                app.log(f"ERROR {method} {url.path}: {e!r}")
                self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

    return Handler


def make_server(config_path: str, bind: str | None = None, port: int | None = None,
                timing: Timing | None = None) -> ThreadingHTTPServer:
    cfg = load_config(config_path)
    api = cfg.raw.get("api") or {}
    app = App(config_path, timing)
    server = ThreadingHTTPServer((bind or str(api.get("bind", "127.0.0.1")), int(api.get("port", 8532)) if port is None else port),
                                 make_handler(app, load_token(cfg)))
    server.daemon_threads = True
    app.log(f"started on {server.server_address[0]}:{server.server_address[1]}")
    return server


def serve(config_path: str) -> int:
    server = make_server(config_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
