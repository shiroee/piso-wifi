#!/usr/bin/env python3
"""Owner dashboard for the Piso WiFi machine.

Shows coin takings, the coin mix, connected devices and recent coins, and lets
the owner change the rate and Insert Coin timings. It is a thin layer over
pisowifi-creditd: every number comes from creditd's admin API on localhost,
and the admin token stays on the Pi (the browser only gets a login cookie).

Open it from the owner's home network: http://<Pi WAN address>:8091
Customers on the Piso WiFi LAN are blocked from this port by pisowifi-guard.

    dashboard.py                  # run the dashboard
    dashboard.py set-password     # set or change the login password
"""

from __future__ import annotations

import argparse
import getpass
import hmac
import http.client
import json
import os
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template_string, request, session
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

CREDITD_URL = f"http://127.0.0.1:{os.environ.get('PISOWIFI_PORT') or '8090'}"
ADMIN_TOKEN = os.environ.get("PISOWIFI_ADMIN_TOKEN", "")
BIND = os.environ.get("PISOWIFI_DASHBOARD_BIND", "0.0.0.0")
PORT = int(os.environ.get("PISOWIFI_DASHBOARD_PORT", "8091"))
PASSWORD_FILE = Path(os.environ.get("PISOWIFI_DASHBOARD_PASSWORD_FILE", "/etc/pisowifi-dashboard.passwd"))
SECRET_FILE = Path(os.environ.get("PISOWIFI_DASHBOARD_SECRET_FILE", "/var/lib/pisowifi/dashboard.secret"))
PORTAL_NAME = os.environ.get("PISOWIFI_PORTAL_NAME", "Piso WiFi")
PORTAL_ADDRESS = os.environ.get("PISOWIFI_PORTAL_ADDRESS", "") or "http://10.20.0.1:2080"

MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
MIN_PASSWORD_LENGTH = 8
DEFAULT_LOOK = {"title": PORTAL_NAME, "dashboard_mode": "auto", "dashboard_accent": "gold"}
SETTING_NAMES = ("minutes_per_peso", "coin_window_seconds", "coin_grace_seconds", "download_kbps", "upload_kbps")
# Dashboard POST routes passed through to creditd, with the only fields each may carry.
FORWARDS = {
    "/api/promos": ("promos",),
    "/api/vouchers": ("count", "minutes", "price"),
    "/api/vouchers/void": ("batch",),
    "/api/coins/assign": ("coin_id", "mac"),
    "/api/appearance": ("title", "dashboard_mode", "dashboard_accent"),
}

UNREACHABLE = (
    "Can't reach the credit service on 127.0.0.1. "
    "On the Pi, check it with: systemctl status pisowifi-creditd"
)
TOKEN_REJECTED = (
    "The credit service rejected the admin token. "
    "Check PISOWIFI_ADMIN_TOKEN in /etc/pisowifi.env, then restart pisowifi-dashboard."
)
SET_PASSWORD_HINT = "sudo /opt/pisowifi-venv/bin/python /opt/pisowifi/dashboard.py set-password"


# ---------------------------------------------------------------------------
# Credit service client
# ---------------------------------------------------------------------------


class CreditdUnavailable(Exception):
    """The credit service could not be reached at all."""


