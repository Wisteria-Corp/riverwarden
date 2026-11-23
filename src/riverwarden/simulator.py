"""A small stand-in for a stock IceRiver miner, for tests and for trying the CLI without hardware.

    python -m riverwarden.simulator --port 8081

It reproduces the firmware behaviour riverwarden depends on, as observed on a
real AL0 (see ``docs/firmware-notes.md``). It is not a full emulation.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


class FakeMiner:
    """State of one simulated miner plus the HTTP server in front of it."""

    def __init__(self, boards: int = 1, fans: int = 2, reboot_seconds: float = 0.3,
                 share_interval: float = 0.05, wake_needs_reboot: bool = False,
                 firmware: str = "ICM168_04_02_1005_10106_miner"):
        self.boards = boards
        self.fans = fans
        self.reboot_seconds = reboot_seconds
        self.share_interval = share_interval        # seconds per accepted share while hashing
        self.wake_needs_reboot = wake_needs_reboot  # emulate "pool reconnects but hashing stays off"
        self.firmware = firmware
        self.lock = threading.Lock()
        self.factory_resets = 0
        self.requests: list[tuple[str, dict]] = []
        self.pools = [{"no": float(i), "addr": f"stratum+tcp://pool.example:{3333 + i}",
                       "user": f"wallet.worker{i}", "pass": ""} for i in (1, 2, 3)]
        self._boot()
        self.server: ThreadingHTTPServer | None = None

    # -- state -------------------------------------------------------------
    def _boot(self) -> None:
        self.mode = 1            # 1 = normal, 0 = sleep
        self.ratio = 0           # fan percent reported by the firmware
        self.manual_fan = False
        self.temp_ok = True
        self.hashing = True
        self.accepted = 0
        self.booted_at = time.time()
        self._share_clock = time.time()
        self.down_until = 0.0

    def _tick(self) -> None:
        now = time.time()
        if self.hashing and self.mode == 1:
            gained = int((now - self._share_clock) / self.share_interval)
            if gained:
                self.accepted += gained
                self._share_clock += gained * self.share_interval
        else:
            self._share_clock = now

    def _status(self) -> dict:
        self._tick()
        up = int(time.time() - self.booted_at)
        hashing = self.hashing and self.mode == 1
        rpm = int(self.ratio * 60) if self.ratio else 0
        return {
            "fancount": str(self.fans), "online": True, "softver1": self.firmware,
            "mac": "00:00:5e:00:53:01", "host": "ICERIVER", "unit": "G",
            "rtpow": "400G" if hashing else "0G", "avgpow": "395G" if hashing else "0G",
            "runtime": f"00:{up // 3600:02d}:{up % 3600 // 60:02d}:{up % 60:02d}",
            "powstate": hashing, "netstate": True, "fanstate": True, "tempstate": self.temp_ok,
            "fans": [rpm] * self.fans + [0] * (4 - self.fans),
            "pools": [{**p, "connect": 1.0 if (i == 0 and self.mode == 1) else -1.0,
                       "accepted": float(self.accepted if i == 0 else 0), "rejected": 0.0}
                      for i, p in enumerate(self.pools)],
            "boards": [{"no": float(n + 1), "chipnum": 18.0, "freq": 875.0,
                        "rtpow": "400.00G" if hashing else "0.00G", "avgpow": "395.00G" if hashing else "0.00G",
                        "intmp": 60.0 if hashing else 35.0, "outtmp": 42.0 if hashing else 33.0,
                        "state": True} for n in range(self.boards)],
        }

    def handle(self, path: str, form: dict) -> tuple[int, dict | None]:
        with self.lock:
            self.requests.append((path, dict(form)))
            if time.time() < self.down_until:
                return 503, None
            post = int(form.get("post", "-1"))
            if path == "/user/userpanel":
                if post == 4:
                    return 200, {"error": 0, "data": self._status(), "message": ""}
                if post == 3:
                    self._boot()
                    self.down_until = time.time() + self.reboot_seconds
                    self.booted_at = self.down_until
                    return 200, {"error": 0, "data": {}, "message": "(null)"}
                if post == 2:
                    self.factory_resets += 1
                    return 200, {"error": 0, "data": {}, "message": ""}
            if path == "/user/machineconfig":
                if post == 1:
                    return 200, {"error": 0, "data": {"pools": self.pools, "mode": self.mode,
                                                      "ratio": self.ratio, "locate": 0}, "message": ""}
                if post == 3:
                    if form.get("fancontrol") == "on":
                        # fan only; the work mode in the same request is ignored
                        self.ratio = int(form.get("fanratio", "100"))
                        self.manual_fan = True
                    else:
                        self._tick()
                        new_mode = 1 if form.get("fanmode") == "normal" else 0
                        if new_mode == 0:
                            self.temp_ok = False          # latches until reboot
                            self.hashing = False
                            self.ratio = 29
                        elif self.mode == 0:
                            self.hashing = not self.wake_needs_reboot
                            self.ratio = 100
                        self.mode = new_mode
                        self.manual_fan = False
                    return 200, {"error": 0, "data": {}, "message": ""}
            return 200, {"error": -1, "data": {}, "message": "unknown"}

    # -- server ------------------------------------------------------------
    def start(self, port: int = 0, host: str = "127.0.0.1") -> str:
        miner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                return

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
                status, doc = miner.handle(self.path.split("?")[0], form)
                data = json.dumps(doc).encode() if doc is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"{host}:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Simulated stock IceRiver miner")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--boards", type=int, default=1)
    ap.add_argument("--fans", type=int, default=2)
    args = ap.parse_args()
    miner = FakeMiner(boards=args.boards, fans=args.fans, reboot_seconds=20, share_interval=5)
    addr = miner.start(args.port)
    print(f"simulated miner listening on http://{addr} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        miner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
