"""Client for the web-console API of stock IceRiver firmware (unofficial).

The stock firmware has no documented API. This module talks to the same HTTP
endpoints the built-in web console uses, so nothing is flashed or modified on
the miner.

Behaviour verified on real hardware (AL0, firmware ``ICM168_04_02_1005``):

* The miner does not enforce the console login. The login check lives in the
  browser (a ``ctime=1`` cookie), so anything that can reach port 80 can read
  and change settings. Restrict access at the network level.
* ``/user/userpanel`` with ``post=2`` is **factory reset**, one digit away from
  reboot (``post=3``). This client refuses to send anything that is not in
  :data:`ALLOWED`.
* ``/user/machineconfig`` with ``post=3`` saves the "hardware" form. The console
  always submits the three pool rows together with it, so this client does too.
* When ``fancontrol=on`` is present the firmware applies the fan percentage and
  **ignores the work mode**. To change the mode (normal / sleep) the request
  must not carry ``fancontrol``. Fan and mode are therefore two separate writes.
* Writing the mode without ``fancontrol`` puts the fan back under firmware
  control. A manual fan percentage is also lost on reboot.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

# The only (path, post) pairs this client will ever send.
# Deliberately absent: userpanel post=2 (factory reset), machineconfig post=2
# (save pools), ipconfig, systemconfig (password) and firconfig (firmware upload).
ALLOWED = frozenset({
    ("/user/userpanel", 4),      # read status
    ("/user/userpanel", 3),      # reboot
    ("/user/machineconfig", 1),  # read settings
    ("/user/machineconfig", 3),  # save fan / work mode
})

MODES = ("normal", "sleep")

# Read-only commands of the status port (4111). No write command is known on that port.
STATUS_PORT = 4111
STATUS_COMMANDS = frozenset({"state", "info", "fan", "board", "boardpow", "getchipinfo", "getpool", "getnet", "getlocate"})
_UNIT_TO_GHS = {"K": 1e-6, "M": 1e-3, "G": 1.0, "T": 1e3, "P": 1e6}


class IceRiverError(Exception):
    """Base class for errors raised by this module."""


class Unreachable(IceRiverError):
    """The miner did not answer (powered off, rebooting, wrong address, filtered)."""


class UnsupportedFirmware(IceRiverError):
    """The device answered, but not like stock IceRiver firmware."""


class Refused(IceRiverError):
    """The firmware answered with a non-zero error code."""


class NotAllowed(IceRiverError):
    """The request is not in :data:`ALLOWED` and was not sent."""


def parse_hashrate(text, default_unit: str = "G") -> float | None:
    """Convert console strings such as ``"403G"`` or ``"1.2T"`` to GH/s."""
    if text is None:
        return None
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*([KMGTP]?)\s*", str(text), re.IGNORECASE)
    if not m:
        return None
    unit = (m.group(2) or default_unit or "G").upper()
    return round(float(m.group(1)) * _UNIT_TO_GHS.get(unit, 1.0), 6)


def parse_uptime(text) -> int | None:
    """Convert the console's ``DD:HH:MM:SS`` runtime to seconds."""
    try:
        d, h, m, s = (int(x) for x in str(text).split(":"))
    except (TypeError, ValueError):
        return None
    return ((d * 24 + h) * 60 + m) * 60 + s


@dataclass
class Board:
    no: int
    chips: int | None
    freq_mhz: float | None
    temp_in_c: float | None
    temp_out_c: float | None
    rt_ghs: float | None
    avg_ghs: float | None
    ok: bool | None


@dataclass
class Pool:
    no: int
    url: str
    user: str
    connected: bool
    accepted: int
    rejected: int


