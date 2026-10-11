# Bosch Air 2000i ring-light automation — project handoff

## Goal

Control the Bosch Air 2000i air purifier's LED air-quality ring independently of its fan/power state, on a schedule (initial target: **off at 22:00, on at 08:00**). The Bosch HomeCom Easy iPhone app already offers manual ring control, but the user cannot schedule it. The system should run on Linux **without Home Assistant initially** and later be adaptable to Home Assistant.

## User environment

- Air purifier: **Bosch Air 2000i** (Matter capable).
- Phone: **iPhone**, running Bosch HomeCom Easy.
- Development/runtime machine: **Linux**; distribution not yet specified.
- Home Assistant: planned, but not yet ready.

## Status (11 October 2026)

**Phase 1 verified on 11 October 2026: authentication works, HomeCom gateways: 0, Bacon devices: 1.** The Air 2000i is on the Bacon/Matter MQTT-shadow backend. **Phase 2 done: the ring is shadow field `lightEnabled` (bool); the app changes only that field in `desired` and the device echoes it in `reported`.** Phase 3 controller: `bosch_air.py` (`login` once, then `light on|off|status`; stores the rotating refresh token in `~/.config/bosch-air/token.json`, mode 0600; it has not been run against the device yet). Phase 2 tool: `python3 snapshot.py` (needs `python3-paho-mqtt`) logs in once, captures ring on/off/on shadows and prints fields that flip only with the ring. Earlier note: No authentication results, device discovery response, MQTT state, or identified ring-light command have been supplied.

The current probe checks both HomeCom gateway and Bacon device discovery. It uses only Python's standard library; run `python3 discover.py`. It prints counts and device types only. The older code block below is retained as a historical draft and should not be run as-is.

There is **no confirmed publicly documented command for this particular model's ring light**. A promising hypothesis is that Bosch's HomeCom Easy cloud infrastructure exposes device discovery and MQTT device-shadow state through APIs implemented by the community `homecom_alt` library. Although related Bosch devices have been investigated in this ecosystem, applicability to the Air 2000i is **unverified**.

## Relevant starting points

- Community Python library: https://github.com/serbanb11/homecom_alt
- Community Home Assistant integration: https://github.com/serbanb11/bosch-homecom-hass
- Bosch Home Comfort API docs: https://github.com/bosch-home-comfort/api-docs

Check the current library version, README, implementation, and function signatures **before executing** the draft below. This handoff records the previously proposed proof-of-concept; it has **not been run** or validated against the installed library.

## Phase 1: Read-only discovery on Linux

### 1. Create isolated Python environment (Debian/Ubuntu/Raspberry Pi OS)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git ca-certificates
mkdir -p ~/bosch-air-api
cd ~/bosch-air-api
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install homecom_alt aiohttp
python -c "import homecom_alt; print('Bosch library installed')"
```

### 2. Validate actual library API before running the draft

Inspect installed exports and signatures, without sending any commands to the purifier:

```bash
cd ~/bosch-air-api
source .venv/bin/activate
python - <<'PY'
import inspect
import homecom_alt
print('Package:', homecom_alt.__file__)
for name in ('HomeComAlt','ConnectionOptions','BaconMqttClient','async_get_bacon_devices','decode_jwt_sub'):
    obj = getattr(homecom_alt, name, None)
    print(name, 'available:', obj is not None)
    if obj is not None:
        try: print('  signature:', inspect.signature(obj))
        except (TypeError, ValueError): pass
PY
```

Also read the upstream source for `ConnectionOptions`, `HomeComAlt.create`, OAuth constants, `async_get_bacon_devices`, `BaconMqttClient.async_connect`, `.async_get_state`, and `.async_disconnect`. The initial draft assumes their names and parameter conventions, which may differ in your installed release.

### 3. Draft read-only OAuth, device-discovery, and state probe

Save as `~/bosch-air-api/discover.py`. **Review against the checked-out upstream library before attempting live authentication**, especially the OAuth PKCE verifier, auth-code exchange and MQTT method signatures.

```python
import asyncio
import base64
import hashlib
import json
import secrets
import sys
from urllib.parse import urlencode, urlparse, parse_qs

from aiohttp import ClientSession
from homecom_alt import (
    HomeComAlt,
    ConnectionOptions,
    BaconMqttClient,
    async_get_bacon_devices,
    decode_jwt_sub,
)
from homecom_alt.const import (
    OAUTH_BROWSER_VERIFIER,
    OAUTH_DOMAIN,
    OAUTH_LOGIN,
    OAUTH_LOGIN_PARAMS,
)

LIGHT_WORDS = ("light", "led", "ring", "display", "brightness", "illumination")


def auth_url():
    verifier = OAUTH_BROWSER_VERIFIER
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    params = dict(OAUTH_LOGIN_PARAMS)
    params["code_challenge"] = challenge
    params["state"] = secrets.token_urlsafe(24)
    return OAUTH_DOMAIN + OAUTH_LOGIN + "?" + urlencode(params), params["state"]


def extract_code(url, expected_state):
    parsed = urlparse(url.strip())
    query = parse_qs(parsed.query)
    if parsed.scheme != "com.bosch.tt.dashtt.pointt":
        raise ValueError("Unexpected redirect scheme")
    if query.get("state", [None])[0] != expected_state:
        raise ValueError("OAuth state mismatch")
    if "error" in query:
        raise ValueError("Bosch login returned an error")
    codes = query.get("code", [])
    if len(codes) != 1:
        raise ValueError("Missing authorization code")
    return codes[0]


