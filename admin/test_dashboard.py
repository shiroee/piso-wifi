"""Tests for dashboard.py. Run with the service venv (it has Flask):
    cd admin && /opt/pisowifi-venv/bin/python -m unittest test_dashboard
"""

import logging
import os
import re
import stat
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from werkzeug.security import generate_password_hash

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "app"))

import dashboard  # noqa: E402

PASSWORD = "correct horse"
CANNED = {
    "/health": {"ok": True},
    "/api/status": {"ok": True, "minutes_per_peso": 5},
    "/api/sales?days=14": {"ok": True, "totals": {"today": {"pesos": 25, "coins": 3}}},
    "/api/coins?limit=15": {"ok": True, "coins": []},
    "/api/settings": {"ok": True, "settings": {"minutes_per_peso": 5}},
    "/api/clients": {"ok": True, "clients": []},
    "/api/promos": {"ok": True, "promos": [], "rate_card": [{"pesos": 1, "minutes": 5}]},
    "/api/voucher-batches": {"ok": True, "batches": []},
    "/api/appearance": {"ok": True, "title": "Aling Nena's WiFi", "dashboard_mode": "dark", "dashboard_accent": "blue"},
    "/api/vouchers?batch=20261016-ABCD": {"ok": True, "batch": "20261016-ABCD", "vouchers": [
        {"code": "ABCD2345", "minutes": 90, "used_by": "", "void": 0},
        {"code": "EFGH6789", "minutes": 90, "used_by": "00:1f:fb:bb:25:bc", "void": 0},
        {"code": "JKLM2345", "minutes": 90, "used_by": "", "void": 1},
    ]},
}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.fail = None  # None, "down", or an HTTP status for every call

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.fail == "down":
            raise dashboard.CreditdUnavailable("connection refused")
        if self.fail:
            return self.fail, {"error": "nope"}
        if method == "POST":
            return 200, {"ok": True, "settings": body}
        return 200, CANNED[path]


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class DashboardTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.password_file = self.dir / "dashboard.passwd"
        self.password_file.write_text(generate_password_hash(PASSWORD))
        self.secret_file = self.dir / "state" / "dashboard.secret"
        self.fake = FakeClient()
        self.clock = Clock()
        self.app = self.make_app()
        self.client = self.app.test_client()

    def make_app(self, client=None):
        return dashboard.create_app(client or self.fake, self.password_file, self.secret_file, self.clock)

    def login(self, password=PASSWORD, client=None):
        return (client or self.client).post("/login", data={"password": password})

    def csrf(self):
        page = self.client.get("/").get_data(as_text=True)
        return re.search(r'name="csrf" content="([^"]+)"', page).group(1)


