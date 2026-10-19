#!/usr/bin/env python3
"""Customer portal for the Piso WiFi machine.

openNDS sends every phone that isn't online yet to this page (its FAS). The
customer taps Insert Coin, drops coins in, and taps Start my time; the page
shows each coin as it lands and, once online, the time left.

The portal never trusts the browser to say who it is: it looks the phone's MAC
address up from the request's IP in the kernel's ARP table, then talks to
pisowifi-creditd on localhost with the admin token, which stays on the Pi.
It only ever acts on the requesting phone's own MAC.

Everything is inline (no CDN), because phones at the portal have no internet yet.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, g, jsonify, render_template_string, request
from markupsafe import Markup

NAME = os.environ.get("PISOWIFI_PORTAL_NAME", "Piso WiFi")
BIND = os.environ.get("PISOWIFI_PORTAL_BIND", "10.20.0.1")
PORT = int(os.environ.get("PISOWIFI_PORTAL_PORT", "2080"))
# Short address shown to customers for checking their time, e.g. http://piso.wifi
# (needs the dnsmasq name and the port-80 redirect in pisowifi-guard.nft).
ADDRESS = os.environ.get("PISOWIFI_PORTAL_ADDRESS", "")
LANGUAGE = os.environ.get("PISOWIFI_PORTAL_LANGUAGE", "en")  # en or tl; customers can switch
CREDITD_URL = f"http://127.0.0.1:{os.environ.get('PISOWIFI_PORT') or '8090'}"
ADMIN_TOKEN = os.environ.get("PISOWIFI_ADMIN_TOKEN", "")
ARP_FILE = Path("/proc/net/arp")

MAC_RE = re.compile(r"(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}")
PORTAL_HEADER = ("X-Requested-With", "piso-portal")  # blocks cross-site form posts


class CreditdUnavailable(Exception):
    """The credit service could not be reached at all."""


class CreditdClient:
    def __init__(self, url: str, token: str, timeout: float = 8.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout  # connect may wait on ndsctl deauth + auth

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


def _json_object(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class TitleCache:
    """The Piso WiFi name set on the dashboard. Phones fetch this page often
    (captive-portal checks), so the name is asked for at most every `ttl` seconds;
    if the credit service can't be reached, the last name (or the env default) is used."""

    def __init__(self, client: CreditdClient, fallback: str, ttl: float = 30.0, clock=time.monotonic) -> None:
        self.client, self.value, self.ttl, self.clock = client, fallback, ttl, clock
        self.fetched = None
        self.lock = threading.Lock()

    def get(self) -> str:
        with self.lock:
            now = self.clock()
            if self.fetched is not None and now - self.fetched < self.ttl:
                return self.value
            self.fetched = now
            try:
                code, body = self.client.call("GET", "/api/appearance")
                if code == 200 and isinstance(body.get("title"), str) and body["title"].strip():
                    self.value = body["title"]
            except CreditdUnavailable:
                pass
            return self.value


def mac_for_ip(ip: str | None, arp_file: Path = ARP_FILE) -> str | None:
    """The phone's MAC from /proc/net/arp. The phone just talked to us, so the
    kernel has a complete ARP entry for it."""
    if not ip:
        return None
    try:
        lines = arp_file.read_text(encoding="ascii", errors="replace").splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) >= 4 and parts[0] == ip:
            mac = parts[3].lower()
            if MAC_RE.fullmatch(mac) and mac != "00:00:00:00:00:00":
                return mac
    return None


