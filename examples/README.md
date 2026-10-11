# Worked example: Bosch Air 2000i

A real run against one Bosch Air 2000i, with personal data removed. Login URLs, redirect URLs, codes, tokens, serials and the account ID never appear in the output. In `shadow-snapshots.json`, the timestamps were shifted to an arbitrary date; the gaps between them are unchanged.

Environment: Debian 13, Python 3.13.5, `python3-paho-mqtt` 2.1.0, HomeCom Easy app on iPhone.

## Step 1: Discovery (`discover.py`)

```console
$ python3 discover.py
Open this URL in the browser where you signed in to SingleKey:

https://singlekey-id.com/auth/connect/authorize?redirect_uri=...   <- your own, unique per run

Before clicking Continue, open browser Developer Tools > Network and enable Preserve log.
...
Paste the callback URL containing code= and state= (hidden):        <- paste the com.bosch.tt.dashtt.pointt://... URL
Authentication: OK
HomeCom gateways: 0
Bacon devices: 1
```

**Result:** the purifier is not on the classic HomeCom gateway API. It is on the Matter "Bacon" backend, which exposes an MQTT device shadow.

## Step 2: Find the ring field (`snapshot.py`)

Have the HomeCom Easy app open. At each prompt, change **only** the ring in the app, then wait about 10 s before pressing Enter so the purifier can confirm.

```console
$ sudo apt install python3-paho-mqtt
$ python3 snapshot.py
...                                       <- same login as step 1
Authentication: OK
MQTT: OK

Set: Ring ON. Wait ~10 s, then press Enter.
  captured 40 fields

Set: Ring OFF (change only the ring in the app). Wait ~10 s, then press Enter.
  captured 41 fields

Set: Ring ON again. Wait ~10 s, then press Enter.
  captured 40 fields

Saved private snapshots.json (do not share).

Device 1 ring candidates (field: on -> off):
  state.delta.lightEnabled: null -> false
  state.desired.lightEnabled: true -> false
```

[`shadow-snapshots.json`](shadow-snapshots.json) contains the three shadows from this run. Notes:

- The shadow holds the purifier's settings: `powerEnabled`, `opMode`, `fanSpeed`, `childLockEnabled`, `onTimer`, `offTimer`, `lightEnabled`, `displayEnabled`, `filterReset`.
- **`lightEnabled` is the ring.** Toggling the ring in the app changed only this field in `desired`.
- In the "Ring OFF" capture, `reported.lightEnabled` is still `true`, and `delta` shows the pending change. The capture was taken about 2 s after the toggle, before the purifier confirmed. In the "Ring ON again" capture, `metadata.reported.lightEnabled.timestamp` advances, which shows the device writes the field back. Waiting about 10 s avoids this race.
- `displayEnabled` is a separate setting that was not changed in this test.

## Step 3: Control (`bosch_air.py`)

> Not yet run against the device. The commands below show expected output; this section will be updated after the first live test.

```console
$ python3 bosch_air.py login      # same browser login, once
Authentication: OK
Saved login to /home/you/.config/bosch-air/token.json
$ python3 bosch_air.py light status
on
$ python3 bosch_air.py light off
light off
$ python3 bosch_air.py light off
light already off
```

When you run it, check that only the ring changes and the fan speed and mode do not.

## Sharing your own results

If you share output from your own device, check that it contains no serials (for example `86DM-…`), no `sub` / account UUID, no URLs containing `code=`, and no `eyJ…` tokens. The private `snapshots.json` written by `snapshot.py` is safe to share only after you check that it contains no such values and shift its timestamps.
