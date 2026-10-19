"""Tests for portal.py. Run with the service venv (it has Flask):
    cd portal && /opt/pisowifi-venv/bin/python -m unittest test_portal
"""

import logging
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "app"))

import portal  # noqa: E402

PHONE_IP, PHONE_MAC = "10.20.0.121", "00:1f:fb:bb:25:bc"
ARP = f"""IP address       HW type     Flags       HW address            Mask     Device
{PHONE_IP}      0x1         0x2         00:1F:FB:BB:25:BC     *        enx00e099004abb
10.20.0.150      0x1         0x0         00:00:00:00:00:00     *        enx00e099004abb
192.168.100.1    0x1         0x2         aa:bb:cc:00:11:22     *        end0
"""


class FakeClient:
    def __init__(self):
        self.calls = []
        self.reply = (200, {"ok": True})
        self.down = False

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.down:
            raise portal.CreditdUnavailable("refused")
        return self.reply


class PortalTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.arp = Path(tmp.name) / "arp"
        self.arp.write_text(ARP)
        self.fake = FakeClient()
        self.app = portal.create_app(self.fake, self.arp, "Piso WiFi")
        self.client = self.app.test_client()

    def get(self, path, ip=PHONE_IP):
        return self.client.get(path, environ_base={"REMOTE_ADDR": ip})

    def post(self, path, ip=PHONE_IP, header=True, json=None):
        headers = {"X-Requested-With": "piso-portal"} if header else {}
        return self.client.post(path, environ_base={"REMOTE_ADDR": ip}, headers=headers, json=json)


class DeviceTests(PortalTest):
    def test_mac_comes_from_the_arp_table(self):
        self.assertEqual(portal.mac_for_ip(PHONE_IP, self.arp), PHONE_MAC)
        self.assertIsNone(portal.mac_for_ip("10.20.0.150", self.arp))  # incomplete entry
        self.assertIsNone(portal.mac_for_ip("10.20.0.99", self.arp))
        self.assertIsNone(portal.mac_for_ip(PHONE_IP, Path("/nonexistent/arp")))

    def test_state_is_for_the_requesting_phone(self):
        self.fake.reply = (200, {"ok": True, "mac": PHONE_MAC})
        r = self.get("/api/state")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fake.calls, [("GET", "/api/customer?mac=00%3A1f%3Afb%3Abb%3A25%3Abc", None)])
        self.assertEqual(r.get_json()["portal_address"], "http://localhost")

    def test_short_address_is_shown_when_set(self):
        app = portal.create_app(self.fake, self.arp, "Piso WiFi", address="http://piso.wifi")
        r = app.test_client().get("/api/state", environ_base={"REMOTE_ADDR": PHONE_IP})
        self.assertEqual(r.get_json()["portal_address"], "http://piso.wifi")

    def test_unknown_phone(self):
        r = self.get("/api/state", ip="10.20.0.99")
        self.assertEqual((r.status_code, r.get_json()["error"]), (400, "unknown_device"))
        self.assertEqual(self.fake.calls, [])