def create_app(client: CreditdClient | None = None, arp_file: Path = ARP_FILE, name: str = NAME,
               address: str = ADDRESS, language: str = LANGUAGE) -> Flask:
    language = language if language in ("en", "tl") else "en"
    app = Flask(__name__)
    client = client or CreditdClient(CREDITD_URL, ADMIN_TOKEN)
    titles = TitleCache(client, name)

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

    def this_device() -> str | None:
        return mac_for_ip(request.remote_addr, arp_file)

    def unknown_device():
        return jsonify(error="unknown_device"), 400

    def offline():
        return jsonify(error="machine_offline"), 502

    def from_portal() -> bool:
        return request.headers.get(PORTAL_HEADER[0]) == PORTAL_HEADER[1]

    @app.get("/api/state")
    def state():
        mac = this_device()
        if not mac:
            return unknown_device()
        try:
            code, body = client.call("GET", "/api/customer?" + urllib.parse.urlencode({"mac": mac}))
        except CreditdUnavailable:
            return offline()
        if code != 200:
            return jsonify(error="machine_error"), 502
        body["portal_address"] = address or f"http://{request.host}"
        return jsonify(body)

    @app.post("/api/insert-coin")
    def insert_coin():
        if not from_portal():
            return jsonify(error="forbidden"), 403
        mac = this_device()
        if not mac:
            return unknown_device()
        try:
            code, body = client.call("POST", "/api/insert-coin", {"mac": mac})
        except CreditdUnavailable:
            return offline()
        if code == 409:
            return jsonify(error="busy", retry_after=body.get("retry_after", 30)), 409
        if code == 429:
            return jsonify(error="too_many_taps", retry_after=body.get("retry_after", 300)), 429
        if code != 200:
            return jsonify(error="machine_error"), 502
        return jsonify(ok=True, seconds_left=body.get("seconds_left"))

    @app.post("/api/connect")
    def connect():
        if not from_portal():
            return jsonify(error="forbidden"), 403
        mac = this_device()
        if not mac:
            return unknown_device()
        try:
            code, body = client.call("POST", "/api/connect", {"mac": mac})
        except CreditdUnavailable:
            return offline()
        if code == 200:
            return jsonify(ok=True, seconds_left=body.get("seconds_left"), minutes_added=body.get("minutes_added"))
        if code == 409 and "switched off" in body.get("error", ""):
            return jsonify(error="internet_off"), 409
        if code == 409:
            return jsonify(error="no_credit"), 409
        return jsonify(error="connect_failed"), 502

    def forward(path: str, body: dict, outcomes: dict):
        """POST for this phone; map credit-service status codes to portal error names."""
        if not from_portal():
            return jsonify(error="forbidden"), 403
        mac = this_device()
        if not mac:
            return unknown_device()
        try:
            code, reply = client.call("POST", path, {"mac": mac, **body})
        except CreditdUnavailable:
            return offline()
        if code == 200:
            return jsonify(ok=True, **{k: reply.get(k) for k in ("seconds_left", "minutes", "started", "paused_seconds")})
        if code == 409 and "switched off" in reply.get("error", ""):
            return jsonify(error="internet_off"), 409
        error = outcomes.get(code, outcomes.get("other", "machine_error"))
        extra = {"retry_after": reply["retry_after"]} if "retry_after" in reply else {}
        return jsonify(error=error, **extra), code if code in (400, 404, 409, 429) else 502

    @app.post("/api/pause")
    def pause():
        return forward("/api/pause", {}, {409: "cannot_pause", "other": "pause_failed"})

    @app.post("/api/resume")
    def resume():
        return forward("/api/resume", {}, {409: "not_paused", "other": "resume_failed"})

    @app.post("/api/redeem")
    def redeem():
        data = request.get_json(silent=True) or {}
        code = data.get("code") if isinstance(data.get("code"), str) else ""
        return forward("/api/redeem", {"code": code[:32]},
                       {400: "bad_code", 404: "bad_code", 409: "used_code", 429: "code_locked"})

    @app.route("/api/<path:_rest>", methods=["GET", "POST"])
    def unknown_api(_rest):
        return jsonify(error="not_found"), 404

    # Captive-portal probes and openNDS's redirect (with its ?fas=... query) all land here.
    @app.get("/")
    @app.get("/<path:_any>")
    def page(_any=""):
        html = render_template_string(
            PAGE_HTML, nonce=g.nonce, name=titles.get(), language=language, css=Markup(PAGE_CSS), js=Markup(PAGE_JS),
        )
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    return app


