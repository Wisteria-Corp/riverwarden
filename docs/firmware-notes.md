# Stock firmware notes

Observations from real hardware. Everything below was measured on **IceRiver AL0, firmware `ICM168_04_02_1005_10106`** unless stated otherwise. Other models and versions may differ; reports are welcome.

## Web console API (port 80)

All requests are `POST` with a form body and a `post=<n>` field that selects the operation.

| URL | `post` | Operation | Used by riverwarden |
|---|---|---|---|
| `/user/userpanel` | 4 | Read status (hashrate, fans, boards, pools, uptime) | yes |
| `/user/userpanel` | 3 | Reboot | yes |
| `/user/userpanel` | 2 | **Factory reset** | never |
| `/user/userpanel` | 5 | Locate (LED) with `locate=0/1` | no |
| `/user/machineconfig` | 1 | Read settings: `pools`, `mode` (1 = normal), `ratio` (fan %) | yes |
| `/user/machineconfig` | 3 | Save fan and work mode | yes |
| `/user/machineconfig` | 2 | Save pools | no |
| `/user/ipconfig` | 1 / 2 | Read / save network settings | no |
| `/user/systemconfig` | 2 | Change the console password | no |
| `/user/firconfig` | 2 | Upload firmware | no |
| `/user/logconfig` | 3 / 2 | Current log / log history | no |

### The login is not enforced

The console's login page sets a `ctime=1` cookie and the JavaScript checks for it. The miner itself answers every request above without any credentials. Treat port 80 as an unauthenticated control interface.

### Fan and work mode are one form, with a catch

`/user/machineconfig` `post=3` carries `fanratio`, `fanmode` (`normal` or `sleep`), an optional `fancontrol=on` checkbox, and the three pool rows. The console always submits the pool rows with it; riverwarden does the same and has not tested leaving them out.

- With `fancontrol=on`, the fan percentage is applied and **the work mode in the same request is ignored**. Sending `fanmode=sleep` this way returns success and the miner keeps hashing.
- Without `fancontrol`, the work mode is applied and the fan goes back to firmware control. Right after switching to normal mode the fan reports 100 %.

So a manual fan speed and a mode change are two separate requests.

### Sleep mode

- After `fanmode=sleep` (no `fancontrol`) the settings read `mode: 0` within about 20 seconds.
- The hash board cooled from 58 °C to 35 °C in three minutes.
- Fans keep turning at 29 % (about 1,650 rpm) while asleep.
- The `tempstate` flag turns `false` once the board is cold (around 43 °C) and **stays `false` after waking** even though hashing resumes normally. Only a reboot sets it back to `true`. Whether the flag affects anything besides the console's status light is unknown.
- Power draw while asleep has not been measured.

### Waking

- Writing `fanmode=normal` reconnects the pool. On two units hashing resumed with no reboot (accepted shares rising again after about 40 and 80 seconds). On another unit, where a fan write followed six seconds after the mode write, hashing did not resume for four and a half minutes and a reboot was needed.
- After a short sleep (under a minute, board still warm) the `tempstate` flag never turned `false`, so in that case a wake without reboot leaves nothing behind.
- After a reboot, accepted shares appear 40 to 50 seconds after the console is back.
- riverwarden's default is therefore: write normal mode, reboot, wait for accepted shares, then re-apply the manual fan. This also clears the `tempstate` flag.

### Fan behaviour

- A manual percentage is lost on reboot; the firmware takes over again.
- Under firmware control the fans stay off (reported 0 %, 0 rpm) until the board reaches about 85 °C, then run at 100 % (about 6,000 rpm) until it has cooled. Units with good airflow sat at 60 °C with the fans off; a manual 42 % (about 2,500 rpm) held a unit at 55 to 58 °C.

### Losing the pool does not reduce heat

With every pool blocked at the firewall for ten minutes, the miner showed the pool as disconnected and accepted shares stopped, but the board temperature stayed at 64 to 65 °C with the fan fixed at 42 %. Disconnecting a miner from its pool is not a way to make it idle.

### Pool slots

A unit configured with the same pool in slots 1 and 2 connected through slot 2 and reported slot 1 as not connected. In an earlier case, changing only slot 1 did not make the miner switch pools until slot 2 was changed as well. The cause is not known.

## Status port (4111)

A line-based JSON protocol: send `{"id":"<command>"}` followed by `\r\n`. No authentication. These commands answered; unknown commands get no reply.

| Command | Returns |
|---|---|
| `state` | `pow`, `net`, `fan`, `temp` flags |
| `info` | Model, firmware versions, fan count |
| `fan` | Fan speeds |
| `board` | Per-board chips, frequency, hashrate, temperatures |
| `boardpow` | Hashrate history |
| `getchipinfo` | Per-chip temperature, voltage and PLL |
| `getpool` | Pool rows with connection state and share counters |
| `getnet` | Network settings |
| `getlocate` | Locate LED state |

No write command was found on this port.
