# bosch-air-api

Turn the LED air-quality ring of a **Bosch Air 2000i** air purifier on and off from Linux, independently of the fan, so it can be scheduled (for example off at 22:00, on at 08:00). The Bosch HomeCom Easy app can toggle the ring by hand but cannot schedule it.

Unofficial, not affiliated with Bosch. Use it only with your own account and device.

## Requirements

- Python 3.10+ (tested on 3.13, Debian 13)
- `paho-mqtt` 2.x: `sudo apt install python3-paho-mqtt` or `pip install "paho-mqtt>=2"`
- A Bosch SingleKey ID account with the purifier added in HomeCom Easy

`discover.py` needs only the standard library.

## Usage

```bash
python3 bosch_air.py login          # once: interactive browser login, saves a refresh token
python3 bosch_air.py light status   # prints on / off
python3 bosch_air.py light off
python3 bosch_air.py light on
```

`light on|off` does nothing if the ring is already in that state. Otherwise it sets only `lightEnabled` and waits up to 30 s for the purifier to confirm. It exits non-zero on any failure.

### Scheduling (cron)

```cron
0 22 * * * cd /path/to/bosch-air-api && /usr/bin/python3 bosch_air.py light off >> /var/log/bosch-air.log 2>&1
0 8  * * * cd /path/to/bosch-air-api && /usr/bin/python3 bosch_air.py light on  >> /var/log/bosch-air.log 2>&1
```

Cron uses the system timezone; check it with `timedatectl`.

### Logging in

SingleKey ID redirects to the app scheme `com.bosch.tt.dashtt.pointt://app/login?code=…&state=…`, which a desktop browser cannot open. To complete the login:

1. Run the command and open the printed URL in a browser.
2. Open Developer Tools, go to **Network** or **Console**, and enable **Preserve log**.
3. Sign in and click **Continue**. The browser reports that it failed to launch the `com.bosch…://` URL.
4. Copy that full URL (or the whole console message) and paste it at the hidden prompt.

Authorization codes are single-use and expire quickly. If a step fails, run the command again.

### Token storage

`login` stores only the refresh token, in `~/.config/bosch-air/token.json` (mode 0600, written atomically). Refresh tokens **rotate**: each run uses the stored token and saves the new one. If a run reports `No saved login` or HTTP 400 from the token endpoint, run `login` again.

## Dashboard (Docker)

A small web dashboard and scheduler in one container. It keeps a persistent MQTT session to the purifier and checks every minute whether the ring should be on:

- **Time windows:** the ring is on while any window is active, whatever the air quality, unless the window is marked **Only if air not good**; then it also needs air quality outside the good range. Start and end can be `HH:MM`, `sunrise`, `sunset`, or an offset such as `sunset+30` or `sunrise-15`. A window whose end is earlier than its start runs past midnight.
- **Air quality:** choose a sensor field and its good limit. Optionally, the ring is also on *outside* the windows while air quality is outside the good range. The purifier pushes sensor readings on its own schedule (often every 30 minutes); the dashboard lists the fields it publishes, and you choose one plus its "good" limit (a number means good is less than or equal to it; text means good equals it). Without a reading from the last 2 hours, air counts as good.
- **Air quality history:** every sensor reading the purifier publishes is stored in `/data/history.json` and drawn as a chart (24 hours, 7 days, or everything kept), with the good limit marked and a table view. Readings are kept for 7 days by default; set 1 to 365 days on the dashboard.
- **Modes:** `auto` (rules), `on`, `off`, and `paused` (never touches the ring). In `auto`, `on` and `off`, a change made in the Bosch app is undone within a minute.

Sunrise and sunset are calculated locally from the latitude and longitude you enter, with no external service (accurate to about 2 minutes; polar day and night have no sunrise, so such windows are skipped).

### Run it (Raspberry Pi or any Docker host)

The image is published for `linux/arm64` (64-bit Raspberry Pi OS), `linux/arm/v7` (32-bit) and `linux/amd64` as `ghcr.io/jeakob/bosch-air-api:latest`. It is Alpine-based (about 100 MB) and uses roughly 15–35 MB of RAM.

