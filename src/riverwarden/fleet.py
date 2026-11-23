"""Fleet-level operations: several miners, verified writes, one writer at a time.

Every write is followed by a read-back. A command that the firmware accepted
but did not act on is reported as a failure instead of being assumed to work.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .iceriver import IceRiverClient, IceRiverError, Status, Unreachable


class FleetError(Exception):
    """An operation did not reach the expected state."""


class Busy(FleetError):
    """Another write operation holds the lock."""


@dataclass
class Unit:
    name: str
    host: str
    fan_percent: int | None = None   # manual fan to re-apply after wake / reboot (None = firmware control)

    def client(self, timeout: float = 8.0) -> IceRiverClient:
        return IceRiverClient(self.host, timeout=timeout)


@dataclass
class Timing:
    """Waits used by sleep / wake. Tests shrink these; real miners need the defaults."""
    poll: float = 10.0            # seconds between read-backs
    sleep_timeout: float = 60.0   # mode must read "sleep" within this
    settle: float = 8.0           # after a fan write, before reading back
    share_poll: float = 15.0      # seconds between "are shares increasing" checks
    wake_timeout: float = 100.0   # shares must increase within this after "normal"
    pre_reboot: float = 5.0       # pause between writing "normal" and rebooting
    boot_wait: float = 45.0       # do not poll a rebooting miner before this
    boot_timeout: float = 240.0   # shares must increase within this after reboot


@dataclass
class Safety:
    fan_min_percent: int = 20     # a manual 0 % would stop the fans
    wake_reboot: bool = True      # reboot on wake: the firmware's temperature flag only clears on reboot
    board_temp_warn_c: float = 95.0


@dataclass
class Config:
    units: list[Unit]
    safety: Safety = field(default_factory=Safety)
    data_dir: Path = Path("./riverwarden-data")
    raw: dict = field(default_factory=dict, repr=False)
    config_dir: Path = Path(".")     # where the configuration file lives; check commands run from here

    def find(self, key: str) -> Unit:
        """Look a unit up by name, host, or the last octet of its address."""
        key = str(key).strip()
        for u in self.units:
            tail = u.host.split(":")[0].rsplit(".", 1)[-1]
            if key in (u.name, u.host, u.host.split(":")[0], tail):
                return u
        raise KeyError(f"no such unit: {key} (known: {', '.join(u.name for u in self.units)})")


def load_config(path: str | os.PathLike) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    units = [Unit(name=str(u["name"]), host=str(u["host"]), fan_percent=u.get("fan_percent"))
             for u in raw.get("units") or []]
    if not units:
        raise ValueError(f"{path}: no units configured")
    names = [u.name for u in units]
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: unit names must be unique")
    safety = Safety(**{k: v for k, v in (raw.get("safety") or {}).items() if k in Safety.__dataclass_fields__})
    config_dir = Path(path).resolve().parent
    data_dir = Path(os.environ.get("RIVERWARDEN_DATA_DIR") or raw.get("data_dir") or "riverwarden-data")
    if not data_dir.is_absolute():
        data_dir = config_dir / data_dir      # relative paths are relative to the configuration file
    return Config(units=units, safety=safety, data_dir=data_dir, raw=raw, config_dir=config_dir)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def read_unit(unit: Unit) -> dict:
    """Status of one unit as a plain dict. ``ok`` is False when it could not be read."""
    out = {"name": unit.name, "host": unit.host, "ok": False}
    try:
        st = unit.client().read_status()
    except IceRiverError as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out.update(ok=True, **st.to_dict())
    return out


def snapshot(cfg: Config) -> dict:
    units = [read_unit(u) for u in cfg.units]
    alerts = []
    for u in units:
        if not u["ok"]:
            alerts.append(f"{u['name']}: unreachable ({u.get('error', '')})")
        elif u["mode"] == "normal" and (u.get("board_temp_max_c") or 0) >= cfg.safety.board_temp_warn_c:
            alerts.append(f"{u['name']}: board at {u['board_temp_max_c']:.0f} C (warn at {cfg.safety.board_temp_warn_c:.0f} C)")
    return {"ts": now_iso(), "units": units, "alerts": alerts}


class WriteLock:
    """Allows one write operation at a time across processes (CLI, scheduler, API)."""

    def __init__(self, data_dir: Path, stale_seconds: float = 900):
        self.path = Path(data_dir) / "write.lock"
        self.stale = stale_seconds
        self.held = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > self.stale:
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Busy("another write operation is in progress; try again in a few minutes") from None
        os.write(fd, f"{os.getpid()} {now_iso()}".encode())
        os.close(fd)
        self.held = True
        return self

    def __exit__(self, *exc):
        if self.held:
            self.path.unlink(missing_ok=True)


class Fleet:
    def __init__(self, cfg: Config, timing: Timing | None = None):
        self.cfg = cfg
        self.t = timing or Timing()

    # -- helpers -----------------------------------------------------------
    def _status(self, unit: Unit) -> Status | None:
        try:
            return unit.client().read_status()
        except Unreachable:
            return None

    def _wait(self, unit: Unit, cond, timeout: float) -> Status | None:
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(self.t.poll)
            st = self._status(unit)
            if st is not None and cond(st):
                return st
        return None

    def _wait_hashing(self, unit: Unit, timeout: float) -> Status | None:
        """Wait until accepted shares increase: evidence of hashing, not just "connected"."""
        prev = None
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(self.t.share_poll)
            st = self._status(unit)
            if st is None:
                prev = None
                continue
            key = (st.uptime_s or 0, st.accepted)
            if prev is not None and st.pool_connected:
                same_boot = key[0] >= prev[0]
                if (same_boot and key[1] > prev[1]) or (not same_boot and key[1] > 0):
                    return st
            prev = key
        return None

    # -- operations --------------------------------------------------------
    def set_fan(self, unit: Unit, percent: int) -> Status:
        lo = self.cfg.safety.fan_min_percent
        if not lo <= int(percent) <= 100:
            raise ValueError(f"fan percent must be {lo}..100 (got {percent})")
        unit.client().set_fan(int(percent))
        time.sleep(self.t.settle)
        st = unit.client().read_status()
        if st.fan_percent != int(percent):
            raise FleetError(f"{unit.name}: fan reads {st.fan_percent} % after writing {percent} %")
        return st

    def sleep(self, unit: Unit) -> Status:
        unit.client().set_mode("sleep")
        st = self._wait(unit, lambda s: s.mode == "sleep", self.t.sleep_timeout)
        if st is None:
            raise FleetError(f"{unit.name}: still not in sleep mode after {self.t.sleep_timeout:.0f} s")
        return st

    def reboot(self, unit: Unit) -> None:
        unit.client().reboot()

    def wake(self, unit: Unit) -> tuple[Status, bool]:
        """Return to normal mode and confirm hashing. Returns ``(status, rebooted)``."""
        unit.client().set_mode("normal")
        st, rebooted = None, False
        if not self.cfg.safety.wake_reboot:
            st = self._wait_hashing(unit, self.t.wake_timeout)
        if st is None:
            time.sleep(self.t.pre_reboot)
            unit.client().reboot()
            rebooted = True
            time.sleep(self.t.boot_wait)
            st = self._wait_hashing(unit, self.t.boot_timeout)
            if st is None:
                raise FleetError(f"{unit.name}: no accepted shares within {self.t.boot_timeout:.0f} s of rebooting")
        if st.mode != "normal":
            raise FleetError(f"{unit.name}: hashing but mode reads {st.mode}")
        if unit.fan_percent:
            st = self.set_fan(unit, int(unit.fan_percent))
        return st, rebooted
