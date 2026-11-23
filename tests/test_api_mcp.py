"""REST API, MCP server, status port and extra checks, against the bundled simulator."""
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from riverwarden import monitor, service
from riverwarden.api import load_token, make_server
from riverwarden.fleet import Timing, load_config
from riverwarden.iceriver import NotAllowed, query_status_port, read_chips
from riverwarden.mcp import Local, Remote, Server
from riverwarden.simulator import FakeMiner

FAST = Timing(poll=0.05, sleep_timeout=2, settle=0.05, share_poll=0.15, wake_timeout=1.5,
              pre_reboot=0.05, boot_wait=0.4, boot_timeout=4)


@pytest.fixture
def stack(tmp_path):
    miners = [FakeMiner() for _ in range(2)]
    hosts = [m.start() for m in miners]
    temp = tmp_path / "room"
    temp.write_text("23.0")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("\n".join([
        "units:", f"  - {{name: rig1, host: '{hosts[0]}'}}", f"  - {{name: rig2, host: '{hosts[1]}'}}",
        f"data_dir: '{(tmp_path / 'data').as_posix()}'",
        "thermostat:", "  low_c: 22", "  high_c: 24", "  max_running: 2",
        f"  sensor: {{type: file, path: '{temp.as_posix()}'}}"]) + "\n", encoding="utf-8")
    server = make_server(str(cfg_path), bind="127.0.0.1", port=0, timing=FAST)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    token = load_token(load_config(cfg_path))
    yield {"base": base, "token": token, "cfg_path": str(cfg_path), "miners": miners, "temp": temp}
    server.shutdown()
    server.server_close()
    for m in miners:
        m.stop()


def call(stack, method, path, body=None, token="use"):
    headers = {"Content-Type": "application/json"}
    if token == "use":
        headers["Authorization"] = f"Bearer {stack['token']}"
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(stack["base"] + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(fn, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


# -- REST API ----------------------------------------------------------------

def test_health_needs_no_token_everything_else_does(stack):
    assert call(stack, "GET", "/health", token=None)[0] == 200
    assert call(stack, "GET", "/api/v1/status", token=None)[0] == 401
    assert call(stack, "GET", "/api/v1/status", token="wrong-token")[0] == 401
    status, doc = call(stack, "GET", "/api/v1")
    assert status == 200 and doc["units"] == ["rig1", "rig2"] and len(doc["endpoints"]) == 12


def test_read_endpoints(stack):
    status, snap = call(stack, "GET", "/api/v1/status")
    assert status == 200 and [u["mode"] for u in snap["units"]] == ["normal", "normal"] and snap["room"]["temp_c"] == 23.0
    assert call(stack, "GET", "/api/v1/units/rig2")[1]["name"] == "rig2"
    assert call(stack, "GET", "/api/v1/units/nope")[0] == 404
    assert call(stack, "GET", "/api/v1/status?cached=1")[0] == 404          # nothing recorded yet
    service.collect(load_config(stack["cfg_path"]))
    assert call(stack, "GET", "/api/v1/status?cached=1")[0] == 200
    status, h = call(stack, "GET", "/api/v1/history?hours=1&unit=rig1")
    assert status == 200 and h["count"] == 1 and list(h["rows"][0]["units"]) == ["rig1"]
    status, th = call(stack, "GET", "/api/v1/thermostat")
    assert status == 200 and th["params"]["high_c"] == 24 and "no change" in th["would_do_now"]["reason"]
    assert call(stack, "GET", "/api/v1/nope")[0] == 404


def test_fan_validation_and_write(stack):
    assert call(stack, "POST", "/api/v1/units/rig1/fan", {})[0] == 400
    assert call(stack, "POST", "/api/v1/units/rig1/fan", {"percent": 5})[0] == 400      # below the fan floor
    status, doc = call(stack, "POST", "/api/v1/units/rig1/fan", {"percent": 45})
    assert status == 200 and doc["unit"]["fan_percent"] == 45
    req = urllib.request.Request(stack["base"] + "/api/v1/units/rig1/fan", data=b"not json", method="POST",
                                 headers={"Authorization": f"Bearer {stack['token']}"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 400


def test_sleep_holds_thermostat_then_wake_in_background(stack):
    status, doc = call(stack, "POST", "/api/v1/units/rig1/sleep", {})
    assert status == 200 and doc["unit"]["mode"] == "sleep"
    th = call(stack, "GET", "/api/v1/thermostat")[1]
    assert th["hold_until"] and "on hold" in th["would_do_now"]["reason"]
    assert call(stack, "POST", "/api/v1/units/rig2/wake", {})[0] == 409                  # already running
    status, doc = call(stack, "POST", "/api/v1/units/rig1/wake", {"hold_minutes": 0})
    assert status == 202
    assert wait_for(lambda: call(stack, "GET", "/api/v1/units/rig1")[1].get("accepted", 0) > 0
                    and call(stack, "GET", "/api/v1/units/rig1")[1].get("mode") == "normal")
    # the background wake may still hold the write lock for a moment: a second write answers 409 until it is done
    assert wait_for(lambda: call(stack, "POST", "/api/v1/units/rig1/reboot", {})[0] == 202)
    assert call(stack, "POST", "/api/v1/units/rig1/explode", {})[0] == 404


def test_wake_refused_at_max_running(stack):
    call(stack, "POST", "/api/v1/units/rig1/sleep", {"hold_minutes": 0})
    assert call(stack, "POST", "/api/v1/thermostat", {"action": "set", "max_running": 1})[0] == 200
    status, doc = call(stack, "POST", "/api/v1/units/rig1/wake", {})
    assert status == 409 and "max_running" in doc["error"]


def test_thermostat_actions(stack):
    status, doc = call(stack, "POST", "/api/v1/thermostat", {"action": "set", "high_c": 26})
    assert status == 200 and doc["thermostat"]["params"]["high_c"] == 26
    assert call(stack, "POST", "/api/v1/thermostat", {"action": "set", "max_running": 9})[0] == 400
    assert call(stack, "POST", "/api/v1/thermostat", {"action": "boom"})[0] == 400
    assert call(stack, "POST", "/api/v1/thermostat", {"action": "reset"})[1]["thermostat"]["params"]["high_c"] == 24
    assert call(stack, "POST", "/api/v1/thermostat", {"action": "hold", "minutes": 5})[1]["thermostat"]["hold_until"]
    assert call(stack, "POST", "/api/v1/thermostat", {"action": "resume"})[1]["thermostat"]["hold_until"] is None


# -- MCP ---------------------------------------------------------------------

def tool(server, name, **args):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return resp["result"]["isError"], resp["result"]["content"][0]["text"]


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_mcp_tools(stack, mode):
    backend = Local(stack["cfg_path"]) if mode == "local" else Remote(stack["base"], stack["token"])
    if mode == "local":
        backend.sleep = lambda unit, hold: service.sleep(load_config(stack["cfg_path"]), unit, hold,
                                                         fleet=__import__("riverwarden.fleet", fromlist=["Fleet"]).Fleet(load_config(stack["cfg_path"]), FAST))
    server = Server(backend)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in listed] == ["fleet_status", "fleet_history", "unit_chips", "unit_set_fan", "unit_sleep", "unit_wake", "thermostat"]
    err, text = tool(server, "fleet_status")
    assert not err and "rig1" in text and "room 23.0 C" in text and "thermostat enabled: 22..24 C" in text
    err, text = tool(server, "unit_sleep", unit="rig2")
    assert not err and text.startswith("[not done]")
    assert call(stack, "GET", "/api/v1/units/rig2")[1]["mode"] == "normal"            # nothing happened
    err, text = tool(server, "unit_sleep", unit="rig2", confirm=True)
    assert not err and "rig2: sleep" in text
    err, text = tool(server, "thermostat")
    assert not err and "on hold until" in text
    err, text = tool(server, "thermostat", action="resume")
    assert not err and "on hold" not in text
    err, text = tool(server, "unit_set_fan", unit="rig1", percent=3, confirm=True)
    assert err and "20..100" in text
    err, text = tool(server, "unit_wake", unit="rig1", confirm=True)
    assert err and "already running" in text
    err, text = tool(server, "fleet_history", hours=1)
    assert not err and "nothing recorded" in text
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "nope"}})["error"]["code"] == -32602
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


