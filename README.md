# riverwarden

Fleet control for **stock-firmware IceRiver miners**: read status from every unit, set fan speed, put units to sleep and wake them again, by hand or from a room-temperature thermostat. It comes with a CLI, a REST API and an MCP server, and every write is checked against what the miner reports back.

It talks to the same HTTP endpoints as the miner's built-in web console. Nothing is flashed, no custom firmware is needed, and there is no fee.

> **Unofficial.** This project is not affiliated with or endorsed by IceRiver. The stock firmware has no documented API; everything here comes from observing the web console. Use it at your own risk.

## Status

Early (alpha). Everything described below is in the repository: the client, the CLI, a room-temperature thermostat with pluggable temperature sources, a heartbeat for push-style monitors, a REST API, an MCP server and a miner simulator. Only the AL0 has been run against real hardware so far (see the table).

### Verified hardware

| Model | Firmware | Read | Fan | Sleep | Wake | Reported by |
|---|---|---|---|---|---|---|
| AL0 | `ICM168_04_02_1005_10106` | yes | yes | yes | yes | author, 5 units; `selftest --write` passed |

Other IceRiver models (KS and AL series) use the same web console, and the parser handles several hash boards and up to four fans, but **they have not been tested**. If you run `riverwarden selftest` on your model, please open an issue with the report it prints. Custom firmware that replaces the web console (for example overclocking builds that move the UI to HTTPS) is not supported: riverwarden stops with an error when a device does not answer like the stock console. That path has not been tested against real custom firmware.

## Why

The stock web console manages one miner at a time and has no scheduler. Common workarounds each have a catch:

- **Custom firmware** adds an API, but it means flashing third-party binaries and usually a dev fee.
- **Cutting the pool connection** does not help: on the AL0 the hash board kept the same temperature for ten minutes with no pool, so it kept drawing power.
- **Smart plugs** cut power the hard way, and at least one common plug could not start an AL0 at all.

Sleep mode is the firmware's own way to stop hashing. It works over the stock console API once you know its quirks (see [docs/firmware-notes.md](docs/firmware-notes.md)).

## Safety model

- **An allow-list of requests.** On the stock console, factory reset is `post=2` and reboot is `post=3` on the same URL. riverwarden will only ever send four request types (read status, read settings, save fan/mode, reboot) and refuses everything else before it reaches the network.
- **Dry run by default.** Commands that change a miner do nothing without `--apply`.
- **Read-back after every write.** Sleep is confirmed by reading the mode; wake is confirmed by accepted shares increasing, not by "pool connected".
- **A fan floor.** Manual fan settings below 20 % are rejected (configurable), because 0 % stops the fans.
- **One writer at a time.** A lock file keeps a scheduler and a manual command from writing to the same fleet together.

### Secure your network first

The stock firmware does **not** enforce its own login: the check happens in the browser. Anything that can reach port 80 of a miner can change its pools, reboot it or reset it, and port 4111 answers status queries with no authentication at all. Put miners on their own VLAN or behind a firewall rule that only lets your management host in. Changing the console password does not protect the miner.

## Install

```bash
pip install git+https://github.com/Wisteria-Corp/riverwarden
```

Python 3.10 or newer. The only dependency is PyYAML.

## Quick start

Try it without hardware first. In one terminal:

```bash
python -m riverwarden.simulator --port 8081
```

In another, create `config.yaml`:

```yaml
units:
  - name: rig1
    host: "127.0.0.1:8081"
```

```bash
riverwarden status
riverwarden sleep rig1            # dry run: prints what it would do
riverwarden sleep rig1 --apply
riverwarden wake rig1 --apply
```

For real miners, copy [config.example.yaml](config.example.yaml) and put your miners' addresses in it.

## Commands

| Command | What it does |
|---|---|
| `status [--json]` | Mode, hashrate, board temperature, fan, accepted shares and uptime for every unit |
| `fan <unit> <percent> --apply` | Manual fan percentage. Lost on reboot unless `fan_percent` is set for the unit |
| `sleep <unit> --apply` | Stop hashing and wait until the miner reports sleep mode |
| `wake <unit> --apply` | Back to normal mode, reboot, wait for accepted shares, re-apply the unit's manual fan |
| `reboot <unit> --apply` | Reboot |
| `thermostat [--apply]` | Evaluate the thermostat once: sleep or wake at most one unit |
| `tick [--apply]` | Scheduled run: record a snapshot, run the thermostat, send the heartbeat |
| `collect` | Record a snapshot to `data_dir/status.jsonl` |
| `hold <minutes>` / `resume` | Pause the thermostat (for example after a manual change) and end the pause |
| `enable` / `disable` | Turn the thermostat on or off without editing the configuration |
| `api` | Serve the REST API |
| `mcp` | Serve the MCP server on stdio |
| `selftest <host> [--write]` | Compatibility report for one miner. `--write` also tests fan, sleep and wake (the miner is rebooted) |

`<unit>` can be the name from the config, the address, or the last octet of the address.

## Thermostat

The miners are the heater. Add a `thermostat` section to the configuration and run `riverwarden tick --apply` every few minutes from cron or a systemd timer:

```yaml
thermostat:
  low_c: 22.0            # colder than this: wake one unit
  high_c: 24.0           # warmer than this: put one unit to sleep
  min_running: 1
  max_running: 5
  sleep_order: [rig1, rig2, rig3]   # first sleeps first; waking goes in reverse
  sensor: {type: file, path: /run/room_temp}
```