@dataclass
class Status:
    mode: str                      # "normal" or "sleep"
    fan_percent: int | None        # as reported by the firmware (0 can mean "idle under auto control")
    fans_rpm: list[int]
    boards: list[Board]
    pools: list[Pool]
    rt_ghs: float | None
    avg_ghs: float | None
    uptime_s: int | None
    uptime: str | None
    hash_ok: bool | None
    net_ok: bool | None
    fan_ok: bool | None
    temp_ok: bool | None           # latches False after sleep until the next reboot
    firmware: str | None
    mac: str | None
    hostname: str | None
    locate: bool | None = None
    raw_config: dict = field(default_factory=dict, repr=False)

    @property
    def board_temp_max_c(self) -> float | None:
        temps = [t for b in self.boards for t in (b.temp_in_c, b.temp_out_c) if t is not None]
        return max(temps) if temps else None

    @property
    def pool_connected(self) -> bool:
        return any(p.connected for p in self.pools)

    @property
    def accepted(self) -> int:
        return sum(p.accepted for p in self.pools)

    @property
    def rejected(self) -> int:
        return sum(p.rejected for p in self.pools)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw_config", None)
        d.update(board_temp_max_c=self.board_temp_max_c, pool_connected=self.pool_connected,
                 accepted=self.accepted, rejected=self.rejected)
        return d


