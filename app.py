"""Dashboard and scheduler for the Bosch Air 2000i ring light.

The ring is on while any time window is active, and outside the windows
(optionally) while air quality is outside the "good" range. Runs one persistent MQTT session
so pushed sensor readings are captured; re-evaluates every TICK_SECONDS.
"""

import base64
import csv
import hmac
import io
import json
import math
import os
import re
import threading
import time
import urllib.request
import zipfile
from collections import deque
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.environ.setdefault("BOSCH_AIR_TOKEN_FILE", os.path.join(DATA_DIR, "token.json"))

from bosch_air import access_token, save_refresh_token  # noqa: E402  (reads the env above)
from discover import RedirectError, authorization_code, exchange_code, request_json, start_login, REDIRECT_URI  # noqa: E402
from snapshot import AGENT, DEVICES, connect, jwt_claims  # noqa: E402

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
PORT = int(os.environ.get("PORT", "8080"))
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
TICK_SECONDS = 60
MAX_RETRY_SECONDS = 30 * 60
STALE_SECONDS = 2 * 3600  # older sensor readings count as unknown
EXPORT_SECONDS = 30 * 60  # the purifier uploads a reading to the cloud history every 30 minutes
OFFLINE_SECONDS = 2 * EXPORT_SECONDS + 10 * 60  # no new reading for this long: the purifier is offline
SILENT_SECONDS = 5 * 60  # no MQTT message at all (we get the shadow every tick): reconnect
CONFIRM_SECONDS = 3 * 60  # the purifier echoes a ring change within seconds when it is online
RESEND_SECONDS = 10 * 60  # repeat an unconfirmed ring change this often, not every tick
MODES = ("auto", "on", "off", "paused")
EXPORT_HOST = "prod-euc1-bosch-bacon-history-export.s3.eu-central-1.amazonaws.com"  # only download exports from here
DEFAULT_CONFIG = {
    "mode": "paused",  # auto: follow rules; on/off: force; paused: never touch the ring
    "timezone": "Europe/London",
    "latitude": 51.51,  # generic central London; change in the dashboard
    "longitude": -0.13,
    "windows": [{"start": "18:00", "end": "23:00", "air_only": False}],
    "air_quality": {"enabled": False, "field": "", "good": None},
    "history_days": 7,  # sensor readings kept for the dashboard chart
}
ENDPOINT = re.compile(r"^(?:([01]\d|2[0-3]):([0-5]\d)|(sunrise|sunset)([+-]\d{1,3})?)$")


# -- schedule ------------------------------------------------------------------

def sun_times(day, lat, lon):
    """UTC sunrise and sunset for a date (NOAA approximation, ~2 min). None during polar day/night."""
    g = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat_r = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(lat_r) * math.cos(decl)) - math.tan(lat_r) * math.tan(decl)
    if abs(cos_ha) > 1:
        return None
    ha = math.degrees(math.acos(cos_ha))
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return (midnight + timedelta(minutes=720 - 4 * (lon + ha) - eqtime),
            midnight + timedelta(minutes=720 - 4 * (lon - ha) - eqtime))


def resolve(spec, day, cfg):
    """Turn "HH:MM", "sunrise", "sunset-30" into an aware datetime on that local day, or None."""
    tz = ZoneInfo(cfg["timezone"])
    m = ENDPOINT.match(spec)
    if m.group(1):
        return datetime(day.year, day.month, day.day, int(m.group(1)), int(m.group(2)), tzinfo=tz)
    if cfg["latitude"] is None or cfg["longitude"] is None:
        return None
    sun = sun_times(day, cfg["latitude"], cfg["longitude"])
    if sun is None:
        return None
    moment = sun[0] if m.group(3) == "sunrise" else sun[1]
    return (moment + timedelta(minutes=int(m.group(4) or 0))).astimezone(tz)


def active_windows(cfg, now):
    """Windows containing now; a window whose end is before its start runs past midnight."""
    today = now.astimezone(ZoneInfo(cfg["timezone"])).date()
    active = []
    for window in cfg["windows"]:
        for day in (today - timedelta(days=1), today):
            start, end = resolve(window["start"], day, cfg), resolve(window["end"], day, cfg)
            if start is None or end is None:
                continue
            if end <= start:
                end = resolve(window["end"], day + timedelta(days=1), cfg)
                if end is None:
                    continue
            if start <= now < end:
                active.append(window)
                break
    return active