class LoginTests(DashboardTest):
    def test_everything_needs_login(self):
        self.assertEqual(self.client.get("/").headers["Location"], "/login")
        self.assertEqual(self.client.get("/api/overview").status_code, 401)
        self.assertEqual(self.client.post("/api/settings", json={}).status_code, 401)
        self.assertEqual(self.fake.calls, [])

    def test_login_opens_the_dashboard(self):
        r = self.login()
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/"))
        self.assertIn("pisowifi_admin=", r.headers["Set-Cookie"])
        self.assertIn("HttpOnly", r.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", r.headers["Set-Cookie"])
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_lockout_after_five_wrong_passwords(self):
        for _ in range(5):
            self.assertEqual(self.login("wrong").status_code, 401)
        self.assertEqual(self.login().status_code, 429)  # even the right one
        self.clock.now += dashboard.LOCKOUT_SECONDS
        self.assertEqual(self.login().status_code, 302)

    def test_a_correct_login_resets_the_count(self):
        for _ in range(4):
            self.login("wrong")
        self.login()
        for _ in range(4):
            self.assertEqual(self.login("wrong").status_code, 401)

    def test_no_password_yet_shows_how_to_set_one(self):
        self.password_file.unlink()
        page = self.client.get("/login").get_data(as_text=True)
        self.assertIn("set-password", page)
        self.assertEqual(self.login().status_code, 503)

    def test_logout_needs_the_page_token(self):
        self.login()
        csrf = self.csrf()
        self.client.post("/logout", data={})
        self.assertEqual(self.client.get("/").status_code, 200)  # still logged in
        self.client.post("/logout", data={"csrf": csrf})
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_login_survives_a_dashboard_restart(self):
        self.login()
        cookie = self.client.get_cookie("pisowifi_admin").value
        restarted = self.make_app().test_client()
        restarted.set_cookie("pisowifi_admin", cookie)
        self.assertEqual(restarted.get("/").status_code, 200)
        self.assertEqual(stat.S_IMODE(os.stat(self.secret_file).st_mode), 0o600)


class PageTests(DashboardTest):
    def test_security_headers_and_nonce(self):
        self.login()
        r = self.client.get("/")
        csp = r.headers["Content-Security-Policy"]
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        page = r.get_data(as_text=True)
        self.assertIn(f'<script nonce="{nonce}">', page)
        self.assertIn(f'<style nonce="{nonce}">', page)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("&#34;", page.split("<script")[0])  # CSS isn't HTML-escaped

    def test_overview_collects_every_section(self):
        self.login()
        data = self.client.get("/api/overview").get_json()
        self.assertEqual(data["sales"]["totals"]["today"]["pesos"], 25)
        self.assertEqual(data["errors"], {})
        self.assertEqual(data["now"], int(self.clock.now))

    def test_credit_service_down(self):
        self.login()
        self.fake.fail = "down"
        data = self.client.get("/api/overview").get_json()
        self.assertEqual(data["errors"], {"creditd": dashboard.UNREACHABLE})

    def test_wrong_admin_token(self):
        self.login()
        self.fake.fail = 401
        self.assertEqual(self.client.get("/api/overview").get_json()["errors"]["creditd"], dashboard.TOKEN_REJECTED)

    def test_one_failing_section_does_not_hide_the_rest(self):
        self.login()
        real_call = self.fake.call

        def call(method, path, body=None):
            if path == "/api/clients":
                return 502, {"error": "openNDS is not running"}
            return real_call(method, path, body)

        self.fake.call = call
        data = self.client.get("/api/overview").get_json()
        self.assertEqual(data["errors"], {"clients": "openNDS is not running"})
        self.assertIn("sales", data)


class SettingsTests(DashboardTest):
    def test_settings_need_the_page_token(self):
        self.login()
        r = self.client.post("/api/settings", json={"minutes_per_peso": 6})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.fake.calls, [])

    def test_only_known_settings_are_forwarded(self):
        self.login()
        csrf = self.csrf()
        r = self.client.post("/api/settings", json={"minutes_per_peso": 6, "evil": 1},
                             headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fake.calls[-1], ("POST", "/api/settings", {"minutes_per_peso": 6}))

    def test_credit_service_errors_are_passed_on(self):
        self.login()
        csrf = self.csrf()
        self.fake.fail = 400
        r = self.client.post("/api/settings", json={"minutes_per_peso": 0}, headers={"X-CSRF-Token": csrf})
        self.assertEqual((r.status_code, r.get_json()["error"]), (400, "nope"))