class ActionTests(PortalTest):
    def test_actions_need_the_portal_header(self):
        for path in ("/api/insert-coin", "/api/connect"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, header=False).status_code, 403)
        self.assertEqual(self.fake.calls, [])

    def test_mac_in_the_request_body_is_ignored(self):
        self.fake.reply = (200, {"ok": True, "seconds_left": 60})
        self.post("/api/insert-coin", json={"mac": "11:22:33:44:55:66"})
        self.assertEqual(self.fake.calls, [("POST", "/api/insert-coin", {"mac": PHONE_MAC})])

    def test_busy_coinslot(self):
        self.fake.reply = (409, {"error": "coinslot busy", "retry_after": 42})
        r = self.post("/api/insert-coin")
        self.assertEqual((r.status_code, r.get_json()), (409, {"error": "busy", "retry_after": 42}))

    def test_connect_outcomes(self):
        cases = [
            ((200, {"ok": True, "seconds_left": 1500, "minutes_added": 25}), 200, None),
            ((409, {"error": "internet access is switched off: set ..."}), 409, "internet_off"),
            ((409, {"error": "no credit yet: insert coins first"}), 409, "no_credit"),
            ((502, {"error": "couldn't switch on internet access"}), 502, "connect_failed"),
        ]
        for reply, status, error in cases:
            with self.subTest(error=error):
                self.fake.reply = reply
                r = self.post("/api/connect")
                self.assertEqual(r.status_code, status)
                self.assertEqual(r.get_json().get("error"), error)

    def test_too_many_taps(self):
        self.fake.reply = (429, {"error": "too many Insert Coin taps without coins", "retry_after": 240})
        r = self.post("/api/insert-coin")
        self.assertEqual((r.status_code, r.get_json()), (429, {"error": "too_many_taps", "retry_after": 240}))

    def test_voucher_outcomes(self):
        cases = [
            ((200, {"ok": True, "minutes": 60, "started": True, "seconds_left": 3600}), 200, None),
            ((404, {"error": "unknown voucher code"}), 404, "bad_code"),
            ((409, {"error": "this voucher has already been used"}), 409, "used_code"),
            ((429, {"error": "too many wrong codes", "retry_after": 500}), 429, "code_locked"),
        ]
        for reply, status, error in cases:
            with self.subTest(error=error):
                self.fake.reply = reply
                r = self.post("/api/redeem", json={"code": "abcd-2345"})
                self.assertEqual((r.status_code, r.get_json().get("error")), (status, error))
        self.assertEqual(self.fake.calls[-1], ("POST", "/api/redeem", {"mac": PHONE_MAC, "code": "abcd-2345"}))
        self.assertEqual(r.get_json()["retry_after"], 500)

    def test_pause_and_resume_outcomes(self):
        self.fake.reply = (200, {"ok": True, "paused_seconds": 5400})
        self.assertEqual(self.post("/api/pause").get_json()["paused_seconds"], 5400)
        self.fake.reply = (409, {"error": "less than a minute left; nothing to pause"})
        self.assertEqual(self.post("/api/pause").get_json()["error"], "cannot_pause")
        self.fake.reply = (502, {"error": "couldn't resume"})
        r = self.post("/api/resume")
        self.assertEqual((r.status_code, r.get_json()["error"]), (502, "resume_failed"))
        self.fake.reply = (409, {"error": "internet access is switched off: set ..."})
        self.assertEqual(self.post("/api/resume").get_json()["error"], "internet_off")

    def test_credit_service_down(self):
        self.fake.down = True
        for r in (self.get("/api/state"), self.post("/api/insert-coin"), self.post("/api/connect")):
            self.assertEqual((r.status_code, r.get_json()["error"]), (502, "machine_offline"))


class PageTests(PortalTest):
    def test_page_answers_any_captive_portal_probe(self):
        for path in ("/", "/generate_204", "/hotspot-detect.html", "/?fas=abc123"):
            with self.subTest(path=path):
                r = self.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertIn("Insert coin", r.get_data(as_text=True))

    def test_strict_csp_with_matching_nonce(self):
        r = self.get("/")
        nonce = re.search(r"'nonce-([^']+)'", r.headers["Content-Security-Policy"]).group(1)
        page = r.get_data(as_text=True)
        self.assertIn(f'<script nonce="{nonce}">', page)
        self.assertNotIn("&#34;", page.split("<script")[0])  # CSS isn't HTML-escaped
        self.assertNotIn("X-Admin-Token", page)

    def test_default_language(self):
        tl = portal.create_app(self.fake, self.arp, language="tl").test_client().get("/")
        self.assertIn('data-default-lang="tl"', tl.get_data(as_text=True))
        odd = portal.create_app(self.fake, self.arp, language="xx").test_client().get("/")
        self.assertIn('data-default-lang="en"', odd.get_data(as_text=True))

    def test_page_shows_the_name_set_on_the_dashboard(self):
        self.fake.reply = (200, {"ok": True, "title": "Aling Nena's WiFi"})
        page = self.get("/").get_data(as_text=True)
        self.assertIn("<h1>Aling Nena&#39;s WiFi</h1>", page)
        self.assertIn("<title>Aling Nena&#39;s WiFi</title>", page)

    def test_name_falls_back_when_the_credit_service_is_down(self):
        self.fake.down = True
        self.assertIn("<h1>Piso WiFi</h1>", self.get("/").get_data(as_text=True))

    def test_name_is_cached(self):
        clock = [100.0]
        cache = portal.TitleCache(self.fake, "Piso WiFi", ttl=30, clock=lambda: clock[0])
        self.fake.reply = (200, {"title": "One"})
        self.assertEqual(cache.get(), "One")
        self.fake.reply = (200, {"title": "Two"})
        clock[0] += 10
        self.assertEqual(cache.get(), "One")
        clock[0] += 25
        self.assertEqual(cache.get(), "Two")
        self.assertEqual(len(self.fake.calls), 2)

    def test_unknown_api_paths(self):
        self.assertEqual(self.get("/api/nope").status_code, 404)