def in_window(cfg, now):
    return bool(active_windows(cfg, now))


def air_not_good(rule, reading):
    """True only for a reading outside the good range. No field chosen, or no recent reading, counts as good."""
    value = (reading or {}).get(rule["field"])
    good = rule["good"]
    if value is None or good is None:
        return False
    if isinstance(value, (int, float)) and isinstance(good, (int, float)):
        return value > good
    return str(value).lower() != str(good).lower()


def wanted_light(cfg, now, reading):
    """True/False for the ring, or None to leave it alone."""
    if cfg["mode"] == "paused":
        return None
    if cfg["mode"] in ("on", "off"):
        return cfg["mode"] == "on"
    bad = air_not_good(cfg["air_quality"], reading)
    # A window turns the ring on whatever the air, unless it is marked "only if air not good".
    if any(bad or not w["air_only"] for w in active_windows(cfg, now)):
        return True
    return cfg["air_quality"]["enabled"] and bad  # optional: also on outside windows when air is not good


# -- config --------------------------------------------------------------------

def validate(cfg):
    """Return a clean config or raise ValueError. This is the dashboard's trust boundary."""
    if cfg.get("mode") not in MODES:
        raise ValueError("mode must be one of " + ", ".join(MODES))
    try:
        ZoneInfo(str(cfg.get("timezone")))
    except Exception:
        raise ValueError("unknown timezone") from None
    lat, lon = cfg.get("latitude"), cfg.get("longitude")
    for name, value, limit in (("latitude", lat, 90), ("longitude", lon, 180)):
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or abs(value) > limit):
            raise ValueError(f"{name} must be empty or a number within ±{limit}")
    windows = cfg.get("windows")
    if not isinstance(windows, list) or len(windows) > 20:
        raise ValueError("windows must be a list of at most 20")
    for w in windows:
        for key in ("start", "end"):
            spec = w.get(key) if isinstance(w, dict) else None
            if not isinstance(spec, str) or not ENDPOINT.match(spec):
                raise ValueError(f"window {key} {spec!r}: use HH:MM, sunrise, sunset, sunset+30, sunrise-15")
            if spec.startswith("sun") and (lat is None or lon is None):
                raise ValueError("sunrise/sunset windows need latitude and longitude")
        if not isinstance(w.get("air_only", False), bool):
            raise ValueError("window air_only must be true or false")
    aq = cfg.get("air_quality")
    if not isinstance(aq, dict) or not isinstance(aq.get("enabled"), bool) or not isinstance(aq.get("field"), str):
        raise ValueError("air_quality needs enabled (bool) and field (text)")
    if (aq["enabled"] or any(w.get("air_only") for w in windows)) and not (aq["field"] and aq.get("good") is not None):
        raise ValueError("choose the air-quality field and its good limit")
    good = aq.get("good")
    if good is not None and (isinstance(good, bool) or not isinstance(good, (int, float, str))):
        raise ValueError("air_quality good must be a number or text")
    days = cfg.get("history_days", DEFAULT_CONFIG["history_days"])  # absent in configs saved before it existed
    # ponytail: whole history rewritten per reading; ~1.5 MB at 365 days of 30-min readings, fine on a Pi
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError("history_days must be a whole number from 1 to 365")
    return {
        "mode": cfg["mode"], "timezone": str(cfg["timezone"]), "latitude": lat, "longitude": lon,
        "windows": [{"start": w["start"], "end": w["end"], "air_only": w.get("air_only", False)} for w in windows],
        "air_quality": {"enabled": aq["enabled"], "field": aq["field"][:64], "good": good},
        "history_days": days,
    }


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return validate(json.load(f))
    except FileNotFoundError:
        return dict(DEFAULT_CONFIG)


def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def parse_stamp(value):
    """Epoch seconds from epoch seconds, epoch milliseconds or an ISO date (naive means UTC); None if neither."""
    try:
        n = float(value)
        return int(n / 1000 if n > 1e11 else n)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())


def parse_export(text):
    """Cloud history export CSV -> readings shaped like topics/sensor ones."""
    readings = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.split(" [")[0]: v for k, v in row.items() if k}  # "pm2_5 [µg/m3]" -> "pm2_5"
        reading = {"timestamp": parse_stamp(row.get("timestamp"))}
        if reading["timestamp"] is None:
            continue
        for field in ("airQuality", "pm2_5", "filterCapacity"):
            try:
                reading[field] = int(float(row[field]))
            except (KeyError, TypeError, ValueError):
                pass
        if "pm2_5" in reading:
            reading["pm2.5"] = reading["pm2_5"]
        readings.append(reading)
    return readings


