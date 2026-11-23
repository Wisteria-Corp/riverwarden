"""Thermostat, sensors and heartbeat, against the bundled simulator."""
import base64
import hashlib
import hmac
import json
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from riverwarden import monitor, sensors, service
from riverwarden.cli import main
from riverwarden.fleet import Config, Fleet, Timing, Unit
from riverwarden.iceriver import IceRiverClient
from riverwarden.simulator import FakeMiner
from riverwarden.thermostat import Params, Thermostat, decide

FAST = Timing(poll=0.05, sleep_timeout=2, settle=0.05, share_poll=0.15, wake_timeout=1.5,
              pre_reboot=0.05, boot_wait=0.4, boot_timeout=4)
P = Params(low_c=22, high_c=24, sleep_order=["a", "b", "c"], min_running=1, max_running=3)


# -- pure decision -----------------------------------------------------------

def test_decide_sleeps_first_in_order_when_hot():
    d = decide(P, 24.5, ["a", "b", "c"], [])
    assert (d.action, d.unit) == ("sleep", "a")
    assert decide(P, 24.5, ["b", "c"], ["a"]).unit == "b"


def test_decide_wakes_in_reverse_order_when_cold():
    d = decide(P, 21.0, ["c"], ["a", "b"])
    assert (d.action, d.unit) == ("wake", "b")


def test_decide_respects_bounds_and_band():
    assert decide(P, 30, ["c"], ["a", "b"]).action == "none"          # min_running
    assert decide(P, 10, ["a", "b", "c"], []).action == "none"        # nothing to wake
    assert decide(P, 23, ["a", "b"], ["c"]).action == "none"          # inside the band
    capped = Params(low_c=22, high_c=24, sleep_order=["a", "b", "c"], max_running=1)
    assert decide(capped, 10, ["a"], ["b", "c"]).action == "none"     # max_running


def test_decide_never_touches_units_outside_the_order():
    p = Params(low_c=22, high_c=24, sleep_order=["a"], max_running=3)
    assert decide(p, 30, ["b", "c"], ["a"]).action == "none"
    assert decide(p, 10, ["a"], ["b"]).action == "none"


# -- sensors -----------------------------------------------------------------

def test_file_sensor_plain_and_json(tmp_path):
    f = tmp_path / "t"
    f.write_text("23.4\n")
    r = sensors.read({"type": "file", "path": str(f)})
    assert r.temp_c == 23.4 and r.age_s < 5
    f.write_text(json.dumps({"body": {"readings": [{"t": 72.5}]}}))
    assert sensors.read({"type": "file", "path": str(f), "json_path": "body.readings.0.t", "unit": "F"}).temp_c == 22.5


def test_command_sensor_with_scale():
    cmd = [sys.executable, "-c", "print(23500)"]
    assert sensors.read({"type": "command", "command": cmd, "scale": 0.001}).temp_c == 23.5


def test_sensor_errors_are_reported_not_raised_raw(tmp_path):
    for spec in ({"type": "nope"}, {"type": "file", "path": str(tmp_path / "missing")},
                 {"type": "command", "command": [sys.executable, "-c", "print('warm')"]},
                 {"type": "command", "command": [sys.executable, "-c", "print(500)"]},      # implausible
                 {"type": "switchbot", "device_id": "X", "token_env": "RW_TEST_UNSET", "secret_env": "RW_TEST_UNSET"}):
        with pytest.raises(sensors.SensorError):
            sensors.read(spec)


