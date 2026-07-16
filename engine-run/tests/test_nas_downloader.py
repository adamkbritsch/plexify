"""Reachability circuit breaker — when the NAS is unreachable (offline / off-LAN / daemon down)
the engine must stop attempting calls instead of hanging every poll."""
import unittest
from unittest import mock

from app import nas_downloader as nd


class TestReachabilityBreaker(unittest.TestCase):
    def setUp(self):
        nd._breaker["until"] = 0.0

    def test_unreachable_opens_breaker_and_skips_next(self):
        with mock.patch.object(nd, "_base_urls", return_value=["http://nas:8788"]), \
             mock.patch.object(nd, "_host_reachable", return_value=False) as reach, \
             mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(nd.NasOfflineError):
                nd._req("/healthz")
            self.assertTrue(nd.is_offline())
            urlopen.assert_not_called()          # never even attempted the HTTP call
            # while the breaker is open, a second call does NOT re-probe reachability
            reach.reset_mock()
            with self.assertRaises(nd.NasOfflineError):
                nd._req("/healthz")
            reach.assert_not_called()

    def test_reachable_success_closes_breaker(self):
        nd._breaker["until"] = 0.0
        resp = mock.MagicMock()
        resp.read.return_value = b'{"ok": true}'
        cm = mock.MagicMock(); cm.__enter__.return_value = resp
        with mock.patch.object(nd, "_base_urls", return_value=["http://nas:8788"]), \
             mock.patch.object(nd, "_host_reachable", return_value=True), \
             mock.patch("urllib.request.urlopen", return_value=cm):
            self.assertEqual(nd._req("/healthz"), {"ok": True})
            self.assertFalse(nd.is_offline())

    def test_reachable_but_request_fails_does_not_open_breaker(self):
        # TCP-reachable but the request errors (500 / slow op / mid-restart) — that's a request
        # problem, not offline; the breaker must stay closed so we keep trying.
        with mock.patch.object(nd, "_base_urls", return_value=["http://nas:8788"]), \
             mock.patch.object(nd, "_host_reachable", return_value=True), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                nd._req("/healthz")
            self.assertFalse(nd.is_offline())     # NOT marked offline

    def test_falls_over_to_second_reachable_host(self):
        resp = mock.MagicMock(); resp.read.return_value = b'{"ok": true}'
        cm = mock.MagicMock(); cm.__enter__.return_value = resp
        calls = []
        def urlopen(req, timeout=10):
            calls.append(req.full_url)
            if "bad" in req.full_url:
                raise OSError("dead")
            return cm
        with mock.patch.object(nd, "_base_urls", return_value=["http://bad:8788", "http://good:8788"]), \
             mock.patch.object(nd, "_host_reachable", return_value=True), \
             mock.patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(nd._req("/x"), {"ok": True})
        self.assertEqual(calls, ["http://bad:8788/x", "http://good:8788/x"])


if __name__ == "__main__":
    unittest.main()
