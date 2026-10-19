"""Tests for coin_reader.py. Run with: python3 -m unittest test_coin_reader

No GPIO hardware or libgpiod is needed; the GPIO loop itself is tested by
running coin_reader.py --dry-run on the Orange Pi.
"""

import contextlib
import io
import json
import logging
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import coin_reader as cr

# Real captures from the Orange Pi PC, Allan 1239 on Medium, line 12 = header pin 3.
CAPTURE_5 = """\
1033.686459669  rising  gpiochip0 12
1047.381607664  falling gpiochip0 12
1047.431624139  rising  gpiochip0 12
1047.531661172  falling gpiochip0 12
1047.581675898  rising  gpiochip0 12
1047.681714015  falling gpiochip0 12
1047.731732740  rising  gpiochip0 12
1047.831769023  falling gpiochip0 12
1047.881786040  rising  gpiochip0 12
1047.981822282  falling gpiochip0 12
1048.031840466  rising  gpiochip0 12
"""

CAPTURE_1 = """\
root@orangepipc:~# gpiomon -c gpiochip0 --bias=pull-up 3 12
1129.568662103  rising  gpiochip0 3
1129.568933924  rising  gpiochip0 12
1138.532268936  falling gpiochip0 12
1138.582287673  rising  gpiochip0 12

1 peso
"""

CAPTURE_10 = """\
1178.911547203  rising  gpiochip0 3
1178.911756153  rising  gpiochip0 12
1186.011165139  falling gpiochip0 12
1186.061178505  rising  gpiochip0 12
1186.161215031  falling gpiochip0 12
1186.211240440  rising  gpiochip0 12
1186.311270425  falling gpiochip0 12
1186.361294626  rising  gpiochip0 12
1186.461327319  falling gpiochip0 12
1186.511345020  rising  gpiochip0 12
1186.611389046  falling gpiochip0 12
1186.661405830  rising  gpiochip0 12
1186.761443690  falling gpiochip0 12
1186.811455766  rising  gpiochip0 12
1186.911501958  falling gpiochip0 12
1186.961513868  rising  gpiochip0 12
1187.061584478  falling gpiochip0 12
1187.111596390  rising  gpiochip0 12
1187.211636132  falling gpiochip0 12
1187.261654169  rising  gpiochip0 12
1187.361696077  falling gpiochip0 12
1187.411709906  rising  gpiochip0 12
"""

CAPTURE_20 = """\
1213.179995845  rising  gpiochip0 12
1220.755065057  falling gpiochip0 12
1220.805082236  rising  gpiochip0 12
1220.905118386  falling gpiochip0 12
1220.955135732  rising  gpiochip0 12
1221.055173633  falling gpiochip0 12
1221.105191982  rising  gpiochip0 12
1221.205227513  falling gpiochip0 12
1221.255279694  rising  gpiochip0 12
1221.355314349  falling gpiochip0 12
1221.405338823  rising  gpiochip0 12
1221.505373729  falling gpiochip0 12
1221.555389578  rising  gpiochip0 12
1221.655430858  falling gpiochip0 12
1221.705444124  rising  gpiochip0 12
1221.805484738  falling gpiochip0 12
1221.855503628  rising  gpiochip0 12
1221.955542284  falling gpiochip0 12
1222.005558841  rising  gpiochip0 12
1222.105595501  falling gpiochip0 12
1222.155616227  rising  gpiochip0 12
1222.255649639  falling gpiochip0 12
1222.305671991  rising  gpiochip0 12
1222.405705819  falling gpiochip0 12
1222.455730878  rising  gpiochip0 12
1222.555766290  falling gpiochip0 12
1222.605815307  rising  gpiochip0 12
1222.705851260  falling gpiochip0 12
1222.755866737  rising  gpiochip0 12
1222.855905023  falling gpiochip0 12
1222.905924250  rising  gpiochip0 12
1223.005963911  falling gpiochip0 12
1223.055983139  rising  gpiochip0 12
1223.156037097  falling gpiochip0 12
1223.206036202  rising  gpiochip0 12
1223.306074411  falling gpiochip0 12
1223.356092515  rising  gpiochip0 12
1223.456128141  falling gpiochip0 12
1223.506149662  rising  gpiochip0 12
1223.606184037  falling gpiochip0 12
1223.656205350  rising  gpiochip0 12
"""