1. Copy [`compose.yaml`](compose.yaml) to the Pi.
2. Create `stack.env` next to it (see [`stack.env.example`](stack.env.example)):
   ```
   DASHBOARD_PASSWORD=some-long-password
   ```
   With Portainer, add `DASHBOARD_PASSWORD` under the stack's **Environment variables**; Portainer writes it to `stack.env`.
3. `docker compose up -d`
4. Open `http://<pi-address>:8080` and sign in with any username and that password.
5. Click **Start login** and complete the Bosch sign-in (see [Logging in](#logging-in)). Then set the location, time windows and mode.

The dashboard defaults to a generic central London location and the Europe/London time zone; change both in the dashboard. The container keeps its log timestamps in UTC.

Moving an existing login: copy `token.json` into the volume, then delete the old copy (refresh tokens rotate, so two copies log each other out):
`docker exec -i -u app bosch-air sh -c 'umask 077; cat > /data/token.json' < token.json`

Build it yourself: `docker build -t bosch-air-dashboard .`

## How it works

The Air 2000i is commissioned over Matter and lives on Bosch's **"Bacon"** cloud backend, not the classic HomeCom "pointt" gateway API (that API lists 0 gateways for this device).

| Step | Detail |
| --- | --- |
| OAuth | SingleKey ID, authorization code + PKCE (S256); client ID and scopes match the HomeCom Easy app |
| Device list | `GET https://claiming.euc1.bacon.bosch-tt-cw.com/v1/users/self/devices` with a bearer token; returns a list of device serials |
| Broker | `wss://broker.euc1.bacon.bosch-tt-cw.com:443/mqtt`, MQTT v5 over WebSocket; `Authorization: Bearer` and `User-Agent` upgrade headers |
| MQTT login | Client ID: 64 lowercase hex chars; username: JWT `sub` claim; password: access token |
| Read state | Publish empty payload to `users/{sub}/devices/{serial}/shadows/state/get` and read the reply on `…/get/accepted` |
| Write | Publish `{"state":{"desired":{"lightEnabled":false}}}` to `…/shadows/state/update` |

The shadow follows the AWS IoT pattern: `desired` (what the app asked for), `reported` (what the device confirms), and `delta` (desired fields not yet reported).

### Finding the ring field

`snapshot.py` reads the shadow three times (ring on, off, on again; toggled in the official app) and lists fields that are equal in both "on" captures but different in "off". This filters out timestamps and version counters. The result:

```
state.desired.lightEnabled: true -> false
state.delta.lightEnabled:   null -> false
```

The app changes only `lightEnabled` in `desired`, and the device then echoes it in `reported.lightEnabled`.

## Files

| File | Purpose |
| --- | --- |
| `app.py`, `index.html` | Dashboard and scheduler (windows, sunrise/sunset, air-quality rule) |
| `Dockerfile`, `compose.yaml`, `stack.env.example` | Container image and deployment |
| `bosch_air.py` | Controller: `login`, `light on/off/status` |
| `discover.py` | Read-only OAuth login and device discovery (pointt gateways + Bacon devices); prints counts only |
| `snapshot.py` | Read-only Phase 2 tool: captures shadows and diffs them; writes private `snapshots.json` |
| `test_*.py` | Unit tests: `python3 -m unittest` |
| `examples/` | Worked example from a real run: redacted outputs and shadow snapshots |
| `docs/research-log.md` | Original plan and research notes, including an early draft that should not be run |

## Privacy

Never commit or share: `snapshots.json`, `~/.config/bosch-air/token.json`, redirect URLs, authorization codes, access or refresh tokens, device serials, or Matter setup codes. `.gitignore` excludes the local files. The scripts print counts, field names, and values, never serials or tokens.

## Status and next steps

- [x] Login and Bacon device discovery
- [x] Ring field identified (`lightEnabled`)
- [x] First live `light off` / `light on` run against the device
- [x] Dashboard with sunrise/sunset, time windows and air-quality rule
- [x] Confirm the air-quality sensor field on a live device
- [ ] Home Assistant switch (e.g. `command_line` switch calling `bosch_air.py`)

## Credits

The protocol details come from [serbanb11/homecom_alt](https://github.com/serbanb11/homecom_alt) (`bacon.py`, v1.8.2).
