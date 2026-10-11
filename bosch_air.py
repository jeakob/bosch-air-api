"""Bosch Air 2000i ring-light control via the Bacon device shadow field lightEnabled.

    python3 bosch_air.py login                 # once, interactive browser login
    python3 bosch_air.py light on|off|status   # non-interactive, cron-safe
"""

import json
import os
import sys
import time
from urllib.parse import urlencode

from discover import CLIENT_ID, login, request_json
from snapshot import AGENT, DEVICES, connect, jwt_sub, read_shadow

TOKEN_URL = "https://singlekey-id.com/auth/connect/token"
TOKEN_FILE = os.environ.get("BOSCH_AIR_TOKEN_FILE") or os.path.expanduser("~/.config/bosch-air/token.json")
CONFIRM_SECONDS = 30


def save_refresh_token(token_response):
    os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
    tmp = TOKEN_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"refresh_token": token_response["refresh_token"]}, f)
    os.replace(tmp, TOKEN_FILE)  # atomic: a crash never leaves a half-written token


def access_token():
    try:
        with open(TOKEN_FILE) as f:
            refresh = json.load(f)["refresh_token"]
    except FileNotFoundError:
        raise RuntimeError("No saved login; log in via the dashboard or: python3 bosch_air.py login") from None
    response = request_json(TOKEN_URL, data=urlencode(
        {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh}
    ).encode())
    save_refresh_token(response)  # refresh tokens rotate; the old one is now spent
    return response["access_token"]


def light(action):
    token = access_token()
    sub = jwt_sub(token)
    serials = request_json(DEVICES, token=token, user_agent=AGENT)
    if len(serials) != 1:
        raise RuntimeError(f"Expected 1 Bacon device, found {len(serials)}")
    serial = serials[0]
    client, events = connect(token, sub)
    try:
        def reported():
            return read_shadow(client, events, sub, serial)["state"]["reported"].get("lightEnabled")

        current = reported()
        if action == "status":
            print("on" if current else "off")
            return
        want = action == "on"
        if current == want:
            print(f"light already {action}")
            return
        client.publish(
            f"users/{sub}/devices/{serial}/shadows/state/update",
            json.dumps({"state": {"desired": {"lightEnabled": want}}}),
        )
        deadline = time.monotonic() + CONFIRM_SECONDS
        while time.monotonic() < deadline:
            time.sleep(2)
            if reported() == want:
                print(f"light {action}")
                return
        raise RuntimeError(f"Device did not confirm light {action} within {CONFIRM_SECONDS} s")
    finally:
        client.loop_stop()
        client.disconnect()


def main(args):
    if args == ["login"]:
        save_refresh_token(login())
        print(f"Saved login to {TOKEN_FILE}")
    elif len(args) == 2 and args[0] == "light" and args[1] in ("on", "off", "status"):
        light(args[1])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as exc:
        print(f"Failed: {type(exc).__name__}: {str(exc)[:150]}", file=sys.stderr)
        raise SystemExit(1) from None
