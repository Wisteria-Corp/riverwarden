"""Room temperature sources for the thermostat.

A sensor is a small mapping in the configuration::

    sensor: {type: file, path: /run/room_temp}                 # a number, or JSON with json_path
    sensor: {type: command, command: "cat /sys/bus/w1/.../temperature", scale: 0.001}
    sensor: {type: http, url: "http://host/api/states/x", json_path: state, header_env: {Authorization: HA_AUTH}}
    sensor: {type: switchbot, device_id: "ABCDEF123456"}        # SwitchBot cloud API v1.1

Secrets are never written in the configuration: HTTP headers and the SwitchBot
token come from environment variables whose *names* are configured.

Optional keys for every type: ``unit`` (``"C"`` default, or ``"F"``) and
``scale`` (multiplier applied before unit conversion).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

SWITCHBOT_API = "https://api.switch-bot.com/v1.1"


class SensorError(Exception):
    """The room temperature could not be read."""


@dataclass
class Reading:
    temp_c: float
    age_s: float      # how old the value is; 0 for sources that are read live
    source: str


def _dig(doc, path: str):
    """Follow a dotted path through dicts and lists (``"body.sensors.0.value"``)."""
    cur = doc
    for part in str(path).split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def _number(value, spec: dict) -> float:
    try:
        x = float(str(value).strip().split()[0])
    except (ValueError, IndexError):
        raise SensorError(f"not a number: {str(value)[:40]!r}") from None
    x *= float(spec.get("scale", 1))
    if str(spec.get("unit", "C")).upper() == "F":
        x = (x - 32) * 5 / 9
    if not -40 <= x <= 80:
        raise SensorError(f"implausible room temperature: {x:.1f} C")
    return round(x, 2)


def _extract(text: str, spec: dict):
    return _dig(json.loads(text), spec["json_path"]) if spec.get("json_path") else text


def _read_file(spec: dict) -> Reading:
    path = Path(spec["path"])
    age = time.time() - path.stat().st_mtime
    return Reading(_number(_extract(path.read_text(encoding="utf-8"), spec), spec), max(age, 0.0), f"file:{path.name}")


def _read_command(spec: dict) -> Reading:
    cmd = spec["command"]
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
                       timeout=float(spec.get("timeout", 20)))
    if r.returncode != 0:
        raise SensorError(f"command exited with {r.returncode}: {r.stderr.strip()[:80]}")
    return Reading(_number(_extract(r.stdout, spec), spec), 0.0, "command")


def _read_http(spec: dict) -> Reading:
    headers = dict(spec.get("headers") or {})
    for name, env in (spec.get("header_env") or {}).items():
        if env not in os.environ:
            raise SensorError(f"environment variable {env} is not set")
        headers[name] = os.environ[env]
    req = urllib.request.Request(spec["url"], headers=headers)
    with urllib.request.urlopen(req, timeout=float(spec.get("timeout", 15))) as resp:
        text = resp.read().decode("utf-8", "replace")
    return Reading(_number(_extract(text, spec), spec), 0.0, "http")


def switchbot_headers(token: str, secret: str, t: str | None = None, nonce: str | None = None) -> dict:
    """Signed headers for the SwitchBot API v1.1."""
    t = t or str(int(time.time() * 1000))
    nonce = nonce or uuid.uuid4().hex
    sign = base64.b64encode(hmac.new(secret.encode(), (token + t + nonce).encode(), hashlib.sha256).digest()).decode()
    return {"Authorization": token, "sign": sign, "t": t, "nonce": nonce, "Content-Type": "application/json; charset=utf8"}


def _read_switchbot(spec: dict) -> Reading:
    token_env = spec.get("token_env", "SWITCHBOT_TOKEN")
    secret_env = spec.get("secret_env", "SWITCHBOT_SECRET")
    try:
        token, secret = os.environ[token_env], os.environ[secret_env]
    except KeyError as e:
        raise SensorError(f"environment variable {e.args[0]} is not set") from None
    req = urllib.request.Request(f"{SWITCHBOT_API}/devices/{spec['device_id']}/status",
                                 headers=switchbot_headers(token, secret))
    with urllib.request.urlopen(req, timeout=float(spec.get("timeout", 15))) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    body = doc.get("body") or {}
    if "temperature" not in body:
        raise SensorError(f"no temperature in the SwitchBot answer (statusCode {doc.get('statusCode')})")
    return Reading(_number(body["temperature"], spec), 0.0, "switchbot")


READERS = {"file": _read_file, "command": _read_command, "http": _read_http, "switchbot": _read_switchbot}


def read(spec: dict) -> Reading:
    """Read the sensor described by ``spec``. Every failure becomes :class:`SensorError`."""
    kind = (spec or {}).get("type")
    if kind not in READERS:
        raise SensorError(f"unknown sensor type {kind!r} (known: {', '.join(READERS)})")
    try:
        return READERS[kind](spec)
    except SensorError:
        raise
    except (OSError, KeyError, IndexError, ValueError, TypeError, subprocess.SubprocessError, urllib.error.URLError) as e:
        raise SensorError(f"{kind} sensor: {type(e).__name__}: {e}"[:200]) from None