def setUpModule():
    # Keep test output quiet; assertLogs still captures what the tests check.
    cr.log.addHandler(logging.NullHandler())
    cr.log.propagate = False


def ms(value):
    return int(value * cr.NS_PER_MS)


def train(start_ms, count, low_ms=50, period_ms=150):
    """Synthetic coin pulses shaped like the real Medium-speed signal."""
    events = []
    for i in range(count):
        t = start_ms + i * period_ms
        events += [(cr.FALLING, ms(t)), (cr.RISING, ms(t + low_ms))]
    return events


def decode_capture(text, line=12):
    return cr.decode_events(cr.parse_gpiomon(text.splitlines(), line), cr.DecoderConfig())


class RealCaptureTests(unittest.TestCase):
    def test_each_coin_counts_correctly(self):
        for text, pesos in ((CAPTURE_1, 1), (CAPTURE_5, 5), (CAPTURE_10, 10), (CAPTURE_20, 20)):
            with self.subTest(pesos=pesos):
                self.assertEqual(decode_capture(text), [pesos])

    def test_session_of_all_coins_gives_four_separate_coins(self):
        session = CAPTURE_5 + CAPTURE_1 + CAPTURE_10 + CAPTURE_20
        self.assertEqual(decode_capture(session), [5, 1, 10, 20])

    def test_other_line_has_no_coins(self):
        self.assertEqual(decode_capture(CAPTURE_10, line=3), [])


class ParseTests(unittest.TestCase):
    def test_timestamp_keeps_nanoseconds(self):
        self.assertEqual(cr._parse_ts_ns("1220.755065057"), 1_220_755_065_057)
        self.assertEqual(cr._parse_ts_ns("12.5"), 12_500_000_000)

    def test_non_event_lines_are_skipped(self):
        lines = ["5 pesos", "", "root@orangepipc:~# gpiomon", "abc  falling gpiochip0 12"]
        self.assertEqual(list(cr.parse_gpiomon(lines, 12)), [])


class DecoderTests(unittest.TestCase):
    def setUp(self):
        self.decoder = cr.PulseDecoder()

    def feed_all(self, events):
        for edge, ts in events:
            self.decoder.feed(edge, ts)

    def test_startup_rising_edge_is_not_a_pulse(self):
        self.decoder.feed(cr.RISING, ms(0))
        self.assertEqual(self.decoder.poll(ms(5000)), 0)

    def test_coin_waits_for_the_gap(self):
        self.feed_all(train(0, 5))  # last pulse ends at 650 ms
        self.assertEqual(self.decoder.poll(ms(650 + 499)), 0)
        self.assertEqual(self.decoder.poll(ms(650 + 500)), 5)
        self.assertEqual(self.decoder.poll(ms(5000)), 0)  # reported only once

    def test_coin_is_not_split_while_a_pulse_is_in_progress(self):
        self.feed_all(train(0, 3))  # last pulse ends at 350 ms
        self.decoder.feed(cr.FALLING, ms(820))
        self.assertEqual(self.decoder.poll(ms(860)), 0)  # gap passed, but line is low
        self.decoder.feed(cr.RISING, ms(870))
        self.assertEqual(self.decoder.poll(ms(870 + 500)), 4)

    def test_short_glitch_is_ignored(self):
        with self.assertLogs("coin-reader", "WARNING"):
            self.feed_all([(cr.FALLING, ms(0)), (cr.RISING, ms(2))])
        self.assertEqual(self.decoder.rejected, 1)
        self.assertEqual(self.decoder.poll(ms(5000)), 0)

    def test_stuck_low_still_releases_counted_pulses(self):
        self.feed_all(train(0, 2))  # last pulse ends at 200 ms
        self.decoder.feed(cr.FALLING, ms(300))
        with self.assertLogs("coin-reader", "WARNING") as logs:
            self.assertEqual(self.decoder.poll(ms(1300)), 2)
        self.assertIn("check the wiring", logs.output[0])
        with self.assertLogs("coin-reader", "WARNING"):
            self.decoder.feed(cr.RISING, ms(1400))  # 1100 ms low is not a pulse
        self.assertEqual(self.decoder.rejected, 1)

    def test_slow_speed_pulses_are_accepted(self):
        events = train(0, 3, low_ms=100, period_ms=200)
        self.assertEqual(cr.decode_events(events, cr.DecoderConfig()), [3])

    def test_coins_separated_by_a_pause_stay_separate(self):
        events = train(0, 5) + train(2000, 1)
        self.assertEqual(cr.decode_events(events, cr.DecoderConfig()), [5, 1])

    def test_flush_returns_unreported_pulses(self):
        self.feed_all(train(0, 3))
        self.assertEqual(self.decoder.flush(), 3)
        self.assertEqual(self.decoder.flush(), 0)

    def test_unknown_edge_is_rejected(self):
        with self.assertRaises(ValueError):
            self.decoder.feed("sideways", 0)


