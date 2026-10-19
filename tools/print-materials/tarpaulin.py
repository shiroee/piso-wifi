"""Landscape 3:2 tarpaulin (5400 x 3600 px) with all six steps and the rates."""
import json
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

import config
from screens import price

HERE = Path(__file__).resolve().parent / "build"
OUT = Path(__file__).resolve().parents[2] / "docs" / "images" / "print"
NAME, SSID = config.NAME, config.SSID


def time_label(m):
    d, rest = divmod(m, 1440)
    h, mins = divmod(rest, 60)
    if d:
        return f"{d} day" + ("s" if d != 1 else "") + (f" {h} hr" if h else "")
    if h:
        return f"{h} hr" + ("s" if h != 1 else "") + (f" {mins} min" if mins else "")
    return f"{mins} min"


RATES = [(f"₱{a}", time_label(price(a))) for a in sorted({1, 5, 10, 20} | {p for p, _ in config.PROMOS})]
BEST = f"₱{config.BEST_VALUE}" if config.BEST_VALUE else None
BOXES = json.loads((HERE / "boxes.json").read_text())
W, H = 1800, 1200            # 3:2 landscape (e.g. 3 x 2 ft or 6 x 4 ft)
INNER = 198
SCALE = INNER / 360
BEZEL = 9

def tap(shot, selector, fx=0.5, fy=0.5, side="right"):
    x, y, w, h = BOXES[shot][selector]
    cx, cy = BEZEL + (x + w * fx) * SCALE, BEZEL + (y + h * fy) * SCALE
    return f'<div class="tap {side}" style="left:{cx:.0f}px;top:{cy:.0f}px"><i></i><i></i><b></b><span>TAP</span></div>'

STEPS = [
    dict(shot="wifi", tap=("#piso", 0.86, 0.5, "left"), title="Join the Wi-Fi", sub=f"Choose <b>{SSID}</b>", tl="Kumonekta sa Wi-Fi"),
    dict(shot="start", tap=None, title="Page opens", sub='Or go to <span class="url">http://piso.wifi</span>', tl="Lalabas ang page"),
    dict(shot="start", tap=("#b-insert", 0.66, 0.7), title="Tap the coin", sub="The coin slot lights up", tl="I-tap ang barya"),
    dict(shot="window", tap=None, slot=True, title="Drop coins", sub=f"{config.WINDOW_SECONDS} seconds · each coin adds time", tl="Ihulog ang barya"),
    dict(shot="insert", tap=("#b-start", 0.6, 0.5), title="Start my time", sub="Tap when you're done", tl="Simulan ang oras"),
    dict(shot="online", tap=None, title="You're online", sub="Pause or add time anytime", tl="Online ka na!"),
]

HEX = ("data:image/svg+xml;utf8," +
       "<svg xmlns='http://www.w3.org/2000/svg' width='56' height='97' viewBox='0 0 56 97'>"
       "<path d='M28 0 56 16v32L28 64 0 48V16zM28 64v33M0 48 -28 64M56 48l28 16' fill='none' stroke='%2300E5FF' stroke-opacity='.09' stroke-width='1.2'/></svg>")

SLOT = '''<svg class="slot" viewBox="0 0 120 92" aria-hidden="true">
  <defs><radialGradient id="g" cx="34%" cy="28%" r="80%"><stop offset="0" stop-color="#FFE29A"/><stop offset=".4" stop-color="#F5B83D"/><stop offset="1" stop-color="#C98408"/></radialGradient>
  <filter id="b" x="-50%" y="-300%" width="200%" height="700%"><feGaussianBlur stdDeviation="4"/></filter></defs>
  <rect x="10" y="44" width="100" height="44" rx="6" fill="#0A0B22" stroke="#00E5FF" stroke-width="2.5"/>
  <rect x="26" y="60" width="68" height="10" rx="5" fill="#00E5FF" filter="url(#b)"/>
  <rect x="26" y="60" width="68" height="10" rx="5" fill="#B8FBFF"/>
  <rect x="31" y="63" width="58" height="4" rx="2" fill="#050614"/>
  <path d="M60 30v10M54 35l6 6 6-6" fill="none" stroke="#FF2BD6" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="60" cy="15" r="13" fill="url(#g)"/>
  <text x="60" y="21" text-anchor="middle" font-family="DejaVu Sans" font-weight="800" font-size="16" fill="#3A2600">₱</text>
</svg>'''