- **One unit per run**, and at least `min_minutes_between_changes` (default 30) between changes, so a slow room does not make the fleet flap.
- **No temperature, no action.** If the sensor cannot be read, or its value is older than `max_sensor_age_minutes` (default 45), nothing changes.
- **Unreachable units are left alone**, and units missing from `sleep_order` are never touched.
- `riverwarden thermostat` without `--apply` shows what it would do right now.

A cron entry is enough to run it:

```
*/10 * * * * cd /etc/riverwarden && RIVERWARDEN_PUSH_URL=... riverwarden tick --apply >> /var/log/riverwarden.log 2>&1
```

Ten minutes is a reasonable interval. A wake can take a few minutes, and the write lock makes an overlapping run skip the thermostat instead of colliding with it.

### Temperature sources

| `type` | Reads | Keys |
|---|---|---|
| `file` | A number in a file, or JSON with `json_path`. The file's modification time is the reading's age | `path` |
| `command` | The first number a command prints | `command` |
| `http` | A URL returning a number or JSON with `json_path` (Home Assistant's REST API, for example) | `url`, `header_env` |
| `switchbot` | A SwitchBot meter through the cloud API | `device_id`, `token_env`, `secret_env` |

Every type accepts `unit: F` and `scale`. Secrets stay out of the configuration: `header_env` maps a header name to the *name* of an environment variable, and the SwitchBot token is read from `SWITCHBOT_TOKEN` / `SWITCHBOT_SECRET` unless other variable names are given.

### Heartbeat

```yaml
monitor:
  push_url_env: RIVERWARDEN_PUSH_URL   # the URL is read from this environment variable
  style: kuma                           # or: healthchecks
```

After each `tick --apply` a heartbeat goes to a push monitor (Uptime Kuma push monitors and healthchecks.io are supported). It reports the result of the run, not just that it ran: a unit unreachable on two runs in a row, a board at or above `board_temp_warn_c`, a missing or stale room temperature, or a failed sleep or wake make it a failure. If the scheduler dies, no heartbeat arrives and the monitor's own timeout raises the alarm.

## REST API

```yaml
api:
  bind: "127.0.0.1"
  port: 8532
```

```bash
riverwarden api
curl -s http://127.0.0.1:8532/health
curl -s -H "Authorization: Bearer $(cat riverwarden-data/api_token)" http://127.0.0.1:8532/api/v1/status
```

| Method | Path | |
|---|---|---|
| GET | `/health` | Liveness, no token needed |
| GET | `/api/v1/status` | Every unit and the room temperature (`?cached=1`: the last recorded snapshot) |
| GET | `/api/v1/units/{unit}` | One unit |
| GET | `/api/v1/units/{unit}/chips` | Per-chip temperature and voltage |
| GET | `/api/v1/history?hours=6&unit=` | Recorded trend, up to 72 hours |
| GET | `/api/v1/thermostat` | Thresholds, state, and what it would do right now |
| POST | `/api/v1/units/{unit}/fan` | `{"percent": 45}` |
| POST | `/api/v1/units/{unit}/sleep` | Optional `{"hold_minutes": 120}` |
| POST | `/api/v1/units/{unit}/wake` | Runs in the background and answers 202 |
| POST | `/api/v1/units/{unit}/reboot` | |
| POST | `/api/v1/thermostat` | `{"action": "enable"}`, `disable`, `hold`, `resume`, `set`, `reset` |

- **Token.** Everything except `/health` needs `Authorization: Bearer <token>`. The token is generated on first start and kept in `<data_dir>/api_token`.
- **This token is the only lock.** The miners have no authentication of their own, so anyone who can reach the API with the token can operate them. Keep `bind` on `127.0.0.1` unless you need more, and firewall the port when you change it. There is no TLS; put a reverse proxy in front if the network is not yours.
- **Manual changes pause the thermostat** for `hold_minutes` (default 120) so it does not undo them on the next run.
- **Waking respects `max_running`.** A wake that would exceed the thermostat's ceiling is refused with 409, as is any write while another write is in progress.

## MCP server

`riverwarden mcp` speaks the Model Context Protocol on stdio, so an AI assistant can read and operate the fleet. Example client entry:

```json
{
  "mcpServers": {
    "riverwarden": {"command": "riverwarden", "args": ["-c", "/path/to/config.yaml", "mcp"]}
  }
}
```

| Tool | |
|---|---|
| `fleet_status` | Every unit, the room temperature and the thermostat |
| `fleet_history` | Trend over the last N hours and the thermostat's changes |
| `unit_chips` | Per-chip temperature and voltage |
| `unit_set_fan`, `unit_sleep`, `unit_wake` | Change one unit |
| `thermostat` | Show, enable, disable, hold, resume, set thresholds, reset |

- **Writes need `confirm: true`.** Without it a tool only says what it would do.
- **Remote mode.** When the configuration has `api.url`, every tool goes through the REST API instead of talking to the miners. Use this when a scheduler and the API run on a server: one process writes to the miners, and the assistant's machine only needs the configuration and the token file (`RIVERWARDEN_API_TOKEN_FILE`). Set `RIVERWARDEN_MCP_LOCAL=1` to force direct access.
- The thermostat's ceiling (`max_running`) can be lowered through the tool but not raised above the value in the configuration.

## Reporting your model

```bash
riverwarden selftest rig1.example            # read-only
riverwarden selftest rig1.example --write    # fan, sleep, wake; takes about 5 minutes
```

The output is a Markdown table. Paste it into a new issue together with the model name; that is how the table of verified hardware grows. The write test changes the fan, puts the miner to sleep, wakes it and reboots it once, so run it when a few minutes without hashing do not matter.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests run against `riverwarden.simulator`, which reproduces the firmware behaviour described in the notes. No hardware is touched.

## License

MIT. See [LICENSE](LICENSE).
