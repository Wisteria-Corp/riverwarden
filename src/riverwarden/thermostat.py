"""Room-temperature thermostat: sleeps or wakes one miner at a time.

The miners are the heater. When the room is warmer than ``high_c`` one running
unit is put to sleep; when it is colder than ``low_c`` one sleeping unit is
woken. At most one unit changes per run, and changes are spaced out by
``min_minutes_between_changes``. When the room temperature is missing or stale
the thermostat does nothing.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import sensors
from .fleet import Busy, Config, Fleet, FleetError, WriteLock, now_iso, read_unit
from .iceriver import IceRiverError

OVERRIDABLE = ("enabled", "low_c", "high_c", "min_running", "max_running")


@dataclass
class Params:
    low_c: float
    high_c: float
    sleep_order: list[str]          # first entry sleeps first; waking goes in reverse
    enabled: bool = True
    min_running: int = 1
    max_running: int = 0
    min_minutes_between_changes: float = 30
    max_sensor_age_minutes: float = 45
    sensor: dict | list = field(default_factory=dict)   # one source, or a list tried in order

    @classmethod
    def from_config(cls, cfg: Config, overrides: dict | None = None) -> "Params":
        raw = dict(cfg.raw.get("thermostat") or {})
        if not raw:
            raise ValueError("the configuration has no 'thermostat' section")
        for key, value in (overrides or {}).items():
            if key in OVERRIDABLE and value is not None:
                raw[key] = value
        names = [u.name for u in cfg.units]
        order = [str(n) for n in (raw.get("sleep_order") or names)]
        unknown = [n for n in order if n not in names]
        if unknown:
            raise ValueError(f"sleep_order names unknown units: {', '.join(unknown)}")
        return cls(
            low_c=float(raw["low_c"]), high_c=float(raw["high_c"]), sleep_order=order,
            enabled=bool(raw.get("enabled", True)), min_running=int(raw.get("min_running", 1)),
            max_running=int(raw.get("max_running", len(names))),
            min_minutes_between_changes=float(raw.get("min_minutes_between_changes", 30)),
            max_sensor_age_minutes=float(raw.get("max_sensor_age_minutes", 45)),
            sensor=raw.get("sensor") or {})


@dataclass
class Decision:
    action: str            # "none", "sleep" or "wake"
    unit: str | None
    reason: str


def decide(p: Params, temp_c: float, running: list[str], sleeping: list[str]) -> Decision:
    """Pure decision: which single unit, if any, should change. Units outside ``sleep_order`` are never touched."""
    if temp_c > p.high_c and len(running) > p.min_running:
        target = next((n for n in p.sleep_order if n in running), None)
        if target:
            return Decision("sleep", target, f"room {temp_c:.1f} C is above {p.high_c:g} C: sleep {target}")
    elif temp_c < p.low_c and len(running) < p.max_running:
        target = next((n for n in reversed(p.sleep_order) if n in sleeping), None)
        if target:
            return Decision("wake", target, f"room {temp_c:.1f} C is below {p.low_c:g} C: wake {target}")
    return Decision("none", None, f"room {temp_c:.1f} C, {len(running)} running: no change")


def read_room(cfg: Config) -> dict:
    """Room temperature as a plain dict; ``ok`` is False with a ``reason`` when it cannot be read."""
    spec = (cfg.raw.get("thermostat") or {}).get("sensor")
    if not spec:
        return {"ok": False, "reason": "no sensor configured"}
    reasons = []
    for one in (spec if isinstance(spec, list) else [spec]):     # a list is tried in order (fallback sensors)
        try:
            r = sensors.read(one)
        except sensors.SensorError as e:
            reasons.append(str(e))
            continue
        return {"ok": True, "temp_c": r.temp_c, "age_min": round(r.age_s / 60, 1), "source": r.source}
    return {"ok": False, "reason": "; ".join(reasons)}


def _now() -> datetime:
    return datetime.now().astimezone()


class Thermostat:
    def __init__(self, cfg: Config, fleet: Fleet | None = None):
        self.cfg = cfg
        self.fleet = fleet or Fleet(cfg)
        self.state_path = Path(cfg.data_dir) / "thermostat.json"
        self.log_path = Path(cfg.data_dir) / "thermostat.jsonl"

    # -- state -------------------------------------------------------------
    def load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def params(self, state: dict | None = None) -> Params:
        state = self.load_state() if state is None else state
        return Params.from_config(self.cfg, state.get("overrides"))

    def hold(self, minutes: int) -> str:
        """Pause the thermostat (for example after a manual sleep or wake)."""
        state = self.load_state()
        state["hold_until"] = (_now() + timedelta(minutes=int(minutes))).replace(microsecond=0).isoformat()
        self.save_state(state)
        return state["hold_until"]

    def resume(self) -> None:
        state = self.load_state()
        state.pop("hold_until", None)
        self.save_state(state)

    def set_overrides(self, **values) -> Params:
        """Change thresholds at run time without editing the configuration. Bounds come from the configuration."""
        base = Params.from_config(self.cfg)
        state = self.load_state()
        ov = dict(state.get("overrides") or {})
        for key, value in values.items():
            if key not in OVERRIDABLE or value is None:
                continue
            if key in ("low_c", "high_c"):
                value = float(value)
                if not 5 <= value <= 40:
                    raise ValueError(f"{key} must be between 5 and 40 C")
            elif key in ("min_running", "max_running"):
                value = int(value)
                if not 0 <= value <= base.max_running:
                    raise ValueError(f"{key} must be between 0 and {base.max_running} (raise max_running in the configuration first)")
            else:
                value = bool(value)
            ov[key] = value
        p = Params.from_config(self.cfg, ov)
        if p.low_c >= p.high_c or p.min_running > p.max_running:
            raise ValueError("low_c must be below high_c and min_running must not exceed max_running")
        state["overrides"] = ov
        self.save_state(state)
        return p

    def reset_overrides(self) -> None:
        state = self.load_state()
        state.pop("overrides", None)
        self.save_state(state)

    # -- one evaluation ----------------------------------------------------
    def run(self, apply: bool, room: dict | None = None) -> dict:
        """Evaluate once and, with ``apply``, act on at most one unit. Returns a record of the decision."""
        state = self.load_state()
        rec: dict = {"ts": now_iso(), "apply": apply, "action": "none", "unit": None}

        def done(reason: str) -> dict:
            rec["reason"] = reason
            if apply:
                state["last_eval"] = rec
                self.save_state(state)
                with open(self.log_path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(rec) + "\n")
            return rec

        try:
            p = self.params(state)
        except (ValueError, KeyError, TypeError) as e:
            return done(f"configuration error: {e}")
        rec["params"] = {k: getattr(p, k) for k in ("low_c", "high_c", "min_running", "max_running")}
        if not p.enabled:
            return done("thermostat is disabled")
        hold = state.get("hold_until")
        if hold and datetime.fromisoformat(hold) > _now():
            return done(f"on hold until {hold}")
        if p.low_c >= p.high_c:
            return done("configuration error: low_c must be below high_c")

        room = room if room is not None else read_room(self.cfg)
        rec["room"] = room
        if not room.get("ok"):
            return done(f"no room temperature ({room.get('reason')}); doing nothing")
        if room["age_min"] > p.max_sensor_age_minutes:
            return done(f"room temperature is {room['age_min']:.0f} min old; doing nothing")

        units = {u.name: read_unit(u) for u in self.cfg.units}
        running = [n for n, s in units.items() if s["ok"] and s["mode"] == "normal"]
        sleeping = [n for n, s in units.items() if s["ok"] and s["mode"] == "sleep"]
        rec.update(running=running, sleeping=sleeping, unreachable=[n for n, s in units.items() if not s["ok"]])

        last = state.get("last_change_at")
        if last and (_now() - datetime.fromisoformat(last)).total_seconds() / 60 < p.min_minutes_between_changes:
            return done(f"last change was at {last}; waiting {p.min_minutes_between_changes:g} min between changes")

        d = decide(p, room["temp_c"], running, sleeping)
        rec.update(action=d.action, unit=d.unit)
        if d.action == "none":
            return done(d.reason)
        if not apply:
            return done(d.reason + " (dry run)")
        unit = self.cfg.find(str(d.unit))
        try:
            with WriteLock(self.cfg.data_dir):
                if d.action == "sleep":
                    self.fleet.sleep(unit)
                else:
                    _, rec["rebooted"] = self.fleet.wake(unit)
        except Busy:
            rec.update(action="none", unit=None)
            return done("another write operation is in progress; skipped this run")
        except (FleetError, IceRiverError) as e:
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
            return done(f"{d.action} {d.unit} failed")
        state["last_change_at"] = now_iso()
        return done(d.reason + " (done)")