async def main():
    url, state = auth_url()
    print("\n1. Open the Bosch login URL in your Linux browser:\n")
    print(url)
    print("\n2. Sign in, then paste the complete redirected app URL locally.")
    redirect = input("Redirect URL: ").strip()
    code = extract_code(redirect, state)
    del redirect

    options = ConnectionOptions(code=code)
    async with ClientSession() as session:
        auth = await HomeComAlt.create(session, options, auth_provider=True)
        await auth.get_token()
        token = auth.token
        if not token:
            raise RuntimeError("No access token returned")
        print("\nAuthentication successful.")

        devices = await async_get_bacon_devices(session, token, region="euc1")
        print("Devices discovered:", len(devices))
        if not devices:
            print("No Bacon devices found; another backend may be in use.")
            return

        sub = decode_jwt_sub(token)
        if not sub:
            raise RuntimeError("Cannot read token subject")
        mqtt = BaconMqttClient()
        try:
            await mqtt.async_connect(token, sub)
            print("MQTT connection successful.")
            for number, device in enumerate(devices, 1):
                print(f"\nDevice {number}")
                serial = device["deviceId"]
                try:
                    shadow = await mqtt.async_get_state(serial)
                except Exception as exc:
                    print("Shadow read failed:", type(exc).__name__, str(exc)[:150])
                    continue
                state_data = shadow.get("state", shadow)
                if not isinstance(state_data, dict):
                    print("Unexpected shadow format")
                    continue
                for section in ("reported", "desired"):
                    values = state_data.get(section, {})
                    if not isinstance(values, dict):
                        continue
                    print(f"\n{section} property names:")
                    print(json.dumps(sorted(values.keys()), indent=2))
                    matches = {
                        key: value for key, value in values.items()
                        if any(word in key.lower() for word in LIGHT_WORDS)
                        and isinstance(value, (bool, int, float))
                    }
                    print(f"{section} light candidates:")
                    print(json.dumps(matches, indent=2))
        finally:
            await mqtt.async_disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print("\nFailed:", type(exc).__name__, str(exc)[:250], file=sys.stderr)
        sys.exit(1)
```

Run only after validating imports/signatures:

```bash
cd ~/bosch-air-api
source .venv/bin/activate
python discover.py
```

A Linux browser might not be able to handle the app-specific `com.bosch.tt.dashtt.pointt://` redirect. If this happens, recover its full URL from the browser locally. **Never send the redirect URL, OAuth code or tokens in chat.** OAuth authorization codes can expire or be single-use. The draft's use of a library-provided fixed PKCE verifier also requires checking against the current implementation.

## How to interpret Phase 1

| Result | Interpretation | Next task |
| --- | --- | --- |
| Authentication works, device found, shadow readable | Cloud protocol is a viable candidate | Collect ring-on and ring-off state snapshots |
| Authentication works, zero devices | This particular discovery endpoint returned none | Inspect account region and alternative Bosch APIs |
| Authentication works, MQTT rejected | Discovery and MQTT permissions/protocol may differ | Inspect client implementation and errors |
| Login/redirect/code exchange fails | OAuth assumptions or app URI handoff may be wrong | Correct against current upstream code |
| Shadow present, no obvious LED field | Light setting may be nested/encoded or outside shadow | Compare redacted snapshots; consider app network capture |

## Phase 2: Identify the light-ring field

After a successful read-only connection:

1. Save a **private** snapshot while the ring is **on**.
2. Turn the ring **off using the official iPhone app** while leaving other settings fixed.
3. Save a second snapshot.
4. Turn the ring on again, save a third snapshot.
5. Diff the JSON and identify fields that change consistently only with lighting.
6. If the device shadow remains identical, investigate alternative APIs or observe traffic from the iPhone app, starting with non-invasive network metadata. Avoid bypassing security controls.

Keep full snapshots locally, not in public issues/chat, until device identifiers and other sensitive values are removed. Device state might be nested under structures that a shallow top-level-key scan misses.

## Phase 3: Minimal write test, only after identifying control semantics

Test one confirmed LED-only command and check readback. Verify no fan speed, purifier power, timer, sleep mode or automation state changes. Add robust authentication/token refresh, retries, timeouts, logging and idempotent state changes. Never send guessed payloads to the device.

Proposed eventual CLI interface (not yet implemented):

```bash
python bosch_air.py light off
python bosch_air.py light on
python bosch_air.py light status
```

## Phase 4: Scheduling

Once commands are proven and reliable, use cron or systemd timers, with the Linux machine staying on. Example *future* cron rules (not functional until the controller exists):

```cron
0 22 * * * /usr/bin/python3 /opt/bosch/bosch_air.py light off
0 8 * * * /usr/bin/python3 /opt/bosch/bosch_air.py light on
```

Ensure scheduler timezone matches intended local time (your local timezone) and handle daylight-saving-time transitions; prefer a persistent systemd timer if missed runs matter.

## Phase 5: Home Assistant

Expose LED control as a switch entity after the Python tool works, ideally with readback. An initial command-line switch or MQTT bridge could be used prior to building a native integration. Matter's default exposed clusters may not include the proprietary light-ring control.

## Safety/privacy and research boundaries

- Work only with the user's own purifier and account.
- Start strictly read-only; don't send unvalidated shadow update or control messages.
- Do not request or share passwords, auth codes, full redirect URLs, bearer/refresh tokens, full serial numbers or Matter setup codes.
- Store credentials in restrictive local files or a secure secret store; avoid shell history and debug logs.
- Public code and API behavior can change. Verify live dependencies; don't treat guessed property names or illustrative JSON as real.

## Recommended next interaction

Ask the user to run `discover.py` on an internet-connected machine and share only its sanitized output. If the installed library differs from the upstream API checked for this probe, inspect its signatures before changing the script. Then decide whether either discovery path is supported by the Air 2000i.
