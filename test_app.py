import unittest
from datetime import date, datetime, timezone

from app import air_not_good, in_window, sun_times, validate, wanted_light

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
        self.assertTrue(air_not_good(self.rule, None))
        self.assertFalse(air_not_good({"enabled": True, "field": "aq", "good": "Good"}, {"aq": "good"}))

    def test_auto_needs_window_and_bad_air(self):
        cfg = {**LONDON, "windows": [{"start": "00:00", "end": "23:59"}], "air_quality": self.rule}
        noon = utc(2024, 7, 1, 11, 0)
        self.assertFalse(wanted_light(cfg, noon, {"pm25": 5}))
        self.assertTrue(wanted_light(cfg, noon, {"pm25": 50}))
        self.assertIsNone(wanted_light({**cfg, "mode": "paused"}, noon, None))


class ValidateTest(unittest.TestCase):
    def test_rejects_bad_input(self):
        for bad in ({**LONDON, "mode": "x"}, {**LONDON, "timezone": "Mars/Base"},
                    {**LONDON, "windows": [{"start": "25:00", "end": "01:00"}]},
                    {**LONDON, "latitude": None, "windows": [{"start": "sunset", "end": "23:00"}]}):
            with self.assertRaises(ValueError):
                validate(bad)
        self.assertEqual(validate(LONDON)["timezone"], "Europe/London")


if __name__ == "__main__":
    unittest.main()