def merge_history(history, items, now, keep_seconds):
    """Add sensor readings, newest timestamp wins per second; keep the last keep_seconds, oldest first."""
    by_ts = {r["timestamp"]: r for r in history}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("timestamp"), (int, float)):
            by_ts[item["timestamp"]] = item
    return [by_ts[t] for t in sorted(by_ts) if now - t <= keep_seconds]


def filter_estimate(history):
    """Project when filterCapacity reaches 0 % from a least-squares line over readings since the last
    filter change. None until it has dropped at least 2 % over at least 2 days (it reports whole %)."""
    pts = [(r["timestamp"], r["filterCapacity"]) for r in history if isinstance(r.get("filterCapacity"), (int, float))]
    for i in range(len(pts) - 1, 0, -1):
        if pts[i][1] > pts[i - 1][1]:  # went up: new filter, or reset
            pts = pts[i:]
            break
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 2 * 86400 or pts[0][1] - pts[-1][1] < 2:
        return None
    n = len(pts)
    mt, mv = sum(t for t, _ in pts) / n, sum(v for _, v in pts) / n
    slope = sum((t - mt) * (v - mv) for t, v in pts) / sum((t - mt) ** 2 for t, _ in pts)  # % per second
    if slope >= 0:
        return None
    now_t, now_v = pts[-1]
    return {"replace_at": int(now_t + now_v / -slope), "per_day": round(-slope * 86400, 2),
            "based_on_days": round((now_t - pts[0][0]) / 86400, 1)}


def load_history():
    try:
        with open(HISTORY_FILE) as f:
            return merge_history([], json.load(f), time.time(), 365 * 86400)  # tick() trims to the setting
    except (OSError, ValueError):
        return []


def save_history(history):
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(history, f)
    os.replace(tmp, HISTORY_FILE)


# -- device --------------------------------------------------------------------

