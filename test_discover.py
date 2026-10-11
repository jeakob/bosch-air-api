import unittest
from unittest.mock import patch

from discover import authorization_code, read_authorization_code
from snapshot import ring_candidates


class RedirectTest(unittest.TestCase):
    target = "com.bosch.tt.dashtt.pointt://app/login"

    def test_accepts_matching_redirect(self):
        redirect = "com.bosch.tt.dashtt.pointt://app/login?state=abc&code=one"
        self.assertEqual(authorization_code(redirect, "abc", self.target), "one")

    def test_rejects_wrong_state(self):
        redirect = "com.bosch.tt.dashtt.pointt://app/login?state=bad&code=one"
        with self.assertRaises(ValueError):
            authorization_code(redirect, "abc", self.target)

    def test_explains_intermediate_singlekey_page(self):
        with self.assertRaisesRegex(ValueError, "intermediate"):
            authorization_code("https://singlekey-id.com/en-gb/redirection?f=example", "abc", self.target)

    def test_accepts_console_message(self):
        message = (
            "Connecting to 'com.bosch.tt.dashtt.pointt://app/login?"
            "code=one&state=abc&iss=https%3A%2F%2Fsinglekey-id.com%2Fauth' "
            "violates Content Security Policy"
        )
        self.assertEqual(authorization_code(message, "abc", self.target), "one")

    def test_rejects_truncated_console_message(self):
        with self.assertRaisesRegex(ValueError, "truncated"):
            authorization_code("com.bosch.tt.dashtt.pointt://app/login?code=one…", "abc", self.target)

    def test_skips_unrelated_console_lines(self):
        with patch("discover.getpass", side_effect=[
            "api.js recaptchacompat disabled",
            "Connecting to 'https://cdn.singlekey-id.com/scripts/ai.3.min.js.map'",
            "Failed to launch 'com.bosch.tt.dashtt.pointt://app/login?code=one&state=abc'",
        ]):
            self.assertEqual(read_authorization_code("abc"), "one")


class RingCandidateTest(unittest.TestCase):
    def test_keeps_only_field_that_flips_and_returns(self):
        on = {"state": {"reported": {"led": {"on": True}, "fan": 2}}, "version": 1, "metadata": {"t": 10}}
        off = {"state": {"reported": {"led": {"on": False}, "fan": 2}}, "version": 2, "metadata": {"t": 20}}
        on_again = {"state": {"reported": {"led": {"on": True}, "fan": 2}}, "version": 3, "metadata": {"t": 30}}
        self.assertEqual(ring_candidates(on, off, on_again), {"state.reported.led.on": (True, False)})


if __name__ == "__main__":
    unittest.main()