CSS = f'''
:root {{ --bg:#050614; --panel:#0D1033; --panel-2:#141A4A; --text:#F4F6FF; --muted:#AEB6E8;
  --cyan:#00E5FF; --magenta:#FF2BD6; --violet:#7B5CFF; --lime:#A6FF3D;
  --gold:#F5B83D; --gold-hi:#FFE29A; --gold-deep:#C98408; --gold-ink:#3A2600; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ width:{W}px; height:{H}px; overflow:hidden; color:var(--text); font-family:"DejaVu Sans", system-ui, sans-serif;
  background:
    radial-gradient(45% 55% at 8% 0%, rgba(123,92,255,.45), transparent 70%),
    radial-gradient(40% 50% at 95% 10%, rgba(255,43,214,.28), transparent 70%),
    radial-gradient(60% 50% at 50% 110%, rgba(0,229,255,.25), transparent 70%),
    url("{HEX}"), var(--bg); }}
.mono {{ font-family:"DejaVu Sans Mono", monospace; letter-spacing:.18em; }}
.wrap {{ position:relative; height:100%; padding:28px 44px 20px; display:flex; flex-direction:column; }}
/* corner HUD brackets */
.wrap::before, .wrap::after {{ content:""; position:absolute; width:84px; height:84px; border:4px solid var(--cyan); opacity:.8; }}
.wrap::before {{ left:8px; top:8px; border-right:0; border-bottom:0; }}
.wrap::after {{ right:8px; bottom:8px; border-left:0; border-top:0; border-color:var(--magenta); }}

header {{ display:flex; align-items:center; gap:26px; }}
.coin {{ border-radius:50%; display:grid; place-items:center; color:var(--gold-ink); font-weight:850; flex:none;
  background:radial-gradient(circle at 34% 28%, var(--gold-hi) 0, var(--gold) 38%, var(--gold-deep) 88%);
  box-shadow: inset 0 0 0 6px rgba(255,255,255,.25), inset 0 -8px 16px rgba(0,0,0,.25), 0 0 40px rgba(245,184,61,.55); }}
header .coin {{ width:108px; height:108px; font-size:58px; }}
.kicker {{ font-size:20px; color:var(--cyan); font-weight:700; }}
h1 {{ font-size:74px; line-height:.95; font-weight:900; font-style:italic; text-transform:uppercase; letter-spacing:.01em; margin-top:6px;
  background:linear-gradient(90deg, #FFFFFF 0%, #B9F7FF 45%, #FFB3F2 100%); -webkit-background-clip:text; background-clip:text; color:transparent;
  filter: drop-shadow(4px 4px 0 rgba(255,43,214,.85)) drop-shadow(0 0 22px rgba(0,229,255,.45)); }}
.tagline {{ font-size:26px; margin-top:10px; color:var(--muted); }}
.tagline b {{ color:var(--lime); }}
.hud {{ margin-left:auto; display:grid; gap:12px; justify-items:end; }}
.chip {{ display:flex; align-items:center; gap:12px; padding:12px 22px; font-size:22px; font-weight:800; color:var(--bg); background:var(--cyan);
  clip-path:polygon(16px 0,100% 0,calc(100% - 16px) 100%,0 100%); }}
.chip.alt {{ background:var(--magenta); color:#fff; }}
.chip small {{ font:700 14px "DejaVu Sans Mono",monospace; letter-spacing:.16em; opacity:.85; }}

.warn {{ margin-top:18px; padding:3px; background:linear-gradient(90deg,var(--magenta),var(--violet),var(--cyan));
  clip-path:polygon(18px 0,100% 0,100% calc(100% - 18px),calc(100% - 18px) 100%,0 100%,0 18px); }}
.warn div {{ display:flex; align-items:center; gap:22px; padding:14px 26px; background:#140A2E; font-size:28px;
  clip-path:polygon(17px 0,100% 0,100% calc(100% - 17px),calc(100% - 17px) 100%,0 100%,0 17px); }}
.warn .mono {{ font-size:20px; color:var(--magenta); font-weight:800; }}
.warn b {{ font-weight:900; }}
.warn span.tl {{ color:#FFC9F3; font-size:22px; margin-left:auto; }}

.steps {{ position:relative; display:grid; grid-template-columns:repeat(6,1fr); gap:24px; margin-top:20px; flex:1; }}
.frame {{ position:relative; padding:3px; display:flex; min-width:0;
  background:linear-gradient(165deg, var(--cyan), rgba(0,229,255,.12) 38%, rgba(123,92,255,.25) 62%, var(--magenta));
  clip-path:polygon(22px 0,100% 0,100% calc(100% - 22px),calc(100% - 22px) 100%,0 100%,0 22px); }}
.step {{ flex:1; display:flex; flex-direction:column; align-items:center; min-width:0; padding:16px 12px 16px;
  background:linear-gradient(180deg, rgba(20,26,74,.97), rgba(9,11,38,.97));
  clip-path:polygon(21px 0,100% 0,100% calc(100% - 21px),calc(100% - 21px) 100%,0 100%,0 21px); }}
.lvl {{ width:100%; display:flex; justify-content:space-between; align-items:center; }}
.lvl .mono {{ font-size:15px; color:var(--cyan); font-weight:800; }}
.lvl .num {{ font-size:58px; font-weight:900; font-style:italic; line-height:1; color:transparent; -webkit-text-stroke:2.5px var(--cyan);
  filter:drop-shadow(0 0 12px rgba(0,229,255,.6)); }}
.phone {{ position:relative; width:{INNER + 2*BEZEL}px; border-radius:32px; background:#03040C; padding:{BEZEL}px; margin:10px 0 14px;
  box-shadow:0 0 0 2px rgba(0,229,255,.65), 0 0 34px rgba(0,229,255,.28), 0 22px 40px -18px rgba(0,0,0,.9); }}
.phone img {{ display:block; width:{INNER}px; border-radius:24px; }}
.tap {{ position:absolute; width:0; height:0; }}
.tap i {{ position:absolute; left:-32px; top:-32px; width:64px; height:64px; border-radius:50%; border:4px solid var(--magenta); box-shadow:0 0 18px var(--magenta); }}
.tap i + i {{ left:-46px; top:-46px; width:92px; height:92px; border-width:2.5px; border-color:rgba(255,43,214,.55); box-shadow:none; }}
.tap b {{ position:absolute; left:-11px; top:-11px; width:22px; height:22px; border-radius:50%; background:#fff; box-shadow:0 0 0 5px rgba(255,43,214,.7), 0 0 18px #fff; }}
.tap span {{ position:absolute; left:30px; top:-50px; background:var(--magenta); color:#fff; font:800 15px "DejaVu Sans Mono",monospace; letter-spacing:.16em;
  padding:4px 11px; clip-path:polygon(7px 0,100% 0,calc(100% - 7px) 100%,0 100%); }}
.tap.left span {{ left:auto; right:30px; }}
.slot {{ position:absolute; right:-6px; bottom:64px; width:84px; filter:drop-shadow(0 0 12px rgba(0,229,255,.6)); }}
.step h2 {{ font-size:27px; font-weight:900; font-style:italic; text-transform:uppercase; text-align:center; line-height:1.05; }}
.step .sub {{ font-size:17px; color:var(--text); text-align:center; margin-top:8px; line-height:1.3; }}
.step .tl {{ font-size:16px; color:var(--muted); text-align:center; margin-top:4px; }}
.url {{ font-weight:900; color:var(--bg); background:var(--cyan); padding:0 5px; white-space:nowrap; }}
.arrow {{ position:absolute; top:40%; width:22px; height:38px; z-index:3; filter:drop-shadow(0 0 8px rgba(0,229,255,.8)); }}

.shop {{ margin-top:18px; padding:3px; background:linear-gradient(90deg,var(--gold),var(--magenta) 45%,var(--violet) 70%,var(--cyan));
  clip-path:polygon(20px 0,100% 0,100% calc(100% - 20px),calc(100% - 20px) 100%,0 100%,0 20px); }}
.shop-in {{ display:flex; align-items:center; gap:22px; padding:16px 22px; background:#080A24;
  clip-path:polygon(19px 0,100% 0,100% calc(100% - 19px),calc(100% - 19px) 100%,0 100%,0 19px); }}
.shop-title b {{ display:block; font-size:34px; font-weight:900; font-style:italic; text-transform:uppercase; color:var(--gold); line-height:1;
  filter:drop-shadow(0 0 14px rgba(245,184,61,.55)); }}
.shop-title span {{ font-size:16px; color:var(--muted); }}
.rates {{ flex:1; display:grid; grid-template-columns:repeat(11,1fr); gap:8px; }}
.rate {{ display:flex; flex-direction:column; align-items:center; gap:6px; padding:10px 4px 9px; background:var(--panel-2);
  border:1.5px solid rgba(0,229,255,.25); clip-path:polygon(10px 0,100% 0,100% calc(100% - 10px),calc(100% - 10px) 100%,0 100%,0 10px); }}
.rate .coin {{ width:54px; height:54px; font-size:17px; box-shadow: inset 0 0 0 4px rgba(255,255,255,.25), 0 0 16px rgba(245,184,61,.4); }}
.rate b {{ font-size:19px; white-space:nowrap; }}
.rate.best {{ border-color:var(--lime); background:linear-gradient(180deg,#1C3A1A,#0E1F14); }}
.rate.best em {{ font:800 11px "DejaVu Sans Mono",monospace; letter-spacing:.1em; font-style:normal; color:var(--bg); background:var(--lime); padding:2px 6px; margin-top:-2px; }}

footer {{ display:flex; justify-content:space-between; align-items:center; margin-top:12px; font-size:20px; color:var(--muted); }}
footer b {{ color:#fff; }}
.bar {{ display:flex; gap:7px; align-items:center; }}
.bar i {{ width:44px; height:12px; background:var(--lime); transform:skewX(-22deg); box-shadow:0 0 12px rgba(166,255,61,.55); }}
.bar .mono {{ font-size:15px; color:var(--cyan); margin-right:8px; }}
'''