class Device:
    """One persistent MQTT session; caches reported shadow, latest sensor and meta payloads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.client = None
        self.sub = self.serial = None
        self.expires = 0
        self.reported = {}
        self.sensor = self.meta = None
        self.sensor_at = None
        self.heard_at = self.message_at = time.time()  # arrival of the newest reading / of any MQTT message
        self.sent = None  # (want, epoch) of the last ring change sent and not yet confirmed
        self.seen = set()
        self.fields = {}  # shadow name -> reported field names, to find a publish-interval setting
        self.history = load_history()
        # So a restart doesn't count the export's replay of known readings as the purifier being heard from.
        self.sensor = self.history[-1] if self.history else None
        self.export = None  # progress of the latest cloud history export (no link)
        self.next_export = 0.0  # epoch seconds; the first export covers all of keep_seconds
        self.keep_seconds = DEFAULT_CONFIG["history_days"] * 86400

    def ensure_connected(self):
        silent = time.time() - self.message_at
        if self.client and silent > SILENT_SECONDS:
            log(f"no MQTT message for {silent // 60:.0f} min; reconnecting", error=True)
        elif self.client and self.client.is_connected() and time.time() < self.expires - 300:
            return
        self.close()
        token = access_token()
        claims = jwt_claims(token)
        serials = request_json(DEVICES, token=token, user_agent=AGENT)
        if len(serials) != 1:
            raise RuntimeError(f"Expected 1 Bacon device, found {len(serials)}")
        self.sub, self.serial, self.expires = str(claims["sub"]), serials[0], claims.get("exp", time.time() + 900)
        self.client, _ = connect(token, self.sub)
        self.client.on_message = self._on_message
        self.message_at = time.time()
        log("connected")

    def request_export(self):
        """Ask the cloud for the purifier's stored history, as the app's "Download historic data" does.
        The first request covers all kept history, later ones the last day."""
        now = int(time.time())
        seconds = self.keep_seconds if not self.next_export else 86400
        self.next_export = now + EXPORT_SECONDS
        self.export = {"state": "requested", "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.client.publish(f"users/{self.sub}/commands/export", json.dumps(
            {"serialNumbers": [self.serial], "between": [now - seconds, now], "features": ["AP_BASE"]}))
        log("history export requested")

    def _export_progress(self, payload):
        """Record the export's progress; of the download link keep only its host, not the URL."""
        if not isinstance(payload, dict):
            return
        link = payload.get("link")
        with self.lock:
            self.export.update({k: payload.get(k) for k in ("state", "errorMessage", "ttl")},
                               link_host=urlsplit(link).hostname if isinstance(link, str) else None)
        log(f"history export {payload.get('state')}{': ' + str(payload['errorMessage']) if payload.get('errorMessage') else ''}",
            error=bool(payload.get("errorMessage")))
        if payload.get("state") == "SUCCEEDED" and isinstance(link, str):
            parts = urlsplit(link)
            if parts.scheme == "https" and parts.hostname == EXPORT_HOST:
                threading.Thread(target=self._fetch_export, args=(link,), daemon=True).start()

    def _fetch_export(self, link):
        """Download the export zip and add its readings to the history."""
        try:
            with urllib.request.urlopen(link, timeout=60) as resp:
                data = resp.read(50_000_001)
            if len(data) > 50_000_000:
                raise ValueError("export larger than 50 MB")
            readings = []
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if info.file_size > 200_000_000:
                        raise ValueError(f"{info.filename} unpacks to over 200 MB")
                    if info.filename.endswith(".csv"):
                        readings += parse_export(zf.read(info).decode("utf-8-sig", "replace"))
        except Exception as exc:  # report, don't kill the thread silently
            with self.lock:
                self.export["download_error"] = str(exc)
            log(f"history export download failed: {exc}", error=True)
            return
        newest = max(readings, key=lambda r: r["timestamp"], default=None)
        with self.lock:
            self.add_history(readings)
            if newest and (not self.sensor or newest["timestamp"] > self.sensor.get("timestamp", 0)):
                self.sensor = newest
                self.sensor_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.heard_at = time.time()
            self.export.update(imported=len(readings), newest=newest and datetime.fromtimestamp(
                newest["timestamp"], timezone.utc).isoformat(timespec="seconds"))
        log(f"history export imported {len(readings)} readings")

    def close(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.client = None

    def _topic(self, suffix):
        return f"users/{self.sub}/devices/{self.serial}/{suffix}"

    def _on_message(self, client, userdata, msg):
        # paho stops its network thread on a callback exception; log it and keep the session alive.
        self.message_at = time.time()
        try:
            self._handle(msg)
        except Exception as exc:
            log(f"MQTT message on {msg.topic.replace(self.sub or '', '{sub}')} failed: {type(exc).__name__}: {exc}",
                error=True)

    def _handle(self, msg):
        channel = msg.topic[len(self._topic("")):] if msg.topic.startswith(self._topic("")) else None
        if (channel or msg.topic) not in self.seen:  # first sighting per topic, to find what starts live readings
            self.seen.add(channel or msg.topic)
            log(f"mqtt channel {channel}" if channel else f"mqtt topic {msg.topic.replace(self.sub, '{sub}')}")
        if channel is None:
            if msg.topic == f"users/{self.sub}/commands/export/progress":
                try:
                    self._export_progress(json.loads(msg.payload))
                except ValueError:
                    log("history export progress was not JSON", error=True)
            return
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            log(f"MQTT message on {channel} was not JSON", error=True)
            return
        with self.lock:
            if channel.startswith("shadows/"):
                reported = (payload.get("state") or {}).get("reported") if isinstance(payload, dict) else None
                if isinstance(reported, dict):
                    shadow = channel.split("/")[1]
                    self.fields[shadow] = sorted(set(self.fields.get(shadow, [])) | set(reported))
                    if shadow == "state":
                        self.reported.update(reported)
            elif channel == "topics/sensor":
                items = payload.get("items") if isinstance(payload, dict) else None
                items = items if isinstance(items, list) and items else [payload]
                stamped = [i for i in items if isinstance(i, dict) and isinstance(i.get("timestamp"), (int, float))]
                newest = max(stamped, key=lambda i: i["timestamp"]) if stamped else items[-1]  # batch order unknown
                known = self.history[-1]["timestamp"] if self.history else None
                # The broker replays the last batch on every connect; only newer readings count as received now.
                if known is None or not stamped or newest["timestamp"] > known:
                    self.sensor_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    self.heard_at = time.time()
                if not self.sensor or newest.get("timestamp", 0) >= self.sensor.get("timestamp", 0):
                    self.sensor = newest
                self.add_history(stamped)
            elif channel == "topics/meta":
                self.meta = payload
            # topics/info is deliberately ignored: it carries Matter onboarding secrets.

    def add_history(self, items):
        """Call with self.lock held."""
        merged = merge_history(self.history, items, time.time(), self.keep_seconds)
        if merged != self.history:
            self.history = merged
            try:
                save_history(merged)  # a few writes an hour at most; fine for an SD card
            except OSError as exc:
                log(f"history not saved: {exc}", error=True)

    def refresh(self):
        self.client.publish(self._topic("shadows/state/get"), "")
        # Not what starts the sensor stream (tested); kept so shadow_fields lists these shadows' settings too.
        for shadow in ("$package", "schedule"):
            self.client.publish(self._topic(f"shadows/{shadow}/get"), "")

    def set_light(self, on):
        info = self.client.publish(self._topic("shadows/state/update"),
                                   json.dumps({"state": {"desired": {"lightEnabled": on}}}))
        if info.rc:  # not queued (e.g. disconnected): fail the tick so it reconnects and retries
            raise RuntimeError(f"ring change not sent (MQTT rc {info.rc})")


device = Device()
status = {"error": None, "last_tick": None, "wanted": None, "in_window": None, "air_not_good": None,
          "air_value": None, "last_change": None, "offline": False, "unconfirmed": None}
errors = deque(maxlen=20)  # newest first, shown on the dashboard
pending_login = {}


def log(message, error=False):
    line = f"{datetime.now().isoformat(timespec='seconds')} {'ERROR ' * error}{message}"
    print(line, flush=True)
    if error:
        errors.appendleft(line)


def send_due(current, want, sent, now):
    """Whether to (re)send a ring change: new, or unconfirmed for RESEND_SECONDS. The shadow keeps the
    desired value, so an offline purifier applies it when it is back; repeating every tick only spams."""
    if want is None or current is None:
        return False
    if sent is not None and sent[0] != want:
        return True  # overwrite the pending opposite change, or the purifier applies it when back
    return current != want and (sent is None or now - sent[1] >= RESEND_SECONDS)


def tick():
    cfg = load_config()
    now = datetime.now(timezone.utc)
    device.keep_seconds = cfg["history_days"] * 86400
    device.ensure_connected()
    device.refresh()
    if time.time() >= device.next_export:
        device.request_export()
    time.sleep(3)  # let the shadow reply land before deciding
    with device.lock:
        reading, current = device.sensor, device.reported.get("lightEnabled")
        heard_at = device.heard_at
        device.add_history([])  # applies a shortened retention
    offline = time.time() - heard_at > OFFLINE_SECONDS
    if offline != status["offline"]:
        since = datetime.fromtimestamp(heard_at, timezone.utc).isoformat(timespec="seconds")
        log(f"purifier offline: no new reading since {since}; ring and power show its last report" if offline
            else "purifier back online", error=offline)
        status["offline"] = offline
    stamp = (reading or {}).get("timestamp")
    if isinstance(stamp, (int, float)) and now.timestamp() - stamp > STALE_SECONDS:
        reading = None
    want = wanted_light(cfg, now, reading)
    rule = cfg["air_quality"]
    status.update(last_tick=now.isoformat(timespec="seconds"), wanted=want, in_window=in_window(cfg, now),
                  air_not_good=air_not_good(rule, reading) if rule["field"] else None,
                  air_value=(reading or {}).get(rule["field"]) if rule["field"] else None, error=None)
    if send_due(current, want, device.sent, now.timestamp()):
        retry = device.sent is not None and device.sent[0] == want
        device.set_light(want)
        device.sent = (want, now.timestamp())
        if not retry:
            status["unconfirmed"] = None
        status["last_change"] = f"{now.isoformat(timespec='seconds')} light {'on' if want else 'off'}{' (resent)' * retry}"
        log(status["last_change"])
    if want is None or (current == want and (device.sent is None or device.sent[0] == want)):
        device.sent = status["unconfirmed"] = None
    if device.sent and not status["unconfirmed"] and now.timestamp() - device.sent[1] >= CONFIRM_SECONDS:
        status["unconfirmed"] = f"purifier hasn't confirmed light {'on' if device.sent[0] else 'off'} sent at " \
            f"{datetime.fromtimestamp(device.sent[1], timezone.utc).isoformat(timespec='seconds')}"
        log(status["unconfirmed"], error=True)


def retry_delay(failures):
    """Seconds to wait after this many failures in a row: 1, 2, 4 ... 30 minutes, so a lasting fault
    doesn't log in to Bosch every minute."""
    return min(TICK_SECONDS * 2 ** (failures - 1), MAX_RETRY_SECONDS)


def scheduler():
    failures = 0
    while True:
        delay = TICK_SECONDS
        try:
            tick()
            if failures:
                log(f"recovered after {failures} failed attempt{'s' * (failures > 1)}")
            failures = 0
        except Exception as exc:
            failures += 1
            delay = retry_delay(failures)
            message = f"{type(exc).__name__}: {str(exc)[:150]}"
            log(f"{message}; retry {failures} in {delay // 60} min", error=True)
            status["error"] = f"{message} (retrying in {delay // 60} min)"
            device.close()
        time.sleep(delay)


def today_sun(cfg):
    if cfg["latitude"] is None or cfg["longitude"] is None:
        return None
    tz = ZoneInfo(cfg["timezone"])
    sun = sun_times(datetime.now(tz).date(), cfg["latitude"], cfg["longitude"])
    return sun and {"sunrise": sun[0].astimezone(tz).strftime("%H:%M"), "sunset": sun[1].astimezone(tz).strftime("%H:%M")}


# -- http ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    timeout = 10  # drop slow or idle clients instead of holding a thread forever

    def log_message(self, *args):
        pass

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        try:
            given = base64.b64decode(header.removeprefix("Basic ")).decode().partition(":")[2]
        except ValueError:
            given = ""
        if hmac.compare_digest(given.encode(), PASSWORD.encode()):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="bosch-air"')
        self.end_headers()
        return False

    def _send(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":  # unauthenticated, for the container healthcheck
            return self._send(200, {"ok": True})
        if not self._authorized():
            return
        if self.path == "/":
            with open(os.path.join(os.path.dirname(__file__), "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            cfg = load_config()
            with device.lock:
                live = {"connected": bool(device.client and device.client.is_connected()),
                        "light": device.reported.get("lightEnabled"),
                        "power": device.reported.get("powerEnabled"), "op_mode": device.reported.get("opMode"),
                        "fan_speed": device.reported.get("fanSpeed"),
                        "publish_interval": device.reported.get("dataLakeBasePublishInterval"),
                        "sensor": device.sensor, "sensor_at": device.sensor_at, "meta": device.meta,
                        "shadow_fields": device.fields, "export": device.export,
                        "filter": filter_estimate(device.history),
                        "heard_at": datetime.fromtimestamp(device.heard_at, timezone.utc).isoformat(timespec="seconds")}
            self._send(200, {"config": cfg, "logged_in": os.path.exists(os.environ["BOSCH_AIR_TOKEN_FILE"]),
                             "sun": today_sun(cfg), "errors": list(errors), **live, **status})
        elif self.path == "/api/history":
            with device.lock:
                history = list(device.history)
            self._send(200, {"readings": history})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(415, {"error": "JSON only"})  # also blocks simple cross-site form posts
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 <= length <= 65536:
                return self._send(413, {"error": "body too large"})
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            if self.path == "/api/config":
                save_config(validate(body))
                self._send(200, {"ok": True})
            elif self.path == "/api/login/start":
                url, state, verifier = start_login()
                pending_login.clear()
                pending_login.update(state=state, verifier=verifier)
                self._send(200, {"url": url})
            elif self.path == "/api/login/finish":
                if not pending_login:
                    raise ValueError("Start the login first")
                code = authorization_code(str(body.get("callback", "")), pending_login["state"], REDIRECT_URI)
                save_refresh_token(exchange_code(code, pending_login["verifier"]))
                pending_login.clear()
                device.close()
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except (ValueError, RedirectError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            log(f"POST {self.path} failed: {type(exc).__name__}", error=True)  # no detail: may echo login responses
            self._send(502, {"error": f"{type(exc).__name__}"})


if __name__ == "__main__":
    if not PASSWORD:
        # Without auth, a page the user visits could DNS-rebind to this host and drive the API.
        raise SystemExit("Set DASHBOARD_PASSWORD (HTTP basic auth) before starting the dashboard.")
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=scheduler, daemon=True).start()
    log(f"dashboard on :{PORT}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
