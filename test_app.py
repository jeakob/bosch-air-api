import unittest
from datetime import date, datetime, timezone

from app import (air_not_good, filter_estimate, in_window, merge_history, parse_export, retry_delay, send_due, sun_times,
                 validate, wanted_light)

LONDON = {"mode": "auto", "timezone": "Europe/London", "latitude": 51.5, "longitude": -0.13,
          "windows": [], "air_quality": {"enabled": False, "field": "", "good": None}}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class SunTest(unittest.TestCase):
    def test_london_midsummer_within_three_minutes(self):
        rise, set_ = sun_times(date(2024, 6, 21), 51.5, -0.13)
        self.assertLess(abs((rise - utc(2024, 6, 21, 3, 43)).total_seconds()), 180)
        self.assertLess(abs((set_ - utc(2024, 6, 21, 20, 21)).total_seconds()), 180)

    def test_polar_night_has_no_sunrise(self):
        self.assertIsNone(sun_times(date(2024, 12, 21), 78.2, 15.6))


class WindowTest(unittest.TestCase):
    def cfg(self, start, end):
        return {**LONDON, "windows": [{"start": start, "end": end}]}

    def test_window_past_midnight(self):
        cfg = self.cfg("22:00", "02:00")  # London is UTC+1 in July
        self.assertTrue(in_window(cfg, utc(2024, 7, 1, 21, 30)))   # 22:30 local
        self.assertTrue(in_window(cfg, utc(2024, 7, 1, 0, 30)))    # 01:30 local
        self.assertFalse(in_window(cfg, utc(2024, 7, 1, 12, 0)))

    def test_sunset_offset(self):
        cfg = self.cfg("sunset+30", "23:00")  # sunset ~21:21 local on 1 July
        self.assertFalse(in_window(cfg, utc(2024, 7, 1, 20, 30)))  # 21:30 local, before sunset+30
        self.assertTrue(in_window(cfg, utc(2024, 7, 1, 21, 0)))    # 22:00 local


class AirTest(unittest.TestCase):
    rule = {"enabled": True, "field": "pm25", "good": 12}

    def test_threshold_text_and_unknown(self):
        self.assertFalse(air_not_good(self.rule, {"pm25": 8}))
        self.assertTrue(air_not_good(self.rule, {"pm25": 30}))
        self.assertFalse(air_not_good(self.rule, None))  # no reading: don't turn the ring on
        self.assertFalse(air_not_good({"enabled": True, "field": "aq", "good": "Good"}, {"aq": "good"}))

    def test_window_overrides_air_quality(self):
        cfg = {**LONDON, "windows": [{"start": "11:00", "end": "13:00", "air_only": False}],
               "air_quality": self.rule}
        noon, evening = utc(2024, 7, 1, 11, 0), utc(2024, 7, 1, 17, 0)  # 12:00 and 18:00 London
        self.assertTrue(wanted_light(cfg, noon, {"pm25": 5}))       # in window, good air
        self.assertFalse(wanted_light(cfg, evening, {"pm25": 5}))   # outside, good air
        self.assertTrue(wanted_light(cfg, evening, {"pm25": 50}))   # outside, bad air
        self.assertFalse(wanted_light(cfg, evening, None))          # outside, no reading
        self.assertFalse(wanted_light({**cfg, "air_quality": {**self.rule, "enabled": False}}, evening, {"pm25": 50}))
        self.assertIsNone(wanted_light({**cfg, "mode": "paused"}, noon, None))

    def test_window_only_if_air_not_good(self):
        cfg = {**LONDON, "windows": [{"start": "11:00", "end": "13:00", "air_only": True}],
               "air_quality": {**self.rule, "enabled": False}}
        noon = utc(2024, 7, 1, 11, 0)
        self.assertFalse(wanted_light(cfg, noon, {"pm25": 5}))
        self.assertTrue(wanted_light(cfg, noon, {"pm25": 50}))
        self.assertFalse(wanted_light(cfg, noon, None))