# -- status port -------------------------------------------------------------

def test_status_port_read_only():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = {}

    def serve():
        conn, _ = srv.accept()
        got["line"] = conn.recv(200)
        conn.sendall(json.dumps({"id": "getchipinfo", "ret": {"code": 0, "chips": [
            {"no": 1, "temp": 55, "voltage": 0.35, "pll": 625}, {"no": 2, "temp": 0, "voltage": 0.0, "pll": 0}]}}).encode())
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    chips = read_chips("127.0.0.1:80", port=srv.getsockname()[1])
    assert chips == [{"no": 1, "temp": 55, "voltage": 0.35, "pll": 625}] and got["line"] == b'{"id": "getchipinfo"}\r\n'
    srv.close()
    with pytest.raises(NotAllowed):
        query_status_port("127.0.0.1", "reboot")


# -- extra checks ------------------------------------------------------------

def test_checks_run_from_config_dir_and_need_two_failures(stack, tmp_path):
    cfg = load_config(stack["cfg_path"])
    (tmp_path / "flag.py").write_text("import sys, pathlib; sys.exit(0 if pathlib.Path('ok').exists() else 3)\n")
    cfg.raw["monitor"] = {"checks": [{"name": "flag", "command": [sys.executable, "flag.py"]}]}
    first = service.collect(cfg)
    assert first["checks"]["flag"]["ok"] is False and any("check failed: flag" in a for a in first["alerts"])
    ok_snap = {**first, "checks": {"flag": {"ok": True, "detail": ""}}}
    assert monitor.evaluate(first, ok_snap, None)[0] is True                # first failure: not yet
    assert monitor.evaluate(first, first, None) == (False, "check failed: flag")
    assert monitor.evaluate(first, {"units": first["units"]}, None)[0] is True   # check is new: no history
    (tmp_path / "ok").write_text("")
    assert service.collect(cfg)["checks"]["flag"]["ok"] is True