class RecordTests(unittest.TestCase):
    def test_record_carries_pesos_not_minutes(self):
        record = cr.make_record(5)
        self.assertEqual(set(record), {"coin_id", "pesos", "pulses", "detected_at"})
        self.assertEqual((record["pesos"], record["pulses"]), (5, 5))
        self.assertEqual(cr.make_record(5, pesos_per_pulse=5)["pesos"], 25)

    def test_acceptance_message(self):
        record = cr.make_record(5)
        assigned = cr.describe_acceptance(record, {"minutes": 25, "mac": "aa:bb:cc:dd:ee:01"})
        self.assertEqual(assigned, "coin accepted: PHP 5 = 25 min for aa:bb:cc:dd:ee:01")
        self.assertIn("no customer had tapped", cr.describe_acceptance(record, {"minutes": 25, "mac": None}))
        self.assertIn("already recorded", cr.describe_acceptance(record, {"minutes": 25, "duplicate": True}))

    def test_each_coin_gets_its_own_id(self):
        self.assertNotEqual(cr.make_record(1)["coin_id"], cr.make_record(1)["coin_id"])


class LedLogicTests(unittest.TestCase):
    def test_follows_the_insert_coin_window(self):
        led = cr.LedLogic()
        self.assertFalse(led.lit(0))
        led.window_open = True
        self.assertTrue(led.lit(1))
        led.window_open = False
        self.assertFalse(led.lit(2))

    def test_each_coin_blinks_twice_then_returns_to_the_window_state(self):
        led = cr.LedLogic()
        led.window_open = True
        led.coin(10.0)
        steps = [led.lit(10.0 + 0.15 * i + 0.01) for i in range(4)]
        self.assertEqual(steps, [False, True, False, True])
        self.assertTrue(led.blinking(10.5))
        self.assertFalse(led.blinking(10.61))
        self.assertTrue(led.lit(10.7))  # window still open
        led.window_open = False
        led.coin(20.0)
        self.assertFalse(led.lit(20.7))  # no window: dark after the blink


class WindowWatcherTests(unittest.TestCase):
    def start_server(self, status=200, body=b'{"ok": true, "active": true}'):
        self.hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                self.hits.append((handler.path, handler.headers.get("X-Admin-Token")))
                handler.send_response(status)
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/api/insert-coin"

    def test_open_window_and_polling_interval(self):
        watcher = cr.WindowWatcher(self.start_server(), "t" * 32, interval=0.5)
        self.assertTrue(watcher.poll(100.0))
        self.assertTrue(watcher.poll(100.2))  # cached
        self.assertEqual(self.hits, [("/api/insert-coin", "t" * 32)])
        watcher.poll(100.6)
        self.assertEqual(len(self.hits), 2)

    def test_closed_or_broken_means_off(self):
        closed = self.start_server(body=b'{"ok": true, "active": false}')
        self.assertFalse(cr.WindowWatcher(closed, None).poll(0))
        refused = self.start_server(status=401, body=b'{"error": "token"}')
        with self.assertLogs("coin-reader", "WARNING"):
            self.assertFalse(cr.WindowWatcher(refused, None).poll(0))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with self.assertLogs("coin-reader", "WARNING"):
            self.assertFalse(cr.WindowWatcher(f"http://127.0.0.1:{port}/api/insert-coin", None, timeout=1).poll(0))

    def test_window_url_sits_next_to_the_coin_route(self):
        self.assertEqual(cr.window_url("http://127.0.0.1:8090/api/coin"), "http://127.0.0.1:8090/api/insert-coin")