ARROW = ('<svg class="arrow" viewBox="0 0 20 34" aria-hidden="true"><path d="M3 3l13 14L3 31" fill="none" stroke="#00E5FF" '
         'stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/></svg>')

def step_html(i, s):
    shot = HERE / f"{s['shot']}.png"
    t = tap(s["shot"], *s["tap"]) if s.get("tap") else ""
    slot = SLOT if s.get("slot") else ""
    return f'''<div class="frame"><section class="step">
      <div class="lvl"><span class="mono">LEVEL</span><span class="num">0{i}</span></div>
      <div class="phone"><img src="file://{shot}">{t}{slot}</div>
      <h2>{s["title"]}</h2><p class="sub">{s["sub"]}</p><p class="tl">{s["tl"]}</p></section></div>'''

def html():
    steps = "".join(step_html(i, s) for i, s in enumerate(STEPS, start=1))
    # Arrows sit in the 24px gaps between the six columns.
    steps += "".join(ARROW.replace('class="arrow"', f'class="arrow" style="left:calc({k} * (100% - 120px) / 6 + {k - 1} * 24px + 1px)"')
                     for k in range(1, 6))
    rates = "".join(
        f'<div class="rate{" best" if c == BEST else ""}"><div class="coin">{c}</div><b>{t}</b>'
        f'{"<em>BEST VALUE</em>" if c == BEST else ""}</div>' for c, t in RATES)
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><style>{CSS}</style></head><body><div class="wrap">
  <header><div class="coin">₱</div>
    <div><p class="kicker mono">HIGH-SPEED INTERNET · COIN OPERATED</p><h1>{NAME}</h1>
      <p class="tagline">Insert coin. Level up your connection. <b>₱1 = {config.MINUTES_PER_PESO} minutes</b></p></div>
    <div class="hud"><span class="chip"><small>WI-FI</small>{SSID}</span><span class="chip alt"><small>CHECK TIME</small>{config.ADDRESS}</span></div>
  </header>
  <div class="warn"><div><span class="mono">⚠ WARNING</span><b>Tap the coin on your phone first, then insert your coins.</b>
    <span class="tl">I-tap muna ang barya bago maghulog</span></div></div>
  <div class="steps">{steps}</div>
  <div class="shop"><div class="shop-in"><div class="shop-title"><b>Coin shop</b><span>Rates · Presyo</span></div><div class="rates">{rates}</div></div></div>
  <footer><div class="bar"><span class="mono">HOW TO PLAY</span><i></i><i></i><i></i><i></i><i></i><i></i></div>
    <span>Need help? <b>Ask the owner.</b> · Magtanong sa may-ari.</span></footer>
</div></body></html>'''

async def main():
    page_file = HERE / "tarp.html"
    page_file.write_text(html())
    OUT.mkdir(parents=True, exist_ok=True)
    png = OUT / "tarpaulin.png"
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": W, "height": H}, device_scale_factor=3)
        pg = await ctx.new_page()
        await pg.goto(f"file://{page_file}")
        await pg.wait_for_timeout(400)
        print("footer bottom", round(await pg.evaluate("document.querySelector('footer').getBoundingClientRect().bottom")), "of", H)
        await pg.screenshot(path=str(png))
        await b.close()
    im = Image.open(png)
    print(im.size)
    im.convert("RGB").save(OUT / "tarpaulin.jpg", quality=92, dpi=(150, 150))
    png.unlink()  # print shops want the JPG, and it keeps the repository small