PAGE_CSS = r"""
/* Piso WiFi customer page. One bold element: the gold coin you tap. */
:root {
  color-scheme: dark;
  --bg: #0D1430; --glow: #24307A; --surface: #161F45; --surface-2: #1E2856; --line: rgba(255, 255, 255, 0.10);
  --text: #F3F5FF; --muted: #A9B1D6; --gold: #F5B83D; --gold-hi: #FFE29A; --gold-deep: #C98408; --gold-ink: #3A2600;
  --mint: #3DDC97; --danger: #FF8E7E; --danger-bg: rgba(255, 142, 126, 0.12); --good-bg: rgba(61, 220, 151, 0.12);
  --focus: #8FB4FF; --radius: 22px;
  --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif;
}
@media (prefers-color-scheme: light) {
  :root {
    color-scheme: light;
    --bg: #F2F4FD; --glow: #D9E0FF; --surface: #FFFFFF; --surface-2: #EDF0FA; --line: #DCE1F2;
    --text: #121A3D; --muted: #5A6388; --mint: #128A57; --danger: #B23A2B; --danger-bg: #FDECE9;
    --good-bg: #E3F5EC; --focus: #2F5FD0;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; min-height: 100vh; color: var(--text); font: 17px/1.5 var(--font); font-variant-numeric: tabular-nums;
  background: radial-gradient(120% 60% at 50% 0%, var(--glow) 0%, var(--bg) 62%) fixed, var(--bg);
}
main { max-width: 27rem; margin: 0 auto; padding: 1rem 1.25rem 2.5rem; }
.top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.75rem; }
.brand { display: flex; align-items: center; gap: 0.55rem; }
.brand .ico { width: 1.6rem; height: 1.6rem; color: var(--gold); }
h1 { font-size: 1.05rem; font-weight: 700; margin: 0; letter-spacing: 0.01em; }
.lang {
  border: 1px solid var(--line); background: var(--surface); color: var(--text); border-radius: 999px;
  padding: 0.35rem 0.9rem; font: inherit; font-size: 0.85rem; font-weight: 600; cursor: pointer;
}
h2 { font-size: 1.85rem; line-height: 1.15; letter-spacing: -0.02em; margin: 0 0 0.4rem; }
h3 { font-size: 0.95rem; color: var(--muted); font-weight: 600; margin: 2rem 0 0.7rem; }
p { margin: 0; }
.lead { color: var(--muted); }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 3px; }
[hidden] { display: none !important; }
.sr-defs { position: absolute; width: 0; height: 0; overflow: hidden; }
.ico { width: 1.25rem; height: 1.25rem; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; flex: none; }
.ico.solid { fill: currentColor; stroke: none; }

/* The coin */
.stage { display: grid; place-items: center; padding: 2rem 0 1.5rem; }
.coin {
  width: 13.5rem; height: 13.5rem; border-radius: 50%; border: 0; cursor: pointer; color: var(--gold-ink);
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 0.15rem;
  background: radial-gradient(circle at 34% 28%, var(--gold-hi) 0, var(--gold) 36%, var(--gold-deep) 82%);
  box-shadow: inset 0 0 0 9px rgba(255, 255, 255, 0.22), inset 0 -10px 22px rgba(0, 0, 0, 0.22),
              0 22px 55px -14px rgba(245, 184, 61, 0.6);
  font: inherit; -webkit-tap-highlight-color: transparent; transition: transform 120ms ease;
}
.coin:active { transform: scale(0.96); }
.coin:disabled { cursor: default; filter: grayscale(0.9) brightness(0.8); box-shadow: inset 0 0 0 9px rgba(255, 255, 255, 0.15); }
.coin-face { font-size: 4.6rem; font-weight: 850; line-height: 1; text-shadow: 0 2px 0 rgba(255, 255, 255, 0.35); }
.coin-label { font-size: 1.05rem; font-weight: 750; max-width: 9.5rem; text-align: center; line-height: 1.2; }
@media (prefers-reduced-motion: no-preference) {
  #v-start .coin:not(:disabled) { animation: invite 2.6s ease-in-out infinite; }
  @keyframes invite {
    0%, 100% { box-shadow: inset 0 0 0 9px rgba(255, 255, 255, 0.22), inset 0 -10px 22px rgba(0, 0, 0, 0.22), 0 22px 55px -14px rgba(245, 184, 61, 0.45); }
    50% { box-shadow: inset 0 0 0 9px rgba(255, 255, 255, 0.22), inset 0 -10px 22px rgba(0, 0, 0, 0.22), 0 22px 70px -6px rgba(245, 184, 61, 0.8); }
  }
}

/* Countdown ring while the slot is open */
.ring { position: relative; width: 13.5rem; height: 13.5rem; }
.ring svg { width: 100%; height: 100%; transform: rotate(-90deg); }
.ring-track { fill: none; stroke: var(--surface-2); stroke-width: 9; }
.ring-fill { fill: none; stroke: var(--gold); stroke-width: 9; stroke-linecap: round; stroke-dasharray: 339.29; }
.ring-center { position: absolute; inset: 0; display: flex; align-items: baseline; justify-content: center; padding-top: 4.3rem; }
.ring-num { font-size: 4rem; font-weight: 800; letter-spacing: -0.03em; line-height: 1; }
.ring-unit { color: var(--muted); font-size: 1.2rem; margin-left: 0.2rem; }
@media (prefers-reduced-motion: no-preference) { .ring-fill { transition: stroke-dashoffset 900ms linear; } }

/* Buttons */
.primary, .secondary {
  display: flex; align-items: center; justify-content: center; gap: 0.5rem; width: 100%; min-height: 3.5rem;
  border-radius: 999px; font: inherit; font-weight: 750; cursor: pointer; padding: 0.8rem 1.2rem;
}
.primary { border: 0; color: var(--gold-ink); background: linear-gradient(180deg, var(--gold-hi) -60%, var(--gold) 55%, var(--gold-deep) 160%); }
.secondary { border: 1px solid var(--line); color: var(--text); background: var(--surface); }
.primary:disabled, .secondary:disabled { opacity: 0.45; cursor: default; }
#b-use-credit { margin-bottom: 0.5rem; }
.actions { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; margin-top: 1.25rem; }

/* Cards */
.card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); }
.rates { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.55rem; margin: 0; padding: 0; list-style: none; }
.rates li { background: var(--surface); border: 1px solid var(--line); border-radius: 18px; padding: 0.75rem 0.25rem 0.7rem; text-align: center; }
.rates li.promo { border-color: rgba(245, 184, 61, 0.55); }
.rates b { display: block; font-size: 1.1rem; }
.rates span { color: var(--muted); font-size: 0.82rem; }
.rates .tag {
  display: inline-block; margin-top: 0.3rem; padding: 0.05rem 0.45rem; border-radius: 999px;
  background: var(--gold); color: var(--gold-ink); font-size: 0.68rem; font-weight: 800;
}
.voucher { margin-top: 1rem; padding: 0.35rem; }
.voucher-toggle {
  display: flex; align-items: center; gap: 0.6rem; width: 100%; padding: 0.75rem 0.85rem; border: 0; border-radius: 18px;
  background: transparent; color: var(--text); font: inherit; font-weight: 650; cursor: pointer; text-align: left;
}
.voucher-toggle .ico { color: var(--gold); }
.voucher form { display: grid; gap: 0.6rem; padding: 0 0.5rem 0.6rem; }
.voucher input {
  width: 100%; padding: 0.85rem 1rem; border-radius: 999px; border: 1px solid var(--line);
  background: var(--surface-2); color: var(--text); font: inherit; letter-spacing: 0.1em; text-transform: uppercase;
}
.voucher input::placeholder { text-transform: none; letter-spacing: normal; color: var(--muted); }
.voucher form .primary { min-height: 3rem; }

.tally { display: flex; justify-content: space-between; align-items: baseline; padding: 1rem 1.25rem; margin-bottom: 1rem; }
.tally span { color: var(--muted); }
.tally b { font-size: 1.5rem; }

/* Online and paused */
.status { display: flex; align-items: center; gap: 0.6rem; font-size: 1.3rem; font-weight: 750; margin-bottom: 1rem; }
.status.online { color: var(--mint); }
.status.paused { color: var(--muted); }
.pulse { width: 0.75rem; height: 0.75rem; border-radius: 50%; background: var(--mint); box-shadow: 0 0 0 0 rgba(61, 220, 151, 0.6); }
@media (prefers-reduced-motion: no-preference) {
  .pulse { animation: pulse 2s ease-out infinite; }
  @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(61, 220, 151, 0.55); } 100% { box-shadow: 0 0 0 0.9rem rgba(61, 220, 151, 0); } }
}
.timer { padding: 1.5rem 1.25rem 1.25rem; text-align: center; }
.big { font-size: clamp(3.4rem, 19vw, 4.8rem); font-weight: 800; letter-spacing: -0.03em; line-height: 1; }
.unit { color: var(--muted); margin-top: 0.35rem; }
.days { color: var(--gold); font-weight: 800; font-size: 1.3rem; margin-bottom: 0.35rem; }
#v-paused .lead { margin: 1rem 0 1.25rem; }
.hint { color: var(--muted); font-size: 0.88rem; margin-top: 1.25rem; text-align: center; }

.notice { background: var(--danger-bg); color: var(--danger); border-radius: 18px; padding: 0.85rem 1rem; margin-bottom: 1.25rem; }
.notice.good { background: var(--good-bg); color: var(--mint); }
.spinner { width: 2.5rem; height: 2.5rem; border-radius: 50%; border: 4px solid var(--surface-2); border-top-color: var(--gold); margin: 3rem auto 1rem; }
@media (prefers-reduced-motion: no-preference) { .spinner { animation: spin 0.9s linear infinite; } @keyframes spin { to { transform: rotate(360deg); } } }
#v-loading .lead, #v-adding .lead, #v-adding h2 { text-align: center; }
footer { color: var(--muted); font-size: 0.78rem; margin-top: 2.5rem; text-align: center; letter-spacing: 0.02em; }
"""