def _int(value, default=None):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class IceRiverClient:
    """One miner. ``host`` may include a port (``"rig1.example:8080"``)."""

    def __init__(self, host: str, timeout: float = 8.0):
        self.host = host
        self.timeout = timeout

    # -- transport ---------------------------------------------------------
    def _post(self, path: str, fields, timeout: float | None = None) -> dict:
        pairs = list(fields.items()) if isinstance(fields, dict) else list(fields)
        posts = [v for k, v in pairs if k == "post"]
        if len(posts) != 1 or (path, _int(posts[0])) not in ALLOWED:
            raise NotAllowed(f"refusing to send {path} post={posts}")
        req = urllib.request.Request(
            f"http://{self.host}{path}", data=urllib.parse.urlencode(pairs).encode(), method="POST",
            headers={"Cookie": "ctime=1", "X-Requested-With": "XMLHttpRequest"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 307, 308):
                raise UnsupportedFirmware(
                    f"{self.host} redirects (HTTP {e.code}); this looks like custom firmware, which is not supported") from None
            raise Unreachable(f"{self.host}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise Unreachable(f"{self.host}: {getattr(e, 'reason', e)}") from None
        try:
            doc = json.loads(body)
        except json.JSONDecodeError:
            raise UnsupportedFirmware(f"{self.host} did not answer with JSON on {path}") from None
        if not isinstance(doc, dict) or "error" not in doc:
            raise UnsupportedFirmware(f"{self.host} answered with an unexpected document on {path}")
        if doc["error"] != 0:
            raise Refused(f"{self.host}: firmware error {doc['error']}: {doc.get('message', '')}")
        return doc.get("data") or {}

    # -- reads -------------------------------------------------------------
    def read_config(self) -> dict:
        """Raw settings document: ``pools``, ``mode`` (1 = normal), ``ratio`` (fan %)."""
        cfg = self._post("/user/machineconfig", {"post": 1})
        if "pools" not in cfg or "mode" not in cfg:
            raise UnsupportedFirmware(f"{self.host}: settings document has no 'pools' / 'mode'")
        return cfg

    def read_status(self) -> Status:
        cfg = self.read_config()
        st = self._post("/user/userpanel", {"post": 4})
        unit = str(st.get("unit") or "G")
        boards = [Board(
            no=_int(b.get("no"), i + 1), chips=_int(b.get("chipnum")), freq_mhz=_float(b.get("freq")),
            temp_in_c=_float(b.get("intmp")), temp_out_c=_float(b.get("outtmp")),
            rt_ghs=parse_hashrate(b.get("rtpow"), unit), avg_ghs=parse_hashrate(b.get("avgpow"), unit),
            ok=b.get("state")) for i, b in enumerate(st.get("boards") or [])]
        pools = [Pool(
            no=_int(p.get("no"), i + 1), url=str(p.get("addr") or ""), user=str(p.get("user") or ""),
            connected=_int(p.get("connect"), 0) == 1, accepted=_int(p.get("accepted"), 0),
            rejected=_int(p.get("rejected"), 0)) for i, p in enumerate(st.get("pools") or [])]
        fans = [_int(x, 0) for x in (st.get("fans") or [])]
        count = _int(st.get("fancount"))
        if count:
            fans = fans[:count]
        return Status(
            mode="normal" if cfg.get("mode") == 1 else "sleep", fan_percent=_int(cfg.get("ratio")),
            fans_rpm=fans, boards=boards, pools=pools,
            rt_ghs=parse_hashrate(st.get("rtpow"), unit), avg_ghs=parse_hashrate(st.get("avgpow"), unit),
            uptime_s=parse_uptime(st.get("runtime")), uptime=st.get("runtime"),
            hash_ok=st.get("powstate"), net_ok=st.get("netstate"), fan_ok=st.get("fanstate"),
            temp_ok=st.get("tempstate"), firmware=st.get("softver1"), mac=st.get("mac"),
            hostname=st.get("host"), locate=bool(cfg.get("locate")) if "locate" in cfg else None,
            raw_config=cfg)

    # -- writes ------------------------------------------------------------
    @staticmethod
    def _pool_fields(cfg: dict) -> list:
        fields = []
        for p in cfg.get("pools") or []:
            n = _int(p.get("no"))
            fields += [(f"pool{n}address", p.get("addr", "")), (f"pool{n}miner", p.get("user", "")),
                       (f"pool{n}pwd", p.get("pass", ""))]
        if len(fields) != 9:
            raise UnsupportedFirmware("expected three pool rows in the settings document; not writing")
        return fields

    def set_fan(self, percent: int) -> None:
        """Set a manual fan percentage. Does not change the work mode."""
        if not 0 <= int(percent) <= 100:
            raise ValueError("fan percent must be 0..100")
        cfg = self.read_config()
        mode = "normal" if cfg.get("mode") == 1 else "sleep"
        self._post("/user/machineconfig", self._pool_fields(cfg) + [
            ("fancontrol", "on"), ("fanratio", str(int(percent))), ("fanmode", mode), ("post", "3")], timeout=60)

    def set_mode(self, mode: str) -> None:
        """Set the work mode. Sent without ``fancontrol``, otherwise the firmware ignores it."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        cfg = self.read_config()
        ratio = _int(cfg.get("ratio")) or 100
        self._post("/user/machineconfig", self._pool_fields(cfg) + [
            ("fanratio", str(ratio)), ("fanmode", mode), ("post", "3")], timeout=60)

    def reboot(self) -> None:
        self._post("/user/userpanel", {"post": 3}, timeout=60)


def query_status_port(host: str, command: str, port: int = STATUS_PORT, timeout: float = 5.0) -> dict:
    """Ask the unauthenticated status port one read-only question and return its ``ret`` document."""
    if command not in STATUS_COMMANDS:
        raise NotAllowed(f"refusing to send status-port command {command!r}")
    name = host.split(":")[0]
    try:
        with socket.create_connection((name, port), timeout=timeout) as s:
            s.sendall(json.dumps({"id": command}).encode() + b"\r\n")
            s.settimeout(2.5)          # the miner keeps the connection open; stop reading when it goes quiet
            buf = b""
            try:
                while True:
                    part = s.recv(8192)
                    if not part:
                        break
                    buf += part
            except socket.timeout:
                pass
    except OSError as e:
        raise Unreachable(f"{name}:{port}: {e}") from None
    try:
        doc = json.loads(buf.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        raise UnsupportedFirmware(f"{name}:{port} did not answer {command!r} with JSON") from None
    return doc.get("ret") or {}


def read_chips(host: str, port: int = STATUS_PORT) -> list[dict]:
    """Per-chip temperature, voltage and PLL (entries that are all zero are dropped)."""
    chips = query_status_port(host, "getchipinfo", port).get("chips") or []
    return [c for c in chips if c.get("temp") or c.get("voltage") or c.get("pll")]
