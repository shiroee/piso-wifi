"""Serve the real portal page with sample data and screenshot the screens the
print materials show. Writes build/*.png and build/boxes.json (tap positions)."""

import json
import sys
import tempfile
import threading
from pathlib import Path

from playwright.async_api import async_playwright
from werkzeug.serving import make_server

import config

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BUILD = HERE / "build"
sys.path.insert(0, str(REPO / "portal"))
sys.path.insert(0, str(REPO / "app"))

import creditd  # noqa: E402  (for the same pricing as the machine)
import portal  # noqa: E402


def price(pesos):
    return creditd.price(pesos, config.MINUTES_PER_PESO, config.PROMOS)


def rate_card():
    amounts = sorted({1, 5, 10, 20} | {p for p, _ in config.PROMOS})
    return [{"pesos": a, "minutes": price(a)} for a in amounts]


def scenes():
    off = {"active": False, "expires": 0, "seconds_left": 0, "paused": False, "paused_seconds": 0}
    base = {"ok": True, "mac": "00:1f:fb:bb:25:bc", "minutes_per_peso": config.MINUTES_PER_PESO,
            "internet_enabled": True, "insert_blocked_for": 0, "rates": rate_card()}
    five = price(5)
    return {
        "start": {**base, "credit_minutes": 0, "session": off, "window": {"state": "free"}},
        "window": {**base, "credit_minutes": 0, "session": off,
                   "window": {"state": "mine", "open": True, "seconds_left": config.WINDOW_SECONDS - 2, "pesos": 0, "minutes": 0}},
        "insert": {**base, "credit_minutes": five, "session": off,
                   "window": {"state": "mine", "open": True, "seconds_left": 41, "pesos": 5, "minutes": five}},
        "online": {**base, "credit_minutes": 0, "session": {**off, "active": True, "seconds_left": five * 60 - 19},
                   "window": {"state": "free"}},
    }


class Fake:
    def __init__(self, scene):
        self.scene = scene

    def call(self, method, path, body=None):
        if path == "/api/appearance":
            return 200, {"title": config.NAME}
        return 200, dict(self.scene)


SHOTS = [("wifi", None, ["#piso"]), ("start", "start", ["#b-insert"]), ("window", "window", []),
         ("insert", "insert", ["#b-start"]), ("online", "online", ["#b-more", "#b-pause"])]


async def capture():
    BUILD.mkdir(exist_ok=True)
    arp = Path(tempfile.mkdtemp()) / "arp"
    arp.write_text("IP address HW type Flags HW address Mask Device\n127.0.0.1 0x1 0x2 00:1f:fb:bb:25:bc * enx0\n")
    servers, ports = [], {}
    for name, scene in scenes().items():
        app = portal.create_app(Fake(scene), arp, config.NAME, address=config.ADDRESS)
        srv = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        ports[name] = srv.server_port
    wifi = BUILD / "wifi_list.html"
    wifi.write_text((HERE / "wifi_list.html").read_text().replace("__SSID__", config.SSID))
    boxes = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for name, scene, selectors in SHOTS:
            ctx = await browser.new_context(viewport={"width": 360, "height": 760}, color_scheme="dark", device_scale_factor=3)
            page = await ctx.new_page()
            await page.goto(wifi.as_uri() if scene is None else f"http://127.0.0.1:{ports[scene]}/")
            await page.wait_for_timeout(1500)
            boxes[name] = {}
            for s in selectors:
                box = await page.locator(s).bounding_box()
                boxes[name][s] = [box["x"], box["y"], box["width"], box["height"]]
            await page.screenshot(path=str(BUILD / f"{name}.png"))
            await ctx.close()
        await browser.close()
    for srv in servers:
        srv.shutdown()
    (BUILD / "boxes.json").write_text(json.dumps(boxes))
    print("screens captured:", ", ".join(boxes))