PAGE_HTML = r"""<!doctype html>
<html lang="{{ language }}" data-default-lang="{{ language }}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0D1430">
<link rel="icon" href="data:,">
<title>{{ name }}</title>
<style nonce="{{ nonce }}">{{ css }}</style>
</head>
<body>
<svg width="0" height="0" class="sr-defs" aria-hidden="true" focusable="false">
  <symbol id="i-wifi" viewBox="0 0 24 24"><path d="M2.5 8.8a14 14 0 0 1 19 0M5.8 12.3a9.2 9.2 0 0 1 12.4 0M9.1 15.7a4.5 4.5 0 0 1 5.8 0"/><circle cx="12" cy="19.2" r="1.3" fill="currentColor" stroke="none"/></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
  <symbol id="i-pause" viewBox="0 0 24 24"><path d="M9 5v14M15 5v14"/></symbol>
  <symbol id="i-play" viewBox="0 0 24 24"><path d="M8 5.5v13l10-6.5z" fill="currentColor"/></symbol>
  <symbol id="i-ticket" viewBox="0 0 24 24"><path d="M3 8.5V6.8C3 6.4 3.4 6 3.8 6h16.4c.4 0 .8.4.8.8v1.7a2.5 2.5 0 0 0 0 5v1.7c0 .4-.4.8-.8.8H3.8a.8.8 0 0 1-.8-.8v-1.7a2.5 2.5 0 0 0 0-5Z"/><path d="M14.5 6v12" stroke-dasharray="2 2"/></symbol>
</svg>
<main>
  <header class="top">
    <div class="brand"><svg class="ico" aria-hidden="true"><use href="#i-wifi"/></svg><h1>{{ name }}</h1></div>
    <button class="lang" id="b-lang" type="button"></button>
  </header>
  <p class="notice" id="notice" role="alert" hidden></p>

  <section id="v-loading"><div class="spinner" aria-hidden="true"></div><p class="lead" data-t="loading"></p></section>

  <section id="v-start" hidden>
    <h2 data-t="get_online"></h2>
    <p class="lead" id="start-lead"></p>
    <div class="stage">
      <button class="coin" id="b-insert" type="button"><span class="coin-face" aria-hidden="true">₱</span><span class="coin-label" data-t="insert_coin"></span></button>
    </div>
    <button class="primary" id="b-use-credit" type="button" hidden></button>
    <h3 data-t="rates_title"></h3>
    <ul class="rates" id="rates"></ul>
    <div class="card voucher">
      <button class="voucher-toggle" id="b-voucher" type="button"><svg class="ico" aria-hidden="true"><use href="#i-ticket"/></svg><span data-t="have_voucher"></span></button>
      <form id="voucher-form" hidden novalidate>
        <input id="voucher-code" autocomplete="off" autocapitalize="characters" spellcheck="false" maxlength="12" inputmode="text">
        <button class="primary" type="submit" data-t="use_voucher"></button>
      </form>
    </div>
  </section>

  <section id="v-busy" hidden>
    <h2 data-t="busy_title"></h2>
    <p class="lead" data-t="busy_lead"></p>
    <div class="stage">
      <button class="coin" type="button" disabled><span class="coin-face" aria-hidden="true">₱</span><span class="coin-label" id="b-busy"></span></button>
    </div>
  </section>

  <section id="v-insert" hidden aria-live="polite">
    <h2 data-t="insert_title"></h2>
    <p class="lead" data-t="insert_lead"></p>
    <div class="stage">
      <div class="ring">
        <svg viewBox="0 0 120 120" aria-hidden="true"><circle class="ring-track" cx="60" cy="60" r="54"/><circle class="ring-fill" id="window-ring" cx="60" cy="60" r="54"/></svg>
        <div class="ring-center"><span class="ring-num" id="window-left">60</span><span class="ring-unit">s</span></div>
      </div>
    </div>
    <div class="tally card"><span id="tally-pesos"></span><b id="tally-time"></b></div>
    <button class="primary" id="b-start" type="button" disabled></button>
  </section>

  <section id="v-adding" hidden><div class="spinner" aria-hidden="true"></div><h2 data-t="adding"></h2><p class="lead" data-t="adding_lead"></p></section>

  <section id="v-online" hidden aria-live="polite">
    <p class="status online"><span class="pulse" aria-hidden="true"></span><span data-t="online_title"></span></p>
    <div class="timer card"><p class="days" id="online-days" hidden></p><p class="big" id="online-left">0:00</p><p class="unit" data-t="left"></p></div>
    <div class="actions">
      <button class="primary" id="b-more" type="button"><svg class="ico" aria-hidden="true"><use href="#i-plus"/></svg><span id="b-more-label"></span></button>
      <button class="secondary" id="b-pause" type="button"><svg class="ico" aria-hidden="true"><use href="#i-pause"/></svg><span data-t="pause"></span></button>
    </div>
    <p class="hint" id="online-hint"></p>
  </section>

  <section id="v-paused" hidden>
    <p class="status paused"><svg class="ico" aria-hidden="true"><use href="#i-pause"/></svg><span data-t="paused_title"></span></p>
    <div class="timer card"><p class="days" id="paused-days" hidden></p><p class="big" id="paused-left">0:00</p><p class="unit" data-t="saved"></p></div>
    <p class="lead" data-t="paused_lead"></p>
    <button class="primary" id="b-resume" type="button"><svg class="ico" aria-hidden="true"><use href="#i-play"/></svg><span data-t="resume"></span></button>
  </section>

  <footer id="device"></footer>
</main>
<script nonce="{{ nonce }}">{{ js }}</script>
</body>
</html>
"""