class HistoryTest(unittest.TestCase):
    def test_merge_sorts_dedupes_and_prunes(self):
        keep = 7 * 86400
        now = 10 * keep
        old = {"timestamp": now - keep - 1, "pm2.5": 9}
        history = merge_history([old], [{"timestamp": now, "pm2.5": 290}, {"timestamp": now - 60, "pm2.5": 5},
                                        {"pm2.5": 1}, "junk"], now, keep)
        self.assertEqual([r["pm2.5"] for r in history], [5, 290])
        again = merge_history(history, [{"timestamp": now, "pm2.5": 291}], now, keep)
        self.assertEqual([r["pm2.5"] for r in again], [5, 291])


class ExportTest(unittest.TestCase):
    def test_parses_cloud_csv_with_any_timestamp_format(self):
        csv_text = ("timestamp,airQuality,fanSpeed,filterCapacity [%],opMode,pm2_5 [µg/m3]\n"
                    "1791964800,1,2,95,auto,4\n"
                    "1791966600000,2,2,95,auto,14.0\n"
                    "2026-10-14T09:00:00Z,1,1,95,silent,\n"
                    "not a time,1,1,95,auto,3\n")
        rows = parse_export(csv_text)
        self.assertEqual([r["timestamp"] for r in rows], [1791964800, 1791966600, 1791968400])
        self.assertEqual(rows[1], {"timestamp": 1791966600, "airQuality": 2, "pm2_5": 14, "filterCapacity": 95, "pm2.5": 14})
        self.assertNotIn("pm2.5", rows[2])  # empty cell: no value rather than 0


class FilterTest(unittest.TestCase):
    def test_projects_linear_wear_and_restarts_after_a_new_filter(self):
        day = 86400
        wear = [{"timestamp": d * day, "filterCapacity": 100 - d} for d in range(11)]  # 1 % a day, 90 % left
        est = filter_estimate(wear)
        self.assertEqual(est["per_day"], 1.0)
        self.assertEqual(est["replace_at"], 100 * day)
        self.assertIsNone(filter_estimate(wear[:2]))  # too little to go on
        fresh = wear + [{"timestamp": 11 * day, "filterCapacity": 100}]
        self.assertIsNone(filter_estimate(fresh))  # new filter: old wear is ignored


class RetryTest(unittest.TestCase):
    def test_backoff_doubles_to_thirty_minutes(self):
        self.assertEqual([retry_delay(n) // 60 for n in range(1, 8)], [1, 2, 4, 8, 16, 30, 30])


class ValidateTest(unittest.TestCase):
    def test_rejects_bad_input(self):
        for bad in ({**LONDON, "mode": "x"}, {**LONDON, "timezone": "Mars/Base"},
                    {**LONDON, "windows": [{"start": "25:00", "end": "01:00"}]},
                    {**LONDON, "latitude": None, "windows": [{"start": "sunset", "end": "23:00"}]},
                    {**LONDON, "windows": [{"start": "10:00", "end": "11:00", "air_only": True}]},
                    {**LONDON, "history_days": 0}, {**LONDON, "history_days": 400}, {**LONDON, "history_days": "7"}):
            with self.assertRaises(ValueError):
                validate(bad)
        self.assertEqual(validate(LONDON)["timezone"], "Europe/London")


if __name__ == "__main__":
    unittest.main()


class SendDueTest(unittest.TestCase):
    def test_sends_once_then_waits_before_resending(self):
        self.assertFalse(send_due(True, True, None, 0))      # already right
        self.assertFalse(send_due(None, False, None, 0))     # state unknown
        self.assertTrue(send_due(True, False, None, 0))      # new change
        self.assertFalse(send_due(True, False, (False, 0), 60))  # unconfirmed, too soon to repeat
        self.assertTrue(send_due(True, False, (False, 0), 600))  # repeat after RESEND_SECONDS
        self.assertTrue(send_due(False, True, (False, 0), 60))   # wanted value changed: send now
        self.assertTrue(send_due(True, True, (False, 0), 60))    # back to what it reports: cancel the pending off
