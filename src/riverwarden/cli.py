"""Command line interface.

    riverwarden -c config.yaml status
    riverwarden -c config.yaml fan rig1 45 --apply
    riverwarden -c config.yaml sleep rig1 --apply
    riverwarden -c config.yaml wake rig1 --apply
    riverwarden -c config.yaml reboot rig1 --apply
    riverwarden selftest rig1.example            # read-only compatibility report
    riverwarden selftest rig1.example --write    # also tries fan, sleep and wake

    riverwarden -c config.yaml thermostat        # what the thermostat would do now
    riverwarden -c config.yaml tick --apply      # scheduled run: collect, thermostat, heartbeat
    riverwarden -c config.yaml hold 120          # pause the thermostat for two hours
    riverwarden -c config.yaml resume

    riverwarden -c config.yaml api               # REST API
    riverwarden -c config.yaml mcp               # MCP server on stdio

Anything that changes a miner needs ``--apply``; without it the command only
says what it would do.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from . import service
from .fleet import Fleet, FleetError, Timing, Unit, WriteLock, load_config
from .iceriver import IceRiverClient, IceRiverError
from .thermostat import Thermostat


def format_snapshot(snap: dict) -> str:
    lines = [f"{len(snap['units'])} unit(s)  {snap['ts']}"]
    room = snap.get("room")
    if room is not None:
        lines.append(f"room {room['temp_c']} C ({room['source']}, {room['age_min']} min old)" if room.get("ok")
                     else f"room temperature unavailable: {room.get('reason')}")
    lines += [f"{'unit':<12} {'mode':<7} {'now GH/s':>9} {'avg GH/s':>9} {'board C':>8} {'fan %':>6}  {'rpm':<14} {'accepted':>9}  uptime"]
    for u in snap["units"]:
        if not u["ok"]:
            lines.append(f"{u['name']:<12} unreachable  {u.get('error', '')}")
            continue
        rpm = "/".join(str(x) for x in u["fans_rpm"])
        temp = u.get("board_temp_max_c")
        lines.append(
            f"{u['name']:<12} {u['mode']:<7} {u['rt_ghs'] or 0:>9.0f} {u['avg_ghs'] or 0:>9.0f} "
            f"{(f'{temp:.0f}' if temp is not None else '-'):>8} {u['fan_percent'] if u['fan_percent'] is not None else '-':>6}  "
            f"{rpm:<14} {u['accepted']:>9}  {u['uptime']}")
    running = sum(1 for u in snap["units"] if u["ok"] and u["mode"] == "normal")
    total = sum((u.get("avg_ghs") or 0) for u in snap["units"] if u["ok"])
    lines.append(f"running {running}/{len(snap['units'])}, total average {total:.0f} GH/s")
    lines += [f"warning: {a}" for a in snap["alerts"]]
    return "\n".join(lines)


def format_thermostat(th: Thermostat) -> str:
    state = th.load_state()
    p = th.params(state)
    lines = [f"thermostat {'enabled' if p.enabled else 'disabled'}: {p.low_c:g}..{p.high_c:g} C, "
             f"{p.min_running}..{p.max_running} running, sleep order {' > '.join(p.sleep_order)}"]
    if state.get("hold_until"):
        lines.append(f"on hold until {state['hold_until']}")
    if state.get("last_change_at"):
        lines.append(f"last change at {state['last_change_at']}")
    return "\n".join(lines)


def selftest(host: str, write: bool, timing: Timing | None = None) -> tuple[bool, str]:
    """Check one miner and produce a report that can be pasted into an issue."""
    rows: list[tuple[str, str, str]] = []
    ok = True

    def step(name, fn):
        nonlocal ok
        t0 = time.time()
        try:
            detail = fn() or ""
            rows.append((name, "PASS", f"{detail} ({time.time() - t0:.0f} s)".strip()))
            return True
        except (IceRiverError, FleetError, ValueError) as e:
            ok = False
            rows.append((name, "FAIL", f"{type(e).__name__}: {e}"))
            return False

    client = IceRiverClient(host)
    info: dict = {}

    def read():
        st = client.read_status()
        info.update(firmware=st.firmware, boards=len(st.boards), fans=len(st.fans_rpm), mode=st.mode,
                    fan=st.fan_percent)
        return f"mode={st.mode} boards={len(st.boards)} fans={len(st.fans_rpm)} temp_ok={st.temp_ok}"

    if step("read status", read) and write:
        from .fleet import Config, Safety  # local import keeps the read-only path minimal
        unit = Unit(name="selftest", host=host)
        fleet = Fleet(Config(units=[unit], safety=Safety(wake_reboot=False)), timing)
        before = info.get("fan") or 0

        def fan():
            target = 50 if before != 50 else 55
            fleet.set_fan(unit, target)
            return f"fan {before} % -> {target} %"

        def sleep():
            st = fleet.sleep(unit)
            return f"mode={st.mode} temp_ok={st.temp_ok}"

        def wake_plain():
            client.set_mode("normal")
            st = fleet._wait_hashing(unit, fleet.t.wake_timeout)
            if st is None:
                raise FleetError("no accepted shares after writing normal mode (a reboot is needed on this model)")
            return f"hashing without reboot, temp_ok={st.temp_ok}"

        def wake_reboot():
            fleet.cfg.safety.wake_reboot = True
            st, _ = fleet.wake(unit)
            return f"hashing after reboot, temp_ok={st.temp_ok}"

        step("set fan (manual)", fan)
        if step("sleep", sleep):
            if not step("wake without reboot", wake_plain):
                ok = True  # informative only: the reboot path below is the supported one
            step("wake with reboot", wake_reboot)
        rows.append(("note", "INFO", "the miner was rebooted; a manual fan setting does not survive a reboot"))

    report = ["### riverwarden selftest", "",
              f"- riverwarden: {__version__}", f"- firmware: `{info.get('firmware', 'unknown')}`",
              f"- boards: {info.get('boards', '?')}, fans: {info.get('fans', '?')}",
              f"- write tests: {'yes' if write else 'no (read-only)'}", "",
              "| step | result | detail |", "|---|---|---|"]
    report += [f"| {n} | {r} | {d} |" for n, r, d in rows]
    return ok, "\n".join(report)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="riverwarden", description="Fleet control for stock-firmware IceRiver miners (unofficial)")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-c", "--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="show every unit")
    s.add_argument("--json", action="store_true")
    f = sub.add_parser("fan", help="set a manual fan percentage")
    f.add_argument("unit"); f.add_argument("percent", type=int); f.add_argument("--apply", action="store_true")
    for name, text in (("sleep", "stop hashing (work mode: sleep)"), ("wake", "return to normal mode and confirm hashing"),
                       ("reboot", "reboot the miner")):
        p = sub.add_parser(name, help=text)
        p.add_argument("unit"); p.add_argument("--apply", action="store_true")
    t = sub.add_parser("selftest", help="check one miner and print a compatibility report")
    t.add_argument("host"); t.add_argument("--write", action="store_true", help="also test fan, sleep and wake (reboots the miner)")
    sub.add_parser("collect", help="append a snapshot to data_dir/status.jsonl")
    for name, text in (("thermostat", "evaluate the thermostat once"),
                       ("tick", "scheduled run: collect, thermostat, heartbeat")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--apply", action="store_true")
    h = sub.add_parser("hold", help="pause the thermostat for N minutes")
    h.add_argument("minutes", type=int)
    for name, text in (("resume", "end a hold"), ("enable", "turn the thermostat on"), ("disable", "turn the thermostat off"),
                       ("api", "serve the REST API"), ("mcp", "serve the MCP server on stdio")):
        sub.add_parser(name, help=text)
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        ok, report = selftest(args.host, args.write)
        print(report)
        return 0 if ok else 1

    if args.cmd == "api":
        from .api import serve
        return serve(args.config)
    if args.cmd == "mcp":
        from .mcp import serve
        return serve(args.config)

    cfg = load_config(args.config)
    if args.cmd == "status":
        snap = service.full_snapshot(cfg)
        if args.json:
            print(json.dumps(snap, indent=1))
        else:
            print(format_snapshot(snap))
            if cfg.raw.get("thermostat"):
                print(format_thermostat(Thermostat(cfg)))
        return 0
    if args.cmd == "collect":
        print(format_snapshot(service.collect(cfg)))
        return 0
    if args.cmd in ("hold", "resume", "enable", "disable"):
        th = Thermostat(cfg)
        if args.cmd == "hold":
            th.hold(args.minutes)
        elif args.cmd == "resume":
            th.resume()
        else:
            th.set_overrides(enabled=args.cmd == "enable")
        print(format_thermostat(th))
        return 0
    if args.cmd == "thermostat":
        rec = Thermostat(cfg).run(args.apply)
        print(rec["reason"] + (f"\n  error: {rec['error']}" if rec.get("error") else ""))
        return 1 if rec.get("error") else 0
    if args.cmd == "tick":
        out = service.tick(cfg, args.apply)
        snap, rec = out["snapshot"], out["thermostat"]
        reachable = sum(1 for u in snap["units"] if u["ok"])
        line = f"[{snap['ts']}] {reachable}/{len(snap['units'])} reachable"
        if rec:
            line += f" / {rec['reason']}" + (f" / error: {rec['error']}" if rec.get("error") else "")
        if out["heartbeat"]:
            line += f" / heartbeat {out['heartbeat']}"
        print(line)
        for a in snap["alerts"]:
            print(f"  warning: {a}")
        return 0

    unit = cfg.find(args.unit)
    what = {"fan": f"set the fan of {unit.name} to {getattr(args, 'percent', '')} %", "sleep": f"put {unit.name} to sleep",
            "wake": f"wake {unit.name}" + (" (with a reboot)" if cfg.safety.wake_reboot else ""),
            "reboot": f"reboot {unit.name}"}[args.cmd]
    if not args.apply:
        print(f"[dry run] would {what}. Add --apply to do it.")
        return 0
    fleet = Fleet(cfg)
    with WriteLock(cfg.data_dir):
        if args.cmd == "fan":
            fleet.set_fan(unit, args.percent)
        elif args.cmd == "sleep":
            fleet.sleep(unit)
        elif args.cmd == "wake":
            _, rebooted = fleet.wake(unit)
            if rebooted:
                print("(rebooted as part of waking)")
        else:
            fleet.reboot(unit)
            print(f"reboot sent to {unit.name}; it will be back in a minute or two")
            return 0
    snap = service.full_snapshot(cfg)
    print(format_snapshot({"ts": snap["ts"], "units": [u for u in snap["units"] if u["name"] == unit.name], "alerts": []}))
    return 0


def run() -> None:
    try:
        sys.exit(main())
    except (KeyError, ValueError, FleetError, IceRiverError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    run()
