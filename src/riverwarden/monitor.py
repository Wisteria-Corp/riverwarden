"""Heartbeat for a push-style monitor (Uptime Kuma push monitors, healthchecks.io and similar).

The heartbeat reports the *result* of a run, not just that it ran: unreachable
units, a hot board, a missing or stale room temperature, or a failed sleep/wake
turn it into a failure. If the scheduler stops altogether no heartbeat arrives
and the monitor's own timeout raises the alarm.

    monitor:
      push_url_env: RIVERWARDEN_PUSH_URL   # the URL itself stays out of the configuration
      style: kuma                           # kuma: GET url?status=up|down&msg=...
                                            # healthchecks: POST url (ok) or url/fail (problem)
      checks:                               # optional: commands run from the configuration's directory;
        - {name: backup, command: ["./check_backup.sh"]}   # non-zero exit on two runs in a row fails the heartbeat
"""
from __future__ import annotations

import os
import urllib.parse
import urllib.request


def evaluate(snap: dict, prev: dict | None, record: dict | None,
             board_temp_warn_c: float = 95, max_sensor_age_minutes: float = 45) -> tuple[bool, str]:
    """Return ``(ok, message)`` for one run.

    A unit counts as unreachable only when it was also unreachable in ``prev``
    (the previous snapshot), so a reboot that overlaps one run is not an alarm.
    A unit that really went away is therefore reported one run late.
    """
    units = snap.get("units", [])
    problems = []
    prev_bad = {u["name"] for u in (prev or {}).get("units", []) if not u.get("ok")}
    bad = [u["name"] for u in units if not u.get("ok") and (prev is None or u["name"] in prev_bad)]
    if bad:
        problems.append("unreachable " + ",".join(bad))
    hot = [f"{u['name']}={u['board_temp_max_c']:.0f}C" for u in units
           if u.get("ok") and u.get("mode") == "normal" and (u.get("board_temp_max_c") or 0) >= board_temp_warn_c]
    if hot:
        problems.append("board hot " + ",".join(hot))
    room = snap.get("room")
    if room is not None:
        if not room.get("ok"):
            problems.append("no room temperature")
        elif room.get("age_min", 0) > max_sensor_age_minutes:
            problems.append(f"room temperature stale {room['age_min']:.0f}min")
    if record and record.get("error"):
        problems.append(f"{record.get('action')} {record.get('unit')} failed")
    prev_checks = (prev or {}).get("checks") or {}
    for name, result in (snap.get("checks") or {}).items():       # extra checks also need two failures in a row
        failed_before = prev is None or (name in prev_checks and not prev_checks[name].get("ok"))
        if not result.get("ok") and failed_before:
            problems.append(f"check failed: {name}")
    if problems:
        return False, "; ".join(problems)[:180]
    running = sum(1 for u in units if u.get("ok") and u.get("mode") == "normal")
    msg = f"running {running}/{len(units)}"
    if room is not None:
        msg += f", room {room.get('temp_c')}C"
    if record and record.get("action") in ("sleep", "wake"):
        msg += f" ({record['action']} {record.get('unit')})"
    return True, msg


def push(url: str, ok: bool, msg: str, style: str = "kuma", timeout: float = 10) -> None:
    if style == "healthchecks":
        target = url if ok else url.rstrip("/") + "/fail"
        req = urllib.request.Request(target, data=msg.encode("utf-8"), method="POST")
    elif style == "kuma":
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(url + sep + urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg}))
    else:
        raise ValueError(f"unknown monitor style: {style}")
    urllib.request.urlopen(req, timeout=timeout).read()


def report(raw_config: dict, snap: dict, prev: dict | None, record: dict | None,
           board_temp_warn_c: float = 95) -> str | None:
    """Evaluate and push. Returns the message, or None when no monitor is configured."""
    m = raw_config.get("monitor") or {}
    url = os.environ.get(m.get("push_url_env", "RIVERWARDEN_PUSH_URL"))
    if not url:
        return None
    max_age = float((raw_config.get("thermostat") or {}).get("max_sensor_age_minutes", 45))
    ok, msg = evaluate(snap, prev, record, board_temp_warn_c, max_age)
    try:
        push(url, ok, msg, m.get("style", "kuma"))
    except (OSError, ValueError) as e:
        return f"{'up' if ok else 'down'}: {msg} (heartbeat not delivered: {e})"
    return f"{'up' if ok else 'down'}: {msg}"