class CreditdClient:
    def __init__(self, url: str, token: str, timeout: float = 5.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        request_ = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={"X-Admin-Token": self.token, "Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request_, timeout=self.timeout) as response:
                return response.status, _json_object(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _json_object(exc.read())
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            raise CreditdUnavailable(str(exc)) from exc


def time_text(minutes: int) -> str:
    days, rest = divmod(int(minutes), 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} day" + ("s" if days != 1 else ""))
    if hours:
        parts.append(f"{hours} hr")
    if mins or not parts:
        parts.append(f"{mins} min")
    return " ".join(parts)


def _json_object(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Password, session secret and login throttling
# ---------------------------------------------------------------------------


def read_password_hash(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def write_private_file(path: Path, text: str) -> None:
    """Atomically write a root-only (0600) file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_or_create_secret(path: Path) -> str:
    """Session-signing key, kept on disk so logins survive a restart."""
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    except FileNotFoundError:
        pass
    value = secrets.token_hex(32)
    write_private_file(path, value)
    return value


class LoginLimiter:
    """Locks an address out after MAX_FAILURES wrong passwords in a row."""

    def __init__(self, max_failures: int = MAX_FAILURES, lockout: int = LOCKOUT_SECONDS) -> None:
        self.max_failures = max_failures
        self.lockout = lockout
        self._failures: dict[str, tuple[int, float]] = {}

    def seconds_locked(self, address: str, now: float) -> int:
        count, last = self._failures.get(address, (0, 0.0))
        if count < self.max_failures:
            return 0
        remaining = int(last + self.lockout - now)
        if remaining <= 0:
            self._failures.pop(address, None)
            return 0
        return remaining

    def failed(self, address: str, now: float) -> None:
        count, _ = self._failures.get(address, (0, 0.0))
        self._failures[address] = (count + 1, now)

    def succeeded(self, address: str) -> None:
        self._failures.pop(address, None)


def set_password_cli(path: Path) -> int:
    first = getpass.getpass("New dashboard password: ")
    if len(first) < MIN_PASSWORD_LENGTH:
        print(f"Use at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Type it again: ") != first:
        print("The passwords didn't match. Nothing was changed.", file=sys.stderr)
        return 1
    write_private_file(path, generate_password_hash(first))
    print(f"Password saved to {path}. Restart the dashboard: systemctl restart pisowifi-dashboard")
    return 0


# ---------------------------------------------------------------------------
# Web app
# ---------------------------------------------------------------------------


def create_app(
    client: CreditdClient | None = None,
    password_file: Path = PASSWORD_FILE,
    secret_file: Path = SECRET_FILE,
    clock=time.time,
) -> Flask:
    app = Flask(__name__)
    app.secret_key = load_or_create_secret(secret_file)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_NAME="pisowifi_admin",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    client = client or CreditdClient(CREDITD_URL, ADMIN_TOKEN)
    limiter = LoginLimiter()

    @app.before_request
    def make_nonce():
        g.nonce = secrets.token_urlsafe(16)

    @app.after_request
    def security_headers(response):
        nonce = getattr(g, "nonce", "")
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self' data:; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def logged_in() -> bool:
        return session.get("admin") is True

    def csrf_ok() -> bool:
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
        expected = session.get("csrf", "")
        return bool(expected) and hmac.compare_digest(sent, expected)

    def require_login(api: bool):
        def decorator(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                if not logged_in():
                    if api:
                        return jsonify(error="Log in again."), 401
                    return redirect("/login")
                return fn(*args, **kwargs)
            return wrapped
        return decorator

    def look() -> dict:
        """The saved name and theme, or the defaults if the credit service is down."""
        try:
            code, body = client.call("GET", "/api/appearance")
        except CreditdUnavailable:
            return dict(DEFAULT_LOOK)
        if code != 200:
            return dict(DEFAULT_LOOK)
        return {k: body.get(k) or v for k, v in DEFAULT_LOOK.items()}

    def render_login(error: str = "", status: int = 200):
        needs_password = read_password_hash(password_file) is None
        page = render_template_string(
            LOGIN_HTML, nonce=g.nonce, css=Markup(BASE_CSS), error=error, look=look(),
            needs_password=needs_password, hint=SET_PASSWORD_HINT,
        )
        return page, status

    @app.get("/login")
    def login_form():
        if logged_in():
            return redirect("/")
        return render_login()

    @app.post("/login")
    def login():
        now = clock()
        address = request.remote_addr or "unknown"
        locked = limiter.seconds_locked(address, now)
        if locked:
            minutes = max(1, (locked + 59) // 60)
            return render_login(f"Too many wrong passwords. Try again in {minutes} min.", 429)
        stored = read_password_hash(password_file)
        if stored is None:
            return render_login("", 503)
        if not check_password_hash(stored, request.form.get("password", "")):
            limiter.failed(address, now)
            return render_login("Wrong password.", 401)
        limiter.succeeded(address)
        session.clear()  # new session on login
        session.permanent = True
        session["admin"] = True
        session["csrf"] = secrets.token_urlsafe(32)
        return redirect("/")

    @app.post("/logout")
    def logout():
        if logged_in() and csrf_ok():
            session.clear()
        return redirect("/login")

    @app.get("/")
    @require_login(api=False)
    def dashboard():
        return render_template_string(
            DASHBOARD_HTML, nonce=g.nonce, css=Markup(BASE_CSS + DASHBOARD_CSS),
            js=Markup(DASHBOARD_JS), csrf=session["csrf"], look=look(),
        )

    @app.get("/api/overview")
    @require_login(api=True)
    def overview():
        out: dict = {"now": int(clock()), "errors": {}}
        sections = {
            "health": "/health",
            "status": "/api/status",
            "sales": "/api/sales?days=14",
            "coins": "/api/coins?limit=15",
            "settings": "/api/settings",
            "clients": "/api/clients",
            "promos": "/api/promos",
            "vouchers": "/api/voucher-batches",
            "appearance": "/api/appearance",
        }
        for name, path in sections.items():
            try:
                code, body = client.call("GET", path)
            except CreditdUnavailable:
                out["errors"]["creditd"] = UNREACHABLE
                break
            if code == 401:
                out["errors"]["creditd"] = TOKEN_REJECTED
                break
            if code != 200:
                out["errors"][name] = body.get("error") or f"The credit service answered HTTP {code}."
                continue
            out[name] = body
        return jsonify(out)

    @app.post("/api/settings")
    @require_login(api=True)
    def save_settings():
        if not csrf_ok():
            return jsonify(error="This page is out of date. Reload it and try again."), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Send the settings as JSON."), 400
        changes = {k: data[k] for k in SETTING_NAMES if k in data}
        try:
            code, body = client.call("POST", "/api/settings", changes)
        except CreditdUnavailable:
            return jsonify(error=UNREACHABLE), 502
        if code == 401:
            return jsonify(error=TOKEN_REJECTED), 502
        return jsonify(body), code

    def make_forward(path: str, fields: tuple):
        @require_login(api=True)
        def forward():
            if not csrf_ok():
                return jsonify(error="This page is out of date. Reload it and try again."), 403
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify(error="Send the request as JSON."), 400
            try:
                code, body = client.call("POST", path, {k: data[k] for k in fields if k in data})
            except CreditdUnavailable:
                return jsonify(error=UNREACHABLE), 502
            if code == 401:
                return jsonify(error=TOKEN_REJECTED), 502
            return jsonify(body), code
        return forward

    for index, (path, fields) in enumerate(FORWARDS.items()):
        app.add_url_rule(path, endpoint=f"forward_{index}", view_func=make_forward(path, fields), methods=["POST"])

    @app.get("/vouchers/<batch>/print")
    @require_login(api=False)
    def print_vouchers(batch):
        try:
            code, body = client.call("GET", "/api/vouchers?" + urllib.parse.urlencode({"batch": batch}))
        except CreditdUnavailable:
            return UNREACHABLE, 502, {"Content-Type": "text/plain; charset=utf-8"}
        if code != 200:
            return body.get("error", "Couldn't load that batch."), code, {"Content-Type": "text/plain; charset=utf-8"}
        cards = [{"code": f"{v['code'][:4]}-{v['code'][4:]}", "time": time_text(v["minutes"])}
                 for v in body["vouchers"] if not v["used_by"] and not v["void"]]
        return render_template_string(
            PRINT_HTML, nonce=g.nonce, batch=batch, cards=cards, name=look()["title"], address=PORTAL_ADDRESS,
        )

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    return app


# ---------------------------------------------------------------------------
# Page templates. Everything is inline so the dashboard works with no internet
# and under a strict Content-Security-Policy (nonce-only scripts and styles).
# ---------------------------------------------------------------------------

BASE_CSS = r"""
:root {
  color-scheme: dark;
  --bg: #0D1430; --glow: #1F2A6B; --surface: #141C40; --surface-2: #1C2552; --line: rgba(255, 255, 255, 0.09);
  --text: #F3F5FF; --muted: #A3ABD0; --gold: #F5B83D; --gold-hi: #FFE29A; --gold-deep: #C98408; --gold-ink: #3A2600;
  --mint: #3DDC97; --danger: #FF8E7E; --danger-bg: rgba(255, 142, 126, 0.12); --focus: #8FB4FF; --bar: #34417E;
  --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif;
}
/* Light: chosen on the dashboard, or "auto" on a device set to light. */
:root[data-theme="light"] {
  color-scheme: light;
  --bg: #F2F4FD; --glow: #DFE5FF; --surface: #FFFFFF; --surface-2: #EEF1FA; --line: #DDE2F1;
  --text: #121A3D; --muted: #5A6388; --mint: #128A57; --danger: #B23A2B; --danger-bg: #FDECE9;
  --focus: #2F5FD0; --bar: #C9D1EE;
}
@media (prefers-color-scheme: light) {
  :root[data-theme="auto"] {
    color-scheme: light;
    --bg: #F2F4FD; --glow: #DFE5FF; --surface: #FFFFFF; --surface-2: #EEF1FA; --line: #DDE2F1;
    --text: #121A3D; --muted: #5A6388; --mint: #128A57; --danger: #B23A2B; --danger-bg: #FDECE9;
    --focus: #2F5FD0; --bar: #C9D1EE;
  }
}
/* Accent colour presets (the coin, buttons, today's bar). */
:root[data-accent="blue"] { --gold: #3B7BF0; --gold-hi: #A9C8FF; --gold-deep: #1F4FC0; --gold-ink: #FFFFFF; }
:root[data-accent="green"] { --gold: #2FBF71; --gold-hi: #A6EDC6; --gold-deep: #17824A; --gold-ink: #042213; }
:root[data-accent="purple"] { --gold: #8F5CF6; --gold-hi: #CDB6FF; --gold-deep: #5F35C7; --gold-ink: #FFFFFF; }
:root[data-accent="red"] { --gold: #F0524B; --gold-hi: #FFB3AE; --gold-deep: #B82E28; --gold-ink: #FFFFFF; }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; min-height: 100vh; color: var(--text); font: 15.5px/1.5 var(--font); font-variant-numeric: tabular-nums;
  background: radial-gradient(90% 45% at 50% 0%, var(--glow) 0%, var(--bg) 70%) fixed, var(--bg);
}
h1, h2, h3 { line-height: 1.2; margin: 0; }
a { color: inherit; }
button, input { font: inherit; color: inherit; }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
[hidden] { display: none !important; }
.button {
  border: 0; border-radius: 999px; padding: 0.65rem 1.3rem; cursor: pointer; font-weight: 700; color: var(--gold-ink);
  background: linear-gradient(180deg, var(--gold-hi) -60%, var(--gold) 55%, var(--gold-deep) 170%);
}
.button:disabled { opacity: 0.6; cursor: progress; }
.quiet {
  background: var(--surface-2); color: var(--text); border: 1px solid var(--line);
  border-radius: 999px; padding: 0.4rem 0.95rem; cursor: pointer; font-weight: 600; font-size: 0.9rem;
}
.notice { background: var(--danger-bg); color: var(--danger); padding: 0.8rem 1rem; border-radius: 14px; margin: 0; }
.mark {
  display: grid; place-items: center; width: 2.4rem; height: 2.4rem; border-radius: 50%; flex: none;
  color: var(--gold-ink); font-weight: 850; font-size: 1.25rem;
  background: radial-gradient(circle at 34% 28%, var(--gold-hi) 0, var(--gold) 38%, var(--gold-deep) 90%);
  box-shadow: inset 0 0 0 3px rgba(255, 255, 255, 0.25);
}
.sr-only {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap;
}
"""

LOGIN_HTML = r"""<!doctype html>
<html lang="en" data-theme="{{ look.dashboard_mode }}" data-accent="{{ look.dashboard_accent }}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0D1430">
<link rel="icon" href="data:,">
<title>{{ look.title }} admin</title>
<style nonce="{{ nonce }}">{{ css }}
.login { max-width: 23rem; margin: 16vh auto 0; padding: 0 1.25rem; }
.login .card { background: var(--surface); border: 1px solid var(--line); border-radius: 24px; padding: 1.75rem 1.5rem; }
.login .brand { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1.5rem; }
.login h1 { font-size: 1.3rem; }
.login .brand p { margin: 0; color: var(--muted); font-size: 0.9rem; }
.login label { display: block; font-weight: 650; margin-bottom: 0.45rem; }
.login input {
  width: 100%; padding: 0.8rem 1rem; border-radius: 14px; border: 1px solid var(--line);
  background: var(--surface-2); margin-bottom: 1.1rem;
}
.login .button { width: 100%; padding: 0.85rem; }
.login .notice { margin-bottom: 1rem; }
.login code { overflow-wrap: anywhere; }
</style>
</head>
<body>
<main class="login">
  <div class="card">
    <div class="brand"><span class="mark" aria-hidden="true">₱</span><div><h1 id="brand-title">{{ look.title }}</h1><p>Owner dashboard</p></div></div>
    {% if needs_password %}
      <p class="notice">No password is set yet. On the Pi, run:<br><code>{{ hint }}</code></p>
    {% else %}
      {% if error %}<p class="notice" role="alert">{{ error }}</p>{% endif %}
      <form method="post" action="/login">
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
        <button class="button" type="submit">Log in</button>
      </form>
    {% endif %}
  </div>
</main>
</body>
</html>
"""

PRINT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Vouchers {{ batch }}</title>
<style nonce="{{ nonce }}">
body { margin: 0; background: #fff; color: #111; font: 15px/1.4 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.bar { display: flex; flex-wrap: wrap; gap: 1rem; align-items: center; padding: 1rem; border-bottom: 1px solid #ddd; }
.bar button { border: 0; border-radius: 999px; padding: 0.6rem 1.2rem; background: #1A2433; color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
.sheet { display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr)); gap: 0.6rem; padding: 1rem; }
.card { border: 1px dashed #888; border-radius: 10px; padding: 0.8rem 1rem; break-inside: avoid; }
.name { font-weight: 700; }
.code { font-size: 1.7rem; font-weight: 800; letter-spacing: 0.12em; margin: 0.25rem 0; font-variant-numeric: tabular-nums; }
.how { font-size: 0.8rem; color: #333; margin-top: 0.3rem; }
@media print { .bar { display: none; } .sheet { padding: 0; } }
</style>
</head>
<body>
<div class="bar">
  <button type="button" id="print">Print</button>
  <span>{{ cards|length }} unused vouchers in batch {{ batch }}. Used and cancelled ones aren't shown.</span>
</div>
<div class="sheet">
{% for card in cards %}
  <div class="card">
    <div class="name">{{ name }}</div>
    <div class="code">{{ card.code }}</div>
    <div>{{ card.time }} of internet</div>
    <div class="how">Join the Wi-Fi, open {{ address }}, tap "Have a voucher code?" and type the code.</div>
  </div>
{% endfor %}
</div>
<script nonce="{{ nonce }}">document.getElementById("print").addEventListener("click", () => window.print());</script>
</body>
</html>
"""

DASHBOARD_CSS = r"""
.page { max-width: 68rem; margin: 0 auto; padding: 1rem 1.25rem 4rem; }
.top { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem 1.25rem; margin-bottom: 1.25rem; }
.brand { display: flex; align-items: center; gap: 0.7rem; margin-right: auto; }
.brand h1 { font-size: 1.15rem; }
.brand p { margin: 0; color: var(--muted); font-size: 0.85rem; }
.health { display: flex; flex-wrap: wrap; gap: 0.4rem; font-size: 0.85rem; }
.health span {
  display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.3rem 0.75rem; border-radius: 999px;
  background: var(--surface); border: 1px solid var(--line); color: var(--muted);
}
.health span::before { content: ""; width: 0.5rem; height: 0.5rem; border-radius: 50%; background: var(--muted); }
.health .up { color: var(--text); }
.health .up::before { background: var(--mint); box-shadow: 0 0 0 3px rgba(61, 220, 151, 0.2); }
.health .down { color: var(--danger); }
.health .down::before { background: var(--danger); }
.updated { color: var(--muted); font-size: 0.82rem; }
.top form { margin: 0; }

.tabs {
  display: flex; gap: 0.3rem; padding: 0.3rem; margin-bottom: 1.5rem; overflow-x: auto;
  background: var(--surface); border: 1px solid var(--line); border-radius: 999px; scrollbar-width: none;
}
.tabs button {
  flex: 1 1 auto; border: 0; border-radius: 999px; padding: 0.55rem 0.65rem; background: transparent;
  color: var(--muted); font-weight: 650; cursor: pointer; white-space: nowrap;
}
.tabs button[aria-selected="true"] { background: var(--surface-2); color: var(--text); box-shadow: inset 0 0 0 1px var(--line); }
.badge {
  display: inline-block; min-width: 1.35rem; padding: 0 0.35rem; margin-left: 0.35rem; border-radius: 999px;
  background: var(--mint); color: #062A1B; font-size: 0.75rem; font-weight: 800; line-height: 1.35rem; text-align: center;
}
#problems:empty { display: none; }
#problems { display: grid; gap: 0.5rem; margin-bottom: 1.25rem; }

.card { background: var(--surface); border: 1px solid var(--line); border-radius: 22px; padding: 1.25rem 1.35rem; }
.grid { display: grid; gap: 1rem; grid-template-columns: minmax(0, 1fr); margin-bottom: 1rem; }
@media (min-width: 52rem) { .grid.two { grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); } }
.card h2 { font-size: 1rem; font-weight: 700; margin-bottom: 0.9rem; }
.card > .note { color: var(--muted); margin: -0.5rem 0 1rem; font-size: 0.9rem; }

.takings .label { color: var(--muted); font-weight: 650; }
.amount { font-size: clamp(3.4rem, 12vw, 5.6rem); font-weight: 800; letter-spacing: -0.035em; line-height: 1; margin: 0.35rem 0 0.5rem; }
.amount .sign { color: var(--gold); font-size: 0.55em; font-weight: 700; margin-right: 0.06em; vertical-align: 0.55em; }
.takings p { margin: 0; }
.today-detail { color: var(--muted); }
.periods { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.6rem; margin: 1.35rem 0 0; }
.periods div { background: var(--surface-2); border-radius: 16px; padding: 0.65rem 0.8rem; }
.periods dt { color: var(--muted); font-size: 0.78rem; }
.periods dd { margin: 0; font-size: 1.15rem; font-weight: 750; }
@media (max-width: 30rem) { .periods { grid-template-columns: repeat(2, minmax(0, 1fr)); } }

.stacks { display: grid; grid-template-columns: repeat(4, 1fr); align-items: end; gap: 0.5rem; min-height: 10rem; }
.stack { display: flex; flex-direction: column; align-items: center; }
.discs { display: flex; flex-direction: column-reverse; align-items: center; min-height: 6.5rem; justify-content: flex-start; }
.disc {
  height: 0.72rem; border-radius: 50%; margin-top: -0.3rem;
  background: linear-gradient(180deg, var(--gold-hi) 0 18%, var(--gold) 18% 55%, var(--gold-deep) 55% 100%);
  box-shadow: 0 1px 0 rgba(0, 0, 0, 0.25);
}
.disc.empty { background: none; border: 1px dashed var(--line); box-shadow: none; }
.stack .face { font-weight: 800; margin-top: 0.65rem; }
.stack .count { color: var(--muted); font-size: 0.82rem; }

.chart-note { color: var(--muted); font-size: 0.88rem; margin: -0.5rem 0 0.9rem; }
.bars { display: flex; align-items: flex-end; gap: 5px; height: 9rem; }
.bar { flex: 1 1 0; background: var(--bar); border-radius: 8px 8px 3px 3px; min-width: 0; }
.bar.today { background: linear-gradient(180deg, var(--gold-hi) -40%, var(--gold) 50%, var(--gold-deep) 160%); }
.bar-labels { display: flex; justify-content: space-between; color: var(--muted); font-size: 0.78rem; margin-top: 0.5rem; }
@media (prefers-reduced-motion: no-preference) { .bar { transition: height 300ms ease-out; } }

.rows { list-style: none; margin: 0; padding: 0; }
.rows li {
  display: grid; grid-template-columns: minmax(3.5rem, auto) minmax(0, 1fr) auto;
  gap: 0 1rem; align-items: center; padding: 0.8rem 0; border-top: 1px solid var(--line);
}
.rows li:first-child { border-top: 0; padding-top: 0; }
.rows li:last-child { padding-bottom: 0; }
.rows li.single { display: block; }
.rows .main { font-weight: 700; }
.rows .detail { color: var(--muted); }
.rows .sub { display: block; color: var(--muted); font-weight: 400; font-size: 0.85rem; }
.rows .right { text-align: right; }
#clients li { grid-template-columns: auto minmax(0, 1fr); }
#batches li { grid-template-columns: minmax(0, 1fr) auto; }
.row-action { grid-column: 1 / -1; justify-self: end; margin-top: 0.5rem; }
.empty-note { color: var(--muted); margin: 0; }
.mac { letter-spacing: 0.02em; }
.state-on { color: var(--mint); font-weight: 650; }

.fields { display: grid; gap: 1.1rem; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr)); margin-bottom: 1.1rem; }
.fields > div { display: grid; grid-template-rows: auto 1fr auto; }
.fields label { display: block; font-weight: 650; }
.fields .help { display: block; color: var(--muted); font-size: 0.84rem; margin-bottom: 0.45rem; }
.fields input, .promo-row input, .assign input {
  padding: 0.6rem 0.8rem; border-radius: 12px; border: 1px solid var(--line); background: var(--surface-2);
}
.fields input { width: 9rem; }
.preview { color: var(--muted); margin: 0 0 1rem; }
.save-row { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem; }
.save-row p { margin: 0; }
.error, #save-message.error, #promo-message.error, #v-message.error { color: var(--danger); }
.help-line { color: var(--muted); margin: -0.4rem 0 1rem; }
.promo-row { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; margin-bottom: 0.6rem; }
.promo-row input { width: 6.5rem; }
#promo-add { margin: 0.25rem 0 1rem; }
.actions { display: flex; flex-wrap: wrap; gap: 0.5rem; justify-content: flex-end; }
a.quiet { text-decoration: none; display: inline-block; }
.assign { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.7rem; }
.assign input { flex: 1; min-width: 12rem; }
.assign .error { width: 100%; margin: 0; }
#v-codes { margin: 1rem 0 0; }
.fields input.wide { width: 100%; max-width: 24rem; }
.choice { border: 0; padding: 0; margin: 0 0 1.1rem; display: flex; flex-wrap: wrap; gap: 0.5rem; }
.choice legend { font-weight: 650; padding: 0; margin-bottom: 0.5rem; }
.choice label {
  display: inline-flex; align-items: center; gap: 0.45rem; padding: 0.45rem 0.9rem; border-radius: 999px;
  border: 1px solid var(--line); background: var(--surface-2); cursor: pointer; font-weight: 600; font-size: 0.92rem;
}
.choice label:has(input:checked) { border-color: var(--gold); box-shadow: inset 0 0 0 1px var(--gold); }
.choice input { accent-color: var(--gold); margin: 0; }
.swatch::before { content: ""; width: 0.95rem; height: 0.95rem; border-radius: 50%; order: 1; }
.swatch input { order: 0; }
.swatch span { order: 2; }
.swatch[data-swatch="gold"]::before { background: #F5B83D; }
.swatch[data-swatch="blue"]::before { background: #3B7BF0; }
.swatch[data-swatch="green"]::before { background: #2FBF71; }
.swatch[data-swatch="purple"]::before { background: #8F5CF6; }
.swatch[data-swatch="red"]::before { background: #F0524B; }

/* Phones: last, so these win over the rules above. */
@media (max-width: 40rem) {
  .top { display: grid; grid-template-columns: minmax(0, 1fr) auto; }
  .top .health { grid-column: 1 / -1; order: 3; font-size: 0.78rem; }
  .health span { padding: 0.2rem 0.6rem; }
  .top .updated { grid-column: 1 / -1; order: 4; margin-top: -0.4rem; }
  .health { gap: 0.3rem; }
  .tabs { gap: 0.15rem; }
  .tabs button { font-size: 0.86rem; padding: 0.5rem 0.4rem; }
  .badge { margin-left: 0.2rem; }
  #batches li { grid-template-columns: minmax(0, 1fr); }
  #batches .actions { justify-content: flex-start; margin-top: 0.6rem; }
}
"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en" data-theme="{{ look.dashboard_mode }}" data-accent="{{ look.dashboard_accent }}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0D1430">
<meta name="csrf" content="{{ csrf }}">
<link rel="icon" href="data:,">
<title>{{ look.title }} admin</title>
<style nonce="{{ nonce }}">{{ css }}</style>
</head>
<body>
<div class="page">
  <header class="top">
    <div class="brand"><span class="mark" aria-hidden="true">₱</span><div><h1 id="brand-title">{{ look.title }}</h1><p>Owner dashboard</p></div></div>
    <div class="health" aria-live="polite">
      <span id="h-credit">Credit service</span>
      <span id="h-portal">Captive portal</span>
    </div>
    <span class="updated" id="updated">Loading…</span>
    <form method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf }}">
      <button class="quiet" type="submit">Log out</button></form>
  </header>

  <nav class="tabs" role="tablist" aria-label="Sections">
    <button role="tab" id="t-overview" aria-controls="tab-overview" type="button">Sales</button>
    <button role="tab" id="t-customers" aria-controls="tab-customers" type="button">Customers<span class="badge" id="online-badge" hidden></span></button>
    <button role="tab" id="t-vouchers" aria-controls="tab-vouchers" type="button">Vouchers</button>
    <button role="tab" id="t-settings" aria-controls="tab-settings" type="button">Settings</button>
  </nav>

  <div id="problems" role="alert"></div>

  <div id="tab-overview" role="tabpanel" aria-labelledby="t-overview">
    <div class="grid two">
      <section class="card takings" aria-labelledby="today-h">
        <h2 class="label" id="today-h">Today</h2>
        <p class="amount"><span class="sign">₱</span><span id="today-pesos">0</span></p>
        <p class="today-detail" id="today-detail">Waiting for data…</p>
        <dl class="periods">
          <div><dt>Last 7 days</dt><dd id="p7">₱0</dd></div>
          <div><dt>Last 30 days</dt><dd id="p30">₱0</dd></div>
          <div><dt>All time</dt><dd id="pall">₱0</dd></div>
          <div><dt>Online now</dt><dd id="online-count">0</dd></div>
        </dl>
      </section>
      <section class="card" aria-labelledby="mix-h">
        <h2 id="mix-h">Coins inserted, last 14 days</h2>
        <div class="stacks" id="stacks"></div>
      </section>
    </div>
    <div class="grid">
      <section class="card" aria-labelledby="chart-h">
        <h2 id="chart-h">Takings per day</h2>
        <p class="chart-note" id="chart-note"></p>
        <div class="bars" id="bars" role="list"></div>
        <div class="bar-labels"><span id="bars-first"></span><span>Today</span></div>
      </section>
      <section class="card" aria-labelledby="coins-h">
        <h2 id="coins-h">Recent coins</h2>
        <ul class="rows" id="coins"></ul>
      </section>
    </div>
  </div>

  <div id="tab-customers" role="tabpanel" aria-labelledby="t-customers" hidden>
    <section class="card" aria-labelledby="clients-h">
      <h2 id="clients-h">Connected now</h2>
      <ul class="rows" id="clients"></ul>
    </section>
  </div>

  <div id="tab-vouchers" role="tabpanel" aria-labelledby="t-vouchers" hidden>
    <div class="grid">
      <section class="card" aria-labelledby="vouchers-h">
        <h2 id="vouchers-h">Create vouchers</h2>
        <form id="voucher-form" novalidate>
          <div class="fields">
            <div>
              <label for="v-count">How many</label>
              <span class="help">Up to 200 at a time.</span>
              <input id="v-count" type="number" inputmode="numeric" min="1" max="200" value="10" required>
            </div>
            <div>
              <label for="v-minutes">Minutes on each</label>
              <span class="help">Internet time a voucher gives.</span>
              <input id="v-minutes" type="number" inputmode="numeric" min="1" max="100000" value="60" required>
            </div>
            <div>
              <label for="v-price">Price of each (₱)</label>
              <span class="help">What you sell one for. For your records only.</span>
              <input id="v-price" type="number" inputmode="numeric" min="0" max="100000" value="10" required>
            </div>
          </div>
          <div class="save-row">
            <button class="button" type="submit" id="v-create">Create vouchers</button>
            <p id="v-message" aria-live="polite"></p>
          </div>
          <p id="v-codes"></p>
        </form>
      </section>
      <section class="card" aria-labelledby="batches-h">
        <h2 id="batches-h">Batches</h2>
        <ul class="rows" id="batches"></ul>
      </section>
    </div>
  </div>

  <div id="tab-settings" role="tabpanel" aria-labelledby="t-settings" hidden>
    <div class="grid">
      <section class="card" aria-labelledby="look-h">
        <h2 id="look-h">Name and look</h2>
        <form id="look-form" novalidate>
          <div class="fields">
            <div>
              <label for="a-title">Piso WiFi name</label>
              <span class="help">Shown to customers on the Wi-Fi page, here, and on printed vouchers. Up to 40 characters.</span>
              <input id="a-title" class="wide" type="text" maxlength="40" autocomplete="off" required>
            </div>
          </div>
          <fieldset class="choice">
            <legend>Dashboard theme</legend>
            <label><input type="radio" name="mode" value="auto"><span>Match my device</span></label>
            <label><input type="radio" name="mode" value="dark"><span>Dark</span></label>
            <label><input type="radio" name="mode" value="light"><span>Light</span></label>
          </fieldset>
          <fieldset class="choice">
            <legend>Accent colour</legend>
            <label class="swatch" data-swatch="gold"><input type="radio" name="accent" value="gold"><span>Gold</span></label>
            <label class="swatch" data-swatch="blue"><input type="radio" name="accent" value="blue"><span>Blue</span></label>
            <label class="swatch" data-swatch="green"><input type="radio" name="accent" value="green"><span>Green</span></label>
            <label class="swatch" data-swatch="purple"><input type="radio" name="accent" value="purple"><span>Purple</span></label>
            <label class="swatch" data-swatch="red"><input type="radio" name="accent" value="red"><span>Red</span></label>
          </fieldset>
          <div class="save-row">
            <button class="button" type="submit" id="look-save">Save name and look</button>
            <p id="look-message" aria-live="polite"></p>
          </div>
        </form>
      </section>
      <section class="card" aria-labelledby="settings-h">
        <h2 id="settings-h">Rate and coinslot</h2>
        <form id="settings-form" novalidate>
          <div class="fields">
            <div>
              <label for="s-rate">Minutes per peso</label>
              <span class="help">Internet time for each ₱1.</span>
              <input id="s-rate" name="minutes_per_peso" type="number" inputmode="numeric" required>
            </div>
            <div>
              <label for="s-window">Insert Coin time limit</label>
              <span class="help">Seconds a customer has to insert coins after tapping Insert Coin.</span>
              <input id="s-window" name="coin_window_seconds" type="number" inputmode="numeric" required>
            </div>
            <div>
              <label for="s-grace">Late coin allowance</label>
              <span class="help">Seconds after the time limit when a coin still being counted goes to that customer.</span>
              <input id="s-grace" name="coin_grace_seconds" type="number" inputmode="numeric" required>
            </div>
            <div>
              <label for="s-down">Download speed limit</label>
              <span class="help">Mbps per customer. 0 means no limit. Applies from each customer's next purchase.</span>
              <input id="s-down" data-kbps="download_kbps" type="number" inputmode="decimal" step="0.1" min="0" max="1000" required>
            </div>
            <div>
              <label for="s-up">Upload speed limit</label>
              <span class="help">Mbps per customer. 0 means no limit.</span>
              <input id="s-up" data-kbps="upload_kbps" type="number" inputmode="decimal" step="0.1" min="0" max="1000" required>
            </div>
          </div>
          <p class="preview" id="rate-preview"></p>
          <div class="save-row">
            <button class="button" type="submit" id="save">Save settings</button>
            <p id="save-message" aria-live="polite"></p>
          </div>
        </form>
      </section>
      <section class="card" aria-labelledby="promos-h">
        <h2 id="promos-h">Promos</h2>
        <form id="promo-form" novalidate>
          <p class="help-line">Give bonus time for bigger amounts, for example ₱10 = 70 min instead of 50.
            Customers get the best mix of promos for everything they insert in one go.</p>
          <div id="promo-rows"></div>
          <button class="quiet" type="button" id="promo-add">Add a promo</button>
          <p class="preview" id="promo-preview"></p>
          <div class="save-row">
            <button class="button" type="submit" id="promo-save">Save promos</button>
            <p id="promo-message" aria-live="polite"></p>
          </div>
        </form>
      </section>
    </div>
  </div>
</div>
<datalist id="connected-macs"></datalist>
<script nonce="{{ nonce }}">{{ js }}</script>
</body>
</html>
"""

DASHBOARD_JS = r"""
"use strict";
const CSRF = document.querySelector('meta[name="csrf"]').content;
const DENOMINATIONS = [1, 5, 10, 20];
const MAX_DISCS = 10;
const $ = (id) => document.getElementById(id);
let settingsLoaded = false, promosLoaded = false, lookLoaded = false, assigning = false;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function peso(n) { return "₱" + Number(n || 0).toLocaleString("en-PH"); }
function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
function duration(minutes) {
  minutes = Math.max(0, Math.round(minutes));
  if (minutes < 60) return minutes + " min";
  const d = Math.floor(minutes / 1440), h = Math.floor((minutes % 1440) / 60), m = minutes % 60;
  const parts = [];
  if (d) parts.push(d + (d === 1 ? " day" : " days"));
  if (h) parts.push(h + " hr");
  if (m) parts.push(m + " min");
  return parts.join(" ");
}
function ago(ts, now) {
  const s = now - ts;
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 172800) return Math.round(s / 3600) + " hr ago";
  return Math.round(s / 86400) + " days ago";
}
function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function shortDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString([], { month: "short", day: "numeric" });
}
function emptyRow(list, text) {
  list.replaceChildren();
  const li = el("li", "single");
  li.append(el("p", "empty-note", text));
  list.append(li);
}
function setHealth(id, state, text) {
  const node = $(id);
  node.className = state;
  node.textContent = text;
}

function renderProblems(errors) {
  const box = $("problems");
  box.replaceChildren();
  const messages = [];
  if (errors.creditd) messages.push(errors.creditd);
  for (const key of ["sales", "coins", "settings", "status", "promos", "vouchers", "appearance"]) if (errors[key]) messages.push(errors[key]);
  for (const text of messages) box.append(el("p", "notice", text));
}

function renderTakings(sales, now) {
  const t = sales.totals;
  $("today-pesos").textContent = Number(t.today.pesos).toLocaleString("en-PH");
  let detail = t.today.coins ? plural(t.today.coins, "coin", "coins") + " today." : "No coins yet today.";
  if (sales.last_coin_ts) detail += " Last coin " + ago(sales.last_coin_ts, now) + ".";
  $("today-detail").textContent = detail;
  $("p7").textContent = peso(t.last_7_days.pesos);
  $("p30").textContent = peso(t.last_30_days.pesos);
  $("pall").textContent = peso(t.all_time.pesos);
  if (sales.unassigned && sales.unassigned.pesos > 0) {
    $("problems").append(el("p", "notice",
      peso(sales.unassigned.pesos) + " in coins arrived when no customer had tapped Insert Coin. " +
      "The money is recorded but wasn't given to anyone."));
  }
}

function renderStacks(sales) {
  const counts = new Map(sales.coin_mix.map((c) => [c.pesos, c.count]));
  const shown = DENOMINATIONS.concat([...counts.keys()].filter((p) => !DENOMINATIONS.includes(p)));
  const most = Math.max(1, ...shown.map((p) => counts.get(p) || 0));
  const box = $("stacks");
  box.replaceChildren();
  for (const face of shown) {
    const count = counts.get(face) || 0;
    const stack = el("div", "stack");
    stack.setAttribute("aria-label", peso(face) + " coins: " + count + ", " + peso(face * count));
    const discs = el("div", "discs");
    const width = 2.1 + Math.min(face, 20) * 0.06;
    const n = count ? Math.max(1, Math.round((count / most) * MAX_DISCS)) : 1;
    for (let i = 0; i < n; i++) {
      const disc = el("div", count ? "disc" : "disc empty");
      disc.style.width = width + "rem";
      discs.append(disc);
    }
    stack.append(discs, el("span", "face", peso(face)), el("span", "count", "×" + count),
                 el("span", "count", peso(face * count)));
    box.append(stack);
  }
}

function renderBars(sales) {
  const bars = $("bars");
  bars.replaceChildren();
  const best = Math.max(0, ...sales.daily.map((d) => d.pesos));
  sales.daily.forEach((day, i) => {
    const bar = el("div", i === sales.daily.length - 1 ? "bar today" : "bar");
    bar.setAttribute("role", "listitem");
    const label = shortDate(day.date) + ": " + peso(day.pesos) + ", " + plural(day.coins, "coin", "coins");
    bar.setAttribute("aria-label", label);
    bar.title = label;
    bar.style.height = best ? Math.max(day.pesos ? 3 : 0, (day.pesos / best) * 100) + "%" : "0";
    bars.append(bar);
  });
  $("bars-first").textContent = sales.daily.length ? shortDate(sales.daily[0].date) : "";
  $("chart-note").textContent = best ? "Best day: " + peso(best) + "." : "No coins in the last 14 days yet.";
}

function renderClients(data, errors, now) {
  const list = $("clients");
  if (errors.clients) {
    setHealth("h-portal", "down", "Captive portal off");
    emptyRow(list, "Can't read connected devices: " + errors.clients);
    return;
  }
  if (!data.clients) return;
  setHealth("h-portal", "up", "Captive portal on");
  const clients = data.clients.clients;
  const online = clients.filter((c) => c.state === "Authenticated").length;
  $("online-count").textContent = online;
  $("online-badge").textContent = online;
  $("online-badge").hidden = online === 0;
  if (!clients.length) { emptyRow(list, "No devices are connected to the Piso WiFi."); return; }
  list.replaceChildren();
  for (const c of clients) {
    const li = el("li");
    const end = Number(c.session_end || 0);
    let state = c.state || "Unknown";
    if (c.state === "Authenticated") state = end > now ? "Online, " + duration((end - now) / 60) + " left" : "Online";
    else if (c.state === "Preauthenticated") state = "At the portal, hasn't paid";
    const who = el("span", "main mac", c.mac || "?");
    who.append(el("span", "sub", c.ip || ""));
    li.append(who, el("span", c.state === "Authenticated" ? "right state-on" : "right", state));
    list.append(li);
  }
}

function renderCoins(data) {
  const list = $("coins");
  if (!data.coins || assigning) return;  // don't wipe a half-filled "give to" form
  const coins = data.coins.coins;
  if (!coins.length) { emptyRow(list, "No coins yet. Inserted coins appear here within a few seconds."); return; }
  list.replaceChildren();
  for (const c of coins) {
    const li = el("li");
    const detail = c.mac ? duration(c.minutes) + " for " + c.mac : "Not given to anyone (no one had tapped Insert Coin)";
    li.append(el("span", "main", peso(c.pesos)), el("span", "detail", detail),
              el("span", "right detail", clock(c.ts)));
    if (!c.mac && c.coin_id) {
      const give = el("button", "quiet row-action", "Give to a customer");
      give.type = "button";
      give.addEventListener("click", () => openAssign(li, c, give));
      li.append(give);
    }
    list.append(li);
  }
}

function openAssign(li, coin, opener) {
  assigning = true;
  opener.hidden = true;
  const form = el("form", "assign");
  const input = el("input");
  input.setAttribute("list", "connected-macs");
  input.placeholder = "Customer's device, e.g. 00:1f:fb:bb:25:bc";
  input.autocomplete = "off";
  const give = el("button", "button", "Give " + peso(coin.pesos));
  give.type = "submit";
  const cancel = el("button", "quiet", "Cancel");
  cancel.type = "button";
  const error = el("p", "error");
  form.append(input, give, cancel, error);
  li.append(form);
  input.focus();
  cancel.addEventListener("click", () => { form.remove(); opener.hidden = false; assigning = false; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const mac = input.value.trim().toLowerCase();
    if (!/^([0-9a-f]{2}:){5}[0-9a-f]{2}$/.test(mac)) { error.textContent = "Type a device address like 00:1f:fb:bb:25:bc."; return; }
    give.disabled = true;
    const { ok, body } = await send("/api/coins/assign", { coin_id: coin.coin_id, mac });
    give.disabled = false;
    if (!ok) { error.textContent = body.error || "The coin wasn't given."; return; }
    assigning = false;
    refresh();
  });
}

async function send(path, payload) {
  try {
    const response = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
      body: JSON.stringify(payload),
    });
    if (response.status === 401) { location.href = "/login"; return { ok: false, body: {} }; }
    let body = {};
    try { body = await response.json(); } catch (e) { /* keep empty */ }
    return { ok: response.ok, body };
  } catch (err) {
    return { ok: false, body: { error: "Can't reach the Pi." } };
  }
}

function rateText(card) {
  return card.map((r) => peso(r.pesos) + " buys " + duration(r.minutes)).join(", ") + ".";
}

function addPromoRow(pesos, minutes) {
  const row = el("div", "promo-row");
  const p = el("input"); p.type = "number"; p.min = 2; p.max = 1000; p.className = "promo-pesos";
  const m = el("input"); m.type = "number"; m.min = 1; m.max = 100000; m.className = "promo-minutes";
  p.value = pesos || ""; m.value = minutes || "";
  p.setAttribute("aria-label", "Promo amount in pesos");
  m.setAttribute("aria-label", "Minutes for that amount");
  const remove = el("button", "quiet", "Remove"); remove.type = "button";
  remove.addEventListener("click", () => row.remove());
  row.append(el("span", null, "₱"), p, el("span", null, "buys"), m, el("span", null, "min"), remove);
  $("promo-rows").append(row);
  return p;
}

function renderPromos(data) {
  if (!data.promos) return;
  $("promo-preview").textContent = "Customers now get: " + rateText(data.promos.rate_card);
  if (promosLoaded) return;
  $("promo-rows").replaceChildren();
  for (const promo of data.promos.promos) addPromoRow(promo.pesos, promo.minutes);
  promosLoaded = true;
}

function renderBatches(data) {
  const list = $("batches");
  if (!data.vouchers) return;
  const batches = data.vouchers.batches;
  if (!batches.length) { emptyRow(list, "No vouchers yet. Create a batch above, then print it."); return; }
  list.replaceChildren();
  for (const b of batches) {
    const li = el("li");
    const unused = b.total - b.used - b.void;
    const main = el("span", "main", duration(b.minutes) + (b.price ? ", " + peso(b.price) + " each" : ""));
    main.append(el("span", "sub", b.used + " of " + b.total + " used" + (b.void ? ", " + b.void + " cancelled" : "")),
                el("span", "sub", b.batch));
    const actions = el("span", "actions");
    if (unused > 0) {
      const print = el("a", "quiet", "Print " + unused);
      print.href = "/vouchers/" + encodeURIComponent(b.batch) + "/print";
      print.target = "_blank";
      print.rel = "noopener";
      const cancel = el("button", "quiet", "Cancel unused");
      cancel.type = "button";
      cancel.addEventListener("click", async () => {
        if (!confirm("Cancel the " + unused + " unused vouchers in " + b.batch + "? They will stop working.")) return;
        const { ok, body } = await send("/api/vouchers/void", { batch: b.batch });
        if (!ok) alert(body.error || "The vouchers weren't cancelled.");
        refresh();
      });
      actions.append(print, cancel);
    }
    li.append(main, actions);
    list.append(li);
  }
}

function renderMacList(data) {
  const list = $("connected-macs");
  list.replaceChildren();
  const clients = (data.clients && data.clients.clients) || [];
  for (const c of clients) {
    if (!c.mac) continue;
    const option = el("option");
    option.value = c.mac;
    option.label = (c.ip || "") + " " + (c.state || "");
    list.append(option);
  }
}

function renderLook(data) {
  if (!data.appearance || lookLoaded) return;  // don't overwrite what the owner is typing
  const a = data.appearance;
  $("a-title").value = a.title;
  for (const input of document.querySelectorAll('#look-form input[type="radio"]')) {
    input.checked = input.value === (input.name === "mode" ? a.dashboard_mode : a.dashboard_accent);
  }
  lookLoaded = true;
}

function applyLook(a) {
  document.documentElement.dataset.theme = a.dashboard_mode;
  document.documentElement.dataset.accent = a.dashboard_accent;
  $("brand-title").textContent = a.title;
  document.title = a.title + " admin";
}

function renderPreview() {
  const rate = Number($("s-rate").value);
  $("rate-preview").textContent = rate > 0
    ? "Without promos: " + DENOMINATIONS.map((p) => peso(p) + " buys " + duration(p * rate)).join(", ") + "."
    : "";
}

function renderSettings(data) {
  if (!data.settings || settingsLoaded) return;  // don't overwrite what the owner is typing
  const s = data.settings.settings, limits = data.settings.limits;
  for (const [id, name] of [["s-rate", "minutes_per_peso"], ["s-window", "coin_window_seconds"], ["s-grace", "coin_grace_seconds"]]) {
    const input = $(id);
    input.value = s[name];
    input.min = limits[name].min;
    input.max = limits[name].max;
  }
  for (const input of document.querySelectorAll("input[data-kbps]")) {
    input.value = s[input.dataset.kbps] / 1000;
  }
  settingsLoaded = true;
  renderPreview();
}

async function refresh() {
  let response;
  try {
    response = await fetch("/api/overview", { headers: { Accept: "application/json" }, credentials: "same-origin" });
  } catch (err) {
    $("updated").textContent = "Can't reach the Pi. Retrying…";
    return;
  }
  if (response.status === 401) { location.href = "/login"; return; }
  const data = await response.json();
  const errors = data.errors || {};
  renderProblems(errors);
  if (errors.creditd) {
    setHealth("h-credit", "down", "Credit service unreachable");
    setHealth("h-portal", "", "Captive portal");
  } else {
    setHealth("h-credit", "up", "Credit service on");
    if (data.sales) { renderTakings(data.sales, data.now); renderStacks(data.sales); renderBars(data.sales); }
    renderClients(data, errors, data.now);
    renderMacList(data);
    renderCoins(data);
    renderSettings(data);
    renderPromos(data);
    renderBatches(data);
    renderLook(data);
  }
  $("updated").textContent = "Updated " + clock(data.now);
}

$("s-rate").addEventListener("input", renderPreview);
$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("save-message"), button = $("save");
  const body = {};
  for (const input of event.target.querySelectorAll("input[name]")) {
    const value = Number(input.value);
    if (!Number.isInteger(value) || value < Number(input.min) || value > Number(input.max)) {
      message.className = "error";
      message.textContent = input.labels[0].textContent + " must be a whole number from " + input.min + " to " + input.max + ".";
      input.focus();
      return;
    }
    body[input.name] = value;
  }
  for (const input of event.target.querySelectorAll("input[data-kbps]")) {
    const mbps = Number(input.value);
    if (!Number.isFinite(mbps) || mbps < 0 || mbps > 1000) {
      message.className = "error";
      message.textContent = input.labels[0].textContent + " must be from 0 to 1000 Mbps.";
      input.focus();
      return;
    }
    body[input.dataset.kbps] = Math.round(mbps * 1000);
  }
  button.disabled = true;
  message.className = "";
  message.textContent = "Saving…";
  try {
    const response = await fetch("/api/settings", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
      body: JSON.stringify(body),
    });
    if (response.status === 401) { location.href = "/login"; return; }
    const result = await response.json();
    if (response.ok) { message.textContent = "Settings saved."; }
    else { message.className = "error"; message.textContent = result.error || "Settings weren't saved."; }
  } catch (err) {
    message.className = "error";
    message.textContent = "Can't reach the Pi. Settings weren't saved.";
  } finally {
    button.disabled = false;
  }
});

$("promo-add").addEventListener("click", () => addPromoRow().focus());
$("promo-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("promo-message");
  const promos = [];
  for (const row of document.querySelectorAll(".promo-row")) {
    const pesos = Number(row.querySelector(".promo-pesos").value);
    const minutes = Number(row.querySelector(".promo-minutes").value);
    if (!row.querySelector(".promo-pesos").value && !row.querySelector(".promo-minutes").value) continue;
    if (!Number.isInteger(pesos) || pesos < 2 || pesos > 1000 || !Number.isInteger(minutes) || minutes < 1) {
      message.className = "error";
      message.textContent = "Each promo needs a whole peso amount from 2 to 1000 and whole minutes.";
      return;
    }
    promos.push({ pesos, minutes });
  }
  message.className = ""; message.textContent = "Saving…";
  const { ok, body } = await send("/api/promos", { promos });
  if (!ok) { message.className = "error"; message.textContent = body.error || "Promos weren't saved."; return; }
  message.textContent = promos.length ? "Promos saved." : "Promos removed.";
  $("promo-preview").textContent = "Customers now get: " + rateText(body.rate_card);
});

$("voucher-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("v-message"), button = $("v-create");
  const count = Number($("v-count").value), minutes = Number($("v-minutes").value), price = Number($("v-price").value);
  if (![count, minutes, price].every(Number.isInteger) || count < 1 || count > 200 || minutes < 1 || price < 0) {
    message.className = "error";
    message.textContent = "Use whole numbers: 1 to 200 vouchers, at least 1 minute each, a price of 0 or more.";
    return;
  }
  button.disabled = true; message.className = ""; message.textContent = "Creating…";
  const { ok, body } = await send("/api/vouchers", { count, minutes, price });
  button.disabled = false;
  if (!ok) { message.className = "error"; message.textContent = body.error || "Vouchers weren't created."; return; }
  message.textContent = "Created " + body.codes.length + " vouchers of " + duration(body.minutes) + ".";
  const codes = $("v-codes");
  codes.replaceChildren();
  const link = el("a", "quiet", "Print them");
  link.href = "/vouchers/" + encodeURIComponent(body.batch) + "/print";
  link.target = "_blank"; link.rel = "noopener";
  codes.append(link);
  refresh();
});

for (const input of document.querySelectorAll('#look-form input[type="radio"]')) {
  // Preview the theme as soon as it's picked; Save keeps it.
  input.addEventListener("change", () => {
    const pick = (name) => document.querySelector('#look-form input[name="' + name + '"]:checked').value;
    document.documentElement.dataset.theme = pick("mode");
    document.documentElement.dataset.accent = pick("accent");
  });
}
$("look-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("look-message");
  const title = $("a-title").value.trim().replace(/\s+/g, " ");
  if (!title || title.length > 40 || /[<>]/.test(title)) {
    message.className = "error";
    message.textContent = "Use 1 to 40 characters, without < or >.";
    $("a-title").focus();
    return;
  }
  const pick = (name) => document.querySelector('#look-form input[name="' + name + '"]:checked').value;
  message.className = ""; message.textContent = "Saving…";
  const { ok, body } = await send("/api/appearance", { title, dashboard_mode: pick("mode"), dashboard_accent: pick("accent") });
  if (!ok) { message.className = "error"; message.textContent = body.error || "Changes weren't saved."; return; }
  applyLook(body);
  message.textContent = "Saved. Customers see the new name within 30 seconds.";
});

const TABS = ["overview", "customers", "vouchers", "settings"];
function openTab(name, focus) {
  if (!TABS.includes(name)) name = "overview";
  for (const t of TABS) {
    const on = t === name;
    $("t-" + t).setAttribute("aria-selected", on ? "true" : "false");
    $("t-" + t).tabIndex = on ? 0 : -1;
    $("tab-" + t).hidden = !on;
  }
  if (focus) $("t-" + name).focus();
  history.replaceState(null, "", "#" + name);
}
for (const t of TABS) $("t-" + t).addEventListener("click", () => openTab(t));
document.querySelector(".tabs").addEventListener("keydown", (event) => {
  const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
  if (!step) return;
  const current = TABS.findIndex((t) => $("t-" + t).getAttribute("aria-selected") === "true");
  openTab(TABS[(current + step + TABS.length) % TABS.length], true);
  event.preventDefault();
});
openTab(location.hash.slice(1));

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Piso WiFi owner dashboard")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("set-password", help=f"set the login password (stored hashed in {PASSWORD_FILE})")
    args = parser.parse_args(argv)
    if args.command == "set-password":
        return set_password_cli(PASSWORD_FILE)
    if not ADMIN_TOKEN or ADMIN_TOKEN.startswith("REPLACE_"):
        print("warning: PISOWIFI_ADMIN_TOKEN is not set; the dashboard will show no data", file=sys.stderr)
    create_app().run(host=BIND, port=PORT, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
