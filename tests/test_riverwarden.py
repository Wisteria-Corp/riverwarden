"""Tests run against the bundled simulator; no hardware needed."""
import pytest

from riverwarden.cli import main, selftest
from riverwarden.fleet import Config, Fleet, FleetError, Safety, Timing, Unit, WriteLock, load_config, snapshot
from riverwarden.iceriver import ALLOWED, IceRiverClient, NotAllowed, Unreachable, parse_hashrate, parse_uptime
from riverwarden.simulator import FakeMiner

FAST = Timing(poll=0.05, sleep_timeout=2, settle=0.05, share_poll=0.15, wake_timeout=1.5,
              pre_reboot=0.05, boot_wait=0.4, boot_timeout=4)


@pytest.fixture
def miner():
    m = FakeMiner()
    m.addr = m.start()
    yield m
    m.stop()


def make_fleet(miner, tmp_path, **safety):
    unit = Unit(name="rig1", host=miner.addr)
    return unit, Fleet(Config(units=[unit], safety=Safety(**safety), data_dir=tmp_path), FAST)


# -- parsing -----------------------------------------------------------------

def test_parse_hashrate_units():
    assert parse_hashrate("403G") == 403
    assert parse_hashrate("1.5T") == 1500
    assert parse_hashrate("250", "M") == 0.25
    assert parse_hashrate("(null)") is None


def test_parse_uptime():
    assert parse_uptime("01:02:03:04") == 93784
    assert parse_uptime("garbage") is None


# -- client ------------------------------------------------------------------

def test_read_status(miner):
    st = IceRiverClient(miner.addr).read_status()
    assert st.mode == "normal" and st.pool_connected and st.temp_ok is True
    assert len(st.boards) == 1 and len(st.fans_rpm) == 2
    assert st.rt_ghs == 400 and st.board_temp_max_c == 60


def test_multi_board_model():
    m = FakeMiner(boards=3, fans=4)
    addr = m.start()
    try:
        st = IceRiverClient(addr).read_status()
        assert len(st.boards) == 3 and len(st.fans_rpm) == 4
    finally:
        m.stop()


def test_dangerous_requests_are_never_sent(miner):
    client = IceRiverClient(miner.addr)
    before = len(miner.requests)
    for path, post in (("/user/userpanel", 2), ("/user/machineconfig", 2), ("/user/systemconfig", 2),
                       ("/user/ipconfig", 2), ("/user/firconfig", 2)):
        assert (path, post) not in ALLOWED
        with pytest.raises(NotAllowed):
            client._post(path, {"post": post})
    assert len(miner.requests) == before and miner.factory_resets == 0


def test_fan_write_does_not_change_mode(miner):
    client = IceRiverClient(miner.addr)
    client.set_mode("sleep")
    client.set_fan(40)
    st = client.read_status()
    assert st.mode == "sleep" and st.fan_percent == 40


def test_mode_write_keeps_pool_rows(miner):
    IceRiverClient(miner.addr).set_mode("sleep")
    _, form = miner.requests[-1]
    assert "fancontrol" not in form and form["fanmode"] == "sleep"
    assert form["pool1address"] == "stratum+tcp://pool.example:3334" and form["pool3miner"] == "wallet.worker3"


def test_unreachable():
    with pytest.raises(Unreachable):
        IceRiverClient("127.0.0.1:9", timeout=1).read_status()


# -- fleet -------------------------------------------------------------------

def test_sleep_then_wake_with_reboot_clears_temp_flag(miner, tmp_path):
    unit, fleet = make_fleet(miner, tmp_path)
    assert fleet.sleep(unit).mode == "sleep"
    assert IceRiverClient(miner.addr).read_status().temp_ok is False   # latched by sleep
    st, rebooted = fleet.wake(unit)
    assert rebooted and st.mode == "normal" and st.temp_ok is True and st.accepted > 0


def test_wake_without_reboot_when_disabled(miner, tmp_path):
    unit, fleet = make_fleet(miner, tmp_path, wake_reboot=False)
    fleet.sleep(unit)
    st, rebooted = fleet.wake(unit)
    assert not rebooted and st.mode == "normal" and st.temp_ok is False


def test_wake_falls_back_to_reboot_when_hashing_does_not_resume(tmp_path):
    m = FakeMiner(wake_needs_reboot=True)
    m.addr = m.start()
    try:
        unit, fleet = make_fleet(m, tmp_path, wake_reboot=False)
        fleet.sleep(unit)
        st, rebooted = fleet.wake(unit)
        assert rebooted and st.accepted > 0
    finally:
        m.stop()


def test_wake_reapplies_manual_fan(miner, tmp_path):
    unit, fleet = make_fleet(miner, tmp_path)
    unit.fan_percent = 42
    fleet.sleep(unit)
    st, _ = fleet.wake(unit)
    assert st.fan_percent == 42


def test_fan_floor(miner, tmp_path):
    unit, fleet = make_fleet(miner, tmp_path)
    with pytest.raises(ValueError):
        fleet.set_fan(unit, 5)
    assert fleet.set_fan(unit, 45).fan_percent == 45


def test_write_lock_is_exclusive(tmp_path):
    with WriteLock(tmp_path):
        with pytest.raises(FleetError):
            with WriteLock(tmp_path):
                pass
    with WriteLock(tmp_path):
        pass


def test_snapshot_reports_unreachable_units(miner, tmp_path):
    cfg = Config(units=[Unit("up", miner.addr), Unit("down", "127.0.0.1:9")], data_dir=tmp_path)
    snap = snapshot(cfg)
    assert [u["ok"] for u in snap["units"]] == [True, False]
    assert any("down" in a for a in snap["alerts"])


# -- cli ---------------------------------------------------------------------

def test_cli_dry_run_changes_nothing(miner, tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"units:\n  - name: rig1\n    host: '{miner.addr}'\ndata_dir: '{tmp_path.as_posix()}'\n", encoding="utf-8")
    assert load_config(cfg).find("rig1").host == miner.addr
    writes_before = sum(1 for _, f in miner.requests if f.get("post") == "3")
    assert main(["-c", str(cfg), "sleep", "rig1"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert sum(1 for _, f in miner.requests if f.get("post") == "3") == writes_before
    assert main(["-c", str(cfg), "status"]) == 0
    assert "rig1" in capsys.readouterr().out


def test_selftest_report(miner):
    ok, report = selftest(miner.addr, write=True, timing=FAST)
    assert ok
    assert "| read status | PASS" in report and "| wake with reboot | PASS" in report
    assert "ICM168" in report
