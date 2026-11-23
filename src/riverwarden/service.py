"""Operations shared by the CLI, the REST API and the MCP server.

Everything here returns plain dicts so the three front ends stay thin.
A scheduler runs ``riverwarden tick --apply`` every few minutes; state and
logs go to ``data_dir``:

* ``status.jsonl``   one snapshot per run
* ``latest.json``    the most recent snapshot
* ``thermostat.json`` / ``thermostat.jsonl``   thermostat state and decisions
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import monitor
from .fleet import Config, Fleet, FleetError, Unit, WriteLock, read_unit, snapshot
from .iceriver import read_chips
from .thermostat import Thermostat, read_room


def has_thermostat(cfg: Config) -> bool:
    return bool(cfg.raw.get("thermostat"))


# -- snapshots ---------------------------------------------------------------

def full_snapshot(cfg: Config, room: dict | None = None) -> dict:
    """Unit status plus, when a thermostat is configured, the room temperature."""
    snap = snapshot(cfg)
    if has_thermostat(cfg):
        snap["room"] = room if room is not None else read_room(cfg)
    return snap


def run_checks(cfg: Config) -> dict:
    """Run the extra commands under ``monitor.checks``. A non-zero exit is a failed check."""
    out = {}
    for c in (cfg.raw.get("monitor") or {}).get("checks") or []:
        cmd = c["command"]
        try:
            r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
                               timeout=float(c.get("timeout", 60)), cwd=str(cfg.config_dir))
            text = (r.stdout.strip() or r.stderr.strip()).splitlines()
            out[str(c["name"])] = {"ok": r.returncode == 0, "detail": (text[-1] if text else "")[:200]}
        except (OSError, subprocess.SubprocessError) as e:
            out[str(c["name"])] = {"ok": False, "detail": f"could not run: {type(e).__name__}: {e}"[:200]}
    return out


def read_latest(cfg: Config) -> dict | None:
    try:
        return json.loads((Path(cfg.data_dir) / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def collect(cfg: Config, room: dict | None = None, checks: dict | None = None) -> dict:
    snap = full_snapshot(cfg, room)
    checks = run_checks(cfg) if checks is None else checks
    if checks:
        snap["checks"] = checks
        snap["alerts"] += [f"check failed: {n} ({c['detail']})" for n, c in checks.items() if not c["ok"]]
    data = Path(cfg.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    with open(data / "status.jsonl", "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(snap) + "\n")
    tmp = data / "latest.json.tmp"
    tmp.write_text(json.dumps(snap, indent=1), encoding="utf-8")
    os.replace(tmp, data / "latest.json")
    return snap


def tick(cfg: Config, apply: bool, fleet: Fleet | None = None) -> dict:
    """One scheduled run. Returns ``{"snapshot", "thermostat", "heartbeat"}``."""
    prev = read_latest(cfg)
    snap = collect(cfg)
    record = None
    if has_thermostat(cfg):
        record = Thermostat(cfg, fleet).run(apply, room=snap.get("room"))
        if apply and record["action"] != "none" and not record.get("error"):
            snap = collect(cfg, room=snap.get("room"), checks=snap.get("checks") or {})
    heartbeat = monitor.report(cfg.raw, snap, prev, record, cfg.safety.board_temp_warn_c) if apply else None
    return {"snapshot": snap, "thermostat": record, "heartbeat": heartbeat}


def _tail_jsonl(path: Path, max_bytes: int = 8_000_000) -> list[dict]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def history(cfg: Config, hours: int = 6, unit: str | None = None) -> dict:
    """Recorded snapshots of the last ``hours`` (1..72), reduced to what a trend needs."""
    hours = max(1, min(int(hours), 72))
    want = cfg.find(unit).name if unit else None
    since = datetime.now().astimezone() - timedelta(hours=hours)
    rows = []
    for r in _tail_jsonl(Path(cfg.data_dir) / "status.jsonl"):
        if datetime.fromisoformat(r["ts"]) < since:
            continue
        units = [u for u in r.get("units", []) if not want or u["name"] == want]
        rows.append({
            "ts": r["ts"], "room_c": (r.get("room") or {}).get("temp_c"),
            "running": sum(1 for u in units if u.get("ok") and u.get("mode") == "normal"),
            "avg_ghs": round(sum((u.get("avg_ghs") or 0) for u in units if u.get("ok")), 1),
            "units": {u["name"]: ({"mode": u["mode"], "board_temp_max_c": u.get("board_temp_max_c"),
                                   "fan_percent": u.get("fan_percent"), "avg_ghs": u.get("avg_ghs")}
                                  if u.get("ok") else None) for u in units}})
    changes = [c for c in _tail_jsonl(Path(cfg.data_dir) / "thermostat.jsonl", 2_000_000)
               if c.get("action") != "none" and datetime.fromisoformat(c["ts"]) >= since]
    return {"hours": hours, "count": len(rows), "rows": rows, "changes": changes}


# -- thermostat --------------------------------------------------------------

def thermostat_view(cfg: Config) -> dict:
    th = Thermostat(cfg)
    state = th.load_state()
    p = th.params(state)
    return {"params": {k: getattr(p, k) for k in ("enabled", "low_c", "high_c", "min_running", "max_running",
                                                  "sleep_order", "min_minutes_between_changes", "max_sensor_age_minutes")},
            "hold_until": state.get("hold_until"), "last_change_at": state.get("last_change_at"),
            "last_eval": state.get("last_eval"), "would_do_now": th.run(apply=False)}


def thermostat_action(cfg: Config, action: str, **kw) -> dict:
    """``enable`` / ``disable`` / ``hold`` (minutes) / ``resume`` / ``set`` (thresholds) / ``reset``."""
    th = Thermostat(cfg)
    if action in ("enable", "disable"):
        th.set_overrides(enabled=action == "enable")
        th.resume()
    elif action == "hold":
        th.hold(max(1, min(int(kw.get("minutes") or 120), 24 * 60)))
    elif action == "resume":
        th.resume()
    elif action == "set":
        th.set_overrides(**{k: kw.get(k) for k in ("low_c", "high_c", "min_running", "max_running")})
    elif action == "reset":
        th.reset_overrides()
    else:
        raise ValueError("action must be one of enable, disable, hold, resume, set, reset")
    return thermostat_view(cfg)


def _hold(cfg: Config, minutes) -> None:
    """After a manual change, keep the thermostat from undoing it straight away."""
    if has_thermostat(cfg) and minutes is not None and int(minutes) > 0:
        Thermostat(cfg).hold(int(minutes))


# -- manual operations -------------------------------------------------------

def unit_status(cfg: Config, key: str) -> dict:
    return read_unit(cfg.find(key))


def chips(cfg: Config, key: str) -> dict:
    unit = cfg.find(key)
    return {"unit": unit.name, "chips": read_chips(unit.host)}


def set_fan(cfg: Config, key: str, percent: int, fleet: Fleet | None = None) -> dict:
    unit = cfg.find(key)
    with WriteLock(cfg.data_dir):
        (fleet or Fleet(cfg)).set_fan(unit, int(percent))
    return read_unit(unit)


def sleep(cfg: Config, key: str, hold_minutes=120, fleet: Fleet | None = None) -> dict:
    unit = cfg.find(key)
    _hold(cfg, hold_minutes)
    with WriteLock(cfg.data_dir):
        (fleet or Fleet(cfg)).sleep(unit)
    return read_unit(unit)


def reboot(cfg: Config, key: str, fleet: Fleet | None = None) -> None:
    unit = cfg.find(key)
    with WriteLock(cfg.data_dir):
        (fleet or Fleet(cfg)).reboot(unit)


def wake_precheck(cfg: Config, key: str) -> Unit:
    """Raise :class:`FleetError` when waking makes no sense or would exceed the thermostat's ``max_running``."""
    unit = cfg.find(key)
    snap = snapshot(cfg)
    cur = next(u for u in snap["units"] if u["name"] == unit.name)
    if cur["ok"] and cur["mode"] == "normal":
        raise FleetError(f"{unit.name} is already running")
    if has_thermostat(cfg):
        cap = Thermostat(cfg).params().max_running
        running = sum(1 for u in snap["units"] if u["ok"] and u["mode"] == "normal")
        if running >= cap:
            raise FleetError(f"{running} units are running and max_running is {cap}; not waking {unit.name}")
    return unit


def wake(cfg: Config, key: str, hold_minutes=120, fleet: Fleet | None = None) -> dict:
    """Blocking wake (minutes). Front ends usually call :func:`wake_async`."""
    unit = cfg.find(key)
    _hold(cfg, hold_minutes)
    with WriteLock(cfg.data_dir):
        _, rebooted = (fleet or Fleet(cfg)).wake(unit)
    out = read_unit(unit)
    out["rebooted"] = rebooted
    return out


def wake_async(cfg: Config, key: str, hold_minutes=120, fleet: Fleet | None = None, on_done=None) -> threading.Thread:
    """Start a wake in the background after :func:`wake_precheck`. ``on_done(result_or_exception)`` is optional."""
    wake_precheck(cfg, key)

    def work():
        try:
            result = wake(cfg, key, hold_minutes, fleet)
        except Exception as e:  # noqa: BLE001 - reported through on_done
            result = e
        if on_done:
            on_done(result)

    t = threading.Thread(target=work, name=f"wake-{key}", daemon=True)
    t.start()
    return t