class FakeSink:
    def __init__(self, fail=False, fail_after=None):
        self.fail = fail
        self.fail_after = fail_after
        self.sent = []

    def send(self, record):
        if self.fail or (self.fail_after is not None and len(self.sent) >= self.fail_after):
            raise cr.SinkError("credit service down")
        self.sent.append(record)


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state" / "coin-spool.jsonl"
        self.spool = cr.Spool(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_coin_is_kept_when_service_is_down(self):
        record = cr.make_record(5)
        cr.deliver(record, self.spool, FakeSink(fail=True))
        self.assertEqual(self.spool.pending(), [record])

    def test_waiting_coins_are_sent_in_order_after_recovery(self):
        sink = FakeSink(fail=True)
        first, second = cr.make_record(1), cr.make_record(20)
        cr.deliver(first, self.spool, sink)
        cr.deliver(second, self.spool, sink)
        sink.fail = False
        self.assertEqual(self.spool.flush(sink), 2)
        self.assertEqual(sink.sent, [first, second])
        self.assertFalse(self.path.exists())

    def test_partial_failure_keeps_the_rest(self):
        records = [cr.make_record(n) for n in (1, 5, 10)]
        for record in records:
            self.spool.add(record)
        sink = FakeSink(fail_after=1)
        self.assertEqual(self.spool.flush(sink), 1)
        self.assertEqual(sink.sent, records[:1])
        self.assertEqual(self.spool.pending(), records[1:])

    def test_unreadable_line_is_dropped_and_logged(self):
        record = cr.make_record(5)
        self.path.write_text(json.dumps(record) + '\n{"coin_id": "cut-off\n', encoding="utf-8")
        with self.assertLogs("coin-reader", "ERROR"):
            self.assertEqual(self.spool.pending(), [record])

    def test_coin_is_sent_directly_if_spool_cannot_be_written(self):
        class ReadOnlySpool(cr.Spool):
            def add(self, record):
                raise PermissionError("read-only filesystem")

        sink = FakeSink()
        record = cr.make_record(10)
        with self.assertLogs("coin-reader", "ERROR"):
            cr.deliver(record, ReadOnlySpool(self.path), sink)
        self.assertEqual(sink.sent, [record])

    def test_dry_run_never_uses_the_real_spool(self):
        args = cr.parse_args(["--dry-run", "--spool", str(self.path)])
        self.assertEqual(cr.make_spool(args).path, cr.DRY_RUN_SPOOL)


class HttpSinkTests(unittest.TestCase):
    def start_server(self, status, reply=b'{"ok": true, "minutes": 25, "mac": null}'):
        received = []
        self.tokens = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                length = int(handler.headers["Content-Length"])
                received.append(json.loads(handler.rfile.read(length)))
                self.tokens.append(handler.headers.get("X-Admin-Token"))
                handler.send_response(status)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(reply)))
                handler.end_headers()
                handler.wfile.write(reply)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/coins", received

    def test_coin_is_posted_as_json_with_token(self):
        url, received = self.start_server(200)
        record = cr.make_record(5)
        reply = cr.HttpSink(url, token="x" * 32).send(record)
        self.assertEqual(received, [record])
        self.assertEqual(self.tokens, ["x" * 32])
        self.assertEqual(reply["minutes"], 25)

    def test_non_json_reply_still_counts_as_accepted(self):
        url, _ = self.start_server(200, reply=b"OK")
        self.assertEqual(cr.HttpSink(url).send(cr.make_record(1)), {})

    def test_error_status_is_a_failure(self):
        url, _ = self.start_server(500)
        with self.assertRaises(cr.SinkError):
            cr.HttpSink(url).send(cr.make_record(1))

    def test_unreachable_service_is_a_failure(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with self.assertRaises(cr.SinkError):
            cr.HttpSink(f"http://127.0.0.1:{port}/coins", timeout=1).send(cr.make_record(1))


GOOD_TOKEN = "t" * 32
CLEAN_ENV = {"COIN_READER_URL": "", "PISOWIFI_PORT": "", "PISOWIFI_ADMIN_TOKEN": "",
             "COIN_LED_LINE": "", "COIN_LED_ACTIVE_LOW": ""}


class ArgsTests(unittest.TestCase):
    def parse(self, argv, **env):
        with mock.patch.dict(os.environ, {**CLEAN_ENV, **env}):
            return cr.parse_args(argv)

    def parse_error(self, argv, **env):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse(argv, **env)

    def test_default_url_is_creditd_coin_route(self):
        args = self.parse([], PISOWIFI_ADMIN_TOKEN=GOOD_TOKEN)
        self.assertEqual(args.url, "http://127.0.0.1:8090/api/coin")
        args = self.parse([], PISOWIFI_ADMIN_TOKEN=GOOD_TOKEN, PISOWIFI_PORT="8091")
        self.assertEqual(args.url, "http://127.0.0.1:8091/api/coin")

    def test_url_override_from_environment(self):
        args = self.parse([], PISOWIFI_ADMIN_TOKEN=GOOD_TOKEN, COIN_READER_URL="http://127.0.0.1:9000/coins")
        self.assertEqual(args.url, "http://127.0.0.1:9000/coins")

    def test_token_required_for_live_mode(self):
        self.parse_error([])
        self.parse_error([], PISOWIFI_ADMIN_TOKEN="REPLACE_WITH_LONG_RANDOM_SECRET")
        self.parse_error([], PISOWIFI_ADMIN_TOKEN="short")
        self.assertEqual(self.parse([], PISOWIFI_ADMIN_TOKEN=GOOD_TOKEN).token, GOOD_TOKEN)

    def test_dry_run_and_replay_need_no_token(self):
        self.parse(["--dry-run"])
        self.parse(["--replay", "capture.txt"])

    def test_bad_url_rejected(self):
        self.parse_error(["--url", "http://127.0.0.1:CHANGE-ME/coins"], PISOWIFI_ADMIN_TOKEN=GOOD_TOKEN)
        self.parse_error(["--url", "ftp://example/coins"], PISOWIFI_ADMIN_TOKEN=GOOD_TOKEN)

    def test_led_line_from_environment(self):
        self.assertIsNone(self.parse(["--dry-run"]).led_line)
        args = self.parse(["--dry-run"], COIN_LED_LINE="11", COIN_LED_ACTIVE_LOW="1")
        self.assertEqual((args.led_line, args.led_active_low), (11, True))
        self.assertFalse(self.parse(["--dry-run"], COIN_LED_LINE="11").led_active_low)

    def test_led_line_cannot_be_the_coin_line(self):
        self.parse_error(["--dry-run", "--led-line", "12"])

    def test_pulse_window_must_make_sense(self):
        self.parse_error(["--dry-run", "--min-pulse-ms", "200", "--max-pulse-ms", "100"])

    def test_token_never_appears_in_logs(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        sink = cr.HttpSink(f"http://127.0.0.1:{port}/api/coin", token=GOOD_TOKEN, timeout=1)
        with tempfile.TemporaryDirectory() as tmp, self.assertLogs("coin-reader", "INFO") as logs:
            cr.deliver(cr.make_record(5), cr.Spool(Path(tmp) / "spool.jsonl"), sink)
        self.assertIn("credit service unavailable", "\n".join(logs.output))
        self.assertNotIn(GOOD_TOKEN, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
