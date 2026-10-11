"""Read-only Phase 2: find the ring-light field by diffing Bacon device shadows.

Protocol per homecom_alt 1.8.2 bacon.py. Needs paho-mqtt >= 2 (apt install python3-paho-mqtt).
"""

import base64
import hashlib
import json
import os
import queue
import ssl

import paho.mqtt.client as mqtt

from discover import login, request_json

BROKER = "broker.euc1.bacon.bosch-tt-cw.com"
DEVICES = "https://claiming.euc1.bacon.bosch-tt-cw.com/v1/users/self/devices"
AGENT = "DashApp/4.0.0 (Android-Release)"
STEPS = ("Ring ON", "Ring OFF (change only the ring in the app)", "Ring ON again")


def flatten(value, prefix=""):
    if not isinstance(value, dict):
        return {prefix: value}
    out = {}
    for key, item in value.items():
        out.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    return out


def ring_candidates(on, off, on_again):
    """Fields equal in both ON snapshots but different in OFF. Timestamps and versions drop out."""
    a, b, c = flatten(on), flatten(off), flatten(on_again)
    return {k: (a.get(k), b.get(k)) for k in sorted(a.keys() | b.keys()) if a.get(k) == c.get(k) != b.get(k)}


def jwt_sub(token):
    part = token.split(".")[1]
    return str(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["sub"])


def connect(token, sub):
    events = queue.Queue()
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=hashlib.sha256(os.urandom(16)).hexdigest(),  # broker requires 64 hex chars
        protocol=mqtt.MQTTv5,
        transport="websockets",
        reconnect_on_failure=False,
    )
    client.ws_set_options(path="/mqtt", headers={"Authorization": f"Bearer {token}", "User-Agent": AGENT})
    client.username_pw_set(sub, token)
    client.tls_set_context(ssl.create_default_context())

    def on_connect(c, userdata, flags, rc, props=None):
        if rc.is_failure:
            events.put(("refused", str(rc)))
        else:
            c.subscribe(f"users/{sub}/#")

    client.on_connect = on_connect
    client.on_subscribe = lambda *args: events.put(("ready", None))
    client.on_message = lambda c, u, msg: events.put((msg.topic, msg.payload))
    client.connect(BROKER, 443, keepalive=60)
    client.loop_start()
    kind, detail = events.get(timeout=15)
    if kind != "ready":
        raise RuntimeError(f"MQTT connect refused: {detail}")
    return client, events


def read_shadow(client, events, sub, serial):
    base = f"users/{sub}/devices/{serial}/shadows/state/get"
    while not events.empty():
        events.get_nowait()
    client.publish(base, "")
    while True:
        topic, payload = events.get(timeout=10)
        if topic == base + "/accepted":
            return json.loads(payload)
        if topic == base + "/rejected":
            raise RuntimeError("Shadow read rejected")


def main():
    token = login()["access_token"]
    serials = request_json(DEVICES, token=token, user_agent=AGENT)
    sub = jwt_sub(token)
    client, events = connect(token, sub)
    print("MQTT: OK")
    snapshots = []
    try:
        for step in STEPS:
            input(f"\nSet: {step}. Wait ~10 s, then press Enter. ")
            snapshots.append([read_shadow(client, events, sub, s) for s in serials])
            print(f"  captured {len(flatten(snapshots[-1][0]))} fields")
    finally:
        client.loop_stop()
        client.disconnect()

    # Full shadows can contain identifiers: keep them private on disk.
    fd = os.open("snapshots.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(dict(zip(STEPS, snapshots)), f, indent=2)
    print("\nSaved private snapshots.json (do not share).")

    for number, (on, off, on_again) in enumerate(zip(*snapshots), 1):
        print(f"\nDevice {number} ring candidates (field: on -> off):")
        found = ring_candidates(on, off, on_again)
        for key, (was, now) in found.items():
            print(f"  {key}: {json.dumps(was)} -> {json.dumps(now)}")
        if not found:
            print("  none; the ring setting may not be in the shadow")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Snapshot failed: {type(exc).__name__}: {str(exc)[:150]}")
        raise SystemExit(1) from None