def test_http_sensor_sends_header_from_environment(monkeypatch):
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):  # noqa: N802
            seen["auth"] = self.headers.get("Authorization")
            body = json.dumps({"state": "21.9"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("RW_TEST_AUTH", "Bearer abc")
        r = sensors.read({"type": "http", "url": f"http://127.0.0.1:{srv.server_address[1]}/x",
                          "json_path": "state", "header_env": {"Authorization": "RW_TEST_AUTH"}})
        assert r.temp_c == 21.9 and seen["auth"] == "Bearer abc"
    finally:
        srv.shutdown()
        srv.server_close()


def test_switchbot_signature():
    h = sensors.switchbot_headers("tok", "sec", t="1700000000000", nonce="n")
    want = base64.b64encode(hmac.new(b"sec", b"tok1700000000000n", hashlib.sha256).digest()).decode()
    assert h["sign"] == want and h["Authorization"] == "tok"


# -- thermostat against simulated miners -------------------------------------

@pytest.fixture
def rig(tmp_path):
    miners = [FakeMiner() for _ in range(3)]
    units = [Unit(name=n, host=m.start()) for n, m in zip("abc", miners)]
    temp = tmp_path / "room"
    temp.write_text("23.0")
    cfg = Config(units=units, data_dir=tmp_path / "data", raw={"thermostat": {
        "low_c": 22, "high_c": 24, "min_running": 1, "max_running": 3, "sleep_order": ["a", "b", "c"],
        "min_minutes_between_changes": 30, "sensor": {"type": "file", "path": str(temp)}}})
    th = Thermostat(cfg, Fleet(cfg, FAST))
    yield cfg, th, temp, miners
    for m in miners:
        m.stop()


def modes(cfg):
    return [IceRiverClient(u.host).read_status().mode for u in cfg.units]


def backdate(th, minutes=31):
    state = th.load_state()
    state["last_change_at"] = (datetime.now().astimezone() - timedelta(minutes=minutes)).isoformat()
    th.save_state(state)


def test_thermostat_full_cycle(rig):
    cfg, th, temp, _ = rig
    assert th.run(apply=True)["action"] == "none"                     # 23.0: inside the band

    temp.write_text("24.6")
    dry = th.run(apply=False)
    assert dry["action"] == "sleep" and "dry run" in dry["reason"] and modes(cfg) == ["normal"] * 3

    rec = th.run(apply=True)
    assert (rec["action"], rec["unit"]) == ("sleep", "a") and modes(cfg) == ["sleep", "normal", "normal"]

    again = th.run(apply=True)                                        # still hot, but too soon
    assert again["action"] == "none" and "waiting" in again["reason"]

    backdate(th)
    assert th.run(apply=True)["unit"] == "b" and modes(cfg) == ["sleep", "sleep", "normal"]

    backdate(th)
    assert th.run(apply=True)["action"] == "none"                     # min_running holds the last one

    temp.write_text("21.2")
    backdate(th)
    rec = th.run(apply=True)
    assert (rec["action"], rec["unit"], rec["rebooted"]) == ("wake", "b", True)
    assert modes(cfg) == ["sleep", "normal", "normal"]
    log = [json.loads(line) for line in th.log_path.read_text().splitlines()]
    assert [r["action"] for r in log].count("sleep") == 2 and log[-1]["action"] == "wake"


def test_thermostat_does_nothing_without_a_fresh_temperature(rig):
    cfg, th, temp, _ = rig
    temp.write_text("30")
    old = time.time() - 3600
    import os
    os.utime(temp, (old, old))
    rec = th.run(apply=True)
    assert rec["action"] == "none" and "old" in rec["reason"] and modes(cfg) == ["normal"] * 3
    temp.unlink()
    rec = th.run(apply=True)
    assert rec["action"] == "none" and "no room temperature" in rec["reason"]


def test_hold_disable_and_overrides(rig):
    cfg, th, temp, _ = rig
    temp.write_text("30")
    th.hold(60)
    assert "on hold" in th.run(apply=True)["reason"]
    th.resume()
    th.set_overrides(enabled=False)
    assert "disabled" in th.run(apply=True)["reason"]
    th.set_overrides(enabled=True, high_c=31)
    assert th.run(apply=True)["action"] == "none"                     # 30 is now inside 22..31
    with pytest.raises(ValueError):
        th.set_overrides(max_running=9)                               # cannot exceed the configured ceiling
    with pytest.raises(ValueError):
        th.set_overrides(low_c=35)                                    # would cross high_c
    th.reset_overrides()
    assert th.params().high_c == 24
    assert modes(cfg) == ["normal"] * 3


def test_unreachable_unit_is_left_alone(rig):
    cfg, th, temp, miners = rig
    miners[0].stop()
    temp.write_text("30")
    rec = th.run(apply=True)
    assert rec["unreachable"] == ["a"] and rec["unit"] == "b"


# -- heartbeat ---------------------------------------------------------------

def U(name, ok=True, mode="normal", temp=60):
    return {"name": name, "ok": ok, "mode": mode, "board_temp_max_c": temp}


ROOM = {"ok": True, "temp_c": 23.0, "age_min": 1}


def test_evaluate_ok_and_action_note():
    ok, msg = monitor.evaluate({"units": [U("a"), U("b", mode="sleep")], "room": ROOM}, None, {"action": "sleep", "unit": "b"})
    assert ok and msg == "running 1/2, room 23.0C (sleep b)"
    assert monitor.evaluate({"units": [U("a")]}, None, None) == (True, "running 1/1")


def test_evaluate_unreachable_needs_two_runs():
    now = {"units": [U("a", ok=False), U("b")], "room": ROOM}
    assert monitor.evaluate(now, {"units": [U("a"), U("b")]}, None)[0] is True
    ok, msg = monitor.evaluate(now, {"units": [U("a", ok=False), U("b")]}, None)
    assert not ok and msg == "unreachable a"
    assert monitor.evaluate(now, None, None)[0] is False               # no history: report at once


def test_evaluate_problems():
    snap = {"units": [U("a", temp=97)], "room": {"ok": True, "temp_c": 23, "age_min": 90}}
    ok, msg = monitor.evaluate(snap, None, {"action": "wake", "unit": "a", "error": "x"})
    assert not ok and msg == "board hot a=97C; room temperature stale 90min; wake a failed"
    assert monitor.evaluate({"units": [U("a")], "room": {"ok": False, "reason": "x"}}, None, None)[1] == "no room temperature"


def test_tick_collects_and_sends_heartbeat(rig, monkeypatch, capsys):
    cfg, th, temp, _ = rig
    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("RIVERWARDEN_PUSH_URL", f"http://127.0.0.1:{srv.server_address[1]}/push/abc")
        temp.write_text("24.6")
        out = service.tick(cfg, apply=True, fleet=Fleet(cfg, FAST))
        assert out["thermostat"]["unit"] == "a" and out["heartbeat"].startswith("up: running 2/3")
        assert "status=up" in hits[-1] and "sleep+a" in hits[-1]
        data = cfg.data_dir
        assert len((data / "status.jsonl").read_text().splitlines()) == 2      # before and after the change
        assert json.loads((data / "latest.json").read_text())["units"][0]["mode"] == "sleep"
        dry = service.tick(cfg, apply=False, fleet=Fleet(cfg, FAST))
        assert dry["heartbeat"] is None and len(hits) == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_cli_thermostat_commands(rig, tmp_path, capsys):
    cfg, _, temp, _ = rig
    path = tmp_path / "config.yaml"
    lines = ["units:"] + [f"  - {{name: {u.name}, host: '{u.host}'}}" for u in cfg.units]
    lines += [f"data_dir: '{(tmp_path / 'clidata').as_posix()}'", "thermostat:", "  low_c: 22", "  high_c: 24",
              f"  sensor: {{type: file, path: '{temp.as_posix()}'}}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert main(["-c", str(path), "status"]) == 0
    out = capsys.readouterr().out
    assert "room 23.0 C" in out and "thermostat enabled: 22..24 C, 1..3 running" in out
    assert main(["-c", str(path), "hold", "30"]) == 0 and "on hold until" in capsys.readouterr().out
    assert main(["-c", str(path), "thermostat"]) == 0 and "on hold" in capsys.readouterr().out
    assert main(["-c", str(path), "resume"]) == 0
    capsys.readouterr()
    assert main(["-c", str(path), "tick"]) == 0
    assert "3/3 reachable / room 23.0 C, 3 running: no change" in capsys.readouterr().out


def test_sensor_list_falls_back_in_order(rig, tmp_path):
    cfg, th, temp, _ = rig
    cfg.raw["thermostat"]["sensor"] = [{"type": "file", "path": str(tmp_path / "missing")},
                                       {"type": "file", "path": str(temp)}]
    temp.write_text("24.8")
    assert th.params().sensor == cfg.raw["thermostat"]["sensor"]
    rec = th.run(apply=False)
    assert rec["room"]["temp_c"] == 24.8 and rec["action"] == "sleep"
    cfg.raw["thermostat"]["sensor"] = [{"type": "file", "path": str(tmp_path / "missing")}, {"type": "nope"}]
    assert "no room temperature" in th.run(apply=False)["reason"]