PAGE_JS = r"""
"use strict";
const $ = (id) => document.getElementById(id);
const VIEWS = ["v-loading", "v-start", "v-busy", "v-insert", "v-adding", "v-online", "v-paused"];
const RING = 339.29;  // circumference of the countdown ring (r = 54)

const STRINGS = {
  en: {
    other_language: "Tagalog", loading: "Connecting to the coin machine…", rates_title: "Rates",
    get_online: "Get online", lead_tap: "Tap the coin, then drop your coins into the slot.",
    lead_time_up: "Your time is up. Tap the coin to buy more.", insert_coin: "Insert coin",
    use_credit: "Start my {time}", promo: "Promo", have_voucher: "Have a voucher code?",
    voucher_placeholder: "Voucher code", use_voucher: "Use voucher",
    busy_title: "The coin slot is in use", busy_lead: "Someone else is inserting coins right now.",
    free_in: "Free in {s} s", insert_title: "Drop your coins in now",
    insert_lead: "Each coin adds time. The slot stays open after every coin.",
    no_coins_yet: "No coins yet", inserted: "{p} inserted", start_time: "Start my time",
    add_to_time: "Add to my time", adding: "Adding your time…", adding_lead: "This takes a few seconds.",
    online_title: "You're online", left: "left", add_time: "Add time",
    slot_busy_retry: "Slot in use", pause: "Pause",
    paused_title: "Your time is paused", saved: "saved",
    paused_lead: "You're offline. Your time doesn't run while it's paused.", resume: "Resume my time",
    check_later: "To check your time later, open {addr} in your browser.", device: "Your device: {mac}",
    blocked_lead: "Too many taps without a coin. You can tap again in {m} min.",
    voucher_ok: "Voucher added: {time}.", hr: "hr", min: "min", day: "day", days: "days", days_and: "{d} and",
    unknown_device: "Can't recognize your phone. Turn Wi-Fi off and on, then open this page again.",
    machine_offline: "Can't reach the coin machine. Trying again…",
    machine_error: "The coin machine had a problem. Trying again…",
    internet_off: "Internet isn't switched on yet. Please tell the owner.",
    connect_failed: "Your time is saved, but the internet didn't switch on. Trying again…",
    busy: "Someone else is inserting coins. Wait a moment.",
    too_many_taps: "Too many taps without a coin. Try again in {m} min.",
    bad_code: "That code isn't valid. Check it and try again.",
    used_code: "That voucher has already been used.",
    code_locked: "Too many wrong codes. Try again in {m} min.",
    cannot_pause: "Less than a minute is left, so there's nothing to pause.",
    pause_failed: "Couldn't pause right now. Try again.", resume_failed: "Couldn't resume right now. Try again.",
    not_paused: "Your time isn't paused.",
  },
  tl: {
    other_language: "English", loading: "Kumokonekta sa makina…", rates_title: "Presyo",
    get_online: "Mag-internet", lead_tap: "I-tap ang barya, tapos ihulog ang barya mo sa slot.",
    lead_time_up: "Ubos na ang oras mo. I-tap ang barya para bumili ulit.", insert_coin: "Maghulog ng barya",
    use_credit: "Simulan ang {time} ko", promo: "Promo", have_voucher: "May voucher code ka?",
    voucher_placeholder: "Code ng voucher", use_voucher: "Gamitin",
    busy_title: "May gumagamit ng coin slot", busy_lead: "May ibang naghuhulog ng barya ngayon.",
    free_in: "Bakante na sa {s} s", insert_title: "Ihulog na ang barya mo",
    insert_lead: "Bawat barya ay dagdag oras. Bukas pa rin ang slot pagkatapos ng bawat barya.",
    no_coins_yet: "Wala pang barya", inserted: "{p} ang naihulog", start_time: "Simulan ang oras ko",
    add_to_time: "Idagdag sa oras ko", adding: "Idinadagdag ang oras mo…", adding_lead: "Ilang segundo lang ito.",
    online_title: "Online ka na", left: "natitira", add_time: "Dagdag oras",
    slot_busy_retry: "May gumagamit", pause: "I-pause",
    paused_title: "Naka-pause ang oras mo", saved: "naka-save",
    paused_lead: "Offline ka muna. Hindi tumatakbo ang oras habang naka-pause.", resume: "Ituloy ang oras ko",
    check_later: "Para tingnan ang oras mo, buksan ang {addr} sa browser.", device: "Device mo: {mac}",
    blocked_lead: "Sobrang daming tap na walang barya. Puwede ka ulit mag-tap sa {m} min.",
    voucher_ok: "Naidagdag ang voucher: {time}.", hr: "oras", min: "min", day: "araw", days: "araw", days_and: "{d} at",
    unknown_device: "Hindi makilala ang phone mo. I-off at i-on ang Wi-Fi, tapos buksan ulit ang page na ito.",
    machine_offline: "Hindi maabot ang makina. Sinusubukan ulit…",
    machine_error: "Nagka-problema ang makina. Sinusubukan ulit…",
    internet_off: "Hindi pa naka-on ang internet. Pakisabi sa may-ari.",
    connect_failed: "Naka-save ang oras mo, pero hindi pa bumukas ang internet. Sinusubukan ulit…",
    busy: "May ibang naghuhulog ng barya. Sandali lang.",
    too_many_taps: "Sobrang daming tap na walang barya. Subukan ulit sa {m} min.",
    bad_code: "Hindi tama ang code. Tingnan at subukan ulit.",
    used_code: "Nagamit na ang voucher na iyan.",
    code_locked: "Sobrang daming maling code. Subukan ulit sa {m} min.",
    cannot_pause: "Kulang sa isang minuto ang natitira, kaya hindi ma-pause.",
    pause_failed: "Hindi ma-pause ngayon. Subukan ulit.", resume_failed: "Hindi maituloy ngayon. Subukan ulit.",
    not_paused: "Hindi naka-pause ang oras mo.",
  },
};

let lang = document.documentElement.dataset.defaultLang || "en";
try { lang = localStorage.getItem("piso-lang") || lang; } catch (e) { /* storage blocked in some captive sheets */ }
if (!STRINGS[lang]) lang = "en";

let state = null, fetchedAt = 0, timer = null, connecting = false, loading = false, windowTotal = 60;
let failedAt = 0;  // after a failed start, wait before retrying on our own

function t(key, vars) {
  let text = STRINGS[lang][key] || STRINGS.en[key] || key;
  for (const [k, v] of Object.entries(vars || {})) text = text.replace("{" + k + "}", v);
  return text;
}
function applyLanguage() {
  document.documentElement.lang = lang;
  for (const node of document.querySelectorAll("[data-t]")) node.textContent = t(node.dataset.t);
  $("voucher-code").placeholder = t("voucher_placeholder");
  $("b-lang").textContent = t("other_language");
  if (state) { renderRates(); render(); }
}

function show(view) { for (const v of VIEWS) $(v).hidden = v !== view; }
function notice(text, good) {
  const n = $("notice");
  n.textContent = text || "";
  n.className = good ? "notice good" : "notice";
  n.hidden = !text;
}
function peso(n) { return "₱" + Number(n || 0).toLocaleString("en-PH"); }
function dayWord(d) { return d + " " + (d === 1 ? t("day") : t("days")); }
function minutes(m) {
  m = Math.max(0, Math.round(m));
  if (m < 60) return m + " " + t("min");
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), r = m % 60;
  const parts = [];
  if (d) parts.push(dayWord(d));
  if (h) parts.push(h + " " + t("hr"));
  if (r) parts.push(r + " " + t("min"));
  return parts.join(" ");
}
function short(m) {  // compact form for the small rate cards
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), r = m % 60;
  if (d) return h ? d + "d " + h + "h" : dayWord(d);
  if (h) return r ? h + "h " + r + "m" : h + "h";
  return m + " min";
}
function setTimer(bigId, daysId, seconds) {
  // Over a day, show "3 days and" above the clock instead of a 70-hour clock.
  const s = Math.max(0, Math.floor(seconds)), d = Math.floor(s / 86400);
  $(bigId).textContent = clockText(d ? s % 86400 : s);
  $(daysId).textContent = d ? t("days_and", { d: dayWord(d) }) : "";
  $(daysId).hidden = !d;
}
function clockText(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = String(m).padStart(h ? 2 : 1, "0"), ss = String(sec).padStart(2, "0");
  return h ? h + ":" + mm + ":" + ss : mm + ":" + ss;
}
function since() { return Math.floor((Date.now() - fetchedAt) / 1000); }
function left(seconds) { return Math.max(0, seconds - since()); }
function inMinutes(seconds) { return Math.max(1, Math.ceil(seconds / 60)); }
function problem(body) {
  return t(body.error || "machine_error", { m: inMinutes(body.retry_after || 60) });
}

async function post(path, payload) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "X-Requested-With": "piso-portal", "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  let body = {};
  try { body = await r.json(); } catch (e) { /* keep empty */ }
  return { status: r.status, body };
}

function renderRates() {
  const list = $("rates");
  list.replaceChildren();
  for (const rate of state.rates) {
    const li = document.createElement("li");
    const b = document.createElement("b"); b.textContent = peso(rate.pesos);
    const s = document.createElement("span"); s.textContent = short(rate.minutes);
    li.append(b, s);
    if (rate.minutes > rate.pesos * state.minutes_per_peso) {
      li.className = "promo";
      const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = t("promo");
      li.append(tag);
    }
    list.append(li);
  }
}

function render() {
  if (!state) return;
  const w = state.window, session = state.session;
  $("device").textContent = t("device", { mac: state.mac });
  if (!state.internet_enabled) notice(t("internet_off"));
  if (connecting) { show("v-adding"); return; }
  if (w.state === "mine" && w.open) {
    const s = left(w.seconds_left);
    windowTotal = Math.max(windowTotal, w.seconds_left);
    $("window-left").textContent = s;
    $("window-ring").style.strokeDashoffset = String(RING * (1 - s / windowTotal));
    $("tally-pesos").textContent = w.pesos ? t("inserted", { p: peso(w.pesos) }) : t("no_coins_yet");
    $("tally-time").textContent = minutes(w.minutes);
    $("b-start").disabled = !(state.credit_minutes > 0) || !state.internet_enabled;
    $("b-start").textContent = session.active ? t("add_to_time") : t("start_time");
    show("v-insert");
    if (s === 0) load();  // the slot just closed: fetch the final tally
    return;
  }
  if (session.paused) {
    setTimer("paused-left", "paused-days", session.paused_seconds);
    show("v-paused");
    return;
  }
  if (session.active && left(session.seconds_left) > 0) {
    setTimer("online-left", "online-days", left(session.seconds_left));
    $("online-hint").textContent = t("check_later", { addr: state.portal_address });
    $("b-more").disabled = w.state === "busy";
    $("b-more-label").textContent = w.state === "busy" ? t("slot_busy_retry") : t("add_time");
    $("b-pause").disabled = left(session.seconds_left) < 60;
    show("v-online");
    return;
  }
  if (session.active) load();  // time just ran out
  if (w.state === "busy") {
    $("b-busy").textContent = t("free_in", { s: left(w.retry_after) });
    show("v-busy");
    if (left(w.retry_after) === 0) load();
    return;
  }
  const credit = $("b-use-credit");
  credit.hidden = !(state.credit_minutes > 0);
  credit.textContent = t("use_credit", { time: minutes(state.credit_minutes) });
  const blocked = left(state.insert_blocked_for || 0);
  $("b-insert").disabled = blocked > 0;
  $("start-lead").textContent = blocked > 0 ? t("blocked_lead", { m: inMinutes(blocked) })
    : session.expires ? t("lead_time_up") : t("lead_tap");
  show("v-start");
}

function nextPoll() {
  if (!state) return 3000;
  const w = state.window;
  if (w.state === "mine") return 1500;  // show coins as they land
  if (w.state === "busy") return 3000;
  return 10000;
}

async function load() {
  if (loading) return;
  loading = true;
  clearTimeout(timer);
  try {
    const r = await fetch("/api/state", { headers: { Accept: "application/json" } });
    const body = await r.json();
    if (!r.ok) {
      notice(problem(body));
    } else {
      state = body; fetchedAt = Date.now();
      if (state.internet_enabled && $("notice").className !== "notice good") notice("");
      renderRates();
      render();
      autoStart();
    }
  } catch (e) {
    notice(t("machine_offline"));
  } finally {
    loading = false;
  }
  timer = setTimeout(load, state ? nextPoll() : 4000);
}

// When the slot closes with coins inserted, start the time without waiting for a tap.
function autoStart() {
  const w = state.window;
  if (w.state === "mine" && !w.open && state.credit_minutes > 0 && state.internet_enabled
      && Date.now() - failedAt > 10000) start();
}

async function insertCoin() {
  notice("");
  windowTotal = 0;
  const { status, body } = await post("/api/insert-coin");
  if (status !== 200) notice(problem(body));
  load();
}

async function start() {
  if (connecting) return;
  connecting = true; render();
  const { status, body } = await post("/api/connect");
  connecting = false;
  if (status !== 200 && body.error !== "no_credit") {
    failedAt = Date.now();
    notice(t(body.error || "connect_failed"));
  }
  load();
}

async function simple(path, button) {
  button.disabled = true;
  notice("");
  const { status, body } = await post(path);
  button.disabled = false;
  if (status !== 200) notice(problem(body));
  load();
}

async function redeem(event) {
  event.preventDefault();
  const input = $("voucher-code");
  const code = input.value.trim();
  if (!code) { input.focus(); return; }
  notice("");
  const { status, body } = await post("/api/redeem", { code });
  if (status === 200) {
    input.value = "";
    $("voucher-form").hidden = true;
    notice(t("voucher_ok", { time: minutes(body.minutes) }), true);
  } else {
    notice(problem(body));
  }
  load();
}

$("b-insert").addEventListener("click", insertCoin);
$("b-more").addEventListener("click", insertCoin);
$("b-start").addEventListener("click", start);
$("b-use-credit").addEventListener("click", start);
$("b-pause").addEventListener("click", () => simple("/api/pause", $("b-pause")));
$("b-resume").addEventListener("click", () => simple("/api/resume", $("b-resume")));
$("b-voucher").addEventListener("click", () => {
  $("voucher-form").hidden = !$("voucher-form").hidden;
  if (!$("voucher-form").hidden) $("voucher-code").focus();
});
$("voucher-form").addEventListener("submit", redeem);
$("b-lang").addEventListener("click", () => {
  lang = lang === "en" ? "tl" : "en";
  try { localStorage.setItem("piso-lang", lang); } catch (e) { /* not saved; fine */ }
  applyLanguage();
});
setInterval(render, 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
applyLanguage();
load();
"""


def main() -> int:
    if not ADMIN_TOKEN or ADMIN_TOKEN.startswith("REPLACE_"):
        print("warning: PISOWIFI_ADMIN_TOKEN is not set; the portal can't reach the credit service", file=sys.stderr)
    create_app().run(host=BIND, port=PORT, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