class OwnerActionTests(DashboardTest):
    def test_actions_need_the_page_token(self):
        self.login()
        for path in dashboard.FORWARDS:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}).status_code, 403)
        self.assertEqual(self.fake.calls, [])

    def test_only_allowed_fields_are_forwarded(self):
        self.login()
        csrf = self.csrf()
        body = {"count": 5, "minutes": 60, "price": 10, "used_by": "x", "code": "FREE"}
        self.client.post("/api/vouchers", json=body, headers={"X-CSRF-Token": csrf})
        self.assertEqual(self.fake.calls[-1], ("POST", "/api/vouchers", {"count": 5, "minutes": 60, "price": 10}))
        self.client.post("/api/coins/assign", json={"coin_id": "c", "mac": "m", "pesos": 99}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(self.fake.calls[-1], ("POST", "/api/coins/assign", {"coin_id": "c", "mac": "m"}))

    def test_print_page_shows_only_unused_codes(self):
        self.assertEqual(self.client.get("/vouchers/20261016-ABCD/print").status_code, 302)  # login first
        self.login()
        page = self.client.get("/vouchers/20261016-ABCD/print").get_data(as_text=True)
        self.assertIn("ABCD-2345", page)
        self.assertNotIn("EFGH-6789", page)  # used
        self.assertNotIn("JKLM-2345", page)  # cancelled
        self.assertIn("1 hr 30 min of internet", page)

    def test_time_text(self):
        self.assertEqual([dashboard.time_text(m) for m in (5, 60, 90, 125)], ["5 min", "1 hr", "1 hr 30 min", "2 hr 5 min"])
        self.assertEqual([dashboard.time_text(m) for m in (1440, 1500, 4320, 44640, 0)],
                         ["1 day", "1 day 1 hr", "3 days", "31 days", "0 min"])


class LookTests(DashboardTest):
    def test_pages_use_the_saved_name_and_theme(self):
        login = self.client.get("/login").get_data(as_text=True)
        self.assertIn('data-theme="dark" data-accent="blue"', login)
        self.assertIn("Aling Nena&#39;s WiFi", login)
        self.login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-theme="dark" data-accent="blue"', page)
        self.assertIn('<h1 id="brand-title">Aling Nena&#39;s WiFi</h1>', page)
        voucher_page = self.client.get("/vouchers/20261016-ABCD/print").get_data(as_text=True)
        self.assertIn("Aling Nena&#39;s WiFi", voucher_page)

    def test_defaults_when_the_credit_service_is_down(self):
        self.fake.fail = "down"
        page = self.client.get("/login").get_data(as_text=True)
        self.assertIn('data-theme="auto" data-accent="gold"', page)
        self.assertIn("Piso WiFi", page)

    def test_appearance_is_forwarded_with_only_its_fields(self):
        self.login()
        r = self.client.post("/api/appearance", json={"title": "X", "dashboard_mode": "light", "admin": True},
                             headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fake.calls[-1], ("POST", "/api/appearance", {"title": "X", "dashboard_mode": "light"}))


class PasswordToolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "dashboard.passwd"

    def run_tool(self, *answers):
        with mock.patch.object(dashboard.getpass, "getpass", side_effect=answers), \
             mock.patch("builtins.print"):
            return dashboard.set_password_cli(self.path)

    def test_saves_a_private_hash(self):
        self.assertEqual(self.run_tool("long enough", "long enough"), 0)
        stored = self.path.read_text().strip()
        self.assertNotIn("long enough", stored)
        self.assertTrue(dashboard.check_password_hash(stored, "long enough"))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_refuses_short_or_mismatched_passwords(self):
        self.assertEqual(self.run_tool("short"), 1)
        self.assertEqual(self.run_tool("long enough", "different!"), 1)
        self.assertFalse(self.path.exists())


class EndToEndTests(DashboardTest):
    """Dashboard -> real pisowifi-creditd over HTTP, with a throwaway database."""

    def setUp(self):
        super().setUp()
        import creditd
        from werkzeug.serving import make_server

        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        token = "t" * 32
        for name, value in (("DB", str(self.dir / "credits.sqlite3")), ("TOKEN", token), ("MINUTES", 5)):
            patcher = mock.patch.object(creditd, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        server = make_server("127.0.0.1", 0, creditd.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.creditd = creditd.app.test_client()
        self.token = token
        real = dashboard.CreditdClient(f"http://127.0.0.1:{server.server_port}", token)
        self.client = self.make_app(real).test_client()

    def test_coins_show_up_and_settings_are_saved(self):
        headers = {"X-Admin-Token": self.token}
        self.creditd.post("/api/coin", json={"coin_id": str(uuid.uuid4()), "pesos": 20, "pulses": 20}, headers=headers)
        self.login()
        data = self.client.get("/api/overview").get_json()
        self.assertEqual(data["sales"]["totals"]["today"]["pesos"], 20)
        self.assertEqual(data["coins"]["coins"][0]["minutes"], 100)
        self.assertNotIn("creditd", data["errors"])
        r = self.client.post("/api/settings", json={"minutes_per_peso": 6}, headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.get_json()["settings"]["minutes_per_peso"], 6)
        self.assertEqual(self.creditd.get("/api/status", headers=headers).get_json()["minutes_per_peso"], 6)

    def test_vouchers_promos_and_giving_a_coin(self):
        self.login()
        csrf = {"X-CSRF-Token": self.csrf()}
        made = self.client.post("/api/vouchers", json={"count": 3, "minutes": 60, "price": 10}, headers=csrf).get_json()
        page = self.client.get(f"/vouchers/{made['batch']}/print").get_data(as_text=True)
        for code in made["codes"]:
            self.assertIn(f"{code[:4]}-{code[4:]}", page)
        r = self.client.post("/api/promos", json={"promos": [{"pesos": 10, "minutes": 70}]}, headers=csrf)
        self.assertEqual(r.get_json()["rate_card"][2], {"pesos": 10, "minutes": 70})
        headers = {"X-Admin-Token": self.token}
        self.creditd.post("/api/coin", json={"coin_id": str(uuid.uuid4()), "pesos": 5, "pulses": 5}, headers=headers)
        coin_id = self.client.get("/api/overview").get_json()["coins"]["coins"][0]["coin_id"]
        r = self.client.post("/api/coins/assign", json={"coin_id": coin_id, "mac": "00:1f:fb:bb:25:bc"}, headers=csrf)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/overview").get_json()["sales"]["unassigned"]["pesos"], 0)

    def test_saving_the_name_and_theme(self):
        self.login()
        r = self.client.post("/api/appearance", json={"title": "Barangay WiFi", "dashboard_mode": "light",
                                                       "dashboard_accent": "green"}, headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.get_json()["title"], "Barangay WiFi")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-theme="light" data-accent="green"', page)
        self.assertIn("Barangay WiFi", page)


if __name__ == "__main__":
    unittest.main()