class FakeNds:
    def __init__(self):
        self.authed = {}

    def run(self, argv, **kw):
        cmd, mac = argv[1], argv[2]
        if cmd == "auth":
            if mac in self.authed:
                return subprocess.CompletedProcess(argv, 1, f"Failed to authenticate client {mac}.\n", "")
            self.authed[mac] = int(argv[3])
            return subprocess.CompletedProcess(argv, 0, f"Client {mac} authenticated.\n", "")
        self.authed.pop(mac, None)
        return subprocess.CompletedProcess(argv, 0, f"Client {mac} deauthenticated.\n", "")


class EndToEndTests(PortalTest):
    """Portal -> real pisowifi-creditd over HTTP, with ndsctl faked."""

    def setUp(self):
        super().setUp()
        import creditd
        from werkzeug.serving import make_server

        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.nds = FakeNds()
        token = "t" * 32
        for target, name, value in ((creditd, "DB", str(Path(tmp.name) / "credits.sqlite3")),
                                    (creditd, "TOKEN", token), (creditd, "MINUTES", 5),
                                    (creditd, "ENABLE_NDS", True), (creditd.subprocess, "run", self.nds.run)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        server = make_server("127.0.0.1", 0, creditd.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.creditd = creditd.app.test_client()
        self.token = token
        real = portal.CreditdClient(f"http://127.0.0.1:{server.server_port}", token)
        self.client = portal.create_app(real, self.arp).test_client()

    def drop_coin(self, pesos):
        self.creditd.post("/api/coin", json={"coin_id": str(uuid.uuid4()), "pesos": pesos, "pulses": pesos},
                          headers={"X-Admin-Token": self.token})

    def test_insert_coins_then_go_online(self):
        self.assertEqual(self.post("/api/insert-coin").status_code, 200)
        self.drop_coin(5)
        self.drop_coin(10)
        state = self.get("/api/state").get_json()
        self.assertEqual((state["window"]["state"], state["window"]["pesos"], state["credit_minutes"]),
                         ("mine", 15, 75))
        r = self.post("/api/connect")
        self.assertEqual((r.status_code, r.get_json()["minutes_added"]), (200, 75))
        self.assertEqual(self.nds.authed, {PHONE_MAC: 75})
        state = self.get("/api/state").get_json()
        self.assertTrue(state["session"]["active"])
        self.assertEqual(state["credit_minutes"], 0)

    def test_voucher_then_pause_and_resume(self):
        code = self.creditd.post("/api/vouchers", json={"count": 1, "minutes": 60},
                                 headers={"X-Admin-Token": self.token}).get_json()["codes"][0]
        r = self.post("/api/redeem", json={"code": code})
        self.assertEqual((r.status_code, r.get_json()["started"]), (200, True))
        self.assertEqual(self.nds.authed, {PHONE_MAC: 60})
        self.assertEqual(self.post("/api/pause").status_code, 200)
        self.assertNotIn(PHONE_MAC, self.nds.authed)
        self.assertTrue(self.get("/api/state").get_json()["session"]["paused"])
        self.assertEqual(self.post("/api/resume").status_code, 200)
        self.assertIn(PHONE_MAC, self.nds.authed)


if __name__ == "__main__":
    unittest.main()
